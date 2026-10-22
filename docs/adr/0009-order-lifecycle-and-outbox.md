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
