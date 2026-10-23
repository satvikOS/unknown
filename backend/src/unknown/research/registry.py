"""Pre-registered experiment registry and complete trial registry (CAP-RES-10; RES-01, RES-04; BR-DAT-07).

Storage: one SQLite file, by default ``~/.unknown/research/experiments.sqlite``
(``UNKNOWN_HOME`` honoured; the path is injectable). It is research-side only;
the money core never reads it (ADR-0015).

Rules enforced here (the database triggers make the tables append-only for
every caller, agent or not; there is no delete or edit API):

* A strategy spec is registered by content hash before use.
* An experiment exists only as a pre-registration: hypothesis, information
  clock, execution rule, cost model, benchmark, parameter search space, search
  budget, rejection criteria and the untouched-holdout plan, hashed together
  with its registration time. Trials cannot exist without one.
* Every run is a trial, written RUNNING *before* it executes and finished as
  SUCCEEDED, FAILED (exception), ABORTED (interrupted, or found RUNNING by
  ``reap_running`` after a crash) or ARTIFACT_MISSING. Failures count.
* Development trials must lie inside the pre-registered search space (an
  amendment extends it visibly) and inside the budget; only the OWNER may
  raise a budget (AUT-04). WALK_FORWARD, SENSITIVITY, STRESS and REPLAY
  trials may only re-run a parameter set that already has a DEVELOPMENT
  trial, so no search can happen outside the counted segment.
* ``family_trial_count`` (M for the DSR) is read from here, all statuses.
* The untouched holdout is consumed at most once per experiment, only by the
  OWNER or the SYSTEM evaluator (never an AGENT); every attempt, granted or
  refused (including one refused because its spec or manifest is invalid), is
  logged.
* The holdout lock binds trials, not only views (review fix 2026-10-22): every
  trial manifest must name its datasets with the last session each may read
  (``until``). A non-HOLDOUT trial whose datasets reach the holdout start is
  refused, except a declared BASELINE evaluated under this experiment's
  GRANTED holdout access (``extra.holdout_access_id``). ``finish_trial``
  refuses returns dated after the manifest's ``until`` or inside the holdout
  without that grant; such a trial is recorded FAILED (it still counts).
"""
from __future__ import annotations

import json
import os
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from ..domain.clock import Clock, SystemClock
from ..domain.fixed import Fixed
from ..domain.principal import Principal, Role
from .bars import HoldoutGrant, issue_grant
from .common import AuthorityError, HoldoutError, RegistryError, plain, stable_dumps, stable_hash
from .spec import StrategySpec, parse_spec

REGISTRY_SCHEMA_VERSION = 1
TRIAL_STATUSES = ("RUNNING", "SUCCEEDED", "FAILED", "ABORTED", "ARTIFACT_MISSING")
SEGMENTS = ("DEVELOPMENT", "WALK_FORWARD", "SENSITIVITY", "STRESS", "BASELINE", "REPLAY", "HOLDOUT")
SELECTION_SEGMENT = "DEVELOPMENT"
# Segments that re-examine an already-counted parameter set (never a search): review fix 2026-10-22.
FOLLOW_UP_SEGMENTS = ("WALK_FORWARD", "SENSITIVITY", "STRESS", "REPLAY")


def default_path() -> Path:
    root = Path(os.environ.get("UNKNOWN_HOME", Path.home() / ".unknown")).expanduser()
    return root / "research" / "experiments.sqlite"


