# IBKR (TWS API) paper-trading adapter

Status (ADR-0001 vocabulary): **implemented, tested offline. Not paper-verified.** No IBKR host has been
connected. LIVE is refused in code: the configuration loader and the adapter constructor reject any mode
other than `PAPER`, and they reject the documented live ports (4001 for IB Gateway, 7496 for TWS) before
any socket is opened.

- Code: `backend/src/unknown/broker/ibkr/` (`adapter.py`, `mapping.py`, `ids.py`, `pacing.py`, `config.py`)
- Tests: `backend/tests/test_ibkr_adapter.py`
- Contract: `backend/src/unknown/broker/port.py`, ADR-0011, ADR-0012
- Source of every rule: the verified IBKR research record (`capreg/ibkr-integration.json`, records R1 to
  R14, IBKR documentation fetched and quote-checked on 2026-10-22). Code comments cite the records as
  `R<n>`.

## 1. Licence (read first)

The official TWS API Python client (`ibapi`, TWS API 10.51.01) ships with a `LICENSE` file containing the GNU
General Public License version 3. Its `setup.py` declares `GPL-3.0-or-later`. IBKR's own pages disagree:
the 2026-08-03 changelog says the TWS API is GPL from 10.49 onward, while the architecture page still calls
it "a BSD implementation". The licence file in the installer settles the question (R1).

What this repository does:

- It never copies, vendors or redistributes `ibapi`. The adapter imports it at runtime from the owner's
  local installation. A test checks that no `ibapi` source tree exists in the repository.
- Importing `unknown.broker.ibkr` does not import `ibapi`. The core (`unknown.core`, `domain`, `store`,
  `risk`, `reconcile`, `gateway`) never imports `ibapi` or the adapter. A test enforces this.
- Open legal question: running an adapter that imports a GPL library inside the owner's own process is not
  distribution. Distributing a combined work, for example a packaged app or container image that includes
  `ibapi`, may create GPL obligations. Get legal review before any such distribution, and before choosing a
  licence for the project's own code (CLAUDE.md: the owner decides).

## 2. Owner setup

### 2.1 Account

1. Open an **IBKR Pro** account. A paper account needs an approved and funded live account. IBKR creates the
   paper account within about 24 hours, and it has its own username and password (R2, R13). Paper accounts
   start with USD 1,000,000, which is not the project's authorized capital.
2. Find the paper credentials in Client Portal. The paper account ID belongs only in the local configuration
   file (section 3), never in git.
3. Use a username for TWS or IB Gateway that is not also logged into Client Portal. If the same username is
   used in both, the host cannot reconnect automatically after the nightly reset (R2). A username can be
   logged into only one trading application at a time.

### 2.2 TWS API (the SDK)

1. Download from IBKR's install page: <https://download2.interactivebrokers.com/installers/tws-api/index.html>.
   As of 2026-10-22, the page lists Stable 1050.02 and Latest 1051.01. Only the **Latest** packages list the
   Python client (R1). IBKR does not host or support `pip install ibapi`. The PyPI package is 9.81.x from
   2020, and the adapter refuses it.
2. Install into a dedicated interpreter (Python 3.11 or later; 3.12 is tested):

   ```bash
   python3.12 -m venv ~/.unknown/ibkr-venv
   ~/.unknown/ibkr-venv/bin/pip install protobuf==5.29.5           # the SDK's own pinned requirement
   ~/.unknown/ibkr-venv/bin/pip install <extracted>/IBJts/source/pythonclient
   ```

3. Record the pin and put it in the configuration (`sdk_version_pin`, `sdk_sha256_pin`). With a pin set, the
   adapter refuses to start if the imported `ibapi` differs (R1):

   ```bash
   cd backend && PYTHONPATH=src ~/.unknown/ibkr-venv/bin/python -c \
     "from unknown.broker.ibkr import load_sdk; s = load_sdk(); print(s.version, s.fingerprint, s.path)"
   ```

   Also record the SHA-256 of the installer you downloaded and the `API_VersionNum.txt` it contains.

### 2.3 Host: IB Gateway (recommended) or TWS

Install the **offline / standalone** build; the online TWS updates itself (R1). On 2026-10-22, the standalone
builds were Latest 10.51.1d and Stable 10.50.1f. Neither updates automatically, and IBKR recommends
TWS / IB Gateway 1050.02 or later. A host that is out of date returns error 503. The adapter then blocks new
orders (`HOST_OUT_OF_DATE`) but still answers queries.

Global Configuration, API, Settings:

