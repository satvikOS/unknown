"""Versioned broker fee schedules from ``config/fees`` (CAP-OMS-11; CAP-RES-05).

A schedule file stores the published parameters of one broker plan for one market, each with its source URL,
retrieval date and a status: ``verified`` only where the page states the number, ``unverified`` otherwise. Two
calculations are offered, and they are kept apart on purpose:

* ``charge``: the expected fee of a fill from the verified parameters (commission, then third-party fees). It is an
  estimate; the broker's commission report is final (LED-07).
* ``pretrade_bound``: a conservative envelope that is never below any charge the order can incur. It uses the
  first-tier rate on every share with no tier discount and no cap, the per-order minimum, the remove-liquidity
  exchange fee on every share, and the file's ``upper_bound`` for components the page does not state.

Money is exact ``Fixed``; every component is rounded UP to the currency's ledger scale.

``FeeScheduleBook`` is what the core reserves with (ADR-0006 amendment, 2026-10-24): a BUY's pretrade fee envelope is
the ``pretrade_bound`` of the schedule, published for the account's broker, that covers the order's venue, product and
currency. No such schedule refuses the entry with the reason. The simulator alone keeps the policy's fee model,
because it charges whatever its fixture states.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed, Rounding, fmax, fmin
from ..domain.money import Money
from ..domain.money import currency as currency_info
from . import fees as policy_fee_model

REPO = Path(__file__).resolve().parents[4]
FEES_DIR = REPO / "config" / "fees"
IBKR_PRO_TIERED_US = FEES_DIR / "ibkr-pro-tiered-us.json"
STATUSES = frozenset({"verified", "unverified"})
SIDES = frozenset({"BUY", "SELL"})
BASES = frozenset({"per_share", "fraction_of_trade_value", "fraction_of_commission"})
ONE_DOLLAR = Fixed(1)


class FeeScheduleError(ValueError):
    pass


def _fixed(d: dict, key: str, where: str) -> Fixed:
    v = d.get(key)
    if not isinstance(v, str):
        raise FeeScheduleError(f"{where}.{key} must be a decimal string")
    f = Fixed.parse(v)
    if f.is_negative():
        raise FeeScheduleError(f"{where}.{key} must not be negative")
    return f


@dataclass(frozen=True)
class Component:
    id: str
    kind: str
    side: str                       # BUY | SELL | BOTH
    basis: str
    rate: Fixed
    status: str
    sources: tuple[str, ...]
    sub_dollar_rate: Fixed | None = None          # fraction of trade value below USD 1.00 per share
    max_fraction_of_value: Fixed | None = None
    valid_from: date | None = None
    valid_through: date | None = None

    def applies(self, side: str) -> bool:
        return self.side in ("BOTH", side)

    def in_window(self, trade_date: date) -> bool:
        return ((self.valid_from is None or trade_date >= self.valid_from)
                and (self.valid_through is None or trade_date <= self.valid_through))


@dataclass(frozen=True)
class Charge:
    commission: Fixed
    third_party: dict[str, Fixed]
    unverified: tuple[str, ...]     # components priced from an assumption, not a page

    @property
    def total(self) -> Fixed:
        return sum(self.third_party.values(), self.commission)

    def to_json(self) -> dict:
        return {"commission": self.commission.to_str(), "third_party": {k: v.to_str() for k, v in self.third_party.items()},
                "total": self.total.to_str(), "unverified_components": list(self.unverified)}


@dataclass(frozen=True)
class TieredSchedule:
    schedule_id: str
    currency: str
    tiers: tuple[tuple[int | None, Fixed], ...]
    minimum: Fixed
    max_fraction_of_value: Fixed
    components: tuple[Component, ...]
    bound_exchange_per_share: Fixed
    bound_exchange_sub_dollar: Fixed
    bound_taf_per_share: Fixed
    sources: dict
    body: dict
    broker: str = ""
    market: str = ""
    products: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()          # ISO 10383 MICs of the listings whose orders this schedule prices
    content_hash: str = ""                 # sha256 of the canonical file body: the exact text that priced an envelope

    @property
    def scale(self) -> int:
        return currency_info(self.currency).ledger_scale

    def _up(self, x: Fixed) -> Fixed:
        return x.rescale(self.scale, Rounding.CEILING) if x.scale > self.scale else x.rescale(self.scale)

    def component(self, cid: str) -> Component:
        for c in self.components:
            if c.id == cid:
                return c
        raise KeyError(cid)

    # ------------------------------------------------------------------ commission
    def base_commission(self, quantity: Fixed, month_volume_before: int = 0) -> Fixed:
        """Marginal tiered per-share charge for whole ``quantity`` after ``month_volume_before`` shares this month."""
        if not quantity.is_positive() or not quantity.is_multiple_of(Fixed(1)):
            raise FeeScheduleError("the tiered schedule needs a positive whole number of shares")
        remaining, position, total = int(quantity.rescale(0).mantissa), int(month_volume_before), Fixed(0)
        for upper, rate in self.tiers:
            if remaining <= 0:
                break
            if upper is not None and position >= upper:
                continue
            room = remaining if upper is None else min(remaining, upper - position)
            total, remaining, position = total + rate * room, remaining - room, position + room
        return total

    def commission(self, quantity: Fixed, price: Fixed, month_volume_before: int = 0) -> Fixed:
        """max(tiered charge, minimum), then the percentage cap (which wins even below the minimum, per the page)."""
        base = fmax(self.base_commission(quantity, month_volume_before), self.minimum)
        return self._up(fmin(base, self.max_fraction_of_value * quantity * price))

    # ------------------------------------------------------------------ third-party fees
    def _component_fee(self, c: Component, quantity: Fixed, price: Fixed, commission: Fixed, rate: Fixed) -> Fixed:
        value = quantity * price
        if c.basis == "per_share":
            fee = value * c.sub_dollar_rate if (c.sub_dollar_rate is not None and price < ONE_DOLLAR) else rate * quantity
        elif c.basis == "fraction_of_trade_value":
            fee = rate * value
        else:
            fee = rate * commission
        if c.max_fraction_of_value is not None:
            fee = fmin(fee, c.max_fraction_of_value * value)
        return self._up(fee)

    def charge(self, side: str, quantity: Fixed, price: Fixed, trade_date: date, month_volume_before: int = 0) -> Charge:
        """Expected fee of one fill, assuming it removes liquidity (no rebate is ever counted)."""
        if side not in SIDES:
            raise FeeScheduleError(f"side must be BUY or SELL, not {side!r}")
        commission = self.commission(quantity, price, month_volume_before)
        third: dict[str, Fixed] = {}
        unverified: list[str] = []
        for c in self.components:
            if not c.applies(side):
                continue
            rate = c.rate
            if not c.in_window(trade_date):
                rate = self._unstated_rate(c)
                unverified.append(c.id)
            third[c.id] = self._component_fee(c, quantity, price, commission, rate)
        return Charge(commission, third, tuple(unverified))

    def _unstated_rate(self, c: Component) -> Fixed:
        if c.id == "finra_taf":
            return self.bound_taf_per_share
        raise FeeScheduleError(f"{c.id}: no rate is stated for this trade date and the schedule has no bound for it")

    # ------------------------------------------------------------------ conservative envelope
    def pretrade_bound(self, side: str, quantity: Fixed, limit_price: Fixed, trade_date: date) -> Fixed:
        """For a BUY: never below ``charge(...).total`` at any month volume, on any venue read, at any fill price at
        or below the limit. For a SELL the value-based fees are evaluated at the limit (a sell can fill higher; the
        proceeds pay them, and the core reserves no sell envelope)."""
        if side not in SIDES:
            raise FeeScheduleError(f"side must be BUY or SELL, not {side!r}")
        if not quantity.is_positive():
            return Fixed(0, self.scale)
        commission = self._up(fmax(self.tiers[0][1] * quantity, self.minimum))     # no tier discount, no cap
        total = commission
        for c in self.components:
            if not c.applies(side):
                continue
            rate = c.rate if c.in_window(trade_date) else self._unstated_rate(c)
            if c.kind == "exchange":
                rate = fmax(rate, self.bound_exchange_per_share)
                exch = Component(c.id, c.kind, c.side, c.basis, rate, c.status, c.sources,
                                 fmax(c.sub_dollar_rate or Fixed(0), self.bound_exchange_sub_dollar))
                total = total + self._component_fee(exch, quantity, limit_price, commission, rate)
                continue
            uncapped = Component(c.id, c.kind, c.side, c.basis, rate, c.status, c.sources, c.sub_dollar_rate)
            total = total + self._component_fee(uncapped, quantity, limit_price, commission, rate)
        return total

    def describe(self) -> dict:
        return {"schedule_id": self.schedule_id, "content_hash": self.content_hash, "broker": self.broker,
                "market": self.market, "products": list(self.products), "venues": list(self.venues),
                "currency": self.currency, "plan": self.body.get("plan"),
                "tiers": [[u, r.to_str()] for u, r in self.tiers], "minimum": self.minimum.to_str(),
                "max_fraction_of_value": self.max_fraction_of_value.to_str(),
                "components": {c.id: {"rate": c.rate.to_str(), "basis": c.basis, "side": c.side, "status": c.status}
                               for c in self.components},
                "sources": self.sources}


def parse_schedule(body: dict) -> TieredSchedule:
    if body.get("schema_version") != 1:
        raise FeeScheduleError("unsupported fee schedule schema_version")
    sid = body.get("schedule_id")
    if not isinstance(sid, str) or "@" not in sid:
        raise FeeScheduleError("schedule_id must be '<name>@<retrieval date>'")
    sources = body.get("sources") or {}
    for key, s in sources.items():
        if not str(s.get("url", "")).startswith("https://"):
            raise FeeScheduleError(f"source {key} needs an https url")
        date.fromisoformat(s["retrieved_on"])

    def check_sourced(d: dict, where: str, keys: list[str]) -> None:
        status = d.get("status")
        if status not in STATUSES:
            raise FeeScheduleError(f"{where}.status must be verified or unverified")
        if status == "verified":
            if not keys or any(k not in sources for k in keys):
                raise FeeScheduleError(f"{where}: a verified value must cite a recorded source")
            if not str(d.get("quote", "")).strip():
                raise FeeScheduleError(f"{where}: a verified value must quote the page")

    com = body["commission"]
    check_sourced(com, "commission", [com.get("source")])
    tiers, last = [], 0
    for i, t in enumerate(com["tiers"]):
        upper = t["up_to_shares"]
        if upper is not None and (type(upper) is not int or upper <= last):
            raise FeeScheduleError("commission.tiers must have increasing integer upper bounds")
        if upper is None and i != len(com["tiers"]) - 1:
            raise FeeScheduleError("only the last commission tier may be open-ended")
        tiers.append((upper, _fixed(t, "per_share", f"commission.tiers[{i}]")))
        last = upper or last
    if not tiers or tiers[-1][0] is not None:
        raise FeeScheduleError("the last commission tier must be open-ended")
    comps = []
    for i, c in enumerate(body.get("third_party", [])):
        where = f"third_party[{i}]"
        check_sourced(c, where, list(c.get("sources", [])))
        if c.get("basis") not in BASES or c.get("side") not in {"BUY", "SELL", "BOTH"}:
            raise FeeScheduleError(f"{where}: unknown basis or side")
        sub = c.get("sub_dollar")
        comps.append(Component(
            id=c["id"], kind=c["kind"], side=c["side"], basis=c["basis"], rate=_fixed(c, "rate", where),
            status=c["status"], sources=tuple(c.get("sources", [])),
            sub_dollar_rate=_fixed(sub, "rate", where + ".sub_dollar") if sub else None,
            max_fraction_of_value=_fixed(c, "max_fraction_of_trade_value", where) if "max_fraction_of_trade_value" in c else None,
            valid_from=date.fromisoformat(c["valid_from"]) if c.get("valid_from") else None,
            valid_through=date.fromisoformat(c["valid_through"]) if c.get("valid_through") else None))
    if len({c.id for c in comps}) != len(comps):
        raise FeeScheduleError("duplicate third-party component id")
    ub = body["upper_bound"]
    if ub.get("status") != "unverified":
        raise FeeScheduleError("upper_bound is an assumption and must be marked unverified")
    ccy = body["currency"]
    currency_info(ccy)
    venues = body.get("venues", [])
    if not isinstance(venues, list) or not all(isinstance(v, str) and len(v) == 4 and v.isalnum() and v.isupper()
                                               for v in venues):
        raise FeeScheduleError("venues must be a list of ISO 10383 MICs")
    if not isinstance(body.get("broker"), str) or not body["broker"]:
        raise FeeScheduleError("broker must name the provider whose accounts this schedule prices")
    return TieredSchedule(
        schedule_id=sid, currency=ccy, tiers=tuple(tiers), minimum=_fixed(com, "minimum_per_order", "commission"),
        max_fraction_of_value=_fixed(com, "maximum_fraction_of_trade_value", "commission"), components=tuple(comps),
        bound_exchange_per_share=_fixed(ub, "exchange_per_share", "upper_bound"),
        bound_exchange_sub_dollar=_fixed(ub, "exchange_fraction_of_trade_value_sub_dollar", "upper_bound"),
        bound_taf_per_share=_fixed(ub, "finra_taf_per_share_outside_verified_window", "upper_bound"),
        sources=dict(sources), body=body, broker=body["broker"], market=str(body.get("market", "")),
        products=tuple(body.get("products") or ()), venues=tuple(venues), content_hash=sha256_hex(canonical_json(body)))


def load_schedule(path: Path | str = IBKR_PRO_TIERED_US) -> TieredSchedule:
    return parse_schedule(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------- the core's fee envelope (ADR-0006 amendment)
PRODUCT_OF_ASSET_CLASS = {"EQUITY": "STK", "ETF": "ETF"}
# The simulator is not a broker with a published schedule: it charges what its fixture states, and the policy's fee
# model is the only statement of that. Every other provider needs a schedule file.
POLICY_MODEL_PROVIDERS = frozenset({"simulated"})
BROKER_SCHEDULE = "BROKER_SCHEDULE"
POLICY_MODEL = "POLICY_MODEL"


class FeeEnvelopeUnavailable(Exception):
    """No fee schedule can price this order; the entry is refused with ``str(exc)`` as the reason."""


@dataclass(frozen=True)
class FeeEnvelope:
    amount: Fixed                       # ledger scale, never below any charge the order can incur
    currency: str
    basis: str                          # BROKER_SCHEDULE | POLICY_MODEL
    schedule_id: str | None
    content_hash: str | None
    unverified: tuple[str, ...]         # what the amount bounds rather than states

    def to_json(self) -> dict:
        return {"amount": self.amount.to_str(), "currency": self.currency, "basis": self.basis,
                "schedule_id": self.schedule_id, "content_hash": self.content_hash, "unverified": list(self.unverified)}

    def detail(self) -> str:
        if self.basis == POLICY_MODEL:
            return f"fee envelope {self.amount.to_str()} {self.currency} from the policy fee model (simulated broker)"
        return (f"fee envelope {self.amount.to_str()} {self.currency} from {self.schedule_id} "
                f"(sha256 {str(self.content_hash)[:12]}); bounded, not stated: {', '.join(self.unverified)}")


def _trade_dates(now_ns: int) -> tuple[date, ...]:
    """Every venue's local trade date is within one day of the UTC date, so the bound is taken over all three."""
    d = datetime.fromtimestamp(now_ns // 1_000_000_000, tz=timezone.utc).date()
    return (d - timedelta(days=1), d, d + timedelta(days=1))


@dataclass(frozen=True)
class FeeScheduleBook:
    """The fee schedules the core may reserve with, and the files that failed to load (named in refusals)."""
    schedules: tuple[TieredSchedule, ...]
    errors: tuple[str, ...] = ()

    @classmethod
    def from_dir(cls, directory: Path | str = FEES_DIR) -> "FeeScheduleBook":
        schedules, errors = [], []
        try:
            paths = sorted(Path(directory).glob("*.json"))
        except OSError as exc:
            return cls((), (f"{directory}: {exc}",))
        for path in paths:
            try:
                schedules.append(load_schedule(path))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{path.name}: {exc}")
        return cls(tuple(schedules), tuple(errors))

    def select(self, broker: str, ccy: str, venue_mic: str, asset_class: str) -> TieredSchedule:
        product = PRODUCT_OF_ASSET_CLASS.get(asset_class)
        found = [s for s in self.schedules if s.broker == broker and s.currency == ccy and venue_mic in s.venues
                 and product in s.products]
        if len(found) == 1:
            return found[0]
        what = f"broker {broker}, venue {venue_mic}, {asset_class} in {ccy}"
        if len(found) > 1:
            raise FeeEnvelopeUnavailable(f"FEE_SCHEDULE_AMBIGUOUS: {len(found)} schedules cover {what}: "
                                         + ", ".join(s.schedule_id for s in found))
        note = f"; schedules that failed to load: {'; '.join(self.errors)}" if self.errors else ""
        raise FeeEnvelopeUnavailable(f"FEE_SCHEDULE_MISSING: no fee schedule in config/fees covers {what}{note}")

    def buy_envelope(self, *, broker: str, policy_fees, instrument, quantity: Fixed, limit_price: Fixed,
                     notional: Fixed, ccy: str, now_ns: int) -> FeeEnvelope:
        """The reservation fee envelope of a BUY (ADR-0006 #4 as amended): exact Fixed at ledger scale."""
        scale = currency_info(ccy).ledger_scale
        if broker in POLICY_MODEL_PROVIDERS:
            env = policy_fee_model.envelope(policy_fees, quantity, Money(ccy, notional))
            return FeeEnvelope(env.amount, ccy, POLICY_MODEL, None, None, ("policy fee model",))
        if instrument is None:
            raise FeeEnvelopeUnavailable("FEE_SCHEDULE_MISSING: instrument not in master")
        if instrument.multiplier != Fixed(1):
            raise FeeEnvelopeUnavailable(f"FEE_SCHEDULE_MISSING: schedules price shares; multiplier {instrument.multiplier}")
        s = self.select(broker, ccy, instrument.venue_mic, instrument.asset_class.value)
        try:
            amount = max(s.pretrade_bound("BUY", quantity, limit_price, d) for d in _trade_dates(now_ns))
        except FeeScheduleError as exc:
            raise FeeEnvelopeUnavailable(f"FEE_SCHEDULE_UNPRICEABLE: {s.schedule_id}: {exc}") from None
        unverified = ["exchange fee on venues not read", "no tier discount", "no cap"]
        if s.body.get("plan_effective", {}).get("status") == "unverified":
            unverified.append("plan in effect")
        return FeeEnvelope(amount.rescale(scale, Rounding.CEILING), ccy, BROKER_SCHEDULE, s.schedule_id, s.content_hash,
                           tuple(unverified))

    def fill_estimate(self, *, broker: str, policy_fees, instrument, side: str, quantity: Fixed, price: Fixed,
                      notional: Money, now_ns: int) -> Money:
        """Provisional fee of one fill until the broker's commission report (LED-07). It uses the schedule that
        priced the envelope, so a whole fill's estimate never exceeds its envelope. A fill is a fact and is never
        refused: without a usable schedule the estimate falls back to the policy's fee model."""
        ccy = notional.currency
        if (broker not in POLICY_MODEL_PROVIDERS and instrument is not None and instrument.multiplier == Fixed(1)
                and quantity.is_positive() and quantity.is_multiple_of(Fixed(1)) and side in SIDES):
            try:
                s = self.select(broker, ccy, instrument.venue_mic, instrument.asset_class.value)
                total = s.charge(side, quantity, price, _trade_dates(now_ns)[1]).total
                return Money(ccy, total.rescale(s.scale, Rounding.CEILING))
            except (FeeEnvelopeUnavailable, FeeScheduleError):
                pass
        return policy_fee_model.estimate(policy_fees, quantity, notional)


_DEFAULT: dict[tuple, FeeScheduleBook] = {}


def default_book(directory: Path | str = FEES_DIR) -> FeeScheduleBook:
    """``config/fees`` as a book, re-read when a file there changes (name, size or modification time)."""
    d = Path(directory)
    try:
        key = (str(d), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(d.glob("*.json"))))
    except OSError:
        key = (str(d), None)
    book = _DEFAULT.get(key)
    if book is None:
        _DEFAULT.clear()
        book = _DEFAULT[key] = FeeScheduleBook.from_dir(d)
    return book
