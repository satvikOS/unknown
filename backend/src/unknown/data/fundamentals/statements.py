"""Standardized financial statements as of a decision time (CAP-CO-04, CAP-CO-05, CAP-CO-06; book ch. 11).

Point in time. Every fact comes from ``ResearchStore.as_of(entity, concept, t)``, which returns only
publications with ``available_time <= t`` (SEC acceptance time) and, for each period, the latest
such publication. A restatement filed after ``t`` therefore never leaks into a statement as of
``t``; one filed before ``t`` replaces the original, and the cell's ``history`` lists the earlier
publications. ``knowledge_cutoff_ns`` passes through for audit replays (what this system knew).

The store keys publications by the filing's ``fp`` as ``period_kind``; the same economic period can
appear under two kinds (a quarter repeated in a later 10-K), so facts are de-duplicated here by
(start, end, unit) with the same rule the store uses: latest available time, then publication id.

Cells. A cell is one line item for one period: a reported fact (``REPORTED``), a quarter derived
from year-to-date values (``DERIVED_QUARTER``: Q2 = 6M - Q1, Q3 = 9M - 6M, Q4 = FY - 9M, or
YTD - reported prior quarters), a computed item (``COMPUTED``), a TTM aggregate (``TTM_SUM`` of four
contiguous discrete quarters; ``TTM_ANNUAL`` when the window is exactly a fiscal year;
``TTM_ALT`` = FY_last + YTD_current - YTD_prior, flagged) or the latest balance
(``LATEST_INSTANT``). A missing cell has ``value=None`` and a reason, never zero.

Missing reasons (``missing_reason``) extend the store vocabulary: NOT_REPORTED, NOT_APPLICABLE,
NOT_DERIVABLE, MISSING_INPUT, NON_CONTIGUOUS, FISCAL_TRANSITION. ``pit_reason()`` maps them to the
store vocabulary when persisting.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed
from ..pit import OPEN_END
from .mapping import ITEMS, LINE_ITEMS, MAPPING_REF, STATEMENTS, LineItem, Term, all_concepts
from .periods import ONE_DAY, FiscalCalendar, Period, duration_class, parse_date

SEC_FACTS = "sec.companyfacts"
# A fiscal year between two quarter ends: 52 weeks (364 days), a calendar year (365/366) or 53 weeks (371), +/- 3 days.
YEAR_AGO_DAYS = (361, 374)
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
PIT_REASONS = {"NOT_REPORTED": "NOT_REPORTED", "NOT_APPLICABLE": "NOT_APPLICABLE", "NOT_YET_PUBLISHED": "NOT_YET_PUBLISHED",
               "NOT_DERIVABLE": "NOT_REPORTED", "MISSING_INPUT": "NOT_REPORTED", "NON_CONTIGUOUS": "NOT_REPORTED",
               "FISCAL_TRANSITION": "NOT_APPLICABLE", "UNMAPPED": "NOT_REPORTED", "NM": "NOT_APPLICABLE",
               "SUPPRESSED": "NOT_APPLICABLE", "INCOMPLETE": "NOT_REPORTED", "CURRENCY_MISMATCH": "NOT_APPLICABLE",
               "NO_PRICE": "NOT_REPORTED"}


def pit_reason(reason: str | None) -> str | None:
    return None if reason is None else PIT_REASONS.get(reason, "NOT_REPORTED")


def cik_of(entity_id: str) -> int | None:
    m = re.fullmatch(r"SEC-CIK-(\d{10})", entity_id or "")
    return int(m.group(1)) if m else None


def sec_index_url(cik: int | None, accession: str) -> str | None:
    if cik is None or not ACCESSION.match(accession or ""):
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"


@dataclass(frozen=True)
class Fact:
    """One reported XBRL value as a point-in-time observation."""
    concept: str
    unit: str
    start: date | None
    end: date
    value: Fixed
    obs_id: int
    available_ns: int
    accession: str
    form: str | None
    source_ref: str
    quality: tuple[str, ...]

    def to_json(self, cik: int | None = None) -> dict:
        return {"obs_id": self.obs_id, "concept": self.concept, "value": self.value.to_str(), "unit": self.unit,
                "period_start": self.start.isoformat() if self.start else None, "period_end": self.end.isoformat(),
                "accession": self.accession, "form": self.form, "available_time": ns_to_iso(self.available_ns),
                "quality": list(self.quality), "sec_filing_index": sec_index_url(cik, self.accession)}


def _fact(row) -> Fact | None:
    if row["value"] is None:
        return None
    quality = tuple(json.loads(row["quality"] or "[]"))
    form = next((q.split(":", 1)[1] for q in quality if q.startswith("form:")), None)
    return Fact(concept=row["field"], unit=row["unit"], start=parse_date(row["period_start"]), end=parse_date(row["period_end"]),
                value=Fixed.parse(row["value"]), obs_id=row["obs_id"], available_ns=row["available_time_ns"],
                accession=row["publication_id"], form=form, source_ref=row["source_ref"], quality=quality)


def load_concept(store, entity_id: str, concept: str, t_ns: int, knowledge_cutoff_ns: int | None = None) -> list[Fact]:
    """Point-in-time facts for one concept, one per (start, end, unit)."""
    best: dict[tuple, object] = {}
    for r in store.as_of(entity_id, concept, t_ns, dataset=SEC_FACTS, knowledge_cutoff_ns=knowledge_cutoff_ns):
        if r["value"] is None:
            continue
        key = (r["period_start"], r["period_end"], r["unit"])
        rank = (r["available_time_ns"], r["publication_id"], r["known_from_ns"])
        if key not in best or rank > (best[key]["available_time_ns"], best[key]["publication_id"], best[key]["known_from_ns"]):
            best[key] = r
    return [f for f in (_fact(r) for r in best.values()) if f is not None]


@dataclass(frozen=True)
class Cell:
    item: str
    period: Period
    unit: str
    value: Fixed | None = None
    method: str = "REPORTED"
    concept: str | None = None
    facts: tuple[Fact, ...] = ()
    components: tuple["Cell", ...] = ()
    formula: str | None = None
    missing_reason: str | None = None
    detail: str | None = None
    not_reported: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    history: tuple[dict, ...] = field(default=(), compare=False)

    @property
    def ok(self) -> bool:
        return self.value is not None

    def all_facts(self) -> list[Fact]:
        seen: dict[int, Fact] = {}
        stack = [self]
        while stack:
            c = stack.pop()
            for f in c.facts:
                seen[f.obs_id] = f
            stack.extend(c.components)
        return sorted(seen.values(), key=lambda f: f.obs_id)

    def obs_ids(self) -> list[int]:
        return [f.obs_id for f in self.all_facts()]

    def available_ns(self) -> int | None:
        facts = self.all_facts()
        return max(f.available_ns for f in facts) if facts else None

    def all_flags(self) -> list[str]:
        out = list(self.flags)
        for c in self.components:
            out += [x for x in c.all_flags() if x not in out]
        return out

    def to_json(self, cik: int | None = None, *, depth: int = 1) -> dict:
        avail = self.available_ns()
        d = {"item": self.item, "period": self.period.to_json(), "value": self.value.to_str() if self.value is not None else None,
             "unit": self.unit, "method": self.method, "rule": ITEMS[self.item].rule() if self.item in ITEMS else None,
             "concept": self.concept, "formula": self.formula, "missing_reason": self.missing_reason, "detail": self.detail,
             "not_reported": list(self.not_reported), "flags": self.all_flags(), "obs_ids": self.obs_ids(),
             "latest_input_available_time": ns_to_iso(avail) if avail is not None else None,
             "sources": [f.to_json(cik) for f in self.facts]}
        if self.history:
            d["history"] = list(self.history)
            d["restated"] = any(h["value"] != d["value"] for h in self.history)
        if self.components:
            d["components"] = ([c.to_json(cik, depth=depth - 1) for c in self.components] if depth > 0 else
                               [{"item": c.item, "period": c.period.label, "value": c.value.to_str() if c.value is not None else None,
                                 "method": c.method, "obs_ids": c.obs_ids()} for c in self.components])
        return d


class Fundamentals:
    """Standardized fundamentals for one issuer as of decision time ``as_of_ns`` (CAP-CO-05/06)."""

    def __init__(self, store, entity_id: str, as_of_ns: int, *, knowledge_cutoff_ns: int | None = None):
        self.store = store
        self.entity_id = entity_id
        self.cik = cik_of(entity_id)
        self.as_of_ns = as_of_ns
        self.knowledge_cutoff_ns = knowledge_cutoff_ns
        raw = {c: load_concept(store, entity_id, c, as_of_ns, knowledge_cutoff_ns) for c in all_concepts()}
        self.fact_count = sum(len(v) for v in raw.values())
        self.currency, self.currency_flags = self._reporting_currency(raw)
        self.index: dict[str, dict[str, dict[tuple, Fact]]] = {}
        spans = set()
        for item in LINE_ITEMS:
            unit = self.unit_for(item)
            per: dict[str, dict[tuple, Fact]] = {}
            for concept in item.concepts:
                d: dict[tuple, Fact] = {}
                for f in raw.get(concept, ()):
                    if f.unit != unit:
                        continue
                    d[(f.start, f.end)] = f
                    if f.start is not None:
                        spans.add((f.start, f.end))
                per[concept] = d
            self.index[item.key] = per
        self.calendar = FiscalCalendar(spans)
        self._cache: dict[tuple, Cell] = {}

    # ------------------------------------------------------------ basics
    @staticmethod
    def _reporting_currency(raw: dict[str, list[Fact]]) -> tuple[str, tuple[str, ...]]:
        """The ISO unit of the most recent revenue / assets / net income fact; USD when none."""
        anchors = [c for k in ("revenue", "total_assets", "net_income") for c in ITEMS[k].concepts]
        facts = [f for c in anchors for f in raw.get(c, ()) if re.fullmatch(r"[A-Z]{3}", f.unit)]
        if not facts:
            return "USD", ("reporting_currency_assumed_USD",)
        latest = max(facts, key=lambda f: (f.end, f.available_ns))
        units = {f.unit for f in facts}
        return latest.unit, (("multiple_currency_units:" + ",".join(sorted(units)),) if len(units) > 1 else ())

    def unit_for(self, item: LineItem) -> str:
        return {"money": self.currency, "per_share": f"{self.currency}/shares", "shares": "shares"}[item.unit_kind]

    @property
    def fiscal_years(self) -> list[Period]:
        return self.calendar.fiscal_years

    @property
    def quarters(self) -> list[Period]:
        return self.calendar.quarters

    def latest_quarter(self) -> Period | None:
        return self.quarters[-1] if self.quarters else None

    def latest_fiscal_year(self) -> Period | None:
        return self.fiscal_years[-1] if self.fiscal_years else None

    def _missing(self, item: LineItem, period: Period, reason: str, detail: str, **kw) -> Cell:
        return Cell(item.key, period, self.unit_for(item), None, method="MISSING", missing_reason=reason, detail=detail, **kw)

    # ------------------------------------------------------------ cells
    def cell(self, key: str, period: Period) -> Cell:
        ck = (key, period.kind, period.start, period.end)
        if ck not in self._cache:
            self._cache[ck] = self._cell(ITEMS[key], period)
        return self._cache[ck]

    def _cell(self, item: LineItem, period: Period) -> Cell:
        if period.kind == "TTM":
            q = next((q for q in self.quarters if q.end == period.end), None)
            if q is None:
                # Never substitute another quarter (ttm(None) would silently mean "the latest quarter").
                return self._missing(item, period, "NOT_REPORTED", f"no fiscal quarter ends on {period.end} at the as-of time; "
                                     "a TTM column is anchored on a known quarter end")
            return self.ttm(item.key, q)
        if item.period_type == "duration" and period.kind == "INSTANT":
            return self._missing(item, period, "NOT_APPLICABLE", "a flow item has no value at an instant")
        if item.compute and item.compute_when == "always":
            return self._computed(item, period)
        lookup = (None, period.end) if item.period_type == "instant" else (period.start, period.end)
        for concept in item.concepts:
            f = self.index[item.key][concept].get(lookup)
            if f is not None:
                return Cell(item.key, period, f.unit, f.value, "REPORTED", concept, (f,), formula=None)
        reason, detail = "NOT_REPORTED", "no mapped concept reported for this period at the as-of time"
        if item.period_type == "duration" and period.kind == "Q":
            if item.summable:
                derived = self._derive_quarter(item, period)
                if derived.ok:
                    return derived
                reason, detail = derived.missing_reason, derived.detail or detail
            else:
                reason, detail = "NOT_DERIVABLE", ("not reported for this quarter; per-share values and share counts are "
                                                   "never derived by difference")
        if item.compute:
            computed = self._computed(item, period)
            if computed.ok:
                return computed
            detail += f"; fallback computation unavailable ({computed.detail})"
        return self._missing(item, period, reason, detail)

    @staticmethod
    def _amended(facts) -> tuple[str, ...]:
        """Flag a derived value with an input from an amended filing (10-K/A, 10-Q/A): the difference then carries the
        whole restatement of the longer period, e.g. Q4 = restated FY - as-originally-filed 9M (CAP-CO-06)."""
        return ("derived_from_amended_filing",) if any((f.form or "").endswith("/A") for f in facts) else ()

    def _derive_quarter(self, item: LineItem, q: Period) -> Cell:
        fs, n = q.fiscal_start, q.quarter
        if fs is None or n is None:
            return self._missing(item, q, "NOT_DERIVABLE", "quarter not aligned to a fiscal year")
        if n == 1:
            return self._missing(item, q, "NOT_REPORTED", "first quarter not reported (it equals its own year-to-date value; nothing to derive from)")
        prev_end = q.start - ONE_DAY
        names = {2: "Q2 = 6M YTD - Q1", 3: "Q3 = 9M YTD - 6M YTD", 4: "Q4 = FY - 9M YTD"}
        for concept in item.concepts:
            idx = self.index[item.key][concept]
            ytd, prev = idx.get((fs, q.end)), idx.get((fs, prev_end))
            if ytd is not None and prev is not None:
                return Cell(item.key, q, ytd.unit, ytd.value - prev.value, "DERIVED_QUARTER", concept, (ytd, prev),
                            formula=f"{names[n]}: [{fs}..{q.end}] {ytd.value} - [{fs}..{prev_end}] {prev.value}",
                            flags=("DERIVED",) + self._amended((ytd, prev)))
        prior = [p for p in self.quarters if p.fiscal_start == fs and p.quarter is not None and p.quarter < n]
        if len(prior) == n - 1:
            for concept in item.concepts:
                idx = self.index[item.key][concept]
                ytd = idx.get((fs, q.end))
                parts = [idx.get((p.start, p.end)) for p in prior]
                if ytd is not None and all(parts):
                    total = parts[0].value
                    for p in parts[1:]:
                        total = total + p.value
                    label = "FY" if n == 4 else f"{3 * n}M YTD"
                    return Cell(item.key, q, ytd.unit, ytd.value - total, "DERIVED_QUARTER", concept, (ytd, *parts),
                                formula=f"Q{n} = {label} - ({' + '.join(f'Q{p.quarter}' for p in prior)})",
                                flags=("DERIVED",) + self._amended((ytd, *parts)))
        return self._missing(item, q, "NOT_DERIVABLE",
                             f"Q{n} not reported and not derivable: needs YTD [{fs}..{q.end}] and YTD [{fs}..{prev_end}] "
                             f"(or the reported prior quarters) from one concept, available at the as-of time")

    def _computed(self, item: LineItem, period: Period) -> Cell:
        terms = list(item.compute)
        if item.key == "total_debt":
            ltd = [self.cell(k, period) for k in ("long_term_debt_current", "long_term_debt_noncurrent")]
            if not any(c.ok for c in ltd):
                total = self.cell("long_term_debt_total", period)
                if not total.ok:
                    return self._missing(item, period, "NOT_REPORTED",
                                         "no long-term debt component (current, noncurrent or total) reported for this date")
                terms = [Term(1, "long_term_debt_total"), Term(1, "commercial_paper", False), Term(1, "short_term_borrowings", False)]
        comps: list[Cell] = []
        missing_required: list[str] = []
        not_reported: list[str] = []
        total: Fixed | None = None
        expr = []
        for t in terms:
            c = self.cell(t.item, period)
            expr.append(("- " if t.sign < 0 else "+ ") + t.item)
            if c.ok:
                comps.append(c)
                v = c.value if t.sign > 0 else -c.value
                total = v if total is None else total + v
            elif t.required:
                missing_required.append(t.item)
            else:
                not_reported.append(t.item)
        formula = " ".join(expr).lstrip("+ ").strip()
        if missing_required:
            return self._missing(item, period, "MISSING_INPUT", f"{formula}: requires {', '.join(missing_required)}",
                                 components=tuple(comps))
        if total is None:
            return self._missing(item, period, "NOT_REPORTED", f"{formula}: no component reported")
        flags = ("components_not_reported",) if not_reported else ()
        return Cell(item.key, period, self.unit_for(item), total, "COMPUTED", None, (), tuple(comps), formula,
                    not_reported=tuple(not_reported), flags=flags)

    # ------------------------------------------------------------ instants and TTM
    def latest_instant(self, key: str, max_end: date | None = None) -> Cell:
        """Most recent instant value of ``key`` (any date <= max_end), e.g. cover-page shares."""
        item = ITEMS[key]
        best: Fact | None = None
        for concept in item.concepts:
            for (start, end), f in self.index[key][concept].items():
                if start is None and (max_end is None or end <= max_end) and (best is None or (end, f.available_ns) > (best.end, best.available_ns)):
                    best = f
        if best is None:
            return self._missing(item, Period("INSTANT", max_end or date.max), "NOT_REPORTED", "no instant reported at the as-of time")
        return Cell(key, Period("INSTANT", best.end), best.unit, best.value, "REPORTED", best.concept, (best,))

    def fiscal_year_of(self, q: Period) -> Period | None:
        for fy in self.fiscal_years:
            if fy.start == q.fiscal_start and fy.end == q.end:
                return fy
        return None

    def ttm(self, key: str, end: Period | None = None) -> Cell:
        item = ITEMS[key]
        qs = self.quarters
        q0 = end or (qs[-1] if qs else None)
        if q0 is None:
            return self._missing(item, Period("TTM", date.min), "NOT_REPORTED", "no fiscal quarter reported at the as-of time")
        if item.period_type == "instant":
            c = self.cell(key, Period("INSTANT", q0.end))
            return replace(c, period=Period("TTM", q0.end), method="LATEST_INSTANT" if c.ok else c.method)
        if not item.summable:
            return self._missing(item, Period("TTM", q0.end), "NOT_APPLICABLE",
                                 "per-share values and share counts are never summed across periods")
        i = qs.index(q0)
        window = qs[max(0, i - 3): i + 1]
        period = Period("TTM", q0.end, window[0].start)
        problems: list[str] = []
        reason = "MISSING_INPUT"
        if len(window) < 4:
            problems.append(f"only {len(window)} fiscal quarters known before {q0.end}")
        for a, b in zip(window, window[1:]):
            if b.start != a.end + ONE_DAY:
                problems.append(f"quarters not contiguous: {a.label} ends {a.end} and {b.label} starts {b.start}; "
                                f"the quarter in between is not available at the as-of time")
                reason = "NON_CONTIGUOUS"
        if any("transition_period" in q.flags for q in window):
            return self._missing(item, period, "FISCAL_TRANSITION",
                                 "window includes a fiscal-year transition period; excluded from TTM by default (CAP-CO-05)")
        cells: list[Cell] = []
        if not problems:
            cells = [self.cell(key, q) for q in window]
            if all(c.ok for c in cells):
                total = cells[0].value
                for c in cells[1:]:
                    total = total + c.value
                return Cell(key, period, cells[0].unit, total, "TTM_SUM", None, (), tuple(cells),
                            formula=" + ".join(c.period.label for c in cells))
            problems.append("missing " + ", ".join(f"{c.period.label} ({c.missing_reason})" for c in cells if not c.ok))
        if q0.quarter == 4:
            fy = self.fiscal_year_of(q0)
            if fy is not None:
                c = self.cell(key, fy)
                if c.ok:
                    return Cell(key, Period("TTM", q0.end, fy.start), c.unit, c.value, "TTM_ANNUAL", None, (), (c,),
                                formula=f"window equals {fy.label}")
        alt = self._ttm_alt(item, q0)
        if alt is not None:
            return alt
        return self._missing(item, period, reason, "; ".join(problems), components=tuple(c for c in cells if c.ok))

    def _ttm_alt(self, item: LineItem, q0: Period) -> Cell | None:
        """TTM = FY_last + YTD_current - YTD_prior (same YTD length), one concept, flagged (CAP-CO-05)."""
        if q0.quarter not in (1, 2, 3) or q0.fiscal_start is None:
            return None
        fs = q0.fiscal_start
        prev_fy = next((fy for fy in self.fiscal_years if fy.end == fs - ONE_DAY), None)
        if prev_fy is None:
            return None
        cls = duration_class(fs, q0.end)
        for concept in item.concepts:
            idx = self.index[item.key][concept]
            fy_f, cur = idx.get((prev_fy.start, prev_fy.end)), idx.get((fs, q0.end))
            prior = next((f for (s, e), f in idx.items() if s == prev_fy.start and e is not None and duration_class(s, e) == cls
                          and e < prev_fy.end), None)
            if fy_f and cur and prior:
                return Cell(item.key, Period("TTM", q0.end, prior.end + ONE_DAY), cur.unit, fy_f.value + cur.value - prior.value,
                            "TTM_ALT", concept, (fy_f, cur, prior),
                            formula=f"{prev_fy.label} + YTD[{fs}..{q0.end}] - YTD[{prior.start}..{prior.end}]",
                            flags=("TTM_ALT_FORMULA",) + self._amended((fy_f, cur, prior)))
        return None

    def quarters_back(self, q0: Period, n: int) -> Period | None:
        """The n-th quarter before ``q0`` in list order. Not a date offset: a quarter missing from the calendar
        (an unfiled 10-Q, a late 10-K) shifts it, so year-over-year comparisons use ``year_ago_quarter``."""
        qs = self.quarters
        i = qs.index(q0) - n
        return qs[i] if i >= 0 else None

    def year_ago_quarter(self, q0: Period) -> Period | None:
        """The fiscal quarter ending one fiscal year before ``q0`` (CAP-CO-05 YoY: value_t / value_{t-4 quarters}),
        found by date: its end lies 361-374 days before ``q0.end`` (52 or 53 weeks, or a calendar year, +/- 3 days).
        None when no such quarter is known at the as-of time; the caller reports the comparison as missing rather
        than comparing with an older quarter."""
        lo, hi = YEAR_AGO_DAYS
        cands = [q for q in self.quarters if lo <= (q0.end - q.end).days <= hi]
        if not cands:
            return None
        return min(cands, key=lambda q: (abs((q0.end - q.end).days - 365), q.start or date.min))

    def ttm_window(self, q0: Period) -> list[Period] | None:
        """The four contiguous discrete quarters ending at ``q0``, or None when they are not all known."""
        qs = self.quarters
        i = qs.index(q0)
        window = qs[max(0, i - 3): i + 1]
        if len(window) < 4 or any(b.start != a.end + ONE_DAY for a, b in zip(window, window[1:])):
            return None
        return window

    def fiscal_year_back(self, fy: Period, n: int) -> Period | None:
        i = self.fiscal_years.index(fy) - n
        return self.fiscal_years[i] if i >= 0 else None

    # ------------------------------------------------------------ restatement history
    def with_history(self, c: Cell) -> Cell:
        """Attach earlier publications of the same reported fact available by the as-of time (CAP-CO-06)."""
        if c.method != "REPORTED" or not c.facts:
            return c
        f = c.facts[0]
        rows = self.store.history(self.entity_id, f.concept, f.end.isoformat())
        hist = []
        for r in rows:
            if r["dataset"] != SEC_FACTS or r["unit"] != f.unit or r["value"] is None:
                continue
            if (r["period_start"] or None) != (f.start.isoformat() if f.start else None):
                continue
            if r["available_time_ns"] > self.as_of_ns or r["obs_id"] == f.obs_id:
                continue
            if self.knowledge_cutoff_ns is None:
                if r["known_to_ns"] != OPEN_END:
                    continue
            elif not (r["known_from_ns"] <= self.knowledge_cutoff_ns < r["known_to_ns"]):
                continue
            hist.append({"obs_id": r["obs_id"], "value": r["value"], "accession": r["publication_id"],
                         "available_time": ns_to_iso(r["available_time_ns"])})
        hist.sort(key=lambda h: (h["available_time"], h["accession"]))
        return replace(c, history=tuple(hist)) if hist else c

    # ------------------------------------------------------------ statements
    def columns(self, basis: str, periods: int) -> list[Period]:
        if basis == "FY":
            return list(reversed(self.fiscal_years[-periods:]))
        if basis == "Q":
            return list(reversed(self.quarters[-periods:]))
        if basis == "TTM":
            cols = []
            for q in reversed(self.quarters[-periods:]):
                window = self.ttm_window(q)       # header only; each cell carries its own TTM window
                cols.append(Period("TTM", q.end, window[0].start if window else None, q.fiscal_year, q.quarter, q.fiscal_start))
            return cols
        raise ValueError("basis must be FY, Q or TTM")

    def statement(self, basis: str = "FY", periods: int = 4, *, history: bool = True) -> dict:
        if not 1 <= periods <= 40:
            raise ValueError("periods must be between 1 and 40")
        cols = self.columns(basis, periods)
        sections: dict[str, list] = {s: [] for s in STATEMENTS}
        for item in LINE_ITEMS:
            cells = []
            for p in cols:
                if basis == "TTM":
                    c = self.ttm(item.key, next(q for q in self.quarters if q.end == p.end))
                else:
                    c = self.cell(item.key, p)
                cells.append((self.with_history(c) if history else c).to_json(self.cik, depth=0))
            sections[item.statement].append({"item": item.key, "label": item.label, "unit": self.unit_for(item),
                                             "rule": item.rule(), "note": item.note, "cells": cells})
        return {"entity_id": self.entity_id, "as_of": ns_to_iso(self.as_of_ns), "as_of_ns": self.as_of_ns,
                "knowledge_cutoff": ns_to_iso(self.knowledge_cutoff_ns) if self.knowledge_cutoff_ns is not None else None,
                "mapping": MAPPING_REF, "basis": basis, "currency": self.currency, "currency_flags": list(self.currency_flags),
                "columns": [p.to_json() for p in cols], "statements": sections,
                "calendar_flags": list(self.calendar.flags), "facts_used": self.fact_count,
                "coverage": self.coverage(),
                "source": {"dataset": SEC_FACTS, "provider": "sec-edgar",
                           "attribution": "Source: U.S. SEC EDGAR XBRL companyfacts (public information)"}}

    def coverage(self, sample: int = 25) -> dict:
        """Concepts the issuer reported by the as-of time that the mapping does not use (CAP-CO-05)."""
        mapped = set(all_concepts())
        # Same point-in-time filter as ResearchStore.as_of: available by t, and current knowledge (research mode)
        # or the versions this system held at the knowledge cutoff (audit mode).
        sql = "SELECT DISTINCT field FROM observation WHERE entity_id=? AND dataset=? AND available_time_ns<=?"
        params: list = [self.entity_id, SEC_FACTS, self.as_of_ns]
        if self.knowledge_cutoff_ns is None:
            sql += " AND known_to_ns=?"
            params.append(OPEN_END)
        else:
            sql += " AND known_from_ns<=? AND known_to_ns>?"
            params += [self.knowledge_cutoff_ns, self.knowledge_cutoff_ns]
        rows = self.store.conn.execute(sql, tuple(params)).fetchall()
        reported = {r["field"] for r in rows}
        unmapped = sorted(reported - mapped)
        used = sorted(c for c in reported & mapped)
        return {"mapping": MAPPING_REF, "reported_concepts": len(reported), "mapped_concepts_used": len(used),
                "unmapped_concepts": len(unmapped), "unmapped_sample": unmapped[:sample],
                "note": "Unmapped concepts (company extensions, detail lines) are excluded from standardized totals; "
                        "extension concepts are not in companyfacts at all (CAP-CO-04)."}
