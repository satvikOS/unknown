"""Versioned owner policy documents and session mandates (ADR-0007, ADR-0008).

Parsing is strict: unknown keys, missing keys and out-of-range values are
errors. An absent value is never treated as a permissive default; the only
defaults are the restrictive ones written in this module and in the proposed
paper policy under ``config/``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from ..domain.instruments import AssetClass
from ..domain.money import Money, currency

POLICY_SCHEMA_VERSION = 1


class PolicyError(ValueError):
    pass


class Mode(enum.Enum):
    SIMULATED = "SIMULATED"
    PAPER = "PAPER"
    LIVE = "LIVE"


class BelowTarget(enum.Enum):
    CLOSE = "CLOSE"
    RESUME = "RESUME"


class StopLiquidation(enum.Enum):
    NONE = "NONE"
    REDUCE_WITHIN_COLLAR = "REDUCE_WITHIN_COLLAR"


class _Reader:
    """Strict field access that records which keys were consumed."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise PolicyError(f"{where} must be an object")
        self.data = data
        self.where = where
        self.used: set[str] = set()

    def _get(self, key: str) -> Any:
        if key not in self.data:
            raise PolicyError(f"{self.where}.{key} is required")
        self.used.add(key)
        return self.data[key]

    def str(self, key: str, choices: set[str] | None = None) -> str:
        v = self._get(key)
        if not isinstance(v, str) or not v:
            raise PolicyError(f"{self.where}.{key} must be a non-empty string")
        if choices is not None and v not in choices:
            raise PolicyError(f"{self.where}.{key} must be one of {sorted(choices)}")
        return v

    def bool(self, key: str) -> bool:
        v = self._get(key)
        if type(v) is not bool:
            raise PolicyError(f"{self.where}.{key} must be a boolean")
        return v

    def int(self, key: str, lo: int, hi: int) -> int:
        v = self._get(key)
        if type(v) is not int or not lo <= v <= hi:
            raise PolicyError(f"{self.where}.{key} must be an integer in [{lo}, {hi}]")
        return v

    def opt_int(self, key: str, lo: int, hi: int) -> int | None:
        if self.data.get(key, ...) is None:
            self.used.add(key)
            return None
        return self.int(key, lo, hi)

    def money(self, key: str, ccy: str, positive: bool = True) -> Money:
        v = self._get(key)
        if not isinstance(v, str):
            raise PolicyError(f"{self.where}.{key} must be a decimal string")
        try:
            amount = Fixed.parse(v)
        except ValueError as exc:
            raise PolicyError(f"{self.where}.{key}: {exc}") from None
        m = Money(ccy, amount)
        if positive and not m.is_positive():
            raise PolicyError(f"{self.where}.{key} must be positive")
        if m.is_negative():
            raise PolicyError(f"{self.where}.{key} must not be negative")
        try:
            m.at_ledger_scale()
        except ArithmeticError:
            raise PolicyError(f"{self.where}.{key} has more precision than the {ccy} ledger scale") from None
        return m

    def fixed(self, key: str) -> Fixed:
        v = self._get(key)
        if not isinstance(v, str):
            raise PolicyError(f"{self.where}.{key} must be a decimal string")
        return Fixed.parse(v)

    def str_list(self, key: str, choices: set[str] | None = None) -> tuple[str, ...]:
        v = self._get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise PolicyError(f"{self.where}.{key} must be a list of strings")
        if len(set(v)) != len(v):
            raise PolicyError(f"{self.where}.{key} has duplicates")
        if choices is not None and not set(v) <= choices:
            raise PolicyError(f"{self.where}.{key} contains values outside {sorted(choices)}")
        return tuple(v)

    def obj(self, key: str) -> "_Reader":
        return _Reader(self._get(key), f"{self.where}.{key}")

    def done(self) -> None:
        extra = set(self.data) - self.used
        if extra:
            raise PolicyError(f"{self.where} has unknown fields: {sorted(extra)}")


