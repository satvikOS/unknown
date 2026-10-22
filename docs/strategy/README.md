# Strategy runner (ADR-0017 runtime)

Status (ADR-0001 vocabulary): **implemented and tested offline**. The tests run against a real core service: the simulated broker and the FIXTURE market, behind the command socket. The runner has never run against IBKR paper or any real market data. Nothing here is evidence that a strategy is profitable. Decision record: [ADR-0017](../adr/0017-strategy-artifacts-promotion-and-runtime.md) (still `proposed`; its status note lists what is missing). Code: `backend/src/unknown/strategy/`. Tests: `backend/tests/test_strategy_runner.py`.

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
- It writes to the core only over the command socket. It uses the owner-issued token `secrets/strategy/<artifact id>.token`, as principal `{id: <artifact id>, role: STRATEGY}`. The core binds that token to that one principal id.
- Every core rule applies unchanged: pretrade, reservation, stop, session state and mandate eligibility.

## One cycle (`run_once`)

1. **Artifact.** The runner loads the artifact from the registry and refuses to run unless all of these hold (`artifacts.load`):
   - The stored bundle still hashes to the artifact hash.
   - The registry status permits the mode.
   - The artifact has not expired.
   - This checkout's research code hash equals the registered `code_hash` (`CODE_DRIFT`).
   - The spec reads exactly one daily point-in-time dataset.
2. **Token.** Without its token, the runner makes no decision (`NO_STRATEGY_TOKEN`). Both modes record through the core.
3. **Session to decide.** The runner decides the latest *eligible* session `D`: the latest business day of the calendar whose decision instant has passed. The decision instant is `D` at the spec's `decision_clock_utc`, the same instant the backtest uses.
   - Calendars are `domain/calendar.py` data: `WEEKDAYS` for fixtures, `XNYS` from `config/calendars/XNYS.json`. Holidays and early closes are honoured.
   - Beyond a calendar's horizon, the runner refuses.
   - Missed sessions are never back-filled. Decisions of an earlier session that an outage or a crash left unrecorded are finished first (not decided again): they appear under `carried_over` in the summary.
4. **Already decided?** An instrument with a final decision for `D` is skipped. The runner looks first in its journal; if the journal was lost, it looks in the core's stored commands.
5. **Data.** Bars come from the point-in-time store through `research.bars.load_series` and the engine's `Cursor`: only bar versions with `available_ns` at or before the decision instant. An instrument is `SKIPPED_STALE_DATA` when either holds:
   - its latest visible bar is not the completed bar of `D`;
   - the calendar knows the session close and the bar ends before it (a partial bar).
6. **Targets.** Targets come from the research implementation itself. `research.strategies.build(spec).decide(...)` runs on a `DecisionContext`, which is the backtest's code path. A test checks that the runtime targets equal the backtest's `DECISION` targets at all 180 decisions of a two-instrument fixture series.
7. **Sizing.** The runner maps targets to orders with algorithm A15 (`strategy/sizing.py`). The steps are those of the engine's `_Run.plan`, in order:
   - per-name cap;
   - aggregate cap with cash reserve;
   - no-trade band;
   - desired shares;
   - limit at the mark ± `limit_offset_bps`;
   - sells capped at unreserved quantity;
   - liquidity filter;
   - sizing down to what can be reserved;
   - minimum notional.

   A test checks that the runtime orders equal the backtest's orders from a flat book. What the engine takes from its simulated ledger, the runtime takes from the core:
   - owned and unreserved quantity of the session's ledger owner;
   - leaves of the session's open BUY and SELL orders, which count as current exposure;
   - the session's available cash as the budget;
   - the active policy's fee envelope (`risk.fees.envelope`).

   The per-name and aggregate caps are applied over every target the strategy returned, as in the engine. An instrument the runner does not trade at this decision (stale data, not registered, a runner gate) keeps its share of the aggregate cap, so skipping it never enlarges the other names' weights (tested: three names at 0.45 with one stale are each sized at 0.316666, not 0.45).
8. **Journal and execute.** Every decision is written PLANNED to the journal, then executed:
   - **Live:**
     - Each intent goes out as `submit_intent`: LIMIT, DAY, `deadline_ns` = the end of its execution session (never later than the next decision instant), `purpose` ENTRY/EXIT, the session mandate's `policy_hash`, and `decision_ref` naming the decision.
     - Then `record_strategy_decision` records the decision.
   - **Shadow:** only `record_strategy_decision`, with `mode: shadow`.

