"""Trading sessions for chart buckets, from the sourced venue registry and calendars.

A known session comes from the calendar inside its data horizon. Outside the horizon the venue's regular hours are
still known but holidays and early closes are not: such a day is used only when bars exist for it, and it never
produces a gap. A MIC with no sourced calendar falls back to whole UTC days, stated in ``notes``.
"""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..domain.calendar import BusinessCalendar, CalendarHorizonError, SessionTimeUnknown
from .intervals import NS

DAY_NS = 86_400 * NS


@dataclass(frozen=True)
class Session:
    day: date            # trading date in the venue's time zone
    open_ns: int
    close_ns: int
    known: bool          # from the calendar inside its horizon (holidays and early closes applied)


@lru_cache(maxsize=1)
def _registry():
    from ..domain.markets import load_registry
    return load_registry()


def _ns(dt: datetime) -> int:
    return _cal.timegm(dt.astimezone(timezone.utc).utctimetuple()) * NS


class SessionClock:
    def __init__(self, mic: str | None, cal: BusinessCalendar | None = None, tz: str | None = None):
        self.mic = mic
        self.notes: list[str] = []
        self.cal, self.tz = cal, tz
        if cal is None and mic:
            try:
                reg = _registry()
                venue = reg.venue(mic)
                self.cal, self.tz = reg.trading_calendar(mic), venue.timezone
            except (KeyError, LookupError, ValueError, OSError) as exc:
                self.notes.append(f"no sourced trading calendar for MIC {mic} ({type(exc).__name__}): buckets are "
                                  "whole UTC days and UTC-aligned intraday buckets; sessions and gaps are not "
                                  "calendar-aware")
        elif cal is None:
            self.notes.append("no market (MIC) in the key: buckets are whole UTC days; gaps are not calendar-aware")
        self.zone = ZoneInfo(self.tz or (self.cal and (self.cal.session_timezone or self.cal.timezone)) or "UTC")
        self._cache: dict[date, Session | None] = {}
        self._outside_horizon = False

    @property
    def calendar_id(self) -> str | None:
        return self.cal.calendar_id if self.cal else None

    @property
    def timezone(self) -> str:
        return str(self.zone.key) if self.cal else "UTC"

    def open_offsets(self) -> set[int]:
        """Seconds from local midnight to the regular open (0 without a calendar)."""
        if self.cal is None or self.cal.regular_session is None:
            return {0}
        t: time = self.cal.regular_session[0]
        return {t.hour * 3600 + t.minute * 60}

    def trading_date(self, ns: int) -> date:
        if self.cal is None:
            return datetime.fromtimestamp(ns // NS, tz=timezone.utc).date()
        return datetime.fromtimestamp(ns // NS, tz=timezone.utc).astimezone(self.zone).date()

    def session(self, day: date) -> Session | None:
        """The session on ``day``; None when the venue is closed (or the day cannot be placed at all)."""
        if day in self._cache:
            return self._cache[day]
        out: Session | None
        if self.cal is None:
            start = _cal.timegm(day.timetuple()) * NS
            out = Session(day, start, start + DAY_NS, False)
        else:
            try:
                utc = self.cal.session_utc(day)
                out = None if utc is None else Session(day, _ns(utc[0]), _ns(utc[1]), True)
            except CalendarHorizonError:
                self._outside_horizon = True
                out = self._regular(day)
            except SessionTimeUnknown as exc:
                self.notes.append(f"{day.isoformat()}: {exc}")
                out = None
        self._cache[day] = out
        return out

    def _regular(self, day: date) -> Session | None:
        if self.cal.regular_session is None or day.weekday() in self.cal.weekend:
            return None
        o, c = self.cal.regular_session
        tz = ZoneInfo(self.cal.session_timezone or self.cal.timezone or "UTC")
        return Session(day, _ns(datetime.combine(day, o, tzinfo=tz)), _ns(datetime.combine(day, c, tzinfo=tz)), False)

    def session_of(self, ns: int) -> Session | None:
        """The session containing instant ``ns`` (start inclusive, close exclusive), else None."""
        s = self.session(self.trading_date(ns))
        return s if s is not None and s.open_ns <= ns < s.close_ns else None

    def known_sessions(self, from_ns: int, to_ns: int) -> list[Session]:
        """Calendar-known sessions overlapping [from_ns, to_ns): the ones whose absence is a gap."""
        out = []
        d, last = self.trading_date(from_ns) - timedelta(days=1), self.trading_date(to_ns)
        while d <= last:
            s = self.session(d)
            if s is not None and s.known and s.close_ns > from_ns and s.open_ns < to_ns:
                out.append(s)
            d += timedelta(days=1)
        return out

    def finish_notes(self) -> list[str]:
        notes = list(self.notes)
        if self._outside_horizon and self.cal is not None:
            notes.append(f"some dates fall outside calendar {self.cal.calendar_id} {self.cal.version} "
                         f"({self.cal.valid_from}..{self.cal.valid_through}): regular hours are used for days that "
                         "have bars; holidays and early closes there are unknown, so those days produce no gaps")
        return notes