| Setting | Required value | Why |
|---|---|---|
| Enable ActiveX and Socket Clients | on | otherwise error 502 (R2) |
| Socket port | 4002 (IB Gateway paper) or 7497 (TWS paper) | must match `port`. If you change it, set `custom_paper_port` to the same value. 4001 and 7496 are refused. |
| Read-Only API | **off only when order entry is approved**. Turning it off is an explicit owner decision; record the date. | Read-Only is on by default and blocks all API orders (R2) |
| Trusted IPs / allow connections from localhost only | 127.0.0.1 only | the adapter refuses any host that is not loopback |
| Master API client ID | leave empty, or use an id different from the adapter's `client_id` | Without it, `execDetails` and `openOrder` return only the adapter's own client (R5). `reqAllOpenOrders` still lists every client's open orders. |
| Maintain and resubmit orders when connection is restored | **recommendation: off**, recorded as an owner attestation | On by default. Orders received while connectivity is lost are saved and resubmitted when it returns (R7), so an order the adapter reported as UNKNOWN can still be transmitted later. If you leave it on, treat every UNKNOWN as possibly live until reconciliation proves otherwise (the core already does this). |
| Reject messages above maximum allowed message rate vs applying pacing | owner choice. R8 recommends **reject**, so violations are visible (error 100) instead of silently delayed. | The adapter paces locally either way |
| Order precautions / bypass | owner decision | The core's pre-trade checks never depend on IBKR precautions. Precaution dialogs can leave API orders unconfirmed (R4 open question). |

Global Configuration, Lock and Exit:

- Choose **Never lock Trader Workstation** and **Auto restart** (R9). Set the restart time outside IBKR's reset
  windows and outside the market hours you trade. North America: 00:15 to 01:45 ET Sunday to Friday, and
  00:00 to 02:00 ET on Saturday (R7).
- Plan for one manual login per week with 2FA (IB Key, Handy Key, SMS or DSC+). IBKR's documents conflict on
  when it is due: "the weekly authentication cycle starts on every Monday" versus "after the Saturday-night
  server reset". Section 5 tracks this.
- **No headless operation and no login automation.** IBKR does not support either. The adapter never drives
  the login window and never stores 2FA secrets. When the host is not logged in, the adapter reports
  `AUTH_REQUIRED: manual login in TWS/IB Gateway (2FA); no automation`.
- The Mac must not sleep while the host runs. A sleeping host is not continuous.

## 3. Local configuration (outside the repository)

Default path: `~/.unknown/ibkr.json`, mode 600. `load_config()` refuses a file, or a `state_dir`, that is
inside any git work tree. Unknown keys are rejected so that typos fail loudly.

```json
{
  "comment": "IBKR paper adapter - local only, never commit",
  "mode": "PAPER",
  "host_type": "GATEWAY",
  "host": "127.0.0.1",
  "port": 4002,
  "client_id": 17,
  "expected_account_id": "DUXXXXXXX",
  "host_time_zone": "America/New_York",
  "state_dir": "~/.unknown/ibkr-state",
  "base_currency": "USD",
  "currencies": ["USD"],
  "expected_account_type": null,
  "market_data_lines": 100,
  "reserved_pacing_percent": 20,
  "sdk_version_pin": "1051.1",
  "sdk_sha256_pin": "<output of load_sdk().fingerprint>"
}
```

