"""The execution rule of a schema v2 spec: submission window and transmission-time limit (BR-DAT-19; book ch. 15
"Execute after the observation becomes available", ch. 2 "must not automatically participate in an auction";
ADR-0009 initial eligibility: LIMIT, DAY, regular hours only).

ONE implementation, used by the backtest engine (``engine._Run.execute``) and by the strategy runner
(``strategy.runner``), so both apply the same rule:

* **Decide** after the official close of session D (the spec's decision clock).
* **Submit** in the next regular session E, inside ``[open + N, close - M]`` (N, M from the spec, minutes). The
  session's hours come from the calendar, early closes included (XNYS 2026-11-27 closes at 13:00 New York, so the
  window ends at 12:45). A calendar without session hours (the WEEKDAYS fixture calendar) may only be used with
  FIXTURE data, whose session convention is 14:30-21:00 UTC (``research.fixtures``); real data needs the venue
  calendar (``SessionHoursUnknown`` otherwise). No auction: N >= 1 and M >= 1 minute (the spec enforces both).
* **Limit at transmission**: buy ``min(model bound, floor_tick(reference x (1 + collar)))``, sell
  ``max(model bound, ceil_tick(reference x (1 - collar)))``. The model bound is the decision's mark +/- the spec's
  ``limit_offset_bps``. The reference is the mid of a fresh, validated quote at transmission (runner). Daily bars
  carry no quote: the backtest uses the execution session's open, the last price known before the window opens,
  and labels it so.
* **Expire** at the window's end: the runner cancels its unfilled remainder; the backtest lets the order trade
  only a window's share of the session volume (``volume_window``: uniform intraday volume, an OPTIMISTIC assumption
  because volume concentrates at the open and the close, which the window excludes). Nothing is carried over.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..domain.calendar import BusinessCalendar, SessionTimeUnknown
from ..domain.clock import NS_PER_S
from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument

FIXTURE_SESSION_BASIS = "FIXTURE session convention 14:30-21:00 UTC (research.fixtures; this calendar has no hours)"
BACKTEST_REFERENCE = "execution session open (daily bars carry no quote at transmission; labelled)"
NS_PER_MIN = 60 * NS_PER_S


class SessionHoursUnknown(LookupError):
    """The window cannot be placed: the calendar has no hours for this session and the data is not a FIXTURE."""


@dataclass(frozen=True)
class SubmissionWindow:
    open_offset_minutes: int
    close_offset_minutes: int
    collar_bps: int
    limit_reference: str = "TRANSMISSION_QUOTE_MID"

    @classmethod
    def from_spec(cls, spec) -> "SubmissionWindow | None":
        """None for a schema v1 spec (legacy: submission 60 s after the open, the limit fixed at the decision)."""
        w = getattr(spec, "submission_window", None)
        if w is None:
            return None
        return cls(w["open_offset_minutes"], w["close_offset_minutes"], w["collar_bps"], w["limit_reference"])

    def to_json(self) -> dict:
        return {"open_offset_minutes": self.open_offset_minutes, "close_offset_minutes": self.close_offset_minutes,
                "collar_bps": self.collar_bps, "limit_reference": self.limit_reference, "auctions": False,
                "remainder": "CANCEL_AT_WINDOW_END", "rule": "execution.window.br-dat-19@1"}


@dataclass(frozen=True)
class WindowTimes:
    session_date: date
    open_ns: int
    close_ns: int
    start_ns: int          # open + N
    end_ns: int            # close - M: the intent's deadline and the remainder's cancellation time
    basis: str

    @property
    def empty(self) -> bool:
        return self.start_ns >= self.end_ns

    @property
    def minutes(self) -> tuple[int, int]:
        """(window minutes, session minutes)."""
        return (max(0, (self.end_ns - self.start_ns) // NS_PER_MIN), (self.close_ns - self.open_ns) // NS_PER_MIN)

    def contains(self, t_ns: int) -> bool:
        return self.start_ns <= t_ns < self.end_ns

    def to_json(self) -> dict:
        return {"session_date": self.session_date.isoformat(), "open_ns": self.open_ns, "close_ns": self.close_ns,
                "start_ns": self.start_ns, "end_ns": self.end_ns, "basis": self.basis}


def session_bounds(calendar: BusinessCalendar, day: date, *, fixture: bool) -> tuple[int, int, str]:
    """(open_ns, close_ns, basis) of the regular session on ``day``. ``CalendarHorizonError`` propagates."""
    try:
        utc = calendar.session_utc(day)
    except SessionTimeUnknown:
        utc = None
        known = False
    else:
        known = True
        if utc is None:
            raise SessionHoursUnknown(f"{calendar.calendar_id} has no session on {day.isoformat()}")
    if known:
        return (int(utc[0].timestamp()) * NS_PER_S, int(utc[1].timestamp()) * NS_PER_S,
                f"{calendar.calendar_id}@{calendar.version} session hours")
    if fixture:
        from .fixtures import session_times
        s, e, _ = session_times(day)
        return s, e, FIXTURE_SESSION_BASIS
    raise SessionHoursUnknown(f"{calendar.calendar_id} has no session hours for {day.isoformat()} and the data is not a "
                              "FIXTURE: pass the venue calendar (e.g. XNYS) to place the submission window")


def window_times(window: SubmissionWindow, calendar: BusinessCalendar, day: date, *, fixture: bool) -> WindowTimes:
    open_ns, close_ns, basis = session_bounds(calendar, day, fixture=fixture)
    return WindowTimes(day, open_ns, close_ns, open_ns + window.open_offset_minutes * NS_PER_MIN,
                       close_ns - window.close_offset_minutes * NS_PER_MIN, basis)


def transmission_limit(side: str, model_bound: Fixed, reference: Fixed, collar_bps: int, instrument: Instrument) -> Fixed:
    """The limit sent at transmission: the model bound, tightened to the collar around ``reference``."""
    if not reference.is_positive():
        raise ValueError("the transmission reference price must be positive")
    if side == "BUY":
        cap = (reference * (10_000 + collar_bps)).div(10_000, reference.scale + 4, Rounding.FLOOR)
        cap = cap.to_increment(instrument.tick_for(cap), Rounding.FLOOR)
        return model_bound if model_bound <= cap else cap
    if side == "SELL":
        floor = (reference * (10_000 - collar_bps)).div(10_000, reference.scale + 4, Rounding.CEILING)
        floor = floor.to_increment(instrument.tick_for(floor), Rounding.CEILING)
        return model_bound if model_bound >= floor else floor
    raise ValueError(f"unknown side {side}")
