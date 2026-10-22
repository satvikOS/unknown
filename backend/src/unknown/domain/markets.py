"""Venue registry: where a listing trades and how it settles (US, India, EU).

The registry is versioned data (``config/markets/venues.json``) joined to the
sourced calendars in ``config/calendars``. For each venue it records the ISO
10383 MIC, country, trading currency, time zone, the trading calendar, the
calendar on which settlement days are counted, and the settlement cycle *by
trade date* (the EU moves from T+2 to T+1 for trades on or after 2027-10-11).
Every settlement rule and every lot/tick convention cites a primary source;
where no primary source was retrieved the field is ``None`` and nothing is
assumed.

The registry is reference data. It grants no trading eligibility: which
venues the desk may trade is an owner policy decision behind release gates
(ADR-0009). Settlement dates computed here are *expectations*; settlement is
confirmed only from broker evidence (ADR-0006).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar import BusinessCalendar, load_calendar
from .instruments import Venue
from .money import currency

REPO = Path(__file__).resolve().parents[4]
DEFAULT_VENUES_PATH = REPO / "config" / "markets" / "venues.json"
DEFAULT_CALENDARS_DIR = REPO / "config" / "calendars"

_MIC = re.compile(r"^[A-Z0-9]{4}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_URL = re.compile(r"^https?://\S+$")


class UnknownVenue(KeyError):
    pass


class NoSettlementRule(LookupError):
    """No sourced settlement cycle covers this trade date: the answer is unknown, not a default."""


class NotATradingDay(ValueError):
    pass


class SettlementCalendarUnavailable(LookupError):
    pass


@dataclass(frozen=True)
class SourceRef:
    key: str
    url: str
    retrieved_on: date
    title: str = ""
    quote: str = ""


@dataclass(frozen=True)
class SettlementCycle:
    from_trade_date: date
    business_days: int
    sources: tuple[SourceRef, ...]
    statement: str = ""


@dataclass(frozen=True)
class Convention:
    """A lot or tick convention stated by a primary source (the rule, not invented values)."""
    rule: str
    statement: str
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True)
class VenueProfile:
    mic: str
    operating_mic: str
    segment_mic: str | None
    name: str
    country: str
    currency: str
    timezone: str
    trading_calendar_id: str
    settlement_calendar_id: str | None
    settlement_cycles: tuple[SettlementCycle, ...]
    lot: Convention | None = None
    tick: Convention | None = None
    provider_codes: dict = field(default_factory=dict)
    sources: tuple[SourceRef, ...] = ()
    notes: tuple[str, ...] = ()

    def settlement_cycle(self, trade_date: date) -> SettlementCycle:
        chosen = None
        for cycle in self.settlement_cycles:
            if cycle.from_trade_date <= trade_date:
                chosen = cycle
        if chosen is None:
            raise NoSettlementRule(f"{self.mic}: no sourced settlement cycle for trade date {trade_date.isoformat()}")
        return chosen

    def as_venue(self) -> Venue:
        """The instrument-master ``Venue`` record for this MIC (trading calendar as calendar_id)."""
        return Venue(mic=self.mic, name=self.name, country=self.country, timezone=self.timezone,
                     calendar_id=self.trading_calendar_id)


class VenueRegistry:
    def __init__(self, venues: dict[str, VenueProfile], calendars: dict[str, BusinessCalendar], version: str = ""):
        self._venues = dict(venues)
        self._calendars = dict(calendars)
        self.version = version

    @property
    def mics(self) -> tuple[str, ...]:
        return tuple(sorted(self._venues))

    def venue(self, mic: str) -> VenueProfile:
        try:
            return self._venues[mic]
        except KeyError:
            raise UnknownVenue(mic) from None

    def calendar(self, calendar_id: str) -> BusinessCalendar:
        return self._calendars[calendar_id]

    def calendars(self) -> dict[str, BusinessCalendar]:
        return dict(self._calendars)

    def trading_calendar(self, mic: str) -> BusinessCalendar:
        return self._calendars[self.venue(mic).trading_calendar_id]

    def settlement_calendar(self, mic: str) -> BusinessCalendar:
        cal_id = self.venue(mic).settlement_calendar_id
        if cal_id is None:
            raise SettlementCalendarUnavailable(f"{mic}: no sourced settlement calendar")
        return self._calendars[cal_id]

    def expected_settlement_date(self, mic: str, trade_date: date) -> date:
        """Expected (intended) settlement date for an on-venue trade executed on ``trade_date``.

        The trade date must be a trading day of the venue (an ordinary session
        or a declared special session such as Muhurat). The cycle is chosen by
        trade date and counted on the settlement calendar. Any date outside a
        calendar's data horizon raises CalendarHorizonError: an unknown date is
        never treated as open.
        """
        venue = self.venue(mic)
        if not self.trading_calendar(mic).is_trading_day(trade_date):
            raise NotATradingDay(f"{mic}: {trade_date.isoformat()} is not a trading day")
        cycle = venue.settlement_cycle(trade_date)
        return self.settlement_calendar(mic).add_business_days(trade_date, cycle.business_days)


# ------------------------------------------------------------------ loading
def _source(key: str, d: dict) -> SourceRef:
    url, retrieved = d.get("url", ""), d.get("retrieved_on", "")
    if not _URL.match(url):
        raise ValueError(f"source {key}: url must be an http(s) URL")
    return SourceRef(key=key, url=url, retrieved_on=date.fromisoformat(retrieved), title=d.get("title", ""),
                     quote=d.get("quote", ""))


def _refs(keys, sources: dict[str, SourceRef], where: str) -> tuple[SourceRef, ...]:
    if not keys:
        raise ValueError(f"{where}: at least one source is required")
    missing = [k for k in keys if k not in sources]
    if missing:
        raise ValueError(f"{where}: unknown source keys {missing}")
    return tuple(sources[k] for k in keys)


def _convention(d: dict | None, sources: dict[str, SourceRef], where: str) -> Convention | None:
    if d is None:
        return None
    return Convention(rule=d["rule"], statement=d["statement"], sources=_refs(d.get("sources"), sources, where))


def load_calendars(directory: Path = DEFAULT_CALENDARS_DIR) -> dict[str, BusinessCalendar]:
    calendars: dict[str, BusinessCalendar] = {}
    for path in sorted(Path(directory).glob("*.json")):
        cal = load_calendar(path)
        if cal.calendar_id != path.stem:
            raise ValueError(f"{path}: calendar_id {cal.calendar_id!r} does not match the file name")
        calendars[cal.calendar_id] = cal
    return calendars


def load_registry(path: Path = DEFAULT_VENUES_PATH, calendars_dir: Path = DEFAULT_CALENDARS_DIR) -> VenueRegistry:
    """Load and validate the venue registry and every calendar it references."""
    body = json.loads(Path(path).read_text())
    calendars = load_calendars(calendars_dir)
    sources = {k: _source(k, v) for k, v in body["sources"].items()}
    venues: dict[str, VenueProfile] = {}
    for v in body["venues"]:
        mic = v["mic"]
        where = f"venue {mic}"
        for code in (mic, v["operating_mic"], v.get("segment_mic") or "XXXX"):
            if not _MIC.match(code):
                raise ValueError(f"{where}: {code!r} is not an ISO 10383 MIC")
        if mic in venues:
            raise ValueError(f"{where}: duplicate")
        if not _COUNTRY.match(v["country"]):
            raise ValueError(f"{where}: country must be ISO 3166-1 alpha-2")
        currency(v["currency"])            # refuses currencies the platform does not account in
        ZoneInfo(v["timezone"])
        trading_id, settlement_id = v["trading_calendar"], v.get("settlement_calendar")
        if trading_id not in calendars:
            raise ValueError(f"{where}: trading calendar {trading_id!r} not found")
        trading = calendars[trading_id]
        if trading.kind != "trading":
            raise ValueError(f"{where}: {trading_id} is not a trading calendar")
        if trading.timezone != v["timezone"]:
            raise ValueError(f"{where}: time zone differs from its trading calendar")
        if settlement_id is not None and settlement_id not in calendars:
            raise ValueError(f"{where}: settlement calendar {settlement_id!r} not found")
        cycles = tuple(
            SettlementCycle(from_trade_date=date.fromisoformat(c["from_trade_date"]), business_days=int(c["business_days"]),
                            sources=_refs(c.get("sources"), sources, f"{where} settlement cycle"),
                            statement=c.get("statement", ""))
            for c in v["settlement_cycles"])
        if not cycles:
            raise ValueError(f"{where}: at least one sourced settlement cycle is required")
        if any(c.business_days < 0 for c in cycles):
            raise ValueError(f"{where}: settlement business days must be >= 0")
        starts = [c.from_trade_date for c in cycles]
        if starts != sorted(starts) or len(set(starts)) != len(starts):
            raise ValueError(f"{where}: settlement cycles must be in strictly increasing trade-date order")
        venues[mic] = VenueProfile(
            mic=mic, operating_mic=v["operating_mic"], segment_mic=v.get("segment_mic"), name=v["name"],
            country=v["country"], currency=v["currency"], timezone=v["timezone"],
            trading_calendar_id=trading_id, settlement_calendar_id=settlement_id, settlement_cycles=cycles,
            lot=_convention(v.get("lot"), sources, f"{where} lot"), tick=_convention(v.get("tick"), sources, f"{where} tick"),
            provider_codes=dict(v.get("provider_codes", {})), sources=_refs(v.get("sources"), sources, where),
            notes=tuple(v.get("notes", ())))
    return VenueRegistry(venues, calendars, version=body.get("version", ""))
