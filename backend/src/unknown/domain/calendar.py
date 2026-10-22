"""Business-day calendars for expected settlement dates and risk days.

A calendar is versioned data, not a rule of thumb: holidays come from a
dated, sourced file. Settlement is *confirmed* only from broker balance
evidence (ADR-0006); the calendar only predicts when to expect it so a late
settlement can be detected.

Two kinds of calendar file exist under ``config/calendars``:

* ``kind: "trading"`` — when a venue trades (regular session, early closes,
  special sessions such as India's Diwali Muhurat session).
* ``kind: "settlement"`` — the business days of the clearing corporation or
  settlement system on which a trade's settlement date is counted. Trading and
  settlement days differ (an Indian bank holiday can be a trading day; Xetra's
  24 and 31 December are settlement days without trading).

Session-level data is additive and optional. When a venue has announced a
session but not its times, the time is stored as unknown and
:meth:`BusinessCalendar.session` raises :class:`SessionTimeUnknown` rather than
assuming the regular hours.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .clock import NS_PER_S


class CalendarHorizonError(LookupError):
    """The calendar has no authoritative data for this date: the answer is unknown, never 'open'."""


class SessionTimeUnknown(LookupError):
    """The venue trades on this date but the session times are not (yet) published in the sourced data."""


@dataclass(frozen=True)
class SessionWindow:
    """A trading window in the calendar's ``session_timezone`` (local wall-clock times)."""
    open: time
    close: time
    kind: str = "regular"      # regular | early_close | special
    label: str = ""

    def __post_init__(self) -> None:
        if not self.open < self.close:
            raise ValueError(f"session must open before it closes: {self.open} >= {self.close}")


@dataclass(frozen=True)
class SpecialSession:
    """A session on a day that is not an ordinary business day (e.g. NSE/BSE Diwali Muhurat trading).

    ``open``/``close`` are ``None`` when the venue has announced the session
    but the retrieved sources do not state its times. ``settles`` records
    whether the venue states that trades create settlement obligations
    (``None`` = not stated in the retrieved source).
    """
    day: date
    kind: str
    label: str
    open: time | None = None
    close: time | None = None
    settles: bool | None = None
    source: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if (self.open is None) != (self.close is None):
            raise ValueError("a special session states both open and close, or neither")
        if self.open is not None and not self.open < self.close:
            raise ValueError("special session must open before it closes")

    @property
    def times_known(self) -> bool:
        return self.open is not None


