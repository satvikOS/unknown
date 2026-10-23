"""Strategy artifact registry, status lifecycle and predeclared promotion gates
(CAP-RES-12; AUT-01..AUT-04; book ch. 20; BR-DAT-01, BR-DAT-10, BR-OPS-10).

Statuses: proposed, reproducible, validated, shadow, paper, eligible, active,
quarantined, retired (ADR-0017 #2). The authoritative status of an artifact
is the last record of its append-only, hash-chained transition log; nothing
else (no dashboard row, no alias field) can change it.

Transition rules (``RULES``):

=========================  ==============================  ===========================================
from -> to                 who                             evidence / approval
=========================  ==============================  ===========================================
proposed -> reproducible   SYSTEM evaluator or OWNER,      REPLAY_EXACT passed
                           never the artifact's author
reproducible -> validated  same                            every gate kind the spec lists (walk-forward,
                                                           paired bootstrap, holdout, costs, stress,
                                                           DSR, PBO, n_eff) passed under one protocol
validated -> shadow        OWNER approval, or SYSTEM       the gate evidence the validation rested on,
                           inside an owner-approved shadow cited again. Under an envelope it must have
                           envelope (``auto_shadow``)      been evaluated under the envelope's protocol,
                                                           and the artifact must lie inside the
                                                           envelope's class, parameter domain,
                                                           instrument set and risk budget
shadow -> paper            OWNER approval                  CORE_REPLAY_MATCH passed
paper -> eligible          OWNER approval                  PAPER_RECONCILIATION passed
eligible -> active         OWNER approval, or SYSTEM       slot named; the incumbent in the slot moves
                           under an owner-approved          back to eligible in the same operation
                           selection policy (AUT-03)
active -> eligible         OWNER or SYSTEM                 deactivation reason
any -> quarantined         OWNER or SYSTEM                 MONITORING_INCIDENT evidence
quarantined -> reproducible OWNER approval                 REPLAY_EXACT passed (re-validation path)
any -> retired             OWNER approval                  reason
=========================  ==============================  ===========================================

An AGENT or STRATEGY principal can never approve or execute a transition;
an agent may register a proposed artifact and file evidence, but only
evidence written by the SYSTEM evaluator or the OWNER counts for a gate.
Gate pass/fail is recomputed here from the evidence payload and the
owner-approved promotion protocol (BR-DAT-10); the submitter's own verdict is
ignored. A protocol approved after an evaluation started fails that
evidence (policy precedence). Evidence references (trials, reports,
manifests, holdout grants) must exist in the research registry.

Evidence is also bound to registry facts the submitter cannot choose (review
fixes 2026-10-22): policy precedence is judged against the earlier of the
claimed evaluation start and the earliest referenced trial; HOLDOUT evidence
must name a GRANTED holdout access consumed for this very artifact and cite
the trial that grant created; DSR evidence must use M equal to the registry's
trial count of the cited experiment; STRESS evidence must cover every stress
case the spec declares. Activation (by the owner or by automatic selection)
refuses an expired artifact, and automatic selection ranks by the same
conservative paired lower bound the gate uses.

Evidence retention (BR-DAT-16). Every manifest dataset entry states
``retention_until`` (``research.manifest``): the last day a result resting on
the data may be used, derived when the manifest is built from the provider
registry and the point-in-time store (``research.bars.pit_manifest_entry``), or
``"unbounded"``. An artifact's evidence datasets are those of its evaluation
manifest and of the trials its passed evidence cites. Its expiry is bounded by
them: registration refuses an ``expires_on`` later than the evaluation
manifest's earliest retention date, and the effective expiry used by every
expiry check (activation, automatic selection and shadow promotion, the
strategy runner) is ``min(expires_on, retention_until of every evidence
dataset, the day before any recorded deletion of that data)``.

A deletion is recorded in the research registry in two ways, both append-only:
the OWNER or the SYSTEM records that a whole dataset was deleted
(``record_evidence_deletion``), or the deletion sweep reads the point-in-time
store's deletion events (retention elapsed, rights withdrawn) for every dataset
the registry's artifacts rest on (``sweep_store_deletions``, pull-based and
idempotent). A store event touches a manifest when it deleted observations of
an entity the manifest's ``scope`` names, available by its cutoff; a manifest
without a scope is touched by any deletion of its dataset.

An artifact a recorded deletion touches is EVIDENCE_EXPIRED (``evidence_status``).
In the same transaction that records the deletion, every such artifact that is
not retired receives EVIDENCE_DELETION evidence naming the deletion (filed by
the recording principal), and an active one steps down to eligible citing that
evidence, so it leaves its slot. Its effective expiry is at most the day before
the deletion, so it never runs again: the strategy runner refuses it as
ARTIFACT_EXPIRED in every mode, and no expiry-checked path (activation,
automatic selection, automatic shadow promotion) accepts it. Every other upward
transition, including the quarantined -> reproducible re-validation path, is
refused as EVIDENCE_EXPIRED; quarantine and retirement still apply. The expiry
is part of the immutable registration, so there is no renewal: new evidence on
retained data is a new artifact. Registration refuses an evaluation manifest
that rests on deleted data, and evidence citing a trial whose data was deleted
never passes (except a monitoring incident or the deletion itself).

"Signed" in CAP-RES-12 is implemented as a hash chain over transition
records (tamper evidence, not a cryptographic signature; no signing key
infrastructure exists yet).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..domain.fixed import Fixed
from ..domain.principal import Principal, Role
from .common import AuthorityError, EvidenceError, RegistryError, stable_dumps, stable_hash
from .manifest import UNBOUNDED, retention_date
from .registry import ResearchRegistry
from .spec import StrategySpec

STATUSES = ("proposed", "reproducible", "validated", "shadow", "paper", "eligible", "active", "quarantined", "retired")
# The promotion ladder in ADR-0017 #2 order; quarantined and retired are off the ladder.
LADDER = ("proposed", "reproducible", "validated", "shadow", "paper", "eligible", "active")
APPROVAL_MAX_AGE_NS = 15 * 60 * 1_000_000_000      # owner re-authentication window, as for step-up commands
GATE_KINDS = ("WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT", "COST_SENSITIVITY", "STRESS", "DSR", "PBO", "N_EFF")
GENESIS = "0" * 64


@dataclass(frozen=True)
class Rule:
    evidence: tuple[str, ...]
    owner_approval: bool
    evaluator: bool = False          # SYSTEM/OWNER executes; must differ from the artifact author
    selection_policy_ok: bool = False
    gate_evidence: bool = False      # the required evidence is every gate kind the spec lists
    envelope_ok: bool = False        # the SYSTEM may execute it inside an owner-approved shadow envelope


RULES: dict[tuple[str, str], Rule] = {
    ("proposed", "reproducible"): Rule(("REPLAY_EXACT",), False, evaluator=True),
    ("reproducible", "validated"): Rule((), False, evaluator=True, gate_evidence=True),
    # ADR-0017 #2: shadow sits between validated and paper. The owner approves it, or the SYSTEM promotes inside an
    # owner-approved envelope (``auto_shadow``). Either way the record cites the gate evidence it rests on.
    ("validated", "shadow"): Rule((), True, gate_evidence=True, envelope_ok=True),
    ("shadow", "paper"): Rule(("CORE_REPLAY_MATCH",), True),
    ("paper", "eligible"): Rule(("PAPER_RECONCILIATION",), True),
    ("eligible", "active"): Rule((), True, selection_policy_ok=True),
    ("active", "eligible"): Rule((), False),
    ("quarantined", "reproducible"): Rule(("REPLAY_EXACT",), True),
}
for _s in ("proposed", "reproducible", "validated", "shadow", "paper", "eligible", "active"):
    RULES[(_s, "quarantined")] = Rule(("MONITORING_INCIDENT",), False)
for _s in ("proposed", "reproducible", "validated", "shadow", "paper", "eligible", "active", "quarantined"):
    RULES[(_s, "retired")] = Rule((), True)

# The envelope inside which the SYSTEM may promote validated -> shadow without a per-artifact owner approval (book
# ch. 20: automatic promotion only for artifacts that fit an owner-approved class, parameter domain, instrument set,
# risk budget and evaluation protocol). Approved by the owner, immutable, hashed into every promotion it allows.
SHADOW_ENVELOPE_FIELDS = frozenset({"envelope_id", "strategy_id", "parameter_domain", "instrument_set", "risk_budget",
                                    "protocol_hash", "max_promotions_per_period", "period_days"})
# The risk budget bounds the spec's risk_limits: every value the spec declares must be at or below the budget's.
RISK_BUDGET_FIELDS = ("max_gross_exposure", "max_single_name_weight", "max_drawdown_stop")
EVIDENCE_STATUSES = ("CURRENT", "EVIDENCE_EXPIRED")     # BR-DAT-16


def _upward(frm: str, to: str) -> bool:
    """A promotion: up the ladder, or out of quarantine into the re-validation path."""
    if frm == "quarantined":
        return to in LADDER
    return frm in LADDER and to in LADDER and LADDER.index(to) > LADDER.index(frm)


def _retention(entry: Any) -> date | None:
    """A manifest dataset entry's ``retention_until`` (ISO date), or None when it states no deletion deadline
    (``"unbounded"``, or not recorded)."""
    value = entry.get("retention_until") if isinstance(entry, dict) else None
    if value is None or value == UNBOUNDED:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise EvidenceError(f"manifest dataset retention_until {value!r} is neither an ISO date nor "
                            f"{UNBOUNDED!r}") from None


def _scope_touched(deletion: dict, scope: Any) -> bool:
    """Whether a recorded deletion removed data a manifest's dataset entry read. A whole-dataset deletion (no scope),
    or an entry that does not say what it read, is always touched. Otherwise the deletion must have removed
    observations of an entity the entry names that were available by the entry's cutoff."""
    removed = deletion.get("scope")
    if removed is None or not isinstance(scope, dict):
        return True
    try:
        cutoff = int(scope["available_until_ns"])
        entities = list(scope["entities"])
    except (KeyError, TypeError, ValueError):
        return True                       # a scope that cannot be read bounds nothing: treat it as touched
    return any(e in removed and int(removed[e]["first_available_ns"]) <= cutoff for e in entities)

