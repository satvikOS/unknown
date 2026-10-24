"""TEST-ONLY SYNTHETIC fixture series for research tests and the fixture study (ADR-0020: never product code).

Every bar produced here is labelled ``fixture=True`` with a source of the form
``FIXTURE:synthetic/<kind>@v1;seed=<n>``. These are not market data, carry no
information about any real instrument, and must never be presented as such
(DAT-06, TRM-03).

Generation uses Python floats and ``random.Random(seed)`` (statistical
simulation, deterministic per seed), then rounds each price once to whole
cents (``round_half_even``) and each volume to whole shares. From that point
on every number is an exact ``Fixed``.

Kinds
-----
``trend``           log returns with persistent drift regimes (+/-) and Gaussian noise
``mean_reverting``  discrete Ornstein-Uhlenbeck log price around log(start price)
``random_walk``     driftless Gaussian log returns

Session-time convention (fixture only): bars span 14:30-21:00 UTC on each
business day of the calendar and become available 15 minutes after the end
(publication lag). Daylight-saving shifts are deliberately ignored.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

from unknown.domain.calendar import WEEKDAYS_ONLY, BusinessCalendar
from unknown.domain.fixed import Fixed
from unknown.domain.instruments import AssetClass, Instrument, TickRule
from unknown.research.bars import Bar
from unknown.research.common import NS_PER_S

KINDS = ("trend", "mean_reverting", "random_walk")
FIXTURE_START_UTC = time(14, 30)
FIXTURE_END_UTC = time(21, 0)
FIXTURE_PUBLICATION_LAG_S = 15 * 60
GENERATOR_VERSION = "v1"


def session_times(day: date) -> tuple[int, int, int]:
    """(start_ns, end_ns, available_ns) for a fixture session."""
    start = int(datetime.combine(day, FIXTURE_START_UTC, tzinfo=timezone.utc).timestamp()) * NS_PER_S
    end = int(datetime.combine(day, FIXTURE_END_UTC, tzinfo=timezone.utc).timestamp()) * NS_PER_S
    return start, end, end + FIXTURE_PUBLICATION_LAG_S * NS_PER_S


def business_days(start: date, count: int, calendar: BusinessCalendar = WEEKDAYS_ONLY) -> list[date]:
    days = []
    d = start
    while len(days) < count:
        if calendar.is_business_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def fixture_instrument(instrument_id: str, symbol: str | None = None) -> Instrument:
    """A FIXTURE cash-equity instrument (US-style ticks: 0.0001 below 1.00, 0.01 at or above)."""
    return Instrument(
        instrument_id=instrument_id, asset_class=AssetClass.EQUITY, symbol=symbol or instrument_id[-8:],
        name=f"Synthetic fixture {instrument_id} (test only; not a real security)", currency="USD",
        settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1), min_quantity=Fixed(1),
        multiplier=Fixed(1), settlement_days=1,
        tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))),
        attributes={"fixture": True})


def _cents(x: float) -> Fixed:
    return Fixed(max(100, round(x * 100)), 2)  # floor at 1.00 keeps the 0.01 tick regime


def synthetic_bars(kind: str, instrument_id: str, *, start: date, sessions: int, seed: int,
                   start_price: float = 50.0, calendar: BusinessCalendar = WEEKDAYS_ONLY,
                   sigma: float | None = None, mean_volume: float = 1_000_000.0) -> list[Bar]:
    if kind not in KINDS:
        raise ValueError(f"unknown fixture kind {kind!r}; expected one of {KINDS}")
    if sessions < 2:
        raise ValueError("need at least two sessions")
    rng = random.Random(f"{kind}:{instrument_id}:{seed}")
    source = f"FIXTURE:synthetic/{kind}@{GENERATOR_VERSION};seed={seed}"
    days = business_days(start, sessions, calendar)
    log_p0 = math.log(start_price)
    x = log_p0
    drift = 0.0
    regime_left = 0
    vol = sigma if sigma is not None else {"trend": 0.012, "mean_reverting": 0.012, "random_walk": 0.015}[kind]
    prev_close = start_price
    out: list[Bar] = []
    for d in days:
        if kind == "trend":
            if regime_left <= 0:
                drift = rng.choice((0.0025, -0.0020, 0.0018, -0.0015))
                regime_left = rng.randint(60, 160)
            regime_left -= 1
            x = x + drift + rng.gauss(0.0, vol)
        elif kind == "mean_reverting":
            x = x + 0.08 * (log_p0 - x) + rng.gauss(0.0, vol)
        else:
            x = x + rng.gauss(0.0, vol)
        close_f = math.exp(x)
        gap = rng.gauss(0.0, vol * 0.4)
        open_f = prev_close * math.exp(gap)
        hi_f = max(open_f, close_f) * math.exp(abs(rng.gauss(0.0, vol * 0.5)))
        lo_f = min(open_f, close_f) * math.exp(-abs(rng.gauss(0.0, vol * 0.5)))
        o, c, h, lo = _cents(open_f), _cents(close_f), _cents(hi_f), _cents(lo_f)
        h = max(h, o, c)
        lo = min(lo, o, c)
        volume = Fixed(max(1_000, int(round(mean_volume * math.exp(rng.gauss(0.0, 0.35))))))
        s, e, a = session_times(d)
        out.append(Bar(instrument_id, d, s, e, a, o, h, lo, c, volume, "USD", source, True, 1))
        prev_close = close_f
    return out


def manual_bar(instrument_id: str, day: date, o: str, h: str, l: str, c: str, volume: str | int = 1_000_000, *,
               version: int = 1, available_delay_s: int | None = None, source: str = "FIXTURE:manual@v1") -> Bar:
    """Hand-written FIXTURE bar for known-answer tests."""
    s, e, a = session_times(day)
    if available_delay_s is not None:
        a = e + available_delay_s * NS_PER_S
    return Bar(instrument_id, day, s, e, a, Fixed.parse(o), Fixed.parse(h), Fixed.parse(l), Fixed.parse(c),
               Fixed.parse(str(volume)), "USD", source, True, version)


def recipe(kind: str, instrument_id: str, *, start: date, sessions: int, seed: int, start_price: float = 50.0,
           sigma: float | None = None, mean_volume: float = 1_000_000.0) -> dict:
    """Re-generation recipe recorded in manifests so a fixture run can be replayed from scratch.

    Every generator argument that changes the bars is recorded (review fix 2026-10-22: ``sigma`` and
    ``mean_volume`` were not, so a non-default fixture could not be regenerated from its recipe)."""
    return {"generator": f"support.research_fixtures.synthetic_bars@{GENERATOR_VERSION}", "kind": kind,
            "instrument_id": instrument_id, "start": start.isoformat(), "sessions": sessions, "seed": seed,
            "start_price": start_price, "sigma": sigma, "mean_volume": mean_volume,
            "calendar": WEEKDAYS_ONLY.calendar_id + "@" + WEEKDAYS_ONLY.version, "label": "FIXTURE"}


def from_recipe(r: dict) -> list[Bar]:
    if r.get("calendar") != WEEKDAYS_ONLY.calendar_id + "@" + WEEKDAYS_ONLY.version:
        raise ValueError(f"recipe calendar {r.get('calendar')!r} is not available to the fixture generator")
    return synthetic_bars(r["kind"], r["instrument_id"], start=date.fromisoformat(r["start"]), sessions=r["sessions"],
                          seed=r["seed"], start_price=r["start_price"], sigma=r.get("sigma"),
                          mean_volume=r.get("mean_volume", 1_000_000.0))
