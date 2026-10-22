# IBKR market data, historical bars and contract resolution

Status (ADR-0001 vocabulary): **implemented, tested offline. Not paper-verified.** No IBKR host has been connected.
Every quote, bar, conId, ISIN and price in the tests is a synthetic FIXTURE.

- Code: `backend/src/unknown/broker/ibkr/marketdata.py`, `history.py`, `contracts.py`
- Read-only routes: `backend/src/unknown/api/routes/marketdata.py`, registered as module `marketdata`
- Tests: `backend/tests/test_ibkr_marketdata.py` (84 tests, including 15 regression tests from the adversarial review
  in section 10). Most run on any interpreter. `SdkTests` need the official SDK and skip cleanly without it:

  ```bash
  cd backend && PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unittest tests.test_ibkr_marketdata -v
  ```

- Capability records: CAP-MKT-01, CAP-MKT-02, CAP-MKT-04, CAP-MKT-05, CAP-MKT-11, CAP-IBKR-08, CAP-IBKR-12,
  CAP-DATA-10 and CAP-INS-04 (`docs/capabilities/register.json`). CAP-MKT-03 (time and sales) is not implemented
  here.
- Licence boundary: the same as `docs/integrations/ibkr.md`. `ibapi` (GPL-3.0-or-later) is imported at runtime only
  from the owner's installation. Importing these modules or the route does not import `ibapi`, and a test enforces
  this.

## 1. Architecture

`IbkrMarketDataSession` is a **separate, read-only TWS API connection** with its own client id. The order adapter's
EWrapper does not forward tick callbacks, and keeping market data off the order connection means no request here
can affect orders. The session never calls `placeOrder`, `cancelOrder` or anything else that touches orders.

It applies the same PAPER rules as the order adapter, before and after the socket opens:

- the configuration is validated (`IbkrConfig.validate`): PAPER mode, a loopback host, the PAPER profile port, and
  the documented live ports refused;
- at handshake, `managedAccounts` must equal exactly the configured paper account. Otherwise the session latches
  `DISCONNECTED_UNSAFE` and disconnects;
- the session never automates login. With no `nextValidId` or `managedAccounts` it reports `AUTH_REQUIRED`.

`history.HistoryClient` and `contracts.ContractResolver` send their requests through the session (`request`,
`register`, `wait`), so every request is paced. The session's `maintain()` is meant to be called periodically by
the owning service, never from the message thread. It cancels dead subscriptions, resubscribes after IBKR 1101
("data lost") and writes the status file.

### Request budget (CAP-IBKR-08)

IBKR allows floor(market-data lines / 2) requests per second for the username. `split_host_budget(config,
md_config)` returns two `PacingScheduler`s whose limits sum to at most that figure. The order adapter must be built
with the first one (`IbkrAdapter(pacer=orders)`), or the two connections together could exceed the limit. This is a
service wiring change (section 7).

## 2. Owner configuration

The file is `~/.unknown/config/ibkr_marketdata.json`. It lives outside git and contains no account identifier.
Unknown keys are rejected.

```json
{
  "comment": "IBKR market-data connection (read-only)",
  "client_id": 18,
  "requested_market_data_type": 1,
  "tws_reserved_lines": 0,
  "pacing_share_percent": 50,
  "feeds": {
    "STK:USD": {
      "consolidation": "non_consolidated",
      "label": "IBKR US Real-Time Non Consolidated Streaming Quotes (Cboe One + IEX)",
      "evidence": "CAP-DATA-10: IBKR pricing page 'The free real-time streaming market data on all US-listed stocks and ETFs from Cboe One and IEX is non-consolidated'; owner's Client Portal subscription list 2026-10-22",
      "observed_on": "2026-10-22"
    }
  },
  "freshness_budgets_ms": {"display": 5000, "signal": 2000, "pre_trade": 1000}
}
```

| Key | Meaning |
|---|---|
| `client_id` | The market-data connection's API client id. It must be positive and differ from the order adapter's `client_id`. |
| `requested_market_data_type` | Sent once per connection with `reqMarketDataType`. Status always comes from what IBKR *delivers* (`marketDataType`), never from this request. |
| `tws_reserved_lines` | Lines the owner uses in TWS watchlists. Lines are shared between TWS and every API connection of the username. |
| `pacing_share_percent` | This connection's share of floor(lines / 2). |
| `feeds` | The owner's assertion about each feed, keyed `secType:currency`. `consolidation` is `consolidated`, `non_consolidated` or `unknown`. Any claim other than `unknown` needs evidence. Without an entry, the label is `unknown`, never `consolidated` (CAP-MKT-02). |
| `freshness_budgets_ms` | Owner-set budgets for `display`, `signal` and `pre_trade` (CAP-MKT-01 open question). Without them, `stale` is `null` with a reason. |

