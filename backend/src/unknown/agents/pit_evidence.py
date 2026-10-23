"""Evidence spans from the point-in-time research store (ADR-0015; ADR-0016 section 7; CAP-MODEL-12).

A research job names issuers and (optionally) XBRL concepts. Its evidence is pinned to one
instant ``t``: for each issuer and concept the store is read with ``as_of(t,
knowledge_cutoff_ns=t)``, which returns exactly what this system had recorded as published by
``t``. The same selection therefore yields the same spans later, which lets the model worker
re-derive the evidence itself instead of trusting text stored by another process: the span ids,
dataset ids and texts it sends must hash to the input hashes fixed in the job envelope at
submission (the runner enforces the hash check), and the dataset id that the data-use gate
checks comes from the store row, not from the job record.

One span is one reported value, written so that a claim can quote it and copy its concept,
period and unit verbatim::

    Apple Inc. (SEC-CIK-0000320193) concept=us-gaap:Revenues period=2024-09-29..2025-09-27 (annual)
    unit=USD value=416161000000. Source: SEC EDGAR XBRL companyfacts, 10-K accession ..., available ...

Each span has a matching ``TypedFact`` (same concept, period and unit, exact decimal), so numeric
claims are checked exactly. Per concept the newest 3 annual durations (350-380 days), 4 quarterly
durations (80-100 days) and 4 instants are kept; year-to-date and other durations are skipped.
Where two publications report the same period, the later-available one wins (a restatement).

Each span also carries its row's typed provenance for research_proposal v2: ``ref_id`` ``obs:<obs_id>``,
``available_ns`` (the row's available time, never after the pinned instant) and ``observed_ns`` (the event
time, else the period-end date at 00:00 UTC). These come from the store row, not from the span text.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..data.pit import ResearchStore
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from .evidence import EvidenceSpan, TypedFact

DEFAULT_FIELDS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:GrossProfit",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:NetIncomeLoss",
    "us-gaap:EarningsPerShareDiluted",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:Assets",
    "us-gaap:Liabilities",
    "us-gaap:StockholdersEquity",
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "dei:EntityCommonStockSharesOutstanding",
)
DATASET_LABELS = {"sec.companyfacts": "SEC EDGAR XBRL companyfacts"}
ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
KEEP = {"annual": 3, "quarterly": 4, "instant": 4}
MAX_IDENTIFIERS = 5
MAX_FIELDS = 40
MAX_SPANS = 300
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}:[A-Za-z][A-Za-z0-9_]{0,159}$")


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceSelection:
    """What a job's evidence is: issuers (resolved PIT entity ids), concepts, and the pinned instant."""
    entity_ids: tuple[str, ...]
    fields: tuple[str, ...]
    as_of_ns: int

    def to_dict(self) -> dict:
        return {"entity_ids": list(self.entity_ids), "fields": list(self.fields), "as_of_ns": self.as_of_ns}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EvidenceSelection":
        if not isinstance(d, Mapping) or set(d) != {"entity_ids", "fields", "as_of_ns"}:
            raise EvidenceError("evidence selection must be {entity_ids, fields, as_of_ns}")
        if type(d["as_of_ns"]) is not int or d["as_of_ns"] < 0:
            raise EvidenceError("as_of_ns must be a non-negative integer")
        return cls(tuple(_idents(d["entity_ids"], "entity_ids")), tuple(_fields(d["fields"])), d["as_of_ns"])


@dataclass(frozen=True)
class Evidence:
    spans: tuple[EvidenceSpan, ...]
    facts: tuple[TypedFact, ...]

    def manifest(self) -> list[dict]:
        """What the job record stores (for display) and what the worker must reproduce exactly."""
        return [{"span_id": s.span_id, "document_id": s.document_id, "dataset_id": s.dataset_id,
                 "content_hash": s.content_hash, "text": s.text} for s in self.spans]


EMPTY = Evidence((), ())


def span_hash(span_id: str, dataset_id: str, text: str) -> str:
    """Binds the span id and the dataset id (which the data-use gate decides on) to the text."""
    return sha256_hex(canonical_json({"span_id": span_id, "dataset_id": dataset_id, "text": text}))


