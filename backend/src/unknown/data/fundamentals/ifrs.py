"""ESEF / IFRS mapping and statements for EU issuers (CAP-CO-03, CAP-CO-05; book ch. 11).

The SEC path (``mapping``, ``statements``) maps us-gaap concepts to standardized line items. This is
the same idea for ESEF filings from filings.xbrl.org (``sources.esef``): a versioned table
``esef-ifrs-map@<version>`` from ``ifrs-full`` concepts to the *same* line-item keys, so the two
paths line up item by item (``compare``). Only annual reports are filed under ESEF, so the basis is
FY only (no derived quarters, no TTM).

Precedence. ``concepts`` are semantically equivalent alternates in order (``Revenue`` before
``RevenueFromContractsWithCustomers``). ``fallback`` concepts are *not* equivalent and are used only
when no alternate is reported for the period, flagged on the cell: net income attributable to the
parent falls back to ``ProfitLoss`` (equal only when there is no non-controlling interest) and
equity attributable to the parent falls back to total ``Equity``. Each cell names its rule, concept,
filing (fxo_id), observation ids and availability time. Issuer extension concepts (for example a
company-specific sales line) are not mapped: ESEF anchoring is not in xBRL-JSON.

Point in time. Facts come from ``ResearchStore.as_of(..., dataset=xbrl_org.esef_facts)``: for each
period, the latest filing available at the as-of time. A figure that came from a later report's
comparative column is flagged ``from_later_filing``; a figure that a later report changed is
``restated`` with the earlier value in ``history``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed
from ..pit import OPEN_END
from ..sources.esef import ATTRIBUTION, BASE, DATASET, entity_id
from .mapping import ITEMS as SEC_ITEMS

IFRS_MAPPING_ID = "esef-ifrs-map"
IFRS_MAPPING_VERSION = 1
IFRS_MAPPING_REF = f"{IFRS_MAPPING_ID}@{IFRS_MAPPING_VERSION}"


@dataclass(frozen=True)
class IfrsItem:
    key: str                         # same key as the SEC line item (mapping.ITEMS)
    concepts: tuple[str, ...]
    fallback: tuple[str, ...] = ()
    fallback_flag: str = ""

    @property
    def sec(self):
        return SEC_ITEMS[self.key]

    def rule(self) -> str:
        return f"{IFRS_MAPPING_REF}:{self.key}"


def _f(*names: str) -> tuple[str, ...]:
    return tuple(f"ifrs-full:{n}" for n in names)


IFRS_ITEMS: tuple[IfrsItem, ...] = (
    IfrsItem("revenue", _f("Revenue", "RevenueFromContractsWithCustomers")),
    IfrsItem("operating_income", _f("ProfitLossFromOperatingActivities")),
    IfrsItem("net_income", _f("ProfitLossAttributableToOwnersOfParent"), _f("ProfitLoss"),
             "parent_share_not_tagged:ProfitLoss_used (equal only without non-controlling interest)"),
    IfrsItem("net_income_including_nci", _f("ProfitLoss")),
    IfrsItem("eps_basic", _f("BasicEarningsLossPerShare")),
    IfrsItem("weighted_shares_basic", _f("WeightedAverageShares")),
    IfrsItem("shares_outstanding_balance", _f("NumberOfSharesOutstanding")),
    IfrsItem("total_assets", _f("Assets")),
    IfrsItem("equity_parent", _f("EquityAttributableToOwnersOfParent"), _f("Equity"),
             "parent_share_not_tagged:Equity_used (equal only without non-controlling interest)"),
    IfrsItem("equity_total", _f("Equity")),
    IfrsItem("cfo", _f("CashFlowsFromUsedInOperatingActivities")),
)
IFRS_BY_KEY = {i.key: i for i in IFRS_ITEMS}
ANCHORS = _f("Revenue", "RevenueFromContractsWithCustomers", "ProfitLoss", "CashFlowsFromUsedInOperatingActivities")


def validate_ifrs_mapping() -> list[str]:
    errs = []
    for i in IFRS_ITEMS:
        if i.key not in SEC_ITEMS:
            errs.append(f"{i.key}: no SEC line item with this key")
        if not i.concepts or any(not c.startswith("ifrs-full:") for c in i.concepts + i.fallback):
            errs.append(f"{i.key}: concepts must be ifrs-full QNames")
        if i.fallback and not i.fallback_flag:
            errs.append(f"{i.key}: a fallback needs a flag")
    return errs


def _iso_unit(unit: str) -> bool:
    return len(unit) == 3 and unit.isalpha() and unit.isupper()


class EsefStatements:
    """Standardized FY statements for one EU issuer as of ``as_of_ns``."""

    def __init__(self, store, lei: str, as_of_ns: int, *, knowledge_cutoff_ns: int | None = None):
        self.store, self.lei, self.as_of_ns = store, lei, as_of_ns
        self.entity_id = entity_id(lei)
        concepts = sorted({c for i in IFRS_ITEMS for c in i.concepts + i.fallback} | set(ANCHORS))
        self.facts = {c: store.as_of(self.entity_id, c, as_of_ns, dataset=DATASET, knowledge_cutoff_ns=knowledge_cutoff_ns)
                      for c in concepts}
        money = [r for c in ANCHORS + _f("Assets") for r in self.facts.get(c, []) if _iso_unit(r["unit"])]
        latest = max(money, key=lambda r: (r["period_end"], r["available_time_ns"])) if money else None
        self.currency = latest["unit"] if latest else None
        self.currency_units = sorted({r["unit"] for r in money})
        spans = {(r["period_start"], r["period_end"]) for c in ANCHORS for r in self.facts.get(c, [])
                 if r["period_kind"] == "FY" and r["period_start"]}
        self.fiscal_years = sorted(spans, key=lambda s: s[1])
        row = store.conn.execute("SELECT name, attributes FROM entity WHERE entity_id=?", (self.entity_id,)).fetchone()
        self.name = row["name"] if row else None
        self.attributes = json.loads(row["attributes"]) if row else {}

    def unit_for(self, item: IfrsItem) -> str | None:
        kind = item.sec.unit_kind
        if kind == "shares":
            return "shares"
        if self.currency is None:
            return None
        return self.currency if kind == "money" else f"{self.currency}/shares"

    def _filing(self, fxo_id: str) -> dict:
        for f in self.attributes.get("filings", []):
            if f.get("fxo_id") == fxo_id:
                return f
        return {}

    def _source(self, r) -> dict:
        f = self._filing(r["publication_id"])
        q = json.loads(r["quality"] or "[]")
        return {"obs_id": r["obs_id"], "concept": r["field"], "value": r["value"], "unit": r["unit"], "period_start": r["period_start"],
                "period_end": r["period_end"], "filing": r["publication_id"], "filing_period_end": f.get("period_end"),
                "filing_country": f.get("country"), "available_time": ns_to_iso(r["available_time_ns"]), "quality": q,
                "viewer_url": BASE + f["viewer_url"] if f.get("viewer_url") else None, "source_ref": r["source_ref"]}

    def _history(self, r) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT obs_id, value, publication_id, available_time_ns FROM observation WHERE dataset=? AND entity_id=? AND field=? "
            "AND COALESCE(period_start,'')=COALESCE(?,'') AND period_end=? AND unit=? AND known_to_ns=? AND available_time_ns<=? "
            "AND obs_id<>? ORDER BY available_time_ns", (DATASET, self.entity_id, r["field"], r["period_start"], r["period_end"], r["unit"],
                                                         OPEN_END, self.as_of_ns, r["obs_id"])).fetchall()
        return [{"obs_id": h["obs_id"], "value": h["value"], "filing": h["publication_id"], "available_time": ns_to_iso(h["available_time_ns"])}
                for h in rows]

    def cell(self, item: IfrsItem, fy: tuple[str, str]) -> dict:
        start, end = fy
        unit = self.unit_for(item)
        base = {"item": item.key, "period": {"kind": "FY", "start": start, "end": end, "label": f"FY{end[:4]}"}, "unit": unit,
                "rule": item.rule()}
        if unit is None:
            return {**base, "value": None, "method": "MISSING", "missing_reason": "NOT_REPORTED",
                    "detail": "no reporting currency: no revenue, profit, cash-flow or assets fact held", "flags": [], "obs_ids": [], "sources": []}
        instant = item.sec.period_type == "instant"
        for group, method in ((item.concepts, "REPORTED"), (item.fallback, "FALLBACK_CONCEPT")):
            for c in group:
                for r in self.facts.get(c, []):
                    if r["unit"] != unit or r["value"] is None or r["period_end"] != end:
                        continue
                    if instant != (r["period_start"] is None) or (not instant and r["period_start"] != start):
                        continue
                    flags = []
                    if method == "FALLBACK_CONCEPT":
                        flags.append(item.fallback_flag)
                    src = self._source(r)
                    if src["filing_period_end"] and src["filing_period_end"] != end:
                        flags.append(f"from_later_filing:{src['filing_period_end']}")
                    hist = self._history(r)
                    out = {**base, "value": r["value"], "method": method, "concept": c, "missing_reason": None, "detail": None,
                           "flags": flags, "obs_ids": [r["obs_id"]], "latest_input_available_time": src["available_time"], "sources": [src]}
                    if hist:
                        out["history"] = hist
                        out["restated"] = any(h["value"] != r["value"] for h in hist)
                    return out
        return {**base, "value": None, "method": "MISSING", "concept": None, "missing_reason": "NOT_REPORTED",
                "detail": f"none of {', '.join(item.concepts + item.fallback)} tagged for {start}..{end} in {unit} "
                          "(non-dimensional facts only; issuer extension concepts are not mapped)", "flags": [], "obs_ids": [], "sources": []}

    def statement(self, periods: int = 4) -> dict:
        if not 1 <= periods <= 20:
            raise ValueError("periods must be between 1 and 20")
        cols = list(reversed(self.fiscal_years[-periods:]))
        sections: dict[str, list] = {}
        for item in IFRS_ITEMS:
            sections.setdefault(item.sec.statement, []).append({
                "item": item.key, "label": item.sec.label, "unit": self.unit_for(item), "rule": item.rule(),
                "concepts": list(item.concepts), "fallback": list(item.fallback), "cells": [self.cell(item, fy) for fy in cols]})
        status = "OK" if cols else ("NO_FILINGS_INDEXED" if self.attributes.get("esef_status") == "NO_FILINGS_INDEXED" else "NOT_AVAILABLE")
        out = {"status": status, "entity_id": self.entity_id, "lei": self.lei, "name": self.name, "as_of": ns_to_iso(self.as_of_ns),
               "mapping": IFRS_MAPPING_REF, "basis": "FY", "currency": self.currency,
               "currency_flags": [f"multiple_currency_units:{','.join(self.currency_units)}"] if len(self.currency_units) > 1 else [],
               "columns": [{"kind": "FY", "start": s, "end": e, "label": f"FY{e[:4]}"} for s, e in cols], "statements": sections,
               "filings": self.attributes.get("filings", []), "countries": self.attributes.get("countries", []),
               "source": {"dataset": DATASET, "provider": "xbrl-filings", "attribution": ATTRIBUTION}}
        if not cols:
            out["reason"] = self.attributes.get("reason") or ("no ESEF annual figures available at the as-of time (not ingested, or "
                                                              "published later)")
        return out


def _cell_brief(c: dict | None, source: str) -> dict | None:
    if c is None:
        return None
    src = (c.get("sources") or [{}])[0]
    return {"value": c.get("value"), "unit": c.get("unit"), "method": c.get("method"), "concept": c.get("concept") or src.get("concept"),
            "missing_reason": c.get("missing_reason"), "obs_ids": c.get("obs_ids", []), "flags": c.get("flags", []), "source": source,
            "document": src.get("filing") or src.get("accession"), "available_time": c.get("latest_input_available_time")}


def _ends_match(a: str, b: str) -> bool:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= 7


def compare(esef: dict, sec: dict | None) -> dict:
    """Side-by-side FY lines for the keys both paths map. A difference is computed only between two
    values in the same unit; different currencies are labelled, never converted."""
    sec_cells: dict[str, dict[str, dict]] = {}
    if sec:
        for rows in sec.get("statements", {}).values():
            for line in rows:
                sec_cells[line["item"]] = {c["period"]["end"]: c for c in line["cells"]}
    ends = [c["end"] for c in esef.get("columns", [])]
    for col in (sec or {}).get("columns", []):
        if not any(_ends_match(col["end"], e) for e in ends):
            ends.append(col["end"])
    ends.sort(reverse=True)
    esef_cells = {line["item"]: {c["period"]["end"]: c for c in line["cells"]} for rows in esef.get("statements", {}).values() for line in rows}
    lines = []
    for item in IFRS_ITEMS:
        cells = []
        for e in ends:
            ec = next((c for k, c in esef_cells.get(item.key, {}).items() if _ends_match(k, e)), None)
            sc = next((c for k, c in sec_cells.get(item.key, {}).items() if _ends_match(k, e)), None)
            a, b = _cell_brief(ec, "ESEF"), _cell_brief(sc, "SEC")
            diff, note = None, None
            if a and b and a["value"] is not None and b["value"] is not None:
                if a["unit"] == b["unit"]:
                    diff = (Fixed.parse(a["value"]) - Fixed.parse(b["value"])).to_str()
                else:
                    note = f"different units ({a['unit']} vs {b['unit']}): not converted"
            cells.append({"fy_end": e, "esef": a, "sec": b, "difference_esef_minus_sec": diff, "note": note})
        lines.append({"item": item.key, "label": item.sec.label, "statement": item.sec.statement, "cells": cells})
    return {"esef": {"lei": esef.get("lei"), "name": esef.get("name"), "currency": esef.get("currency"), "mapping": esef.get("mapping"),
                     "status": esef.get("status"), "source": esef.get("source")},
            "sec": None if sec is None else {"entity_id": sec.get("entity_id"), "currency": sec.get("currency"), "mapping": sec.get("mapping"),
                                             "source": sec.get("source")},
            "fy_ends": ends, "lines": lines,
            "note": "Columns are matched by fiscal-year end (within 7 days). ESEF figures are IFRS as tagged in the annual report; "
                    "SEC figures are as tagged in the SEC filing (e.g. a 20-F). The pairing of the two issuers is the owner's explicit "
                    "choice (sec=<CIK>), never inferred from names."}
