# Strategy runner (ADR-0017 runtime)

Status (ADR-0001 vocabulary): **implemented and tested offline**. The tests run against a real core service: the simulated broker and the FIXTURE market, behind the command socket. The runner has never run against IBKR paper or any real market data. Nothing here is evidence that a strategy is profitable. Decision record: [ADR-0017](../adr/0017-strategy-artifacts-promotion-and-runtime.md) (still `proposed`; its status note lists what is missing). Code: `backend/src/unknown/strategy/`. Tests: `backend/tests/test_strategy_runner.py`, `test_strategy_shadow_status.py`, `test_strategy_risk_window.py`, `test_strategy_pending_exposure.py`, `test_strategy_features.py`, `test_strategy_transmission_gates.py`.

## What it is

A strategy runner is a separate research-side process. Each runner handles **one registered artifact for one account**, in one of two modes:

- `shadow`: records decisions only.
- `live`: submits intents and records decisions.

What the runner can touch:

- It holds no broker credential and never talks to the broker.
- It reads three stores, all read-only:
  - the research registry (`<home>/research/experiments.sqlite`);
  - the point-in-time store (`<home>/research/research.sqlite`);
  - the core database (`mode=ro`, as `api/queries.py` does).
- It writes to the core only over the command socket. It uses the owner-issued token `secrets/strategy/<artifact id>.token`, as principal `{id: <artifact id>, role: STRATEGY}`. The core binds that token to that one principal id. It also asks the core for its current quotes (the socket's `quotes` query) when it prices an intent.
- Every core rule applies unchanged: pretrade, reservation, stop, session state and mandate eligibility.

## One cycle (`run_once`)

1. **Artifact.** The runner loads the artifact from the registry and refuses to run unless all of these hold (`artifacts.load`):
   - The stored bundle still hashes to the artifact hash.
   - The registry status permits the mode.
   - The artifact has not expired. The expiry is the registered one, bounded by the retention of the data its evidence rests on (BR-DAT-16, below).
   - This checkout's research code hash equals the registered `code_hash` (`CODE_DRIFT`).
   - The spec is schema v2 (`SPEC_SCHEMA_V1` otherwise): it declares the submission window, the risk budget and the feature validity windows the runtime applies.
   - The spec reads exactly one daily point-in-time dataset.
2. **Token.** Without its token, the runner makes no decision (`NO_STRATEGY_TOKEN`). Both modes record through the core.
3. **Restart check.** At the first cycle of a process, the runner re-warms the strategy from the point-in-time store and compares it with its journal (BR-DAT-18, below). A mismatch blocks new entries for the life of the process.
4. **Session to decide.** The runner decides the latest *eligible* session `D`: the latest business day of the calendar whose decision instant has passed. The decision instant is `D` at the spec's `decision_clock_utc`, the same instant the backtest uses.
   - Calendars are `domain/calendar.py` data: `WEEKDAYS` for fixtures, `XNYS` from `config/calendars/XNYS.json`. Holidays and early closes are honoured.
   - Beyond a calendar's horizon, the runner refuses.
   - Missed sessions are never back-filled. Decisions of an earlier session that an outage or a crash left unrecorded are finished first (not decided again): they appear under `carried_over` in the summary.
5. **Already decided?** An instrument with a final decision for `D` is skipped. The runner looks first in its journal; if the journal was lost, it looks in the core's stored commands.
6. **Data and features.** Bars come from the point-in-time store through `research.bars.load_series` and the engine's `Cursor`: only bar versions with `available_ns` at or before the decision instant. An instrument is `SKIPPED_STALE_DATA` when either holds (the reason names the missingness: `PROVIDER_DELAY` or `NOT_REPORTED`):
   - its latest visible bar is not the completed bar of `D`;
   - the calendar knows the session close and the bar ends before it (a partial bar).

   Its feature rows (`research.features`, below) must be usable at the decision instant; otherwise it is `BLOCKED` `FEATURE_INVALID` (an enumerated missing reason) or `FEATURE_OUTSIDE_VALIDITY_WINDOW`.
7. **Targets.** Targets come from the research implementation itself. `research.strategies.build(spec).decide(...)` runs on a `DecisionContext`, which is the backtest's code path. A test checks that the runtime targets equal the backtest's `DECISION` targets at all 180 decisions of a two-instrument fixture series.
8. **Exposure gates (BR-DAT-02).** No new intent for an instrument with an `UNKNOWN` or `CANCEL_PENDING` order in the session, whoever placed it (`PENDING_UNRESOLVED`), and at most one live intent per session, artifact and instrument (`OPEN_INTENT`). Checked at the decision and again at transmission. See "Pending exposure" below.
9. **Sizing.** The runner maps targets to orders with algorithm A15, `research.sizing.plan_orders`: the **same function** the backtest engine's `_Run.plan` calls. In order:
   - per-name cap;
   - aggregate cap with cash reserve;
   - no-trade band;
   - desired shares;
   - model limit at the mark ± `limit_offset_bps`;
   - sells capped at unreserved quantity;
   - liquidity filter;
   - the risk term (BR-DAT-04, below);
   - sizing down to what can be reserved;
   - minimum notional.

   What the engine takes from its simulated ledger, the runtime takes from the core:
   - owned and unreserved quantity of the session's ledger owner;
   - leaves of the session's open BUY and SELL orders, which count as current exposure;
   - the session's available cash as the budget;
   - the active policy's fee model, for the reservation envelope and the risk term (`risk.fees.envelope`).

   The per-name and aggregate caps are applied over every target the strategy returned, as in the engine. An instrument the runner does not trade at this decision (stale data, not registered, a runner gate) keeps its share of the aggregate cap, so skipping it never enlarges the other names' weights (tested: three names at 0.45 with one stale are each sized at 0.316666, not 0.45).
10. **Journal and execute.** Every decision is written PLANNED to the journal, then executed:
    - **Live:** each intent waits for its submission window, then is priced, journaled and sent as `submit_intent` (BR-DAT-19, below): LIMIT, DAY, `deadline_ns` = the end of the window, `purpose` ENTRY/EXIT, the session mandate's `policy_hash`, and `decision_ref` naming the decision. Then `record_strategy_decision` records the decision. At the window's end the remainder is cancelled.
    - **Shadow:** only `record_strategy_decision`, with `mode: shadow`, at the decision.

| Outcome | When |
|---|---|
| `INTENT_SUBMITTED` | live; the core accepted the intent (the record names its order id; the reason starts `TRANSMITTED in the window: limit L = min(model bound B, mid M + c bps)`) |
| `BLOCKED` | live; the core rejected the intent (the reason carries its code and failed checks, e.g. `PRICE_COLLAR`, `CONCENTRATION`, `GROSS_EXPOSURE`). Also used for a runner gate: instrument not registered in the core, fixture data on a non-SIMULATED account, `PENDING_UNRESOLVED`, `OPEN_INTENT`, `FEATURE_INVALID`, `FEATURE_OUTSIDE_VALIDITY_WINDOW`, `FEATURE_STATE_MISMATCH` (buys after a failed restart check), `SESSION_HOURS_UNKNOWN`, `NO_SUBMISSION_WINDOW`, and an intent never sent: `RUNNER_LATE` (its window ended; the reason names the last quote refusal, if any), `FEATURE_EXPIRED` (its feature values left their validity window), `MODE_CHANGED` |
| `NO_TRADE` | warming up, inside the no-trade band, below minimum size, the risk budget cannot cover the adverse move plus entry and exit fees, unfunded, below minimum ADV, the strategy holds. In shadow mode, also a decision that would have traded: the reason starts `SHADOW_WOULD_SUBMIT BUY 4 @ 10.05 ...` (the model bound), and `target_quantity`/`current_quantity` show the gap |
| `SKIPPED_STALE_DATA` | no completed bar for the eligible session at the decision instant |

Each record carries the following:

- `inputs_digest`: SHA-256 of the artifact id and hash, the mode, the session and decision instant, the visible bars, the core exposure snapshot, the runtime's own code hash and the calendar.
- `score`: the trend z, as a decimal string.
- `target_quantity` and `current_quantity` (current includes pending orders).
- `observation_time_ns`: the decision instant (an intent's record is written when it is transmitted, the next session, but names the decision instant).

The journal row of every decision also keeps, for audit and the restart check: the feature rows, the strategy's target, the risk term's inputs and result, the window, the model bound, and the transmission record (quote, mid, collar, limit, time).

## Submission window and transmission (BR-DAT-19)

TREND_DAILY_V1's execution rule (spec schema v2, `rules.execution.submission_window`), applied by the backtest and the runner through one implementation, `research.execution`:

- **Decide** after the official close of `D` (the decision instant).
- **Submit** in the next regular session, between open + N and close − M minutes (N = M = 15 for TREND_DAILY_V1), never in an auction. The session's hours come from the calendar, early closes included: XNYS on 2026-11-27 closes at 13:00 New York, so the window is 09:45–12:45 (tested). A calendar without session hours (`WEEKDAYS`) may place the window only for FIXTURE data, with the fixture convention 14:30–21:00 UTC; real data needs the venue calendar (`SESSION_HOURS_UNKNOWN`).
- **Limit at transmission.** The decision's model bound is the mark ± `limit_offset_bps`. When the window opens, the runner asks the core for its quote and validates it with the core's own rules (present, live, two-sided, younger than the policy's `quote_max_age_ms`, consolidated on a LIVE account). The limit is then `min(model bound, mid × (1 + collar))` for a buy and `max(model bound, mid × (1 − collar))` for a sell, snapped to the tick (collar 50 bps for TREND_DAILY_V1). Without a valid quote, or while the instrument has an `UNKNOWN` or `CANCEL_PENDING` order or another working intent of the artifact (the BR-DAT-02 gates, re-read from the core at transmission because the intent is sent hours after its decision), the intent stays PLANNED and is retried every cycle; after the window it is recorded `RUNNER_LATE`, naming the last refusal.
- **Write-ahead.** The priced payload is written to the journal before it is sent, so a resend after a crash sends exactly that payload under the same command id.
- **Deadline and remainder.** The intent's `deadline_ns` is the window's end. At the window's end the runner sends `request_cancel` for an accepted intent still working (one deterministic id, `strat.<artifact>.<YYYYMMDD>.<h16>.cancel`); an order already terminal is noted, not cancelled. This also happens in a cycle whose artifact is refused (quarantined, retired, expired, code drift): such an artifact decides nothing, but the remainder of an intent it already sent is still cancelled at its window's end. Nothing is carried over: the next decision recomputes from current plus pending holdings.
- **The backtest** submits at open + N, references the execution session's open (daily bars carry no quote; labelled), lets the order trade only the window's share of the bar volume (uniform intraday volume, an optimistic assumption, labelled) and cancels the remainder at close − M (`WINDOW_END_REMAINDER`). Its fill rule is still the daily-bar rule: a buy fills if the day's low is below the limit, at the open when the open is at or below the limit. Both use prices from outside the window (the open, the first N and last M minutes, the auctions), so the backtest can fill an order the runner's would not, at a price set before it was sent: optimistic, not modelled.

Tested against a real core (`test_strategy_risk_window.py`): an intent decided at the close waits for the next session's window; a stale model bound of 44.22 against a 42.00 mid is sent at 42.21 and accepted (fixed at the decision it was rejected by the core's 1% `PRICE_COLLAR`); a resting intent is cancelled at the window's end and its reservation released; no valid quote keeps it planned, then `RUNNER_LATE`. In `test_strategy_transmission_gates.py`: an owner order that went `CANCEL_PENDING` after the decision holds that instrument's intent until the cancellation is confirmed, and the other instrument's intent is sent; the resting intent of an artifact quarantined while it works is still cancelled at the window's end, by `--once` and by the loop, and so is that of a cycle that fails before deciding (its point-in-time store missing).

## Risk term (BR-DAT-04)

The buy step of A15 limits the position to the largest whole quantity Q with

```
Q × (d + c) + Fee_entry(Q − current) + Fee_exit(Q) ≤ B
```

- `d` = limit × `adverse_move_fraction`; `c` = limit × (half spread + slippage + third-party sell fee), from the spec's costs;
- `Fee_entry`, `Fee_exit` = `risk.fees.envelope`, the function the core reserves fees with (per share, the minimum per order, the estimate margin), under the active policy's fee model at runtime and the engine's schedule in the backtest;
- `B` = equity × `loss_fraction_of_equity` (TREND_DAILY_V1: 3% of equity for a 5% adverse move).

When the budget cannot cover the fees, the buy is zero and the decision is `NO_TRADE`. The risk term never creates a sell. On the 100.00 fixture session with the simulated policy's 1.00 minimum commission (1.20 reserved each way), TREND_DAILY_V1 may buy 1 share of a 10.00 instrument instead of 4, and none of a 42.00 one (tested against a real core). The book's own example (B = 0.50, d + c = 0.25, a 0.75 minimum each way) sizes to 0, where the uncorrected formula gave 2 (tested).

## Pending exposure (BR-DAT-02)

An order is pending while it is in one of the core's open states: `RESERVED`, `SUBMIT_PENDING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`, `UNKNOWN` (`core.state.OPEN_ORDER_STATES`). Its leaves count, whoever placed it in the session: a pending buy as if it fills, a pending sell as already sold. `FILLED`, `CANCELED`, `REJECTED` and `EXPIRED` orders count nothing. Tested state by state against the core (`test_strategy_pending_exposure.py`).

The core's pretrade uses the same states: `CONCENTRATION` adds the leaves of the session's open buys in the instrument, `GROSS_EXPOSURE` adds every open buy of the account. Tested: a rejection by each check because of pending buys, and acceptance of the same order without them; against a real core through the runner, an owner's resting buy counts as the runner's current exposure and in the core's checks (`CONCENTRATION (position after 40.200000 ...)` for a 1-share intent; `GROSS_EXPOSURE (81.200000 > 60.00)`).

The runner adds two gates (`runner.pending_gate`): no new intent for an instrument with an `UNKNOWN` or `CANCEL_PENDING` order in the session (it may still fill), and at most one live intent per session, artifact and instrument. Both are checked at the decision and again at transmission, from the core's open orders: a gate that closes in between keeps the intent PLANNED until it opens again or the window ends. The command id of every decision is deterministic in (artifact, session date, instrument), so reprocessing a session returns the original result.

## Features, validity and restart re-warm (BR-DAT-18)

Every decision builds one feature row per instrument and declared feature (`research.features`), under the spec's feature contract (id, version, warm-up, `freshness_budget_s`, `validity_window_s`):

- **validity window:** from the availability of the feature's newest input, for `min(validity_window_s, freshness_budget_s)` (4 days for TREND_DAILY_V1's daily features): the freshness budget (book ch. 11) bounds the window. A row must be usable at the decision (`FEATURE_OUTSIDE_VALIDITY_WINDOW` otherwise, tested with a one-minute freshness budget); an intent whose feature values have expired by transmission is never sent (`FEATURE_EXPIRED`);
- **warm-up state:** COLD, WARMING, WARM against the implementation's warm-up, which may not exceed the contract's (the contract's is an upper bound over the parameter domain);
- **missingness:** a value, or exactly one of `NOT_REPORTED` (no observation, or a partial bar), `PROVIDER_DELAY` (the session's bar is not available yet), `SOURCE_GAP` (a calendar session inside the warm-up span has no bar), `NOT_APPLICABLE` (the implementation computes no such feature), `QUARANTINED` (a non-finite value); a missing value is never imputed;
- **versions:** a v2 spec whose feature ids or versions differ from what the implementation computes is refused when the strategy is built (`research.strategies.build`: the study's backtests and the runner).

There is no persisted feature state to restore: every decision rebuilds the strategy and feeds it every bar visible at the decision instant from the point-in-time store, so the state is re-warmed deterministically on every decision. At the first cycle of a process the runner re-warms as of its journal's latest decision and compares the feature rows and (for TREND_DAILY_V1, whose targets depend only on the data) the targets with the journal's snapshot. Tested against a real core: a restarted runner reports `MATCH` and computes today's targets equal to the research code path on the store's history; after the provider silently corrects a past bar in the store, it reports `MISMATCH` and blocks every new buy (`FEATURE_STATE_MISMATCH`) for the life of the process, including a buy an earlier process planned on the mismatched snapshot and had not transmitted (recorded `BLOCKED`, never sent; `test_strategy_transmission_gates.py`). Exits stay possible.

## Evidence retention (BR-DAT-16)

The runner's expiry check uses the registry's effective expiry: the registered `expires_on`, bounded by the `retention_until` of every dataset the artifact's evidence rests on. An artifact whose evidence data was deleted is `EVIDENCE_EXPIRED` in the registry: it keeps running until its effective expiry (the summary's `evidence_status` says so, with a warning in the log), but it can never be promoted again (`docs/research/README.md`).

## Artifact identity and eligibility

There is one artifact hash: the one `research.promotion.PromotionService.register_artifact` computes. It is `stable_hash` over `{spec_hash, strategy_id, parameters, features, code_hash, dependency_lock, schema}`, and `spec_hash` covers the spec's data requirements. The ADR-0017 id is derived from it and is not a second scheme:

```
<strategy_id>@<first 16 hex of the artifact hash>      e.g. TREND_DAILY_V1@0123456789abcdef
```

`python -m unknown.strategy id --hash <64-hex hash>` prints it. A changed parameter, feature, spec or code hash is a new artifact with a new id.

Mode gating follows the research registry's own ladder (`research.promotion.LADDER`, the ADR-0017 order `proposed < reproducible < validated < shadow < paper < eligible < active`):

- `shadow` mode needs `shadow` or later. The owner promotes `validated -> shadow`, or the SYSTEM does inside an owner-approved shadow envelope (`PromotionService.auto_shadow`; `docs/research/README.md`). Tested end to end in `test_strategy_shadow_status.py`.
- `live` mode needs `paper` or later. `paper` is reached only from `shadow`, by the owner. The core separately requires the session mandate to list the id.
- `quarantined` and `retired` never run.
- On a LIVE account, live intents need `active` (implemented; not exercised by a test, because the core refuses LIVE routing in this release).

## Restart safety

Command ids and the order id are derived from `(artifact id, session date, instrument)` only (`journal.decision_ids`):

```
strat.<artifact>.<YYYYMMDD>.<h16>.intent      submit_intent command id
strat.<artifact>.<YYYYMMDD>.<h16>.decision    record_strategy_decision command id
strat.<artifact>.<YYYYMMDD>.<h16>.cancel      request_cancel at the window's end
sr-<YYYYMMDD>-<h16>                           client order id
```

The journal, `<home>/strategy/<account>/<artifact>.journal.sqlite`, stores the exact payloads before anything is sent, including the payload priced at transmission. There is one journal per account and artifact, because what was decided is a fact of one account's core.

| Failure | What happens (tested) |
|---|---|
| Restart after a completed cycle | Every instrument is already decided; nothing is sent. |
| Restart while intents wait for their window | They stay PLANNED and are priced and sent in the window by whichever process runs then, unless that process's restart check reports `MISMATCH`: its planned buys are then recorded `BLOCKED` (`FEATURE_STATE_MISMATCH`) and never sent. |
| Crash after the core accepted an intent, before the journal knew | The same command is resent under the same id, with the payload priced before the crash. The core returns its stored result (`duplicate: true`), and the decision is recorded once. |
| Core down | Decisions stay PLANNED. After the core is back (and the owner's green start), the planned intents are priced and sent, once, while their window is open. |
| Core down past the intent deadline (or into the next session) | The intent is never sent. If the core already holds it (a crash after sending), its stored result is recorded; otherwise the decision is recorded `BLOCKED` with a `RUNNER_LATE` reason. Rows of an earlier session are finished before the new session is decided. |
| Journal lost | The core's stored commands are adopted: a recorded decision is not decided again. A stored intent without a record is recorded from the core's stored result and never resubmitted. That record carries the inputs digest named in the intent's `decision_ref`, and no target or current quantity, because those were lost with the journal. |
| Restarted in shadow mode while a live run's intents are still PLANNED | They are never sent. Each is recorded `BLOCKED` with a `MODE_CHANGED` reason, or as submitted if the core already holds it. A session's decision is made once per account, artifact and instrument, whatever the mode: switching modes does not decide that session again. |
| History corrected in the store between processes | The restart check reports `MISMATCH`; new buys, including planned ones not yet transmitted, are blocked for the life of the process. |
| Artifact quarantined (or otherwise refused) while its intent works | The cycle is refused and nothing is decided, but the remainder is still cancelled at the window's end. |
| Two runners for one artifact and account | The second refuses (`RUNNER_ALREADY_RUNNING`, exclusive `flock`). The same artifact may run for another account; it has its own journal. |

The order id cannot be reused either: the core refuses `DUPLICATE_ORDER_ID`.

Only commands that this artifact's principal issued are adopted. The ids are predictable, and the core stores a command that any principal authorized for its kind sends under one of them, even one it rejects. The runner never takes such a command as its own (tested):

- a decision whose record id another principal holds is not traded (no intent without its record); it is reported `COMMAND_ID_CLAIMED` and logged as an error;
- an intent whose submit id another principal holds is recorded `BLOCKED` with `COMMAND_ID_CLAIMED`.

## Running it (owner)

1. In research, promote the artifact to `shadow` (owner approval, or the SYSTEM inside a shadow envelope) for shadow mode, and on to `paper` (owner approval) for live mode (ADR-0017 #2, `research.promotion`). Then get its id with `python -m unknown.strategy id --hash <hash>`.
2. Issue its token with `python -m unknown.cli strategy-token --artifact <id>`. Restart the core so it loads the token.
3. List the id in the policy's `session_template.strategy_eligibility`. Publish and activate the policy, then create a session. The mandate is frozen from the active template when the session is created.
4. Make sure the point-in-time store holds the spec's dataset (`research.bars.record_bars`).
5. Run it. A live runner must run `--loop` (or `--once` repeatedly) through the next session: it decides after the close and transmits in the next session's window.

```bash
cd backend
PYTHONPATH=src python3 -m unknown.strategy run --artifact <id> --account SIM-1 --mode shadow --once
PYTHONPATH=src python3 -m unknown.strategy run --artifact <id> --account SIM-1 --mode live --loop --calendar XNYS
```

| Option | Meaning |
|---|---|
| `--session S` | Pins a session. The default is the account's single active session, re-read every cycle. |
| `--grace-s` (default 600) | `--loop` decides each eligible session once its decision instant plus this grace period has passed. |
| `--poll-s` (default 60) | Seconds between `--loop` cycles. Each cycle re-reads the registry status, so a quarantined artifact stops deciding at the next cycle; it also transmits intents whose window has opened and cancels remainders whose window has ended, the latter even for a refused artifact. |

Exit codes for `--once`:

| Code | Meaning |
|---|---|
| 0 | Decisions handled. A JSON summary is printed. |
| 2 | Refused: artifact, status, token, code drift, schema v1 spec, calendar or session hours. |
| 3 | Core unreachable: its socket, or its database (missing, unreadable, or without the account). |
| 4 | No session to record into. |

## Not implemented / known limits

- **The core mandate cannot express "shadow only".** A strategy principal needs the mandate listing to record any decision, shadow included. That same listing also permits intents. Shadow-only behaviour is enforced by the runner (registry status and mode), not by the core.
- **The core has no distinct outcome for a shadow decision that would have traded.** It is recorded as `NO_TRADE` with a `SHADOW_WOULD_SUBMIT` reason.
- **The core does not enforce the submission window.** It checks `deadline_ns` only before transmission; the broker keeps a DAY order until the close. The window's end is enforced by the runner's cancellation, so a runner that is down at the window's end leaves the remainder working until the close (the next decision then sees it as `OPEN_INTENT` or `PENDING_UNRESOLVED`).
- **The backtest's transmission reference is the session open.** Daily bars carry no quote at open + N; the window's volume share assumes uniform intraday volume, which is optimistic. Both are labelled in the run config. The fill test and price still use the whole day's bar (the open, and a low or high that may lie outside the window), which is optimistic too and not modelled.
- **The restart check covers one snapshot.** It compares the journal's latest decision only, and a mismatch blocks entries for the process's lifetime, not until an owner acknowledges it: the next process compares against the newest snapshot again.
- **The backtest does not build feature rows.** On a dataset with a hole in an instrument's history or a late bar, the runner blocks that instrument (`FEATURE_INVALID` / `SKIPPED_STALE_DATA`) where the backtest decides on the bars it has. On complete data (every committed fixture) the two agree; on gappy real data they would differ.
- **The artifact identity misses part of ADR-0017 #1.** The registry's bundle has three gaps:
  - it carries dataset ids (through the spec) but no dataset versions;
  - its dependency lock is a constant string, not a lock-file hash;
  - its code hash is supplied by the caller and does not cover this runtime package.

  The runner records its own code hash in every inputs digest, so a runtime change is visible, but it does not change the artifact id.
- **Corporate-action versions (ADR-0017 #4) are not validated.** The bar schema has no corporate-action or adjustment version. Corrected bars are handled only as point-in-time bar versions.
- **IBKR historical bars are not readable by the runner.** They live in a separate `mkt_bar` table (`broker/ibkr/history.py`). The runner reads only point-in-time observation bars (`research.bars`).
- **Exposure is the session's, not the artifact's.** The core's ledger owner is the session. Positions, open orders and cash of every principal trading in the session (the owner's manual orders, another artifact the mandate lists) count as this artifact's current exposure and budget, and the session equity counts only the spec's instruments. Until the core attributes exposure per strategy, run one artifact per session.
- **The engine's strategy drawdown stop is not applied at runtime.** The core's session drawdown trigger and loss budget are the operative stops, and they are tighter.
- **Explicit (non-target) strategy orders are not supported.** They are recorded `BLOCKED`.
- **Command ids cannot be claimed by another principal** (fixed 2026-10-22). The core reserves `strat.<artifact>.` ids for that artifact's own STRATEGY principal: anyone else is refused UNAUTHORIZED, and nothing is stored under the id. The runner's `COMMAND_ID_CLAIMED` check remains as defence in depth.
