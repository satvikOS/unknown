# Alpaca market data (bus source)

Market data only: the adapter (`backend/src/unknown/broker/alpaca/`) never calls Alpaca's trading API. ADR-0018 covers
how sources plug into the bus; `config/providers/alpaca.json` records rights and terms.

## What was verified (2026-10-23, docs.alpaca.markets)

| Fact | Source |
|---|---|
| Stream URL `wss://stream.data.alpaca.markets/{version}/{feed}`; feeds `v2/iex`, `v2/sip`, `v2/delayed_sip` (also `v1beta1/boats`, `v1beta1/overnight`, not used) | https://docs.alpaca.markets/docs/real-time-stock-pricing-data |
| Auth `{"action":"auth","key":...,"secret":...}`; subscribe `{"action":"subscribe","trades":[...],"quotes":[...]}` | same |
| Control messages `[{"T":"success","msg":"connected"}]`, `[{"T":"success","msg":"authenticated"}]`, `[{"T":"subscription",...}]` | same, and https://docs.alpaca.markets/docs/streaming-market-data |
| Quote `T=q`: S, bx, bp, bs, ax, ap, as, c, t, z; sizes are documented as round lots; `t` is RFC-3339 with nanosecond precision | real-time-stock-pricing-data |
| Trade `T=t`: S, i, x, p, s, c, t, z | same |
| Every server message is a JSON array; error codes 400, 401, 402, 403, 404, 405 (symbol limit exceeded; existing subscriptions unchanged), 406 (connection limit exceeded; usually 1 connection), 407 (slow client), 409 (insufficient subscription), 410 (invalid subscribe action for this feed), 500 | streaming-market-data |
| Basic plan: IEX real time, "30 symbols" of websocket subscriptions; Algo Trader Plus: all US exchanges, unlimited | https://docs.alpaca.markets/docs/about-market-data-api |

Tests use fixtures copied from the documented examples (`backend/tests/test_marketdata_alpaca.py`) and a local fake
server; nothing in the test suite connects to Alpaca.

## Setup (owner)

1. Create `<UNKNOWN_HOME>/secrets/alpaca.json` (mode 0600, outside git): `{"key_id": "...", "secret_key": "..."}`.
   Without it the source stays configured and refuses keys with that reason, so routes fall back.
2. Enable it in `<UNKNOWN_HOME>/config/marketdata.json`, for example:

```json
{"sources": {"ALPACA": {"enabled": true, "feed": "iex"}, "IBKR": {"enabled": true, "client_id": 19, "lines": 40}},
 "routes": [{"match": "*.XNAS", "sources": ["IBKR", "ALPACA"]}, {"match": "*.XNYS", "sources": ["IBKR", "ALPACA"]},
            {"match": "*.ARCX", "sources": ["IBKR", "ALPACA"]}, {"match": "*", "sources": ["IBKR"]}]}
```

Options: `feed` (`iex` default, `sip`, `delayed_sip`), `symbol_limit` (default: the plan limit above; `null` for
unlimited), `url` (tests only).

## Labels

- `data_type`: REALTIME for `iex` and `sip`, DELAYED for `delayed_sip`.
- `feed`: `alpaca:iex` is a single venue (IEX), not a consolidated quote (CAP-MKT-02); `alpaca:sip` is consolidated.
- `size_unit`: `round_lots` for quote sizes, as documented. Trade sizes are passed through as received.
- `volume`, `open`, `high`, `low`, `prev_close` are null: the adapter subscribes to trades and quotes only and never
  computes a day volume from the trades it happened to see.

## Limits and open points

- One connection per account on most plans (error 406): only one process may run the Alpaca source.
- The 30-symbol limit is enforced by the bus before subscribing; a 405 from Alpaca still becomes a LINE_BUDGET status.
- Alpaca's market data agreements for display (NYSE, Nasdaq) are listed on https://alpaca.markets/disclosures; the
  owner has not reviewed them for this use (see `config/providers/alpaca.json`).