@dataclass(frozen=True)
class Eligibility:
    asset_classes: tuple[str, ...]
    venues: tuple[str, ...]
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]
    outside_regular_hours: bool
    allow_short: bool
    allow_margin: bool
    allow_derivatives: bool
    allow_leveraged_or_inverse: bool
    allow_fractional: bool
    allow_fx_conversion: bool
    allow_transfers: bool

    @classmethod
    def parse(cls, r: _Reader) -> "Eligibility":
        e = cls(
            asset_classes=r.str_list("asset_classes", {a.value for a in AssetClass}),
            venues=r.str_list("venues"),
            order_types=r.str_list("order_types", {"LIMIT"}),
            time_in_force=r.str_list("time_in_force", {"DAY"}),
            outside_regular_hours=r.bool("outside_regular_hours"),
            allow_short=r.bool("allow_short"),
            allow_margin=r.bool("allow_margin"),
            allow_derivatives=r.bool("allow_derivatives"),
            allow_leveraged_or_inverse=r.bool("allow_leveraged_or_inverse"),
            allow_fractional=r.bool("allow_fractional"),
            allow_fx_conversion=r.bool("allow_fx_conversion"),
            allow_transfers=r.bool("allow_transfers"),
        )
        r.done()
        # The initial product supports only the cash-equity profile. A policy that
        # asks for more is rejected until the corresponding release exists (RSK-01).
        if any([e.allow_short, e.allow_margin, e.allow_derivatives, e.allow_fx_conversion, e.allow_transfers]):
            raise PolicyError("short, margin, derivatives, FX conversion and transfers are not released")
        if e.outside_regular_hours:
            raise PolicyError("outside-regular-hours trading is not released")
        if not set(e.asset_classes) <= {"EQUITY", "ETF"}:
            raise PolicyError("only EQUITY and ETF are released for trading")
        if not e.venues:
            raise PolicyError("eligibility.venues must name at least one venue")
        return e


@dataclass(frozen=True)
class AccountLimits:
    authorized_capital: Money
    operational_buffer: Money
    max_order_notional: Money
    max_position_bps_of_session_equity: int
    max_gross_exposure: Money
    max_orders_per_minute: int
    price_collar_bps: int
    quote_max_age_ms: int
    daily_loss_trigger: Money
    cumulative_loss_allowance: Money
    risk_day_timezone: str
    max_reconciliation_age_s: int
    reauth_max_age_s: int
    max_clock_skew_ms: int
    min_free_disk_mb: int
    max_participation_bps_of_adv: int
    require_liquidity_data: bool

    @classmethod
    def parse(cls, r: _Reader, ccy: str) -> "AccountLimits":
        lim = cls(
            authorized_capital=r.money("authorized_capital", ccy),
            operational_buffer=r.money("operational_buffer", ccy, positive=False),
            max_order_notional=r.money("max_order_notional", ccy),
            max_position_bps_of_session_equity=r.int("max_position_bps_of_session_equity", 1, 10_000),
            max_gross_exposure=r.money("max_gross_exposure", ccy),
            max_orders_per_minute=r.int("max_orders_per_minute", 1, 600),
            price_collar_bps=r.int("price_collar_bps", 1, 2_000),
            quote_max_age_ms=r.int("quote_max_age_ms", 1, 86_400_000),
            daily_loss_trigger=r.money("daily_loss_trigger", ccy),
            cumulative_loss_allowance=r.money("cumulative_loss_allowance", ccy),
            risk_day_timezone=r.str("risk_day_timezone"),
            max_reconciliation_age_s=r.int("max_reconciliation_age_s", 1, 86_400),
            reauth_max_age_s=r.int("reauth_max_age_s", 30, 86_400),
            max_clock_skew_ms=r.int("max_clock_skew_ms", 1, 600_000),
            min_free_disk_mb=r.int("min_free_disk_mb", 0, 10_000_000),
            max_participation_bps_of_adv=r.int("max_participation_bps_of_adv", 1, 10_000),
            require_liquidity_data=r.bool("require_liquidity_data"),
        )
        r.done()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(lim.risk_day_timezone)
        except Exception:
            raise PolicyError(f"unknown risk_day_timezone {lim.risk_day_timezone!r}") from None
        return lim