| Key | Meaning |
|---|---|
| `mode` | must be `PAPER`. Anything else is refused. |
| `host_type`, `port`, `custom_paper_port` | The port must equal the PAPER profile port: 4002 for GATEWAY, 7497 for TWS, or `custom_paper_port`. |
| `client_id` | Fixed per deployment, positive, never 0. Client 0 binds manual TWS orders (R3), and this release never binds. If the host reports the id in use (326), the adapter latches `DISCONNECTED_UNSAFE` (ADR-0011 #4). |
| `expected_account_id` | The paper account id. The handshake's managed-account list must equal exactly this one id, or the adapter disconnects and latches. |
| `host_time_zone` | IANA zone of the host's "since midnight" execution day. Also used for execution times that carry no zone suffix. IBKR does not state which zone applies (R5), so confirm it on paper. |
| `expected_account_type` | Optional. When set, an `AccountType` that differs makes every snapshot incomplete (R11). Fill it in only after you have seen the real string. |
| `market_data_lines`, `reserved_pacing_percent` | Requests per second = floor(lines / 2) (R8). The reserved share is kept for cancels and reconciliation. |
| `execution_last_n_days` | `null` by default. IB Gateway returns executions since midnight only, and `lastNDays` is unverified (R5). |
| timeouts (`connect_timeout_s`, `submit_timeout_s`, `cancel_timeout_s`, `request_timeout_s`), `pacing_wait_s`, `pacing_backoff_s`, `allocation_retention_days` | Bounded waits. A submit or cancel timeout is always UNKNOWN. |
| `sdk_version_pin`, `sdk_sha256_pin` | R1 pin for the imported `ibapi` |

`state_dir` holds `ibkr-order-ids-<hash>-c<client_id>.json` (mode 600). It stores only a hash of the account
id. **Back it up and never delete it while orders may be live.** It is what makes order ids crash-safe, and it
is what allows `lookup` to answer "never transmitted".

## 4. Behaviour summary

**Connection and identity (R2).** `connect()` opens the socket, with the handshake bounded by
`connect_timeout_s`. It then waits for both `nextValidId` and `managedAccounts`. It sets `mode_verified` only
when (a) the configured port is the PAPER profile port, (b) the managed accounts equal
`{expected_account_id}`, and (c) the mode is PAPER. No request is sent before that. On a mismatch, it
disconnects with zero subscriptions and latches `DISCONNECTED_UNSAFE` until a new adapter is constructed.
`capabilities()` raises `IbkrNotReady` until verification, because the port has no "unverified" account mode.

**Order ids (R3).** `allocate_broker_order_id(client_order_id)` returns `max(nextValidId, persisted max + 1)`,
written with fsync to the file and its directory before it returns. It is idempotent per client order id.
`submit()` persists "placeOrder about to be called" before it calls `placeOrder`. If an unsent allocation has
fallen below an id that was already transmitted, it is replaced, because sending it would only earn error 103.
`broker_order_id` is `str(orderId)`, and `permId` is recorded when it arrives. Error 103 rejects the order and
asks the host for a fresh id (`reqIds`) before the next entry.

**submit (R3, R4).** The adapter builds a stock `Contract`: `conId` when `provider_ref` is numeric, otherwise
symbol, `SMART` and the currency. The `Order` is LMT, DAY, `orderRef` = the ADR-0011 tag, `outsideRth=False`,
`transmit=True`, `account` = the configured paper account. Outcomes:

- ACCEPTED on PreSubmitted, Submitted, Filled or WarnState, or on PendingSubmit when the deadline passes.
- REJECTED on 201, 202, 103, 110, 200, 203, 426, 434, 435, 10268 or 10269 with no fills.
- UNKNOWN on timeout, on socket or upstream loss, on Inactive, or on a transport error after the send.
- A second submit of a client order id that was already transmitted is never re-sent; it returns UNKNOWN.
- Local refusals carry the reason prefix `NOT_TRANSMITTED`. They cover validation, entry blocks, pacing and
  disconnection, and in each case `placeOrder` was never called.

**cancel (R4).** `cancelOrder(orderId, OrderCancel())` is the 10.51.01 signature. Outcomes:

- ACCEPTED on PendingCancel, PreCancelled or Cancelled, or on 202 after our own request.
- REJECTED on 104, 135, 136, 161, 10147 or 10148, or when the order is Filled.
- UNKNOWN on timeout.

Acceptance is not confirmation. The core waits for the CANCELED status.

**Status normalization (R4).**

| IBKR status | Normalized status |
|---|---|
| PendingSubmit | PENDING_SUBMIT |
| PreSubmitted, Submitted | ACKNOWLEDGED, or PARTIALLY_FILLED when filled > 0 |
| WarnState | ACKNOWLEDGED, with a warning in the reason |
| Filled | FILLED |
| PendingCancel, PreCancelled | PENDING_CANCEL |
| Cancelled | CANCELED, or REJECTED when a 201 preceded it with no fills, or a 202 preceded it with no fills and no cancel request from us |
| Inactive | INACTIVE |
| any undocumented string | no event; an anomaly is recorded |

Identical messages are de-duplicated on the R4 key (permId or (clientId, orderId), status, filled, remaining,
avgFillPrice, lastFillPrice, whyHeld, mktCapPrice). Foreign orders never share our id namespace: their
`broker_order_id` is `perm:<permId>` or `client:<id>:order:<id>`.

**What the core does with INACTIVE (decision 2026-10-22; ADR-0009 amendment, `core/order_machine.py`).** The core
reads Inactive as an order that exists but is not working: a precaution, a hold, or a rejection whose reason has not
arrived yet. Nothing in the status says it is final, so the core no longer releases it as REJECTED. The order becomes UNKNOWN and keeps every reservation; the report is kept as
`broker_held`, and a blocking `unknown-order:<id>` case opens. A buy also gets a cancel request. The reservation is
released only by a later terminal status (Cancelled, Filled, or a rejection), or by the owner's
`close_unknown_order` on broker evidence after a reconciliation shows nothing working (RB-13). A locally terminal
order that a reconciliation finds INACTIVE is a blocking anomaly, and a buy gets a cancel. `TERMINAL_BROKER_STATUSES`
in `broker/port.py` no longer lists INACTIVE. Which real sequences lead to Inactive (precautions, holds, rejects
whose reason arrives later) is still to be captured on the paper account (section 5, item 4).

**Executions and fees (R5).**

- `exec_key` is `ibkr:<account>:<execId>`. BOT maps to BUY and SLD maps to SELL. Shares come from the
  Decimal; the price comes from the float through `repr`, and the raw value is kept.
- Times parse as `YYYYMMDD-HH:mm:ss` (UTC) or as `YYYYMMDD HH:mm:ss <IANA zone>`. With no zone suffix, the
  time uses `host_time_zone` and is marked `assumed`. An unparseable execution is held as unrepresentable and
  makes `executions_since` incomplete. It is never booked at a guessed value.
- Corrections: a higher revision of the same base sets `correction_of` to the latest prior revision. If no
  prior revision was observed, it points to the presumed previous id, so the core applies a correction or
  opens an orphan-correction case instead of booking an additive fill.
- Commission reports are joined by execId in either order. An unset double becomes null and is never 0.
- Executions with permId 0, or from another client, carry `client_order_id=None` and raise an anomaly.

**executions_since.** It calls `reqExecutions` with an empty `ExecutionFilter` and waits for `execDetailsEnd`.
The watermark indexes the adapter's append-only logs. Overlap is harmless, and the reconciler's convergence
check sees an empty batch when nothing is new.

`complete=False` in any of these cases:

- `execDetailsEnd` did not arrive.
- An execution is unrepresentable.
- The watermark predates what IBKR can still return (`HISTORY_GAP`). That happens when it was issued on an
  earlier host day by another process, or when a disconnect spanned midnight. The owner clears it with
  `acknowledge_history_gap(through_day, by=..., note=...)` after reconciling from statements or a Flex Query.

**account_snapshot (R6, R11).**

- Requests: `reqAccountSummary(reqId, "All", "AccountType,NetLiquidation,TotalCashValue,SettledCash,AvailableFunds")`
  and `reqPositions()`. Both are cancelled after every snapshot, and a third summary subscription is refused.
- Cash: settled = SettledCash and total = TotalCashValue. AvailableFunds and NetLiquidation are kept in raw
  only. Values are about 3 minutes stale by IBKR's design.
- `complete` requires both End callbacks, every cash value set, and AccountType equal to the expected type
  when one is configured. Zero-quantity positions are skipped.

**open_orders (R3).** `reqOpenOrders` (our client) and then `reqAllOpenOrders` (every client, plus TWS). The
result is complete only after both `openOrderEnd` callbacks. Other clients' orders and manual orders are
reported with `client_order_id=None`.

**lookup.** It tries open orders, then `reqCompletedOrders(True)` (matched by permId, client and order id, or
`orderRef`), then executions. Results:

- `found=False` only when an id was allocated but `placeOrder` was never called, according to the persisted
  state.
- `found=None` when disconnected, when there is no allocation record, or when the order was transmitted but no
  trace exists.

**Connectivity (R7, R9).**

| Signal | Result |
|---|---|
| 1100 or 2110 | `ConnectivityEvent(connected=False)`; new entries blocked |
| 1101 or 1102 | restored (1101 means market data was lost) |
| 1300, `connectionClosed`, 504, or the message loop ending | SOCKET_DOWN |
| 326 | latched unsafe |
| 503 | READ_ONLY entry block |
| 100 | pacing back-off; a second one is an entry block |
| 404, 426, or `whyHeld=locate` | SHORT_SALE_SIGNAL entry block, until `clear_entry_block` |
| "Login failed ..." | AUTH_REQUIRED |
| 2104, 2106, 2107, 2108, 2158, 2119 | informational, never alert |

**Pacing (R8).** A sliding one-second window of floor(lines / 2) requests is shared by every request class.
A reserved share is kept for `cancel` and `reconcile`.

**Evidence (R13).** Every execution, commission and status raw record carries `evidence_level: BROKER_PAPER`.

## 5. Evidence checklist to move capabilities to `paper-verified`

Run each item against the owner's paper account with IB Gateway at the pinned build. Keep the raw callback
journal and screenshots outside the repository, and record the outcome in `docs/status/STATUS.md`. A
capability moves to `paper-verified` only when its item passes.

**Read-only items: the evidence runner.** With IB Gateway logged in on the paper port, run:

```bash
cd backend
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.evidence --account IBKR-PAPER-1
```

The runner uses its own client ids, 92 and 93. It refuses 0 and the ids of the order adapter, the core's
market-data session and the bus source. It runs every item below that needs no order:

- identity on both of its connections (item 2);
- the negotiated server version and the SDK fingerprint (item 1);
- clock skew against the paper policy;
- contract resolution of the registered universe. These are the instruments with an `ibkr` mapping in the core
  database, which it opens read-only. `--universe` is used only when nothing is registered;
- the market-data type IBKR delivers for each instrument;
- the account summary and positions snapshot that reconciliation uses (item 7: `AccountType`, `SettledCash`,
  `TotalCashValue`);
- the open orders of every client (item 8);
- the executions and commission reports IBKR still returns (item 6, including the raw `Execution.time` text);
- the error mapping on a deliberately invalid read: contract details for conId 1, where IBKR 200 must map to
  UNRESOLVED.

It writes `~/.unknown/evidence/ibkr-paper-<UTC>.json` (mode 600) with a `.md` table beside it, never into the
repository, and prints the table. Each item is PASS, FAIL, INCONCLUSIVE (for example a frozen market-data type
outside regular hours), NOT_RUN (with the reason) or OWNER_SESSION. Exit code 0 means every read-only item passed.
The runner places, modifies and cancels nothing, never presses start and writes nothing to the core.

Every item that needs an order, a configuration change or a Gateway restart is listed as OWNER_SESSION with its
exact steps, and the runner never executes it. That covers the rest of items 1 and 2, items 3 to 5, the order parts
of items 6 and 7, the manual order in item 8, and items 9 to 11. Tests: `backend/tests/test_ibkr_evidence.py`
(fake IBKR hosts).

1. **SDK and host pin (R1).** Installer SHA-256, `API_VersionNum.txt`, `load_sdk()` fingerprint, host build and
   negotiated server version recorded. Changing the pin makes the adapter refuse to start. A PyPI-installed
   `ibapi` is refused.
2. **Identity (R2).** A paper login lists only the paper account id. A deliberately wrong
   `expected_account_id` leads to disconnect plus `DISCONNECTED_UNSAFE` with zero requests in the API log.
   A second client with the same `client_id` produces 326 and is latched. A stopped gateway produces 502.
   Read-Only API on rejects orders, and the reject is surfaced.
3. **Order ids (R3).** Kill the process between allocation and send, and again after send. On restart, no id is
   reused, there are no duplicate orders, and `lookup` returns False and None respectively. A forced
   duplicate id produces 103, a rejection, and a resync.
4. **Status table (R4).** Capture real sequences for these cases:
   - a resting limit order, then cancel (PendingCancel, then Cancelled, plus any 202 text)
   - a marketable limit far through the market (expect 202)
   - a partial fill, then cancel
   - an order held by precautions (which status: PreSubmitted, Inactive or WarnState?)

   Duplicate status streams must produce one event each.
5. **orderRef (R3).** Check whether `orderRef` is accepted and echoed in `openOrder`, `execDetails` and
   `completedOrder` for an individual IBKR Pro account, and find the maximum length. This decides
   `order_reference_max_length`, which stays 0 until then.
6. **Executions (R5).** Capture the exact `Execution.time` string format (it decides the time parser), a
   multi-fill order with one commission report per execId and its arrival order, a mid-day restart whose
   backfill matches the live journal, `lastNDays=2` on the next day on IB Gateway, and a correction if one
   occurs.
7. **Snapshots (R6, R11).** Record the real `AccountType` string, SettledCash and TotalCashValue after a buy
   (and after settlement), the 3-minute update cadence, `reqPositions` agreement with the ledger after a
   1-share buy, and local refusal of a third summary subscription.
8. **Open and completed orders.** A manual TWS order appears as foreign in `open_orders`. Record the
   `completedOrder` fields that are populated (orderId, clientId, permId, status strings, `completedStatus`).
9. **Connectivity (R7, R9).**
   - Disable the network for 60 s while an order is working, and record the 1100 / 1101 / 1102 sequence.
   - Find out whether a code follows 2110. The adapter clears UPSTREAM_DOWN only on 1101/1102 or on reconnect.
   - Kill and restart IB Gateway: SOCKET_DOWN, then reconnect and full reconciliation. No process is restarted.
     The core's health query shows `broker_connection` going from `reconnecting` to `connected`, and
     `market_data` going from `RECONNECTING` to `CONNECTED`. `GET /api/marketdata/sources` shows the bus source's
     state. Offline tests: `backend/tests/test_ibkr_reconnect.py`.
   - Observe one nightly reset and one weekly re-login, and record what was actually needed and when.
10. **Pacing (R8).** A soak at 1.2 x R_max shows no error 100. Cancels still go out while entries are
    saturated.
11. **Fidelity (R13).** Record paper-only behaviour seen in this run, for example a dividend that was not
    processed and top-of-book fills.

## 6. Open questions (verbatim scope of the research record, plus adapter-specific ones)

R1 SDK and versions:

- Which GPL version applies to TWS API 10.49+? Does it cover every language client? How does it reconcile with
  "BSD implementation"? The 10.51.01 Python client ships a GPL v3 `LICENSE`, and its `setup.py` says
  GPL-3.0-or-later.
- Does the Stable 1050.02 installer contain the Python client?
- Is the PyPI "IBG LLC" publisher controlled by IBKR?
- How do API "1050.02" and host "10.50.1f" version strings map to each other?
- Does vendoring GPL SDK source create obligations if the repository becomes public? The adapter never
  vendors it; legal review is still needed before any distribution.

R2 connection and identity:

- Does a paper login's handshake list only the paper account id?
- Which IBKR entity holds the owner's account?
- Should Read-Only API stay on for a monitoring host and be off only on a separate order-entry username?

R3 order identity:

- Is `orderRef` preserved and echoed for individual IBKR Pro accounts?
- Can nextValidId move backwards after a sequence reset on another host?
- Is permId present in the first orderStatus for every order type?

R4 statuses:

- Which status do API orders show when precaution dialogs are not bypassed?
- Does paper emit the same 201 and 202 price-check rejections as live?

R5 executions:

- Does a corrected execution (X.02) get its own commission report, and does it replace the earlier fee?
- How many days does `lastNDays` return on TWS and on IB Gateway at 10.50 / 10.51?
- Is `exec_rev` always numerically increasing in correction order?
- What is the Flex Query retrieval path and licensing?

R6 snapshots:

- Does paper SettledCash follow the settlement cycle, given that paper skips some corporate actions?
- Is reqPnL a better real-time cross-check than the 3-minute account values?

R7 connectivity:

- With resubmission enabled, are orders placed during 1100 acknowledged before restore?
- Does paper emit 1100, 1101 and 1102 during resets with live semantics?
- Which data-centre region serves the owner (this decides the reset window)?

R8 pacing:

- Do placeOrder, cancelOrder and whatIf all count equally toward lines / 2? The adapter assumes they do.
- What is the Order Efficiency Ratio threshold for an individual account?
- Is L / 2 floored for odd line counts? The adapter floors it.

R9 restarts and 2FA:

- Is the weekly re-login due Monday, or after the Saturday reset?
- Which 2FA method will the owner use, and how does it behave at the weekly login?

R10 what-if (not used by this adapter):

- Does a what-if consume an orderId and pacing budget?
- Are paper what-if commissions computed on the live plan?
- What do the margin fields show for a cash account?

R11 cash account:

- What AccountType string does IBKR report for cash and for margin individual accounts?
- Does IBKR India offer the same cash-account semantics?
- Can a limited-permission (no-withdrawal) username keep API trading rights?

R12 market data (not used by this adapter):

- Which subscriptions cover NSE/BSE and the main European venues for API use, and at what price?
- May API ticks be stored locally and used for non-display purposes?
- Does sharing to paper carry delayed-data behaviour for instruments without a subscription?
- Does a paper username sharing live subscriptions get real-time equity data?

R13 paper fidelity:

- Are SMART-routed market orders "exchange-directed" for the partial-remainder rule?
- How long are paper statements kept?

R14 Web API (secondary path, not implemented):

- Can an individual get first-party OAuth?
- Is the qa sandbox open to individuals?
- Is the Web API the same for IBKR India and IBKR Ireland?
- What does `/iserver/account/orders` return?

Adapter-specific (found while implementing against the 10.51.01 source):

- What is the exact `Execution.time` string format? No fetched page shows a sample. The adapter accepts
  `YYYYMMDD-HH:mm:ss` (UTC) and `YYYYMMDD HH:mm:ss <zone>`, and holds anything else as unrepresentable.
- Which `completedOrder` fields are populated? The classic decoder does not decode orderId or clientId for
  completed orders, so matching relies on permId or `orderRef`. Which status strings appear (for example
  "ApiCancelled")?
- Is 2110 followed by 1101 or 1102 when connectivity returns?
- Does `managedAccounts` for a paper login include anything besides the paper account id, for example for a
  Friends and Family structure?
- What exact error code accompanies the weekly "Login failed" message? The adapter matches the text.

## 7. Wiring (implemented 2026-10-22; not paper-verified)

```bash
cd backend
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev
```

- **`unknown.broker.ibkr.serve`.** Builds the adapter from `~/.unknown/ibkr.json` and runs the generic `CoreService`. The dependency points from the adapter to the core, and the core never imports the GPL adapter (ADR-0014; enforced by `ImportBoundaryTests`).
- **Instrument mapping.** `instrument_resolver` maps an IBKR conId to the project instrument id through the `provider_mappings` rows with provider `ibkr`. The service copies them each cycle into a dict, so the adapter's message thread never touches SQLite.
- **Reconnects.** The service reconnects with capped exponential backoff (5 s doubling to 300 s). It never retries a `DISCONNECTED_UNSAFE` latch and never logs in for the owner.
- **Host clock.** It is measured with `reqCurrentTimeInMillis` (server version 197 or later) after READY and whenever the last answer is older than 30 s. A measurement older than 120 s is withheld, so preflight's skew check fails closed.
- **Unready adapter.** Health reports an unknown account mode while the adapter is not ready, and preflight blocks on `BROKER_ACCOUNT_MODE_UNKNOWN`.
- **Owner actions on adapter state.** These are recorded by the core first, then applied:
  - `python -m unknown.cli broker-admin --account IBKR-PAPER-1 clear-entry-block <NAME> --note "..."`
  - `... ack-history-gap <YYYY-MM-DD> --note "..."`, which forces a fresh reconciliation.
  - `python -m unknown.cli alerts` lists adapter anomalies and entry blocks.
- **Order ids.** `submit()` allocates the IBKR orderId and fsyncs it before `placeOrder`. A crash therefore never reuses an id, and a re-submission of a transmitted client order id is reported UNKNOWN, never re-sent.
- **Broker-event journal (BR-ORD-20; ADR-0012 amendment).** IBKR does not redeliver callbacks, so `CoreService` appends every event drained from the adapter to `~/.unknown/data/<account>.events` (fsync'd, hash-chained) before applying it. At start it applies whatever an earlier process journaled but did not apply. Health reports `event_journal`, and RB-15 covers a full or corrupt journal. An event the adapter had received but the gateway had not yet journaled when the process died is still recovered only by reconciliation, inside the `executions_since` history window.

## 7a. Owner steps for the first paper session (account switched to IBKR Pro Tiered on 2026-10-23)

1. **Client Portal:** share the live account's market data with the paper account (the paper account's
   settings). Until that is in effect the paper account gets delayed data, and the core refuses buys on
   delayed quotes (`Quote.delayed`, CAP-MKT-01).
2. **IB Gateway**, logged in with the paper username: API settings as in section 2.3 (socket clients on,
   port 4002, 127.0.0.1 only). The check below works with Read-Only API on or off.
3. **Run the read-only connectivity check** (`unknown.broker.ibkr.check`):
   ```bash
   cd backend
   PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.check --account IBKR-PAPER-1 [--client-id 90] [--symbol AAPL:NASDAQ] [--json]
   ```
   It opens one connection with a dedicated client id (default 90; refused if it equals the order adapter's
   or the market-data connection's id, so it can run while `serve` is up). It verifies the PAPER identity
   (paper port, `managedAccounts` equals the configured paper account) and prints the server version. It
   measures clock skew with `reqCurrentTimeInMillis` (server version >= 197), at the midpoint of the round
   trip, against the policy's `max_clock_skew_ms` (2,000). It reports the market-data type IBKR **delivered**
   for one streaming AAPL request and whether real-time is entitled: type 1 with no delayed tick ids. Type
   3/4 or an entitlement error (354, 10089, 10186, 10197) means no. Type 2 (frozen, usually a closed market)
   or no report means unknown. It then cancels the request and disconnects. It places no order, sends nothing to the core, writes no
   status file and stores no price. Exit 0 only if every check passes, 1 otherwise, 2 if the configuration
   is missing or invalid.
4. **Bootstrap**, `--dry-run` first (section 7b).

**Pricing.** The account uses IBKR Pro **Tiered** US stock pricing. Tiered applies to SmartRouted orders
only ("directed API orders cannot use the Tiered fee structure"); the adapter sends every order SMART. Under
Tiered **every order pays at least the USD 0.35 per-order minimum**, plus third-party fees. The page also
says that when the 1% cap is below the minimum, the cap is charged. `config/fees/ibkr-pro-tiered-us.json`
records each parameter with its page, retrieval date (2026-10-23) and status:

| Component | Value | Status |
|---|---|---|
| Commission, monthly volume <= 300,000 / to 3M / to 20M / to 100M / above | USD 0.0035 / 0.0020 / 0.0015 / 0.0010 / 0.0005 per share, marginal within the month | verified |
| Minimum / maximum per order | USD 0.35 / 1% of trade value | verified |
| Exchange (remove liquidity, >= USD 1.00) | USD 0.0030 per share on NASDAQ, NYSE and ARCA; 0.3% of value below USD 1.00 | verified for those three venues; other venues unverified |
| Clearing (NSCC, DTC) | USD 0.00020 per share, max 0.5% of trade value | verified |
| NYSE / FINRA pass-through | commission x 0.000175 / x 0.00056 | verified |
| FINRA CAT | USD 0.000003 x quantity | verified |
| SEC transaction fee (sells) | 0.0000206 x value of sales | verified |
| FINRA TAF (sells) | USD 0.00 for trades 2026-10-01..2026-12-31 (fee holiday) | verified for that window; after it **unverified**, bounded at USD 0.000200 per share |

`risk.fee_schedule.TieredSchedule.charge` is the expected fee of a fill. `pretrade_bound` is a conservative
envelope: the first-tier rate with no discount or cap, the minimum, the remove-liquidity fee on every share,
and the unverified bounds. A USD 100 buy of 4 shares @ 25.00 is charged USD 0.35 commission and USD 0.36307
in all; a USD 300 buy of 12 shares, USD 0.35 and USD 0.388694. The core's pretrade reservation still uses the
paper policy's fee model (USD 0.005/share, USD 1.00 minimum, +20%, so USD 1.20 per buy). Tests prove that it
covers the Tiered bound for every order up to the policy's USD 100 `max_order_notional`. It does **not** cover
it above that for low-priced stocks: 300 shares @ USD 1.00 reserve USD 1.80, against a USD 2.01 bound. So
raising the order limit needs a fee-model review. The plan's effective date is owner-reported; the first paper
commission reports are the evidence that Tiered is in force.

## 7b. Paper bootstrap (after the paper account is live and IB Gateway is logged in)

```bash
cd backend
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev &
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.bootstrap_paper --account IBKR-PAPER-1 \
    --deployment mac-dev --universe AAPL:NASDAQ,MSFT:NASDAQ --dry-run      # resolve only
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.bootstrap_paper --account IBKR-PAPER-1 \
    --deployment mac-dev --universe AAPL:NASDAQ,MSFT:NASDAQ
```

Bootstrap checks the passphrase first. It then:
1. registers the PAPER account and activates `config/policies/paper-default.v1.json`;
2. resolves each listing by symbol and primary exchange on the read-only market-data connection, and registers only VERIFIED contracts with their conId mapping;
3. asks you to attest that no other deployment trades the account, then acquires ownership;
4. waits for the opening reconciliation and authorizes the policy's capital.

It never starts trading. Green start is yours, in the terminal after reviewing preflight. Then work through the evidence checklist in section 5.

## 8. Tests

```bash
cd backend
PYTHONPATH=src python3 -m unittest tests.test_ibkr_adapter -v                       # pure tests; adapter tests skip
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_ibkr_check.py' -v      # connectivity check (fake host)
PYTHONPATH=src python3 -m unittest tests.test_fee_schedule -v                       # Tiered fee schedule
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unittest tests.test_ibkr_adapter -v # all tests, needs ibapi
```

The adapter tests drive a `FakeHost` (a subclass of `ibapi.client.EClient` with no socket) and feed real
`ibapi` objects through the EWrapper callbacks. A signature test pins every EClient method and EWrapper
callback the adapter uses to the installed SDK. Offline results are not evidence of IBKR compatibility.