| Outcome | When |
|---|---|
| `INTENT_SUBMITTED` | live; the core accepted the intent (the record names its order id) |
| `BLOCKED` | live; the core rejected the intent (the reason carries its code and failed checks, e.g. `PRICE_COLLAR`). Also used for a runner gate: instrument not registered in the core, fixture data on a non-SIMULATED account, the intent deadline passed before submission |
| `NO_TRADE` | warming up, inside the no-trade band, below minimum size, unfunded, below minimum ADV, the strategy holds. In shadow mode, also a decision that would have traded: the reason starts `SHADOW_WOULD_SUBMIT BUY 4 @ 10.05 ...`, and `target_quantity`/`current_quantity` show the gap |
| `SKIPPED_STALE_DATA` | no completed bar for the eligible session at the decision instant |

Each record carries the following:

- `inputs_digest`: SHA-256 of the artifact id and hash, the mode, the session and decision instant, the visible bars, the core exposure snapshot, the runtime's own code hash and the calendar.
- `score`: the trend z, as a decimal string.
- `target_quantity` and `current_quantity`.
- `observation_time_ns`: the decision instant.

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
sr-<YYYYMMDD>-<h16>                           client order id
```

The journal, `<home>/strategy/<account>/<artifact>.journal.sqlite`, stores the exact payloads before anything is sent. There is one journal per account and artifact, because what was decided is a fact of one account's core.

| Failure | What happens (tested) |
|---|---|
| Restart after a completed cycle | Every instrument is already decided; nothing is sent. |
| Crash after the core accepted an intent, before the journal knew | The same command is resent under the same id. The core returns its stored result (`duplicate: true`), and the decision is recorded once. |
| Core down | Decisions stay PLANNED. After the core is back (and the owner's green start), exactly the planned payloads are sent, once, while their deadline has not passed. |
| Core down past the intent deadline (or into the next session) | The intent is never sent. If the core already holds it (a crash after sending), its stored result is recorded; otherwise the decision is recorded `BLOCKED` with a `RUNNER_LATE` reason. Rows of an earlier session are finished before the new session is decided. |
| Journal lost | The core's stored commands are adopted: a recorded decision is not decided again. A stored intent without a record is recorded from the core's stored result and never resubmitted. That record carries the inputs digest named in the intent's `decision_ref`, and no target or current quantity, because those were lost with the journal. |
| Restarted in shadow mode while a live run's intents are still PLANNED | They are never sent. Each is recorded `BLOCKED` with a `MODE_CHANGED` reason, or as submitted if the core already holds it. A session's decision is made once per account, artifact and instrument, whatever the mode: switching modes does not decide that session again. |
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
5. Run it:

```bash
cd backend
PYTHONPATH=src python3 -m unknown.strategy run --artifact <id> --account SIM-1 --mode shadow --once
PYTHONPATH=src python3 -m unknown.strategy run --artifact <id> --account SIM-1 --mode live --loop --calendar XNYS
```

| Option | Meaning |
|---|---|
| `--session S` | Pins a session. The default is the account's single active session, re-read every cycle. |
| `--grace-s` (default 600) | `--loop` decides each eligible session once its decision instant plus this grace period has passed. |
| `--poll-s` (default 60) | Seconds between `--loop` cycles. Each cycle re-reads the registry status, so a quarantined artifact stops at the next cycle. |

Exit codes for `--once`:

| Code | Meaning |
|---|---|
| 0 | Decisions handled. A JSON summary is printed. |
| 2 | Refused: artifact, status, token, code drift or calendar. |
| 3 | Core unreachable: its socket, or its database (missing, unreadable, or without the account). |
| 4 | No session to record into. |

## Not implemented / known limits

- **The core mandate cannot express "shadow only".** A strategy principal needs the mandate listing to record any decision, shadow included. That same listing also permits intents. Shadow-only behaviour is enforced by the runner (registry status and mode), not by the core.
- **The core has no distinct outcome for a shadow decision that would have traded.** It is recorded as `NO_TRADE` with a `SHADOW_WOULD_SUBMIT` reason.
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
