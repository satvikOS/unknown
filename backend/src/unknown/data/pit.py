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
core never reads it (ADR-0002/0015)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..domain.fixed import Fixed

MISSING_REASONS = frozenset({"NOT_REPORTED", "NOT_YET_PUBLISHED", "PROVIDER_DELAYED", "UNAUTHORIZED", "NOT_APPLICABLE", "ERROR"})
OPEN_END = 2 ** 62  # sentinel for "still current" in known_to

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS raw_object (
  sha256 TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, request TEXT NOT NULL,
  receipt_time_ns INTEGER NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
  retention_until_ns INTEGER, deleted_at_ns INTEGER
);
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

    # ------------------------------------------------------------ raw layer
    def put_raw(self, data: bytes, *, provider: str, dataset: str, request: dict, receipt_time_ns: int,
                media_type: str, retention_until_ns: int | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
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

        A *publication* is keyed by (dataset, entity, field, period, available_time). A later
        publication for the same period (e.g. a restatement filed later) is simply another row with
        its own available_time; nothing is closed. If the *same* publication arrives with different
        content (a silent provider correction), the previous version's knowledge interval is closed
        and the new version links to it, flagged ``silent_correction``."""
        key = (obs.dataset, obs.entity_id, obs.field, obs.period_start, obs.period_end, obs.available_time_ns)
        cur = self.conn.execute(
            "SELECT obs_id, value, value_text, missing_reason, unit, currency FROM observation WHERE dataset=? AND entity_id=? AND field=? "
            "AND COALESCE(period_start,'')=COALESCE(?, '') AND COALESCE(period_end,'')=COALESCE(?, '') AND available_time_ns=? AND known_to_ns=?",
            key + (OPEN_END,)).fetchone()
        new_value = obs.value.to_str() if obs.value is not None else None
        quality = list(obs.quality)
        if cur is not None:
            if (cur["value"], cur["value_text"], cur["missing_reason"], cur["unit"], cur["currency"]) == \
                    (new_value, obs.value_text, obs.missing_reason, obs.unit, obs.currency):
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
                "supersedes, source_ref, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs.dataset, obs.entity_id, obs.field, obs.period_start, obs.period_end, obs.period_kind, new_value, obs.value_text,
                 obs.unit, obs.currency, obs.missing_reason, obs.event_time_ns, obs.published_time_ns, obs.receipt_time_ns,
                 obs.available_time_ns, known_at_ns, OPEN_END, supersedes, obs.source_ref, json.dumps(quality)))
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
            k = (r["dataset"], r["period_kind"], r["period_start"], r["period_end"])
            if k not in best or (r["available_time_ns"], r["known_from_ns"]) > (best[k]["available_time_ns"], best[k]["known_from_ns"]):
                best[k] = r
        return sorted(best.values(), key=lambda r: (r["period_end"] or "", r["period_start"] or ""))

    def latest_as_of(self, entity_id: str, field: str, t_ns: int, *, dataset: str | None = None, period_kind: str | None = None,
                     knowledge_cutoff_ns: int | None = None):
        rows = [r for r in self.as_of(entity_id, field, t_ns, dataset=dataset, knowledge_cutoff_ns=knowledge_cutoff_ns)
                if period_kind is None or r["period_kind"] == period_kind]
        return rows[-1] if rows else None

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

    def close(self) -> None:
        self.conn.close()