@dataclass(frozen=True)
class SessionTemplate:
    seed: Money
    target_terminal_equity: Money
    strategy_eligibility: tuple[str, ...]
    session_drawdown_trigger_bps: int
    session_max_loss: Money
    auto_start: bool
    expires_after_s: int | None
    below_target_after_target_observed: BelowTarget
    max_resumes: int

    @classmethod
    def parse(cls, r: _Reader, ccy: str) -> "SessionTemplate":
        t = cls(
            seed=r.money("seed", ccy),
            target_terminal_equity=r.money("target_terminal_equity", ccy),
            strategy_eligibility=r.str_list("strategy_eligibility"),
            session_drawdown_trigger_bps=r.int("session_drawdown_trigger_bps", 1, 10_000),
            session_max_loss=r.money("session_max_loss", ccy),
            auto_start=r.bool("auto_start"),
            expires_after_s=r.opt_int("expires_after_s", 60, 10 * 365 * 86_400),
            below_target_after_target_observed=BelowTarget(r.str("below_target_after_target_observed", {b.value for b in BelowTarget})),
            max_resumes=r.int("max_resumes", 0, 100),
        )
        r.done()
        t.validate()
        return t

    def validate(self) -> None:
        if self.target_terminal_equity <= self.seed:
            raise PolicyError("target_terminal_equity must exceed seed (it is terminal equity, not profit)")
        if self.below_target_after_target_observed is BelowTarget.RESUME and self.max_resumes < 1:
            raise PolicyError("RESUME requires max_resumes >= 1")
        if self.below_target_after_target_observed is BelowTarget.CLOSE and self.max_resumes != 0:
            raise PolicyError("max_resumes must be 0 when below-target policy is CLOSE")

    def to_json(self) -> dict:
        return {
            "seed": self.seed.amount.to_str(),
            "target_terminal_equity": self.target_terminal_equity.amount.to_str(),
            "strategy_eligibility": list(self.strategy_eligibility),
            "session_drawdown_trigger_bps": self.session_drawdown_trigger_bps,
            "session_max_loss": self.session_max_loss.amount.to_str(),
            "auto_start": self.auto_start,
            "expires_after_s": self.expires_after_s,
            "below_target_after_target_observed": self.below_target_after_target_observed.value,
            "max_resumes": self.max_resumes,
        }


@dataclass(frozen=True)
class SuccessorRule:
    create_on_success: bool
    reinvest_protected_proceeds: bool

    @classmethod
    def parse(cls, r: _Reader) -> "SuccessorRule":
        s = cls(create_on_success=r.bool("create_on_success"),
                reinvest_protected_proceeds=r.bool("reinvest_protected_proceeds"))
        r.str("mandate_source", {"ACTIVE_TEMPLATE_AT_CREATION"})
        r.done()
        if s.reinvest_protected_proceeds:
            raise PolicyError("reinvesting protected proceeds is not offered by policy schema 1 (ADR-0008)")
        return s


@dataclass(frozen=True)
class WindDown:
    collar_bps: int
    reprice_after_s: int
    max_attempts: int

    @classmethod
    def parse(cls, r: _Reader) -> "WindDown":
        r.str("order_style", {"LIMIT_AT_BID_MINUS_COLLAR"})
        w = cls(collar_bps=r.int("collar_bps", 1, 2_000), reprice_after_s=r.int("reprice_after_s", 5, 86_400),
                max_attempts=r.int("max_attempts", 1, 100))
        r.done()
        return w


@dataclass(frozen=True)
class FeeModel:
    """Conservative fee *estimate* used for reservations; the broker's report is final."""
    per_share: Fixed
    minimum: Money
    maximum_bps_of_notional: int
    estimate_margin_bps: int
    source: str
    verified_on: str

    @classmethod
    def parse(cls, r: _Reader, ccy: str) -> "FeeModel":
        r.str("model", {"PER_SHARE"})
        f = cls(per_share=r.fixed("per_share"), minimum=r.money("minimum", ccy, positive=False),
                maximum_bps_of_notional=r.int("maximum_bps_of_notional", 0, 10_000),
                estimate_margin_bps=r.int("estimate_margin_bps", 0, 100_000),
                source=r.str("source"), verified_on=r.str("verified_on"))
        r.done()
        if f.per_share.is_negative():
            raise PolicyError("fees.per_share must not be negative")
        return f


@dataclass(frozen=True)
class Policy:
    body: dict
    policy_hash: str
    name: str
    mode: Mode
    currency: str
    eligibility: Eligibility
    limits: AccountLimits
    session_template: SessionTemplate
    successor: SuccessorRule
    wind_down: WindDown
    on_stop_liquidation: StopLiquidation
    auto_resume_after_clean_reconciliation: bool
    fees: FeeModel
    settlement_calendar: str

    @classmethod
    def parse(cls, body: dict) -> "Policy":
        r = _Reader(body, "policy")
        version = r.int("schema_version", 1, 1000)
        if version != POLICY_SCHEMA_VERSION:
            raise PolicyError(f"unsupported policy schema_version {version}")
        name = r.str("name")
        mode = Mode(r.str("mode", {m.value for m in Mode}))
        ccy = r.str("currency")
        try:
            currency(ccy)
        except ValueError as exc:
            raise PolicyError(str(exc)) from None
        p = cls(
            body=body,
            policy_hash=policy_hash(body),
            name=name,
            mode=mode,
            currency=ccy,
            eligibility=Eligibility.parse(r.obj("eligibility")),
            limits=AccountLimits.parse(r.obj("account_limits"), ccy),
            session_template=SessionTemplate.parse(r.obj("session_template"), ccy),
            successor=SuccessorRule.parse(r.obj("successor")),
            wind_down=WindDown.parse(r.obj("wind_down")),
            on_stop_liquidation=StopLiquidation(r.str("on_stop_liquidation", {s.value for s in StopLiquidation})),
            auto_resume_after_clean_reconciliation=r.bool("auto_resume_after_clean_reconciliation"),
            fees=FeeModel.parse(r.obj("fees"), ccy),
            settlement_calendar=r.str("settlement_calendar"),
        )
        r.done()
        if p.session_template.seed > p.limits.authorized_capital:
            raise PolicyError("session seed exceeds authorized capital")
        if p.on_stop_liquidation is not StopLiquidation.NONE:
            raise PolicyError("stop liquidation policies other than NONE are not released")
        return p