The values above are an example. The owner sets the budgets.

## 3. Level 1 quotes (CAP-MKT-01, CAP-MKT-02, CAP-IBKR-12, CAP-DATA-10)

`subscribe(instrument_id, ContractSpec(con_id=...))` sends `reqMktData(reqId, contract, "", False, False, [])`.
That is a streaming request: never a snapshot, and never a paid regulatory snapshot. A symbol needs its primary
exchange. The session never looks a contract up by name.

### Delivered type, source and the `delayed` flag

| Delivered (`marketDataType`) | Status | `Quote.source` | `Quote.delayed` |
|---|---|---|---|
| 1 live, with no delayed tick ids | `LIVE` | `ibkr:live:<consolidation>`, e.g. `ibkr:live:non_consolidated` | False |
| 2 frozen | `FROZEN` | `ibkr:frozen:<consolidation>` | **True** |
| 3 delayed (tick ids 66–76) | `DELAYED` | `ibkr:delayed:unknown` | True |
| 4 delayed frozen | `DELAYED_FROZEN` | `ibkr:delayed_frozen:unknown` | True |
| not reported yet | `TYPE_UNREPORTED` | `ibkr:unreported:unknown` | True |
| 1 reported but delayed tick ids seen | `TYPE_MISMATCH` (anomaly) | `ibkr:live:unknown` | True |
| entitlement error | `UNAVAILABLE` | `ibkr:unavailable:<state>` (empty quote) | True |
| socket down, 1100/2110, data farm down, cancelled | `STALE` / `CANCELLED` | `ibkr:stale:<why>` (empty quote) | True |

`Quote.delayed` is True for **every quote that is not proven live**, frozen included. The task asked for this on
types 3 and 4. It is extended because `risk.pretrade` checks only `delayed` and age, and IBKR L1 ticks carry no
provider timestamp. A frozen quote would otherwise have a fresh receipt-time age and pass. A test runs the real
`risk.pretrade.evaluate` and confirms that `QUOTE_NOT_DELAYED` passes only for type 1. Quote age is the receipt
time of the latest tick on the subscription (`Quote.event_time_ns` is None).

When the delivered type changes, values received under the previous type are dropped. Delayed values are never
merged into a live quote, and frozen values never become live. One audit entry is written per change. On the first
report, ticks already received are kept only if their tick-id class matches the reported type.

**Interruptions.** When a stream is interrupted, every value received before the interruption is discarded. That
covers an entitlement error (including the non-terminal 10197), 1100/2110 upstream loss (even when 1102 later says
"data maintained"), a 2103 data-farm break, and socket loss. IBKR streams only changes, so a kept value would be
republished as live, with a fresh receipt time, the moment any other field ticked. After resumption a quote holds
only values received since, and stays one-sided or empty until both sides tick. Each discard writes a
`FIELDS_INVALIDATED` audit entry.

**Publish ordering.** `QuoteBook.put` keeps whichever quote has the later receipt time. A publish therefore never
goes backwards in time for its instrument. Otherwise, after a backwards wall-clock step (NTP), a blanking publish
would be dropped and the previous live quote kept. Such publishes are counted (`publish_clock_regressions`) and
raise a `CLOCK_STEPPED_BACK` anomaly.

A `tickPrice` of -1 or 0 is stored as null, never as a price. When the paired `tickSize` of 0 follows (ibapi
`decoder.py:177-195` always sends one for BID/ASK/LAST), the null reason becomes `no data currently available`.
Quotes are classified `normal`, `locked`, `crossed`, `one_sided` or `empty`. All are kept, and each state change is
counted per subscription.

### Entitlement and request errors

Each of these becomes an explicit state with a specific cause. Texts are from IBKR's message-codes page (fetched
2026-10-22). 10089 is absent from that page; its meaning comes from CAP-IBKR-12.

