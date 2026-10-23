# ADR-0009: Order lifecycle, outbox and unknown outcomes

Status: accepted (2026-10-22)

## Decision

### States

The book's states are kept: `CREATED`, `RESERVED`, `SUBMIT_PENDING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED` and `UNKNOWN`.

Economic quantities are stored separately from the state label:

- `cum_qty`, `leaves_qty` and `avg_price`;
- `fees_estimated` and `fees_final`;
- `cancel_state`: `NONE`, `REQUESTED`, `SENT`, `REJECTED` or `CONFIRMED`.

`CREATED` exists only inside the accepting transaction. An intent that passes pretrade checks commits directly as `RESERVED`, with its holds and an outbox `SUBMIT` row.

### Submission

1. The sender takes the oldest `PENDING` outbox row and commits `SUBMIT_PENDING` **before** calling the broker.
   - If the account has a stop latch or recovery gate by then, the order is canceled locally (never transmitted) and its holds are released.
2. It calls the broker and then records the result:
   - `ACCEPTED` → `ACKNOWLEDGED`.
   - Unambiguous `REJECTED` → `REJECTED`, holds released.
   - Timeout, exception or disconnect → **`UNKNOWN`**: holds retained, an `UNKNOWN_ORDER` exception opened, new entries on the account blocked.
3. Recovery of `UNKNOWN` and of any `SUBMIT_PENDING` found after a restart is lookup by the client order id carried in the broker order reference field. Retry is permitted only after the broker conclusively reports that the order does not exist.

### Cancellation

A cancel request sets `CANCEL_PENDING` and enqueues an outbox `CANCEL` row. It releases nothing.

- Only a broker cancellation confirmation, or conclusive reconciliation, makes the order `CANCELED` and releases `leaves_qty`.
- A cancel rejection ("too late") restores the prior state.
- A fill during `CANCEL_PENDING` is applied normally and consumes its share of the holds.

### Executions

Executions are deduplicated by `(provider, account_key, execution_key)`. A provider correction (for IBKR, a changed execution-id suffix) is recorded as a correction event linked to the original, with reversing and replacing journal entries.

An execution is always recorded. Conditions that open an exception and block new exposure:

- the order is unknown;
- the fill is beyond the remaining reserved quantity;
- the price is outside the limit;
- the fill arrives after a terminal state.

### Initial order eligibility

The initial eligibility (the policy can only narrow it): limit orders only, TIF `DAY`, regular trading hours only, fully paid long cash equities and unleveraged ETFs, no short sale (a sell needs owned, unreserved session inventory), and no cancel/replace. A replacement is a cancel confirmation followed by a new intent with a fresh risk check.

## Amendment (2026-10-22): transition table, held orders, owner close on evidence, changed redelivery

Implemented on `impl/foundation` at 8b98d00 (book-review findings BR-ORD-07, BR-ORD-03, BR-ORD-05). The decision above stands; this section records how it is enforced and decides the cases it left open.

**One published transition table.** `backend/src/unknown/core/order_machine.py` declares every order state change, each with its reason. `Context.emit` checks every order row a handler writes against it before the event is recorded, so the table is the whole order state machine. An undeclared change is refused: the command rolls back, and the desk records an `OrderTransitionRefused` event (with the received payload when the command is a broker observation), a blocking critical `ORDER_TRANSITION_REFUSED` case and the stop latch. The command is stored `REJECTED` with code `UNDECLARED_ORDER_TRANSITION`. A terminal state (`FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`) is never left; executions after it change quantities, never the label. Holds are released only on entering a terminal state.

**Held and Inactive orders.** IBKR `PreSubmitted` maps to `ACKNOWLEDGED` (working). IBKR `Inactive` (normalized `INACTIVE`) means the order exists but is not working, and IBKR does not say it is final. The core treats it like a timeout: the order becomes `UNKNOWN`, keeps every hold, records the report as `broker_held`, opens the blocking `unknown-order:<id>` case and, for a buy, queues a cancel. Only a later broker terminal report or the owner's close on evidence (below) releases it. A locally terminal order that a reconciliation finds listed as `INACTIVE` (or working) is a blocking `EXECUTION_ANOMALY`, and a buy gets a cancel. This replaces the earlier behaviour, in which the core released an Inactive order as `REJECTED`; `broker/port.py` no longer lists `INACTIVE` among the terminal broker statuses.

**Unknown outcomes are never resubmitted.** A retry is a new intent with a new order id. Reconciliation resolves an `UNKNOWN` order when the broker lists or finds it, or when a lookup conclusively reports that the broker never received it (the IBKR adapter is conclusive only when `placeOrder` was never called).

**Owner close on evidence.** A stuck `UNKNOWN` order is closed only by `resolve_exception` with action `close_unknown_order`, on its `UNKNOWN_ORDER` case or on the `ORDER_UNRESOLVED` / `ORDER_MISSING_AT_BROKER` reconciliation case that names it. The command needs fresh owner authentication, a note and evidence `{kind: BROKER_STATEMENT | BROKER_CONFIRM | SUPPORT_REFERENCE, reference}`. It never releases a hold the broker may still work. The account's latest reconciliation must be complete, run in this process, younger than `max_reconciliation_age_s`, and include the order's last material change. The broker's open orders in it must not list the order, its lookup must not have found it, and its only blocking differences may be those of unresolved `UNKNOWN` orders. Otherwise the command is refused `CLOSE_NOT_PROVEN`, naming every unmet condition, and nothing changes. On success the order ends `REJECTED` (no fills) or `CANCELED` (fills kept), its holds are released, the related cases resolve with the evidence, an `UnknownOrderClosedByOwner` event records the evidence, run and lookup, and the account must reconcile again.

**Same execution id, changed content.** A redelivery whose economic fields (side, quantity, price, instrument, currency) differ from the booked execution is recorded, never rejected and never booked. The original stays booked. The received payload is kept in an `ExecutionContentConflict` event, and a blocking critical `EXECUTION_ANOMALY` names the execution's order, session and current owner until the owner resolves it. A genuine amendment reaches the ledger as a broker correction (a new execution id) or as a reconciliation difference. An identical redelivery is a duplicate.

**Executions after a session ended** are booked to whoever holds that session's balances now (ADR-0008 amendment).
