"""The published order transition table (ADR-0009; book-review finding BR-ORD-07).

One state label per order (ADR-0009 keeps the book's vocabulary); quantities, fees, ``cancel_state`` and broker ids
are stored beside it and may change in any state. Every order row a handler writes passes through
``Context.emit``, which calls ``check`` before the event is recorded: a change of ``state`` that is not declared
below is refused, nothing of the command is written, and the desk records an ``OrderTransitionRefused`` event, a
blocking ``ORDER_TRANSITION_REFUSED`` case and the stop latch instead (``Desk._refuse_transition``). The table is
therefore the whole order state machine: a handler cannot take an order anywhere it does not list.

Rules the table encodes:

* ``CREATED`` exists only inside the accepting transaction: an intent that passes pretrade is committed directly as
  ``RESERVED`` with its holds and outbox ``SUBMIT`` row.
* A terminal state (``FILLED``, ``CANCELED``, ``REJECTED``, ``EXPIRED``) is never left. Executions are still
  accepted in every state (an execution after a terminal state is booked and opens an EXECUTION_ANOMALY case), but
  they change quantities, never the terminal label.
* Holds are released only on entering a terminal state; every other transition keeps the reservation.
* A cancel rejected as too late returns ``CANCEL_PENDING`` to the state it came from (``prev_state``), or to
  ``PARTIALLY_FILLED`` when fills arrived meanwhile.

IBKR held and Inactive orders (decision, 2026-10-22). ``PreSubmitted`` is accepted-but-not-yet-elected; the adapter
maps it to ACKNOWLEDGED (working), which keeps the reservation, so nothing more is needed. ``Inactive`` is the
broker saying the order exists but is not working (a precaution, a hold or a rejection whose reason has not arrived)
and IBKR does not say it is final. The core therefore treats ``INACTIVE`` as an outcome it cannot rely on, the same
as a timeout: the order becomes ``UNKNOWN``, keeps every hold, opens the blocking ``unknown-order:<id>`` case and,
for a buy, queues a cancel (an inactive buy must not come back to life unattended). The reservation is released
only by a later broker terminal report (CANCELED, REJECTED, EXPIRED, FILLED) or by the owner's
``close_unknown_order`` resolution on broker evidence after a reconciliation shows nothing working. Before this
decision the core released an Inactive order as REJECTED.
"""
from __future__ import annotations

OPEN_STATES = ("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN")
TERMINAL_STATES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
STATES = OPEN_STATES + TERMINAL_STATES

# (from, to, why). ``None`` as the source means the order row does not exist yet.
_DECLARED: tuple[tuple[tuple[str | None, ...], tuple[str, ...], str], ...] = (
    ((None,), ("RESERVED",),
     "intent passed pretrade: holds, order and outbox SUBMIT commit together (CREATED exists only inside that transaction)"),
    (("RESERVED",), ("SUBMIT_PENDING",),
     "the sender claims the outbox row and commits SUBMIT_PENDING before the broker call"),
    (("RESERVED",), ("CANCELED",),
     "canceled, or entry blocked, before transmission: never sent, holds released"),
    (("RESERVED",), ("EXPIRED",),
     "intent deadline passed before transmission: never sent, holds released"),
    (("RESERVED", "SUBMIT_PENDING", "UNKNOWN"), ("ACKNOWLEDGED", "PARTIALLY_FILLED"),
     "broker acceptance, a working status or an execution: the order is working at the broker"),
    (("ACKNOWLEDGED",), ("PARTIALLY_FILLED",),
     "an execution leaves quantity working"),
    (("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"), ("CANCEL_PENDING",),
     "cancel requested, or the broker reports PENDING_CANCEL: the reservation stays until confirmation"),
    (("CANCEL_PENDING",), ("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED"),
     "cancel rejected (too late): the prior state returns, PARTIALLY_FILLED if fills arrived meanwhile"),
    (("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING"), ("UNKNOWN",),
     "submission outcome unknown (timeout, transport loss), a restart or restore during submission, or the broker "
     "reports the order Inactive (held, not working): every hold is kept and the order is looked up"),
    (OPEN_STATES, ("FILLED",),
     "executions reach the order quantity (executions are accepted in every state), or a broker FILLED report once "
     "every execution is ingested; holds released"),
    (OPEN_STATES, ("CANCELED",),
     "broker-confirmed cancellation, a broker terminal report with fewer shares than ordered, or the owner closed an "
     "UNKNOWN order with fills on broker evidence; remaining holds released"),
    (OPEN_STATES, ("REJECTED",),
     "broker rejection with no fills, a conclusive 'never received' at reconciliation, or the owner closed an UNKNOWN "
     "order without fills on broker evidence; holds released"),
    (OPEN_STATES, ("EXPIRED",),
     "the broker reports the day order expired; remaining holds released"),
)

TRANSITIONS: dict[tuple[str | None, str], str] = {}
for _froms, _tos, _why in _DECLARED:
    for _f in _froms:
        for _t in _tos:
            TRANSITIONS.setdefault((_f, _t), _why)


class UndeclaredOrderTransition(Exception):
    """A handler tried to write an order state change the table does not declare. Not a ``Rejected``: the desk
    rolls the command back, then records the refusal as an anomaly (see ``Desk._refuse_transition``)."""

    def __init__(self, transitions: list[dict], event_type: str):
        self.transitions = transitions
        self.event_type = event_type
        super().__init__("; ".join(f"{t['order_id']}: {t['from']} -> {t['to']}" for t in transitions))


def allowed(old: str | None, new: str) -> bool:
    """Whether ``old -> new`` is declared. Staying in a state is always allowed (field updates)."""
    return old == new or (old, new) in TRANSITIONS


def check(tx, rows: list[dict], event_type: str) -> None:
    """Refuse an event whose order rows change ``state`` in a way the table does not declare.

    Called by ``Context.emit`` for every event that writes orders, before anything is recorded."""
    current: dict[str, str | None] = {}
    bad: list[dict] = []
    for row in rows:
        order_id = row.get("order_id")
        new = row.get("state")
        if order_id not in current:
            r = tx.one("SELECT state FROM orders WHERE order_id=?", (order_id,))
            current[order_id] = r["state"] if r else None
        old = current[order_id]
        if new not in STATES or not allowed(old, new):
            bad.append({"order_id": order_id, "account_key": row.get("account_key"), "session_id": row.get("session_id"),
                        "from": old, "to": new})
        current[order_id] = new
    if bad:
        raise UndeclaredOrderTransition(bad, event_type)


def table() -> list[dict]:
    """The declared transitions as rows (for documentation, the terminal and tests)."""
    return [{"from": f, "to": t, "why": why} for (f, t), why in sorted(
        TRANSITIONS.items(), key=lambda kv: (STATES.index(kv[0][0]) if kv[0][0] else -1, STATES.index(kv[0][1])))]
