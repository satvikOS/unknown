"""Seeded randomized scenarios: replay equivalence and economic conservation.

ADR-0005 #3 makes replay equivalence a tested property; book Ch. 28 asks for
generated legal sequences (reserve, partial fill, cancel request, late fill,
cancel confirmation, sell, settlement, completion, successor, restart) with
conservation and ownership checked at every step. Each scenario here drives a
desk through a seeded mix of intents, fills (partial, duplicated, delayed
commission), deferred cancels, scripted submit/cancel faults, out-of-order
callbacks, quote moves, valuations, ticks, reconciliations, settlement days,
process restarts, red stops and green starts, and asserts after EVERY step that
the ledger invariants hold (``Harness.verify() == []``). At the end it checks:

* a final reconciliation is CLEAN (no exception is deliberately injected that a
  reconciliation cannot clear);
* conservation after a clean reconciliation: the ledger's CASH summed over every
  owner equals the broker's settled cash, and the open lot quantities sum to the
  broker position;
* an independent shadow tally of every fill the test caused (qty, price,
  commission) equals both the ledger's liquid total over all owners and the
  broker's total cash, and the net filled quantity equals the lot total;
* replaying the event log into an empty database reproduces every projection
  byte for byte (``core.replay.replay_and_compare``).

Every number is a TEST FIXTURE. The simulated broker is a deterministic fixture
(BRK-05); passing these tests is not evidence about any real broker.
"""
from __future__ import annotations

import json
import random
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402

from unknown.broker.port import BrokerOrderStatus  # noqa: E402
from unknown.broker.simulated import CancelBehavior, SimulatedBroker, SubmitBehavior  # noqa: E402
from unknown.core.desk import Command  # noqa: E402
from unknown.core.replay import replay_and_compare, table_dump  # noqa: E402
from unknown.core.state import OPEN_ORDER_STATES, TERMINAL_ORDER_STATES  # noqa: E402
from unknown.domain.calendar import local_date  # noqa: E402
from unknown.domain.errors import CommandConflict  # noqa: E402
from unknown.domain.fixed import Fixed, Rounding  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.core import ledger  # noqa: E402

NS = 1_000_000_000
INST = "FIX-EQ-1"
INITIAL_CASH = "400.00"          # broker settled cash at import (fixture)
AUTHORIZED = "300.00"            # authorized to the system; 100.00 stays 'outside'
SEEDS = range(1, 25)             # 24 seeds
STEPS = 70                       # random steps per seed (plus quiesce and settlement)

NON_TERMINAL_SESSION_STATES = ("REQUESTED", "WAITING_FUNDS", "READY", "RUNNING", "SUSPENDED", "WINDING_DOWN",
                               "RECONCILING", "FAILED_REVIEW")
# Exception kinds an owner may acknowledge in these scenarios without changing money state.
ACKNOWLEDGEABLE = ("EXTERNAL_TRADE", "POST_COMPLETION_ADJUSTMENT", "SETTLEMENT_LATE", "RECONCILIATION_INCOMPLETE")


def policy_variant(seed: int) -> dict:
    """Three fixture policies so risk stops, completions and below-target resumes all occur."""
    v = seed % 3
    if v == 0:
        return load_policy()  # shipped defaults: commissions trip the daily loss budget quickly
    loose = dict(account_limits__daily_loss_trigger="40.00", account_limits__cumulative_loss_allowance="80.00",
                 session_template__session_drawdown_trigger_bps=1500, session_template__session_max_loss="30.00")
    if v == 1:
        return load_policy(session_template__target_terminal_equity="100.10", **loose)
    return load_policy(session_template__target_terminal_equity="100.05", auto_resume_after_clean_reconciliation=True,
                       session_template__below_target_after_target_observed="RESUME",
                       session_template__max_resumes=1, **loose)


class Shadow:
    """Independent tally of every execution the test caused at the broker (signed cash, quantity)."""

    def __init__(self, initial_cash: str):
        self.cash = Fixed.parse(initial_cash)
        self.qty = Fixed(0)
        self.fills = 0

    def record(self, side: str, qty: Fixed, price: Fixed, commission: Fixed) -> None:
        notional = qty * price
        if side == "BUY":
            self.cash = self.cash - notional - commission
            self.qty = self.qty + qty
        else:
            self.cash = self.cash + notional - commission
            self.qty = self.qty - qty
        self.fills += 1

    def correct(self, side: str, old_qty: Fixed, old_price: Fixed, new_qty: Fixed, new_price: Fixed) -> None:
        """A broker correction replaces the original's quantity and price; its commission is not re-charged."""
        sign = Fixed(1) if side == "BUY" else Fixed(-1)
        self.cash = self.cash + sign * (old_qty * old_price - new_qty * new_price)
        self.qty = self.qty + sign * (new_qty - old_qty)