PROTOCOL_FIELDS = {
    "confidence": "decimal in (0.5, 1)", "replications": "int >= 200", "mean_block": "int >= 1 or null (round(n^(1/3)))",
    "statistic": "mean | utility.ch20@1", "utility_c_drawdown": "decimal >= 0", "utility_c_es": "decimal >= 0",
    "hurdle_per_period": "decimal (net of trading costs, per-session units)", "min_n_eff": "int >= 1",
    "min_oos_observations": "int >= 1", "min_oos_sharpe_per_period": "decimal", "max_pbo": "decimal in [0,1]",
    "min_dsr": "decimal in [0,1]", "cost_multiplier_required": "decimal >= 1", "max_stress_drawdown": "decimal in (0,1]",
    "holdout_min_difference": "decimal", "min_paper_days": "int >= 1", "seed": "int",
}


def _dec(v: Any, name: str) -> Fixed:
    if not isinstance(v, str):
        raise EvidenceError(f"protocol.{name} must be a decimal string")
    return Fixed.parse(v)


def validate_protocol(body: dict) -> dict:
    """Strict check of the owner's promotion protocol (BR-DAT-10): every field required, nothing extra."""
    if not isinstance(body, dict) or set(body) != set(PROTOCOL_FIELDS):
        raise EvidenceError(f"protocol must have exactly the fields {sorted(PROTOCOL_FIELDS)}")
    conf = _dec(body["confidence"], "confidence")
    if not Fixed.parse("0.5") < conf < Fixed(1):
        raise EvidenceError("protocol.confidence must be in (0.5, 1)")
    if type(body["replications"]) is not int or body["replications"] < 200:
        raise EvidenceError("protocol.replications must be an integer >= 200")
    if body["mean_block"] is not None and (type(body["mean_block"]) is not int or body["mean_block"] < 1):
        raise EvidenceError("protocol.mean_block must be null or an integer >= 1")
    if body["statistic"] not in ("mean", "utility.ch20@1"):
        raise EvidenceError("protocol.statistic must be 'mean' or 'utility.ch20@1'")
    for k in ("utility_c_drawdown", "utility_c_es"):
        if _dec(body[k], k).is_negative():
            raise EvidenceError(f"protocol.{k} must be >= 0")
    for k in ("hurdle_per_period", "min_oos_sharpe_per_period", "holdout_min_difference"):
        _dec(body[k], k)
    for k in ("max_pbo", "min_dsr"):
        if not Fixed(0) <= _dec(body[k], k) <= Fixed(1):
            raise EvidenceError(f"protocol.{k} must be in [0, 1]")
    if _dec(body["cost_multiplier_required"], "cost_multiplier_required") < Fixed(1):
        raise EvidenceError("protocol.cost_multiplier_required must be >= 1")
    if not Fixed(0) < _dec(body["max_stress_drawdown"], "max_stress_drawdown") <= Fixed(1):
        raise EvidenceError("protocol.max_stress_drawdown must be in (0, 1]")
    for k in ("min_n_eff", "min_oos_observations", "min_paper_days"):
        if type(body[k]) is not int or body[k] < 1:
            raise EvidenceError(f"protocol.{k} must be an integer >= 1")
    if type(body["seed"]) is not int:
        raise EvidenceError("protocol.seed must be an integer")
    return body


def _f(x: str) -> float:
    return float(Fixed.parse(x).to_str())


def check_evidence(kind: str, payload: dict, protocol: dict | None) -> tuple[bool, list[str]]:
    """Recompute a gate verdict from the evidence payload and the owner's protocol. Never trusts 'passes' fields."""
    why: list[str] = []
    p = protocol or {}

    def need_protocol() -> bool:
        if protocol is None:
            why.append("no promotion protocol referenced")
            return False
        return True

    if kind == "REPLAY_EXACT":
        if payload.get("replay_exact") != 1:
            why.append("replay event-log hash differs from the original")
    elif kind == "CORE_REPLAY_MATCH":
        if payload.get("mismatches") != [] or payload.get("ledger_problems") != [] or not payload.get("fills_compared"):
            why.append("core replay did not match the engine exactly (or compared nothing)")
    elif kind == "WALK_FORWARD_OOS":
        if need_protocol():
            n = payload.get("n") or 0
            sr = payload.get("sharpe_per_period")
            if n < p["min_oos_observations"]:
                why.append(f"OOS observations {n} < {p['min_oos_observations']}")
            if sr is None or sr < _f(p["min_oos_sharpe_per_period"]):
                why.append(f"OOS Sharpe {sr} < {p['min_oos_sharpe_per_period']}")
    elif kind in ("PAIRED_BOOTSTRAP", "HOLDOUT"):
        if need_protocol():
            b = payload.get("bootstrap") or {}
            if not b.get("computable"):
                why.append("paired bootstrap not computable")
            else:
                if abs(b.get("confidence", -1) - _f(p["confidence"])) > 1e-12:
                    why.append("bootstrap confidence differs from the protocol")
                if b.get("replications", 0) < p["replications"]:
                    why.append("fewer bootstrap replications than the protocol")
                if b.get("statistic") != p["statistic"]:
                    why.append("bootstrap statistic differs from the protocol")
                lower = b.get("conservative_lower", b.get("lower", -1e300))
                if kind == "PAIRED_BOOTSTRAP" and not lower > _f(p["hurdle_per_period"]):
                    why.append(f"conservative lower bound {lower} does not exceed the hurdle {p['hurdle_per_period']}")
                if kind == "HOLDOUT":
                    if not payload.get("holdout_access_id"):
                        why.append("holdout evaluation without a recorded grant")
                    if not b.get("point", -1e300) > _f(p["holdout_min_difference"]):
                        why.append(f"holdout paired difference {b.get('point')} <= {p['holdout_min_difference']}")
    elif kind == "COST_SENSITIVITY":
        if need_protocol():
            rows = {Fixed.parse(k): v for k, v in (payload.get("by_multiplier") or {}).items()}
            for m in ("0.5", "1", "2", "3"):
                if Fixed.parse(m) not in rows:
                    why.append(f"cost multiplier {m}x missing")
            req = rows.get(Fixed.parse(p["cost_multiplier_required"]))
            if req is None or not (req.get("net_mean") or -1) > 0:
                why.append(f"net mean return not positive at {p['cost_multiplier_required']}x costs")
    elif kind == "STRESS":
        if need_protocol():
            cases = payload.get("cases") or {}
            if not cases:
                why.append("no stress cases")
            for cid, c in cases.items():
                dd = c.get("max_drawdown")
                if dd is None or dd > _f(p["max_stress_drawdown"]):
                    why.append(f"stress {cid}: drawdown {dd} > {p['max_stress_drawdown']}")
    elif kind == "DSR":
        if need_protocol():
            v = payload.get("dsr")
            if v is None or v < _f(p["min_dsr"]):
                why.append(f"DSR {v} < {p['min_dsr']} (or not computable)")
    elif kind == "PBO":
        if need_protocol():
            v = payload.get("pbo")
            if v is None or v > _f(p["max_pbo"]):
                why.append(f"PBO {v} > {p['max_pbo']} (or not computable)")
    elif kind == "N_EFF":
        if need_protocol():
            v = payload.get("gate")
            if v is None or v < p["min_n_eff"]:
                why.append(f"n_eff {v} < {p['min_n_eff']}")
    elif kind == "PAPER_RECONCILIATION":
        if need_protocol():
            if payload.get("reconciliation_breaks") != 0 or (payload.get("paper_days") or 0) < p["min_paper_days"]:
                why.append("paper stage incomplete or reconciliation breaks present")
    elif kind == "MONITORING_INCIDENT":
        if payload.get("category") not in ("STATISTICAL_UNDERPERFORMANCE", "SOFTWARE_DEFECT", "DATA_DEFECT",
                                           "RECONCILIATION_BREAK", "FEATURE_DRIFT", "OPERATIONAL_FAILURES"):
            why.append("incident needs a category (statistical underperformance vs defect are distinct)")
    elif kind == "EVIDENCE_DELETION":
        # BR-DAT-16. Bound to the registry's deletion record in PromotionService._bind_evidence.
        if not isinstance(payload.get("deletion_id"), str) or not isinstance(payload.get("dataset"), str) \
                or type(payload.get("deleted_at_ns")) is not int:
            why.append("an evidence deletion names the recorded deletion, its dataset and deleted_at_ns")
    else:
        why.append(f"unknown evidence kind {kind}")
    return not why, why


