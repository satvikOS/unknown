"""IBKR historical bars: request pacing, parsing, completeness and a point-in-time research bars table.

Capability records (docs/capabilities/register.json):

* CAP-MKT-04: half-open bars ``[start, end)``. A vendor bar is complete only if IBKR computed the response after
  ``end``. IBKR computes it at an unknown instant between sending and receipt, so the only safe bound is the *send*
  time: ``complete = end <= min(sent, endDateTime)``. A response that arrives late (IBKR can hold requests) must
  not turn the partial bar it snapshotted into a complete one, and a bar that extends past the request's own
  ``endDateTime`` is cut off by it. Strategies get only complete bars, so an incomplete bar is returned to the
  caller but never stored. Bars of 30 s or less older than six months are
  answered locally with an explicit coverage gap, not an empty success.
* CAP-IBKR-08: IBKR's historical pacing rules are enforced locally before a request is sent. They apply to all
  bar sizes (conservative; IBKR's page heads them "small bars"):
    - no identical request within 15 s;
    - fewer than six requests for the same contract/exchange/tickType within 2 s;
    - at most 60 requests in any 10 minutes;
    - BID_ASK counts twice in every rule;
    - at most 50 simultaneous open historical requests.
  Source: interactivebrokers.github.io/tws-api/historical_limitations.html (fetched 2026-10-22) and CAP-IBKR-08
  analytics ``hist_ok``.
* CAP-MKT-05: adjustment is labelled, never assumed. IBKR states "TRADES data is adjusted for splits, but not
  dividends" and "ADJUSTED_LAST data is adjusted for splits and dividends". MIDPOINT/BID/ASK/BID_ASK adjustment is
  not documented on the fetched pages and is labelled ``unknown``. IBKR offers no unadjusted series, and an
  adjusted series is expressed on the corporate-action basis of its *fetch* time. ``as_of`` therefore flags rows
  whose basis is later than the decision time, and ``require_unadjusted`` refuses them.
* CAP-IBKR-12 and ADR-0015: bars are stored only when the provider registry (``config/providers``) records the
  ``storage`` use for dataset ``ibkr.historical_bars``. No such entry exists on 2026-10-22, so live bars are
  returned to the caller and not stored, with an explicit reason. FIXTURE bars are stored only with
  ``StorageRights.fixture_only()``, and every such row carries ``fixture = 1``.

Point in time: ``available_time = bar_end + settle_delay`` and ``known_from = receipt time``.
``BarStore.as_of(t)`` returns only rows with ``available_time <= t``. With ``knowledge_cutoff_ns`` it returns
only the versions this system held at that instant, the same contract as ``unknown.data.pit``.

Bar times: requests use ``formatDate=2``. For bars smaller than a day IBKR then returns epoch seconds; for daily
bars it returns "yyyyMMdd", and "the date of the bar will correspond to the day on which the bar closes"
(historical_bars.html). An intraday timestamp is taken as the bar *start* (IBKR convention, not stated on the
fetched pages; to verify on paper), so ``end = start + bar size``. That end is never earlier than the real end
(conservative for a short first or last bar of a session). A daily bar ends at the session close of its date for
``use_rth`` (the calendar's regular close or early close). Without RTH it ends at the next local midnight
(conservative).

SDK: EClient.reqHistoricalData client.py:4863, cancelHistoricalData :5073; EWrapper.historicalData wrapper.py:455,
historicalDataEnd :473; BarData common.py:112 (date, open, high, low, close, volume, wap, barCount).

Status (ADR-0001): implemented and tested offline. NOT paper-verified.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...domain.fixed import Fixed
from . import mapping as M
from .marketdata import ContractSpec

PROVIDER = "ibkr"
BAR_SPEC_VERSION = "ibkr.hist_bars@1"
DATASET_ID = "ibkr.historical_bars"
FIXTURE_DATASET_ID = "FIXTURE.ibkr.historical_bars"
FORMAT_DATE = 2                     # epoch seconds for intraday bars; daily bars are always yyyyMMdd
OPEN_END = 2 ** 62
NS = 1_000_000_000

# Bar sizes IBKR lists (historical_bars.html). Weekly and monthly bars are refused: their end time needs a week /
# month session calendar this module does not implement.
BAR_SECONDS = {"1 sec": 1, "5 secs": 5, "10 secs": 10, "15 secs": 15, "30 secs": 30, "1 min": 60, "2 mins": 120,
               "3 mins": 180, "5 mins": 300, "10 mins": 600, "15 mins": 900, "20 mins": 1200, "30 mins": 1800,
               "1 hour": 3600, "2 hours": 7200, "3 hours": 10800, "4 hours": 14400, "8 hours": 28800}
DAILY = "1 day"
SMALL_BAR_MAX_SECONDS = 30          # "Bars whose size is 30 seconds or less older than six months" are unavailable

ADJUSTMENT = {
    "TRADES": ("split_adjusted", "IBKR: TRADES data is adjusted for splits, but not dividends (CAP-MKT-05)"),
    "ADJUSTED_LAST": ("split_and_dividend_adjusted", "IBKR: ADJUSTED_LAST data is adjusted for splits and dividends"),
    "MIDPOINT": ("unknown", "adjustment of MIDPOINT bars is not documented on the fetched IBKR pages"),
    "BID": ("unknown", "adjustment of BID bars is not documented on the fetched IBKR pages"),
    "ASK": ("unknown", "adjustment of ASK bars is not documented on the fetched IBKR pages"),
    "BID_ASK": ("unknown", "adjustment of BID_ASK bars is not documented on the fetched IBKR pages; the four price "
                           "columns are not trade OHLC"),
}

# Local pacing rule constants (historical_limitations.html; CAP-IBKR-08)
IDENTICAL_WINDOW_NS = 15 * NS
SAME_KEY_WINDOW_NS = 2 * NS
SAME_KEY_MAX_WEIGHT = 5             # "six or more ... within two seconds" is a violation -> at most 5
TEN_MINUTE_WINDOW_NS = 600 * NS
TEN_MINUTE_MAX_WEIGHT = 60          # "more than 60 requests within any ten minute period" is a violation
MAX_OPEN_REQUESTS = 50              # "maximum number of simultaneous open historical data requests ... is 50"
HMDS_PACING_BACKOFF_NS = 60 * NS    # after an IBKR-reported historical pacing violation (project choice)

_DURATION = re.compile(r"^([1-9][0-9]{0,4}) ([SDWMY])$")


class BarStorageRefused(PermissionError):
    """Storage of IBKR bars is not permitted by the provider registry (CAP-IBKR-12, ADR-0015)."""


class UnadjustedUnavailable(LookupError):
    """An unadjusted series was required, but the stored series is adjusted or of unknown adjustment."""


# ============================================================================ pacing
@dataclass(frozen=True)
class PacingDecision:
    ok: bool
    rule: str = ""
    retry_after_ns: int = 0


class HistoricalPacer:
    """IBKR historical-data pacing, enforced locally with a monotonic clock (never wall time)."""

    def __init__(self, monotonic_ns: Callable[[], int]):
        self._mono = monotonic_ns
        self._lock = threading.Lock()
        self._sent: deque[tuple[int, tuple, tuple, int]] = deque()   # (t, identical_key, same_key, weight)
        self._open = 0
        self._penalty_until = 0
        self.refused: dict[str, int] = {}

    def _prune(self, now: int) -> None:
        while self._sent and self._sent[0][0] <= now - TEN_MINUTE_WINDOW_NS:
            self._sent.popleft()

    def try_acquire(self, identical_key: tuple, same_key: tuple, weight: int) -> PacingDecision:
        with self._lock:
            now = self._mono()
            self._prune(now)
            decision = self._check(now, identical_key, same_key, weight)
            if decision.ok:
                self._sent.append((now, identical_key, same_key, weight))
                self._open += 1
            else:
                self.refused[decision.rule] = self.refused.get(decision.rule, 0) + 1
            return decision

    def _check(self, now: int, identical_key: tuple, same_key: tuple, weight: int) -> PacingDecision:
        if now < self._penalty_until:
            return PacingDecision(False, "HMDS_PACING_BACKOFF", self._penalty_until - now)
        if self._open >= MAX_OPEN_REQUESTS:
            return PacingDecision(False, "MAX_50_OPEN_REQUESTS", NS)
        identical = [t for t, k, _, _ in self._sent if k == identical_key and t > now - IDENTICAL_WINDOW_NS]
        if identical:
            return PacingDecision(False, "IDENTICAL_WITHIN_15S", identical[0] + IDENTICAL_WINDOW_NS - now)
        same = [(t, w) for t, _, k, w in self._sent if k == same_key and t > now - SAME_KEY_WINDOW_NS]
        if sum(w for _, w in same) + weight > SAME_KEY_MAX_WEIGHT:
            return PacingDecision(False, "SIX_SAME_CONTRACT_EXCHANGE_TICKTYPE_WITHIN_2S",
                                  same[0][0] + SAME_KEY_WINDOW_NS - now)
        total = sum(w for _, _, _, w in self._sent)
        if total + weight > TEN_MINUTE_MAX_WEIGHT:
            # earliest instant at which enough weight has aged out of the 600 s window
            need, freed, retry = total + weight - TEN_MINUTE_MAX_WEIGHT, 0, 0
            for t, _, _, w in self._sent:
                freed += w
                if freed >= need:
                    retry = t + TEN_MINUTE_WINDOW_NS - now
                    break
            return PacingDecision(False, "MORE_THAN_60_IN_10_MINUTES", max(1, retry))
        return PacingDecision(True)

    def release(self) -> None:
        with self._lock:
            self._open = max(0, self._open - 1)

    def penalize(self, ns: int) -> None:
        with self._lock:
            self._penalty_until = max(self._penalty_until, self._mono() + ns)

    def weighted_in_window(self) -> int:
        with self._lock:
            self._prune(self._mono())
            return sum(w for _, _, _, w in self._sent)


# ============================================================================ request
def parse_duration(text: str) -> tuple[int, str]:
    m = _DURATION.match(text or "")
    if not m:
        raise ValueError(f"duration {text!r} must be '<integer> <S|D|W|M|Y>' (IBKR durationStr)")
    return int(m.group(1)), m.group(2)


def earliest_start_ns(end_ns: int, duration: str) -> int:
    """Conservative (earliest) estimate of the first bar a request can return.

    'D' may count trading days, so weekends and up to four holidays are added. Used only for the local six-month
    coverage check, where an earlier estimate is the safe direction."""
    n, unit = parse_duration(duration)
    days = {"S": 0, "D": n + (n // 5) * 2 + 4, "W": 7 * n, "M": 31 * n, "Y": 366 * n}[unit]
    return end_ns - (n * NS if unit == "S" else days * 86_400 * NS)


def months_before(ns: int, months: int) -> int:
    d = datetime.fromtimestamp(ns // NS, tz=timezone.utc)
    y, m = divmod(d.month - 1 - months, 12)
    year, month = d.year + y, m + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31,
                      30, 31, 30, 31][month - 1])
    return int(d.replace(year=year, month=month, day=day).timestamp()) * NS


@dataclass(frozen=True)
class HistoricalRequest:
    instrument_id: str
    contract: ContractSpec
    bar_size: str
    duration: str
    what_to_show: str = "TRADES"
    use_rth: bool = True
    end_ns: int | None = None          # None = up to now (endDateTime ""), which can include a partial bar
    timezone: str = "America/New_York"  # listing zone: dates of daily bars and the session rule

    def problems(self) -> list[str]:
        out = list(self.contract.problems())
        if self.bar_size not in BAR_SECONDS and self.bar_size != DAILY:
            out.append(f"bar size {self.bar_size!r} is not supported (weekly/monthly bars need a period calendar)")
        if self.what_to_show not in ADJUSTMENT:
            out.append(f"whatToShow {self.what_to_show!r} is not supported (TRADES, ADJUSTED_LAST, MIDPOINT, BID, "
                       "ASK, BID_ASK)")
        if self.what_to_show == "ADJUSTED_LAST" and self.end_ns is not None:
            out.append("ADJUSTED_LAST is requested only up to now (endDateTime empty) in this module")
        try:
            parse_duration(self.duration)
        except ValueError as exc:
            out.append(str(exc))
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            out.append(f"timezone {self.timezone!r} is not an IANA zone")
        if not self.instrument_id:
            out.append("instrument_id is required")
        return out

    @property
    def daily(self) -> bool:
        return self.bar_size == DAILY

    @property
    def weight(self) -> int:
        return 2 if self.what_to_show == "BID_ASK" else 1   # "each request is counted twice"

    def end_date_time(self) -> str:
        """IBKR endDateTime "yyyymmdd HH:mm:ss ttt" (client.py docstring) with ttt = UTC; "" = now."""
        if self.end_ns is None:
            return ""
        return datetime.fromtimestamp(self.end_ns // NS, tz=timezone.utc).strftime("%Y%m%d %H:%M:%S") + " UTC"

    def identical_key(self) -> tuple:
        c = self.contract
        return (c.con_id, c.symbol, c.primary_exchange, c.exchange, c.currency, self.end_date_time(), self.duration,
                self.bar_size, self.what_to_show, int(self.use_rth))

    def same_key(self) -> tuple:
        c = self.contract
        return (c.con_id or f"{c.symbol}@{c.primary_exchange}", c.exchange, self.what_to_show)

    def to_json(self) -> dict:
        return {"instrument_id": self.instrument_id, "contract": self.contract.to_json(), "bar_size": self.bar_size,
                "duration": self.duration, "what_to_show": self.what_to_show, "use_rth": self.use_rth,
                "end_ns": self.end_ns, "end_date_time": self.end_date_time(), "format_date": FORMAT_DATE,
                "timezone": self.timezone, "bar_spec_version": BAR_SPEC_VERSION}


# ============================================================================ session calendar
@dataclass(frozen=True)
class SessionRule:
    """Regular close per date for daily-bar end times (calendar file, e.g. config/calendars/XNYS.json)."""
    calendar_id: str
    version: str
    timezone: str
    regular_close: time
    early_closes: dict = field(default_factory=dict)     # date -> time
    holidays: frozenset = frozenset()
    valid_from: date | None = None
    valid_through: date | None = None
    source: str = ""

    @classmethod
    def from_calendar_file(cls, path: Path | str) -> "SessionRule":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        close = d["regular_session"]["close"]
        return cls(calendar_id=d["calendar_id"], version=d["version"], timezone=d["timezone"],
                   regular_close=time.fromisoformat(close),
                   early_closes={date.fromisoformat(e[0]): time.fromisoformat(e[1]) for e in d.get("early_closes", [])},
                   holidays=frozenset(date.fromisoformat(h[0]) for h in d.get("holidays", [])),
                   valid_from=date.fromisoformat(d["valid_from"]), valid_through=date.fromisoformat(d["valid_through"]),
                   source=d.get("source", ""))

    def close_ns(self, day: date) -> tuple[int | None, str]:
        """(close instant, note); None when the calendar has no authoritative data for the day."""
        if (self.valid_from and day < self.valid_from) or (self.valid_through and day > self.valid_through):
            return None, f"{self.calendar_id} {self.version} has no data for {day}"
        note = ""
        if day.weekday() >= 5 or day in self.holidays:
            note = f"{day} is not a session day in {self.calendar_id} {self.version}"
        t = self.early_closes.get(day, self.regular_close)
        if day in self.early_closes:
            note = f"early close {t.isoformat('minutes')}"
        local = datetime.combine(day, t, tzinfo=ZoneInfo(self.timezone))
        return int(local.timestamp()) * NS, note


def _next_midnight_ns(day: date, tz: str) -> int:
    return int(datetime.combine(day + timedelta(days=1), time(0), tzinfo=ZoneInfo(tz)).timestamp()) * NS


def _midnight_ns(day: date, tz: str) -> int:
    return int(datetime.combine(day, time(0), tzinfo=ZoneInfo(tz)).timestamp()) * NS


# ============================================================================ bars
@dataclass
class ParsedBar:
    bar_start_ns: int
    bar_end_ns: int
    bar_date: str | None
    available_ns: int
    complete: bool
    open: Fixed | None
    high: Fixed | None
    low: Fixed | None
    close: Fixed | None
    volume: Fixed | None
    wap: Fixed | None
    bar_count: int | None
    raw: dict
    quality: list = field(default_factory=list)
    availability_rule: str = ""

    def values_key(self) -> tuple:
        return tuple(None if v is None else v.to_str() for v in (self.open, self.high, self.low, self.close,
                                                                    self.volume, self.wap)) + (self.bar_count,)

    def to_json(self) -> dict:
        s = lambda v: None if v is None else v.to_str()   # noqa: E731
        return {"bar_start_ns": self.bar_start_ns, "bar_end_ns": self.bar_end_ns, "bar_date": self.bar_date,
                "available_time_ns": self.available_ns, "complete": self.complete, "open": s(self.open),
                "high": s(self.high), "low": s(self.low), "close": s(self.close), "volume": s(self.volume),
                "wap": s(self.wap), "bar_count": self.bar_count, "quality": list(self.quality),
                "availability_rule": self.availability_rule}


def _price(value, quality: list, name: str) -> Fixed | None:
    try:
        fx = M.float_to_fixed(value) if not isinstance(value, Decimal) else M.decimal_to_fixed(value)
    except (TypeError, ValueError):
        quality.append(f"{name}_unparseable")
        return None
    if fx is None:
        return None
    if not fx.is_positive():
        quality.append(f"{name}_non_positive")   # IBKR uses -1 / 0 for "not available"; never stored as a price
        return None
    return fx


def _amount(value, quality: list, name: str) -> Fixed | None:
    try:
        fx = M.decimal_to_fixed(value) if isinstance(value, (Decimal, str)) else M.float_to_fixed(value)
    except (TypeError, ValueError):
        quality.append(f"{name}_unparseable")
        return None
    if fx is not None and fx.is_negative():
        return None                               # -1 = not applicable for this whatToShow
    return fx


_LOCAL_TIME = re.compile(r"^(\d{8})\s+(\d{2}):(\d{2}):(\d{2})(?:\s+(\S+))?$")


def parse_bar(raw: dict, req: HistoricalRequest, *, receipt_ns: int, settle_delay_ns: int,
              session_rule: SessionRule | None, host_time_zone: str | None = None,
              requested_ns: int | None = None) -> ParsedBar:
    """One ``BarData`` (already copied to a dict) to a ParsedBar with start, end, availability and completeness.

    Completeness (CAP-MKT-04): ``complete = end <= min(requested_ns or receipt_ns, receipt_ns, req.end_ns)``.
    ``requested_ns`` is when the request was sent: IBKR computed the response no earlier than that, and possibly
    long before ``receipt_ns``. A bar running past ``req.end_ns`` is truncated by the request and never complete."""
    quality: list[str] = []
    text = str(raw.get("date", "")).strip()
    tz = req.timezone
    bar_date = None
    if req.daily:
        if not (len(text) == 8 and text.isdigit()):
            raise ValueError(f"daily bar date {text!r} is not yyyyMMdd")
        day = date(int(text[:4]), int(text[4:6]), int(text[6:]))
        bar_date = day.isoformat()
        start = _midnight_ns(day, tz)
        if req.use_rth:
            if session_rule is None:
                raise ValueError("a session rule is required for RTH daily bars")
            close, note = session_rule.close_ns(day)
            if close is None:
                end = _next_midnight_ns(day, tz)
                quality.append("calendar_horizon")
                rule = f"daily RTH bar: {note}; end = next local midnight (conservative)"
            else:
                end = close
                if note:
                    quality.append(note.replace(" ", "_"))
                rule = (f"daily RTH bar dated by its closing day (IBKR): end = {session_rule.calendar_id} "
                        f"{session_rule.version} close{(' (' + note + ')') if note else ''}")
        else:
            end = _next_midnight_ns(day, tz)
            rule = "daily all-hours bar: end = next local midnight in the listing zone (conservative)"
    else:
        seconds = BAR_SECONDS[req.bar_size]
        if text.isdigit() and len(text) > 8:
            start = int(text) * NS
            source = "epoch seconds (formatDate=2)"
        else:
            m = _LOCAL_TIME.match(text)
            if not m:
                raise ValueError(f"bar time {text!r} is neither epoch seconds nor 'yyyymmdd hh:mm:ss [tz]'")
            d8, hh, mm, ss, zone = m.groups()
            zone_name = zone or host_time_zone
            if not zone_name:
                raise ValueError(f"bar time {text!r} has no zone and no host time zone is configured")
            if not zone:
                quality.append("zone_assumed_host")
            local = datetime(int(d8[:4]), int(d8[4:6]), int(d8[6:]), int(hh), int(mm), int(ss),
                             tzinfo=timezone.utc if zone_name.upper() in ("UTC", "GMT") else ZoneInfo(zone_name))
            start = int(local.timestamp()) * NS
            source = f"local time ({zone_name})"
        end = start + seconds * NS
        rule = (f"intraday bar stamped at its start ({source}; IBKR convention, verify on paper): "
                f"end = start + {req.bar_size}")
    available = end + settle_delay_ns
    rule += f"; available = end + settle {settle_delay_ns / NS:g} s"
    complete_bound = receipt_ns if requested_ns is None else min(receipt_ns, requested_ns)
    if req.end_ns is not None:
        complete_bound = min(complete_bound, req.end_ns)
    bar_count = raw.get("barCount")
    bar_count = int(bar_count) if isinstance(bar_count, int) and not isinstance(bar_count, bool) and bar_count >= 0 \
        else None
    return ParsedBar(
        bar_start_ns=start, bar_end_ns=end, bar_date=bar_date, available_ns=available, complete=end <= complete_bound,
        open=_price(raw.get("open"), quality, "open"), high=_price(raw.get("high"), quality, "high"),
        low=_price(raw.get("low"), quality, "low"), close=_price(raw.get("close"), quality, "close"),
        volume=_amount(raw.get("volume"), quality, "volume"), wap=_amount(raw.get("wap"), quality, "wap"),
        bar_count=bar_count, raw={k: repr(v) for k, v in raw.items()}, quality=quality, availability_rule=rule)


# ============================================================================ storage rights
@dataclass(frozen=True)
class StorageRights:
    permitted: bool
    dataset_id: str
    reason: str
    evidence: str = ""
    fixture: bool = False

    @classmethod
    def from_registry(cls, directory: Path | None = None, dataset_id: str = DATASET_ID) -> "StorageRights":
        """ADR-0015: 'A dataset without recorded rights stays out of production ingestion.'"""
        from ...data.registry import RegistryError, dataset, load_registry
        try:
            ds = dataset(load_registry(directory), dataset_id)
        except RegistryError as exc:
            return cls(False, dataset_id, f"UNAUTHORIZED: {exc}; IBKR storage rights are not verified "
                                          "(CAP-IBKR-12: treat storage as not permitted until verified)")
        except (ValueError, KeyError, TypeError, OSError) as exc:   # an unreadable registry fails closed
            return cls(False, dataset_id, f"UNAUTHORIZED: provider registry unreadable ({type(exc).__name__}: "
                                          f"{exc}); storage refused")
        if not ds.permits("storage"):
            return cls(False, dataset_id, f"UNAUTHORIZED: dataset {dataset_id} does not grant 'storage'")
        return cls(True, dataset_id, "registry grants storage", f"{ds.rights_source} (verified {ds.rights_verified_on})")

    @classmethod
    def fixture_only(cls) -> "StorageRights":
        return cls(True, FIXTURE_DATASET_ID, "FIXTURE: synthetic test data, never market data", "test fixture", True)


# ============================================================================ store
BAR_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS mkt_bar_request (
  request_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
  request TEXT NOT NULL, sent_ns INTEGER, receipt_ns INTEGER NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
  ibkr_start TEXT, ibkr_end TEXT, bars_received INTEGER NOT NULL, fixture INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mkt_bar (
  bar_id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL, dataset_id TEXT NOT NULL, instrument_id TEXT NOT NULL, bar_spec_version TEXT NOT NULL,
  bar_size TEXT NOT NULL, what_to_show TEXT NOT NULL, use_rth INTEGER NOT NULL, timezone TEXT NOT NULL,
  adjustment TEXT NOT NULL, adjustment_note TEXT NOT NULL,
  bar_start_ns INTEGER NOT NULL, bar_end_ns INTEGER NOT NULL, bar_date TEXT,
  open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, wap TEXT, bar_count INTEGER, currency TEXT NOT NULL,
  receipt_time_ns INTEGER NOT NULL, available_time_ns INTEGER NOT NULL, availability_rule TEXT NOT NULL,
  known_from_ns INTEGER NOT NULL, known_to_ns INTEGER NOT NULL, supersedes INTEGER REFERENCES mkt_bar(bar_id),
  request_id TEXT NOT NULL, raw TEXT NOT NULL, quality TEXT NOT NULL, fixture INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mkt_bar_key ON mkt_bar(instrument_id, bar_size, what_to_show, use_rth, bar_start_ns,
  known_to_ns);
CREATE INDEX IF NOT EXISTS mkt_bar_available ON mkt_bar(instrument_id, available_time_ns);
CREATE TABLE IF NOT EXISTS mkt_bar_gap (
  gap_id INTEGER PRIMARY KEY, provider TEXT NOT NULL, instrument_id TEXT NOT NULL, bar_size TEXT NOT NULL,
  what_to_show TEXT NOT NULL, use_rth INTEGER NOT NULL, start_ns INTEGER, end_ns INTEGER, reason TEXT NOT NULL,
  detail TEXT NOT NULL, recorded_ns INTEGER NOT NULL, request_id TEXT NOT NULL, fixture INTEGER NOT NULL
);
"""