class Scenario:
    """One seeded random run against a fresh Harness."""

    def __init__(self, test: unittest.TestCase, seed: int, steps: int):
        self.t = test
        self.seed = seed
        self.steps = steps
        self.rng = random.Random(seed * 7919 + 17)
        self.h = Harness(initial_cash=INITIAL_CASH, policy=policy_variant(seed), seed=seed)
        self.mid = 1000                    # cents; quotes are mid -/+ 1 cent
        self.bull = seed % 3 == 2          # cheap commissions and a rally-biased walk: targets get observed
        self.shadow = Shadow(INITIAL_CASH)
        self.trace: list[str] = []
        self.last_cmd: Command | None = None
        self.next_session = 1
        self.next_order = 1
        self.replays = 0
        self.completed = 0
        self.corrected_at_broker: set[str] = set()
        self.clean_recons = 0
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------- plumbing
    def fail(self, msg: str) -> None:
        tail = "\n  ".join(self.trace[-25:])
        self.t.fail(f"seed {self.seed}: {msg}\n  last steps:\n  {tail}")

    def note(self, text: str) -> None:
        self.trace.append(text)

    def count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def cmd(self, kind: str, principal: Principal | None = None, **payload):
        h = self.h
        if principal is None:
            h.owner = Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns())
            principal = h.owner
        h.n += 1
        c = Command(f"rt.{self.seed}.{kind}.{h.n}", kind, principal, payload)
        self.last_cmd = c
        return h.desk.execute(c)

    def requote(self) -> None:
        bid = Fixed(self.mid - 1, 2).to_str()
        ask = Fixed(self.mid + 1, 2).to_str()
        self.h.quote(bid, ask)

    def advance(self, seconds: int) -> None:
        self.h.clock.advance(seconds * NS)
        self.requote()

    def event_count(self) -> int:
        return self.h.desk.store.query("SELECT COUNT(*) AS n FROM events")[0]["n"]

    def sessions(self, states: tuple[str, ...]) -> list[dict]:
        return [s for s in self.h.sessions() if s["state"] in states]

    def local_open_orders(self) -> list[dict]:
        rows = self.h.desk.store.query("SELECT row FROM orders WHERE account_key=? ORDER BY rowid", (ACCOUNT,))
        return [o for o in (json.loads(r["row"]) for r in rows) if o["state"] in OPEN_ORDER_STATES]

    def unreserved(self, session_id: str) -> Fixed:
        owner = ledger.session_owner(session_id)
        with self.h.desk.store.read() as tx:
            return ledger.owned_quantity(tx, ACCOUNT, owner, INST) - ledger.active_quantity_holds(tx, ACCOUNT, owner, INST)

    def report_connectivity(self) -> None:
        """What a gateway reports after a process start: the broker session as it really is."""
        s = self.h.broker.session()
        self.h.desk.run_system("broker_connectivity", {"account_key": ACCOUNT, "connected": s.connected,
                                                       "authenticated": s.authenticated})

    def connected(self) -> bool:
        return self.h.broker.session().connected

    def broker_working(self) -> list:
        if not self.connected():
            return []
        rows, _ = self.h.broker.open_orders()
        return [r for r in rows if r.client_order_id is not None]

    def intent(self, session_id: str, side: str, qty: int, limit_cents: int):
        sess = self.h.row("sessions", "session_id", session_id)
        mandate = json.loads(self.h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?",
                                                     (sess["mandate_hash"],))[0]["body"])
        order_id = f"R{self.seed}-{self.next_order}"
        self.next_order += 1
        r = self.cmd("submit_intent", account_key=ACCOUNT, session_id=session_id, order_id=order_id,
                     instrument_id=INST, side=side, quantity=str(qty), limit_price=Fixed(limit_cents, 2).to_str(),
                     deadline_ns=self.h.clock.now_ns() + 60 * NS, policy_hash=mandate["policy_hash"])
        self.note(f"intent {order_id} {side} {qty}@{Fixed(limit_cents, 2)} in {session_id[:14]} -> "
                  f"{'ACCEPTED' if r.accepted else r.rejected_code}")
        self.count("intent_accepted" if r.accepted else "intent_rejected")
        return r

    # ------------------------------------------------------------- invariants
    def check_invariants(self, where: str) -> None:
        problems = self.h.verify()
        if problems:
            self.fail(f"ledger invariants broken after {where}: {problems[:5]}")
        self.check_holds(where)
        self.check_finished_sessions(where)

    def check_holds(self, where: str) -> None:
        """ADR-0006/0009, ORD-04, LED-05: every hold equals the reservation implied by its order right now.

        Recomputed independently: a non-terminal BUY holds ceil(leaves x limit) + unused fee envelope, a
        non-terminal SELL holds leaves_qty shares, and only a terminal order's hold is released (a pending
        cancel releases nothing)."""
        holds = {r["hold_id"]: r for r in self.h.desk.store.query("SELECT hold_id, kind, amount, status FROM holds")}
        for r in self.h.desk.store.query("SELECT row FROM orders"):
            o = json.loads(r["row"])
            suffix = "cash" if o["side"] == "BUY" else "qty"
            hold = holds.get(f"{o['order_id']}:{suffix}")
            if hold is None:
                self.fail(f"{where}: order {o['order_id']} has no hold")
            if o["state"] in TERMINAL_ORDER_STATES:
                if hold["status"] != "RELEASED" or not Fixed.parse(hold["amount"]).is_zero():
                    self.fail(f"{where}: terminal order {o['order_id']} ({o['state']}) still holds {hold['amount']}")
                continue
            if o["side"] == "BUY":
                leaves, limit = Fixed.parse(o["leaves_qty"]), Fixed.parse(o["limit_price"])
                envelope, used = Fixed.parse(o["fee_envelope"]), Fixed.parse(o["fees_estimated"])
                expected = (leaves * limit).rescale(6, Rounding.CEILING) + (envelope - used if envelope > used else Fixed(0))
            else:
                expected = Fixed.parse(o["leaves_qty"])
            if hold["status"] != "ACTIVE" or Fixed.parse(hold["amount"]) != expected:
                self.fail(f"{where}: order {o['order_id']} ({o['state']}, cancel {o['cancel_state']}) hold "
                          f"{hold['status']} {hold['amount']} != expected {expected}")

    def check_finished_sessions(self, where: str) -> None:
        """Ch. 28 core property, SES-05: a completed or closed session has no position and no open order."""
        for s in self.sessions(("COMPLETED_UNSETTLED", "COMPLETED_SETTLED", "CLOSED")):
            sid = s["session_id"]
            with self.h.desk.store.read() as tx:
                lots = ledger.owner_lots(tx, ACCOUNT, ledger.session_owner(sid))
            open_orders = [o["order_id"] for o in self.local_open_orders() if o["session_id"] == sid]
            if lots or open_orders:
                self.fail(f"{where}: finished session {sid} ({s['state']}) has lots {lots} / open orders {open_orders}")

    def check_protected_never_spent(self) -> None:
        """SES-07/SES-09: protected proceeds are never transferred out (no owner transfer debits a protected owner)."""
        rows = self.h.desk.store.query(
            "SELECT p.entry_id, p.owner, p.account_code, p.amount FROM postings p JOIN journal_entries j ON j.entry_id=p.entry_id "
            "WHERE j.kind='OWNER_TRANSFER' AND p.owner LIKE 'protected:%'")
        per_entry: dict[tuple, Fixed] = {}
        for r in rows:
            k = (r["entry_id"], r["owner"])
            per_entry[k] = per_entry.get(k, Fixed(0)) + Fixed.parse(r["amount"])
        for (entry_id, owner), net in per_entry.items():
            if net.is_negative():
                self.fail(f"owner transfer {entry_id} moved {net} out of {owner}")

    def check_replay(self, where: str) -> None:
        self.replays += 1
        target = Path(self.h.tmp.name) / f"replay-{self.replays}.sqlite"
        diffs = replay_and_compare(self.h.db_path, target)
        if diffs:
            self.fail(f"replay differs from live projections after {where}: {diffs[:3]}")

    def check_conservation(self, where: str) -> None:
        """After a CLEAN reconciliation: ledger CASH == broker settled; lots == broker position."""
        snap = self.h.broker.account_snapshot()
        broker_settled = sum((c.settled for c in snap.cash if c.currency == "USD"), Fixed(0))
        broker_pos = sum((p.quantity for p in snap.positions if p.instrument_id == INST), Fixed(0))
        with self.h.desk.store.read() as tx:
            ledger_cash = ledger.code_total(tx, ACCOUNT, ledger.CASH, "USD")
            lot_qty = ledger.position_quantity(tx, ACCOUNT, INST)
        if ledger_cash != broker_settled:
            self.fail(f"{where}: ledger CASH over all owners {ledger_cash} != broker settled {broker_settled}")
        if lot_qty != broker_pos:
            self.fail(f"{where}: lot quantity {lot_qty} != broker position {broker_pos}")

    def reconcile(self, why: str):
        r = self.h.reconcile()
        status = r.body.get("status") if r.accepted else r.rejected_code
        self.note(f"reconcile ({why}) -> {status} {[d['kind'] for d in r.body.get('differences', [])] if r.accepted else ''}")
        self.count(f"recon_{status}")
        if r.accepted and status == "CLEAN" and self.connected():
            self.clean_recons += 1
            self.check_conservation(f"clean reconciliation ({why})")
        return r

    # ------------------------------------------------------------- steps
    def step_buy(self) -> None:
        running = self.sessions(("RUNNING",))
        if not running:
            return self.step_tick()
        s = self.rng.choice(running)
        self.intent(s["session_id"], "BUY", self.rng.randint(1, 5), self.mid + self.rng.randint(-4, 4))
        if self.rng.random() < 0.6:
            self.step_pump()

    def step_sell(self) -> None:
        cands = [s for s in self.sessions(("RUNNING", "SUSPENDED", "WINDING_DOWN"))
                 if self.unreserved(s["session_id"]).is_positive()]
        if not cands:
            return self.step_buy()
        s = self.rng.choice(cands)
        free = int(self.unreserved(s["session_id"]).to_str().split(".")[0])
        self.intent(s["session_id"], "SELL", self.rng.randint(1, free), self.mid + self.rng.randint(-4, 4))
        if self.rng.random() < 0.6:
            self.step_pump()

    def step_pump(self) -> None:
        st = self.h.pump()
        self.note(f"pump sent={st.sent} skipped={st.skipped} events={st.events}")

    def step_fill(self, pump: bool | None = None) -> None:
        working = self.broker_working()
        if not working and self.h.runner.pending_outbox():
            self.step_pump()
            working = self.broker_working()
        if not working:
            return self.step_buy()
        row = self.rng.choice(working)
        order = self.h.row("orders", "order_id", row.client_order_id)
        limit = Fixed.parse(order["limit_price"])
        remaining = int(row.remaining_quantity.to_str().split(".")[0])
        qty = remaining if self.rng.random() < 0.45 else self.rng.randint(1, remaining)
        # A marketable limit order executes at the market when that is better than its limit.
        offset = Fixed(self.rng.randint(0, 3), 2)
        if row.side == "BUY":
            ask = Fixed(self.mid + 1, 2)
            price = (ask if ask < limit else limit) - offset
        else:
            bid = Fixed(self.mid - 1, 2)
            price = (bid if bid > limit else limit) + offset
        if not price.is_positive():
            price = limit
        dup = self.rng.choice((0, 0, 0, 1, 2))
        delay = self.rng.random() < 0.3
        fee = self.commission(Fixed(qty))
        self.h.broker.fill(row.client_order_id, Fixed(qty), price, duplicate_callbacks=dup, delay_commission=delay,
                           commission=fee)
        self.shadow.record(row.side, Fixed(qty), price, fee)
        self.note(f"fill {row.client_order_id} {row.side} {qty}/{remaining} @{price} fee={fee} dup={dup} delay_fee={delay}")
        self.count("fill_partial" if qty < remaining else "fill_full")
        if dup:
            self.count("fill_duplicated")
        if delay:
            self.count("fill_fee_delayed")
        if pump is None:
            pump = self.rng.random() < 0.4
        if pump:
            self.step_pump()

    def commission(self, qty: Fixed) -> Fixed:
        """Broker-reported commission: the fixture's default schedule, or a scripted amount below or above
        the core's estimate (estimate-vs-final adjustments in both directions, LED-07)."""
        roll = self.rng.random()
        if self.bull and roll < 0.85:
            return Fixed(self.rng.randint(1, 20), 2)
        if roll < 0.4:
            return SimulatedBroker.default_commission(qty)
        if roll < 0.8:
            return Fixed(self.rng.randint(5, 60), 2)
        return Fixed(self.rng.randint(61, 180), 2)

    def step_release_commissions(self) -> None:
        n = self.h.broker.release_commissions()
        self.note(f"release {n} delayed commissions")

    def step_cancel(self) -> None:
        open_orders = [o for o in self.local_open_orders() if o["state"] != "RESERVED" or self.rng.random() < 0.3]
        if not open_orders:
            return self.step_fill()
        o = self.rng.choice(open_orders)
        defer = self.rng.random() < 0.5
        self.h.broker.defer_cancel_confirmation(defer)
        r = self.cmd("request_cancel", order_id=o["order_id"], reason="randomized")
        self.note(f"cancel {o['order_id']} ({o['state']}) defer={defer} -> {r.body.get('state') if r.accepted else r.rejected_code}")
        self.count("cancel_deferred" if defer else "cancel")
        if self.rng.random() < 0.7:
            self.step_pump()

    def step_confirm_cancel(self) -> None:
        pending = [r for r in self.broker_working() if r.status is BrokerOrderStatus.PENDING_CANCEL]
        if not pending:
            return self.step_cancel()
        row = self.rng.choice(pending)
        ok = self.h.broker.confirm_cancel(row.client_order_id)
        self.note(f"confirm cancel {row.client_order_id} -> {ok}")
        self.count("cancel_confirmed_late")

    def step_quote(self) -> None:
        lo, hi = (-6, 6) if self.seed % 3 == 0 else (-6, 18)
        roll = self.rng.random()
        if roll < (0.35 if self.bull else 0.15):
            lo, hi = 20, 45     # rally: targets observed, wind-downs, completions, successors
        elif roll < 0.22:
            lo, hi = -35, -15   # selloff: drawdown suspensions and loss-budget stops
        self.mid = max(800, min(1400, self.mid + self.rng.randint(lo, hi)))
        self.requote()
        self.note(f"quote mid {Fixed(self.mid, 2)}")

    def step_tick(self) -> None:
        self.h.tick()
        self.note("tick " + ",".join(f"{s['session_id'][:10]}:{s['state']}" for s in self.h.sessions()))

    def step_reconcile(self) -> None:
        self.reconcile("random")

    def step_new_day(self) -> None:
        # DAY orders expire at the close; delayed commission reports arrive by the end of the trading
        # day (a commission withheld past settlement is a CASH_MISMATCH by design, not tested here).
        released = self.h.broker.release_commissions()
        expired = self.h.broker.expire_day_orders()
        self.h.clock.advance(24 * 3600 * NS)
        while datetime.fromtimestamp(self.h.clock.now_ns() // NS, tz=timezone.utc).weekday() >= 5:
            self.h.clock.advance(24 * 3600 * NS)
        settled = self.h.broker.advance_settlement()
        self.requote()
        self.note(f"new day: released={released} expired={expired} broker settled {settled} movements")
        self.count("new_day")
        if self.rng.random() < 0.7:
            self.step_pump()

    def step_restart(self) -> None:
        self.h.restart()
        self.report_connectivity()
        if self.rng.random() < 0.85:
            self.requote()  # quotes are not events: the new process has none until market data resubscribes
        self.note(f"restart -> epoch {self.h.desk.store.epoch}")
        self.count("restart")
        self.check_invariants("restart")
        self.check_replay("restart")
        if self.rng.random() < 0.75:
            self.step_start(procedure=True)

    def step_stop(self) -> None:
        r = self.cmd("stop", account_key=ACCOUNT, reason="randomized owner stop")
        self.note(f"STOP -> canceled {r.body.get('entries_canceled')}")
        self.count("stop")

    def step_start(self, procedure: bool | None = None) -> None:
        if procedure is None:
            procedure = self.rng.random() < 0.7
        if procedure:
            if not self.connected():
                self.h.broker.reconnect()
                self.step_pump()
            self.reconcile("green-start preflight")
        r = self.cmd("start", account_key=ACCOUNT)
        self.note(f"START -> {'granted' if r.accepted else r.body.get('message', r.rejected_code)}")
        self.count("start_granted" if r.accepted else "start_refused")

    def step_session_admin(self) -> None:
        live = self.sessions(NON_TERMINAL_SESSION_STATES)
        roll = self.rng.random()
        if len(live) < 2 and roll < 0.5:
            sid = f"S{self.seed}x{self.next_session}"
            self.next_session += 1
            r = self.cmd("create_session", account_key=ACCOUNT, session_id=sid)
            self.note(f"create session {sid} -> {r.body.get('state') if r.accepted else r.rejected_code}")
            return
        running = [s for s in live if s["state"] in ("RUNNING", "SUSPENDED")]
        if running and roll < 0.75:
            s = self.rng.choice(running)
            if self.rng.random() < 0.2:
                kind = "end_session"
            else:
                kind = "suspend_session" if s["state"] == "RUNNING" else "resume_session"
            r = self.cmd(kind, session_id=s["session_id"])
            self.note(f"{kind} {s['session_id'][:14]} -> {r.body.get('state') if r.accepted else r.rejected_code}")
            return
        waiting = [s for s in live if s["state"] in ("REQUESTED", "WAITING_FUNDS", "READY")]
        if waiting and roll < 0.85:
            s = self.rng.choice(waiting)
            r = self.cmd("cancel_session", session_id=s["session_id"])
            self.note(f"cancel_session {s['session_id'][:14]} -> {r.body.get('state') if r.accepted else r.rejected_code}")
            return
        cases = [e for e in self.h.open_exceptions() if e["kind"] in ACKNOWLEDGEABLE]
        if cases:
            e = self.rng.choice(cases)
            r = self.cmd("resolve_exception", case_id=e["case_id"], action="acknowledge", note="randomized owner review")
            self.note(f"acknowledge {e['kind']} -> {r.accepted}")
            return
        self.step_tick()

    def step_submit_fault(self) -> None:
        b = self.rng.choice((SubmitBehavior.TIMEOUT_AFTER_ACCEPT, SubmitBehavior.TIMEOUT_BEFORE_ACCEPT,
                             SubmitBehavior.REJECT, SubmitBehavior.RAISE))
        self.h.broker.script_submit(b)
        self.note(f"script next submit: {b.value}")
        self.count(f"fault_submit_{b.value}")
        self.step_buy()
        self.step_pump()

    def step_cancel_fault(self) -> None:
        b = self.rng.choice((CancelBehavior.REJECT_TOO_LATE, CancelBehavior.TIMEOUT))
        self.h.broker.script_cancel(b)
        self.note(f"script next cancel: {b.value}")
        self.count(f"fault_cancel_{b.value}")
        self.step_cancel()

    def step_shuffle(self) -> None:
        if self.h.broker.pending_event_count() < 2:
            self.step_fill(pump=False)
        n = self.h.broker.pending_event_count()
        if n < 2:
            return self.step_pump()
        order = list(range(n))
        self.rng.shuffle(order)
        self.h.broker.shuffle_pending_events(order)
        self.note(f"shuffle {n} pending broker events {order}")
        self.count("shuffle")
        self.step_pump()

    def step_external(self) -> None:
        qty = Fixed(self.rng.randint(1, 2))
        price = Fixed(self.mid, 2)
        self.h.broker.external_trade(INST, "BUY", qty, price)
        self.shadow.record("BUY", qty, price, SimulatedBroker.default_commission(qty))
        self.note(f"external manual BUY {qty} @{price}")
        self.count("external_trade")
        self.step_pump()

    def step_connectivity(self) -> None:
        if self.connected():
            working = self.broker_working()
            self.h.broker.disconnect()
            self.note("broker disconnect")
            self.count("disconnect")
            if working and self.rng.random() < 0.7:
                row = self.rng.choice(working)
                order = self.h.row("orders", "order_id", row.client_order_id)
                price = Fixed.parse(order["limit_price"])
                qty = Fixed(1)
                fee = self.commission(qty)
                self.h.broker.fill(row.client_order_id, qty, price, commission=fee)
                self.shadow.record(row.side, qty, price, fee)
                self.note(f"fill while disconnected {row.client_order_id} {row.side} 1 @{price}")
                self.count("fill_while_disconnected")
            if self.rng.random() < 0.3:
                self.step_buy()  # submission while disconnected -> UNKNOWN, reservation kept
                self.step_pump()
            if self.rng.random() < 0.3:
                self.reconcile("while disconnected")  # INCOMPLETE: partial data proves nothing
            if self.rng.random() < 0.85:
                self.h.broker.reconnect()
                self.note("broker reconnect")
        else:
            self.h.broker.reconnect()
            self.note("broker reconnect")
        self.step_pump()
        if self.connected() and self.rng.random() < 0.6:
            self.reconcile("after reconnect")

    def step_correct(self) -> None:
        """LED-10/ORD-06: a provider correction (new execution id with a suffix) of a booked fill.

        Restricted to corrections the core can book without owner review: the original is ACTIVE, its
        commission is already final (a correction that overtakes a DELAYED commission report is covered
        by CorrectionCommissionConservationTest below), its settlement date has not arrived (a
        correction landing after the broker settled the original but before a reconciliation confirmed it is
        reported as a blocking CASH_MISMATCH by design: settlement matching is all-or-nothing over due
        items, ADR-0006/REC-04), a corrected buy lot is undisposed when the correction is ingested (a
        correction of a partly sold buy is covered by CorrectionCommissionConservationTest), the session
        is active, the corrected price stays inside the limit, and each broker execution is corrected at
        most once (the fixture computes a second correction of the same id against the original row).
        Queued callbacks are drained first and the correction is ingested immediately, so no fill can
        dispose of the lot in between."""
        self.step_pump()
        today = local_date(self.h.clock.now_ns(), "America/New_York")
        cands = []
        for r in self.h.desk.store.query("SELECT row FROM executions ORDER BY rowid"):
            e = json.loads(r["row"])
            if e["status"] != "ACTIVE" or e["fee_status"] != "FINAL" or not e.get("order_id"):
                continue
            if e["exec_key"] in self.corrected_at_broker:
                continue
            if self.h.session_state(e["session_id"]) not in ("RUNNING", "SUSPENDED", "WINDING_DOWN"):
                continue
            stl = self.h.row("settlements", "settlement_id", e["settlement_id"])
            if stl is None or stl["status"] != "EXPECTED" or date.fromisoformat(stl["expected_date"]) <= today:
                continue
            if e["side"] == "BUY":
                lot = self.h.row("lots", "lot_id", f"lot:{e['exec_key']}")
                if lot is None or lot["open_qty"] != lot["original_qty"]:
                    continue
                if self.unreserved(e["session_id"]) < Fixed.parse(e["quantity"]):
                    continue  # a sell already holds these shares
            cands.append(e)
        if not cands:
            return self.step_fill()
        e = self.rng.choice(cands)
        order = self.h.row("orders", "order_id", e["order_id"])
        limit = Fixed.parse(order["limit_price"])
        old_qty, old_price = Fixed.parse(e["quantity"]), Fixed.parse(e["price"])
        step = Fixed(self.rng.randint(1, 3), 2)
        new_price = old_price - step if e["side"] == "BUY" else old_price + step
        if not new_price.is_positive() or (e["side"] == "BUY" and new_price > limit) or (e["side"] == "SELL" and new_price < limit):
            new_price = old_price
        new_qty = old_qty - Fixed(1) if old_qty > Fixed(1) and self.rng.random() < 0.3 else old_qty
        self.h.broker.correct_execution(e["exec_key"], new_qty, new_price)
        self.corrected_at_broker.add(e["exec_key"])
        self.shadow.correct(e["side"], old_qty, old_price, new_qty, new_price)
        self.note(f"correct {e['exec_key']} {e['side']} {old_qty}@{old_price} -> {new_qty}@{new_price}")
        self.count("correction")
        self.step_pump()

    def projection_snapshot(self) -> list:
        conn = self.h.desk.store.conn
        return [table_dump(conn, t) for t in ("balances", "holds", "orders", "lots", "sessions", "outbox", "control")]

    def step_retry(self) -> None:
        """Ch. 28, ORD-01: retry the last command with the same payload (idempotent: the stored result, no new
        event) and with a changed payload (refused with CommandConflict, no change to any projection)."""
        c = self.last_cmd
        if c is None:
            return self.step_tick()
        before_events, before = self.event_count(), self.projection_snapshot()
        again = self.h.desk.execute(c)
        if not again.duplicate or self.event_count() != before_events or self.projection_snapshot() != before:
            self.fail(f"retry of {c.command_id} was not an idempotent duplicate")
        try:
            self.h.desk.execute(Command(c.command_id, c.kind, c.principal, {**c.payload, "tampered": True}))
        except CommandConflict:
            pass
        else:
            self.fail(f"reused command id {c.command_id} with a changed payload was not refused")
        if self.projection_snapshot() != before:
            self.fail("a refused conflicting command changed projections")
        self.note(f"retry {c.kind} -> duplicate, conflict refused")
        self.count("retry")

    WEIGHTS = (
        ("buy", 12), ("sell", 12), ("pump", 10), ("fill", 16), ("release_commissions", 4), ("cancel", 7),
        ("confirm_cancel", 5), ("quote", 10), ("tick", 9), ("reconcile", 6), ("new_day", 3), ("restart", 3),
        ("stop", 1), ("start", 4), ("session_admin", 4), ("submit_fault", 3), ("cancel_fault", 2), ("shuffle", 3),
        ("external", 1), ("connectivity", 2), ("retry", 3), ("correct", 3),
    )

    # ------------------------------------------------------------- run
    def run(self) -> None:
        h = self.h
        try:
            h.bootstrap(authorize=AUTHORIZED)
            self.requote()
            r = self.cmd("create_session", account_key=ACCOUNT, session_id=f"S{self.seed}x0")
            self.t.assertTrue(r.accepted, r.body)
            self.check_invariants("bootstrap")
            names = [n for n, _ in self.WEIGHTS]
            weights = [w for _, w in self.WEIGHTS]
            for i in range(self.steps):
                self.advance(self.rng.randint(1, 4))
                name = self.rng.choices(names, weights)[0]
                self.count(name)
                try:
                    getattr(self, f"step_{name}")()
                except AssertionError:
                    raise
                except Exception as exc:  # a core crash on a legal sequence is a finding
                    self.fail(f"step {i} ({name}) raised {type(exc).__name__}: {exc}")
                self.check_invariants(f"step {i} ({name})")
            self.quiesce()
        finally:
            h.close()

    def quiesce(self) -> None:
        """Deliver everything outstanding, then require CLEAN reconciliations and conservation."""
        h = self.h
        if not self.connected():
            h.broker.reconnect()
        h.broker.defer_cancel_confirmation(False)
        h.broker.release_commissions()
        for row in self.broker_working():
            if row.status is BrokerOrderStatus.PENDING_CANCEL:
                h.broker.confirm_cancel(row.client_order_id)
        self.advance(1)
        self.step_pump()
        self.check_invariants("quiesce pump")
        r = self.reconcile("final")
        if not (r.accepted and r.body["status"] == "CLEAN"):
            self.fail(f"final reconciliation not CLEAN: {r.body}")
        self.check_invariants("final reconciliation")
        self.step_tick()
        self.check_invariants("final tick")
        # Settle everything outstanding: next business days until the broker holds no pending cash.
        for _ in range(4):
            if not h.broker.pending_settlements():
                break
            self.step_new_day()
            self.check_invariants("settlement day")
            r = self.reconcile("settlement")
            if not (r.accepted and r.body["status"] == "CLEAN"):
                self.fail(f"settlement reconciliation not CLEAN: {r.body}")
            self.step_tick()
            self.check_invariants("settlement tick")
        self.t.assertEqual(h.broker.pending_settlements(), [], "broker still has unsettled cash")
        r = self.reconcile("closing")
        if not (r.accepted and r.body["status"] == "CLEAN"):
            self.fail(f"closing reconciliation not CLEAN: {r.body}")
        self.check_invariants("closing reconciliation")
        self.completed = len(self.sessions(("COMPLETED_UNSETTLED", "COMPLETED_SETTLED")))
        self.check_shadow()
        self.check_protected_never_spent()
        self.check_replay("end of scenario")

    def check_shadow(self) -> None:
        """Independent tally vs ledger and broker once every trade has settled and every fee is final."""
        h = self.h
        snap = h.broker.account_snapshot()
        broker_total = sum((c.total for c in snap.cash if c.currency == "USD"), Fixed(0))
        broker_settled = sum((c.settled for c in snap.cash if c.currency == "USD"), Fixed(0))
        with h.desk.store.read() as tx:
            liquid = Fixed(0)
            for code in ledger.LIQUID_CODES:
                liquid = liquid + ledger.code_total(tx, ACCOUNT, code, "USD")
            cash = ledger.code_total(tx, ACCOUNT, ledger.CASH, "USD")
            lots = ledger.position_quantity(tx, ACCOUNT, INST)
            expected = tx.all("SELECT row FROM settlements WHERE status='EXPECTED'")
            estimated = tx.all("SELECT exec_key FROM executions WHERE json_extract(row,'$.status')='ACTIVE' "
                               "AND json_extract(row,'$.fee_status')='ESTIMATED' AND json_extract(row,'$.owner')!='external'")
        t = self.t
        t.assertEqual(broker_total, self.shadow.cash, f"seed {self.seed}: broker total cash vs shadow tally")
        t.assertEqual(broker_settled, self.shadow.cash, f"seed {self.seed}: broker settled cash vs shadow tally")
        t.assertEqual(len(expected), 0, f"seed {self.seed}: settlements still EXPECTED after settling")
        t.assertEqual(len(estimated), 0, f"seed {self.seed}: fees still estimated after every report delivered")
        t.assertEqual(cash, self.shadow.cash, f"seed {self.seed}: ledger CASH over all owners vs shadow tally")
        t.assertEqual(liquid, self.shadow.cash, f"seed {self.seed}: ledger liquid total over all owners vs shadow")
        t.assertEqual(lots, self.shadow.qty, f"seed {self.seed}: lot quantity vs net filled quantity")


class RandomizedReplayEquivalenceTest(unittest.TestCase):
    """ADR-0005 #3 / book Ch. 28 state-machine testing over seeded random sequences."""

    def test_seeded_scenarios_preserve_invariants_replay_and_conservation(self):
        """LED-01..LED-08, LED-10, ORD-01..ORD-06, REC-01..REC-04, RSK-03..RSK-05, SES-05, SES-07, SES-09, SES-12:
        after every step of 24 seeded 70-step scenarios the ledger verifies, holds equal the reservation implied
        by their order, and finished sessions are flat; each scenario ends with a
        CLEAN reconciliation, CASH over all owners == broker settled cash, lots == broker position, an
        independent shadow tally agrees with ledger and broker to the cent, protected proceeds were never
        transferred out, and replaying the event log reproduces every projection (ADR-0005 #3)."""
        totals: dict[str, int] = {}
        for seed in SEEDS:
            with self.subTest(seed=seed):
                sc = Scenario(self, seed, STEPS)
                sc.run()
                self.assertGreaterEqual(sc.clean_recons, 2, f"seed {seed}: expected several clean reconciliations")
                sc.stats["completed"] = sc.completed
                for k, v in sc.stats.items():
                    totals[k] = totals.get(k, 0) + v
        # The generator must actually exercise the behaviours it claims to (guards against a vacuous run).
        self.assertGreater(totals.get("completed", 0), 0, f"no session ever completed: {totals}")
        for key in ("fill_partial", "fill_full", "fill_duplicated", "fill_fee_delayed", "cancel_deferred",
                    "cancel_confirmed_late", "restart", "stop", "start_granted", "new_day", "shuffle", "retry",
                    "fault_submit_TIMEOUT_AFTER_ACCEPT", "fault_submit_TIMEOUT_BEFORE_ACCEPT", "external_trade",
                    "disconnect", "intent_accepted", "recon_CLEAN", "correction", "fill_while_disconnected"):
            self.assertGreater(totals.get(key, 0), 0, f"randomized runs never exercised {key}: {totals}")

    def test_same_seed_reproduces_the_same_event_chain(self):
        """ADR-0005 #1/#3, BRK-05: a seeded scenario on the deterministic fixture broker and manual clock
        produces an identical hash-chained event log when run twice, so a replay failure is reproducible."""
        heads = []
        for _ in range(2):
            sc = Scenario(self, 5, 40)
            sc.h.close = lambda: None  # keep the database open to read its chain head
            sc.run()
            row = sc.h.desk.store.query("SELECT COUNT(*) AS n, MAX(seq) AS s FROM events")[0]
            head = sc.h.desk.store.query("SELECT chain_hash FROM events ORDER BY seq DESC LIMIT 1")[0]["chain_hash"]
            heads.append((row["n"], head))
            sc.h.desk.store.close()
            sc.h.tmp.cleanup()
        self.assertEqual(heads[0], heads[1])


class CorrectionCommissionConservationTest(unittest.TestCase):
    """Correction interleavings the randomized generator found and isolates (LED-07, LED-10, ORD-06)."""

    def _corrected_buy(self, *, delay_commission: bool) -> Harness:
        h = Harness(initial_cash=INITIAL_CASH)
        h.bootstrap(authorize=AUTHORIZED)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="C").accepted)
        h.quote("9.99", "10.01")
        self.assertTrue(h.intent("C1", "BUY", "5", "10.00", "C").accepted)
        h.pump()
        row = h.broker.fill("C1", Fixed(5), Fixed.parse("10.00"), commission=Fixed.parse("1.00"),
                            delay_commission=delay_commission)
        h.pump()
        self.assertEqual(h.verify(), [])
        # Provider correction before settlement: 5 @ 10.00 becomes 5 @ 9.98 (new execution id E1.01).
        h.broker.correct_execution(row.exec_key, Fixed(5), Fixed.parse("9.98"))
        h.pump()
        self.assertEqual(h.verify(), [])
        h.broker.release_commissions()   # the delayed report (if any) arrives after the correction
        h.pump()
        self.assertEqual(h.verify(), [])
        return h

    def _assert_charged_once_and_settles_clean(self, h: Harness) -> None:
        with h.desk.store.read() as tx:
            commission = ledger.code_total(tx, ACCOUNT, ledger.COMMISSION, "USD")
        # The broker charged one commission of 1.00 for the corrected trade (fixture: "not charged again").
        self.assertEqual(commission, Fixed.parse("1.00"), "commission expense over all owners")
        h.clock.advance(24 * 3600 * NS)
        self.assertEqual(h.broker.advance_settlement(), 2)  # original movement + correction delta
        h.quote("9.99", "10.01")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        snap = h.broker.account_snapshot()
        self.assertEqual(snap.cash[0].settled, Fixed.parse("349.10"))  # 400.00 - 5 x 9.98 - 1.00
        with h.desk.store.read() as tx:
            self.assertEqual(ledger.code_total(tx, ACCOUNT, ledger.CASH, "USD"), Fixed.parse("349.10"))
            self.assertEqual(ledger.position_quantity(tx, ACCOUNT, INST), Fixed(5))
        self.assertEqual(h.verify(), [])
        self.assertEqual([e["kind"] for e in h.open_exceptions()], [])
        self.assertEqual(replay_and_compare(h.db_path, Path(h.tmp.name) / "replay.sqlite"), [])

    def test_correction_after_final_commission_is_charged_once(self):
        """LED-07, LED-10, ORD-06, REC-04: a correction of a fee-final buy reverses the original (including its
        commission) and replaces it; the re-reported commission is booked once, settlement confirms CLEAN,
        ledger CASH equals broker settled cash (349.10) and replay reproduces the projections."""
        h = self._corrected_buy(delay_commission=False)
        try:
            self._assert_charged_once_and_settles_clean(h)
        finally:
            h.close()

    def test_price_correction_of_a_partly_sold_buy_is_booked(self):
        """LED-10 (corrections as reversing plus replacing entries linked to the original), ORD-06, REC-04,
        ADR-0009 "Executions": buy 5 @ 10.00, sell 2 @ 10.20 (FIFO from that lot), then the provider corrects
        the buy to 5 @ 9.98 before settlement. ADR-0009 books every correction as reversing and replacing
        entries and does not exempt partly disposed lots, so the correction must be booked: no open
        exception, the 2 sold shares realize 0.44 at the corrected cost, 3 shares remain at 29.94, and the
        settlement reconciliation is CLEAN with ledger CASH == broker settled cash (368.50)."""
        h = Harness(initial_cash=INITIAL_CASH)
        try:
            h.bootstrap(authorize=AUTHORIZED)
            self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="D").accepted)
            h.quote("9.99", "10.01")
            self.assertTrue(h.intent("D1", "BUY", "5", "10.00", "D").accepted)
            h.pump()
            buy = h.broker.fill("D1", Fixed(5), Fixed.parse("10.00"), commission=Fixed.parse("1.00"))
            h.pump()
            h.quote("10.19", "10.21")
            self.assertTrue(h.intent("D2", "SELL", "2", "10.20", "D").accepted)
            h.pump()
            h.broker.fill("D2", Fixed(2), Fixed.parse("10.20"), commission=Fixed.parse("1.00"))
            h.pump()
            self.assertEqual(h.verify(), [])
            h.broker.correct_execution(buy.exec_key, Fixed(5), Fixed.parse("9.98"))
            h.pump()
            self.assertEqual(h.verify(), [])
            self.assertEqual([(e["kind"], e["detail"].get("issue")) for e in h.open_exceptions()], [],
                             "a correction of a partly sold buy was not booked")
            with h.desk.store.read() as tx:
                self.assertEqual(ledger.code_total(tx, ACCOUNT, ledger.REALIZED, "USD"), Fixed.parse("-0.44"))
                self.assertEqual(ledger.code_total(tx, ACCOUNT, ledger.INVENTORY, "USD"), Fixed.parse("29.94"))
            h.clock.advance(24 * 3600 * NS)
            h.broker.advance_settlement()
            h.quote("10.19", "10.21")
            rec = h.reconcile()
            self.assertEqual(rec.body["status"], "CLEAN", rec.body)
            self.assertEqual(h.broker.account_snapshot().cash[0].settled, Fixed.parse("368.50"))
            with h.desk.store.read() as tx:
                self.assertEqual(ledger.code_total(tx, ACCOUNT, ledger.CASH, "USD"), Fixed.parse("368.50"))
                self.assertEqual(ledger.position_quantity(tx, ACCOUNT, INST), Fixed(3))
            self.assertEqual(h.verify(), [])
            self.assertEqual(replay_and_compare(h.db_path, Path(h.tmp.name) / "replay.sqlite"), [])
        finally:
            h.close()

    def test_delayed_commission_of_a_corrected_execution_is_charged_once(self):
        """LED-07 (delayed commission handling), LED-10 (corrections as reversing plus replacing entries),
        ORD-06, REC-04: when a correction overtakes the original execution's DELAYED commission report, the
        late report for the superseded execution must not be booked as a second charge. The broker charged
        1.00 once; the ledger must show 1.00 of COMMISSION, settle CLEAN at 349.10, and replay."""
        h = self._corrected_buy(delay_commission=True)
        try:
            self._assert_charged_once_and_settles_clean(h)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
