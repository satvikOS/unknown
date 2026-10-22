"""Fiscal-period alignment for XBRL facts (CAP-CO-04, CAP-CO-05).

A fact's own period is its (start, end) dates. The companyfacts ``fy``/``fp`` fields describe the
*filing's* fiscal focus, not the fact (a 10-K carries prior-year comparatives with fp=FY, a Q2
10-Q carries both a 3-month and a 6-month period with fp=Q2), so they are never used to classify.

Duration classes use inclusive day counts with the SEC frames tolerance (annual 365 +/- 30 days,
quarterly 91 +/- 30) extended to half-year (182 +/- 30) and nine-month (273 +/- 30) year-to-date
periods. 52-53 week fiscal years (364 or 371 days, quarters of 91 or 98 days) fall inside the
windows, so no calendar-quarter assumption is made anywhere.

Fiscal years are the reported annual periods. A fiscal year starts at the start of an annual
period, the day after an annual period ends, or the start of a half-year / nine-month YTD period.
Quarter boundaries come from the ends of YTD periods (3M/6M/9M/FY measured from a fiscal start)
and from reported 3-month periods; quarter ``n`` is the one whose fiscal-start-to-end span falls in
the n-th window. Labelling convention (this project's): ``fiscal_year`` is the calendar year in
which the fiscal year ends (e.g. Apple's year ending 2025-09-27 is FY2025); filers' own names can
differ (retailers ending in January). Consecutive annual periods that do not abut are a fiscal
calendar break (fiscal-year change); periods spanning a break are not combined.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

ONE_DAY = timedelta(days=1)
DURATION_CLASSES = (("Q", 61, 121), ("H1", 152, 213), ("9M", 243, 304), ("FY", 335, 395))
POSITION = {"Q": 1, "H1": 2, "9M": 3, "FY": 4}
YEAR_DAYS = (364, 365, 366, 371)   # 52 weeks, calendar year, leap year, 53 weeks


def parse_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def days_inclusive(start: date, end: date) -> int:
    return (end - start).days + 1


def duration_class(start: date, end: date) -> str:
    n = days_inclusive(start, end)
    for name, lo, hi in DURATION_CLASSES:
        if lo <= n <= hi:
            return name
    return "OTHER"


def _add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:      # 29 February
        return d.replace(year=d.year + 1, day=28)


@dataclass(frozen=True)
class Period:
    """A standardized statement column. ``kind`` is FY, Q, TTM or INSTANT."""
    kind: str
    end: date
    start: date | None = None
    fiscal_year: int | None = None
    quarter: int | None = None
    fiscal_start: date | None = None
    flags: tuple[str, ...] = ()

    @property
    def key(self) -> tuple:
        return (self.start, self.end)

    @property
    def label(self) -> str:
        if self.kind == "FY":
            return f"FY{self.fiscal_year}"
        if self.kind == "Q":
            return f"FY{self.fiscal_year} Q{self.quarter}"
        if self.kind == "TTM":
            return f"TTM to {self.end.isoformat()}"
        return self.end.isoformat()

    def to_json(self) -> dict:
        return {"label": self.label, "kind": self.kind, "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat(), "days": days_inclusive(self.start, self.end) if self.start else None,
                "fiscal_year": self.fiscal_year, "fiscal_quarter": self.quarter,
                "fiscal_start": self.fiscal_start.isoformat() if self.fiscal_start else None, "flags": list(self.flags)}


class FiscalCalendar:
    """Fiscal years and discrete quarters implied by an issuer's reported duration periods."""

    def __init__(self, spans: Iterable[tuple[date, date]]):
        spans = {(s, e) for s, e in spans if s is not None and e is not None and e >= s}
        self.flags: list[str] = []
        self.annual = sorted({(s, e) for s, e in spans if duration_class(s, e) == "FY"}, key=lambda p: (p[1], p[0]))
        starts = {s for s, _ in self.annual} | {e + ONE_DAY for _, e in self.annual}
        starts |= {s for s, e in spans if duration_class(s, e) in ("H1", "9M")}
        # A 3-month period that starts exactly one fiscal year (52 or 53 weeks, or a calendar year)
        # after a known fiscal start begins the next fiscal year, before that year's annual report
        # exists (e.g. a Q1 10-Q filed before a late 10-K).
        for _ in range(3):
            more = {s for s, e in spans if duration_class(s, e) == "Q" and s not in starts
                    and any((s - k).days in YEAR_DAYS for k in starts)}
            if not more:
                break
            starts |= more
        self.starts = sorted(starts)
        self.breaks: list[tuple[date, date]] = []
        for (s1, e1), (s2, e2) in zip(self.annual, self.annual[1:]):
            if s2 != e1 + ONE_DAY:
                self.breaks.append((e1, s2))
                self.flags.append(f"fiscal_calendar_break:{e1.isoformat()}->{s2.isoformat()}")
        self.other = sorted((s, e) for s, e in spans if duration_class(s, e) == "OTHER")
        self.fiscal_years = [self._fy_period(s, e) for s, e in self.annual]
        self.quarters = self._quarters(spans)

    # ------------------------------------------------------------ fiscal years
    def fiscal_start_for(self, start: date, end: date) -> date | None:
        candidates = [s for s in self.starts if s <= start and days_inclusive(s, end) <= 395]
        return max(candidates) if candidates else None

    def fiscal_year_end(self, fiscal_start: date) -> tuple[date, bool]:
        """(end, estimated). Estimated ends assume a 12-month year and only label the year."""
        for s, e in self.annual:
            if s == fiscal_start:
                return e, False
        return _add_year(fiscal_start) - ONE_DAY, True

    def _fy_period(self, s: date, e: date) -> Period:
        flags = ()
        same_year = [p for p in self.annual if p[1].year == e.year and p != (s, e)]
        if same_year:
            flags = ("two_fiscal_years_end_in_same_calendar_year",)
        return Period("FY", e, s, fiscal_year=e.year, fiscal_start=s, flags=flags)

    # ------------------------------------------------------------ quarters
    def _quarters(self, spans: set) -> list[Period]:
        boundaries: dict[date, set[date]] = {}
        for s, e in spans:
            cls = duration_class(s, e)
            if cls in ("H1", "9M", "FY"):
                boundaries.setdefault(s, set()).add(e)
            elif cls == "Q":
                fs = self.fiscal_start_for(s, e)
                if fs is None:
                    continue
                boundaries.setdefault(fs, set()).add(e)
                if s != fs:
                    boundaries[fs].add(s - ONE_DAY)
        out: dict[tuple, Period] = {}
        for fs, ends in boundaries.items():
            fy_end, estimated = self.fiscal_year_end(fs)
            prev = fs
            for e in sorted(ends):
                if e < fs:
                    continue
                pos = duration_class(fs, e)
                if pos == "OTHER" or e > fy_end + timedelta(days=7):
                    continue
                q_start = prev
                prev = e + ONE_DAY
                if duration_class(q_start, e) != "Q":
                    continue  # a missing boundary in between: the span is not one quarter
                flags = ("fiscal_year_end_estimated",) if estimated else ()
                if self._in_transition(q_start, e):
                    flags += ("transition_period",)
                out[(q_start, e)] = Period("Q", e, q_start, fiscal_year=fy_end.year, quarter=POSITION[pos],
                                           fiscal_start=fs, flags=flags)
        return sorted(out.values(), key=lambda p: (p.end, p.start))

    def _in_transition(self, start: date, end: date) -> bool:
        """True when the span lies in the gap between two non-abutting annual periods (a fiscal-year
        change). Such periods are excluded from TTM and growth unless the owner overrides (CAP-CO-05)."""
        return any(e1 < start and end < s2 for e1, s2 in self.breaks if s2 > e1 + ONE_DAY)

    def quarter_for(self, start: date, end: date) -> Period | None:
        for q in self.quarters:
            if q.start == start and q.end == end:
                return q
        return None