_IMMUTABLE = ("strategy_spec", "experiment", "amendment", "manifest", "holdout_access", "report",
              "artifact", "evidence", "protocol", "selection_policy", "shadow_envelope", "evidence_deletion",
              "evidence_deletion_event", "transition")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_spec (
  spec_hash TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL, family TEXT NOT NULL,
  body TEXT NOT NULL, registered_at_ns INTEGER NOT NULL, registered_by TEXT NOT NULL, registered_role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment (
  experiment_id TEXT PRIMARY KEY, spec_hash TEXT NOT NULL REFERENCES strategy_spec(spec_hash),
  prereg TEXT NOT NULL, prereg_hash TEXT NOT NULL UNIQUE, created_at_ns INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_role TEXT NOT NULL, budget INTEGER NOT NULL,
  holdout_start TEXT NOT NULL, holdout_end TEXT NOT NULL, fixture INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS amendment (
  amendment_id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL REFERENCES experiment(experiment_id),
  body TEXT NOT NULL, body_hash TEXT NOT NULL, created_at_ns INTEGER NOT NULL, created_by TEXT NOT NULL, created_role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manifest (manifest_hash TEXT PRIMARY KEY, body TEXT NOT NULL, created_at_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trial (
  trial_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiment(experiment_id), seq INTEGER NOT NULL,
  segment TEXT NOT NULL, strategy_id TEXT NOT NULL, spec_hash TEXT NOT NULL REFERENCES strategy_spec(spec_hash),
  params TEXT NOT NULL, params_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL REFERENCES manifest(manifest_hash),
  label TEXT NOT NULL, author TEXT NOT NULL, author_role TEXT NOT NULL, started_at_ns INTEGER NOT NULL,
  status TEXT NOT NULL, finished_at_ns INTEGER, results_hash TEXT, metrics TEXT, returns TEXT, returns_hash TEXT, error TEXT,
  UNIQUE (experiment_id, seq));
CREATE INDEX IF NOT EXISTS trial_family ON trial(experiment_id, segment);
CREATE TABLE IF NOT EXISTS holdout_access (
  access_id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL REFERENCES experiment(experiment_id),
  artifact_hash TEXT NOT NULL, outcome TEXT NOT NULL CHECK (outcome IN ('GRANTED','REFUSED')), trial_id TEXT,
  principal TEXT NOT NULL, role TEXT NOT NULL, at_ns INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS holdout_once ON holdout_access(experiment_id) WHERE outcome = 'GRANTED';
CREATE TABLE IF NOT EXISTS report (
  report_id TEXT PRIMARY KEY, experiment_id TEXT, kind TEXT NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL,
  created_at_ns INTEGER NOT NULL, fixture INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS artifact (
  artifact_hash TEXT PRIMARY KEY, spec_hash TEXT NOT NULL REFERENCES strategy_spec(spec_hash), strategy_id TEXT NOT NULL,
  body TEXT NOT NULL, author TEXT NOT NULL, author_role TEXT NOT NULL, registered_at_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, artifact_hash TEXT NOT NULL REFERENCES artifact(artifact_hash), kind TEXT NOT NULL,
  payload TEXT NOT NULL, payload_hash TEXT NOT NULL, refs TEXT NOT NULL, protocol_hash TEXT,
  evaluation_started_at_ns INTEGER NOT NULL, passed INTEGER NOT NULL, created_at_ns INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS protocol (
  protocol_hash TEXT PRIMARY KEY, body TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS selection_policy (
  policy_id TEXT PRIMARY KEY, body TEXT NOT NULL, body_hash TEXT NOT NULL, approved_by TEXT NOT NULL,
  approved_at_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_envelope (
  envelope_id TEXT PRIMARY KEY, body TEXT NOT NULL, body_hash TEXT NOT NULL, approved_by TEXT NOT NULL,
  approved_at_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_deletion (
  dataset TEXT PRIMARY KEY, deleted_at_ns INTEGER NOT NULL, reason TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL,
  recorded_by TEXT NOT NULL, recorded_role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_deletion_event (
  event_id TEXT PRIMARY KEY, dataset TEXT NOT NULL, cause TEXT NOT NULL, reason TEXT NOT NULL,
  deleted_at_ns INTEGER NOT NULL, scope TEXT NOT NULL, observations INTEGER NOT NULL, raw_objects TEXT NOT NULL,
  recorded_at_ns INTEGER NOT NULL, recorded_by TEXT NOT NULL, recorded_role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transition (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_hash TEXT NOT NULL REFERENCES artifact(artifact_hash),
  from_status TEXT, to_status TEXT NOT NULL, at_ns INTEGER NOT NULL, principal TEXT NOT NULL, role TEXT NOT NULL,
  evidence_ids TEXT NOT NULL, approval TEXT, slot TEXT, incumbent_hash TEXT, reason TEXT NOT NULL,
  prev_record_hash TEXT NOT NULL, record_hash TEXT NOT NULL);
"""


def _triggers() -> str:
    out = []
    for t in _IMMUTABLE:
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
    out.append("CREATE TRIGGER IF NOT EXISTS trial_no_delete BEFORE DELETE ON trial "
               "BEGIN SELECT RAISE(ABORT, 'trials are never deleted'); END;")
    immutable_cols = ("trial_id", "experiment_id", "seq", "segment", "strategy_id", "spec_hash", "params", "params_hash",
                      "manifest_hash", "label", "author", "author_role", "started_at_ns")
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in immutable_cols)
    out.append("CREATE TRIGGER IF NOT EXISTS trial_finish_once BEFORE UPDATE ON trial "
               f"WHEN OLD.status <> 'RUNNING' OR NEW.status = 'RUNNING' OR {changed} "
               "BEGIN SELECT RAISE(ABORT, 'a trial is finished exactly once and its identity never changes'); END;")
    return "\n".join(out)


def _role_name(p: Principal) -> str:
    return p.role.value


@dataclass
class TrialHandle:
    registry: "ResearchRegistry"
    trial_id: str
    finished: bool = False
    grant: HoldoutGrant | None = None

    def succeed(self, *, metrics: dict, returns: list[tuple[date, float]] | None = None, results_hash: str | None = None) -> None:
        try:
            self.registry.finish_trial(self.trial_id, status="SUCCEEDED", metrics=metrics, returns=returns,
                                       results_hash=results_hash)
        except HoldoutError:
            self.finished = True      # finish_trial already recorded the trial FAILED (holdout leak)
            raise
        self.finished = True


class ResearchRegistry:
    def __init__(self, path: str | Path | None = None, *, clock: Clock | None = None):
        self.path = Path(path) if path is not None else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(_triggers())
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(REGISTRY_SCHEMA_VERSION),))

    @classmethod
    def open_readonly(cls, path: str | Path) -> "ResearchRegistry":
        """Read-only view for API routes: no DDL, no writes (SQLite ``mode=ro``); raises FileNotFoundError if absent."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        self = cls.__new__(cls)
        self.path = p
        self.clock = SystemClock()
        self.conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        return self

    def schema_version(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        self.conn.close()

    def now(self) -> int:
        return self.clock.now_ns()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ specs
    def register_spec(self, spec: StrategySpec, principal: Principal) -> str:
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot register research specs")
        row = self.conn.execute("SELECT spec_hash FROM strategy_spec WHERE spec_hash=?", (spec.spec_hash,)).fetchone()
        if row:
            return spec.spec_hash
        self.conn.execute("INSERT INTO strategy_spec VALUES (?,?,?,?,?,?,?,?)",
                          (spec.spec_hash, spec.strategy_id, spec.version, spec.family, stable_dumps(spec.body),
                           self.now(), principal.id, _role_name(principal)))
        return spec.spec_hash

    def spec(self, spec_hash: str) -> StrategySpec:
        row = self.conn.execute("SELECT body FROM strategy_spec WHERE spec_hash=?", (spec_hash,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown spec {spec_hash}", "UNKNOWN_SPEC")
        spec = parse_spec(json.loads(row["body"]))
        if spec.spec_hash != spec_hash:
            raise RegistryError(f"stored spec {spec_hash} no longer hashes to its key", "SPEC_TAMPERED")
        return spec

    # ------------------------------------------------------ experiments
    def preregister(self, experiment_id: str, spec: StrategySpec, *, search_space: dict[str, list[str]], budget: int,
                    rejection_criteria: list[str], benchmark: str, principal: Principal, fixture: bool,
                    notes: str = "") -> dict:
        """File the pre-registration (hashed with its registration time) before any trial touches evaluation data."""
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot pre-register experiments")
        if not isinstance(budget, int) or not 1 <= budget <= 100_000:
            raise RegistryError("budget must be an integer in [1, 100000]", "INVALID_BUDGET")
        if not rejection_criteria or not all(isinstance(c, str) and c.strip() for c in rejection_criteria):
            raise RegistryError("at least one rejection criterion is required", "INVALID_PREREGISTRATION")
        self._check_space(spec, search_space)
        if self.conn.execute("SELECT 1 FROM experiment WHERE experiment_id=?", (experiment_id,)).fetchone():
            raise RegistryError(f"experiment {experiment_id} already pre-registered", "EXPERIMENT_EXISTS")
        self.register_spec(spec, principal)
        now = self.now()
        body = {"experiment_id": experiment_id, "spec_hash": spec.spec_hash, "strategy_id": spec.strategy_id,
                "hypothesis": spec.body["hypothesis"], "universe": spec.body["universe"],
                "information_clock": spec.execution["decision_clock_utc"] + " UTC, bars visible when available_ns <= decision",
                "execution_rule": spec.body["rules"]["execution"], "cost_model": spec.body["costs"],
                "benchmark": benchmark, "search_space": {k: list(v) for k, v in sorted(search_space.items())},
                "budget": budget, "rejection_criteria": list(rejection_criteria),
                "development": spec.body["validation_plan"]["development"],
                "holdout_plan": {**spec.body["validation_plan"]["untouched_holdout"], "consumptions_allowed": 1},
                "walk_forward": spec.body["validation_plan"]["walk_forward"], "selection_metric": spec.walk_forward["selection_metric"],
                "fixture": fixture, "notes": notes, "registered_at_ns": now, "registered_by": principal.id}
        h = stable_hash(body)
        self.conn.execute("INSERT INTO experiment VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (experiment_id, spec.spec_hash, stable_dumps(body), h, now, principal.id, _role_name(principal),
                           budget, spec.holdout[0].isoformat(), spec.holdout[1].isoformat(), int(fixture)))
        return {**body, "prereg_hash": h}

    @staticmethod
    def _check_space(spec: StrategySpec, space: dict[str, list[str]]) -> None:
        if not isinstance(space, dict) or not space:
            raise RegistryError("search space must name at least one parameter", "INVALID_PREREGISTRATION")
        for name, values in space.items():
            if name not in spec.parameter_domain:
                raise RegistryError(f"search space parameter {name} is not declared in the spec", "OUTSIDE_DOMAIN")
            if not isinstance(values, list) or not values or len(set(values)) != len(values):
                raise RegistryError(f"search space {name} must list distinct values", "INVALID_PREREGISTRATION")
            for v in values:
                try:
                    spec.with_parameters(**{name: v})
                except Exception as exc:
                    raise RegistryError(f"search space value {name}={v} outside the spec domain: {exc}", "OUTSIDE_DOMAIN") from None

    def experiment(self, experiment_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM experiment WHERE experiment_id=?", (experiment_id,)).fetchone()
        if row is None:
            raise RegistryError(f"experiment {experiment_id} has no pre-registration", "NO_PREREGISTRATION")
        out = dict(row)
        out["prereg"] = json.loads(row["prereg"])
        if stable_hash(out["prereg"]) != row["prereg_hash"]:
            raise RegistryError(f"pre-registration of {experiment_id} does not match its hash", "PREREG_TAMPERED")
        return out

    def experiments(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT experiment_id, spec_hash, prereg_hash, created_at_ns, created_by, created_role, budget, holdout_start, "
            "holdout_end, fixture FROM experiment ORDER BY created_at_ns, experiment_id")]

    def amend(self, experiment_id: str, *, principal: Principal, reason: str, extend_search_space: dict[str, list[str]] | None = None,
              budget: int | None = None) -> dict:
        exp = self.experiment(experiment_id)
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot amend experiments")
        if budget is not None:
            if principal.role is not Role.OWNER:
                raise AuthorityError("only the owner may change a search budget (AUT-04)")
            if not isinstance(budget, int) or budget < self.trial_count(experiment_id):
                raise RegistryError("a budget cannot be set below the trials already run", "INVALID_BUDGET")
        if extend_search_space:
            spec = self.spec(exp["spec_hash"])
            self._check_space(spec, extend_search_space)
        if not reason.strip() or (budget is None and not extend_search_space):
            raise RegistryError("an amendment needs a reason and a change", "INVALID_AMENDMENT")
        body = {"experiment_id": experiment_id, "reason": reason, "extend_search_space": extend_search_space or {},
                "budget": budget, "at_ns": self.now(), "by": principal.id}
        h = stable_hash(body)
        self.conn.execute("INSERT INTO amendment(experiment_id, body, body_hash, created_at_ns, created_by, created_role) "
                          "VALUES (?,?,?,?,?,?)", (experiment_id, stable_dumps(body), h, body["at_ns"], principal.id,
                                                   _role_name(principal)))
        return {**body, "body_hash": h}

    def amendments(self, experiment_id: str) -> list[dict]:
        return [{**json.loads(r["body"]), "body_hash": r["body_hash"], "amendment_id": r["amendment_id"]}
                for r in self.conn.execute("SELECT * FROM amendment WHERE experiment_id=? ORDER BY amendment_id", (experiment_id,))]

    def search_space(self, experiment_id: str) -> dict[str, list[str]]:
        space = {k: list(v) for k, v in self.experiment(experiment_id)["prereg"]["search_space"].items()}
        for a in self.amendments(experiment_id):
            for k, vals in a["extend_search_space"].items():
                space.setdefault(k, [])
                space[k].extend(v for v in vals if v not in space[k])
        return space

    def budget(self, experiment_id: str) -> int:
        b = self.experiment(experiment_id)["budget"]
        for a in self.amendments(experiment_id):
            if a["budget"] is not None:
                b = a["budget"]
        return b

    # ------------------------------------------------------------ trials
    def trial_count(self, experiment_id: str, segment: str = SELECTION_SEGMENT) -> int:
        """M: every trial of the family in ``segment`` regardless of status (failures count)."""
        return self.conn.execute("SELECT COUNT(*) FROM trial WHERE experiment_id=? AND segment=?",
                                 (experiment_id, segment)).fetchone()[0]

    def budget_consumption(self, experiment_id: str) -> float:
        return self.trial_count(experiment_id) / self.budget(experiment_id)

    def start_trial(self, experiment_id: str, *, spec: StrategySpec, segment: str, manifest: dict, principal: Principal,
                    label: str = "") -> str:
        if segment == "HOLDOUT":
            raise HoldoutError("holdout trials are created only by consume_holdout", "HOLDOUT_REQUIRES_GRANT")
        return self._insert_trial(experiment_id, spec=spec, segment=segment, manifest=manifest, principal=principal,
                                  label=label)

    def _insert_trial(self, experiment_id: str, *, spec: StrategySpec, segment: str, manifest: dict, principal: Principal,
                      label: str) -> str:
        if segment not in SEGMENTS:
            raise RegistryError(f"unknown segment {segment}", "INVALID_SEGMENT")
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot run research trials")
        exp = self.experiment(experiment_id)
        now = self.now()
        if now <= exp["created_at_ns"]:
            raise RegistryError("a trial must start after its pre-registration was filed", "PREREG_NOT_BEFORE_TRIAL")
        family_spec = self.spec(exp["spec_hash"])
        if segment == "BASELINE":
            if spec.strategy_id not in family_spec.baselines:
                raise RegistryError(f"{spec.strategy_id} is not a declared baseline of {family_spec.strategy_id}", "NOT_A_BASELINE")
        else:
            if spec.strategy_id != family_spec.strategy_id:
                raise RegistryError("trial strategy differs from the pre-registered strategy", "WRONG_STRATEGY")
            self._check_in_space(experiment_id, family_spec, spec, segment)
        if manifest.get("spec_hash") != spec.spec_hash or not manifest.get("manifest_hash"):
            raise RegistryError("the manifest must name this spec and carry its hash", "MANIFEST_MISMATCH")
        self._check_manifest_scope(exp, segment, manifest)
        self.register_spec(spec, principal)
        params = {k: v.to_str() for k, v in sorted(spec.parameters.items())}
        with self._tx() as c:
            if segment == SELECTION_SEGMENT:
                used = c.execute("SELECT COUNT(*) FROM trial WHERE experiment_id=? AND segment=?",
                                 (experiment_id, SELECTION_SEGMENT)).fetchone()[0]
                if used >= self.budget(experiment_id):
                    raise RegistryError(f"search budget of {experiment_id} exhausted ({used}); an owner amendment is required",
                                        "BUDGET_EXHAUSTED")
            elif segment in FOLLOW_UP_SEGMENTS:
                # Only parameter sets already counted in M (and in the budget) may be re-run in a follow-up segment;
                # otherwise a search could run outside DEVELOPMENT and the DSR's M would understate it.
                if c.execute("SELECT 1 FROM trial WHERE experiment_id=? AND segment=? AND params_hash=?",
                             (experiment_id, SELECTION_SEGMENT, stable_hash(params))).fetchone() is None:
                    raise RegistryError(f"{segment} trials may only re-run parameters already evaluated as a DEVELOPMENT "
                                        f"trial of {experiment_id} (counted in M)", "UNCOUNTED_PARAMETERS")
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trial WHERE experiment_id=?", (experiment_id,)).fetchone()[0]
            trial_id = f"{experiment_id}:T{seq:05d}"
            c.execute("INSERT OR IGNORE INTO manifest VALUES (?,?,?)", (manifest["manifest_hash"], stable_dumps(manifest), now))
            c.execute("INSERT INTO trial(trial_id, experiment_id, seq, segment, strategy_id, spec_hash, params, params_hash, "
                      "manifest_hash, label, author, author_role, started_at_ns, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (trial_id, experiment_id, seq, segment, spec.strategy_id, spec.spec_hash, stable_dumps(params),
                       stable_hash(params), manifest["manifest_hash"], label, principal.id, _role_name(principal), now, "RUNNING"))
        return trial_id

    @staticmethod
    def _manifest_untils(manifest: dict) -> dict[str, date]:
        ds = manifest.get("datasets")
        if not isinstance(ds, dict) or not ds:
            raise RegistryError("the manifest must name the datasets the run may read (CAP-RES-11)", "MANIFEST_INCOMPLETE")
        out = {}
        for name, d in ds.items():
            until = d.get("until") if isinstance(d, dict) else None
            try:
                out[name] = date.fromisoformat(until)
            except (TypeError, ValueError):
                raise RegistryError(f"manifest dataset {name} must state 'until', the last session the run may read",
                                    "MANIFEST_INCOMPLETE") from None
        return out

    def _granted_access(self, experiment_id: str, access_id: Any) -> bool:
        """True if ``access_id`` ('holdout:<n>') is this experiment's GRANTED holdout access."""
        try:
            n = int(str(access_id).split(":", 1)[1]) if str(access_id).startswith("holdout:") else None
        except (IndexError, ValueError):
            n = None
        if n is None:
            return False
        return self.conn.execute("SELECT 1 FROM holdout_access WHERE access_id=? AND experiment_id=? AND outcome='GRANTED'",
                                 (n, experiment_id)).fetchone() is not None

    def _check_manifest_scope(self, exp: dict, segment: str, manifest: dict) -> None:
        """BR-DAT-07: a trial outside the single HOLDOUT evaluation may not read the untouched holdout."""
        untils = self._manifest_untils(manifest)
        ho_start, ho_end = date.fromisoformat(exp["holdout_start"]), date.fromisoformat(exp["holdout_end"])
        beyond = sorted(n for n, u in untils.items() if u > ho_end)
        if beyond:
            raise HoldoutError(f"manifest datasets {beyond} extend beyond the registered holdout end {ho_end}",
                               "BEYOND_HOLDOUT")
        reach = sorted(n for n, u in untils.items() if u >= ho_start)
        if reach and segment != "HOLDOUT":
            access = (manifest.get("extra") or {}).get("holdout_access_id")
            if segment != "BASELINE" or not self._granted_access(exp["experiment_id"], access):
                raise HoldoutError(f"{segment} trial datasets {reach} reach into the untouched holdout starting {ho_start}; "
                                   "only the single HOLDOUT evaluation (and the declared baselines under its grant) may",
                                   "HOLDOUT_REQUIRES_GRANT")

    def _check_in_space(self, experiment_id: str, family: StrategySpec, spec: StrategySpec, segment: str) -> None:
        space = self.search_space(experiment_id)
        for name, value in spec.parameters.items():
            v = value.to_str()
            if name in space:
                allowed = {Fixed.parse(x) for x in space[name]}
                if value not in allowed:
                    raise RegistryError(f"{name}={v} is outside the pre-registered search space; file an amendment",
                                        "OUTSIDE_SEARCH_SPACE")
            elif value != family.parameters[name]:
                raise RegistryError(f"{name}={v} differs from the pre-registered fixed value {family.parameters[name]}",
                                    "OUTSIDE_SEARCH_SPACE")
        fixed_body = {k: v for k, v in spec.body.items() if k not in ("parameters", "sensitivity_plan")}
        family_body = {k: v for k, v in family.body.items() if k not in ("parameters", "sensitivity_plan")}
        if stable_hash(fixed_body) != stable_hash(family_body):
            raise RegistryError("only parameters may differ from the pre-registered spec", "SPEC_CHANGED")

    def finish_trial(self, trial_id: str, *, status: str, metrics: dict | None = None,
                     returns: list[tuple[date, float]] | None = None, results_hash: str | None = None,
                     error: str | None = None) -> None:
        if status not in TRIAL_STATUSES or status == "RUNNING":
            raise RegistryError(f"invalid terminal status {status}", "INVALID_STATUS")
        leak = self._returns_leak(trial_id, returns) if returns else None
        if leak is not None:
            self._finish(trial_id, "FAILED", None, None, None, f"HOLDOUT_LEAK: {leak}")
            raise HoldoutError(leak, "HOLDOUT_LEAK")
        self._finish(trial_id, status, metrics, returns, results_hash, error)

    def _returns_leak(self, trial_id: str, returns: list[tuple[date, float]]) -> str | None:
        """Outputs are bound to the manifest: no return after its ``until``, none in the holdout without the grant."""
        row = self.conn.execute("SELECT experiment_id, segment, manifest_hash FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown trial {trial_id}", "UNKNOWN_TRIAL")
        exp = self.experiment(row["experiment_id"])
        manifest = self.manifest(row["manifest_hash"])
        last = max(self._manifest_untils(manifest).values())
        dates = [d for d, _ in returns]
        if max(dates) > last:
            return f"returns dated up to {max(dates)} but the manifest's datasets end at {last}"
        ho_start = date.fromisoformat(exp["holdout_start"])
        inside = [d for d in dates if d >= ho_start]
        if inside and row["segment"] != "HOLDOUT" and not (
                row["segment"] == "BASELINE"
                and self._granted_access(row["experiment_id"], (manifest.get("extra") or {}).get("holdout_access_id"))):
            return f"{row['segment']} trial returned {len(inside)} returns inside the untouched holdout (from {ho_start})"
        return None

    def _finish(self, trial_id: str, status: str, metrics: dict | None, returns: list[tuple[date, float]] | None,
                results_hash: str | None, error: str | None) -> None:
        ret = [[d.isoformat(), r] for d, r in returns] if returns is not None else None
        try:
            cur = self.conn.execute(
                "UPDATE trial SET status=?, finished_at_ns=?, results_hash=?, metrics=?, returns=?, returns_hash=?, error=? "
                "WHERE trial_id=?", (status, self.now(), results_hash, stable_dumps(plain(metrics)) if metrics is not None else None,
                                     stable_dumps(ret) if ret is not None else None,
                                     stable_hash(ret) if ret is not None else None, error, trial_id))
        except sqlite3.DatabaseError as exc:
            raise RegistryError(f"trial {trial_id} cannot be finished again: {exc}", "TRIAL_FINISHED") from None
        if cur.rowcount != 1:
            raise RegistryError(f"unknown trial {trial_id}", "UNKNOWN_TRIAL")

    @contextmanager
    def run_trial(self, experiment_id: str, *, spec: StrategySpec, segment: str, manifest: dict, principal: Principal,
                  label: str = "") -> Iterator[TrialHandle]:
        """Record a trial around a run: an exception marks it FAILED (and re-raises); an interrupt marks it ABORTED."""
        tid = self.start_trial(experiment_id, spec=spec, segment=segment, manifest=manifest, principal=principal, label=label)
        with self._guard(tid) as handle:
            yield handle

    @contextmanager
    def run_holdout(self, experiment_id: str, *, spec: StrategySpec, artifact_hash: str, manifest: dict,
                    principal: Principal, label: str = "holdout") -> Iterator[TrialHandle]:
        """Consume the experiment's single holdout evaluation and record it as a guarded HOLDOUT trial.
        The handle carries the ``HoldoutGrant`` needed to build the holdout ``DatasetView``; the
        declared baselines may be evaluated on identical dates under the same grant (one event)."""
        grant, tid = self.consume_holdout(experiment_id, spec=spec, artifact_hash=artifact_hash, manifest=manifest,
                                          principal=principal, label=label)
        with self._guard(tid) as handle:
            handle.grant = grant
            yield handle

    @contextmanager
    def _guard(self, tid: str) -> Iterator[TrialHandle]:
        handle = TrialHandle(self, tid)
        try:
            yield handle
        except Exception as exc:
            if not handle.finished:
                self.finish_trial(tid, status="FAILED", error="".join(traceback.format_exception_only(type(exc), exc)).strip()[:2000])
            raise
        except BaseException as exc:
            if not handle.finished:
                self.finish_trial(tid, status="ABORTED", error=f"interrupted: {type(exc).__name__}")
            raise
        if not handle.finished:
            self.finish_trial(tid, status="ARTIFACT_MISSING", error="run returned without recording results")

    def reap_running(self, older_than_ns: int) -> list[str]:
        """Trials still RUNNING from a crashed or killed process are finished as ABORTED (they stay counted)."""
        cutoff = self.now() - older_than_ns
        ids = [r[0] for r in self.conn.execute("SELECT trial_id FROM trial WHERE status='RUNNING' AND started_at_ns<=?", (cutoff,))]
        for tid in ids:
            self.finish_trial(tid, status="ABORTED", error="found RUNNING after its process ended (reaped)")
        return ids

    def trial(self, trial_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown trial {trial_id}", "UNKNOWN_TRIAL")
        return self._trial_row(row, with_returns=True)

    @staticmethod
    def _trial_row(row: sqlite3.Row, with_returns: bool = False) -> dict:
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
        if with_returns:
            d["returns"] = json.loads(d["returns"]) if d["returns"] else None
        else:
            d.pop("returns", None)
        return d

    def trials(self, experiment_id: str, segment: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trial WHERE experiment_id=?"
        args: tuple = (experiment_id,)
        if segment:
            sql += " AND segment=?"
            args += (segment,)
        return [self._trial_row(r) for r in self.conn.execute(sql + " ORDER BY seq", args)]

    def manifest(self, manifest_hash: str) -> dict:
        row = self.conn.execute("SELECT body FROM manifest WHERE manifest_hash=?", (manifest_hash,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown manifest {manifest_hash}", "UNKNOWN_MANIFEST")
        return json.loads(row["body"])

    def family_matrix(self, experiment_id: str, segment: str = SELECTION_SEGMENT) -> dict:
        """Synchronous T x M return matrix of the family's SUCCEEDED trials on their common dates.

        ``trial_count`` (all statuses) is reported alongside, because a failed
        trial is still a trial for the DSR even though it has no returns."""
        rows = [r for r in self.conn.execute("SELECT trial_id, returns FROM trial WHERE experiment_id=? AND segment=? "
                                             "AND status='SUCCEEDED' AND returns IS NOT NULL ORDER BY seq",
                                             (experiment_id, segment))]
        series = {r["trial_id"]: dict((d, v) for d, v in json.loads(r["returns"])) for r in rows}
        if not series:
            return {"dates": [], "trial_ids": [], "columns": [], "trial_count": self.trial_count(experiment_id, segment),
                    "dropped_dates": 0}
        common = sorted(set.intersection(*(set(s) for s in series.values())))
        union = set().union(*(set(s) for s in series.values()))
        ids = list(series)
        return {"dates": common, "trial_ids": ids, "columns": [[series[t][d] for d in common] for t in ids],
                "trial_count": self.trial_count(experiment_id, segment), "dropped_dates": len(union) - len(common)}

    # ----------------------------------------------------------- holdout
    def consume_holdout(self, experiment_id: str, *, spec: StrategySpec, artifact_hash: str, manifest: dict,
                        principal: Principal, label: str = "holdout") -> tuple[HoldoutGrant, str]:
        """Grant the single holdout evaluation of this experiment; every attempt is logged."""
        exp = self.experiment(experiment_id)
        now = self.now()

        def log_refusal(reason: str) -> None:
            self.conn.execute("INSERT INTO holdout_access(experiment_id, artifact_hash, outcome, trial_id, principal, role, "
                              "at_ns, reason) VALUES (?,?,?,?,?,?,?,?)",
                              (experiment_id, artifact_hash, "REFUSED", None, principal.id, _role_name(principal), now, reason))

        def refuse(reason: str, code: str):
            log_refusal(reason)
            raise HoldoutError(reason, code)

        if principal.role not in (Role.OWNER, Role.SYSTEM):
            refuse(f"role {principal.role.value} may not read the untouched holdout (BR-DAT-07: evaluator only)", "HOLDOUT_ROLE")
        granted = self.conn.execute("SELECT access_id, artifact_hash, trial_id, at_ns FROM holdout_access WHERE experiment_id=? "
                                    "AND outcome='GRANTED'", (experiment_id,)).fetchone()
        if granted is not None:
            refuse(f"holdout of {experiment_id} already consumed by {granted['artifact_hash'][:16]} (trial {granted['trial_id']}); "
                   "a further evaluation needs a new pre-registered experiment with a fresh holdout", "HOLDOUT_CONSUMED")
        try:
            trial_id = self._insert_trial(experiment_id, spec=spec, segment="HOLDOUT", manifest=manifest, principal=principal,
                                          label=label)
        except (RegistryError, AuthorityError, HoldoutError) as exc:
            log_refusal(f"{exc.code}: {exc}"[:2000])   # every attempt is logged, including invalid ones
            raise
        try:
            cur = self.conn.execute("INSERT INTO holdout_access(experiment_id, artifact_hash, outcome, trial_id, principal, "
                                    "role, at_ns, reason) VALUES (?,?,?,?,?,?,?,?)",
                                    (experiment_id, artifact_hash, "GRANTED", trial_id, principal.id, _role_name(principal),
                                     now, "single pre-registered holdout evaluation"))
        except sqlite3.IntegrityError:
            self.finish_trial(trial_id, status="ABORTED", error="holdout grant lost a race; consumed concurrently")
            raise HoldoutError("holdout consumed concurrently", "HOLDOUT_CONSUMED") from None
        grant = issue_grant(f"holdout:{cur.lastrowid}", experiment_id, artifact_hash,
                            date.fromisoformat(exp["holdout_start"]), date.fromisoformat(exp["holdout_end"]))
        return grant, trial_id

    def holdout_log(self, experiment_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM holdout_access"
        args: tuple = ()
        if experiment_id:
            sql += " WHERE experiment_id=?"
            args = (experiment_id,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY access_id", args)]

    # ----------------------------------------------------------- reports
    def add_report(self, report_id: str, body: dict, *, kind: str, experiment_id: str | None, fixture: bool) -> str:
        h = stable_hash(body)
        self.conn.execute("INSERT INTO report VALUES (?,?,?,?,?,?,?)",
                          (report_id, experiment_id, kind, stable_dumps(body), h, self.now(), int(fixture)))
        return h

    def report(self, report_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM report WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown report {report_id}", "UNKNOWN_REPORT")
        d = dict(row)
        d["body"] = json.loads(d["body"])
        return d

    def reports(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT report_id, experiment_id, kind, body_hash, created_at_ns, fixture FROM report ORDER BY created_at_ns, report_id")]

    def exists(self, table: str, column: str, value: Any) -> bool:
        if table not in ("trial", "report", "manifest", "experiment", "strategy_spec", "evidence", "artifact"):
            raise ValueError(table)
        return self.conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone() is not None
