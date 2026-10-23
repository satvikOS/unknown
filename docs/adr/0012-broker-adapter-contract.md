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

## Amendment (2026-10-22): journal before apply; INACTIVE is not terminal

Implemented on `impl/foundation` at 8b98d00 (book-review findings BR-ORD-20, BR-ORD-07).

**Journal before apply.** `drain_events()` pops events from the adapter, and an IBKR callback is never redelivered, so the gateway (`gateway/runner.py`) makes every popped event durable before applying it. The core service attaches the account's event journal (`<UNKNOWN_HOME>/data/<account>.events`; ADR-0003 amendment) to the broker, so the service's runner and the reconciler's runner journal through the same file.

1. Each drain appends the events taken from the adapter to the journal in one fsync'd transaction. `pump()` does this before its outbox pass.
2. It then applies the unapplied entries in journal order. Each entry is applied as an ordinary core command with the deterministic id `sys.bev.<journal id>.<seq>`, then marked applied. A crash between append and apply loses nothing: the next process applies the unapplied entries before any request or new event. A crash between apply and mark re-applies the entry under the same id, which the core answers with the stored result. The handlers' scoped broker keys keep even a re-application under a new id from having a second economic effect.
3. The in-memory hand-off is bounded. If the journal cannot be written, the runner stops taking events from the adapter: at most one batch waits in the runner's memory, and the rest stays in the adapter. It also latches the stop with reason `EVENT_JOURNAL_UNWRITABLE: <error>` and records an `INCOMPLETE` reconciliation, so a clean reconciliation is required before a green start. Entries already journaled are still applied. When the append fails in `pump()`, the failure is recorded before the outbox pass, so the pass sends cancels but transmits no new buy.
4. A corrupt entry (failed checksum or broken chain), or an entry whose command id the core holds for a different command (a journal older than the core), breaks the journal. The verified entries before it are applied, nothing is appended behind it, and the desk fails closed the same way. An event the gateway cannot map to a command blocks only itself and what follows it.
5. The core, not the marks, decides what was applied. After a restore, marked entries whose commands the restored core lacks are applied again before any new entry, in journal order.

What the journal does not cover: an event the adapter had received but the gateway had not yet appended when the process died. Such an event is recovered only by reconciliation (executions inside the broker's history window, statuses through the open-order list and lookups).

**`INACTIVE` is not a terminal broker status.** `TERMINAL_BROKER_STATUSES` no longer includes it. The core holds such an order as `UNKNOWN` with its reservations (ADR-0009 amendment).
