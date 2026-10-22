"""Second validation path: replay the fast engine's orders through the REAL core (RES-05; CAP-RES-04).

The fast engine (``engine``) and the production core (``core.desk``) are two
independent implementations of the same cash-account rules. This module
drives a *temporary* core database, wired to the deterministic
``SimulatedBroker`` fixture exactly as the core test harness does, with every
order the engine submitted:

1. bootstrap a SIMULATED account with a replay policy whose limits never bind
   before the engine's own rules (fees: the same per-share/minimum/1% cap
   schedule with a zero estimate margin, so the core's reservation envelope
   equals the engine's);
2. each session: advance the clock, let the simulated broker settle what is
   due, reconcile (settlement is confirmed only from broker evidence,
   REC-04), then submit each engine order as a STRATEGY intent through the
   common pretrade pipeline, transmit it through the outbox, and have the
   broker fill exactly the engine's quantity and price with the engine's fee;
   unfilled remainders expire at the end of the day (TIF DAY);
3. after each session compare the core's session ledger with the engine:
   settled cash (CASH), the unsettled total (RECEIVABLE + PAYABLE +
   FEES_ACCRUED), positions (open lot quantities), and the ledger invariants
   (``Desk.verify() == []``); orders the engine rejected for cash-account
   reasons must also be rejected by the core;
4. at the end, settle everything and compare final cash, lot quantities, lot
   cost and every execution (quantity, price, notional, final commission).
   Because the core books whatever commission the broker reports, that last
   comparison is bookkeeping only; each engine commission is therefore also
   recomputed with the core's own fee model (``risk.fees.estimate``, an
   independent implementation of per-share/minimum/cap) and must match.

Rounding: the core rounds trade notional HALF_EVEN and splits lot cost
HALF_EVEN at the USD ledger scale (6 dp); the engine uses the same rules, and
fixture prices have at most 4 dp, so no rounding difference arises (the
report states this; any difference is a mismatch, not tolerated).

Nothing here touches a real account, the network or the owner's core
database: the database lives in a temporary directory that is deleted.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ..broker.simulated import SimulatedBroker
from ..core import ledger
from ..core.desk import Command, HealthSnapshot, open_desk
from ..domain.calendar import WEEKDAYS_ONLY
from ..domain.clock import ManualClock
from ..domain.fixed import Fixed, Rounding
from ..domain.ids import IdGenerator, SequenceRandom
from ..domain.instruments import Instrument
from ..domain.principal import Principal, Role
from ..gateway.runner import GatewayRunner
from ..domain.money import Money
from ..market.quotes import Quote
from ..policy.model import FeeModel
from ..reconcile.reconciler import Reconciler
from ..risk import fees as core_fees
from .common import NS_PER_S
from .engine import BacktestResult

ACCOUNT = "RESEARCH-REPLAY"
DEPLOYMENT = "research-replay"
SESSION = "RS-1"
STRATEGY_ID = "strategy.research-replay"
BIG = "1000000000.00"


def _cap_bps(fee_schedule: dict) -> int:
    bps = Fixed.parse(fee_schedule["max_fraction_of_value"]) * 10_000
    if not bps.is_multiple_of(Fixed(1)):
        raise ValueError("fee cap is not a whole number of basis points")
    return int(bps.rescale(0).mantissa)


def core_fee_model(fee_schedule: dict) -> FeeModel:
    """The core's own fee model (``risk.fees``) for the engine's schedule: an independent implementation of
    min(max(per_share x qty, minimum), cap) used to check every engine commission (review fix 2026-10-22)."""
    return FeeModel(per_share=Fixed.parse(fee_schedule["per_share"]), minimum=Money("USD", Fixed.parse(fee_schedule["minimum"])),
                    maximum_bps_of_notional=_cap_bps(fee_schedule), estimate_margin_bps=0,
                    source=fee_schedule.get("source", "research replay"), verified_on="research replay")


def replay_policy(initial_cash: Fixed, fee_schedule: dict) -> dict:
    """SIMULATED policy for the replay: the engine's cash-account rules decide, the core's generic limits do not bind."""
    cash = initial_cash.rescale(2, Rounding.FLOOR).to_str()
    return {
        "schema_version": 1, "name": "research-core-replay-v1", "mode": "SIMULATED", "currency": "USD",
        "eligibility": {"asset_classes": ["EQUITY", "ETF"], "venues": ["XNAS", "XNYS", "ARCX", "BATS", "XASE"],
                        "order_types": ["LIMIT"], "time_in_force": ["DAY"], "outside_regular_hours": False,
                        "allow_short": False, "allow_margin": False, "allow_derivatives": False,
                        "allow_leveraged_or_inverse": False, "allow_fractional": False, "allow_fx_conversion": False,
                        "allow_transfers": False},
        "account_limits": {"authorized_capital": cash, "operational_buffer": "0.00", "max_order_notional": BIG,
                           "max_position_bps_of_session_equity": 10000, "max_gross_exposure": BIG,
                           "max_orders_per_minute": 600, "price_collar_bps": 2000, "quote_max_age_ms": 600000,
                           "daily_loss_trigger": BIG, "cumulative_loss_allowance": BIG,
                           "risk_day_timezone": "America/New_York", "max_reconciliation_age_s": 86400,
                           "reauth_max_age_s": 86400, "max_clock_skew_ms": 600000, "min_free_disk_mb": 0,
                           "max_participation_bps_of_adv": 10000, "require_liquidity_data": False},
        "session_template": {"seed": cash, "target_terminal_equity": BIG, "strategy_eligibility": [STRATEGY_ID],
                             "session_drawdown_trigger_bps": 10000, "session_max_loss": BIG, "auto_start": True,
                             "expires_after_s": None, "below_target_after_target_observed": "CLOSE", "max_resumes": 0},
        "successor": {"create_on_success": True, "mandate_source": "ACTIVE_TEMPLATE_AT_CREATION",
                      "reinvest_protected_proceeds": False},
        "wind_down": {"order_style": "LIMIT_AT_BID_MINUS_COLLAR", "collar_bps": 50, "reprice_after_s": 300, "max_attempts": 5},
        "on_stop_liquidation": "NONE", "auto_resume_after_clean_reconciliation": False,
        "fees": {"model": "PER_SHARE", "per_share": fee_schedule["per_share"], "minimum": fee_schedule["minimum"],
                 "maximum_bps_of_notional": _cap_bps(fee_schedule), "estimate_margin_bps": 0,
                 "source": fee_schedule.get("source", "research replay"),
                 "verified_on": "research replay policy: mirrors the backtest fee schedule; not a live policy"},
        "settlement_calendar": "WEEKDAYS",
    }


def _at(day: date, hh: int, mm: int = 0) -> int:
    return int(datetime.combine(day, time(hh, mm), tzinfo=timezone.utc).timestamp()) * NS_PER_S


class _Core:
    def __init__(self, root: Path, initial_cash: Fixed, first_day: date):
        self.clock = ManualClock(_at(first_day - timedelta(days=1), 12))
        self.ids = IdGenerator(lambda: self.clock.now_ns() // 1_000_000, SequenceRandom(7))
        self.broker = SimulatedBroker(self.clock, initial_settled_cash=initial_cash, calendar=WEEKDAYS_ONLY, settlement_days=1)
        self.desk = open_desk(root / "replay-core.sqlite", self.clock, self.ids, deployment_id=DEPLOYMENT, quotes=None,
                              health=self.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY})
        self.runner = GatewayRunner(self.desk, self.broker, ACCOUNT)
        self.n = 0

    def health(self) -> HealthSnapshot:
        s = self.broker.session()
        return HealthSnapshot(s.connected, s.authenticated, s.server_time_ns, 50_000, "simulated broker (research replay)",
                              broker_account_mode=self.broker.capabilities().account_mode.value)

    def owner(self, kind: str, **payload):
        self.n += 1
        p = Principal("owner", Role.OWNER, authenticated_at_ns=self.clock.now_ns())
        return self.desk.execute(Command(f"replay.{kind}.{self.n}", kind, p, payload))

    def strategy(self, kind: str, **payload):
        self.n += 1
        return self.desk.execute(Command(f"replay.{kind}.{self.n}", kind, Principal(STRATEGY_ID, Role.STRATEGY), payload))

    def quote(self, instrument_id: str, bid: Fixed, ask: Fixed) -> None:
        now = self.clock.now_ns()
        self.desk.quotes.put(Quote(instrument_id, bid, ask, None, "USD", "research-replay:fixture", now, now, fixture=True))

    def reconcile(self):
        return Reconciler(self.desk, self.broker, ACCOUNT).run()

    def balance(self, code: str, owner: str) -> Fixed:
        with self.desk.store.read() as tx:
            return ledger.balance(tx, ACCOUNT, code, owner, "USD")

    def lots(self, owner: str) -> dict[str, tuple[Fixed, Fixed]]:
        out: dict[str, tuple[Fixed, Fixed]] = {}
        with self.desk.store.read() as tx:
            for lot in ledger.owner_lots(tx, ACCOUNT, owner):
                q, c = out.get(lot["instrument_id"], (Fixed(0), Fixed(0)))
                out[lot["instrument_id"]] = (q + Fixed.parse(lot["open_qty"]), c + Fixed.parse(lot["open_cost"]))
        return out


def replay_through_core(result: BacktestResult, instruments: dict[str, Instrument], *, workdir: Path | None = None) -> dict:
    """Replay ``result``'s orders through a temporary core and compare fills, cash and positions exactly."""
    if result.config["calendar"] != f"{WEEKDAYS_ONLY.calendar_id}@{WEEKDAYS_ONLY.version}":
        return {"comparable": False, "reason": "core replay supports the WEEKDAYS settlement calendar only",
                "mismatches": ["calendar not supported"], "ledger_problems": [], "fills_compared": 0}
    sched = result.config["cost_model"]["schedule"]
    if "per_share" not in sched or result.config["cost_model"]["multiplier"] not in ("1", "1.0"):
        return {"comparable": False, "reason": "core fee model expresses only the unscaled per-share schedule",
                "mismatches": ["fee schedule not expressible"], "ledger_problems": [], "fills_compared": 0}
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="research-core-replay-")
        workdir = Path(tmp.name)
    try:
        return _replay(result, instruments, workdir, sched)
    finally:
        if tmp is not None:
            tmp.cleanup()


