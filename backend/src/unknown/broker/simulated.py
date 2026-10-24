"""Deterministic simulated broker, labelled SIMULATED everywhere: the default safety mode (no order leaves the host).

It implements ``BrokerPort`` with scripted fault injection so the core can be
exercised against timeouts after acceptance, duplicate and out-of-order
callbacks, cancel/fill races, delayed commissions, execution corrections,
disconnects, manual external trades and foreign orders (ADR-0012, BRK-05).

Nothing here is random, threaded or time-sleeping: the caller drives every step
through explicit methods and the injected clock. The simulator never invents a
price: a fill happens only when ``fill`` is called, by a test or, in a running
core, by ``unknown.service.sim_execution`` with a quote from the configured real
source (none configured: nothing fills, ADR-0020). Results are not evidence of
execution quality or broker compatibility.
"""
from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from ..domain.calendar import BusinessCalendar, local_date
from ..domain.clock import Clock
from ..domain.fixed import Fixed, Rounding, fmax
from .port import (
    AccountMode,
    AccountSnapshot,
    BrokerCapabilities,
    BrokerEvent,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerSessionState,
    CashBalance,
    CommissionEvent,
    CommissionRow,
    ConnectivityEvent,
    ExecutionBatch,
    ExecutionEvent,
    ExecutionRow,
    LookupResult,
    OpenOrderRow,
    OrderStatusEvent,
    PositionRow,
    SubmitResult,
    SubmitStatus,
)

PROVIDER = "simulated"
COMMISSION_SCALE = 6
AVG_COST_SCALE = 8

_WORKING = frozenset({BrokerOrderStatus.ACKNOWLEDGED, BrokerOrderStatus.PARTIALLY_FILLED,
                      BrokerOrderStatus.PENDING_CANCEL})


