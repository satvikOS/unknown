# Unavailable values: the reason vocabulary

Status: implemented (ADR-0020). Code: `backend/src/unknown/domain/unavailable.py`. Served by
`GET /api/meta/unavailable-reasons`.

The platform shows only real data. When a value is not available, the API sends `null` for the value and a
reason code from the table below. The terminal shows an em dash (—) that carries the code, readable on hover,
on keyboard focus and by assistive technology. An error is shown as an error. A value is never replaced by zero,
a made-up number, a sample or an example security.

This is how Bloomberg (`#N/A N/A`, `#N/A Not Subscribed`, `#N/A Field Not Applicable`, `#N/A Requesting Data`),
S&P Capital IQ (`NA` not available, `NM` not meaningful) and LSEG Workspace (`n/a` with a reason) present
missing values.

| Code | Meaning | Comparable vendor display |
|---|---|---|
| `NO_SOURCE` | no data source is configured for this value | Bloomberg `#N/A` (no source) |
| `NOT_CONNECTED` | the source is configured but not connected now | Bloomberg `#N/A Connection` |
| `NOT_ENTITLED` | the source refused it: no subscription, permission or licence | Bloomberg `#N/A Not Subscribed` / `#N/A Authorization` |
| `PENDING` | requested from the source; no value has arrived yet | Bloomberg `#N/A Requesting Data...` |
| `NO_DATA` | the source answered and has no value for this request | Bloomberg `#N/A N/A`, `#N/A History` |
| `NOT_REPORTED` | the issuer or publisher did not report this value | Capital IQ `NA` |
| `NOT_APPLICABLE` | the value does not apply to this item | Bloomberg `#N/A Field Not Applicable`; Capital IQ `NM` |
| `NOT_FOUND` | the security, series or record is not known to any configured source | Bloomberg `#N/A Invalid Security` |
| `STALE` | the last value is older than its freshness budget | stale flag (greyed, time-stamped) |
| `ERROR` | the source or the computation failed; the detail carries the error | error text, never a value |

## Field convention

* The value field is `null`.
* `<field>_reason` (or `reason` on an object describing one value) holds the code.
* An optional `<field>_detail` / `detail` / `unavailable_reason` / `missing_reason` string says more (provider
  error text, the stale age). Existing specific codes (`NOT_INGESTED`, `NO_PRICE`, `NOT_YET_PUBLISHED`, ...) stay
  in the detail and map to a vocabulary code with `unavailable.classify()`.
* A count of rows that exist is a real zero (`count: 0` for an empty list). A money amount, price, ratio or
  statistic that cannot be computed is `null` with a reason, never `0`.

## What the API does today

* Positions (`GET /api/positions`): `mark`, `market_value` and `unrealized` are `null` with `mark_reason`
  (`NOT_CONNECTED` when the core's quote book cannot be read, `NO_DATA` when no quote with a bid has arrived).
* Quotes (`GET /api/quotes`): an unreachable core is HTTP 503 with `reason: NOT_CONNECTED`, never an empty book.
* Market data (`/api/marketdata/*`): `available: false` responses carry `reason`; bars, series, gaps and contracts
  never serve FIXTURE rows (there is no `include_fixture` option); quote analytics without a quote carry
  `analytics_reason`.
* Live bus: without `config/marketdata.json` there is no source; every key's status is `UNAVAILABLE` with
  `code: NO_SOURCE`. Status events carry `code` (`docs/design/marketdata-stream-contract.md`).
* Charts: without a configured bar source that covers the security there are no bars and `reason: NO_SOURCE`; every
  source failing is `ERROR`; an empty range is `NO_DATA`.
* Research: a missing registry is `reason: NO_DATA`; FIXTURE-flagged rows are never served.
* Performance statistics: an unconfigured risk-free rate is `risk_free.annual: null` with `reason: NO_SOURCE`.
* Simulated execution: without a configured real quote source nothing fills and the core's health reports
  `sim_execution.reason = NO_SOURCE`.

Not yet swept field by field: the remaining analytics outputs (holdings, exposure, VaR, stress, allocation) keep
their existing `status`/`missing_reason` fields; mapping them to `<field>_reason` is follow-up work (ADR-0020).