def _idents(values: Any, what: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_IDENTIFIERS:
        raise EvidenceError(f"{what} must be a list of at most {MAX_IDENTIFIERS} identifiers")
    out = []
    for v in values:
        if not isinstance(v, str) or not IDENT_RE.match(v):
            raise EvidenceError(f"invalid identifier {v!r} in {what}")
        if v not in out:
            out.append(v)
    return out


def _fields(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_FIELDS:
        raise EvidenceError(f"fields must be a non-empty list of at most {MAX_FIELDS} concepts (taxonomy:Concept)")
    out = []
    for v in values:
        if not isinstance(v, str) or not FIELD_RE.match(v):
            raise EvidenceError(f"invalid concept {v!r}; use taxonomy:Concept, e.g. us-gaap:Revenues")
        if v not in out:
            out.append(v)
    return out


def resolve_identifiers(store: ResearchStore, identifiers: Sequence[str], at_ns: int) -> tuple[str, ...]:
    """Each identifier is a PIT entity id (e.g. SEC-CIK-0000320193) or a ticker known to the store at ``at_ns``
    (any ``ticker:<exchange>`` alias, case-insensitive). Unknown or ambiguous identifiers raise."""
    out: list[str] = []
    for ident in _idents(identifiers, "issuer_ids"):
        row = store.conn.execute("SELECT entity_id FROM entity WHERE entity_id = ?", (ident,)).fetchone()
        if row is not None:
            eid = row["entity_id"]
        else:
            rows = store.conn.execute(
                "SELECT DISTINCT entity_id FROM entity_alias WHERE scheme LIKE 'ticker:%' AND value = ? AND valid_from_ns <= ?"
                " AND valid_to_ns > ?", (ident.upper(), at_ns, at_ns)).fetchall()
            ids = sorted({r["entity_id"] for r in rows})
            if not ids:
                raise EvidenceError(f"{ident!r} is neither an entity id nor a ticker in the research store at that time")
            if len(ids) > 1:
                raise EvidenceError(f"ticker {ident!r} is ambiguous at that time: {ids}")
            eid = ids[0]
        if eid not in out:
            out.append(eid)
    return tuple(out)


def selection(store: ResearchStore, identifiers: Sequence[str], fields: Sequence[str] | None, as_of_ns: int) -> EvidenceSelection:
    if type(as_of_ns) is not int or as_of_ns < 0:
        raise EvidenceError("as_of_ns must be a non-negative integer")
    return EvidenceSelection(resolve_identifiers(store, identifiers, as_of_ns),
                             tuple(_fields(DEFAULT_FIELDS if fields is None else fields)), as_of_ns)


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observed_ns(row: Mapping[str, Any]) -> int | None:
    """When the observed fact happened: the row's event time when the source gives one, otherwise the period-end
    date at 00:00 UTC (for an XBRL fact, the end of the reported period). None when neither is known."""
    if row["event_time_ns"] is not None:
        return int(row["event_time_ns"])
    try:
        end = date.fromisoformat(row["period_end"])
    except (TypeError, ValueError):
        return None
    return int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) * 1_000_000_000


def _form(quality: str) -> str:
    try:
        tags = json.loads(quality or "[]")
    except ValueError:
        return "filing"
    forms = [t[5:] for t in tags if isinstance(t, str) and t.startswith("form:")]
    return forms[0] if forms else "filing"


def derive(store: ResearchStore, sel: EvidenceSelection) -> Evidence:
    """The spans and typed facts of ``sel``. Deterministic: same store history, same selection, same result."""
    if not sel.entity_ids:
        return EMPTY
    spans: list[EvidenceSpan] = []
    facts: list[TypedFact] = []
    for eid in sel.entity_ids:
        ent = store.conn.execute("SELECT name FROM entity WHERE entity_id = ?", (eid,)).fetchone()
        if ent is None:
            raise EvidenceError(f"entity {eid} is not in the research store")
        name = ent["name"] or eid
        for field in sel.fields:
            best: dict[tuple, Any] = {}
            for r in store.as_of(eid, field, sel.as_of_ns, knowledge_cutoff_ns=sel.as_of_ns):
                if r["value"] is None:
                    continue  # missing or textual: never presented as a number
                key = (r["period_start"], r["period_end"], r["unit"])
                rank = (r["available_time_ns"], r["publication_id"], r["known_from_ns"])
                if key not in best or rank > (best[key]["available_time_ns"], best[key]["publication_id"], best[key]["known_from_ns"]):
                    best[key] = r
            buckets: dict[str, list] = {"annual": [], "quarterly": [], "instant": []}
            for (start, end, _unit), r in best.items():
                if not end:
                    continue
                if start is None:
                    buckets["instant"].append(r)
                    continue
                try:
                    days = _days(start, end)
                except ValueError:
                    continue
                if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
                    buckets["annual"].append(r)
                elif QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
                    buckets["quarterly"].append(r)
            for klass in ("annual", "quarterly", "instant"):
                rows = sorted(buckets[klass], key=lambda r: (r["period_end"], r["period_start"] or "", r["unit"]))[-KEEP[klass]:]
                for r in rows:
                    period = r["period_end"] if r["period_start"] is None else f"{r['period_start']}..{r['period_end']}"
                    dataset = r["dataset"]
                    label = DATASET_LABELS.get(dataset, dataset)
                    text = (f"{name} ({eid}) concept={field} period={period} ({klass}) unit={r['unit']} value={r['value']}. "
                            f"Source: {label}, {_form(r['quality'])} accession {r['publication_id']}, "
                            f"available {_iso(r['available_time_ns'])}.")
                    span_id = f"S{len(spans) + 1}"
                    spans.append(EvidenceSpan(span_id, f"{eid}/{r['publication_id']}", dataset, text,
                                              content_hash=span_hash(span_id, dataset, text), source=r["source_ref"],
                                              ref_type="observation", ref_id=f"obs:{r['obs_id']}",
                                              observed_ns=observed_ns(r), available_ns=r["available_time_ns"]))
                    facts.append(TypedFact(field, period, r["unit"], Fixed.parse(r["value"])))
                    if len(spans) > MAX_SPANS:
                        raise EvidenceError(f"more than {MAX_SPANS} evidence spans; name fewer issuers or concepts")
    return Evidence(tuple(spans), tuple(facts))


INSTRUMENT_LINE_PREFIX = "Instruments (internal instrument ids): "


def instrument_line(instrument_ids: Sequence[str]) -> str:
    """The task line naming the instruments a job is about. It puts the ids into the task hash (and so into the
    dedupe key), and the worker refuses a job whose stored ids are not the ones in its task
    (``task_names_instruments``)."""
    return INSTRUMENT_LINE_PREFIX + ", ".join(instrument_ids) + "."


def question_has_instrument_line(question: str) -> bool:
    """Whether a line of ``question`` starts like the task's instrument line. Such a question is refused at
    submission and by the worker: otherwise a task could be split into question and instrument line two ways."""
    return any(line.lstrip().startswith(INSTRUMENT_LINE_PREFIX.strip()) for line in question.splitlines())


def task_names_instruments(task: str, question: str, instrument_ids: Sequence[str]) -> bool:
    """Whether ``task`` is the stored question followed by exactly the instrument line ``task_text`` writes for
    ``instrument_ids`` (and by none when there are none). Anchored at the line after the question, so an id list
    that was emptied, shortened, reordered or replaced is caught. A question with a line of its own that starts
    like the instrument line is refused (``question_has_instrument_line``), so the question's text cannot stand in
    for the instrument line, even when the stored question and ids are rewritten together."""
    if question_has_instrument_line(question):
        return False
    head = question.strip() + "\n"
    if not task.startswith(head):
        return False
    rest = task[len(head):]
    if instrument_ids:
        return rest.startswith(instrument_line(instrument_ids) + "\n")
    return not rest.startswith(INSTRUMENT_LINE_PREFIX)


def task_text(question: str, sel: EvidenceSelection | None, evidence: Evidence, instrument_ids: Sequence[str] = ()) -> str:
    """The TASK block sent to the model: the owner's question plus how to use the evidence."""
    lines = [question.strip()]
    if instrument_ids:
        lines.append(instrument_line(instrument_ids))
    if sel is not None and evidence.spans:
        lines.append("")
        lines.append(f"The EVIDENCE spans are point-in-time SEC XBRL facts as recorded at {_iso(sel.as_of_ns)}. "
                     "When a claim relies on a span, cite its span id, quote the span text verbatim (for example "
                     "\"concept=... period=... unit=... value=...\"), and copy concept, period and unit exactly as "
                     "written in the span. Do not use figures that are not in the evidence.")
    else:
        lines.append("")
        lines.append("No evidence spans are supplied for this question. Claims without cited evidence are recorded "
                     "as unsupported; abstain if the question needs data.")
    return "\n".join(lines)


def manifest_matches(stored: Iterable[Mapping[str, Any]], evidence: Evidence) -> str | None:
    """None when the stored manifest equals the re-derived evidence; otherwise a short description."""
    stored = list(stored)
    derived = evidence.manifest()
    if len(stored) != len(derived):
        return f"{len(stored)} spans recorded at submission, {len(derived)} derivable now"
    for a, b in zip(stored, derived):
        if a != b:
            return f"span {b['span_id']} differs from the recorded span {a.get('span_id')}"
    return None