| Code | State | Effect |
|---|---|---|
| 354 | `NOT_SUBSCRIBED` (no live data for the instrument on this username, or the Market Data API Acknowledgement is not signed) | unavailable; the line is freed by `maintain()` cancelling it |
| 10089 | `API_SUBSCRIPTION_REQUIRED` (free on-platform TWS data is not available through the API) | the same |
| 10186 | `NOT_SUBSCRIBED_DELAYED_DISABLED` | the same |
| 10197 | `COMPETING_SESSION` (paper and live logged in at once; IBKR prefers live) | unavailable until data resumes; not cancelled |
| 10090 | `PARTIALLY_SUBSCRIBED` | a warning; the status stays as delivered |
| 101 | `LINE_LIMIT_REACHED` | the line is freed, plus an anomaly: the local budget should have prevented it (raise `tws_reserved_lines`) |
| 200, 321, 322 | `NO_SECURITY_DEFINITION` / `REQUEST_ERROR` | inactive; the line is freed |
| 100 | pacing violation | the scheduler backs off for `pacing_backoff_s`, plus an anomaly |
| 1100/2110, 1101, 1102, 2103/2104 | connectivity | quotes are blanked as `STALE`. After 1101, `maintain()` resubscribes. 2103 makes quotes stale until 2104 arrives for the same farm. |

The entitlement error rate (`entitlement errors / requests`, CAP-IBKR-12) is in the status. The status reports the
numerator and denominator separately. Before any request has been sent, the value is `null` with a
`missing_reason`, never `0/1`.

The status lists every subscription record. `current: false` marks an earlier record of an instrument that was
re-subscribed, and `subscription(instrument_id)` always returns the current record.

### Line budget

`LineBudget` capacity = `market_data_lines - tws_reserved_lines`. A subscription beyond capacity is refused locally
(`LINE_BUDGET_EXHAUSTED`) and never sent, so IBKR error 101 is not triggered. Headroom = capacity - active
subscriptions (CAP-MKT-11).

### Status file and storage rights

`maintain()` writes `<UNKNOWN_HOME>/run/ibkr-marketdata-status.json` (mode 600, atomic replace). It contains
statuses, entitlement states, field null reasons, ages, the line budget, pacing and the audit log, but **no price
values**. CAP-IBKR-12 says to treat storage of exchange data as not permitted until the rights are verified. Prices
reach the core only through the in-memory `QuoteBook`, and the status route joins them from the core at request
time.

## 4. Historical bars (CAP-MKT-04, CAP-MKT-05, CAP-IBKR-08)

`HistoryClient.fetch(HistoricalRequest(...))` sends `reqHistoricalData(reqId, contract, endDateTime, durationStr,
barSizeSetting, whatToShow, useRTH, formatDate=2, keepUpToDate=False, chartOptions=[])`. `endDateTime` is
`"yyyymmdd HH:mm:ss UTC"`, or `""` for now.

**Local pacing.** These rules are enforced before sending, for every bar size. That is conservative: IBKR heads
them "small bars".

| Rule | Limit |
|---|---|
| identical request | not again within 15 s |
| same contract / exchange / whatToShow | fewer than 6 within 2 s |
| all requests | at most 60 in any 10 minutes |
| BID_ASK | counts twice in every rule |
| open requests | at most 50 at once |

A refusal returns `PACING_REFUSED` with the rule and a `retry_after_ns`. An IBKR-reported pacing violation (162)
blocks further historical requests for 60 s. One pacer is shared by every `HistoryClient` on a session. A 10-minute
soak test against a mock that enforces IBKR's rules records zero violations.

**Times.** With `formatDate=2`, bars smaller than a day carry epoch seconds. They are taken as the bar **start**
(IBKR convention, not stated on the fetched pages; verify on paper), so `end = start + bar size`. That end is never
earlier than the true end of a short first or last bar.

Daily bars are dated by the day they close (IBKR). For `use_rth`, a daily bar ends at the calendar's regular close
or early close (`SessionRule.from_calendar_file(config/calendars/XNYS.json)`). Without RTH it ends at the next
local midnight (conservative). Weekly and monthly bars are refused.

**Point in time.**

- `available_time = bar_end + settle_delay` (default 0 s, an owner setting; CAP-MKT-04 open question).
- `known_from = receipt time`.
- A bar is complete only if `bar_end <= min(request sent time, endDateTime)`. IBKR computes the response at an
  unknown instant between sending and receipt, and it can hold requests, so only the send time is a safe bound. A
  late answer must not turn the partial bar IBKR captured into a complete one. A bar running past the request's own
  `endDateTime` is cut off by it. Incomplete bars are returned but **never stored**.