def policy_hash(body: dict) -> str:
    return sha256_hex(canonical_json(body))


@dataclass(frozen=True)
class Mandate:
    """Immutable session mandate snapshot (SES-01). Hash covers every field."""
    session_id: str
    account_key: str
    currency: str
    seed: Money
    target_terminal_equity: Money
    strategy_eligibility: tuple[str, ...]
    session_drawdown_trigger_bps: int
    session_max_loss: Money
    auto_start: bool
    expires_at_ns: int | None
    below_target_after_target_observed: BelowTarget
    max_resumes: int
    policy_hash: str
    created_at_ns: int
    origin: str            # "OWNER" or "SUCCESSOR_OF:<session id>"

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "account_key": self.account_key,
            "currency": self.currency,
            "seed": self.seed.amount.to_str(),
            "target_terminal_equity": self.target_terminal_equity.amount.to_str(),
            "strategy_eligibility": list(self.strategy_eligibility),
            "session_drawdown_trigger_bps": self.session_drawdown_trigger_bps,
            "session_max_loss": self.session_max_loss.amount.to_str(),
            "auto_start": self.auto_start,
            "expires_at_ns": self.expires_at_ns,
            "below_target_after_target_observed": self.below_target_after_target_observed.value,
            "max_resumes": self.max_resumes,
            "policy_hash": self.policy_hash,
            "created_at_ns": self.created_at_ns,
            "origin": self.origin,
        }

    @property
    def mandate_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_json()))

    @classmethod
    def from_json(cls, d: dict) -> "Mandate":
        ccy = d["currency"]
        return cls(
            session_id=d["session_id"], account_key=d["account_key"], currency=ccy,
            seed=Money(ccy, Fixed.parse(d["seed"])),
            target_terminal_equity=Money(ccy, Fixed.parse(d["target_terminal_equity"])),
            strategy_eligibility=tuple(d["strategy_eligibility"]),
            session_drawdown_trigger_bps=d["session_drawdown_trigger_bps"],
            session_max_loss=Money(ccy, Fixed.parse(d["session_max_loss"])),
            auto_start=d["auto_start"], expires_at_ns=d["expires_at_ns"],
            below_target_after_target_observed=BelowTarget(d["below_target_after_target_observed"]),
            max_resumes=d["max_resumes"], policy_hash=d["policy_hash"],
            created_at_ns=d["created_at_ns"], origin=d["origin"],
        )

    @classmethod
    def from_template(cls, template: SessionTemplate, *, session_id: str, account_key: str, currency_code: str,
                      policy_hash_value: str, now_ns: int, origin: str,
                      seed: Money | None = None, target: Money | None = None) -> "Mandate":
        """Snapshot a template. Explicit owner overrides apply to this mandate only."""
        seed = seed or template.seed
        target = target or template.target_terminal_equity
        for m in (seed, target):
            if m.currency != currency_code:
                raise PolicyError("mandate amounts must be in the policy currency")
            m.at_ledger_scale()
        if not seed.is_positive() or target <= seed:
            raise PolicyError("seed must be positive and target terminal equity must exceed seed")
        expires = now_ns + template.expires_after_s * 1_000_000_000 if template.expires_after_s else None
        return cls(session_id=session_id, account_key=account_key, currency=currency_code, seed=seed,
                   target_terminal_equity=target, strategy_eligibility=template.strategy_eligibility,
                   session_drawdown_trigger_bps=template.session_drawdown_trigger_bps,
                   session_max_loss=template.session_max_loss, auto_start=template.auto_start,
                   expires_at_ns=expires,
                   below_target_after_target_observed=template.below_target_after_target_observed,
                   max_resumes=template.max_resumes, policy_hash=policy_hash_value,
                   created_at_ns=now_ns, origin=origin)
