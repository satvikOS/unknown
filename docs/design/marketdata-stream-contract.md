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
- `trade`: `{"key", "seq", "price", "size", "exchange", "conditions", "ts_source_ns", "ts_recv_ns", "ts_pub_ns",
  "source", "data_type"}`. Trades are never coalesced.
- `depth`: `{"key", "seq", "side", "levels": [{"price", "size", "mm"}], "source", "data_type", "ts_recv_ns"}`.
  Each event is a full snapshot of that side, not a delta.
- `status`: `{"key", "state", "reason"}`. `state` is one of `SUBSCRIBED`, `UNAVAILABLE`, `NOT_ENTITLED`,
  `LINE_BUDGET`, `STALE` or `ENDED`.
- `heartbeat`: `{"ts_pub_ns"}`, sent every 5 s.

`seq` increases per key and per event type. A gap means the client must re-read the snapshot from
`GET /api/marketdata/snapshot?keys=...` (same `quote` objects).

## Latency

`ts_source_ns` (venue or provider time, when the source gives one), `ts_recv_ns` (adapter receipt) and
`ts_pub_ns` (bus publish) travel with each event. The browser adds receipt and paint times locally.
`GET /api/marketdata/latency` reports p50, p99 and max for each stage.
