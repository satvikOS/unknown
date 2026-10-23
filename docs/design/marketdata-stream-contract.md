# Market-data stream contract (wave F)

This is the shared wire format between the market-data bus (backend), the quote monitor, the charts and the AI
tools. A change to it updates this file in the same commit.

## Endpoint

`GET /api/marketdata/stream?keys=<k1>,<k2>,...` is an owner-session Server-Sent Events stream. A key is a security
key (`<SYMBOL>.<MIC>`, e.g. `AAPL.XNAS`; see `terminal/src/ui/link.ts`) or a core instrument id. A request for
more keys than the line budget allows gets a `status` event per refused key, never a silent drop.

## Events

Every decimal is a string (exact; never a float). Timestamps are integer nanoseconds since the Unix epoch, sent
as strings when they exceed 2^53.

- `quote`: `{"key", "seq", "bid", "ask", "bid_size", "ask_size", "last", "last_size", "volume", "open", "high",
  "low", "prev_close", "currency", "source", "data_type", "ts_source_ns", "ts_recv_ns", "ts_pub_ns"}`
  - Fields the source did not send are `null`, never zero.
  - `source` is the provider id (`IBKR`, `SYNTHETIC`, ...).
  - `data_type` is `REALTIME`, `DELAYED`, `FROZEN` or `SYNTHETIC`. A SYNTHETIC quote is never shown without its
    label.
  - Quotes are coalesced per key: at most one event per key every 50 ms, always the latest state.
  - Additive fields (2026-10-23, ADR-0018): `feed` names the provider feed and its consolidation (e.g. `alpaca:iex`
    single venue, `alpaca:sip` consolidated, `ibkr:live`, `synthetic`); `size_unit` is `shares`, `round_lots` or
    `null` when the source does not state it. Clients ignore fields they do not know.
- `trade`: `{"key", "seq", "price", "size", "exchange", "conditions", "ts_source_ns", "ts_recv_ns", "ts_pub_ns",
  "source", "data_type"}` (plus the additive `feed`). Trades are never coalesced and never silently dropped: when a
  client's buffer is full the bus sends a `status` (`STALE`, reason naming the count) and the trade `seq` shows the gap;
  the trades remain in the tick store (`GET /api/marketdata/trades?key=&before_ns=&limit=`, newest first).
- `depth`: `{"key", "seq", "side", "levels": [{"price", "size", "mm"}], "source", "data_type", "ts_recv_ns"}`.
  Each event is a full snapshot of that side, not a delta.
- `status`: `{"key", "state", "reason"}`. `state` is one of `SUBSCRIBED`, `UNAVAILABLE`, `NOT_ENTITLED`,
  `LINE_BUDGET`, `STALE` or `ENDED`.
- `heartbeat`: `{"ts_pub_ns"}`, sent every 5 s.

`seq` increases per key and per event type. A gap means the client must re-read the snapshot from
`GET /api/marketdata/snapshot?keys=...` (same `quote` objects): `{"quotes": [quote, ...], "status": [status, ...],
"ts_pub_ns"}`, with a `status` for each requested key that has no quote yet or is not streaming.
`GET /api/marketdata/sources` lists the configured sources, their capabilities, data types, line budgets and lines
in use, and the routes.

## Latency

`ts_source_ns` (venue or provider time, when the source gives one), `ts_recv_ns` (adapter receipt) and
`ts_pub_ns` (bus publish) travel with each event. The browser adds receipt and paint times locally.
`GET /api/marketdata/latency` reports p50, p99 and max for each stage.