- `BarStore.as_of(t)` returns only `available_time <= t`.
- `knowledge_cutoff_ns` restricts to the versions this system held at that instant (audit mode).
- A re-fetch with different values closes the old version (`known_to`) and links the new one (`supersedes`).

**Adjustment.**

| `whatToShow` | Label | Basis |
|---|---|---|
| TRADES | `split_adjusted` | IBKR: adjusted for splits, not dividends |
| ADJUSTED_LAST | `split_and_dividend_adjusted` | IBKR: adjusted for splits and dividends |
| MIDPOINT, BID, ASK, BID_ASK | `unknown` | not documented on the fetched pages |

IBKR supplies no unadjusted series, and an adjusted series is on the corporate-action basis of its fetch time. Rows
whose basis is later than the decision time are flagged `adjustment_basis_after_decision`. `require_unadjusted=True`
raises `UnadjustedUnavailable`. Raw prices need a licensed source (CAP-MKT-05).

**Coverage gaps.** Bars of 30 s or less reaching more than six months back are answered locally with a coverage gap
row, not an empty success. An IBKR "no data" (162) is also recorded as a gap. So is IBKR 200 ("no security
definition"): outcome `NO_SECURITY_DEFINITION`, gap reason `IBKR_NO_SECURITY_DEFINITION`. IBKR has no data for
securities that no longer trade, so this is the survivorship case (CAP-MKT-05). It must stay distinguishable from
a series that was never fetched. Gaps are metadata and are always recorded. A request the session refuses locally
(per-second budget, or not ready) is reported as `PACING_REFUSED` or `NOT_READY`, not as an IBKR error.

**Storage gate.** Bars are written only if `StorageRights.from_registry()` finds dataset `ibkr.historical_bars`
granting `storage` in `config/providers/`. **No such entry exists.** Until the owner records IBKR's storage terms,
fetched bars are returned to the caller and `not_stored_reason` says `UNAUTHORIZED`. The request is still logged.
Tests store bars only with `StorageRights.fixture_only()` (`fixture = 1`, dataset `FIXTURE.ibkr.historical_bars`).

**Tables** (research DB `<UNKNOWN_HOME>/research/research.sqlite`, never read by the core):

| Table | Holds |
|---|---|
| `mkt_bar` | provider, dataset, instrument, `bar_spec_version` (`ibkr.hist_bars@1`), bar size, `what_to_show`, `use_rth`, timezone, adjustment and note, start/end/date, OHLC/volume/WAP/count as exact decimal text, currency, receipt/available/known times, availability rule, supersedes, request id, raw, quality, fixture |
| `mkt_bar_request` | the request log |
| `mkt_bar_gap` | coverage gaps |

## 5. Contract resolution (CAP-INS-04)

`ContractResolver.resolve(ListingQuery(...), instrument_id=..., settlement_days=...)` sends `reqContractDetails`.
The query is `STK`, symbol, currency and primary exchange on `SMART`, or a conId. There is no name search, and a
query without a primary exchange is refused before anything is sent. For a single match the resolver then sends
`reqMarketRule` for the primary exchange's rule; rules are cached.

| Status | When | Routing |
|---|---|---|
| `VERIFIED` | exactly one match and every check passed | allowed |
| `AMBIGUOUS` | more than one match, or IBKR 200 "ambiguous"; no mapping is created | blocked |
| `UNRESOLVED` | no match, or IBKR 200 | blocked |
| `CONFLICT` | ISIN (when visible) ≠ master ISIN, primary exchange → MIC ≠ listing MIC, currency, conId, or LongName similarity < 0.6 (equal after removing legal-form and share-class words, else the `difflib` ratio; no prefix shortcut, which would score "Apple Inc." 1.0 against "APPLE HOSPITALITY REIT INC") | blocked |
| `CHANGED` | the instrument's current mapping names another conId | blocked until the owner calls `ContractStore.approve_remap`, which creates mapping version n+1; old rows are kept, and the remap row inherits the instrument's FIXTURE flag |
| `INCOMPLETE` | missing market rule, primary exchange not in the MIC table, unmapped `stockType`, `settlement_days` not supplied, or timeout | blocked |
| `REFUSED` | invalid query | blocked |

A verified result carries an `Instrument` proposal (`Instrument.to_json()`, validated by construction) and a
`ProviderMapping` (`ibkr`, conId, version, `verified_at_ns`).

- Tick rules come from the market rule's `lowEdge` / `increment` bands. `MinTick` alone reports only the smallest
  tick. A test proves 101.2501 is rejected and 0.5001 accepted under a 0.0001 / 0.01 schedule.
- Quantity increment is 1 share, because fractional quantities are disabled.
- IBKR does not provide leveraged or inverse flags; they are marked for owner review.
- Resolutions are appended to `ibkr_contract_resolution` in the research DB. This is contract reference data, not
  exchange market data.

The IBKR exchange → MIC table (NASDAQ→XNAS, NYSE→XNYS, ARCA→ARCX, AMEX→XASE, BATS→BATS, IEX→IEXG) has not been
checked against the ISO 10383 file. The owner should confirm it (CAP-MKT-02 open question).

## 6. Read-only API routes (module `marketdata`)

| GET | Returns |
|---|---|
| `/api/marketdata/status` | the session status file (connection, line budget, pacing, delivered types, entitlement states, audit), joined with the core's quotes to give `mid`, `spread`, `spread_bps`, `age_ms`, `stale` and `quote_state`, each with a formula id (`mkt.*@1`) and its inputs. Analytics are joined only to the `current` subscription record. When the file is older than 60 s it **fails closed**: every subscription reads `status: STATUS_FILE_STALE` and `live: false`, keeping the file's values as `reported_status` and `reported_live`. Without the file: `available: false` and `unavailable_reason`. |
| `/api/marketdata/bars?instrument=&bar_size=1 day&what_to_show=TRADES&use_rth=1&as_of_ns=&knowledge_cutoff_ns=&start_ns=&end_ns=&limit=&include_fixture=0` | point-in-time bars with lineage: formula `ibkr.hist_bars@1`, units, availability rule, adjustment and basis flag, request id, and the count excluded as not yet available. `truncated` (with a note) says when `limit` cut the result; the earliest bars are returned. Coverage gaps are listed, and when the result is empty `missing_reason` says `NOT_YET_PUBLISHED`, `COVERAGE_GAP` (with the gap reasons) or never fetched / not stored. |
| `/api/marketdata/bars/series` | stored series with version counts |
| `/api/marketdata/bars/gaps?instrument=` | coverage gaps |
| `/api/marketdata/contracts` | the contract-verification table, latest per instrument, with verification pass rate (`ins.verification_pass_rate@1`). `last_resolved_ns` is the latest attempt. `last_verified_ns` is the latest attempt that **passed**, or null. |

FIXTURE rows are hidden unless `include_fixture=1`, and are then labelled. The route module does not import the
IBKR package; it reads the files that package writes, and a test checks that the names match.

## 7. Core changes needed (not made here: other owners' files)

1. **`unknown.market.quotes.Quote`**: add `market_data_type` (live, frozen, delayed, delayed_frozen, unreported) and
   `consolidation` (consolidated, non_consolidated, unknown). The consolidation currently travels in `source`
   (`ibkr:live:non_consolidated`).
2. **`risk.pretrade`**: require `market_data_type == live` explicitly, not just `not delayed`. For LIVE mode, require
   `consolidated` unless the owner decides otherwise (STATUS.md follow-up). Non-consolidated real-time quotes stay
   allowed for PAPER only.
3. **`service/core.py`**: when `--broker ibkr`, construct the session with the desk's `QuoteBook` and
   `default_status_path()`, and build the order adapter with the first scheduler from `split_host_budget`. Call
   `maintain()` on a timer.
4. **`api/routes/__init__.py`**: add `"marketdata"` to `MODULES`.
5. **Owner decision**: record IBKR's storage terms as dataset `ibkr.historical_bars` in `config/providers/` before any
   real bar is stored (CAP-IBKR-12, ADR-0015).

## 8. Not verified / open

- Nothing has run against IB Gateway. The first paper session must confirm:
  - whether `marketDataType` arrives before the first tick;
  - that `"yyyymmdd HH:mm:ss UTC"` is accepted;
  - that intraday bars are start-stamped;
  - the actual 162 texts;
  - the volume units of historical bars.
- Weekly and monthly bars, keepUpToDate, real-time 5-second bars, tick-by-tick, depth and regulatory snapshots are
  not implemented. Snapshots are paid and never requested.
- CAP-INS-04's 500-listing preflight and CAP-MKT-01's 20-stock NBBO comparison need paper or live data.
- Open questions are unchanged: whether non-professional status covers non-display use (CAP-MKT-11), and whether
  storing IBKR data for research is permitted (CAP-IBKR-12).
- Known limitations recorded by the review, not changed:
  - With the default settle delay of 0 s, an RTH daily bar becomes available at 16:00:00, before closing-auction
    prints that its close may include. The owner should set a settle delay (CAP-MKT-04 open question).
  - `BarStore.as_of(limit=)` returns the earliest rows and does not say when it truncated. Only the route reports
    `truncated`.
  - The "same contract" pacing key treats a contract given by conId and the same contract given by symbol as
    different.
  - `as_of` does not filter on `bar_spec_version` or `dataset_id`. A future bar-spec version would return duplicate
    rows.
  - A non-2100-range warning that carries a historical request id ends the wait early and fails the request.

  Only the last fails safe, because the request fails. The others can hand a caller early, incomplete or
  duplicated inputs, and each needs fixing before that use: a strategy reading `as_of` without a window, or a
  second bar-spec version.

## 9. SDK surface (TWS API 10.51.01, `IBJts/source/pythonclient/ibapi`; the installed copy is byte-identical)

| Kind | Names and locations |
|---|---|
| EClient | `connect` client.py:442, `disconnect` :542, `isConnected` :554, `run` :586, `reqMktData` :744, `cancelMktData` :931, `reqMarketDataType` :987, `reqMarketRule` :1112, `reqContractDetails` :4150, `reqHistoricalData` :4863, `cancelHistoricalData` :5073 |
| EWrapper | `error` wrapper.py:147, `connectAck` :174, `marketDataType` :178, `tickPrice` :189, `tickSize` :196, `tickSnapshotEnd` :201, `tickGeneric` :207, `tickString` :210, `connectionClosed` :303, `nextValidId` :340, `contractDetails` :345, `contractDetailsEnd` :358, `managedAccounts` :439, `historicalData` :455, `historicalDataEnd` :473, `tickReqParams` :768, `marketRule` :817 |
| Objects | `BarData` common.py:112, `TickAttrib` :235, `PriceIncrement` :276, `Contract` contract.py:76, `ContractDetails` :142, `TagValue` tag_value.py:28, tick ids ticktype.py `TickTypeEnum` |

`SdkTests` assert every parameter list and tick id against the installed SDK. In the review all 28 line citations
above were re-checked against the official 1051.01 source and match. So does the claim that `decoder.py` follows
every BID/ASK/LAST (and DELAYED_*) `tickPrice` with its paired `tickSize`; the protobuf path does the same.

## 10. Adversarial review (2026-10-22)

An independent review reproduced each defect below with a probe before fixing it. Each fix has a regression test.
Reverting any single fix in a scratch copy turns its test red on an assertion, and an unmutated control stays
green.

| Defect | Effect before the fix |
|---|---|
| Bar completeness judged on the local receipt after the wait | a late (held) response stored the partial bar IBKR had captured as a complete bar (CAP-MKT-04 look-ahead) |
| No bound at the request's `endDateTime` | a bar cut off by `endDateTime` was stored as a full bar |
| Values kept across interruptions (10197, 2103/2104, 1100/1102) | the pre-outage ask was republished as live, with a fresh receipt time, on the first new bid tick |
| `QuoteBook.put` drops older receipt times | after a backwards clock step a 354 blanking publish was dropped, and the live quote stayed in the book while the session said UNAVAILABLE |
| `maintain()` resubscribed what it had just cancelled | a terminal entitlement error followed by a socket loss produced a fresh `reqMktData` for a dead subscription |
| Stale status file served as-is | the route reported `LIVE` / `live: true` from a session that may have stopped |
| Contract name check prefix shortcut | "Apple Inc." scored 1.0 against "APPLE HOSPITALITY REIT INC", defeating check (d) |
| IBKR 200 on a bar request | a bare `ERROR` with no gap: a delisted security looked like one never fetched (survivorship) |
| `last_verified_ns` | showed the time of a later **failed** attempt |
| Entitlement error rate | read `0/1` with no request sent |
| `subscription()` / route join | returned, or attached analytics to, an earlier cancelled record of a re-subscribed instrument |
| `approve_remap` | wrote `fixture = 0` for a fixture listing, which then surfaced unlabelled |
| Bars route `limit` | cut results silently |
| Local per-second refusal | reported as outcome `ERROR` |
| Pre-trade acceptance test | `skipTest` if `PretradeInput` gained a field, which would silently drop the CAP-IBKR-12 proof; it now fails |
