"""Strategy specification schema with strict validation (RES-01; BR-DAT-08; CAP-RES-12).

One hashed, versioned document holds everything RES-01 asks a strategy to
declare before it is evaluated:

* hypothesis and the market conditions it is about;
* equations, variables with units, and assumptions;
* causal features and data requirements (point-in-time only);
* entry, exit, sizing, holding and execution rules;
* expected costs, capacity, liquidity and operational constraints;
* risk limits and failure conditions;
* the development / untouched-holdout / walk-forward plan;
* baselines, the sensitivity and stress plans and the promotion evidence.

Parsing is strict in the same way as the owner policy (ADR-0007): unknown keys,
missing keys, wrong types and out-of-range values are errors. Numbers are
decimal strings so the canonical hash never depends on float formatting. The
spec hash is SHA-256 over the canonical JSON (``domain.canonical``).

Validation enforces the project's rules rather than just shapes: long-only
cash-equity limits (RSK-01), same-bar fills prohibited (CAP-RES-04), a
non-overlapping chronological plan with an untouched holdout (CAP-RES-07), a
purge at least as long as label horizon plus execution delay (BR-DAT-14),
cost multipliers covering 0.5x/1x/2x/3x (CAP-RES-04/05), several impact
coefficients (book ch. 7) and the evidence kinds the ch. 20 gates consume.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from .common import FORMULAS, SpecError

SPEC_SCHEMA_VERSION = 1

FAMILIES = frozenset({"TREND", "CROSS_SECTIONAL", "MEAN_REVERSION", "PAIRS", "FUNDAMENTAL_EVENT", "MACRO",
                      "MICROSTRUCTURE", "EXECUTION_OPTIMIZATION", "BASELINE"})  # RES-02
VARIABLE_KINDS = frozenset({"input", "parameter", "derived", "output"})
AVAILABILITY_BASES = frozenset({"BAR_CLOSE_PLUS_LAG", "PUBLISHED_PLUS_ALLOWANCE", "DATE_ONLY_NEXT_SESSION", "CAPTURED"})  # BR-DAT-05
FREQUENCIES = frozenset({"DAILY", "INTRADAY", "EVENT"})
SIZING_METHODS = frozenset({"TARGET_WEIGHT"})
STRESS_KINDS = frozenset({"COST_MULTIPLIER", "PARTICIPATION", "IMPACT_Y", "LIMIT_OFFSET", "REGIME"})
EVIDENCE_KINDS = frozenset({"REPLAY_EXACT", "CORE_REPLAY_MATCH", "WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT",
                            "COST_SENSITIVITY", "STRESS", "DSR", "PBO", "N_EFF", "PAPER_RECONCILIATION",
                            "MONITORING_INCIDENT"})
# Evidence the book ch. 20 gates always need (no causal leakage is enforced by the engine itself).
REQUIRED_EVIDENCE = frozenset({"REPLAY_EXACT", "WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT", "COST_SENSITIVITY",
                               "STRESS"})
REQUIRED_COST_MULTIPLIERS = (Fixed.parse("0.5"), Fixed(1), Fixed(2), Fixed(3))
PARAM_KINDS = frozenset({"INTEGER", "DECIMAL"})

_ID = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _R:
    """Strict reader: every key must be consumed, nothing defaults silently."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise SpecError(f"{where} must be an object")
        self.d = data
        self.where = where
        self.used: set[str] = set()

    def get(self, key: str) -> Any:
        if key not in self.d:
            raise SpecError(f"{self.where}.{key} is required")
        self.used.add(key)
        return self.d[key]

    def text(self, key: str, *, pattern: re.Pattern | None = None, choices: frozenset | None = None,
             max_len: int = 4000) -> str:
        v = self.get(key)
        if not isinstance(v, str) or not v.strip() or len(v) > max_len:
            raise SpecError(f"{self.where}.{key} must be a non-empty string of at most {max_len} characters")
        if pattern is not None and not pattern.fullmatch(v):
            raise SpecError(f"{self.where}.{key} has an invalid format: {v!r}")
        if choices is not None and v not in choices:
            raise SpecError(f"{self.where}.{key} must be one of {sorted(choices)}")
        return v

    def integer(self, key: str, lo: int, hi: int) -> int:
        v = self.get(key)
        if type(v) is not int or not lo <= v <= hi:
            raise SpecError(f"{self.where}.{key} must be an integer in [{lo}, {hi}]")
        return v

    def opt_integer(self, key: str, lo: int, hi: int) -> int | None:
        if self.d.get(key, ...) is None:
            self.used.add(key)
            return None
        return self.integer(key, lo, hi)

    def boolean(self, key: str, *, must_be: bool | None = None, why: str = "") -> bool:
        v = self.get(key)
        if type(v) is not bool:
            raise SpecError(f"{self.where}.{key} must be a boolean")
        if must_be is not None and v is not must_be:
            raise SpecError(f"{self.where}.{key} must be {str(must_be).lower()}: {why}")
        return v

    def dec(self, key: str, lo: Fixed | None = None, hi: Fixed | None = None, *, lo_open: bool = False) -> Fixed:
        return _dec(self.get(key), f"{self.where}.{key}", lo, hi, lo_open=lo_open)

    def day(self, key: str) -> date:
        v = self.get(key)
        if not isinstance(v, str) or len(v) != 10:
            raise SpecError(f"{self.where}.{key} must be an ISO date YYYY-MM-DD")
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise SpecError(f"{self.where}.{key} must be an ISO date YYYY-MM-DD") from None

    def texts(self, key: str, *, min_items: int = 1, max_items: int = 100, choices: frozenset | None = None,
              pattern: re.Pattern | None = None) -> tuple[str, ...]:
        v = self.get(key)
        if not isinstance(v, list) or not min_items <= len(v) <= max_items:
            raise SpecError(f"{self.where}.{key} must be a list of {min_items}..{max_items} strings")
        out = []
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip() or len(item) > 2000:
                raise SpecError(f"{self.where}.{key}[{i}] must be a non-empty string")
            if choices is not None and item not in choices:
                raise SpecError(f"{self.where}.{key}[{i}] must be one of {sorted(choices)}")
            if pattern is not None and not pattern.fullmatch(item):
                raise SpecError(f"{self.where}.{key}[{i}] has an invalid format: {item!r}")
            out.append(item)
        if len(set(out)) != len(out):
            raise SpecError(f"{self.where}.{key} has duplicates")
        return tuple(out)

    def objs(self, key: str, *, min_items: int = 1, max_items: int = 100) -> list["_R"]:
        v = self.get(key)
        if not isinstance(v, list) or not min_items <= len(v) <= max_items:
            raise SpecError(f"{self.where}.{key} must be a list of {min_items}..{max_items} objects")
        return [_R(item, f"{self.where}.{key}[{i}]") for i, item in enumerate(v)]

    def obj(self, key: str) -> "_R":
        return _R(self.get(key), f"{self.where}.{key}")

    def done(self) -> None:
        extra = set(self.d) - self.used
        if extra:
            raise SpecError(f"{self.where} has unknown fields: {sorted(extra)}")