class BarStore:
    """Research bars table (ADR-0015: 'Bars and ticks get their own tables keyed by instrument, bar specification
    version and time'). Lives in the research database (``<UNKNOWN_HOME>/research/research.sqlite``) beside the
    observation tables, and is never read by the money core."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(BAR_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    def record_request(self, result: "HistoricalResult", outcome: str, detail: str, fixture: bool) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO mkt_bar_request VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (result.request_id, PROVIDER, result.dataset_id or DATASET_ID, result.request.instrument_id,
                 json.dumps(result.request.to_json(), sort_keys=True), result.sent_ns, result.receipt_ns, outcome,
                 detail, result.ibkr_start, result.ibkr_end, len(result.bars), int(fixture)))

    def record_gap(self, req: HistoricalRequest, *, start_ns: int | None, end_ns: int | None, reason: str,
                   detail: str, recorded_ns: int, request_id: str, fixture: bool = False) -> int:
        """A coverage gap is metadata about what IBKR cannot supply, not market data, so it is always recorded."""
        with self._lock:
            c = self.conn.execute(
                "INSERT INTO mkt_bar_gap(provider, instrument_id, bar_size, what_to_show, use_rth, start_ns, end_ns, "
                "reason, detail, recorded_ns, request_id, fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (PROVIDER, req.instrument_id, req.bar_size, req.what_to_show, int(req.use_rth), start_ns, end_ns,
                 reason, detail, recorded_ns, request_id, int(fixture)))
            return c.lastrowid

    def store(self, result: "HistoricalResult", rights: StorageRights) -> dict:
        """Store the complete bars of one response. Returns counts; raises BarStorageRefused without rights."""
        if not rights.permitted:
            raise BarStorageRefused(rights.reason)
        req = result.request
        adjustment, note = ADJUSTMENT[req.what_to_show]
        counts = {"new": 0, "unchanged": 0, "revised": 0, "incomplete_not_stored": 0}
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for bar in result.bars:
                    if not bar.complete:
                        counts["incomplete_not_stored"] += 1
                        continue
                    cur = self.conn.execute(
                        "SELECT bar_id, open, high, low, close, volume, wap, bar_count, available_time_ns FROM mkt_bar "
                        "WHERE dataset_id=? AND instrument_id=? AND bar_spec_version=? AND bar_size=? AND what_to_show=? "
                        "AND use_rth=? AND bar_start_ns=? AND known_to_ns=?",
                        (rights.dataset_id, req.instrument_id, BAR_SPEC_VERSION, req.bar_size, req.what_to_show,
                         int(req.use_rth), bar.bar_start_ns, OPEN_END)).fetchone()
                    if cur is not None:
                        stored = (cur["open"], cur["high"], cur["low"], cur["close"], cur["volume"], cur["wap"],
                                  cur["bar_count"])
                        if stored == bar.values_key() and cur["available_time_ns"] == bar.available_ns:
                            counts["unchanged"] += 1
                            continue
                        self.conn.execute("UPDATE mkt_bar SET known_to_ns=? WHERE bar_id=?",
                                          (result.receipt_ns, cur["bar_id"]))
                    quality = list(bar.quality) + (["revised"] if cur is not None else [])
                    s = lambda v: None if v is None else v.to_str()   # noqa: E731
                    self.conn.execute(
                        "INSERT INTO mkt_bar(provider, dataset_id, instrument_id, bar_spec_version, bar_size, "
                        "what_to_show, use_rth, timezone, adjustment, adjustment_note, bar_start_ns, bar_end_ns, "
                        "bar_date, open, high, low, close, volume, wap, bar_count, currency, receipt_time_ns, "
                        "available_time_ns, availability_rule, known_from_ns, known_to_ns, supersedes, request_id, raw, "
                        "quality, fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (PROVIDER, rights.dataset_id, req.instrument_id, BAR_SPEC_VERSION, req.bar_size,
                         req.what_to_show, int(req.use_rth), req.timezone, adjustment, note, bar.bar_start_ns,
                         bar.bar_end_ns, bar.bar_date, s(bar.open), s(bar.high), s(bar.low), s(bar.close),
                         s(bar.volume), s(bar.wap), bar.bar_count, req.contract.currency, result.receipt_ns,
                         bar.available_ns, bar.availability_rule, result.receipt_ns, OPEN_END,
                         cur["bar_id"] if cur is not None else None, result.request_id,
                         json.dumps(bar.raw, sort_keys=True), json.dumps(quality), int(rights.fixture)))
                    counts["revised" if cur is not None else "new"] += 1
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return counts

    def as_of(self, instrument_id: str, *, bar_size: str, what_to_show: str, use_rth: bool, t_ns: int,
              start_ns: int | None = None, end_ns: int | None = None, knowledge_cutoff_ns: int | None = None,
              include_fixture: bool = False, require_unadjusted: bool = False, limit: int = 10_000) -> list[dict]:
        """Bars usable for a decision at ``t_ns``: ``available_time <= t`` (never later information).

        Research mode uses the current version of each bar. Audit mode (``knowledge_cutoff_ns``) uses only versions
        this system held at that instant. FIXTURE rows are excluded unless ``include_fixture``."""
        sql = ("SELECT * FROM mkt_bar WHERE instrument_id=? AND bar_size=? AND what_to_show=? AND use_rth=? "
               "AND available_time_ns<=?")
        params: list = [instrument_id, bar_size, what_to_show, int(use_rth), t_ns]
        if knowledge_cutoff_ns is None:
            sql += " AND known_to_ns=?"
            params.append(OPEN_END)
        else:
            sql += " AND known_from_ns<=? AND known_to_ns>?"
            params += [knowledge_cutoff_ns, knowledge_cutoff_ns]
        if not include_fixture:
            sql += " AND fixture=0"
        if start_ns is not None:
            sql += " AND bar_start_ns>=?"
            params.append(start_ns)
        if end_ns is not None:
            sql += " AND bar_start_ns<?"
            params.append(end_ns)
        sql += " ORDER BY bar_start_ns, known_from_ns LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(sql, tuple(params))]
        out = []
        for r in rows:
            r["adjustment_basis_ns"] = r["known_from_ns"]
            r["adjustment_basis_after_decision"] = r["adjustment"] != "unadjusted" and r["known_from_ns"] > t_ns
            r["quality"] = json.loads(r["quality"])
            r["raw"] = json.loads(r["raw"])
            out.append(r)
        if require_unadjusted and any(r["adjustment"] != "unadjusted" for r in out):
            raise UnadjustedUnavailable(f"{instrument_id} {what_to_show}: IBKR supplies only adjusted or "
                                        "unknown-adjustment series; build raw prices from a licensed source (CAP-MKT-05)")
        return out

    def gaps(self, instrument_id: str | None = None) -> list[dict]:
        with self._lock:
            if instrument_id:
                return [dict(r) for r in self.conn.execute("SELECT * FROM mkt_bar_gap WHERE instrument_id=? ORDER BY "
                                                           "gap_id", (instrument_id,))]
            return [dict(r) for r in self.conn.execute("SELECT * FROM mkt_bar_gap ORDER BY gap_id")]


# ============================================================================ client
@dataclass
class HistoricalResult:
    request: HistoricalRequest
    request_id: str
    outcome: str          # COMPLETE | INCOMPLETE | NO_DATA | NO_SECURITY_DEFINITION | UNAUTHORIZED | PACING_VIOLATION |
    #                       ERROR | REFUSED | PACING_REFUSED | COVERAGE_GAP | NOT_READY
    detail: str
    bars: list = field(default_factory=list)
    sent_ns: int | None = None
    receipt_ns: int = 0
    ibkr_start: str | None = None
    ibkr_end: str | None = None
    errors: list = field(default_factory=list)
    retry_after_ns: int = 0
    dataset_id: str | None = None
    stored: dict | None = None
    not_stored_reason: str | None = None
    gap_id: int | None = None

    def complete_bars(self) -> list[ParsedBar]:
        return [b for b in self.bars if b.complete]

    def to_json(self) -> dict:
        return {"request": self.request.to_json(), "request_id": self.request_id, "outcome": self.outcome,
                "detail": self.detail, "bars": [b.to_json() for b in self.bars], "sent_ns": self.sent_ns,
                "receipt_ns": self.receipt_ns, "ibkr_start": self.ibkr_start, "ibkr_end": self.ibkr_end,
                "errors": self.errors, "retry_after_ns": self.retry_after_ns, "stored": self.stored,
                "not_stored_reason": self.not_stored_reason, "gap_id": self.gap_id}


class _HistHandler:
    def __init__(self):
        self.rows: list[dict] = []
        self.end = False
        self.start_str = None
        self.end_str = None
        self.error: tuple[int, str] | None = None
        self.errors: list[dict] = []

    def on_historical_data(self, bar) -> None:
        self.rows.append({"date": getattr(bar, "date", ""), "open": getattr(bar, "open", None),
                          "high": getattr(bar, "high", None), "low": getattr(bar, "low", None),
                          "close": getattr(bar, "close", None), "volume": getattr(bar, "volume", None),
                          "wap": getattr(bar, "wap", None), "barCount": getattr(bar, "barCount", None)})

    def on_historical_end(self, start: str, end: str) -> None:
        self.end, self.start_str, self.end_str = True, start, end

    def on_error(self, code: int, text: str) -> None:
        self.errors.append({"code": code, "text": text})
        if 2100 <= code < 2200:
            return          # TWS warning range (e.g. farm notices): recorded, never a failure of the request
        self.error = (code, text)


def classify_hmds_error(code: int, text: str) -> str:
    """Error 162 'Historical market data Service error message' carries its cause in the text (raw text kept)."""
    low = text.lower()
    if code == 162 and "pacing violation" in low:
        return "PACING_VIOLATION"
    if code == 162 and "no data" in low:
        return "NO_DATA"
    if "permission" in low or code in (354, 10089, 10186):
        return "UNAUTHORIZED"
    if code == 200:
        return "NO_SECURITY_DEFINITION"
    return "ERROR"


class HistoryClient:
    """reqHistoricalData through an ``IbkrMarketDataSession`` with local pacing, parsing and optional storage."""

    def __init__(self, session, *, store: BarStore | None = None, rights: StorageRights | None = None,
                 pacer: HistoricalPacer | None = None, settle_delay_ns: int = 0,
                 session_rules: dict[str, SessionRule] | None = None, timeout_s: float | None = None):
        if type(settle_delay_ns) is not int or settle_delay_ns < 0:
            raise ValueError("settle_delay_ns must be a non-negative int")
        self._session = session
        self._store = store
        self._rights = rights
        if pacer is None:
            # One pacer per session: IBKR's limits are per connection/username, so two clients must not each get them.
            pacer = getattr(session, "historical_pacer", None)
            if pacer is None:
                pacer = HistoricalPacer(session.clock.monotonic_ns)
                if hasattr(session, "historical_pacer"):
                    session.historical_pacer = pacer
        self._pacer = pacer
        self._settle = settle_delay_ns
        self._rules = dict(session_rules or {})
        self._timeout = timeout_s

    @property
    def pacer(self) -> HistoricalPacer:
        return self._pacer

    def fetch(self, req: HistoricalRequest) -> HistoricalResult:
        clock = self._session.clock
        now = clock.now_ns()
        request_id = f"hist-{now}-{uuid.uuid4().hex[:12]}"
        result = HistoricalResult(req, request_id, "REFUSED", "", receipt_ns=now)
        problems = req.problems()
        rule = self._rules.get(req.timezone)
        if req.daily and req.use_rth and rule is None:
            problems.append(f"no session rule for {req.timezone}: the end of an RTH daily bar is unknown")
        if problems:
            result.detail = "REFUSED: " + "; ".join(problems)
            return self._finish(result)
        end_ns = req.end_ns if req.end_ns is not None else now
        if not req.daily and BAR_SECONDS[req.bar_size] <= SMALL_BAR_MAX_SECONDS:
            start = earliest_start_ns(end_ns, req.duration)
            horizon = months_before(now, 6)
            if start < horizon:
                result.outcome = "COVERAGE_GAP"
                result.detail = (f"IBKR_UNAVAILABLE: bars of {req.bar_size} older than six months are not available "
                                 "(IBKR historical data limitations); not requested")
                if self._store is not None:
                    result.gap_id = self._store.record_gap(req, start_ns=start, end_ns=min(horizon, end_ns),
                                                           reason="SMALL_BARS_OLDER_THAN_SIX_MONTHS",
                                                           detail=result.detail, recorded_ns=now,
                                                           request_id=request_id,
                                                           fixture=bool(self._rights and self._rights.fixture))
                return self._finish(result)
        ready = self._session.ready_reason()
        if ready:
            result.outcome, result.detail = "NOT_READY", f"NOT_READY: {ready}"
            return self._finish(result)
        decision = self._pacer.try_acquire(req.identical_key(), req.same_key(), req.weight)
        if not decision.ok:
            result.outcome = "PACING_REFUSED"
            result.retry_after_ns = decision.retry_after_ns
            result.detail = (f"PACING_REFUSED: {decision.rule}; retry after {decision.retry_after_ns / NS:.3f} s "
                             "(IBKR historical pacing enforced locally, CAP-IBKR-08)")
            return self._finish(result)
        handler = _HistHandler()
        req_id = self._session.new_req_id()
        self._session.register(req_id, handler)
        try:
            contract = req.contract.to_sdk(self._session.sdk.Contract)
            result.sent_ns = clock.now_ns()
            err = self._session.request("query", "reqHistoricalData", req_id, contract, req.end_date_time(),
                                        req.duration, req.bar_size, req.what_to_show, 1 if req.use_rth else 0,
                                        FORMAT_DATE, False, [])
            if err:
                # request() refusals are local: nothing reached IBKR.
                result.outcome = next((o for o in ("PACING_REFUSED", "NOT_READY") if err.startswith(o)), "ERROR")
                result.detail = err
                return self._finish(result)
            timeout = self._timeout if self._timeout is not None else self._session.config.request_timeout_s
            done = self._session.wait(lambda: handler.end or handler.error is not None, timeout)
            if not done and not handler.end:
                self._session.request("cancel", "cancelHistoricalData", req_id)
        finally:
            self._session.unregister(req_id)
            self._pacer.release()
        result.receipt_ns = clock.now_ns()
        result.ibkr_start, result.ibkr_end = handler.start_str, handler.end_str
        result.errors = handler.errors
        if handler.error is not None and not handler.end:
            code, text = handler.error
            kind = classify_hmds_error(code, text)
            if kind == "PACING_VIOLATION":
                self._pacer.penalize(HMDS_PACING_BACKOFF_NS)
            result.outcome = {"NO_DATA": "NO_DATA", "UNAUTHORIZED": "UNAUTHORIZED",
                              "PACING_VIOLATION": "PACING_VIOLATION",
                              "NO_SECURITY_DEFINITION": "NO_SECURITY_DEFINITION"}.get(kind, "ERROR")
            result.detail = f"IBKR {code} ({kind}): {text}"
            if kind == "NO_SECURITY_DEFINITION":
                # CAP-MKT-05: IBKR has no data for securities no longer trading. Recording the gap keeps a delisted
                # instrument distinguishable from one never fetched (no silent survivorship exclusion).
                result.detail += ("; the security may no longer trade (IBKR provides no data for it: survivorship "
                                  "coverage gap, CAP-MKT-05) or the contract specification is wrong")
            gap_reason = {"NO_DATA": "IBKR_NO_DATA", "NO_SECURITY_DEFINITION": "IBKR_NO_SECURITY_DEFINITION"}.get(kind)
            if gap_reason and self._store is not None:
                result.gap_id = self._store.record_gap(req, start_ns=None, end_ns=req.end_ns, reason=gap_reason,
                                                       detail=result.detail, recorded_ns=result.receipt_ns,
                                                       request_id=request_id,
                                                       fixture=bool(self._rights and self._rights.fixture))
            return self._finish(result)
        host_tz = getattr(self._session.config, "host_time_zone", None)
        parse_problems = []
        for row in handler.rows:
            try:
                result.bars.append(parse_bar(row, req, receipt_ns=result.receipt_ns, settle_delay_ns=self._settle,
                                             session_rule=rule, host_time_zone=host_tz,
                                             requested_ns=result.sent_ns))
            except (ValueError, KeyError) as exc:
                parse_problems.append(f"{row.get('date')!r}: {exc}")
        if not handler.end:
            result.outcome = "INCOMPLETE"
            result.detail = "historicalDataEnd not received within the timeout; request cancelled"
        elif parse_problems:
            result.outcome = "INCOMPLETE"
            result.detail = "unparseable bars held back: " + "; ".join(parse_problems[:5])
        else:
            result.outcome = "COMPLETE"
            partial = sum(1 for b in result.bars if not b.complete)
            result.detail = f"{len(result.bars)} bars" + (f"; {partial} incomplete (bar end after receipt)"
                                                          if partial else "")
        return self._finish(result)

    def _finish(self, result: HistoricalResult) -> HistoricalResult:
        store, rights = self._store, self._rights
        if store is None:
            result.not_stored_reason = "no bar store configured"
            return result
        fixture = bool(rights and rights.fixture)
        if result.outcome in ("COMPLETE", "INCOMPLETE") and result.bars:
            if rights is None or not rights.permitted:
                result.not_stored_reason = rights.reason if rights is not None else \
                    "UNAUTHORIZED: no storage rights decision supplied (CAP-IBKR-12)"
            else:
                result.dataset_id = rights.dataset_id
                result.stored = store.store(result, rights)
        result.dataset_id = result.dataset_id or (rights.dataset_id if rights else DATASET_ID)
        detail = result.detail + (f"; NOT_STORED: {result.not_stored_reason}" if result.not_stored_reason else "")
        store.record_request(result, result.outcome, detail, fixture)
        return result
