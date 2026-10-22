# ADR-0005: Event-sourced core with same-transaction projections

Status: accepted (2026-10-22)

## Context

The product contract (book Chapter 1) requires every operation to be "visible in the terminal and reconstructible from the event history". Chapter 8 requires that a crash/restart reproduces the same balances and that duplicates never duplicate money.

## Decision

1. Every state change is a **typed, versioned event** appended to `events`. Each event carries: `seq`, `event_id` (UUIDv7), `type`, `schema_version`, the causing `command_id`, `correlation_id`, `causation_id`, optional `account_key`/`session_id`/`order_id`/`instrument_id`, `event_time_ns` (source time, nullable), `receipt_time_ns`, `recorded_time_ns`, canonical payload, payload hash, previous hash and chain hash.
2. Command handlers **decide**. They read projections inside the transaction and return events or a rejection, and they never write projections directly. Each event type has one pure `apply` function that updates projections. The handler's events are applied in the same transaction.
3. **Replay equivalence is a tested property.** Replaying the full event log into an empty database reproduces every projection byte-for-byte (`tests/test_replay.py`).
4. **Command idempotency.** `commands` stores `(command_id, payload_hash, principal, result)`. A retry with the same payload returns the stored result. Reusing a `command_id` with a changed payload is rejected and recorded as a security-relevant event.
5. **Broker observations are commands too**, issued by the `SYSTEM` principal with deterministic identities derived from broker keys (for example `ibkr:exec:<account>:<execId>`). Economic deduplication additionally uses scoped execution keys (ADR-0006), because a provider can re-deliver one execution under different message identities.
6. The hash chain is a tamper diagnostic, not a security boundary (book Chapter 23).

## Rejected alternatives

- A state-only projection with an audit trail (the book's reference engine): it cannot prove that history reconstructs state.
- Pure event sourcing with asynchronous projections: the terminal and the risk checks need read-your-writes consistency on the single writer.
