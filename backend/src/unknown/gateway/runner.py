"""Gateway runner: moves outbox rows to the broker and broker events into the core.

The runner holds no state of its own: every decision is a core command. It is
driven explicitly (``pump``) by tests and by the supervised service loop.
Observation commands get fresh command ids; economic deduplication is done by
the handlers on scoped broker keys (ADR-0005 #5), so a re-delivered execution
with different transport metadata is a duplicate, never a conflict.

Broker events are popped from the broker (``BrokerPort.drain_events``). Popped
events that have not been applied yet are kept in a backlog that belongs to the
broker object, so the service's runner, the reconciler's runner and any other
runner on the same broker apply them once and in order. An event whose command
raises stays at the head of the backlog with everything after it, and the next
drain retries it first (review finding 58). The backlog lives in memory: after a
crash, reconciliation's execution query is what recovers executions."""
from __future__ import annotations

import json
import weakref
from collections import deque
from dataclasses import dataclass

from ..broker.port import (BrokerOrder, BrokerPort, CommissionEvent, CommissionRow, ConnectivityEvent, ExecutionEvent,
                           ExecutionRow, OrderStatusEvent, SubmitStatus)
from ..core.desk import Desk
from ..domain.fixed import Fixed


def execution_payload(account_key: str, e: ExecutionRow) -> dict:
    return {"account_key": account_key, "exec_key": e.exec_key, "provider_exec_id": e.provider_exec_id,
            "client_order_id": e.client_order_id, "order_ref": e.order_ref, "broker_order_id": e.broker_order_id,
            "instrument_id": e.instrument_id, "provider_ref": e.provider_ref, "side": e.side,
            "quantity": e.quantity.to_str(), "price": e.price.to_str(), "currency": e.currency,
            "exec_time_ns": e.exec_time_ns, "correction_of": e.correction_of}


def commission_payload(account_key: str, c: CommissionRow) -> dict:
    return {"account_key": account_key, "exec_key": c.exec_key, "amount": c.amount.to_str(), "currency": c.currency}


def status_payload(account_key: str, s: OrderStatusEvent) -> dict:
    return {"account_key": account_key, "client_order_id": s.client_order_id, "order_ref": s.order_ref,
            "broker_order_id": s.broker_order_id, "status": s.status.value, "filled_quantity": s.filled_quantity.to_str(),
            "remaining_quantity": s.remaining_quantity.to_str(), "reason": s.reason}


_BACKLOGS: "weakref.WeakKeyDictionary[object, deque]" = weakref.WeakKeyDictionary()
_PINNED_BACKLOGS: dict[int, tuple[object, deque]] = {}   # brokers that cannot be weakly referenced (kept alive)


def event_backlog(broker: BrokerPort) -> deque:
    """Events taken from ``broker`` but not yet applied to the core, oldest first."""
    try:
        backlog = _BACKLOGS.get(broker)
        if backlog is None:
            backlog = _BACKLOGS[broker] = deque()
        return backlog
    except TypeError:
        pinned = _PINNED_BACKLOGS.get(id(broker))
        if pinned is None or pinned[0] is not broker:
            pinned = _PINNED_BACKLOGS[id(broker)] = (broker, deque())
        return pinned[1]


@dataclass
class PumpStats:
    sent: int = 0
    skipped: int = 0
    events: int = 0


class GatewayRunner:
    def __init__(self, desk: Desk, broker: BrokerPort, account_key: str):
        self.desk = desk
        self.broker = broker
        self.account_key = account_key

    # ---------------------------------------------------------------- outbox
    def pending_outbox(self) -> list[str]:
        rows = self.desk.store.query("SELECT outbox_id FROM outbox WHERE account_key=? AND state='PENDING' ORDER BY created_seq, outbox_id",
                                     (self.account_key,))
        return [r["outbox_id"] for r in rows]

    def pump_outbox(self, limit: int = 100) -> PumpStats:
        stats = PumpStats()
        for outbox_id in self.pending_outbox()[:limit]:
            begin = self.desk.run_system("outbox_begin", {"outbox_id": outbox_id})
            if not begin.accepted or not begin.body.get("send"):
                stats.skipped += 1
                continue
            body = begin.body
            try:
                if body["action"] == "SUBMIT":
                    o = body["order"]
                    result = self.broker.submit(BrokerOrder(
                        client_order_id=o["client_order_id"], order_ref=o["order_ref"], instrument_id=o["instrument_id"],
                        provider_ref=o["provider_ref"], side=o["side"], quantity=Fixed.parse(o["quantity"]),
                        limit_price=Fixed.parse(o["limit_price"]), currency=o["currency"], time_in_force=o["time_in_force"]))
                else:
                    result = self.broker.cancel(body["client_order_id"], body.get("broker_order_id"))
                status, broker_id, reason = result.status.value, result.broker_order_id, result.reason
            except Exception as exc:  # transport failure: the outcome is unknown, never assumed
                status, broker_id, reason = SubmitStatus.UNKNOWN.value, None, f"{type(exc).__name__}: {exc}"
            self.desk.run_system("outbox_result", {"outbox_id": outbox_id, "status": status,
                                                   "broker_order_id": broker_id, "reason": reason})
            stats.sent += 1
        return stats

    # ---------------------------------------------------------------- events
    def drain_events(self, limit: int = 1000) -> int:
        """Apply up to ``limit`` broker events in order and return how many were applied.

        Events are taken from the broker only to fill the backlog up to ``limit``. If an event's
        command raises, that event and every later one stay in the backlog, in order, and the
        exception propagates (the service counts a failed cycle; a reconciliation records an
        INCOMPLETE run). Nothing taken from the broker is dropped (review finding 58)."""
        backlog = event_backlog(self.broker)
        if len(backlog) < limit:
            backlog.extend(self.broker.drain_events(limit - len(backlog)))
        n = 0
        while backlog and n < limit:
            self.apply_event(backlog[0])
            backlog.popleft()
            n += 1
        return n

    def apply_event(self, ev) -> None:
        key = self.account_key
        if isinstance(ev, ExecutionEvent):
            self.desk.run_system("broker_execution", execution_payload(key, ev.execution))
        elif isinstance(ev, CommissionEvent):
            self.desk.run_system("broker_commission", commission_payload(key, ev.commission))
        elif isinstance(ev, OrderStatusEvent):
            self.desk.run_system("broker_order_status", status_payload(key, ev))
        elif isinstance(ev, ConnectivityEvent):
            self.desk.run_system("broker_connectivity", {"account_key": key, "connected": ev.connected,
                                                         "authenticated": ev.authenticated, "detail": ev.detail})
        else:
            raise TypeError(f"unknown broker event {type(ev).__name__}")

    def pump(self) -> PumpStats:
        stats = self.pump_outbox()
        stats.events = self.drain_events()
        return stats

    # ---------------------------------------------------------------- periodic
    def tick(self) -> None:
        self.desk.run_system("valuation", {"account_key": self.account_key})
        self.desk.run_system("session_tick", {"account_key": self.account_key})

    def last_watermark(self) -> str | None:
        # The last CLEAN run recorded, by insertion order: wall time can step back (review finding 51).
        r = self.desk.store.query("SELECT row FROM reconciliations WHERE account_key=? AND status='CLEAN' "
                                  "ORDER BY rowid DESC LIMIT 1", (self.account_key,))
        return json.loads(r[0]["row"]).get("watermark") if r else None
