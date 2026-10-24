# ADR-0018: Broker-agnostic live market-data bus

Status: proposed (2026-10-23)

## Context

The terminal needs live quotes, time & sales and depth for watchlists, a ticker tape, charts and AI tools
(CAP-MKT-02, CAP-MKT-03, CAP-MKT-06, CAP-MKT-08). IBKR is the owner's main broker, not the only one: other brokers and
data vendors (Alpaca first) must plug in without changing the terminal, the API or the wire format
(`docs/design/marketdata-stream-contract.md`). The core already keeps marks from its own IBKR market-data session in an
in-memory `QuoteBook` for valuation and pre-trade checks; that path is authoritative and stays unchanged.

## Decision

1. **Sources are adapters behind one interface** (`backend/src/unknown/marketdata/source.py`). A source declares its
   capabilities (quotes, trades, depth, tick-by-tick, delayed), its data type (REALTIME, DELAYED, FROZEN; a
   source can also label each event, as IBKR does with the type it delivered) and its line budget. Adapters live with
   their provider (`broker/ibkr/mdsource.py`, `broker/alpaca/stream.py`) and are loaded by dotted factory path only
   when enabled, so the bus never imports a broker package.
2. **A source is chosen per key by configuration** (`<UNKNOWN_HOME>/config/marketdata.json`, outside git): ordered
   routes of key patterns to source lists. A key refused by a source (line budget, entitlement, coverage) falls back to
   the next; an entitlement error after subscribing fails the key over the same way. Every event carries `source`.
3. ~~**SYNTHETIC** is deterministic and labelled. It is the only source when no configuration exists (development and
   the demo) and the configuration loader refuses it alongside any real source, so it is never used when a real
   source is configured.~~ **Superseded by ADR-0020 (2026-10-23):** there is no SYNTHETIC source. Without a
   configuration there is no source and every key is `UNAVAILABLE` with code `NO_SOURCE`; tests inject a `TEST` double
   in code.
4. **The bus runs inside the API process**, on its own threads, created on first use. Reasons: the stream is an
   owner-session route like every other (no second authentication surface or proxy hop); the measured cost is small
   (500 keys at 4 Hz: bus-to-client p99 about 2 ms on the development machine, `tests/test_marketdata_perf.py`); and a
   failure of the bus cannot reach the core, which is a separate process. A separate process
   (`python -m unknown.marketdata.serve` behind the API) is the fallback if `/api/marketdata/latency` shows the API's
   request handling and the bus contending; the interface (sources, sink, bus) does not change.
5. **Market data never enters the core's event-sourced database.** Time & sales history goes to a separate SQLite
   tick store (`<UNKNOWN_HOME>/marketdata/ticks.sqlite`) with bounded retention by age and rows per key.
6. **Delivery rules**: quotes are coalesced per key to at most one event per 50 ms (latest merged state, first quote
   after a quiet window at once); trades are never coalesced and never silently dropped (a full per-client buffer
   counts the drop, sends a `status` naming the count, and the store keeps the trade); depth events are full side
   snapshots (latest wins per client); line budgets are enforced before a source is asked and every refused key gets a
   `status` event.
7. **IBKR**: the bus source uses its own read-only session (client id distinct from the order adapter and the core's
   market-data session) and an explicit share of the username's lines, because IBKR lines are shared by every API
   client and TWS. Tick-by-tick (AllLast) and depth are requested only when the owner's configuration asserts the
   entitlement, within 5% of lines for tick-by-tick and the owner's stated depth allowance.
8. **Alpaca**: market data only, through a small standard-library websocket client (no third-party dependency to
   isolate); IEX by default, `sip` or `delayed_sip` configurable; the Basic plan's 30 websocket symbols is the line
   budget; keys only from `<UNKNOWN_HOME>/secrets/alpaca.json`.

## Consequences

- Adding a provider is one adapter plus a factory entry; the terminal shows it by `source` and `data_type` without
  code changes.
- Two IBKR sessions (core marks, bus) consume lines from one allocation; the owner's configuration must split them.
- ~~The monitor can show SYNTHETIC data in the demo, always labelled.~~ Superseded by ADR-0020: the monitor shows only
  real data; without a source each row is an em dash with `NO_SOURCE`.
- Not decided here: consolidated-tape licensing (CAP-MKT-02), historical tick backfill (CAP-MKT-03 reqHistoricalTicks),
  per-row polling for keys beyond the line budget (CAP-MKT-08 acceptance criterion).