class PromotionService:
    """Executes transitions. Holds no credentials beyond the research registry connection."""

    def __init__(self, registry: ResearchRegistry):
        self.r = registry
        self.c = registry.conn

    # ------------------------------------------------------ authority helpers
    def _owner_approval(self, principal: Principal) -> dict:
        if principal.role is not Role.OWNER:
            raise AuthorityError(f"{principal.role.value} '{principal.id}' cannot approve: owner approval required "
                                 "(agents can never approve, AUT-02/AUT-04)")
        now = self.r.now()
        auth = principal.authenticated_at_ns
        if auth is None or now - auth > APPROVAL_MAX_AGE_NS or auth > now:
            raise AuthorityError("owner approval needs a re-authentication within the last 15 minutes")
        return {"kind": "OWNER", "by": principal.id, "at_ns": now, "authenticated_at_ns": auth}

    @staticmethod
    def _no_agents(principal: Principal, what: str) -> None:
        if principal.role in (Role.AGENT, Role.STRATEGY):
            raise AuthorityError(f"{principal.role.value} principals cannot {what}")

    # ---------------------------------------------------------- protocols
    def approve_protocol(self, body: dict, principal: Principal) -> str:
        validate_protocol(body)
        approval = self._owner_approval(principal)
        h = stable_hash(body)
        if self.c.execute("SELECT 1 FROM protocol WHERE protocol_hash=?", (h,)).fetchone() is None:
            self.c.execute("INSERT INTO protocol VALUES (?,?,?,?)", (h, stable_dumps(body), approval["by"], approval["at_ns"]))
        return h

    def protocol(self, protocol_hash: str) -> dict:
        row = self.c.execute("SELECT * FROM protocol WHERE protocol_hash=?", (protocol_hash,)).fetchone()
        if row is None:
            raise EvidenceError(f"unknown promotion protocol {protocol_hash}")
        body = json.loads(row["body"])
        if stable_hash(body) != protocol_hash:
            raise EvidenceError("protocol body does not match its hash")
        return {"body": body, "approved_by": row["approved_by"], "approved_at_ns": row["approved_at_ns"]}

    # ---------------------------------------------------------- artifacts
    def register_artifact(self, spec: StrategySpec, principal: Principal, *, code_hash: str, training_interval: tuple[date, date],
                          evaluation_manifest_hash: str, allowed_markets: list[str], horizon: str, capacity_estimate: str,
                          expires_on: date, rollback_predecessor: str | None = None) -> str:
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot register artifacts")
        self.r.register_spec(spec, principal)
        if not self.r.exists("manifest", "manifest_hash", evaluation_manifest_hash):
            raise EvidenceError("evaluation manifest is not in the research registry")
        # BR-DAT-16: the artifact may not outlive the evidence it rests on, nor rest on data already deleted.
        bound = self._retention_bound([evaluation_manifest_hash])
        if bound is not None and expires_on > bound:
            raise EvidenceError(f"expires_on {expires_on.isoformat()} is after {bound.isoformat()}, the last day the "
                                "evaluation manifest's data may be used under its licence (BR-DAT-16): set the expiry on "
                                "or before the retention date")
        gone = self._manifest_deletions(evaluation_manifest_hash)
        if gone:
            raise EvidenceError(f"EVIDENCE_EXPIRED: the evaluation manifest rests on deleted data "
                                f"{sorted({d['dataset'] for d in gone})} (BR-DAT-16)")
        bundle = {"spec_hash": spec.spec_hash, "strategy_id": spec.strategy_id, "parameters": {k: v.to_str() for k, v in
                  sorted(spec.parameters.items())}, "features": spec.body["features"], "code_hash": code_hash,
                  "dependency_lock": "standard library only (no third-party packages)", "schema": "strategy-spec@1"}
        artifact_hash = stable_hash(bundle)
        if self.c.execute("SELECT 1 FROM artifact WHERE artifact_hash=?", (artifact_hash,)).fetchone():
            return artifact_hash
        if rollback_predecessor and not self.c.execute("SELECT 1 FROM artifact WHERE artifact_hash=?", (rollback_predecessor,)).fetchone():
            raise EvidenceError("rollback predecessor is not a registered artifact")
        body = {"bundle": bundle, "training_interval": [d.isoformat() for d in training_interval],
                "evaluation_manifest_hash": evaluation_manifest_hash, "allowed_markets": allowed_markets, "horizon": horizon,
                "capacity_estimate": capacity_estimate, "expires_on": expires_on.isoformat(),
                "rollback_predecessor": rollback_predecessor, "instruments": list(spec.instrument_ids)}
        now = self.r.now()
        with self.r._tx() as c:
            c.execute("INSERT INTO artifact VALUES (?,?,?,?,?,?,?)", (artifact_hash, spec.spec_hash, spec.strategy_id,
                                                                     stable_dumps(body), principal.id, principal.role.value, now))
            self._append(c, artifact_hash, None, "proposed", principal, [], None, None, None, "registered")
        return artifact_hash

    def artifact(self, artifact_hash: str) -> dict:
        row = self.c.execute("SELECT * FROM artifact WHERE artifact_hash=?", (artifact_hash,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown artifact {artifact_hash}", "UNKNOWN_ARTIFACT")
        d = dict(row)
        d["body"] = json.loads(d["body"])
        d["status"] = self.status(artifact_hash)
        return d

    def status(self, artifact_hash: str) -> str:
        row = self.c.execute("SELECT to_status FROM transition WHERE artifact_hash=? ORDER BY seq DESC LIMIT 1",
                             (artifact_hash,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown artifact {artifact_hash}", "UNKNOWN_ARTIFACT")
        return row["to_status"]

    def history(self, artifact_hash: str) -> list[dict]:
        return [self._row(r) for r in self.c.execute("SELECT * FROM transition WHERE artifact_hash=? ORDER BY seq", (artifact_hash,))]

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["evidence_ids"] = json.loads(d["evidence_ids"])
        d["approval"] = json.loads(d["approval"]) if d["approval"] else None
        return d

    def verify_chain(self) -> list[str]:
        problems = []
        prev = GENESIS
        for r in self.c.execute("SELECT * FROM transition ORDER BY seq"):
            d = self._row(r)
            if d["prev_record_hash"] != prev:
                problems.append(f"transition {d['seq']} breaks the chain")
            if stable_hash(self._record(d)) != d["record_hash"]:
                problems.append(f"transition {d['seq']} content does not match its hash")
            prev = d["record_hash"]
        return problems

    @staticmethod
    def _record(d: dict) -> dict:
        return {k: d[k] for k in ("artifact_hash", "from_status", "to_status", "at_ns", "principal", "role", "evidence_ids",
                                  "approval", "slot", "incumbent_hash", "reason", "prev_record_hash")}

    def _append(self, c, artifact_hash: str, frm: str | None, to: str, principal: Principal, evidence_ids: list[str],
                approval: dict | None, slot: str | None, incumbent: str | None, reason: str) -> dict:
        last = c.execute("SELECT record_hash FROM transition ORDER BY seq DESC LIMIT 1").fetchone()
        rec = {"artifact_hash": artifact_hash, "from_status": frm, "to_status": to, "at_ns": self.r.now(),
               "principal": principal.id, "role": principal.role.value, "evidence_ids": sorted(evidence_ids),
               "approval": approval, "slot": slot, "incumbent_hash": incumbent, "reason": reason,
               "prev_record_hash": last["record_hash"] if last else GENESIS}
        h = stable_hash(rec)
        c.execute("INSERT INTO transition(artifact_hash, from_status, to_status, at_ns, principal, role, evidence_ids, approval, "
                  "slot, incumbent_hash, reason, prev_record_hash, record_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (artifact_hash, frm, to, rec["at_ns"], principal.id, principal.role.value, json.dumps(rec["evidence_ids"]),
                   stable_dumps(approval) if approval else None, slot, incumbent, reason, rec["prev_record_hash"], h))
        return {**rec, "record_hash": h}

    # ----------------------------------------------------------- evidence
    def add_evidence(self, artifact_hash: str, kind: str, payload: dict, *, refs: list[dict], principal: Principal,
                     evaluation_started_at_ns: int, protocol_hash: str | None = None) -> dict:
        self.artifact(artifact_hash)
        if principal.role is Role.STRATEGY:
            raise AuthorityError("a strategy principal cannot file evidence")
        for ref in refs:
            table, col = {"trial": ("trial", "trial_id"), "report": ("report", "report_id"),
                          "manifest": ("manifest", "manifest_hash")}.get(ref.get("type"), (None, None))
            if ref.get("type") == "holdout_access":
                ok = self.c.execute("SELECT 1 FROM holdout_access WHERE access_id=? AND outcome='GRANTED'",
                                    (int(str(ref.get("id")).split(":")[-1]),)).fetchone() is not None
            elif ref.get("type") == "deletion":
                ok = any(d["deletion_id"] == ref.get("id") for d in self.deletions())
            elif table is None:
                raise EvidenceError(f"unknown evidence reference type {ref.get('type')}")
            else:
                ok = self.r.exists(table, col, ref.get("id"))
            if not ok:
                raise EvidenceError(f"evidence reference {ref} does not exist in the research registry")
        protocol = None
        reasons: list[str] = []
        now = self.r.now()
        trial_rows = [self.c.execute("SELECT trial_id, experiment_id, segment, started_at_ns FROM trial WHERE trial_id=?",
                                     (ref.get("id"),)).fetchone() for ref in refs if ref.get("type") == "trial"]
        # Policy precedence is judged against when the evaluation really started: the caller's claim, or the
        # earliest referenced trial if that is earlier (a claimed start cannot postdate the work it cites).
        effective_start = min([evaluation_started_at_ns] + [t["started_at_ns"] for t in trial_rows])
        if evaluation_started_at_ns > now:
            reasons.append("evaluation start lies in the future")
        if protocol_hash is not None:
            pr = self.protocol(protocol_hash)
            protocol = pr["body"]
            if not pr["approved_at_ns"] < effective_start:
                reasons.append("protocol approved after the evaluation started (policy precedence; earliest referenced "
                               "trial or claimed start)")
        reasons += self._bind_evidence(artifact_hash, kind, payload, refs, trial_rows)
        passed, why = check_evidence(kind, payload, protocol)
        reasons += why
        passed = passed and not reasons
        if principal.role is Role.AGENT:
            reasons.append("filed by an agent: recorded, but agent-filed evidence never satisfies a gate")
            passed = False
        eid = f"ev:{stable_hash([artifact_hash, kind, stable_hash(payload), now])[:24]}"
        self.c.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (eid, artifact_hash, kind, stable_dumps({"payload": payload, "gate_reasons": reasons}), stable_hash(payload),
                        stable_dumps(refs), protocol_hash, effective_start, int(passed), now, principal.id,
                        principal.role.value))
        return {"evidence_id": eid, "kind": kind, "passed": passed, "reasons": reasons}

    def _bind_evidence(self, artifact_hash: str, kind: str, payload: dict, refs: list[dict], trial_rows: list) -> list[str]:
        """Checks that tie a payload to registry facts the submitter cannot choose (review fix 2026-10-22)."""
        why: list[str] = []
        if kind == "HOLDOUT":
            # The grant must exist, be for THIS artifact, and its holdout trial must be cited.
            aid = str(payload.get("holdout_access_id") or "")
            row = None
            if aid.startswith("holdout:") and aid.split(":", 1)[1].isdigit():
                row = self.c.execute("SELECT * FROM holdout_access WHERE access_id=? AND outcome='GRANTED'",
                                     (int(aid.split(":", 1)[1]),)).fetchone()
            if row is None:
                why.append(f"holdout access {aid or '(none)'} is not a GRANTED holdout access in the registry")
            else:
                if row["artifact_hash"] != artifact_hash:
                    why.append("the holdout was consumed for a different artifact")
                if row["trial_id"] not in {t["trial_id"] for t in trial_rows if t is not None}:
                    why.append("the evidence does not cite the holdout trial its grant created")
        elif kind == "DSR":
            # CAP-RES-08/-10: the M used must equal the registry's count for the family; no manual override.
            exps = {t["experiment_id"] for t in trial_rows if t is not None and t["segment"] == "DEVELOPMENT"}
            if len(exps) != 1:
                why.append("DSR evidence must cite the DEVELOPMENT trials of exactly one experiment")
            else:
                exp = exps.pop()
                count = self.r.trial_count(exp)
                if payload.get("M") != count:
                    why.append(f"DSR used M = {payload.get('M')} but the registry counts {count} trials in {exp}")
        elif kind == "STRESS":
            spec = self.r.spec(self.artifact(artifact_hash)["spec_hash"])
            missing = sorted({s["id"] for s in spec.stress_plan} - set((payload.get("cases") or {})))
            if missing:
                why.append(f"stress cases declared in the spec are missing: {missing}")
        elif kind == "EVIDENCE_DELETION":
            # BR-DAT-16: the deletion must be recorded, match the payload, and touch data this artifact rests on.
            did = payload.get("deletion_id")
            d = next((x for x in self.deletions() if x["deletion_id"] == did), None)
            if d is None:
                why.append(f"deletion {did!r} is not recorded in the research registry")
            else:
                if (payload.get("dataset"), payload.get("deleted_at_ns")) != (d["dataset"], d["deleted_at_ns"]):
                    why.append("the payload's dataset or deletion time differs from the recorded deletion")
                if not any(r.get("type") == "deletion" and r.get("id") == did for r in refs):
                    why.append("the evidence does not cite the recorded deletion")
                if not self._touching(self.evidence_datasets(artifact_hash), d):
                    why.append("the deletion did not remove data this artifact's evidence rests on")
        if kind not in ("MONITORING_INCIDENT", "EVIDENCE_DELETION"):
            # BR-DAT-16: a trial whose data was deleted supports nothing new (it would put the artifact on deleted data).
            for t in trial_rows:
                if t is not None and self._manifest_deletions(self.r.trial(t["trial_id"])["manifest_hash"]):
                    why.append(f"cites trial {t['trial_id']} whose data was deleted (BR-DAT-16)")
        return why

    def evidence(self, artifact_hash: str) -> list[dict]:
        out = []
        for r in self.c.execute("SELECT * FROM evidence WHERE artifact_hash=? ORDER BY created_at_ns, evidence_id", (artifact_hash,)):
            d = dict(r)
            body = json.loads(d.pop("payload"))
            d["payload"], d["gate_reasons"], d["refs"] = body["payload"], body["gate_reasons"], json.loads(d["refs"])
            d["passed"] = bool(d["passed"])
            out.append(d)
        return out

    # --------------------------------------------------------- transitions
    def transition(self, artifact_hash: str, to: str, principal: Principal, *, evidence_ids: list[str] = (),
                   reason: str, slot: str | None = None) -> dict:
        """Move one artifact. Refused (status unchanged) unless the rule's authority and evidence are satisfied."""
        self._no_agents(principal, "execute or approve promotion transitions")
        if to not in STATUSES:
            raise RegistryError(f"unknown status {to}", "INVALID_STATUS")
        art = self.artifact(artifact_hash)
        frm = art["status"]
        rule = RULES.get((frm, to))
        if rule is None:
            raise RegistryError(f"transition {frm} -> {to} is not permitted", "INVALID_TRANSITION")
        if not reason or not reason.strip():
            raise RegistryError("a transition needs a reason", "REASON_REQUIRED")
        approval = self._owner_approval(principal) if rule.owner_approval else None
        if rule.evaluator and principal.id == art["author"]:
            raise AuthorityError("the evaluator must differ from the artifact's author (BR-OPS-10)")
        if not rule.owner_approval and principal.role not in (Role.SYSTEM, Role.OWNER):
            raise AuthorityError("only the SYSTEM evaluator or the OWNER may execute this transition")
        if to in ("eligible", "active") and self._expired(artifact_hash):
            raise EvidenceError("artifact expired (registered expiry, or the retention of the data its evidence rests "
                                "on); it needs new evidence before promotion")
        if _upward(frm, to):
            self._refuse_if_evidence_expired(artifact_hash, f"{frm} -> {to}")
        self._check_required(artifact_hash, self._required(art, rule), list(evidence_ids))
        if to == "active":
            if not slot:
                raise RegistryError("activation needs a slot", "SLOT_REQUIRED")
            return self._activate(artifact_hash, slot, principal, list(evidence_ids), approval, reason)
        with self.r._tx() as c:
            rec = self._append(c, artifact_hash, frm, to, principal, list(evidence_ids), approval, slot, None, reason)
        return rec

    def _today(self) -> date:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.r.now() // 1_000_000_000, tz=timezone.utc).date()

    def _required(self, art: dict, rule: Rule) -> list[str]:
        """The evidence kinds a transition needs: the rule's own, or every gate kind the spec lists."""
        if rule.gate_evidence:
            spec = self.r.spec(art["spec_hash"])
            return [k for k in spec.promotion_evidence if k in GATE_KINDS]
        return list(rule.evidence)

    def _check_required(self, artifact_hash: str, required: list[str], evidence_ids: list[str]) -> set[str]:
        """Refuse unless every required kind has passed evidence of this artifact among ``evidence_ids``; returns
        the protocol hash(es) the cited gate evidence was evaluated under (at most one)."""
        have: dict[str, list[dict]] = {}
        protocols = set()
        for eid in evidence_ids:
            row = self.c.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            if row is None or row["artifact_hash"] != artifact_hash:
                raise EvidenceError(f"evidence {eid} does not exist for this artifact")
            body = json.loads(row["payload"])
            if stable_hash(body["payload"]) != row["payload_hash"]:
                raise EvidenceError(f"evidence {eid} payload does not match its hash")
            have.setdefault(row["kind"], []).append(dict(row))
            if row["kind"] in GATE_KINDS and row["protocol_hash"]:
                protocols.add(row["protocol_hash"])
        missing = [k for k in required if not any(e["passed"] for e in have.get(k, []))]
        if missing:
            raise EvidenceError(f"missing or failed evidence: {missing}")
        if len(protocols) > 1:
            raise EvidenceError("gate evidence was evaluated under different protocols")
        return protocols

    def active_in_slot(self, slot: str) -> str | None:
        """The artifact whose LATEST transition activated it in ``slot`` (an artifact deactivated here and later
        activated in another slot is not active here)."""
        row = self.c.execute("SELECT artifact_hash FROM transition WHERE slot=? AND to_status='active' "
                             "ORDER BY seq DESC LIMIT 1", (slot,)).fetchone()
        if row is None:
            return None
        last = self.c.execute("SELECT to_status, slot FROM transition WHERE artifact_hash=? ORDER BY seq DESC LIMIT 1",
                              (row["artifact_hash"],)).fetchone()
        return row["artifact_hash"] if last["to_status"] == "active" and last["slot"] == slot else None

    def _expired(self, artifact_hash: str) -> bool:
        return self.effective_expiry(artifact_hash) < self._today()

    # ------------------------------------------------- evidence retention (BR-DAT-16)
    def _retention_bound(self, manifest_hashes: list[str]) -> date | None:
        dates = [d for mh in manifest_hashes for d in (_retention(e) for e in (self.r.manifest(mh).get("datasets") or {}).values())
                 if d is not None]
        return min(dates) if dates else None

    def evidence_datasets(self, artifact_hash: str) -> dict[str, dict]:
        """{dataset: {retention_until, retention, manifests, scopes}} over the evaluation manifest and the manifests of
        the trials that the artifact's PASSED evidence cites (failed or agent-filed evidence supports nothing, so it
        bounds nothing). ``retention_until`` is the earliest date any of those entries states (None: no date);
        ``retention`` labels it: that date, ``unbounded`` (every entry says so) or ``unrecorded`` (an entry written
        before entries stated retention); ``scopes`` is what each manifest read (None: not recorded)."""
        art = self.artifact(artifact_hash)
        manifests = [art["body"]["evaluation_manifest_hash"]]
        for e in self.evidence(artifact_hash):
            if not e["passed"]:
                continue
            for ref in e["refs"]:
                if ref.get("type") == "trial":
                    mh = self.r.trial(ref["id"])["manifest_hash"]
                    if mh not in manifests:
                        manifests.append(mh)
        out: dict[str, dict] = {}
        labels: dict[str, set[str]] = {}
        for mh in manifests:
            for name, entry in (self.r.manifest(mh).get("datasets") or {}).items():
                d = _retention(entry)
                cur = out.setdefault(name, {"retention_until": None, "manifests": [], "scopes": {}})
                cur["manifests"].append(mh)
                cur["scopes"][mh] = entry.get("scope") if isinstance(entry, dict) else None
                raw = entry.get("retention_until") if isinstance(entry, dict) else None
                labels.setdefault(name, set()).add("date" if d is not None else (UNBOUNDED if raw == UNBOUNDED else "unrecorded"))
                if d is not None and (cur["retention_until"] is None or d < cur["retention_until"]):
                    cur["retention_until"] = d
        for name, cur in out.items():
            cur["retention"] = (cur["retention_until"].isoformat() if cur["retention_until"] is not None
                                else "unrecorded" if "unrecorded" in labels[name] else UNBOUNDED)
        return out

    # ---- recorded deletions
    def _deletions(self) -> dict[str, dict]:
        """Whole-dataset deletions recorded by the OWNER or the SYSTEM (``record_evidence_deletion``), by dataset."""
        try:
            rows = self.c.execute("SELECT * FROM evidence_deletion ORDER BY dataset").fetchall()
        except sqlite3.OperationalError:     # a registry opened read-only before any deletion was ever recorded
            return {}
        return {r["dataset"]: dict(r) for r in rows}

    def _store_deletions(self) -> list[dict]:
        if self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_deletion_event'").fetchone() is None:
            return []                        # a registry file older than the table, opened read-only
        return [dict(r) for r in self.c.execute("SELECT * FROM evidence_deletion_event ORDER BY recorded_at_ns, event_id")]

    def deletions(self) -> list[dict]:
        """Every recorded deletion, in one shape: ``deletion_id`` (``dataset:<name>`` for a whole-dataset record,
        the point-in-time store's event id for a swept event), dataset, cause, reason, deleted_at_ns and ``scope``
        (None for a whole dataset; else {entity: {observations, first_available_ns, last_available_ns}})."""
        out = [{"deletion_id": f"dataset:{name}", "dataset": name, "cause": "DATASET_DELETED", "reason": r["reason"],
                "deleted_at_ns": r["deleted_at_ns"], "scope": None, "recorded_at_ns": r["recorded_at_ns"],
                "recorded_by": r["recorded_by"]} for name, r in self._deletions().items()]
        for r in self._store_deletions():
            out.append({"deletion_id": r["event_id"], "dataset": r["dataset"], "cause": r["cause"], "reason": r["reason"],
                        "deleted_at_ns": r["deleted_at_ns"], "scope": json.loads(r["scope"]),
                        "observations": r["observations"], "raw_objects": json.loads(r["raw_objects"]),
                        "recorded_at_ns": r["recorded_at_ns"], "recorded_by": r["recorded_by"]})
        return out

    @staticmethod
    def _touching(datasets: dict[str, dict], deletion: dict) -> list[str]:
        """The manifests among ``datasets`` (``evidence_datasets``) whose data ``deletion`` removed."""
        ds = datasets.get(deletion["dataset"])
        if ds is None:
            return []
        return [mh for mh in ds["manifests"] if _scope_touched(deletion, ds["scopes"].get(mh))]

    def _manifest_deletions(self, manifest_hash: str) -> list[dict]:
        """The recorded deletions that removed data one manifest read."""
        entries = self.r.manifest(manifest_hash).get("datasets") or {}
        out = []
        for d in self.deletions():
            entry = entries.get(d["dataset"])
            if d["dataset"] in entries and _scope_touched(d, entry.get("scope") if isinstance(entry, dict) else None):
                out.append(d)
        return out

    def effective_expiry(self, artifact_hash: str) -> date:
        """min(registered expires_on, retention_until of every evidence dataset, the day before any recorded deletion
        of the data the evidence rests on): the date every expiry check uses."""
        return self._expiry(artifact_hash, self.evidence_datasets(artifact_hash), self.deletions())

    def _expiry(self, artifact_hash: str, datasets: dict[str, dict], deletions: list[dict]) -> date:
        expiry = date.fromisoformat(self.artifact(artifact_hash)["body"]["expires_on"])
        for ds in datasets.values():
            if ds["retention_until"] is not None and ds["retention_until"] < expiry:
                expiry = ds["retention_until"]
        for d in deletions:
            if self._touching(datasets, d):
                expiry = min(expiry, retention_date(d["deleted_at_ns"]))
        return expiry

    def record_evidence_deletion(self, dataset: str, *, principal: Principal, reason: str,
                                 deleted_at_ns: int | None = None) -> dict:
        """Record (append-only, once per dataset) that a whole dataset named by research manifests was deleted, e.g. its
        raw data at the end of its licence retention. In the same transaction every artifact resting on it becomes
        EVIDENCE_EXPIRED with the deletion as its evidence (``_apply_deletion``). Returns the deletion record."""
        self._no_agents(principal, "record evidence deletions")
        if principal.role not in (Role.OWNER, Role.SYSTEM):
            raise AuthorityError("only the OWNER or the SYSTEM data-retention process records a deletion")
        if not isinstance(dataset, str) or not dataset.strip() or not isinstance(reason, str) or not reason.strip():
            raise RegistryError("a deletion names its dataset and a reason", "INVALID_DELETION")
        if deleted_at_ns is not None and (type(deleted_at_ns) is not int or deleted_at_ns < 0):
            raise RegistryError("deleted_at_ns must be a non-negative integer", "INVALID_DELETION")
        now = self.r.now()
        with self.r._tx() as c:
            if c.execute("SELECT 1 FROM evidence_deletion WHERE dataset=?", (dataset,)).fetchone() is None:
                c.execute("INSERT INTO evidence_deletion VALUES (?,?,?,?,?,?)",
                          (dataset, deleted_at_ns if deleted_at_ns is not None else now, reason, now, principal.id,
                           principal.role.value))
                self._apply_deletion(c, f"dataset:{dataset}", principal)
        return self._deletions()[dataset]

    def sweep_store_deletions(self, store, *, principal: Principal) -> dict:
        """The pull-based link to the point-in-time store (BR-DAT-16): read the store's deletion events for every
        dataset the registry's artifacts rest on (``ResearchStore.deletion_events``), record each new one once
        (append-only) and, in the same transaction, make every artifact it touches EVIDENCE_EXPIRED with the deletion
        as its evidence. Idempotent: run it after every retention run or rights withdrawal, and on a schedule. It also
        completes a deletion recorded earlier whose artifacts do not cite it yet (a deletion recorded before deletions
        were filed as evidence)."""
        self._no_agents(principal, "sweep evidence deletions")
        if principal.role not in (Role.OWNER, Role.SYSTEM):
            raise AuthorityError("only the OWNER or the SYSTEM data-retention process sweeps deletions")
        datasets = sorted({name for a in self.artifacts() for name in self.evidence_datasets(a["artifact_hash"])})
        events = store.deletion_events(datasets=datasets) if datasets else []
        known = {d["deletion_id"] for d in self.deletions()}
        recorded, expired = [], []
        for ev in events:
            if ev["event_id"] in known:
                continue
            with self.r._tx() as c:
                if c.execute("SELECT 1 FROM evidence_deletion_event WHERE event_id=?", (ev["event_id"],)).fetchone():
                    continue                 # recorded meanwhile by another sweep
                c.execute("INSERT INTO evidence_deletion_event VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ev["event_id"], ev["dataset"], ev["cause"], ev["reason"], ev["deleted_at_ns"],
                           stable_dumps(ev["entities"]), ev["observations"], stable_dumps(ev["raw_objects"]),
                           self.r.now(), principal.id, principal.role.value))
                expired += self._apply_deletion(c, ev["event_id"], principal)
            known.add(ev["event_id"])
            recorded.append(ev["event_id"])
        for d in self.deletions():           # catch-up
            if self._pending(d):
                with self.r._tx() as c:
                    expired += self._apply_deletion(c, d["deletion_id"], principal)
        return {"datasets": datasets, "events_read": len(events), "recorded": recorded, "expired": expired}

    def _cites_deletion(self, artifact_hash: str, deletion_id: str) -> bool:
        return any(e["kind"] == "EVIDENCE_DELETION" and e["passed"] and e["payload"].get("deletion_id") == deletion_id
                   for e in self.evidence(artifact_hash))

    def _pending(self, deletion: dict) -> list[dict]:
        """The artifacts (not retired) a deletion touches that do not cite it yet, with the manifests it touched."""
        out = []
        for a in self.artifacts():
            if a["status"] == "retired":
                continue
            touched = self._touching(self.evidence_datasets(a["artifact_hash"]), deletion)
            if touched and not self._cites_deletion(a["artifact_hash"], deletion["deletion_id"]):
                out.append({**a, "manifests": touched})
        return out

    def _apply_deletion(self, c, deletion_id: str, principal: Principal) -> list[dict]:
        """Inside the caller's transaction: every artifact the deletion touches (``_pending``) receives passed
        EVIDENCE_DELETION evidence naming it, filed by ``principal``; an active one steps down to eligible in the slot it
        held, citing that evidence. Its effective expiry is already bounded by the deletion (``_expiry``)."""
        d = next(x for x in self.deletions() if x["deletion_id"] == deletion_id)
        out = []
        for a in self._pending(d):
            h, status = a["artifact_hash"], a["status"]
            payload = {"deletion_id": deletion_id, "dataset": d["dataset"], "cause": d["cause"], "reason": d["reason"],
                       "deleted_at_ns": d["deleted_at_ns"], "scope": d["scope"], "manifests": a["manifests"]}
            ev = self.add_evidence(h, "EVIDENCE_DELETION", payload, refs=[{"type": "deletion", "id": deletion_id}],
                                   principal=principal, evaluation_started_at_ns=self.r.now())
            if not ev["passed"]:
                raise EvidenceError(f"the deletion evidence for {h} did not pass: {ev['reasons']}")
            record_hash = None
            if status == "active":
                slot = c.execute("SELECT slot FROM transition WHERE artifact_hash=? AND to_status='active' ORDER BY seq "
                                 "DESC LIMIT 1", (h,)).fetchone()["slot"]
                record_hash = self._append(c, h, "active", "eligible", principal, [ev["evidence_id"]], None, slot, None,
                                           f"EVIDENCE_EXPIRED: {d['cause']} of {d['dataset']} ({d['reason']})")["record_hash"]
            out.append({"artifact_hash": h, "status_before": status, "deletion_id": deletion_id,
                        "evidence_id": ev["evidence_id"], "deactivated": record_hash is not None,
                        "record_hash": record_hash})
        return out

    def evidence_status(self, artifact_hash: str) -> dict:
        """BR-DAT-16: CURRENT, or EVIDENCE_EXPIRED once a recorded deletion removed data the artifact's evidence rests
        on. ``deleted`` lists those deletions and the EVIDENCE_DELETION evidence that cites each (None until a sweep
        files it, for a deletion recorded before deletions were filed as evidence)."""
        datasets = self.evidence_datasets(artifact_hash)
        deletions = self.deletions()
        cited = {e["payload"].get("deletion_id"): e["evidence_id"] for e in self.evidence(artifact_hash)
                 if e["kind"] == "EVIDENCE_DELETION" and e["passed"]}
        hit = []
        for d in deletions:
            touched = self._touching(datasets, d)
            if touched:
                hit.append({"deletion_id": d["deletion_id"], "dataset": d["dataset"], "cause": d["cause"],
                            "deleted_at_ns": d["deleted_at_ns"], "reason": d["reason"], "manifests": touched,
                            "evidence_id": cited.get(d["deletion_id"])})
        retention = [d["retention_until"] for d in datasets.values() if d["retention_until"] is not None]
        return {"status": "EVIDENCE_EXPIRED" if hit else "CURRENT", "deleted": hit,
                "retention_until": min(retention).isoformat() if retention else None,
                "retention_unrecorded": sorted(n for n, d in datasets.items() if d["retention"] == "unrecorded"),
                "effective_expiry": self._expiry(artifact_hash, datasets, deletions).isoformat(),
                "datasets": {name: {"retention_until": d["retention_until"].isoformat() if d["retention_until"] else None,
                                    "retention": d["retention"]} for name, d in sorted(datasets.items())}}

    def _refuse_if_evidence_expired(self, artifact_hash: str, what: str) -> None:
        st = self.evidence_status(artifact_hash)
        if st["status"] == "EVIDENCE_EXPIRED":
            raise EvidenceError(f"EVIDENCE_EXPIRED: {what} refused: the evidence rests on deleted data "
                                f"{[d['dataset'] for d in st['deleted']]}; it needs new evidence (BR-DAT-16)")

    def _activate(self, artifact_hash: str, slot: str, principal: Principal, evidence_ids: list[str], approval: dict | None,
                  reason: str) -> dict:
        if self._expired(artifact_hash):   # every activation path, including automatic selection
            raise EvidenceError("artifact expired; it needs new evidence before activation")
        self._refuse_if_evidence_expired(artifact_hash, "activation")     # BR-DAT-16, every activation path
        incumbent = self.active_in_slot(slot)
        if incumbent == artifact_hash:
            raise RegistryError("artifact is already active in this slot", "NOOP")
        with self.r._tx() as c:
            if incumbent is not None:
                self._append(c, incumbent, "active", "eligible", principal, [], approval, slot, artifact_hash,
                             f"superseded in slot {slot}")
            rec = self._append(c, artifact_hash, "eligible", "active", principal, evidence_ids, approval, slot, incumbent, reason)
        return rec

    # ---------------------------------------------------- selection policy
    def approve_selection_policy(self, body: dict, principal: Principal) -> str:
        """AUT-03: the owner-approved envelope inside which automatic selection may act."""
        need = {"policy_id", "slot", "strategy_id", "parameter_domain", "instrument_set", "risk_budget", "protocol_hash",
                "max_activations_per_period", "period_days", "ranking"}
        if not isinstance(body, dict) or set(body) != need:
            raise EvidenceError(f"selection policy must have exactly {sorted(need)}")
        self.protocol(body["protocol_hash"])
        if body["ranking"] != "PAIRED_BOOTSTRAP.lower":
            raise EvidenceError("ranking must be PAIRED_BOOTSTRAP.lower")
        for name, bounds in body["parameter_domain"].items():
            if not (isinstance(bounds, list) and len(bounds) == 2 and Fixed.parse(bounds[0]) <= Fixed.parse(bounds[1])):
                raise EvidenceError(f"parameter_domain.{name} must be [min, max]")
        if type(body["max_activations_per_period"]) is not int or body["max_activations_per_period"] < 1 or \
                type(body["period_days"]) is not int or body["period_days"] < 1:
            raise EvidenceError("activation limit and period must be positive integers")
        approval = self._owner_approval(principal)
        if self.c.execute("SELECT 1 FROM selection_policy WHERE policy_id=?", (body["policy_id"],)).fetchone():
            raise RegistryError("policy id already used; policies are immutable (create a new one)", "POLICY_EXISTS")
        self.c.execute("INSERT INTO selection_policy VALUES (?,?,?,?,?)", (body["policy_id"], stable_dumps(body),
                                                                           stable_hash(body), approval["by"], approval["at_ns"]))
        return stable_hash(body)

    def selection_policy(self, policy_id: str) -> dict:
        row = self.c.execute("SELECT * FROM selection_policy WHERE policy_id=?", (policy_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown selection policy {policy_id}", "UNKNOWN_POLICY")
        body = json.loads(row["body"])
        if stable_hash(body) != row["body_hash"]:
            raise EvidenceError("selection policy body does not match its hash")
        return {"body": body, "approved_by": row["approved_by"], "approved_at_ns": row["approved_at_ns"], "hash": row["body_hash"]}

    def within_policy(self, policy: dict, artifact_hash: str) -> tuple[bool, list[str]]:
        art = self.artifact(artifact_hash)
        why = []
        if art["strategy_id"] != policy["strategy_id"]:
            why.append("strategy outside policy")
        params = art["body"]["bundle"]["parameters"]
        for name, (lo, hi) in policy["parameter_domain"].items():
            if name not in params or not Fixed.parse(lo) <= Fixed.parse(params[name]) <= Fixed.parse(hi):
                why.append(f"parameter {name}={params.get(name)} outside [{lo}, {hi}]")
        extra = set(params) - set(policy["parameter_domain"])
        if extra:
            why.append(f"parameters not covered by the approved domain: {sorted(extra)}")
        if not set(art["body"]["instruments"]) <= set(policy["instrument_set"]):
            why.append("instruments outside the approved set")
        return not why, why

    def auto_select(self, policy_id: str, principal: Principal, *, artifact_hash: str | None = None) -> dict:
        """Automatic selection (AUT-03): only eligible artifacts inside the owner-approved policy; never expands it."""
        self._no_agents(principal, "run automatic selection")
        if principal.role is not Role.SYSTEM:
            raise AuthorityError("automatic selection runs as the SYSTEM promotion service")
        pol = self.selection_policy(policy_id)
        body = pol["body"]
        if artifact_hash is not None:
            ok, why = self.within_policy(body, artifact_hash)
            if not ok:
                raise AuthorityError(f"automatic selection outside the approved policy refused: {why}")
            if self.status(artifact_hash) != "eligible":
                raise AuthorityError("automatic selection may choose only eligible artifacts")
            if self._expired(artifact_hash):
                raise AuthorityError("automatic selection may not choose an expired artifact")
            if self.evidence_status(artifact_hash)["status"] == "EVIDENCE_EXPIRED":
                raise AuthorityError("automatic selection may not choose an artifact whose evidence was deleted (BR-DAT-16)")
            pool = [artifact_hash]
        else:
            pool = [r["artifact_hash"] for r in self.c.execute("SELECT artifact_hash FROM artifact WHERE strategy_id=? "
                                                               "ORDER BY artifact_hash", (body["strategy_id"],))]
        ranked, refused = [], {}
        for h in pool:
            if self.status(h) != "eligible":
                continue
            ok, why = self.within_policy(body, h)
            if not ok:
                refused[h] = why
                continue
            if self._expired(h):
                refused[h] = ["artifact expired"]
                continue
            if self.evidence_status(h)["status"] == "EVIDENCE_EXPIRED":
                refused[h] = ["EVIDENCE_EXPIRED: the evidence rests on deleted data"]
                continue
            score = None
            for e in self.evidence(h):
                if e["kind"] == "PAIRED_BOOTSTRAP" and e["passed"] and e["protocol_hash"] == body["protocol_hash"]:
                    # rank by the same bound the gate used (conservative_lower), not the looser percentile bound
                    b = e["payload"]["bootstrap"]
                    score = max(score if score is not None else -1e300, b.get("conservative_lower", b["lower"]))
            if score is None:
                refused[h] = ["no passed PAIRED_BOOTSTRAP evidence under the policy's protocol"]
                continue
            ranked.append((score, h))
        if not ranked:
            return {"selected": None, "refused": refused, "reason": "no eligible artifact inside the approved policy"}
        ranked.sort(key=lambda x: (-x[0], x[1]))
        chosen = ranked[0][1]
        if self.active_in_slot(body["slot"]) == chosen:
            return {"selected": chosen, "changed": False, "refused": refused}
        window = body["period_days"] * 86_400 * 1_000_000_000
        recent = self.c.execute("SELECT COUNT(*) FROM transition WHERE slot=? AND to_status='active' AND at_ns>=? AND "
                                "json_extract(approval, '$.policy_id')=?", (body["slot"], self.r.now() - window, policy_id)).fetchone()[0]
        if recent >= body["max_activations_per_period"]:
            return {"selected": None, "refused": refused, "reason": "activation limit for the period reached"}
        approval = {"kind": "SELECTION_POLICY", "policy_id": policy_id, "policy_hash": pol["hash"],
                    "approved_by": pol["approved_by"], "approved_at_ns": pol["approved_at_ns"], "at_ns": self.r.now()}
        rec = self._activate(chosen, body["slot"], principal, [], approval, f"automatic selection under {policy_id}")
        return {"selected": chosen, "changed": True, "transition": rec, "refused": refused}

    # ------------------------------------------------------ shadow envelope
    def approve_shadow_envelope(self, body: dict, principal: Principal) -> str:
        """ADR-0017 #2: the owner-approved envelope inside which the SYSTEM may promote validated -> shadow.
        Immutable once approved (a change is a new envelope id); its hash is recorded on every promotion it allows."""
        if not isinstance(body, dict) or set(body) != SHADOW_ENVELOPE_FIELDS:
            raise EvidenceError(f"shadow envelope must have exactly {sorted(SHADOW_ENVELOPE_FIELDS)}")
        if not isinstance(body["envelope_id"], str) or not body["envelope_id"].strip():
            raise EvidenceError("envelope_id must be a non-empty string")
        if not isinstance(body["strategy_id"], str) or not body["strategy_id"].strip():
            raise EvidenceError("strategy_id (the artifact class) must be a non-empty string")
        self.protocol(body["protocol_hash"])
        dom = body["parameter_domain"]
        if not isinstance(dom, dict) or not dom:
            raise EvidenceError("parameter_domain must name every parameter with [min, max]")
        for name, bounds in dom.items():
            if not (isinstance(bounds, list) and len(bounds) == 2 and all(isinstance(b, str) for b in bounds)
                    and Fixed.parse(bounds[0]) <= Fixed.parse(bounds[1])):
                raise EvidenceError(f"parameter_domain.{name} must be [min, max] decimal strings")
        insts = body["instrument_set"]
        if not isinstance(insts, list) or not insts or not all(isinstance(i, str) and i for i in insts):
            raise EvidenceError("instrument_set must be a non-empty list of instrument ids")
        rb = body["risk_budget"]
        if not isinstance(rb, dict) or set(rb) != set(RISK_BUDGET_FIELDS):
            raise EvidenceError(f"risk_budget must have exactly {list(RISK_BUDGET_FIELDS)}")
        for k in RISK_BUDGET_FIELDS:
            if not isinstance(rb[k], str) or not Fixed(0) < Fixed.parse(rb[k]) <= Fixed(1):
                raise EvidenceError(f"risk_budget.{k} must be a decimal string in (0, 1]")
        for k in ("max_promotions_per_period", "period_days"):
            if type(body[k]) is not int or body[k] < 1:
                raise EvidenceError(f"{k} must be a positive integer")
        approval = self._owner_approval(principal)
        if self.c.execute("SELECT 1 FROM shadow_envelope WHERE envelope_id=?", (body["envelope_id"],)).fetchone():
            raise RegistryError("envelope id already used; envelopes are immutable (create a new one)", "ENVELOPE_EXISTS")
        h = stable_hash(body)
        self.c.execute("INSERT INTO shadow_envelope VALUES (?,?,?,?,?)", (body["envelope_id"], stable_dumps(body), h,
                                                                          approval["by"], approval["at_ns"]))
        return h

    def shadow_envelope(self, envelope_id: str) -> dict:
        row = self.c.execute("SELECT * FROM shadow_envelope WHERE envelope_id=?", (envelope_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown shadow envelope {envelope_id}", "UNKNOWN_ENVELOPE")
        body = json.loads(row["body"])
        if stable_hash(body) != row["body_hash"]:
            raise EvidenceError("shadow envelope body does not match its hash")
        return {"body": body, "approved_by": row["approved_by"], "approved_at_ns": row["approved_at_ns"], "hash": row["body_hash"]}

    def within_envelope(self, envelope: dict, artifact_hash: str) -> tuple[bool, list[str]]:
        """Class, parameter domain and instrument set as for a selection policy, plus the risk budget: every risk
        limit the artifact's spec declares must be at or below the envelope's."""
        _, why = self.within_policy(envelope, artifact_hash)
        limits = self.r.spec(self.artifact(artifact_hash)["spec_hash"]).body["risk_limits"]
        for k in RISK_BUDGET_FIELDS:
            if Fixed.parse(limits[k]) > Fixed.parse(envelope["risk_budget"][k]):
                why.append(f"risk limit {k}={limits[k]} exceeds the envelope's risk budget {envelope['risk_budget'][k]}")
        return not why, why

    def auto_shadow(self, envelope_id: str, artifact_hash: str, principal: Principal, *, evidence_ids: list[str]) -> dict:
        """Automatic validated -> shadow promotion (ADR-0017 #2) inside an owner-approved envelope. Runs as the SYSTEM
        promotion service. Refused unless the artifact is validated and unexpired, lies inside the envelope, and the
        cited evidence passes every gate the spec lists under the envelope's own protocol. The transition record
        carries that evidence and the envelope (id, hash, approver), never a per-artifact owner approval."""
        self._no_agents(principal, "run automatic shadow promotion")
        if principal.role is not Role.SYSTEM:
            raise AuthorityError("automatic shadow promotion runs as the SYSTEM promotion service")
        env = self.shadow_envelope(envelope_id)
        body = env["body"]
        art = self.artifact(artifact_hash)
        if art["status"] != "validated":
            raise AuthorityError(f"automatic shadow promotion applies only to validated artifacts (this one is "
                                 f"'{art['status']}')")
        ok, why = self.within_envelope(body, artifact_hash)
        if not ok:
            raise AuthorityError(f"automatic shadow promotion outside the approved envelope refused: {why}")
        if self._expired(artifact_hash):
            raise EvidenceError("artifact expired; it needs new evidence before promotion")
        self._refuse_if_evidence_expired(artifact_hash, "automatic shadow promotion")
        evidence_ids = list(evidence_ids)
        protocols = self._check_required(artifact_hash, self._required(art, RULES[("validated", "shadow")]), evidence_ids)
        if protocols != {body["protocol_hash"]}:
            raise AuthorityError("the cited gate evidence was not evaluated under the envelope's evaluation protocol")
        window = body["period_days"] * 86_400 * 1_000_000_000
        recent = self.c.execute("SELECT COUNT(*) FROM transition WHERE to_status='shadow' AND at_ns>=? AND "
                                "json_extract(approval, '$.envelope_id')=?", (self.r.now() - window, envelope_id)).fetchone()[0]
        if recent >= body["max_promotions_per_period"]:
            raise AuthorityError(f"the envelope's limit of {body['max_promotions_per_period']} automatic promotions per "
                                 f"{body['period_days']} days is reached")
        approval = {"kind": "SHADOW_ENVELOPE", "envelope_id": envelope_id, "envelope_hash": env["hash"],
                    "approved_by": env["approved_by"], "approved_at_ns": env["approved_at_ns"], "at_ns": self.r.now()}
        with self.r._tx() as c:
            now = c.execute("SELECT to_status FROM transition WHERE artifact_hash=? ORDER BY seq DESC LIMIT 1",
                            (artifact_hash,)).fetchone()["to_status"]
            if now != "validated":      # moved concurrently: never record a transition from a status it no longer has
                raise RegistryError(f"artifact is now '{now}', not validated", "INVALID_TRANSITION")
            return self._append(c, artifact_hash, "validated", "shadow", principal, evidence_ids, approval, None, None,
                                f"automatic shadow promotion inside envelope {envelope_id}")

    def artifacts(self) -> list[dict]:
        out = []
        for r in self.c.execute("SELECT artifact_hash, spec_hash, strategy_id, author, author_role, registered_at_ns FROM artifact "
                                "ORDER BY registered_at_ns, artifact_hash"):
            d = dict(r)
            d["status"] = self.status(d["artifact_hash"])
            out.append(d)
        return out