@dataclass(frozen=True)
class BusinessCalendar:
    calendar_id: str
    version: str
    holidays: frozenset[date] = field(default_factory=frozenset)
    weekend: frozenset[int] = frozenset({5, 6})  # Monday=0
    source: str = ""
    valid_from: date | None = None
    valid_through: date | None = None   # None = rule-based fixture calendar without a data horizon
    # (date, "HH:MM" close) — the close is None when the venue announced a shortened day but not its time.
    early_closes: tuple[tuple[date, str | None], ...] = ()
    # Additive session data (all optional; absent for fixture calendars).
    kind: str = "trading"                       # trading | settlement
    timezone: str | None = None                 # IANA zone of the venue / settlement system
    session_timezone: str | None = None         # zone the session times are stated in (defaults to timezone)
    regular_session: tuple[time, time] | None = None
    special_sessions: tuple[SpecialSession, ...] = ()
    session_times_pending: tuple[tuple[date, str], ...] = ()   # trading day whose hours the venue has not fixed yet

    # ------------------------------------------------------------ horizon
    def _check_horizon(self, day: date) -> None:
        if (self.valid_through is not None and day > self.valid_through) or (self.valid_from is not None and day < self.valid_from):
            raise CalendarHorizonError(f"{self.calendar_id} {self.version} has no data for {day.isoformat()}")

    # ------------------------------------------------------- business days
    def is_business_day(self, day: date) -> bool:
        """An ordinary weekday that is not a holiday. Special sessions are *not* business days."""
        self._check_horizon(day)
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

    # ------------------------------------------------------------ sessions
    def special_session(self, day: date) -> SpecialSession | None:
        self._check_horizon(day)
        for s in self.special_sessions:
            if s.day == day:
                return s
        return None

    def is_trading_day(self, day: date) -> bool:
        """True on an ordinary business day or on a day with a declared special session."""
        return self.is_business_day(day) or self.special_session(day) is not None

    def early_close(self, day: date) -> tuple[bool, str | None]:
        """(is an early-close day, close "HH:MM" or None when the time is not published)."""
        self._check_horizon(day)
        for d, close in self.early_closes:
            if d == day:
                return True, close
        return False, None

    def session(self, day: date) -> SessionWindow | None:
        """The trading window on ``day`` in ``session_timezone``; None when the venue is closed.

        Raises CalendarHorizonError beyond the data horizon and SessionTimeUnknown
        when the venue trades but the sourced data does not give the hours.
        """
        special = self.special_session(day)
        if special is not None:
            if not special.times_known:
                raise SessionTimeUnknown(f"{self.calendar_id}: {special.kind} session on {day.isoformat()} has no published times")
            return SessionWindow(special.open, special.close, "special", special.label)
        if not self.is_business_day(day):
            return None
        for d, note in self.session_times_pending:
            if d == day:
                raise SessionTimeUnknown(f"{self.calendar_id}: hours on {day.isoformat()} not yet fixed by the venue ({note})")
        if self.regular_session is None:
            raise SessionTimeUnknown(f"{self.calendar_id} has no sourced regular session")
        is_early, close = self.early_close(day)
        if is_early:
            if close is None:
                raise SessionTimeUnknown(f"{self.calendar_id}: shortened session on {day.isoformat()} has no published close")
            return SessionWindow(self.regular_session[0], _parse_hhmm(close), "early_close")
        return SessionWindow(self.regular_session[0], self.regular_session[1], "regular")

    def session_utc(self, day: date) -> tuple[datetime, datetime] | None:
        """The session on ``day`` as aware UTC datetimes (None when closed)."""
        window = self.session(day)
        if window is None:
            return None
        zone_name = self.session_timezone or self.timezone
        if zone_name is None:
            raise SessionTimeUnknown(f"{self.calendar_id} has no time zone for its session times")
        zone = ZoneInfo(zone_name)
        start = datetime.combine(day, window.open, tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(day, window.close, tzinfo=zone).astimezone(timezone.utc)
        return start, end


def local_date(ns: int, tz: str) -> date:
    """Calendar date of a UTC instant in the given IANA timezone."""
    return datetime.fromtimestamp(ns // NS_PER_S, tz=timezone.utc).astimezone(ZoneInfo(tz)).date()


WEEKDAYS_ONLY = BusinessCalendar("WEEKDAYS", "1", source="fixture: weekends only, no holidays")


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    if len(hh) != 2 or len(mm) != 2:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    return time(int(hh), int(mm))


def _parse_special(d: dict) -> SpecialSession:
    open_ = d.get("open")
    close = d.get("close")
    return SpecialSession(
        day=date.fromisoformat(d["date"]), kind=d["kind"], label=d["label"],
        open=_parse_hhmm(open_) if open_ is not None else None,
        close=_parse_hhmm(close) if close is not None else None,
        settles=d.get("settles"), source=d.get("source", ""), notes=d.get("notes", ""))


def load_calendar(path) -> BusinessCalendar:
    """Load a sourced calendar data file (config/calendars/*.json).

    Required keys are those of XNYS.json; ``kind``, ``timezone``,
    ``session_timezone``, ``regular_session``, ``special_sessions`` and
    ``session_times_pending`` are optional. The file is validated so a typo
    cannot silently widen the horizon or hide a session.
    """
    import json
    from pathlib import Path
    d = json.loads(Path(path).read_text())
    valid_from = date.fromisoformat(d["valid_from"])
    valid_through = date.fromisoformat(d["valid_through"])
    if valid_through < valid_from:
        raise ValueError(f"{path}: valid_through precedes valid_from")
    holidays = [date.fromisoformat(h[0]) for h in d["holidays"]]
    early = tuple((date.fromisoformat(e[0]), e[1]) for e in d.get("early_closes", []))
    specials = tuple(_parse_special(s) for s in d.get("special_sessions", []))
    pending = tuple((date.fromisoformat(p[0]), p[1]) for p in d.get("session_times_pending", []))
    regular = d.get("regular_session")
    session = (_parse_hhmm(regular["open"]), _parse_hhmm(regular["close"])) if regular else None
    for label, days in (("holiday", holidays), ("early close", [e[0] for e in early]),
                        ("special session", [s.day for s in specials]), ("pending session", [p[0] for p in pending])):
        if len(set(days)) != len(days):
            raise ValueError(f"{path}: duplicate {label} date")
        for day in days:
            if not valid_from <= day <= valid_through:
                raise ValueError(f"{path}: {label} {day.isoformat()} is outside the data horizon")
    for e_day, close in early:
        if close is not None:
            _parse_hhmm(close)
        if e_day in holidays:
            raise ValueError(f"{path}: {e_day.isoformat()} is both a holiday and an early close")
    for s in specials:
        if s.day.weekday() < 5 and s.day not in holidays:
            # A special session must be explicit: it cannot sit on an ordinary business day.
            raise ValueError(f"{path}: special session {s.day.isoformat()} falls on an ordinary business day")
    for zone in (d.get("timezone"), d.get("session_timezone")):
        if zone is not None:
            ZoneInfo(zone)
    cal = BusinessCalendar(
        calendar_id=d["calendar_id"], version=d["version"],
        holidays=frozenset(holidays), source=d["source"],
        valid_from=valid_from, valid_through=valid_through,
        early_closes=early, kind=d.get("kind", "trading"), timezone=d.get("timezone"),
        session_timezone=d.get("session_timezone"), regular_session=session,
        special_sessions=specials, session_times_pending=pending)
    if cal.kind not in ("trading", "settlement"):
        raise ValueError(f"{path}: unknown calendar kind {cal.kind!r}")
    return cal
