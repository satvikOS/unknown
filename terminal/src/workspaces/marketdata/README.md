# Market data workspace

This workspace shows the IBKR market-data session status from `GET /api/marketdata/status` (`backend/src/unknown/api/routes/marketdata.py`). The session (`broker/ibkr/marketdata.py`) writes a status file on every maintenance cycle, and the API joins that file with the core's quote book. This page polls every 5 s, because the file is process state outside the core event log.

Shortcut `g d`, nav order 55. Routes:

- `#/marketdata` shows the whole page.
- `#/marketdata/<instrument_id>` also opens that subscription's detail.

## What it shows

- **Session state.** Whether the session is connected and whether its PAPER identity is verified, with the reason when it is not. It also shows upstream and data-farm outages, the host and client id, and the masked account. The status file's write time and age are shown, along with the API's evidence level ("offline-tested; NOT paper-verified").
- **A stale status file fails closed.** When the file is older than 60 s, the API reports every subscription as `STATUS_FILE_STALE` and not live. The page says so in an alert. The session's last reported status still drives the counts and appears in the detail.
- **Subscriptions.** Each subscription shows its status (LIVE, DELAYED, FROZEN, UNAVAILABLE, TYPE_MISMATCH, STALE, PENDING and so on) and whether it is usable as live. It shows the **delivered** market-data type that IBKR reported, flagged when it differs from the requested type. It also shows the **consolidation** label (consolidated, non-consolidated or unknown), the entitlement state, the quote state, the last-tick age and the tick count.
- **Consolidation is explained.** A non-consolidated real-time feed is not the national best bid/offer. It validates SIMULATED and PAPER orders only; LIVE requires a consolidated feed (pretrade `QUOTE_CONSOLIDATED`).
- **Entitlements.** The entitlement error rate is shown as numerator/denominator with its formula id. Each unavailable subscription is listed with its IBKR code and the session's cause text. The codes are 354 not subscribed, 10089 API subscription required, 10186 not subscribed with delayed disabled, 10197 competing session, plus the line limit and request errors. Partial subscriptions (10090) are listed as warnings.
- **Lines and pacing.** Lines in use against capacity (with the TWS reserve), headroom, local refusals, the pacing window, violations, and the owner's freshness budgets.
- **Subscription detail.** The feed label and the evidence for its consolidation claim, the source label, which fields are present (and the reason for each null field), warnings, errors, and the API's quote analytics. When no IBKR quote reaches the core, the detail gives the reason. The status file never holds prices.
- **The session's audit log** (the last 100 entries) and its anomalies.

When no session is running, the page shows the API's `unavailable_reason` and nothing else.

## API gaps found

- The status file has no per-subscription symbol: `contract.symbol` is empty when the subscription was made by conId. The page shows the instrument id and conId.
- `/api/marketdata/bars`, `/bars/series`, `/bars/gaps` and `/contracts` exist but are not shown here yet. Contract verification belongs with Instruments, and stored bars with a chart view.
