"""Business-day calendars for expected settlement dates and risk days.

A calendar is versioned data, not a rule of thumb: holidays come from a
dated, sourced file. Settlement is *confirmed* only from broker balance
evidence (ADR-0006); the calendar only predicts when to expect it so a late
settlement can be detected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .clock import NS_PER_S


class CalendarHorizonError(LookupError):
    """The calendar has no authoritative data for this date: the answer is unknown, never 'open'."""


@dataclass(frozen=True)
class BusinessCalendar:
    calendar_id: str
    version: str
    holidays: frozenset[date] = field(default_factory=frozenset)
    weekend: frozenset[int] = frozenset({5, 6})  # Monday=0
    source: str = ""
    valid_from: date | None = None
    valid_through: date | None = None   # None = rule-based fixture calendar without a data horizon
    early_closes: tuple[tuple[date, str], ...] = ()

    def is_business_day(self, day: date) -> bool:
        if (self.valid_through is not None and day > self.valid_through) or (self.valid_from is not None and day < self.valid_from):
            raise CalendarHorizonError(f"{self.calendar_id} {self.version} has no data for {day.isoformat()}")
        return day.weekday() not in self.weekend and day not in self.holidays

    def add_business_days(self, start: date, days: int) -> date:
        if days < 0:
            raise ValueError("days must be >= 0")
        current = start
        remaining = days
        while remaining:
            current += timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current


def local_date(ns: int, tz: str) -> date:
    """Calendar date of a UTC instant in the given IANA timezone."""
    return datetime.fromtimestamp(ns // NS_PER_S, tz=timezone.utc).astimezone(ZoneInfo(tz)).date()


WEEKDAYS_ONLY = BusinessCalendar("WEEKDAYS", "1", source="fixture: weekends only, no holidays")


def load_calendar(path) -> BusinessCalendar:
    """Load a sourced calendar data file (config/calendars/*.json)."""
    import json
    from pathlib import Path
    d = json.loads(Path(path).read_text())
    return BusinessCalendar(
        calendar_id=d["calendar_id"], version=d["version"],
        holidays=frozenset(date.fromisoformat(h[0]) for h in d["holidays"]), source=d["source"],
        valid_from=date.fromisoformat(d["valid_from"]), valid_through=date.fromisoformat(d["valid_through"]),
        early_closes=tuple((date.fromisoformat(e[0]), e[1]) for e in d.get("early_closes", [])))