def _dec(v: Any, where: str, lo: Fixed | None, hi: Fixed | None, *, lo_open: bool = False) -> Fixed:
    if not isinstance(v, str):
        raise SpecError(f"{where} must be a decimal string (numbers are strings so hashes are exact)")
    try:
        x = Fixed.parse(v)
    except ValueError:
        raise SpecError(f"{where} is not a plain decimal: {v!r}") from None
    if lo is not None and (x < lo or (lo_open and x == lo)):
        raise SpecError(f"{where} must be {'>' if lo_open else '>='} {lo}")
    if hi is not None and x > hi:
        raise SpecError(f"{where} must be <= {hi}")
    return x


ZERO, ONE = Fixed(0), Fixed(1)


@dataclass(frozen=True)
class StrategySpec:
    """A validated strategy specification. ``body`` is the exact document that was hashed."""
    body: dict
    spec_hash: str
    strategy_id: str
    version: int
    family: str
    parameters: dict[str, Fixed]
    parameter_domain: dict[str, tuple[str, Fixed, Fixed]]
    instrument_ids: tuple[str, ...]
    sizing: dict[str, Fixed]
    execution: dict[str, Any]
    costs: dict[str, Any]
    development: tuple[date, date]
    holdout: tuple[date, date]
    walk_forward: dict[str, Any]
    cost_multipliers: tuple[Fixed, ...]
    parameter_grid: dict[str, tuple[Fixed, ...]]
    stress_plan: tuple[dict, ...]
    promotion_evidence: tuple[str, ...]
    baselines: tuple[str, ...]

    @property
    def artifact_id(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    def param(self, name: str) -> Fixed:
        return self.parameters[name]

    def param_int(self, name: str) -> int:
        v = self.parameters[name]
        if not v.is_multiple_of(ONE):
            raise SpecError(f"parameter {name} is not an integer")
        return int(v.rescale(0).mantissa)

    def with_parameters(self, **values: str) -> "StrategySpec":
        """A new spec (new hash) with some parameters replaced; validated like any other."""
        body = {**self.body, "parameters": {**self.body["parameters"], **values}}
        return parse_spec(body)


def parse_spec(body: dict) -> StrategySpec:
    """Validate a strategy specification document; raise ``SpecError`` with the first problem."""
    r = _R(body, "spec")
    if r.integer("schema_version", 1, 1000) != SPEC_SCHEMA_VERSION:
        raise SpecError(f"unsupported spec schema_version (expected {SPEC_SCHEMA_VERSION})")
    strategy_id = r.text("strategy_id", pattern=_ID)
    version = r.integer("version", 1, 1_000_000)
    family = r.text("family", choices=FAMILIES)
    r.text("title", max_len=200)
    r.text("author", pattern=_TOKEN)

    h = r.obj("hypothesis")
    h.text("statement")
    h.text("mechanism")
    h.texts("market_conditions")
    h.text("expected_effect")
    h.done()

    for e in r.objs("equations"):
        e.text("id", pattern=_TOKEN)
        e.text("expression")
        e.text("book_ref", max_len=200)
        fid = e.text("formula_id", pattern=_TOKEN)
        if fid not in FORMULAS:
            raise SpecError(f"{e.where}.formula_id {fid!r} is not in the formula registry")
        e.done()
    symbols = set()
    for v in r.objs("variables"):
        symbols.add(v.text("symbol", max_len=64))
        v.text("description")
        v.text("unit", max_len=100)
        v.text("kind", choices=VARIABLE_KINDS)
        v.done()
    r.texts("assumptions")

    for f in r.objs("features"):
        f.text("id", pattern=_TOKEN)
        f.integer("version", 1, 1_000_000)
        fid = f.text("formula_id", pattern=_TOKEN)
        if fid not in FORMULAS:
            raise SpecError(f"{f.where}.formula_id {fid!r} is not in the formula registry")
        f.texts("inputs")
        f.text("unit", max_len=100)
        f.text("availability_basis", choices=AVAILABILITY_BASES)
        f.integer("freshness_budget_s", 1, 10 * 365 * 86_400)
        f.integer("warm_up_observations", 0, 100_000)
        f.boolean("causal", must_be=True, why="features use only data available at the decision time (DAT-04)")
        f.done()
    for d in r.objs("data_requirements"):
        d.text("dataset_id", pattern=_TOKEN)
        d.texts("fields")
        d.text("frequency", choices=FREQUENCIES)
        d.boolean("point_in_time", must_be=True, why="research reads only point-in-time data (ADR-0015)")
        d.text("rights_ref")
        d.boolean("fixture_allowed")
        d.done()

    u = r.obj("universe")
    u.text("rule")
    instrument_ids = u.texts("instrument_ids", pattern=_TOKEN)
    u.boolean("point_in_time_membership")
    u.text("survivorship_note")
    u.done()

    rules = r.obj("rules")
    rules.text("entry")
    rules.text("exit")
    s = rules.obj("sizing")
    s.text("method", choices=SIZING_METHODS)
    sizing = {
        "max_weight": s.dec("max_weight", ZERO, ONE, lo_open=True),
        "aggregate_cap": s.dec("aggregate_cap", ZERO, ONE, lo_open=True),
        "cash_reserve": s.dec("cash_reserve", ZERO, Fixed.parse("0.99")),
        "no_trade_band": s.dec("no_trade_band", ZERO, Fixed.parse("0.5")),
        "min_order_notional": s.dec("min_order_notional", ZERO),
    }
    s.done()
    if sizing["max_weight"] > sizing["aggregate_cap"]:
        raise SpecError("spec.rules.sizing.max_weight must not exceed aggregate_cap")
    hold = rules.obj("holding")
    min_hold = hold.integer("min_holding_sessions", 0, 10_000)
    max_hold = hold.opt_integer("max_holding_sessions", 1, 100_000)
    hold.done()
    if max_hold is not None and max_hold < min_hold:
        raise SpecError("spec.rules.holding.max_holding_sessions < min_holding_sessions")
    x = rules.obj("execution")
    execution = {
        "decision_clock_utc": x.text("decision_clock_utc", pattern=_TIME),
        "order_type": x.text("order_type", choices=frozenset({"LIMIT"})),
        "time_in_force": x.text("time_in_force", choices=frozenset({"DAY"})),
        "limit_offset_bps": x.integer("limit_offset_bps", 0, 2_000),
        "price_bound_source": x.text("price_bound_source"),
        "execution_delay_sessions": x.integer("execution_delay_sessions", 1, 5),
        "participation_cap": x.dec("participation_cap", ZERO, ONE, lo_open=True),
        "same_bar_fill": x.boolean("same_bar_fill", must_be=False,
                                   why="a bar's close cannot both generate a signal and fill at that close (CAP-RES-04)"),
        "unfilled_policy": x.text("unfilled_policy", choices=frozenset({"EXPIRE_END_OF_DAY"})),
    }
    x.done()
    rules.done()

    c = r.obj("costs")
    y_values = tuple(_dec(v, f"spec.costs.impact_y_values[{i}]", ZERO, Fixed(10))
                     for i, v in enumerate(c.texts("impact_y_values", min_items=2, max_items=20)))
    costs = {
        "fee_schedule_id": c.text("fee_schedule_id", pattern=_TOKEN),
        "spread_bps": c.dec("spread_bps", ZERO, Fixed(1000)),
        "slippage_bps": c.dec("slippage_bps", ZERO, Fixed(1000)),
        "impact_y_values": y_values,
        "impact_y_default": c.dec("impact_y_default", ZERO, Fixed(10)),
        "third_party_sell_bps": c.dec("third_party_sell_bps", ZERO, Fixed(100)),
        "expected_cost_bps_per_unit_turnover": c.dec("expected_cost_bps_per_unit_turnover", ZERO, Fixed(10_000)),
    }
    c.done()
    if costs["fee_schedule_id"] not in FORMULAS:
        raise SpecError("spec.costs.fee_schedule_id must name a registered fee schedule formula")
    if len(set(y_values)) < 2:
        raise SpecError("spec.costs.impact_y_values needs at least two distinct Y values (impact is a scenario model)")
    if costs["impact_y_default"] not in y_values:
        raise SpecError("spec.costs.impact_y_default must be one of impact_y_values")

    cap = r.obj("capacity")
    cap.text("method")
    cap.dec("estimate", ZERO)
    cap.text("currency", pattern=re.compile(r"^[A-Z]{3}$"))
    cap.done()
    liq = r.obj("liquidity")
    liq.dec("min_adv_shares", ZERO)
    max_part = liq.dec("max_participation", ZERO, ONE, lo_open=True)
    liq.done()
    if execution["participation_cap"] > max_part:
        raise SpecError("spec.rules.execution.participation_cap exceeds spec.liquidity.max_participation")
    r.texts("operational_constraints")

    rl = r.obj("risk_limits")
    gross = rl.dec("max_gross_exposure", ZERO, ONE, lo_open=True)
    single = rl.dec("max_single_name_weight", ZERO, ONE, lo_open=True)
    rl.dec("max_drawdown_stop", ZERO, ONE, lo_open=True)
    rl.boolean("long_only", must_be=True, why="the initial cash-equity policy is long-only (RSK-01)")
    rl.boolean("allow_short", must_be=False, why="shorting is not released (RSK-01)")
    rl.boolean("allow_margin", must_be=False, why="borrowing is not released (RSK-01)")
    rl.done()
    if sizing["max_weight"] > single or sizing["aggregate_cap"] > gross:
        raise SpecError("spec.rules.sizing exceeds spec.risk_limits")
    r.texts("failure_conditions")

    vp = r.obj("validation_plan")
    dev = vp.obj("development")
    dev_range = (dev.day("start"), dev.day("end"))
    dev.done()
    ho = vp.obj("untouched_holdout")
    holdout = (ho.day("start"), ho.day("end"))
    ho.done()
    wf = vp.obj("walk_forward")
    walk = {
        "train_sessions": wf.integer("train_sessions", 20, 100_000),
        "test_sessions": wf.integer("test_sessions", 5, 100_000),
        "expanding": wf.boolean("expanding"),
        "label_horizon_sessions": wf.integer("label_horizon_sessions", 1, 1_000),
        "purge_sessions": wf.integer("purge_sessions", 0, 10_000),
        "embargo_sessions": wf.integer("embargo_sessions", 0, 10_000),
    }
    wf.done()
    selection_metric = vp.text("selection_metric", choices=frozenset({"sharpe.per_period@1", "utility.ch20@1"}))
    vp.done()
    if not dev_range[0] < dev_range[1] < holdout[0] <= holdout[1]:
        raise SpecError("validation_plan must be chronological: development.start < development.end < "
                        "untouched_holdout.start <= untouched_holdout.end (no overlap)")
    if walk["purge_sessions"] < walk["label_horizon_sessions"] + execution["execution_delay_sessions"]:
        raise SpecError("walk_forward.purge_sessions must be >= label_horizon_sessions + execution_delay_sessions (BR-DAT-14)")

    baselines = []
    for b in r.objs("baselines"):
        baselines.append(b.text("id", pattern=_ID))
        b.text("description")
        b.done()
    if strategy_id in baselines:
        raise SpecError("a strategy cannot be its own baseline")

    sp = r.obj("sensitivity_plan")
    grid_raw = sp.get("parameter_grid")
    mults = tuple(_dec(v, f"spec.sensitivity_plan.cost_multipliers[{i}]", ZERO, Fixed(20), lo_open=True)
                  for i, v in enumerate(sp.texts("cost_multipliers", min_items=4, max_items=20)))
    sp.done()
    for need in REQUIRED_COST_MULTIPLIERS:
        if need not in mults:
            raise SpecError("spec.sensitivity_plan.cost_multipliers must include 0.5, 1, 2 and 3 (CAP-RES-04)")

    stress = []
    for st in r.objs("stress_plan"):
        stress.append({"id": st.text("id", pattern=_TOKEN), "description": st.text("description"),
                       "kind": st.text("kind", choices=STRESS_KINDS),
                       "value": _dec(st.get("value"), f"{st.where}.value", ZERO, Fixed(1000), lo_open=True)})
        st.done()
    if len({s_["id"] for s_ in stress}) != len(stress):
        raise SpecError("spec.stress_plan ids must be unique")

    evidence = r.texts("promotion_evidence", choices=EVIDENCE_KINDS)
    missing = REQUIRED_EVIDENCE - set(evidence)
    if missing:
        raise SpecError(f"spec.promotion_evidence is missing required kinds {sorted(missing)} (book ch. 20)")

    dom_r = r.obj("parameter_domain")
    domain: dict[str, tuple[str, Fixed, Fixed]] = {}
    for name in list(dom_r.d):
        if not _NAME.fullmatch(name):
            raise SpecError(f"spec.parameter_domain key {name!r} must be snake_case")
        p = dom_r.obj(name)
        kind = p.text("kind", choices=PARAM_KINDS)
        lo, hi = p.dec("min"), p.dec("max")
        p.done()
        if lo > hi:
            raise SpecError(f"spec.parameter_domain.{name}: min > max")
        if kind == "INTEGER" and not (lo.is_multiple_of(ONE) and hi.is_multiple_of(ONE)):
            raise SpecError(f"spec.parameter_domain.{name}: integer bounds required")
        domain[name] = (kind, lo, hi)
    dom_r.done()
    par_r = r.obj("parameters")
    params: dict[str, Fixed] = {}
    for name in list(par_r.d):
        if name not in domain:
            raise SpecError(f"spec.parameters.{name} has no entry in parameter_domain")
        params[name] = _in_domain(par_r.get(name), f"spec.parameters.{name}", domain[name])
    par_r.done()
    if set(params) != set(domain):
        raise SpecError(f"spec.parameters must set every domain parameter; missing {sorted(set(domain) - set(params))}")

    if not isinstance(grid_raw, dict) or not grid_raw:
        raise SpecError("spec.sensitivity_plan.parameter_grid must be a non-empty object")
    grid: dict[str, tuple[Fixed, ...]] = {}
    for name, values in grid_raw.items():
        if name not in domain:
            raise SpecError(f"spec.sensitivity_plan.parameter_grid.{name} is not a declared parameter")
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise SpecError(f"spec.sensitivity_plan.parameter_grid.{name} must list 1..50 values")
        vals = tuple(_in_domain(v, f"spec.sensitivity_plan.parameter_grid.{name}", domain[name]) for v in values)
        if len(set(vals)) != len(vals):
            raise SpecError(f"spec.sensitivity_plan.parameter_grid.{name} has duplicates")
        grid[name] = vals
    r.done()

    return StrategySpec(
        body=body, spec_hash=sha256_hex(canonical_json(body)), strategy_id=strategy_id, version=version, family=family,
        parameters=params, parameter_domain=domain, instrument_ids=instrument_ids, sizing=sizing, execution=execution,
        costs=costs, development=dev_range, holdout=holdout, walk_forward={**walk, "selection_metric": selection_metric},
        cost_multipliers=mults, parameter_grid=grid, stress_plan=tuple(stress), promotion_evidence=evidence,
        baselines=tuple(baselines))


def _in_domain(v: Any, where: str, dom: tuple[str, Fixed, Fixed]) -> Fixed:
    kind, lo, hi = dom
    x = _dec(v, where, lo, hi)
    if kind == "INTEGER" and not x.is_multiple_of(ONE):
        raise SpecError(f"{where} must be an integer")
    return x


def spec_hash(body: dict) -> str:
    return sha256_hex(canonical_json(body))