def _replay(result: BacktestResult, instruments: dict[str, Instrument], root: Path, sched: dict) -> dict:
    initial = Fixed.parse(result.config["initial_cash"])
    days = sorted({e["date"] for e in result.events if e["type"] in ("ORDER_SUBMITTED", "ORDER_REJECTED", "MARK")})
    core = _Core(root, initial, date.fromisoformat(days[0]))
    try:
        return _compare(result, instruments, core, days, initial, sched)
    finally:
        core.desk.store.close()


def _compare(result: BacktestResult, instruments: dict[str, Instrument], core: _Core, days: list[str], initial: Fixed,
             sched: dict) -> dict:
    mismatches: list[str] = []
    ledger_problems: list[str] = []
    owner = ledger.session_owner(SESSION)

    def check(cond: bool, msg: str) -> None:
        if not cond:
            mismatches.append(msg)

    # ---------------------------------------------------------- bootstrap
    steps = [
        core.owner("register_account", account_key=ACCOUNT, provider="simulated", mode="SIMULATED", base_currency="USD"),
    ]
    pub = core.owner("publish_policy", body=replay_policy(initial, sched))
    steps.append(pub)
    if not pub.accepted:
        return {"comparable": False, "reason": f"core refused the replay policy: {pub.body}", "mismatches": ["policy refused"],
                "ledger_problems": [], "fills_compared": 0}
    policy_hash = pub.body["policy_hash"]
    steps.append(core.owner("activate_policy", account_key=ACCOUNT, policy_hash=policy_hash))
    for inst_id in sorted(instruments):
        steps.append(core.owner("register_instrument", instrument=instruments[inst_id].to_json(),
                                mappings=[{"provider": "simulated", "external_id": inst_id}]))
    steps.append(core.owner("acquire_execution_ownership", account_key=ACCOUNT, deployment_id=DEPLOYMENT))
    core.desk.run_system("broker_connectivity", {"account_key": ACCOUNT, "connected": True, "authenticated": True})
    rec = core.reconcile()
    steps.append(rec)
    steps.append(core.owner("authorize_capital", account_key=ACCOUNT, amount=initial.rescale(2, Rounding.FLOOR).to_str()))
    steps.append(core.owner("start", account_key=ACCOUNT))
    sess = core.owner("create_session", account_key=ACCOUNT, session_id=SESSION, seed=initial.rescale(2, Rounding.FLOOR).to_str())
    steps.append(sess)
    refused = [s.body for s in steps if not s.accepted]
    if refused or sess.body.get("state") != "RUNNING":
        return {"comparable": False, "reason": f"core bootstrap refused: {refused or sess.body}",
                "mismatches": ["bootstrap refused"], "ledger_problems": core.desk.verify(), "fills_compared": 0}

    orders = {o["order_id"]: o for o in result.orders}
    fills_by_order: dict[str, list[dict]] = {}
    for f in result.fills:
        fills_by_order.setdefault(f["order_id"], []).append(f)
    events_by_day: dict[str, list[dict]] = {}
    for e in result.events:
        events_by_day.setdefault(e["date"], []).append(e)

    compared_days = 0
    submitted = rejected_both = 0
    for day_s in days:
        day = date.fromisoformat(day_s)
        start = _at(day, 13)
        if start > core.clock.now_ns():
            core.clock.set(start)
        core.broker.advance_settlement()
        r = core.reconcile()
        check(r.accepted and r.body.get("status") == "CLEAN", f"{day_s}: start-of-day reconciliation not clean: {r.body}")
        for e in events_by_day.get(day_s, []):
            if e["type"] not in ("ORDER_SUBMITTED", "ORDER_REJECTED"):
                continue
            o = orders[e["order_id"]]
            if e["t_ns"] > core.clock.now_ns():
                core.clock.set(e["t_ns"])
            limit = Fixed.parse(o["limit_price"])
            tick = instruments[o["instrument_id"]].tick_for(limit)
            core.quote(o["instrument_id"], limit - tick, limit + tick)
            res = core.strategy("submit_intent", account_key=ACCOUNT, session_id=SESSION, order_id=o["order_id"],
                                instrument_id=o["instrument_id"], side=o["side"], quantity=o["quantity"],
                                limit_price=o["limit_price"], deadline_ns=core.clock.now_ns() + 60 * NS_PER_S,
                                policy_hash=policy_hash, strategy_id=STRATEGY_ID)
            if e["type"] == "ORDER_REJECTED":
                if res.accepted:
                    mismatches.append(f"{day_s}: engine rejected {o['order_id']} ({e['reason']}) but the core accepted it")
                else:
                    rejected_both += 1
                    expected = {"INSUFFICIENT_SETTLED_CASH": "CASH", "NO_SHORTING": "OWNED_QUANTITY"}.get(e["reason"])
                    check(res.rejected_code == expected, f"{day_s}: {o['order_id']} rejection code {res.rejected_code} "
                                                         f"!= {expected} for engine reason {e['reason']}")
                continue
            if not res.accepted:
                mismatches.append(f"{day_s}: core rejected {o['order_id']} that the engine accepted: {res.body}")
                continue
            submitted += 1
            core.runner.pump_outbox()
            for f in fills_by_order.get(o["order_id"], []):
                core.broker.fill(o["order_id"], Fixed.parse(f["quantity"]), Fixed.parse(f["price"]),
                                 commission=Fixed.parse(f["fees"]["total"]), exec_time_ns=core.clock.now_ns())
            core.runner.drain_events()
        core.broker.expire_day_orders()
        core.runner.pump()
        mark = next((e for e in events_by_day.get(day_s, []) if e["type"] == "MARK"), None)
        if mark is not None:
            compared_days += 1
            cash = core.balance(ledger.CASH, owner)
            unsettled = core.balance(ledger.RECEIVABLE, owner) + core.balance(ledger.PAYABLE, owner) + \
                core.balance(ledger.FEES_ACCRUED, owner)
            check(cash == Fixed.parse(mark["settled_cash"]), f"{day_s}: core settled cash {cash} != engine {mark['settled_cash']}")
            check(unsettled == Fixed.parse(mark["unsettled"]), f"{day_s}: core unsettled {unsettled} != engine {mark['unsettled']}")
            lots = core.lots(owner)
            for inst_id, q in mark["positions"].items():
                check(lots.get(inst_id, (Fixed(0), Fixed(0)))[0] == Fixed.parse(q),
                      f"{day_s}: core position {inst_id} {lots.get(inst_id, (Fixed(0),))[0]} != engine {q}")
        problems = core.desk.verify()
        if problems:
            ledger_problems.extend(f"{day_s}: {p}" for p in problems)

    # ---------------------------------------------------- settle everything
    last_due = max([date.fromisoformat(u["settle_date"]) for u in result.final["unsettled"]] or [date.fromisoformat(days[-1])])
    day = date.fromisoformat(days[-1])
    while day < last_due:
        day += timedelta(days=1)
        if WEEKDAYS_ONLY.is_business_day(day):
            core.clock.set(max(core.clock.now_ns(), _at(day, 13)))
            core.broker.advance_settlement()
            r = core.reconcile()
            check(r.accepted and r.body.get("status") == "CLEAN", f"{day}: settlement reconciliation not clean: {r.body}")
    expected_cash = Fixed.parse(result.final["settled_cash"]) + sum((Fixed.parse(u["amount"]) for u in result.final["unsettled"]), Fixed(0))
    final_cash = core.balance(ledger.CASH, owner)
    check(final_cash == expected_cash, f"final: core cash {final_cash} != engine cash after settlement {expected_cash}")
    lots = core.lots(owner)
    for inst_id, pos in result.final["positions"].items():
        q, c = lots.get(inst_id, (Fixed(0), Fixed(0)))
        check(q == Fixed.parse(pos["quantity"]), f"final: {inst_id} quantity {q} != {pos['quantity']}")
        check(c == Fixed.parse(pos["cost"]), f"final: {inst_id} lot cost {c} != {pos['cost']}")
    core_execs = sorted((json.loads(r["row"]) for r in core.desk.store.query("SELECT row FROM executions")),
                        key=lambda x: (x["exec_time_ns"], x["exec_key"]))
    check(len(core_execs) == len(result.fills), f"core has {len(core_execs)} executions, engine {len(result.fills)} fills")
    for ce, ef in zip(core_execs, sorted(result.fills, key=lambda f: (f["exec_time_ns"], f["fill_id"]))):
        check(ce["order_id"] == ef["order_id"] and Fixed.parse(ce["quantity"]) == Fixed.parse(ef["quantity"])
              and Fixed.parse(ce["price"]) == Fixed.parse(ef["price"]) and Fixed.parse(ce["notional"]) == Fixed.parse(ef["notional"]),
              f"execution {ce['exec_key']} differs from engine fill {ef['fill_id']}")
        check(ce.get("fee_final") is not None and Fixed.parse(ce["fee_final"]) == Fixed.parse(ef["fees"]["total"]),
              f"execution {ce['exec_key']} final fee {ce.get('fee_final')} != engine {ef['fees']['total']}")
    # The core books the commission the broker reports, so the check above is bookkeeping only. The engine's
    # commission is checked independently against the core's fee model (risk.fees.estimate, with the cap).
    model = core_fee_model(sched)
    fees_checked = 0
    for ef in result.fills:
        core_fee = core_fees.estimate(model, Fixed.parse(ef["quantity"]), Money("USD", Fixed.parse(ef["notional"]))).amount
        check(core_fee == Fixed.parse(ef["fees"]["commission"]),
              f"fill {ef['fill_id']}: engine commission {ef['fees']['commission']} != core fee model {core_fee.to_str()}")
        fees_checked += 1
    problems = core.desk.verify()
    ledger_problems.extend(f"final: {p}" for p in problems)
    open_exc = [json.loads(r["row"])["kind"] for r in core.desk.store.query("SELECT row FROM exceptions WHERE status='OPEN'")]
    check(not open_exc, f"core opened exceptions during replay: {open_exc}")
    return {"comparable": True, "mode": "core replay: SIMULATED account on a temporary database with the SimulatedBroker fixture",
            "fills_compared": len(result.fills), "commissions_checked_against_core_fee_model": fees_checked,
            "orders_submitted": submitted, "rejections_matched": rejected_both,
            "days_compared": compared_days, "mismatches": mismatches, "ledger_problems": ledger_problems,
            "final": {"cash": final_cash.to_str(), "positions": {k: [v[0].to_str(), v[1].to_str()] for k, v in sorted(lots.items())}},
            "rounding": "none observed: prices <= 4 dp and integer quantities are exact at the 6 dp ledger scale; the core's "
                        "HALF_EVEN notional and lot-cost rounding is mirrored by the engine",
            "event_log_hash": result.event_log_hash}
