"""Bitemporal point-in-time research store (ADR-0015; DAT-02, DAT-04, DAT-06).

Two clocks per observation:
  * available_time — earliest legitimate decision-use time of the publication;
  * known_from / known_to — when *this system* held this version as current.
A restatement is a later publication (its own available_time); a silent
correction of the same publication closes the previous version's knowledge
interval. Nothing is updated in place except closing that interval.
``as_of(t)`` answers "what had been published by t" (research);
``as_of(t, knowledge_cutoff_ns=c)`` answers "what did this system know at c" (audit).

Values are exact decimal text (never floats). Missing data is a row with a
``missing_reason``, never a zero. The store is research-side only: the money
core never reads it (ADR-0002/0015).

Retention and deletion (BR-DAT-16). A raw payload carries its licence deadline
(``raw_object.retention_until_ns``) and, once deleted, ``deleted_at_ns``; an
observation extracted from it names it in ``source_ref`` (``raw:<sha256>#...``).
``retention_facts`` answers, read-only, what a research run reading a dataset is
bound by. Deletion is the one exception to "nothing is updated in place": it
happens only when a deadline elapses (``enforce_retention``) or the rights are
withdrawn (``withdraw_rights``). It removes the observations and the raw files,
keeps the raw rows (hash and request, with ``deleted_at_ns``), and appends one
``deletion_event`` per dataset, which the research registry reads
(``deletion_events``) to expire the artifacts whose evidence rested on the data."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from ..domain.fixed import Fixed

MISSING_REASONS = frozenset({"NOT_REPORTED", "NOT_YET_PUBLISHED", "PROVIDER_DELAYED", "UNAUTHORIZED", "NOT_APPLICABLE", "ERROR"})
OPEN_END = 2 ** 62  # sentinel for "still current" in known_to
NS_PER_DAY = 86_400 * 1_000_000_000
DELETION_CAUSES = ("RETENTION_ELAPSED", "RIGHTS_WITHDRAWN")
# The raw payload an observation was extracted from: ``source_ref`` = "raw:<64 hex>" optionally followed by "#...".
_RAW_JOIN = ("o.source_ref LIKE 'raw:%' AND length(o.source_ref) >= 68 AND r.sha256 = substr(o.source_ref, 5, 64)")

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS raw_object (
  sha256 TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, request TEXT NOT NULL,
  receipt_time_ns INTEGER NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
  retention_until_ns INTEGER, deleted_at_ns INTEGER
);
CREATE TABLE IF NOT EXISTS deletion_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, dataset TEXT NOT NULL,
  cause TEXT NOT NULL CHECK (cause IN ('RETENTION_ELAPSED', 'RIGHTS_WITHDRAWN')), reason TEXT NOT NULL,
  deleted_at_ns INTEGER NOT NULL, observations INTEGER NOT NULL, entities TEXT NOT NULL, raw_objects TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deletion_event_dataset ON deletion_event(dataset, seq);
CREATE TRIGGER IF NOT EXISTS deletion_event_no_update BEFORE UPDATE ON deletion_event
  BEGIN SELECT RAISE(ABORT, 'deletion_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS deletion_event_no_delete BEFORE DELETE ON deletion_event
  BEGIN SELECT RAISE(ABORT, 'deletion_event is append-only'); END;
CREATE TABLE IF NOT EXISTS ingestion_run (
  run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, started_ns INTEGER NOT NULL,
  finished_ns INTEGER, status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity (
  entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, attributes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_alias (
  scheme TEXT NOT NULL, value TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entity(entity_id),
  valid_from_ns INTEGER NOT NULL, valid_to_ns INTEGER NOT NULL, source_ref TEXT NOT NULL,
  PRIMARY KEY (scheme, value, valid_from_ns)
);
CREATE TABLE IF NOT EXISTS observation (
  obs_id INTEGER PRIMARY KEY,
  dataset TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT NOT NULL,
  period_start TEXT, period_end TEXT, period_kind TEXT,
  value TEXT, value_text TEXT, unit TEXT NOT NULL, currency TEXT,
  missing_reason TEXT,
  event_time_ns INTEGER, published_time_ns INTEGER, receipt_time_ns INTEGER NOT NULL, available_time_ns INTEGER NOT NULL,
  known_from_ns INTEGER NOT NULL, known_to_ns INTEGER NOT NULL,
  supersedes INTEGER REFERENCES observation(obs_id),
  publication_id TEXT NOT NULL,
  source_ref TEXT NOT NULL, quality TEXT NOT NULL,
  CHECK ((value IS NOT NULL) + (value_text IS NOT NULL) + (missing_reason IS NOT NULL) = 1)
);
CREATE INDEX IF NOT EXISTS obs_key ON observation(dataset, entity_id, field, period_end, known_from_ns);
CREATE INDEX IF NOT EXISTS obs_available ON observation(entity_id, field, available_time_ns);
CREATE TABLE IF NOT EXISTS derived_metric (
  metric_id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL, metric TEXT NOT NULL, formula TEXT NOT NULL,
  as_of_ns INTEGER NOT NULL, value TEXT, unit TEXT NOT NULL, missing_reason TEXT,
  inputs TEXT NOT NULL, computed_at_ns INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Observation:
    dataset: str
    entity_id: str
    field: str
    unit: str
    receipt_time_ns: int
    available_time_ns: int
    source_ref: str
    value: Fixed | None = None
    value_text: str | None = None
    missing_reason: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_kind: str | None = None        # e.g. "FY", "Q1", "INSTANT", "DAY"
    currency: str | None = None
    event_time_ns: int | None = None
    published_time_ns: int | None = None
    quality: tuple[str, ...] = ()
    publication_id: str = ""   # source document identity (e.g. SEC accession); "" = one publication per available time


    def __post_init__(self) -> None:
        if sum(x is not None for x in (self.value, self.value_text, self.missing_reason)) != 1:
            raise ValueError("exactly one of value, value_text, missing_reason")
        if self.missing_reason is not None and self.missing_reason not in MISSING_REASONS:
            raise ValueError(f"unknown missing_reason {self.missing_reason}")
        if self.value is not None and not isinstance(self.value, Fixed):
            raise TypeError("value must be Fixed (exact decimal)")
        if self.available_time_ns < 0 or self.receipt_time_ns < 0:
            raise ValueError("times must be non-negative")


class ResearchStore:
    def __init__(self, path: str | Path, raw_dir: str | Path | None = None):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.raw_dir = Path(raw_dir) if raw_dir else (Path(self.path).parent / "raw" if self.path != ":memory:" else None)

    @classmethod
    def open_readonly(cls, path: str | Path) -> "ResearchStore":
        """Open an existing store for reading only (research consumers such as the agent worker and the
        terminal API): no schema creation, no journal-mode change, and any write raises. A missing file
        raises ``FileNotFoundError`` instead of creating an empty store."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"research store {target} does not exist")
        store = cls.__new__(cls)
        store.path = str(target)
        store.conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, isolation_level=None)
        store.conn.row_factory = sqlite3.Row
        store.raw_dir = None
        return store

    # ------------------------------------------------------------ raw layer
    def put_raw(self, data: bytes, *, provider: str, dataset: str, request: dict, receipt_time_ns: int,
                media_type: str, retention_until_ns: int | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        gone = self.conn.execute("SELECT deleted_at_ns FROM raw_object WHERE sha256=? AND deleted_at_ns IS NOT NULL",
                                 (digest,)).fetchone()
        if gone is not None:
            # The row would keep saying "deleted" while the file came back, and retention would never delete it again.
            raise ValueError(f"raw payload {digest} was deleted at {gone['deleted_at_ns']} (retention or rights "
                             "withdrawal); it is not stored again")
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            target = self.raw_dir / digest
            if not target.exists():
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(target)
        self.conn.execute("INSERT OR IGNORE INTO raw_object VALUES (?,?,?,?,?,?,?,?,NULL)",
                          (digest, provider, dataset, json.dumps(request, sort_keys=True), receipt_time_ns, len(data),
                           media_type, retention_until_ns))
        return digest

    # ------------------------------------------------------------ entities
    def upsert_entity(self, entity_id: str, kind: str, name: str, attributes: dict | None = None) -> None:
        self.conn.execute("INSERT INTO entity VALUES (?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET name=excluded.name, "
                          "attributes=excluded.attributes", (entity_id, kind, name, json.dumps(attributes or {}, sort_keys=True)))

    def add_alias(self, scheme: str, value: str, entity_id: str, valid_from_ns: int, source_ref: str,
                  valid_to_ns: int = OPEN_END) -> None:
        """Symbology history: a ticker can map to different entities over time (book Ch. 3)."""
        self.conn.execute("INSERT OR IGNORE INTO entity_alias VALUES (?,?,?,?,?,?)",
                          (scheme, value, entity_id, valid_from_ns, valid_to_ns, source_ref))

    def resolve(self, scheme: str, value: str, at_ns: int) -> str | None:
        rows = self.conn.execute("SELECT entity_id FROM entity_alias WHERE scheme=? AND value=? AND valid_from_ns<=? AND valid_to_ns>?",
                                 (scheme, value, at_ns, at_ns)).fetchall()
        ids = {r["entity_id"] for r in rows}
        if len(ids) > 1:
            raise LookupError(f"{scheme}:{value} is ambiguous at {at_ns}: {sorted(ids)}")
        return ids.pop() if ids else None

    # ------------------------------------------------------------ observations
    def record(self, obs: Observation, known_at_ns: int) -> tuple[int, str]:
        """Insert a version of one publication. Returns (obs_id, outcome): NEW | UNCHANGED | CORRECTED.

        A *publication* is keyed by (dataset, entity, field, period, publication_id), where the
        publication id is the source document (an SEC accession number); two documents filed at
        the same instant are two publications, never a correction of each other. A later
        publication for the same period (e.g. a restatement filed later) is simply another row with
        its own available_time; nothing is closed. If the *same* publication arrives with different
        content (a silent provider correction), the previous version's knowledge interval is closed
        and the new version links to it, flagged ``silent_correction``."""
        pub = obs.publication_id or f"t:{obs.available_time_ns}"
        key = (obs.dataset, obs.entity_id, obs.field, obs.period_start, obs.period_end, obs.unit, pub)
        cur = self.conn.execute(
            "SELECT obs_id, value, value_text, missing_reason, unit, currency, available_time_ns FROM observation WHERE dataset=? AND entity_id=? "
            "AND field=? AND COALESCE(period_start,'')=COALESCE(?, '') AND COALESCE(period_end,'')=COALESCE(?, '') AND unit=? "
            "AND publication_id=? AND known_to_ns=?",
            key + (OPEN_END,)).fetchone()
        new_value = obs.value.to_str() if obs.value is not None else None
        quality = list(obs.quality)
        if cur is not None:
            if (cur["value"], cur["value_text"], cur["missing_reason"], cur["unit"], cur["currency"], cur["available_time_ns"]) == \
                    (new_value, obs.value_text, obs.missing_reason, obs.unit, obs.currency, obs.available_time_ns):
                return cur["obs_id"], "UNCHANGED"
            quality.append("silent_correction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            supersedes = None
            if cur is not None:
                self.conn.execute("UPDATE observation SET known_to_ns=? WHERE obs_id=?", (known_at_ns, cur["obs_id"]))
                supersedes = cur["obs_id"]
            c = self.conn.execute(
                "INSERT INTO observation(dataset, entity_id, field, period_start, period_end, period_kind, value, value_text, unit, currency, "
                "missing_reason, event_time_ns, published_time_ns, receipt_time_ns, available_time_ns, known_from_ns, known_to_ns, "
                "supersedes, publication_id, source_ref, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs.dataset, obs.entity_id, obs.field, obs.period_start, obs.period_end, obs.period_kind, new_value, obs.value_text,
                 obs.unit, obs.currency, obs.missing_reason, obs.event_time_ns, obs.published_time_ns, obs.receipt_time_ns,
                 obs.available_time_ns, known_at_ns, OPEN_END, supersedes, pub, obs.source_ref, json.dumps(quality)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return c.lastrowid, "CORRECTED" if supersedes else "NEW"

    def as_of(self, entity_id: str, field: str, t_ns: int, *, dataset: str | None = None,
              knowledge_cutoff_ns: int | None = None) -> list[sqlite3.Row]:
        """Point-in-time values: for each period, the latest *publication* with available_time <= t.

        * Research mode (default): uses the current knowledge of each publication, so a later
          restatement never leaks backwards but an ingestion fix of the same publication applies.
        * Audit mode (``knowledge_cutoff_ns``): additionally restricts to versions this system held
          at that instant (known_from <= cutoff < known_to), i.e. exactly what a live decision saw."""
        sql = "SELECT * FROM observation WHERE entity_id=? AND field=? AND available_time_ns<=?"
        params: list = [entity_id, field, t_ns]
        if knowledge_cutoff_ns is None:
            sql += " AND known_to_ns=?"
            params.append(OPEN_END)
        else:
            sql += " AND known_from_ns<=? AND known_to_ns>?"
            params += [knowledge_cutoff_ns, knowledge_cutoff_ns]
        if dataset:
            sql += " AND dataset=?"
            params.append(dataset)
        best: dict[tuple, sqlite3.Row] = {}
        for r in self.conn.execute(sql, tuple(params)):
            # Latest publication wins; ties at the same instant break deterministically by publication id
            # (``conflicts()`` exposes same-instant publications with different values for review).
            k = (r["dataset"], r["period_kind"], r["period_start"], r["period_end"], r["unit"])
            rank = (r["available_time_ns"], r["publication_id"], r["known_from_ns"])
            if k not in best or rank > (best[k]["available_time_ns"], best[k]["publication_id"], best[k]["known_from_ns"]):
                best[k] = r
        return sorted(best.values(), key=lambda r: (r["period_end"] or "", r["period_start"] or ""))

    def latest_as_of(self, entity_id: str, field: str, t_ns: int, *, dataset: str | None = None, period_kind: str | None = None,
                     knowledge_cutoff_ns: int | None = None):
        rows = [r for r in self.as_of(entity_id, field, t_ns, dataset=dataset, knowledge_cutoff_ns=knowledge_cutoff_ns)
                if period_kind is None or r["period_kind"] == period_kind]
        return rows[-1] if rows else None

    def conflicts(self, entity_id: str, field: str) -> list[sqlite3.Row]:
        """Distinct current publications for one period available at the same instant with different values."""
        return self.conn.execute(
            "SELECT a.period_start, a.period_end, a.unit, a.available_time_ns, a.publication_id AS pub_a, a.value AS value_a, "
            "b.publication_id AS pub_b, b.value AS value_b FROM observation a JOIN observation b ON a.entity_id=b.entity_id "
            "AND a.field=b.field AND COALESCE(a.period_start,'')=COALESCE(b.period_start,'') AND a.period_end=b.period_end "
            "AND a.unit=b.unit AND a.available_time_ns=b.available_time_ns AND a.publication_id<b.publication_id "
            "AND a.known_to_ns=? AND b.known_to_ns=? AND COALESCE(a.value,'')<>COALESCE(b.value,'') "
            "WHERE a.entity_id=? AND a.field=?", (OPEN_END, OPEN_END, entity_id, field)).fetchall()

    def history(self, entity_id: str, field: str, period_end: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM observation WHERE entity_id=? AND field=? AND period_end=? ORDER BY known_from_ns",
                                 (entity_id, field, period_end)).fetchall()

    def record_metric(self, entity_id: str, metric: str, formula: str, as_of_ns: int, value: Fixed | None, unit: str,
                      inputs: list[int], computed_at_ns: int, missing_reason: str | None = None) -> int:
        """Derived metric with its formula id@version and input observation ids (DAT-05)."""
        if (value is None) == (missing_reason is None):
            raise ValueError("exactly one of value or missing_reason")
        c = self.conn.execute("INSERT INTO derived_metric(entity_id, metric, formula, as_of_ns, value, unit, missing_reason, inputs, computed_at_ns) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", (entity_id, metric, formula, as_of_ns, value.to_str() if value is not None else None,
                                                             unit, missing_reason, json.dumps(inputs), computed_at_ns))
        return c.lastrowid

    # ------------------------------------------------------------ retention and deletion (BR-DAT-16)
    def retention_facts(self, dataset: str, entity_ids: Iterable[str], available_until_ns: int) -> dict:
        """Read-only retention facts of the observations a research run reads: those of ``dataset`` for
        ``entity_ids`` available by ``available_until_ns`` under current knowledge (what ``research.bars.load_series``
        loads). How many there are, the earliest receipt time (a registry retention period runs from receipt) and the
        earliest ``retention_until_ns`` of the raw payloads they were extracted from."""
        ids = sorted(set(entity_ids))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(o.receipt_time_ns) AS first_receipt, MIN(r.retention_until_ns) AS raw_until, "
            "COUNT(r.retention_until_ns) AS bounded, COUNT(r.deleted_at_ns) AS on_deleted_raw "
            f"FROM observation o LEFT JOIN raw_object r ON {_RAW_JOIN} "
            "WHERE o.dataset=? AND o.entity_id IN (SELECT value FROM json_each(?)) AND o.available_time_ns<=? "
            "AND o.known_to_ns=?", (dataset, json.dumps(ids), available_until_ns, OPEN_END)).fetchone()
        return {"dataset": dataset, "entities": ids, "available_until_ns": available_until_ns, "observations": row["n"],
                "first_receipt_ns": row["first_receipt"], "raw_retention_until_ns": row["raw_until"],
                "bounded_observations": row["bounded"], "observations_on_deleted_raw": row["on_deleted_raw"]}

    def deletion_events(self, *, datasets: Iterable[str] | None = None, after_seq: int = 0) -> list[dict]:
        """Read-only: deletion events, oldest first, optionally only for ``datasets`` and after ``after_seq``. Each
        names its dataset, cause, reason, ``deleted_at_ns``, the raw payloads deleted and, per entity, how many
        observations went and the span of their availability times. A store with no deletion table has none."""
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deletion_event'").fetchone() is None:
            return []
        sql, params = "SELECT * FROM deletion_event WHERE seq>?", [after_seq]
        if datasets is not None:
            sql += " AND dataset IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(sorted(set(datasets))))
        return [self._event(r) for r in self.conn.execute(sql + " ORDER BY seq", params)]

    @staticmethod
    def _event(row) -> dict:
        d = dict(row)
        d["entities"] = json.loads(d["entities"])
        d["raw_objects"] = json.loads(d["raw_objects"])
        return d

    def enforce_retention(self, now_ns: int, *, retention_days: Mapping[str, int | None] | None = None,
                          reason: str = "licence retention elapsed") -> list[dict]:
        """Delete what the licences no longer allow the store to hold at ``now_ns``: every raw payload whose
        ``retention_until_ns`` has been reached, with the observations extracted from it, and, for each dataset given
        a retention period in ``retention_days`` (the provider registry's ``retention_days``), every observation
        received that many days ago or earlier. One RETENTION_ELAPSED event per dataset; nothing due, no event."""
        for ds, days in (retention_days or {}).items():
            if days is not None and (type(days) is not int or days < 0):
                raise ValueError(f"retention_days[{ds!r}] must be a non-negative integer or None")

        def plan() -> dict[str, tuple[set[int], set[str]]]:
            out: dict[str, tuple[set[int], set[str]]] = {}
            for r in self.conn.execute("SELECT sha256, dataset FROM raw_object WHERE retention_until_ns IS NOT NULL "
                                       "AND retention_until_ns<=? AND deleted_at_ns IS NULL", (now_ns,)):
                out.setdefault(r["dataset"], (set(), set()))[1].add(r["sha256"])
            due = sorted(s for _, raws in out.values() for s in raws)
            if due:
                for r in self.conn.execute(f"SELECT o.obs_id, o.dataset FROM observation o JOIN raw_object r ON {_RAW_JOIN} "
                                           "WHERE r.sha256 IN (SELECT value FROM json_each(?))", (json.dumps(due),)):
                    out.setdefault(r["dataset"], (set(), set()))[0].add(r["obs_id"])
            for ds, days in sorted((retention_days or {}).items()):
                if days is None:
                    continue
                for r in self.conn.execute("SELECT obs_id FROM observation WHERE dataset=? AND receipt_time_ns<=?",
                                           (ds, now_ns - days * NS_PER_DAY)):
                    out.setdefault(ds, (set(), set()))[0].add(r["obs_id"])
            return out

        return self._delete("RETENTION_ELAPSED", reason, now_ns, plan)

    def withdraw_rights(self, dataset: str, *, reason: str, at_ns: int, entity_ids: Iterable[str] | None = None) -> dict:
        """The rights to ``dataset``, or to some of its entities, were withdrawn: delete those observations (every
        version) and the raw payloads they were extracted from; dataset-wide, every raw payload of the dataset. One
        RIGHTS_WITHDRAWN event, recorded even when nothing was stored."""
        ids = None
        if entity_ids is not None:
            ids = sorted(set(entity_ids))
            if not ids:
                raise ValueError("entity_ids, when given, must name at least one entity")

        def plan() -> dict[str, tuple[set[int], set[str]]]:
            sql, params = "SELECT obs_id FROM observation WHERE dataset=?", [dataset]
            if ids is not None:
                sql += " AND entity_id IN (SELECT value FROM json_each(?))"
                params.append(json.dumps(ids))
            obs = {r["obs_id"] for r in self.conn.execute(sql, params)}
            raws = {r["sha256"] for r in self.conn.execute(
                f"SELECT DISTINCT r.sha256 FROM observation o JOIN raw_object r ON {_RAW_JOIN} "
                "WHERE o.obs_id IN (SELECT value FROM json_each(?)) AND r.deleted_at_ns IS NULL", (json.dumps(sorted(obs)),))}
            if ids is None:
                raws |= {r["sha256"] for r in self.conn.execute("SELECT sha256 FROM raw_object WHERE dataset=? AND "
                                                                "deleted_at_ns IS NULL", (dataset,))}
            return {dataset: (obs, raws)}

        return self._delete("RIGHTS_WITHDRAWN", reason, at_ns, plan, always=(dataset,))[0]

    def _delete(self, cause: str, reason: str, at_ns: int, plan, *, always: tuple[str, ...] = ()) -> list[dict]:
        """Apply a deletion plan {dataset: (observation ids, raw sha256s)} in one transaction (the plan is computed
        inside it), append one event per dataset, then remove the raw files."""
        if cause not in DELETION_CAUSES:
            raise ValueError(f"unknown deletion cause {cause}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a deletion states its reason")
        if type(at_ns) is not int or at_ns < 0:
            raise ValueError("deleted_at_ns must be a non-negative integer")
        event_ids: list[str] = []
        files: list[str] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            todo = plan()
            for ds in sorted(set(todo) | set(always)):
                obs_ids, raws = todo.get(ds, (set(), set()))
                if not obs_ids and not raws and ds not in always:
                    continue
                ids_json = json.dumps(sorted(obs_ids))
                entities = {r["entity_id"]: {"observations": r["n"], "first_available_ns": r["first"],
                                             "last_available_ns": r["last"]}
                            for r in self.conn.execute(
                                "SELECT entity_id, COUNT(*) AS n, MIN(available_time_ns) AS first, MAX(available_time_ns) "
                                "AS last FROM observation WHERE obs_id IN (SELECT value FROM json_each(?)) "
                                "GROUP BY entity_id ORDER BY entity_id", (ids_json,))}
                # A surviving later version keeps its row; it no longer links to a version that is gone.
                self.conn.execute("UPDATE observation SET supersedes=NULL WHERE supersedes IN (SELECT value FROM json_each(?)) "
                                  "AND obs_id NOT IN (SELECT value FROM json_each(?))", (ids_json, ids_json))
                self.conn.execute("DELETE FROM observation WHERE obs_id IN (SELECT value FROM json_each(?))", (ids_json,))
                raw_list = sorted(raws)
                self.conn.execute("UPDATE raw_object SET deleted_at_ns=? WHERE sha256 IN (SELECT value FROM json_each(?)) "
                                  "AND deleted_at_ns IS NULL", (at_ns, json.dumps(raw_list)))
                body = {"dataset": ds, "cause": cause, "reason": reason, "deleted_at_ns": at_ns,
                        "observations": len(obs_ids), "entities": entities, "raw_objects": raw_list}
                event_id = "pitdel:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))
                                                      .encode()).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO deletion_event(event_id, dataset, cause, reason, deleted_at_ns, "
                                  "observations, entities, raw_objects) VALUES (?,?,?,?,?,?,?,?)",
                                  (event_id, ds, cause, reason, at_ns, len(obs_ids), json.dumps(entities, sort_keys=True),
                                   json.dumps(raw_list)))
                event_ids.append(event_id)
                files += raw_list
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if self.raw_dir is not None:
            for sha in files:
                (self.raw_dir / sha).unlink(missing_ok=True)
        return [self._event(self.conn.execute("SELECT * FROM deletion_event WHERE event_id=?", (e,)).fetchone())
                for e in event_ids]

    def close(self) -> None:
        self.conn.close()
