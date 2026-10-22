# ADR-0012: Broker adapter contract

Status: accepted (2026-10-22)

## Context

`contracts/broker_port.py` in the book has untyped `dict` results. It has no authentication or connection state, no completeness or pagination status, no commission reports, no correction semantics and no capability typing. Book Chapter 12 requires all of these.

## Decision

The production port lives in `backend/src/unknown/broker/port.py`. It is synchronous, with explicit timeouts. Adapters run their own I/O threads and queue observations for the core's single writer.

- `capabilities() -> BrokerCapabilities` is typed. Every unknown capability is `False`/empty, so it is disabled by default. Fields:
  - account mode (`SIMULATED`, `PAPER`, `LIVE`), legal entity (if known) and base currency;
  - order types, TIFs, outside-RTH, fractional quantities, short, margin;
  - supported currencies and markets;
  - order-reference field length, execution-history depth and correction semantics;
  - pacing limits.
- `session() -> BrokerSessionState` reports connected, authenticated, `auth_required` reason, server time and last heartbeat.
- `account_snapshot() -> AccountSnapshot` reports per-currency balances. Each field has documented semantics (settled cash vs total cash vs buying power). It also gives positions with average cost as reported, `as_of`, and completeness.
- `open_orders()` and `executions_since(watermark)` return typed rows with a completeness flag and the new watermark. Commission reports arrive as separate rows linked to execution keys.
- `submit(order) -> SubmitResult` returns `ACCEPTED`, `REJECTED` or `UNKNOWN`. A timeout is always `UNKNOWN`.
- `cancel(...)` and `lookup(client_order_id)` return a found / not-found / unknown tri-state.
- `drain_events()` returns the queued asynchronous observations: order status, execution, commission, error and connectivity.

Raw provider messages are kept next to the normalized events where retention rights permit. Broker-specific fields remain visible in a namespaced `extensions` map and are not discarded.

Three implementations are planned:

1. `SimulatedBroker`: a deterministic fixture with scripted fault injection (timeouts after acceptance, duplicate and out-of-order callbacks, cancel/fill races, delayed commissions, corrections, disconnects, manual external trades). It is labelled `SIMULATED` everywhere.
2. `IbkrAdapter`: the TWS API through TWS or IB Gateway, paper account first. It is enabled only with an explicitly configured paper account identity, kept outside the repository.
3. Later brokers, each with its own capability record.