class SubmitBehavior(enum.Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    TIMEOUT_AFTER_ACCEPT = "TIMEOUT_AFTER_ACCEPT"    # broker has the order; caller sees UNKNOWN
    TIMEOUT_BEFORE_ACCEPT = "TIMEOUT_BEFORE_ACCEPT"  # broker never got it; caller sees UNKNOWN
    RAISE = "RAISE"                                  # transport error raised to the caller


class CancelBehavior(enum.Enum):
    ACCEPT = "ACCEPT"
    REJECT_TOO_LATE = "REJECT_TOO_LATE"
    TIMEOUT = "TIMEOUT"                              # cancel never reached the broker; caller sees UNKNOWN


@dataclass
class _SimOrder:
    client_order_id: str | None
    order_ref: str | None
    broker_order_id: str
    instrument_id: str
    side: str
    quantity: Fixed
    limit_price: Fixed
    currency: str
    time_in_force: str
    status: BrokerOrderStatus
    filled: Fixed = field(default_factory=lambda: Fixed(0))
    foreign: bool = False

    @property
    def remaining(self) -> Fixed:
        rest = self.quantity - self.filled
        return rest if rest.is_positive() else Fixed(0)

    @property
    def working(self) -> bool:
        return self.status in _WORKING


@dataclass
class _Position:
    quantity: Fixed
    cost: Fixed          # total cost basis in the position currency
    currency: str


@dataclass
class _PendingCash:
    currency: str
    amount: Fixed        # signed: negative = debit (purchase), positive = credit (sale)
    settle_date: date
    source: str          # exec_key that created the movement


@dataclass
class _ExecRecord:
    row: ExecutionRow
    commission: CommissionRow | None
    released: bool


class SimulatedBroker:
    """Step-driven ``BrokerPort`` implementation for deterministic tests."""

    def __init__(self, clock: Clock, *, base_currency: str = "USD", initial_settled_cash: Fixed,
                 calendar: BusinessCalendar, settlement_days: int = 1, tz: str = "America/New_York",
                 account_tag: str = "SIM"):
        if not isinstance(initial_settled_cash, Fixed) or initial_settled_cash.is_negative():
            raise ValueError("initial_settled_cash must be a non-negative Fixed")
        if settlement_days < 0:
            raise ValueError("settlement_days must be >= 0")
        self._clock = clock
        self._ccy = base_currency
        self._calendar = calendar
        self._settlement_days = settlement_days
        self._tz = tz
        self._tag = account_tag
        self._settled: dict[str, Fixed] = {base_currency: initial_settled_cash}
        self._pending_cash: list[_PendingCash] = []
        self._positions: dict[str, _Position] = {}
        self._orders: dict[str, _SimOrder] = {}            # ours, keyed by client_order_id
        self._foreign: dict[str, _SimOrder] = {}           # not placed through our API, keyed by broker id
        self._executions: list[_ExecRecord] = []
        self._exec_by_key: dict[str, _ExecRecord] = {}
        self._correction_count: dict[str, int] = {}        # base provider exec id -> corrections issued
        self._delayed_commissions: list[CommissionRow] = []
        self._events: deque[BrokerEvent] = deque()
        self._submit_script: deque[SubmitBehavior] = deque()
        self._cancel_script: deque[CancelBehavior] = deque()
        self._defer_cancel = False
        self._snapshot_complete = True
        self._connected = True
        self._authenticated = True
        self._auth_reason: str | None = None
        self._order_seq = 0
        self._exec_seq = 0
        self._manual_seq = 0
        self._foreign_seq = 0

    # ------------------------------------------------------------- port: info
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=PROVIDER,
            account_mode=AccountMode.SIMULATED,
            base_currency=self._ccy,
            legal_entity=None,
            currencies=(self._ccy,),
            markets=(),
            order_types=("LIMIT",),
            time_in_force=("DAY",),
            outside_regular_hours=False,
            fractional_quantities=False,
            short_selling=False,
            margin=False,
            atomic_replace=False,
            order_reference_max_length=64,
            execution_history_days=7,
            execution_corrections="new execution id with numeric suffix; original retained",
            settlement_evidence="settled cash per currency in account_snapshot",
            pacing=(),
            extensions={"simulated": True},
        )

    def session(self) -> BrokerSessionState:
        now = self._clock.now_ns()
        return BrokerSessionState(
            connected=self._connected,
            authenticated=self._connected and self._authenticated,
            auth_required_reason=self._auth_reason,
            server_time_ns=now if self._connected else None,
            last_heartbeat_ns=now if self._connected else None,
            detail="simulated broker: no order leaves this host",
        )

    # ---------------------------------------------------- port: snapshots
    def _require_connection(self) -> None:
        if not self._connected:
            raise ConnectionError("simulated broker disconnected")

    def account_snapshot(self) -> AccountSnapshot:
        self._require_connection()
        currencies = sorted(set(self._settled) | {p.currency for p in self._pending_cash})
        cash = []
        for ccy in currencies:
            settled = self._settled.get(ccy, Fixed(0))
            pending = Fixed(0)
            for movement in self._pending_cash:
                if movement.currency == ccy:
                    pending = pending + movement.amount
            cash.append(CashBalance(ccy, settled, settled + pending,
                                    "simulated: settled = cash after settlement; total = settled + pending trade cash"))
        positions = []
        for instrument_id in sorted(self._positions):
            pos = self._positions[instrument_id]
            avg = None if pos.quantity.is_zero() else pos.cost.div(pos.quantity, AVG_COST_SCALE, Rounding.HALF_EVEN)
            positions.append(PositionRow(instrument_id, instrument_id, pos.quantity, avg, pos.currency))
        now = self._clock.now_ns()
        return AccountSnapshot(as_of_ns=now, cash=tuple(cash), positions=tuple(positions),
                               complete=self._snapshot_complete, server_time_ns=now,
                               raw={"provider": PROVIDER, "account_tag": self._tag})

    def open_orders(self) -> tuple[list[OpenOrderRow], bool]:
        self._require_connection()
        rows = []
        for order in list(self._orders.values()) + list(self._foreign.values()):
            if order.working:
                rows.append(OpenOrderRow(order.client_order_id, order.order_ref, order.broker_order_id,
                                         order.instrument_id, order.side, order.status, order.filled, order.remaining))
        return rows, True

    def working_orders(self) -> list[_SimOrder]:
        """Our working orders with quantity left (read by ``service.sim_execution``; foreign orders excluded)."""
        return [o for o in self._orders.values() if o.working and o.remaining.is_positive()]

    def executions_since(self, watermark: str | None) -> ExecutionBatch:
        self._require_connection()
        start = 0 if watermark is None else int(watermark)
        if start < 0:
            raise ValueError("watermark must be non-negative")
        records = self._executions[start:]
        commissions = tuple(r.commission for r in records if r.commission is not None and r.released)
        return ExecutionBatch(executions=tuple(r.row for r in records), commissions=commissions,
                              watermark=str(len(self._executions)), complete=True)

    def lookup(self, client_order_id: str) -> LookupResult:
        if not self._connected:
            return LookupResult(found=None, reason="disconnected")
        order = self._orders.get(client_order_id)
        if order is None:
            return LookupResult(found=False, reason="broker has no order with this client id")
        return LookupResult(found=True, status=order.status, broker_order_id=order.broker_order_id,
                            filled_quantity=order.filled, remaining_quantity=order.remaining)

    def drain_events(self, limit: int = 1000) -> list[BrokerEvent]:
        out = []
        while self._events and len(out) < limit:
            out.append(self._events.popleft())
        return out

    # --------------------------------------------------------- port: orders
    def submit(self, order: BrokerOrder) -> SubmitResult:
        if not self._connected:
            return SubmitResult(SubmitStatus.UNKNOWN, reason="disconnected")
        behavior = self._submit_script.popleft() if self._submit_script else SubmitBehavior.ACCEPT
        if behavior is SubmitBehavior.RAISE:
            raise ConnectionError("simulated transport failure during submit")
        if behavior is SubmitBehavior.TIMEOUT_BEFORE_ACCEPT:
            return SubmitResult(SubmitStatus.UNKNOWN, reason="timeout before broker acceptance (simulated)")
        if not self._authenticated:
            return SubmitResult(SubmitStatus.REJECTED, reason=f"authentication required: {self._auth_reason}")
        if order.client_order_id in self._orders:
            return SubmitResult(SubmitStatus.REJECTED, reason="duplicate client order id")
        problem = self._validate(order)
        if problem:
            return SubmitResult(SubmitStatus.REJECTED, reason=problem)
        if behavior is SubmitBehavior.REJECT:
            return SubmitResult(SubmitStatus.REJECTED, reason="scripted rejection")
        self._order_seq += 1
        sim = _SimOrder(order.client_order_id, order.order_ref, f"SIMORD-{self._order_seq}", order.instrument_id,
                        order.side, order.quantity, order.limit_price, order.currency, order.time_in_force,
                        BrokerOrderStatus.ACKNOWLEDGED)
        self._orders[order.client_order_id] = sim
        self._emit_status(sim)
        if behavior is SubmitBehavior.TIMEOUT_AFTER_ACCEPT:
            return SubmitResult(SubmitStatus.UNKNOWN, reason="timeout after broker acceptance (simulated)")
        return SubmitResult(SubmitStatus.ACCEPTED, broker_order_id=sim.broker_order_id,
                            raw={"broker_order_id": sim.broker_order_id})

    def _validate(self, order: BrokerOrder) -> str:
        if order.side not in ("BUY", "SELL"):
            return f"unsupported side {order.side}"
        if order.time_in_force != "DAY":
            return f"unsupported time in force {order.time_in_force}"
        if order.outside_regular_hours:
            return "outside regular hours not supported"
        if order.currency != self._ccy:
            return f"unsupported currency {order.currency}"
        if not order.quantity.is_positive() or not order.limit_price.is_positive():
            return "quantity and limit price must be positive"
        if not order.quantity.is_multiple_of(Fixed(1)):
            return "fractional quantities not supported"
        if order.side == "SELL":
            pos = self._positions.get(order.instrument_id)
            held = pos.quantity if pos else Fixed(0)
            committed = Fixed(0)
            for o in self._orders.values():
                if o.working and o.side == "SELL" and o.instrument_id == order.instrument_id:
                    committed = committed + o.remaining
            if order.quantity > held - committed:
                return "short selling not permitted: quantity exceeds uncommitted position"
        else:
            available = self._settled.get(self._ccy, Fixed(0))
            for movement in self._pending_cash:
                if movement.currency == self._ccy and movement.amount.is_negative():
                    available = available + movement.amount
            for o in self._orders.values():
                if o.working and o.side == "BUY" and o.currency == self._ccy:
                    available = available - o.limit_price * o.remaining
            if order.limit_price * order.quantity > available:
                return "insufficient settled cash for cash-account purchase"
        return ""

    def cancel(self, client_order_id: str, broker_order_id: str | None) -> SubmitResult:
        if not self._connected:
            return SubmitResult(SubmitStatus.UNKNOWN, reason="disconnected")
        behavior = self._cancel_script.popleft() if self._cancel_script else CancelBehavior.ACCEPT
        if behavior is CancelBehavior.TIMEOUT:
            return SubmitResult(SubmitStatus.UNKNOWN, reason="cancel timed out before reaching broker (simulated)")
        order = self._orders.get(client_order_id)
        if order is None or (broker_order_id is not None and order.broker_order_id != broker_order_id):
            return SubmitResult(SubmitStatus.REJECTED, reason="unknown order")
        if behavior is CancelBehavior.REJECT_TOO_LATE or not order.working:
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=order.broker_order_id, reason="too late")
        if order.status is BrokerOrderStatus.PENDING_CANCEL:
            return SubmitResult(SubmitStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                                reason="cancel already pending")
        if self._defer_cancel:
            order.status = BrokerOrderStatus.PENDING_CANCEL
            self._emit_status(order)
        else:
            order.status = BrokerOrderStatus.CANCELED
            self._emit_status(order)
        return SubmitResult(SubmitStatus.ACCEPTED, broker_order_id=order.broker_order_id)

    # ------------------------------------------------------ scripting knobs
    def script_submit(self, *behaviors: SubmitBehavior) -> None:
        self._submit_script.extend(behaviors)

    def script_cancel(self, *behaviors: CancelBehavior) -> None:
        self._cancel_script.extend(behaviors)

    def defer_cancel_confirmation(self, enabled: bool) -> None:
        self._defer_cancel = enabled

    def confirm_cancel(self, client_order_id: str) -> bool:
        """Emit CANCELED for an order in PENDING_CANCEL; False if it filled meanwhile."""
        order = self._orders[client_order_id]
        if order.status is not BrokerOrderStatus.PENDING_CANCEL:
            return False
        order.status = BrokerOrderStatus.CANCELED
        self._emit_status(order)
        return True

    def set_snapshot_incomplete(self, incomplete: bool) -> None:
        self._snapshot_complete = not incomplete

    def disconnect(self) -> None:
        self._connected = False
        self._events.append(ConnectivityEvent(False, False, "simulated disconnect", self._clock.now_ns()))

    def reconnect(self) -> None:
        self._connected = True
        self._events.append(ConnectivityEvent(True, self._authenticated, "simulated reconnect", self._clock.now_ns()))

    def require_auth(self, reason: str) -> None:
        self._authenticated = False
        self._auth_reason = reason
        self._events.append(ConnectivityEvent(self._connected, False, f"authentication required: {reason}",
                                              self._clock.now_ns()))

    def authenticate(self) -> None:
        self._authenticated = True
        self._auth_reason = None
        self._events.append(ConnectivityEvent(self._connected, True, "authenticated", self._clock.now_ns()))

    def shuffle_pending_events(self, order: list[int]) -> None:
        """Reorder queued events: ``order`` is a permutation of current queue indices."""
        current = list(self._events)
        if sorted(order) != list(range(len(current))):
            raise ValueError("order must be a permutation of the pending event indices")
        self._events = deque(current[i] for i in order)

    def pending_event_count(self) -> int:
        return len(self._events)

    # --------------------------------------------------- market activity
    def fill(self, client_order_id: str, quantity: Fixed, price: Fixed, *, commission: Fixed | None = None,
             delay_commission: bool = False, duplicate_callbacks: int = 0, exec_time_ns: int | None = None,
             allow_overfill: bool = False, allow_price_violation: bool = False,
             allow_terminal: bool = False) -> ExecutionRow:
        order = self._orders.get(client_order_id)
        if order is None:
            raise ValueError(f"no simulated order {client_order_id!r}")
        if not order.working and not allow_terminal:
            raise ValueError(f"order {client_order_id} is {order.status.value}, not working")
        if not quantity.is_positive() or not price.is_positive():
            raise ValueError("fill quantity and price must be positive")
        if quantity > order.remaining and not allow_overfill:
            raise ValueError("fill exceeds remaining quantity")
        if not allow_price_violation:
            if order.side == "BUY" and price > order.limit_price:
                raise ValueError("buy fill above limit")
            if order.side == "SELL" and price < order.limit_price:
                raise ValueError("sell fill below limit")
        when = self._clock.now_ns() if exec_time_ns is None else exec_time_ns
        row, fee = self._execute(order.instrument_id, order.side, quantity, price, order.currency, when,
                                 client_order_id=order.client_order_id, order_ref=order.order_ref,
                                 broker_order_id=order.broker_order_id, commission=commission)
        terminal_before = not order.working
        order.filled = order.filled + quantity
        if not terminal_before:
            if order.remaining.is_zero():
                order.status = BrokerOrderStatus.FILLED
            elif order.status is not BrokerOrderStatus.PENDING_CANCEL:
                order.status = BrokerOrderStatus.PARTIALLY_FILLED
        self._record_execution(row, fee, delay_commission, duplicate_callbacks)
        self._emit_status(order)
        return row

    def external_trade(self, instrument_id: str, side: str, quantity: Fixed, price: Fixed, *,
                       commission: Fixed | None = None) -> ExecutionRow:
        """A manual trade placed outside our API (e.g. by the owner in another app)."""
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        self._manual_seq += 1
        row, fee = self._execute(instrument_id, side, quantity, price, self._ccy, self._clock.now_ns(),
                                 client_order_id=None, order_ref=None,
                                 broker_order_id=f"MANUAL-{self._manual_seq}", commission=commission)
        self._record_execution(row, fee, False, 0)
        return row

    def foreign_order(self, instrument_id: str, side: str, qty: Fixed, limit: Fixed,
                      order_ref: str = "OTHER-DEPLOYMENT:1:x") -> OpenOrderRow:
        """A working order at the broker that this deployment did not place."""
        self._foreign_seq += 1
        sim = _SimOrder(None, order_ref, f"FOREIGN-{self._foreign_seq}", instrument_id, side, qty, limit,
                        self._ccy, "DAY", BrokerOrderStatus.ACKNOWLEDGED, foreign=True)
        self._foreign[sim.broker_order_id] = sim
        return OpenOrderRow(None, order_ref, sim.broker_order_id, instrument_id, side, sim.status,
                            sim.filled, sim.remaining)

    def correct_execution(self, exec_key: str, new_quantity: Fixed, new_price: Fixed) -> ExecutionRow:
        """Broker correction: a new execution id with a numeric suffix replaces ``exec_key``.

        Positions and pending cash move by the difference only; the commission
        already charged is not charged again (a commission report with the same
        amount is issued for the new id, as a provider would report it)."""
        original = self._exec_by_key.get(exec_key)
        if original is None:
            raise ValueError(f"unknown execution {exec_key!r}")
        if not new_quantity.is_positive() or not new_price.is_positive():
            raise ValueError("corrected quantity and price must be positive")
        old = original.row
        base = old.provider_exec_id.split(".")[0]
        count = self._correction_count.get(base, 0) + 1
        self._correction_count[base] = count
        provider_exec_id = f"{base}.{count:02d}"
        new_row = ExecutionRow(
            exec_key=f"sim:{self._tag}:{provider_exec_id}", provider_exec_id=provider_exec_id,
            client_order_id=old.client_order_id, order_ref=old.order_ref, broker_order_id=old.broker_order_id,
            instrument_id=old.instrument_id, provider_ref=old.provider_ref, side=old.side,
            quantity=new_quantity, price=new_price, currency=old.currency, exec_time_ns=old.exec_time_ns,
            correction_of=old.exec_key,
            raw={"provider_exec_id": provider_exec_id, "correction_of": old.exec_key},
        )
        delta_qty = new_quantity - old.quantity
        delta_notional = new_price * new_quantity - old.price * old.quantity
        sign = Fixed(1) if old.side == "BUY" else Fixed(-1)
        if old.side == "BUY":
            self._adjust_position(old.instrument_id, old.currency, delta_qty, delta_notional)
        elif delta_qty.is_positive():
            self._reduce_position(old.instrument_id, old.currency, delta_qty, new_price)
        elif delta_qty.is_negative():
            # Fewer shares were actually sold: restore them at the current average cost.
            pos = self._positions.get(old.instrument_id)
            restored = -delta_qty
            if pos is not None and pos.quantity.is_positive():
                cost = (pos.cost * restored).div(pos.quantity, AVG_COST_SCALE, Rounding.HALF_EVEN)
            else:
                cost = old.price * restored
            self._adjust_position(old.instrument_id, old.currency, restored, cost)
        if not delta_notional.is_zero():
            self._pending_cash.append(_PendingCash(old.currency, -delta_notional * sign,
                                                   self._settle_date(old.exec_time_ns), new_row.exec_key))
        if old.client_order_id is not None and old.client_order_id in self._orders:
            order = self._orders[old.client_order_id]
            order.filled = order.filled + delta_qty
        fee = None
        if original.commission is not None:
            fee = CommissionRow(new_row.exec_key, original.commission.amount, original.commission.currency,
                                raw={"note": "re-reported for corrected execution; not charged again"})
        self._record_execution(new_row, fee, not original.released, 0)
        return new_row

    def release_commissions(self) -> int:
        """Deliver every delayed commission report (as events and in executions_since)."""
        released = 0
        for fee in self._delayed_commissions:
            self._exec_by_key[fee.exec_key].released = True
            self._events.append(CommissionEvent(fee))
            released += 1
        self._delayed_commissions.clear()
        return released

    def expire_day_orders(self) -> int:
        expired = 0
        for order in list(self._orders.values()) + list(self._foreign.values()):
            if order.working and order.time_in_force == "DAY":
                order.status = BrokerOrderStatus.EXPIRED
                if not order.foreign:
                    self._emit_status(order)
                expired += 1
        return expired

    # ---------------------------------------------------------- cash
    def deposit(self, amount: Fixed) -> None:
        if not amount.is_positive():
            raise ValueError("deposit must be positive")
        self._settled[self._ccy] = self._settled.get(self._ccy, Fixed(0)) + amount

    def withdraw(self, amount: Fixed) -> None:
        if not amount.is_positive():
            raise ValueError("withdrawal must be positive")
        current = self._settled.get(self._ccy, Fixed(0))
        if amount > current:
            raise ValueError("withdrawal exceeds settled cash")
        self._settled[self._ccy] = current - amount

    def advance_settlement(self) -> int:
        """Apply every pending movement whose settlement date is on or before today (local tz)."""
        today = local_date(self._clock.now_ns(), self._tz)
        due = [m for m in self._pending_cash if m.settle_date <= today]
        self._pending_cash = [m for m in self._pending_cash if m.settle_date > today]
        for movement in due:
            self._settled[movement.currency] = self._settled.get(movement.currency, Fixed(0)) + movement.amount
        return len(due)

    def pending_settlements(self) -> list[tuple[str, Fixed, date, str]]:
        return [(m.currency, m.amount, m.settle_date, m.source) for m in self._pending_cash]

    # ------------------------------------------------------- internals
    def _settle_date(self, exec_time_ns: int) -> date:
        trade_date = local_date(exec_time_ns, self._tz)
        return self._calendar.add_business_days(trade_date, self._settlement_days)

    @staticmethod
    def default_commission(quantity: Fixed) -> Fixed:
        return fmax(Fixed.parse("1.00"), Fixed.parse("0.005") * quantity).rescale(COMMISSION_SCALE, Rounding.HALF_EVEN)

    def _execute(self, instrument_id: str, side: str, quantity: Fixed, price: Fixed, ccy: str, when: int, *,
                 client_order_id: str | None, order_ref: str | None, broker_order_id: str | None,
                 commission: Fixed | None) -> tuple[ExecutionRow, CommissionRow]:
        self._exec_seq += 1
        provider_exec_id = f"E{self._exec_seq}"
        exec_key = f"sim:{self._tag}:{provider_exec_id}"
        fee_amount = self.default_commission(quantity) if commission is None else commission
        notional = price * quantity
        if side == "BUY":
            self._adjust_position(instrument_id, ccy, quantity, notional)
            cash_movement = -(notional + fee_amount)
        else:
            self._reduce_position(instrument_id, ccy, quantity, price)
            cash_movement = notional - fee_amount
        self._pending_cash.append(_PendingCash(ccy, cash_movement, self._settle_date(when), exec_key))
        row = ExecutionRow(
            exec_key=exec_key, provider_exec_id=provider_exec_id, client_order_id=client_order_id,
            order_ref=order_ref, broker_order_id=broker_order_id, instrument_id=instrument_id,
            provider_ref=instrument_id, side=side, quantity=quantity, price=price, currency=ccy,
            exec_time_ns=when, correction_of=None,
            raw={"provider_exec_id": provider_exec_id, "broker_order_id": broker_order_id or ""},
        )
        fee = CommissionRow(exec_key, fee_amount, ccy, raw={"source": "simulated default schedule"
                                                                 if commission is None else "scripted"})
        return row, fee

    def _record_execution(self, row: ExecutionRow, fee: CommissionRow | None, delay: bool, duplicates: int) -> None:
        record = _ExecRecord(row, fee, released=fee is not None and not delay)
        self._executions.append(record)
        self._exec_by_key[row.exec_key] = record
        for _ in range(1 + max(0, duplicates)):
            self._events.append(ExecutionEvent(row))
        if fee is not None:
            if delay:
                self._delayed_commissions.append(fee)
            else:
                self._events.append(CommissionEvent(fee))

    def _adjust_position(self, instrument_id: str, ccy: str, delta_qty: Fixed, delta_cost: Fixed) -> None:
        pos = self._positions.get(instrument_id) or _Position(Fixed(0), Fixed(0), ccy)
        pos.quantity = pos.quantity + delta_qty
        pos.cost = pos.cost + delta_cost
        if pos.quantity.is_zero():
            self._positions.pop(instrument_id, None)
        else:
            self._positions[instrument_id] = pos

    def _reduce_position(self, instrument_id: str, ccy: str, quantity: Fixed, price: Fixed) -> None:
        pos = self._positions.get(instrument_id) or _Position(Fixed(0), Fixed(0), ccy)
        if pos.quantity.is_positive() and quantity < pos.quantity:
            removed = (pos.cost * quantity).div(pos.quantity, AVG_COST_SCALE, Rounding.HALF_EVEN)
            pos.cost = pos.cost - removed
            pos.quantity = pos.quantity - quantity
        elif pos.quantity.is_positive():
            # Sells the whole long; any excess (overfill / external anomaly) opens a short at the fill price.
            excess = quantity - pos.quantity
            pos.quantity = -excess
            pos.cost = -(price * excess)
        else:
            pos.quantity = pos.quantity - quantity
            pos.cost = pos.cost - price * quantity
        if pos.quantity.is_zero():
            self._positions.pop(instrument_id, None)
        else:
            self._positions[instrument_id] = pos

    def _emit_status(self, order: _SimOrder) -> None:
        self._events.append(OrderStatusEvent(
            client_order_id=order.client_order_id, order_ref=order.order_ref,
            broker_order_id=order.broker_order_id, status=order.status, filled_quantity=order.filled,
            remaining_quantity=order.remaining, reason="", event_time_ns=self._clock.now_ns(),
            raw={"status": order.status.value},
        ))
