# Research engine (increment I5)

Status words follow ADR-0001. Everything here is **implemented** and **tested offline** on SYNTHETIC fixtures (Python 3.12 and 3.14). Nothing is paper-verified or live-verified. Passing these tests is evidence about the research machinery, not about profitability, live compatibility or legal compliance.

Code: `backend/src/unknown/research/`. Tests: `backend/tests/test_research_*.py`. Read-only API: `backend/src/unknown/api/routes/research.py`. Report artifact: `docs/research/reports/FS-20261022-R2-report.json` (regenerated after the adversarial review of 2026-10-22; see [Review fixes](#review-fixes-2026-10-22)).

Requirements: RES-01, RES-03, RES-04, RES-05 (in part RES-02: trend family only), AUT-03. Capabilities: CAP-RES-04, -05, -07, -08, -09, -10, -11, -12 (CAP-RES-01 is the data platform's as-of layer, which this package reads; CAP-RES-13/-14 are not built).

## Boundaries

- Research never posts money to the core ledger and holds no broker credentials (SEC-02). The only path into core code is `core_replay`, which drives a *temporary* core database with the `SimulatedBroker` fixture and deletes it.
- Money is exact: cash, unsettled amounts, prices, lots and fees are `Fixed` at the USD ledger scale (6 dp). Statistics (returns, volatility, Sharpe, ES, bootstrap, DSR, PBO) are floats; each function states its convention and none is used to post money. Where a statistical estimate crosses into exact arithmetic (a cost fraction, a target weight) it goes through `common.float_to_fixed` with one explicit rounding.
- Point in time: a run may read only a `DatasetView`; a decision sees only bars with `available_ns <= decision instant`. Corrections are later *versions* and stay invisible until their own availability time. `BarSeries.as_of` has the semantics of `data.pit.ResearchStore.as_of`, and a test proves the two agree at every availability boundary.
- Every derived number carries a `formula_id@version` from `research/common.py:FORMULAS` (also served at `GET /api/research/formulas`).

## Modules

| Module | Purpose | Capability |
|---|---|---|
| `spec.py` | Strict strategy specification schema; spec hash = SHA-256 of canonical JSON | RES-01, BR-DAT-08, CAP-RES-12 |
| `bars.py` | Bars with `[start, end)` and availability time, versions, `Cursor`, `DatasetView`, holdout guard, PIT-store bridge (`record_bars`, `bars_as_of_pit`, `load_series`) | DAT-02, DAT-04, BR-DAT-09 |
| `fixtures.py` | SYNTHETIC trend / mean-reverting / random-walk series, labelled `FIXTURE:synthetic/<kind>@v1;seed=n` | DAT-06, TRM-03 |
| `costs.py` | IBKR-like fee schedules, spread, slippage, square-root impact | CAP-RES-05, BR-DAT-20 |
| `engine.py` | Causal event-driven daily-bar backtester | CAP-RES-04, RES-03 |
| `sizing.py` | Algorithm A15, the one implementation the backtester and the strategy runner both call; the risk term with entry and exit fees by `risk.fees.envelope` | BR-DAT-04, BR-DAT-15 |
| `execution.py` | Submission window `[open + N, close − M]` (calendar-aware) and the limit set at transmission, shared by the backtester and the runner | BR-DAT-19 |
| `features.py` | Feature rows under the spec's feature contract: validity window, warm-up state, enumerated missingness; implementation versions checked against the contract | BR-DAT-18 |
| `core_replay.py` | Replays the engine's orders through the real core and compares exactly | RES-05 |
| `stats.py` | Moments, n_eff, PSR/DSR, PBO (CSCV), paired stationary bootstrap, flat-top HAC, Holm/Bonferroni | CAP-RES-08, CAP-RES-09 |
| `folds.py` | Walk-forward, purged k-fold, CPCV with purge and embargo | CAP-RES-07, BR-DAT-14 |
| `strategies.py` | `TREND_DAILY_V1`, `BUY_AND_HOLD`, `VOL_CAPPED_PASSIVE` and their spec builders | book ch. 15 |
| `manifest.py` | Reproducibility manifests and replay comparison | CAP-RES-11 |
| `registry.py` | Pre-registered experiments, complete trial registry, holdout lock, reports | CAP-RES-10, BR-DAT-07 |
| `promotion.py` | Artifact registry, status lifecycle, evidence gates, owner approvals, selection policy | CAP-RES-12, AUT-03, BR-DAT-01, BR-DAT-10, BR-OPS-10 |
| `study.py` | End-to-end fixture study and report (CLI) | I5 deliverable 6 |

## Strategy specification (RES-01)

`parse_spec` rejects unknown keys, missing keys, wrong types and out-of-range values. Numbers are decimal strings. RES-01 fields and where they live:

- **Hypothesis and market conditions:** `hypothesis.{statement, mechanism, market_conditions[], expected_effect}`.
- **Equations, variables, units, assumptions:** `equations[]` (each with a registered `formula_id`), `variables[]` (symbol, unit, kind) and `assumptions[]`.
- **Causal features and data requirements:**
  - `features[]` carries version, formula, `availability_basis` (BR-DAT-05 enum), freshness budget, warm-up (an upper bound over the parameter domain) and, in schema v2, `validity_window_s` (BR-DAT-18), and must have `causal: true`; ids are unique. For a v2 spec, `strategies.build` refuses an implementation whose feature ids and versions differ from the contract, or whose warm-up exceeds it; the runtime's feature rows (validity window bounded by the freshness budget, warm-up state, enumerated missingness) are described in `docs/strategy/README.md`;
  - `data_requirements[]` must have `point_in_time: true` and carry a rights reference.
- **Rules:**
  - `rules.entry`, `rules.exit` and `rules.holding`;
  - `rules.sizing`: target weight, per-name cap, aggregate cap, cash reserve, no-trade band and minimum notional; in schema v2 also `risk_budget` (`loss_fraction_of_equity`, `adverse_move_fraction`, BR-DAT-04);
  - `rules.execution`: decision clock, LIMIT/DAY only, limit offset, execution delay, participation cap and the unfilled policy. `same_bar_fill` must be `false`. Schema v1: `EXPIRE_END_OF_DAY`. Schema v2: `submission_window` (`open_offset_minutes` and `close_offset_minutes` at least 1, `auctions: false`, `limit_reference: TRANSMISSION_QUOTE_MID`, `collar_bps`) and `CANCEL_AT_WINDOW_END` (BR-DAT-19).
- **Schema versions.** Version 2 is current; the builders write it. A v1 document still parses, so registries written before v2 stay readable; the backtester runs it with the legacy rules (submission 60 s after the open, the limit fixed at the decision, no risk term), and its run config carries neither `submission_window` nor `risk_term`, so a v1 config hash is unchanged. Its orders and fills are those of the legacy rules; the text of the DECISION events' plan notes changed (one A15 implementation writes them for both versions), so an event-log hash recorded before this change does not replay exactly, as with any code change. The strategy runner refuses a v1 spec.
- **Costs, capacity, liquidity, operations:** `costs` (fee schedule id, spread, slippage, at least two impact Y values, an UNVERIFIED third-party sell-fee assumption), `capacity`, `liquidity` and `operational_constraints[]`.
- **Risk limits and failure conditions:** `risk_limits` must keep `long_only: true`, `allow_short: false` and `allow_margin: false` (RSK-01). `failure_conditions[]` is required.
- **Train / validation / untouched evaluation:** `validation_plan.{development, untouched_holdout, walk_forward, selection_metric}`. Periods must be chronological and must not overlap. `purge_sessions >= label_horizon + execution_delay` (BR-DAT-14).
- **Baselines:** `baselines[]`, which may not include the strategy itself.
- **Sensitivity and stress:** `sensitivity_plan.parameter_grid` (inside the domain) and `cost_multipliers` (must include 0.5, 1, 2 and 3), plus `stress_plan[]`.
- **Promotion evidence:** `promotion_evidence[]` must include REPLAY_EXACT, WALK_FORWARD_OOS, PAIRED_BOOTSTRAP, HOLDOUT, COST_SENSITIVITY and STRESS.
- **Parameters:** `parameters` must set every `parameter_domain` entry, inside its bounds; integer kinds must be integers.

## Registry (CAP-RES-10, CAP-RES-11)

Default path `~/.unknown/research/experiments.sqlite` (`UNKNOWN_HOME` honoured; injectable). It is a separate file from the data platform's `research.sqlite`.

- **Append-only for everyone.** Database triggers refuse UPDATE and DELETE on specs, experiments, amendments, manifests, holdout access, reports, artifacts, evidence, protocols, selection policies, evidence deletions (both tables) and transitions. A trial row may change exactly once, from RUNNING to a terminal status, and its identity columns never change. No delete or edit API exists.
- **Pre-registration first.**
  - The pre-registration is hashed together with its registration time. It holds the hypothesis, information clock, execution rule, cost model, benchmark, search space, budget, rejection criteria, holdout plan, walk-forward plan and selection metric.
  - Trials cannot exist without one and must start after it.
- **Every run is a trial.**
  - `run_trial` writes RUNNING before the run. An exception finishes it FAILED and is re-raised. An interrupt finishes it ABORTED. A run that returns without results is ARTIFACT_MISSING. `reap_running` finishes trials left RUNNING by a crashed process as ABORTED.
  - `trial_count` (M for the DSR) counts every status.
- **Search space and budget.**
  - A development trial must lie in the pre-registered search space and may differ from the pre-registered spec only in parameters. A visible amendment extends the space.
  - The budget counts DEVELOPMENT trials, and only the OWNER can raise it (AUT-04).
- **Follow-up segments.** WALK_FORWARD, SENSITIVITY, STRESS and REPLAY trials may only re-run a parameter set that already has a DEVELOPMENT trial (`UNCOUNTED_PARAMETERS` otherwise), so no search can run outside the segment that M and the budget count.
- **Holdout (BR-DAT-07).**
  - The holdout is consumed at most once per experiment, by the OWNER or the SYSTEM evaluator, never by an AGENT. Every attempt, granted or refused, is logged, including attempts refused because the spec or manifest is invalid.
  - A `DatasetView` that reaches into the holdout needs the `HoldoutGrant` the registry issued. A grant carries a random nonce known only to the issuing process, so a hand-made `HoldoutGrant` is refused.
  - The lock binds trials as well as views. Every manifest must name its datasets with `until`, the last session the run may read (`MANIFEST_INCOMPLETE` otherwise). A non-HOLDOUT trial whose datasets reach the holdout start is refused (`HOLDOUT_REQUIRES_GRANT`), except a declared BASELINE whose manifest names this experiment's GRANTED access (`extra.holdout_access_id`). `finish_trial` refuses returns dated after the manifest's `until`, or inside the holdout without that grant: the trial is recorded FAILED (`HOLDOUT_LEAK`), nothing from the holdout is stored, and the trial still counts.
  - The declared baselines are evaluated under the same grant, on identical dates, as one evaluation event.
- **Manifests.** A manifest holds the git commit with a dirty flag, a code hash over `research/`, `domain/` and `data/pit.py`, dataset fingerprints (and regeneration recipes for fixtures), the config hash, spec hash, parameters, seeds, simulator version and Python version.
  - `manifest_hash` is SHA-256 of the canonical manifest.
  - `compare_replay` reports `replay_exact` (event-log SHA-256 equality) and names the manifest fields that differ, including `python.version`, `python.implementation`, `git.commit` and `git.dirty` (the interpreter is the only dependency).
  - Fixture recipes record every generator argument (`sigma` and `mean_volume` included), so `from_recipe` regenerates the exact bars.

## Backtester (CAP-RES-04, CAP-RES-05)

Each session date `d` runs four phases in order:

1. **Settlement.** Amounts due on or before `d` settle.
2. **Submission and execution of the orders decided at the previous decision, sells first.**
   - Schema v2 (BR-DAT-19, `research.execution`): submission at open + N of `d`, from the calendar's session hours (early closes included; the FIXTURE convention 14:30–21:00 UTC for fixture data on `WEEKDAYS`; real data on a calendar without hours raises `SessionHoursUnknown`). The limit is set at submission: `min(model bound, open x (1 + collar))` for a buy, `max(model bound, open x (1 − collar))` for a sell, snapped to the tick. The session open stands in for the quote at open + N (daily bars carry no quote; labelled `transmission_reference`). The order records its `model_bound`, and `limit_price` becomes the transmitted limit. An empty window expires the order `NO_SUBMISSION_WINDOW`. Schema v1: submission 60 s after the bar's start, at the decision's limit.
   - A buy is reserved at submission. It needs `limit x qty + fee envelope` within settled cash minus unsettled payables and accrued fees. Sale proceeds are not spendable until T+1. Otherwise it is rejected `INSUFFICIENT_SETTLED_CASH`.
   - A sell needs owned quantity, otherwise it is rejected `NO_SHORTING`.
   - Execution uses only the instrument's bar for `d`, which must start after the decision instant (otherwise `CausalityError`). A session without a bar expires the order `NO_BAR`.
3. **Mark.** NAV is marked at the latest bar visible at the decision instant.
4. **Decision.** The decision instant is `d` + `decision_clock_utc` (21:30 UTC for the fixtures; bars become available at 21:15). The strategy sees only visible bars. A DECISION event is written for every session, including no-trade decisions. Target weights are mapped to price-bounded DAY limits for the next session by algorithm A15, `research.sizing.plan_orders`, the function the strategy runner also calls:
   - weights are capped per name and scaled to `min(aggregate_cap, 1 - cash_reserve)` (BR-DAT-15);
   - the no-trade band applies, except that a target of 0 always exits;
   - a new buy is skipped (`BELOW_MIN_ADV`) when the mean volume of the last `adv_window` (20) visible bars is below the spec's `liquidity.min_adv_shares`; exits are never blocked;
   - model limits are `close x (1 +/- offset)` snapped to the tick;
   - schema v2, the risk term (BR-DAT-04): a buy may bring the position only to the largest Q with `Q x (d + c) + Fee_entry(Q − current) + Fee_exit(Q) <= B`, where `d` = limit × `adverse_move_fraction`, `c` = limit × (half spread + slippage + third-party sell fee) × the cost multiplier, `B` = equity × `loss_fraction_of_equity`, and both fees come from `risk.fees.envelope` (the core's reservation function) under the schedule as a core fee model (`CostModel.core_fee_model`, whose envelope equals the engine's, tested). Zero means no order (`NO_TRADE`, "the risk budget cannot cover ..."); the term never sells. The decision's plan records `risk_term` and, per instrument, the evaluated `risk`;
   - Once the drawdown stop fires, every instrument is targeted to zero (even when the strategy returns no targets) and explicit BUY orders are refused and recorded in the decision (`RISK_STOPPED`).

Refused rather than ignored:

- A spec that declares a minimum or maximum holding period: the daily engine v1 does not implement holding rules, so `BacktestConfig.from_spec` raises `SpecError`.
- An instrument whose trading or settlement currency is not the fee schedule's (USD), or whose settlement cycle differs from the run's (T+1): a market without a recorded fee schedule is refused (CAP-RES-05).

A strategy order that asks to fill at the same bar's close raises `CausalityError`. Reading a bar that is not yet available raises `LookaheadError`.

**Fill rules.**

- No fill unless the bar trades *through* the limit: a buy needs `low < limit`, a sell needs `high > limit`. A touch is not a fill.
- Gap handling: a session that opens beyond the limit in the trader's favour executes from the open; otherwise the order executes from the limit.
- Price cost `c = k x (spread/2 + slippage + Y sigma sqrt(Q/V))` worsens the price. The result is snapped to the tick against the trader and never crosses the limit.
- Fills are capped at `floor(participation x bar volume)`; under a submission window, at that share of the volume the window covers (`window minutes / session minutes`, assuming uniform intraday volume: optimistic, because volume concentrates at the open and the close the window excludes; labelled). The remainder expires at the end of the day (`END_OF_DAY_REMAINDER`), or is cancelled at close − M (`WINDOW_END_REMAINDER`). Nothing is carried over.

**Fees.**

- **IBKR Pro Fixed** (`fee.ibkr.us.fixed@2026-10-22`): `min(max(0.005 x shares, 1.00), 1% of value)`.
- **IBKR Pro Tiered** (`fee.ibkr.us.tiered@2026-10-22`): marginal tiers within the calendar month, per-order minimum 0.35, maximum 1% of value.
- Both use the values recorded in CAP-RES-05 (page fetched 2026-10-22). The page's known answers are tests.
- Third-party sell fees are an **UNVERIFIED conservative assumption**: 1 bp of sell notional in the fixture specs, carried in every spec and manifest.

**Other details.**

- FIFO lots and the HALF_EVEN cost split mirror the core ledger.
- The report gives gross P&L (net + fees + price costs), net P&L and all-in P&L. All-in costs default to 0 and are labelled NOT CONFIGURED.
- Implementation shortfall uses the corrected eq. 7.1 from BR-DAT-03, with execution, fee and opportunity components. The opportunity mark is the close of the execution session.
- Performance: about 45 ms per backtest of 2 instruments over 375 sessions (Python 3.14, this Mac).

## Second validation path: the real core (RES-05)

`core_replay.replay_through_core(result, instruments)` sets up a temporary core and replays the engine's orders through it:

- **Setup.**
  - It bootstraps a SIMULATED account with a replay policy whose generic limits never bind before the engine's own rules.
  - The fee estimate uses the same per-share, minimum and cap values with a zero margin, so the core's reservation envelope equals the engine's.
- **Each session.**
  - Advance the clock, settle at the broker and reconcile (settlement is confirmed only from broker evidence, REC-04).
  - Submit each engine order as a STRATEGY intent through pretrade and the outbox.
  - Fill at exactly the engine's quantity, price and fee, then expire the remainders.
  - Compare settled CASH, unsettled (RECEIVABLE + PAYABLE + FEES_ACCRUED), positions, and `Desk.verify() == []`.
  - Orders the engine rejected must be rejected by the core: `INSUFFICIENT_SETTLED_CASH` maps to `CASH`, and `NO_SHORTING` maps to `OWNED_QUANTITY`.
- **At the end.** Settle everything and compare final cash, lot quantity and lot cost, and every execution's quantity, price, notional and final commission. Any open core exception counts as a mismatch.
- **Independent commission check.** The core books whatever commission the broker reports, so the final-commission comparison is bookkeeping only. Each engine commission is therefore also recomputed with the core's own fee model (`risk.fees.estimate`, a separate implementation of per-share, minimum and cap) and must match. Before the review, an engine schedule that forgot the 1.00 minimum replayed "clean"; a test now proves it is caught.

Result on the fixtures: **exact agreement, no rounding differences**. Fixture prices have at most 4 dp and quantities are integers, so every amount is exact at the 6 dp ledger scale. The engine also uses the core's HALF_EVEN notional and lot-cost rules. The tests cover:

- a trend run with buys, sells and third-party fees;
- a scripted run with a participation-capped partial fill, a T+1 cash rejection and a no-shorting rejection;
- a mutation test, which proves that a tampered fee or position is reported;
- a consistently wrong engine commission (no minimum), caught by the core fee model.

Scaled cost multipliers and non-WEEKDAYS calendars are reported as not comparable rather than compared.

## Statistics (CAP-RES-07, -08, -09)

- **Sharpe** is per observation (n - 1 sd). Annualization by sqrt(252) is always shown as a labelled assumption next to n_eff.
- **n_eff** follows book eq. 4.2 with a declared truncation K. The gate form follows BR-DAT-13: `min(n, n / label_horizon, n / max(1, 1 + 2 sum rho_k))`. The study's N_EFF gate uses the walk-forward OOS paired differences (candidate minus incumbent), the evidence the paired comparison rests on; the development n_eff is reported next to it.
- **Walk-forward, purged k-fold and CPCV.**
  - Observation `i` carries the label interval `[i, i + h]`.
  - Purge removes training observations within `h` of a test run on either side. Embargo removes training observations that start within `embargo` after a test run.
  - Property test: zero violations over 10,000 random configurations. CPCV(6, 2) gives 15 splits and 5 paths.
- **Paired stationary block bootstrap.**
  - The same index sequence is applied to candidate and incumbent in every replication. The mean block length is round(n^(1/3)). The interval is a percentile interval.
  - **Calibration finding (2026-10-22):** the nominal 95% percentile interval alone covered only **92.75%** on AR(1) phi = 0.3, n = 300 (2,000 replications, B = 999). That is below CAP-RES-09's 93-97% band. Longer blocks made it worse, because circular autocovariances sum to zero.
  - The gate therefore uses the **more conservative** of the bootstrap interval and a flat-top-kernel HAC normal interval (`lrv.flat_top@1`). That combination covered **94.15%** (2,000 replications), passing.
  - Other measurements (1,000 replications, B = 499): iid 95.6% for the gate interval (94.2% bootstrap alone). Strong dependence, phi = 0.6: **92.7%** for the gate interval (89.8% bootstrap alone). This is a known limitation.
- **DSR, PSR, SR0 and N-hat** follow Bailey and Lopez de Prado as recorded in CAP-RES-08. M is the registry's trial count for the family (every status), passed as `trial_count`; V[SR_n] and rho come from the trials that produced returns. A failed trial therefore raises the deflation instead of silently lowering M. Both M and N-hat are reported, and results are flagged ill-conditioned when T < M(M-1)/2.
  - Known answers: the PSR closed form; SR0 against an independent bisection inverse-normal; SR0 within 2% of the exact expected maximum of 100 normals.
  - Under the null, DSR > 0.95 is rejected at most 5% + 3 MC standard errors (2,000 simulations; passes).
- **PBO via CSCV.** Rank 1 is the worst; ties get average ranks.
  - S = 16 enumerates C(16, 8) = 12,870 splits. The paper prints "12, 780".
  - Known answers: an IS-best that is always OOS-worst gives PBO = 1 with logits ln(1/2); a dominant strategy gives PBO = 0; pure noise gives about 0.5; a planted edge gives less than 0.1.
- The Monte Carlo calibration tests are opt-in: `UNKNOWN_SLOW_TESTS=1`.

## TREND_DAILY_V1 and baselines (book ch. 15)

- **TREND_DAILY_V1** computes `z = (EMA_f(log P) - EMA_s(log P)) / (sigma sqrt(h) + eps)`, with EMA by span initialized at the first close and sigma from the EWMA of squared log returns with half-life H. The weight is `w = min(w_max, k max(0, z))`, and it is zero during warm-up (fewer than `slow_span` closes). There is a known-answer test of eq. 15.1 by hand.
- **BUY_AND_HOLD** buys `weight / N` once and holds. It is the incumbent.
- **VOL_CAPPED_PASSIVE** uses `min(w_cap, sigma_target / sigma_hat) / N` and rebalances through the band.

## Promotion (CAP-RES-12, AUT-03)

Statuses: proposed, reproducible, validated, shadow, paper, eligible, active, quarantined, retired (ADR-0017 #2). The authoritative status is the last record of a hash-chained, append-only transition log. "Signed" in CAP-RES-12 is implemented as tamper evidence, not as a cryptographic signature; there is no signing-key infrastructure yet.

| Transition | Who | Requires |
|---|---|---|
| proposed → reproducible | SYSTEM evaluator or OWNER, never the author | REPLAY_EXACT passed |
| reproducible → validated | same | every gate kind in the spec passed under one owner-approved protocol |
| validated → shadow | OWNER approval, or SYSTEM inside an owner-approved shadow envelope (`auto_shadow`) | the gate evidence the validation rested on, cited again; under an envelope, evaluated under the envelope's protocol, with the artifact inside its class, parameter domain, instrument set and risk budget, and within its promotions-per-period limit |
| shadow → paper | OWNER approval | CORE_REPLAY_MATCH passed |
| paper → eligible | OWNER approval | PAPER_RECONCILIATION passed (externally blocked until BRK-02) |
| eligible → active | OWNER approval, or SYSTEM under an owner-approved selection policy | slot; the incumbent moves back to eligible, and both hashes are recorded |
| any → quarantined | OWNER or SYSTEM | MONITORING_INCIDENT |
| quarantined → reproducible | OWNER approval | REPLAY_EXACT |
| any → retired | OWNER approval | reason |

Rules the promotion service enforces:

- An owner approval needs re-authentication within 15 minutes. It is recorded with the principal and the time.
- AGENT and STRATEGY principals can never approve, transition, approve a protocol or policy, or run selection. Agents may propose artifacts and file evidence, but agent-filed evidence never satisfies a gate.
- Gate verdicts are recomputed from the payload and the owner's protocol; a submitter's own `passes` field is ignored.
- Evidence fails if its protocol was approved after the evaluation started. The start is the earlier of the claimed start and the earliest referenced trial, and that effective start is what is stored; a start in the future fails.
- Evidence references must exist in the registry, and the payload is bound to registry facts:
  - HOLDOUT evidence must name a GRANTED holdout access consumed for this very artifact and cite the HOLDOUT trial that grant created;
  - DSR evidence must cite the DEVELOPMENT trials of one experiment and use M equal to its registry count;
  - STRESS evidence must cover every stress case the spec declares.
- Expired artifacts cannot become eligible or active, on every activation path, automatic selection included.

**Evidence retention (BR-DAT-16).** Every manifest dataset entry states `retention_until`: the last UTC day a result resting on the data may be used, or `"unbounded"` when the dataset has no retention. An entry written before this rule has none and is reported as `unrecorded` (`evidence_status.retention_unrecorded`); `build_manifest` refuses any other value.

- **Where it comes from.** `research.bars.pit_manifest_entry(store, dataset, series, until, cutoff_ns)` builds the entry of a point-in-time dataset a run reads. It records the `scope` the run read (instruments, availability cutoff) and the retention of exactly those observations (`ResearchStore.retention_facts`). The deadline is the earlier of:
  - the earliest `retention_until_ns` of the raw payloads they were extracted from (`source_ref` `raw:<sha256>`);
  - the earliest receipt time (of those observations or of the payloads they were extracted from) plus the provider registry's `retention_days` for the dataset.

  `retention_until` is the UTC day before that deadline (`manifest.retention_date`), so a day-granular expiry never reaches the deadline instant. An unregistered dataset is refused: it has no recorded rights, so no retention can be stated for it. The fixture study's synthetic datasets are `"unbounded"`.
- **Expiry.** An artifact's evidence datasets are those of its evaluation manifest and of the trials its *passed* evidence cites (failed or agent-filed evidence supports nothing, so it bounds nothing). Registration refuses an `expires_on` after the evaluation manifest's earliest `retention_until`. The effective expiry is `min(expires_on, retention_until of every evidence dataset, the day before any recorded deletion of that data)`, where a deletion counts from when it happened or, if earlier, from when it was recorded (a deletion dated in the future still ends the artifact when it is recorded). Every expiry check uses it: activation, automatic selection, automatic shadow promotion and the strategy runner. The check guards promotions only: the owner may still step an expired active artifact down to eligible, out of its slot.
- **Deletion in the point-in-time store** (`data.pit`):
  - `enforce_retention(now_ns, retention_days=...)` deletes every raw payload whose deadline has passed and, for a dataset whose registry retention period has elapsed since receipt, the raw payloads and observations received that long ago. With every deleted payload go the observations extracted from it. Each run also removes the files of any earlier deletion that a crash left on disk between its commit and its file removal.
  - `withdraw_rights(dataset, reason, at_ns, entity_ids=None)` deletes the observations of the dataset (or of some entities), every version, and the raw payloads they came from.
  - Both remove the raw files, keep the raw rows (hash, request, `deleted_at_ns`), and append one `deletion_event` per dataset (append-only). The event records, per entity, how many observations went and their availability span. A deleted payload is not stored again.
- **Recording a deletion in the research registry.** There are two ways, both append-only:
  - `record_evidence_deletion(dataset, principal, reason)`: the OWNER or the SYSTEM records a whole-dataset deletion, once per dataset;
  - `sweep_store_deletions(store, principal)`: the pull-based link to the store. It reads `deletion_events` for every dataset any registry manifest names (not only those artifacts rest on, so that registration and evidence refuse the deleted data before any artifact rests on it) and records each new event once. It is idempotent: run it after every retention run or rights withdrawal, and on a schedule. It also files the evidence for any artifact touched by a deletion recorded before deletions were filed as evidence, and steps down any touched artifact that is still active, even one whose deletion evidence was filed by hand.

  A store event touches a manifest entry when it deleted observations of an entity the entry's `scope` names, available by the entry's cutoff. An entry without a scope is touched by any deletion of its dataset.
- **EVIDENCE_EXPIRED.** An artifact a recorded deletion touches is `EVIDENCE_EXPIRED` (`evidence_status`, which lists each deletion and the evidence that cites it). In the same transaction that records the deletion:
  - every such artifact that is not retired receives passed `EVIDENCE_DELETION` evidence naming the deletion, filed by the recording principal. That evidence must match a recorded deletion that touches the artifact, so an agent's or a fabricated filing never passes;
  - an active one steps down to eligible in its slot, citing that evidence (the slot is free for the owner or automatic selection).

  Its effective expiry is at most the day before the deletion (or before its recording, if earlier), so it never runs again: the strategy runner, which reloads the artifact every cycle, refuses it as `ARTIFACT_EXPIRED` in both modes. No expiry-checked path accepts it either (activation, automatic selection, automatic shadow promotion). Every other upward transition, including `quarantined -> reproducible`, is refused as `EVIDENCE_EXPIRED`; quarantine and retirement still apply. Registration on a manifest that rests on deleted data is refused, and evidence citing such a trial never passes (except a monitoring incident or the deletion itself).

  EVIDENCE_EXPIRED is not a lifecycle status. Adding one would change the status vocabulary that the terminal (`terminal/src/workspaces/research/model.ts`, checked by `test_terminal_vocabulary_sync`) and the runner's status table (`test_strategy_runner`) pin, and both are outside this change. The refusal to run comes from the expiry instead.
- There is no renewal: the expiry is part of the immutable registration. New evidence on retained data is a new artifact (a new spec version or parameter set: the artifact hash does not cover the evaluation manifest).
- **Not covered.** Between a store deletion and the next sweep, the registry does not know about the deletion. A retention deletion is already covered by the date bound recorded in the manifest, as long as the provider registry's `retention_days` is not shortened after the manifest was built. A rights withdrawal is not: in that window an artifact on withdrawn data still runs. In the other direction, a deletion event names entities and an availability span, not versions, so deleting only a superseded version that a run never read (its current version came from a payload retained longer) still expires the artifacts on that entity. Derived metrics (`derived_metric`) computed from deleted observations are not deleted. Whether a licence allows keeping derived features after the raw data is deleted (`derived_storage`) is not modelled: deletion removes the observations too.

Tests: `test_research_evidence_retention` (13), `test_research_evidence_expiry_wiring` (10, end to end with the point-in-time store, the deletion sweep and a real strategy-runner cycle), and `test_pit_store` `RetentionAndDeletionTests` (8).

The promotion protocol (BR-DAT-10) has exactly these fields: confidence, replications, mean block, statistic, utility coefficients, hurdle per period (net of trading costs), min n_eff, min OOS observations, min OOS Sharpe, max PBO, min DSR, required cost multiplier, max stress drawdown, holdout minimum difference, min paper days and seed.

The selection policy (AUT-03) is approved by the owner and is immutable. It holds the slot, strategy, parameter domain, instrument set, risk budget, protocol hash, activation limit per period and ranking. `auto_select`:

- runs as SYSTEM;
- considers only eligible, unexpired artifacts inside the domain;
- ranks them by the conservative paired lower bound (`conservative_lower`, the bound the gate used);
- refuses an explicit choice outside the domain;
- never expands the policy.

The shadow envelope (ADR-0017 #2, book ch. 20 "automatic promotion only for artifacts that fit an owner-approved class, parameter domain, instrument set, risk budget and evaluation protocol") is approved by the owner and is immutable. It holds the class (`strategy_id`), parameter domain, instrument set, risk budget (`max_gross_exposure`, `max_single_name_weight`, `max_drawdown_stop`: every one of the spec's `risk_limits` must be at or below it), protocol hash and a promotions-per-period limit. `auto_shadow`:

- runs as SYSTEM, and only on a `validated`, unexpired artifact inside the envelope;
- requires the cited evidence to pass every gate kind the spec lists under the envelope's own protocol;
- records the transition with that evidence and an approval of kind `SHADOW_ENVELOPE` (envelope id, hash, approver);
- stops at `shadow`: `paper` and later remain owner decisions.

Tests: `test_research_shadow` (9).

## Fixture study and report (test-only since ADR-0020)

The study runs on generated series, so it is no longer product code: it lives in `backend/tests/support/research_study.py`
and runs from the test tree. Its report below is evidence about the research machinery, not about any market.

```bash
cd backend/tests && PYTHONPATH=../src python3 -m support.research_study --out ../../docs/research/reports --study-id FS-20261022-R2
```

The command writes the report to the registry and to `docs/research/reports/FS-20261022-R2-report.json`. The registry is `~/.unknown/research/experiments.sqlite` unless `--registry` says otherwise. Each kind uses 750 sessions, 2 instruments, development 70% / holdout 30%, a 6-point grid (fast 5/10/20 × slow 40/80), the spec's purge of 2 sessions, and protocol `FIXTURE_PROTOCOL`. The protocol was approved by a *fixture* owner principal; it is an illustration, not an owner decision. The run takes about 25 s.

The registry also still holds study `FS-20261022`, produced before the review fixes (walk-forward purge of 1 session, HOLDOUT gate not bound to a grant, n_eff on development returns). The store is append-only, so those rows stay; they are FIXTURE rows and superseded by `FS-20261022-R2`.

The committed `FS-20261022-R2` report predates spec schema v2: its specs are v1 (they still parse) and were run with the legacy execution and sizing rules. The study now builds v2 specs, so a new run has new spec hashes, event logs and configs (the submission window and the risk term are recorded in every config). The fills should not change: a one-off check (2026-10-22, not a committed test) ran TREND_DAILY_V1 over the study's 6-point grid and both baselines on 750-session trend, mean-reverting and random-walk fixtures at 100,000 of initial cash, under v1 and v2. The two arms differed in submission time (14 minutes later), transmission records and risk evaluations, and every fill (quantity, price, fees) was identical: the 50 bps collar never bound below the open, the window's volume share never bound the participation cap, and the risk term allows 58% per name against the 45% cap. The report has not been regenerated.

| SYNTHETIC kind | Selected (fast/slow) | Dev Sharpe/period | OOS paired diff (conservative lower) | DSR | PBO | Core replay | Status | Failing gates |
|---|---|---|---|---|---|---|---|---|
| trend | 5/40 | 0.130 | +0.00047/day (−0.00023) | 0.999 | 0.41 | 13 fills, exact | reproducible | PAIRED_BOOTSTRAP |
| mean_reverting | 20/80 | −0.150 | −0.00031/day (−0.00103) | 0.0001 | 0.31 | 13 fills, exact | reproducible | WALK_FORWARD_OOS, PAIRED_BOOTSTRAP, HOLDOUT, COST_SENSITIVITY, DSR |
| random_walk | 20/40 | −0.039 | −0.00022/day (−0.00107) | 0.18 | 0.75 | 18 fills, exact | reproducible | WALK_FORWARD_OOS, PAIRED_BOOTSTRAP, COST_SENSITIVITY, DSR, PBO |

DSR uses M = 6 from the registry in every row. The trend candidate's N_EFF gate on the paired OOS differences is 124 (protocol minimum 100).

The machinery behaves as expected:

- On the trend fixture the candidate beats buy-and-hold in point estimate, but the paired interval does not clear the hurdle.
- On the mean-reverting fixture the trend rule loses.
- On the random walk, PBO is 0.75 and the DSR is low.
- No candidate reaches `validated`, and none goes further without the owner.

These are synthetic series: **not market evidence.**

## Read-only API (deliverable 8)

Route module `api/routes/research.py` (module name `research`). Until the orchestrator registers it in `api/routes/__init__.py` it is not served. All endpoints are GET:

| Endpoint | Returns |
|---|---|
| `/api/research` | index and counts |
| `/api/research/formulas` | the formula registry |
| `/api/research/strategies` | registered specs |
| `/api/research/strategies/<spec_hash>` | one spec, hash re-verified |
| `/api/research/experiments` | experiments with trial counts per segment, current budget and holdout status |
| `/api/research/experiments/<id>` | pre-registration (hash verified), amendments, search space, counts and holdout log |
| `/api/research/experiments/<id>/trials[?segment=]` | every trial, without returns |
| `/api/research/trials/<trial_id>` | one trial with metrics, returns and its manifest |
| `/api/research/reports`, `/api/research/reports/<id>` | report index and bodies, FIXTURE-labelled |
| `/api/research/artifacts`, `/api/research/artifacts/<hash>` | status from the transition log, history, evidence and chain check |

Every response is `{available, data, provenance}`. Provenance carries the source, path, schema version, read time and, where relevant, formulas. Experiments, trial lists, single trials (with their return series) and artifacts derived from a FIXTURE experiment carry `fixture: true` and `label: "FIXTURE"`; an artifact whose evaluation manifest has no trial is labelled `UNKNOWN_ORIGIN`. An unknown `segment` query value is a 400 (`ValueError`). A missing registry returns `available: false` with the reason. The registry is opened with SQLite `mode=ro`, so a route cannot write, change a status or approve.

## Tests

Run with `cd backend && PYTHONPATH=src python3 -m unittest tests.test_research_<name>`.

| Module | Tests |
|---|---|
| `test_research_spec` | 15 |
| `test_research_costs` | 13 |
| `test_research_engine` | 32 |
| `test_research_stats` | 34 (2 opt-in Monte Carlo) |
| `test_research_strategies` | 9 |
| `test_research_registry` | 28 |
| `test_research_promotion` | 25 |
| `test_research_shadow` | 9 (shadow status and envelope, ADR-0017 #2) |
| `test_research_evidence_retention` | 13 (BR-DAT-16) |
| `test_research_evidence_expiry_wiring` | 10 (BR-DAT-16, end to end) |
| `test_research_spec_v2` | 3 (schema v2, v1 readability) |
| `test_research_risk_sizing` | 10 (BR-DAT-04) |
| `test_research_window` | 8 (BR-DAT-19) |
| `test_research_core_replay` | 5 |
| `test_research_study` | 14 (study plus API routes) |

All pass (2 opt-in skipped) on Python 3.14.7 and 3.12.13. The full research set takes about 25-60 s depending on machine load; the core replay and the study account for most of it. The runtime side of BR-DAT-02/-04/-18/-19 is tested in `test_strategy_*` (`docs/strategy/README.md`).

## Known limitations and open questions

- **Bootstrap coverage under strong dependence.** The gate interval covered 92.7% at phi = 0.6 and n = 300. The owner should decide whether the protocol needs a wider confidence level or a minimum n_eff for high-autocorrelation candidates. Automatic optimal block length (Politis-White) is not implemented; the block rule is round(n^(1/3)).
- **Calendar.** Runs default to the WEEKDAYS calendar for sessions and settlement. Real US data needs the XNYS calendar (holidays) passed to `BacktestConfig.from_spec(calendar=...)`; the core replay compares WEEKDAYS runs only.
- **Daily bars only.** The submission window (open + N to close − M, BR-DAT-19) is modelled only as far as daily bars allow: the transmission reference is the session open, and the window's share of the bar volume assumes uniform intraday volume (optimistic). The fill test (low below a buy limit, high above a sell limit) and the open-gap fill price still use the whole day's bar, including the open, the auctions and the minutes outside the window, so an order sent at open + N can fill at the open price or on a low it was not yet working for (optimistic; not modelled). There is no auction model, no queue model and no latency distribution.
- **Not handled:**
  - corporate actions (BR-DAT-11);
  - point-in-time universe membership and survivorship (CAP-RES-02). The fixtures use explicit lists, labelled as such;
  - multi-currency, FX costs and borrow;
  - capacity curves (CAP-RES-05 lists one; only a capacity field and the participation cap exist);
  - regime-labelled stresses (`REGIME` stress kind refused);
  - the rollback activation of a predecessor (CAP-RES-14);
  - drift monitoring (CAP-RES-14).
- **Costs.**
  - IBKR commission values are those recorded in the capability register on 2026-10-22. Third-party fees are an unverified assumption.
  - The Tiered schedule is applied marginally within the calendar month, as CAP-RES-05 describes. Whether IBKR prices a single order that crosses a tier boundary this way is unverified.
- **Paper stage.** PAPER_RECONCILIATION evidence cannot be produced until the IBKR paper account exists (BRK-02). No artifact can reach `eligible` today.
- **Unenforced or untested:**
  - BR-OPS-10 (evaluator differs from author) is enforced by principal id; separate credentials for the evaluator are not built.
  - "Signed" transitions are hash-chained only.
  - The DSR's ill-conditioning flag is reported, but nothing acts on it.
- **SQLite durability.** The research registry uses SQLite WAL with default synchronous settings. Durability over speed was chosen deliberately; promotion tests take about 6 s because of per-insert syncs.

## Review fixes (2026-10-22)

An independent adversarial review confirmed and fixed these defects. Each has a regression test that fails on the pre-review code (checked by running the new tests against a copy of the original sources).

| # | Defect | Fix | Test |
|---|---|---|---|
| 1 | A hand-made `HoldoutGrant` opened the holdout | grants carry a nonce from `bars.issue_grant`; `DatasetView` accepts only issued grants | `HoldoutLockReviewTest.test_a_hand_made_grant_does_not_open_the_holdout` |
| 2 | A DEVELOPMENT trial could read and record holdout returns (a view without `holdout=` was unchecked) | manifests must state `until`; non-HOLDOUT trials reaching the holdout are refused; `finish_trial` refuses leaked returns and records the trial FAILED | `test_a_non_holdout_trial_whose_manifest_reaches_the_holdout_is_refused`, `test_holdout_returns_from_a_development_trial_are_refused_and_the_trial_still_counts`, `test_a_manifest_must_state_what_the_run_may_read`, `test_declared_baselines_may_use_the_granted_holdout_and_nothing_else_may` |
| 3 | Parameter search in SENSITIVITY/STRESS/REPLAY/WALK_FORWARD left M = 0 and the budget untouched | follow-up segments may only re-run parameter sets with a DEVELOPMENT trial | `test_follow_up_segments_cannot_evaluate_uncounted_parameters` |
| 4 | A holdout attempt refused for an invalid spec or manifest was not logged | the refusal is logged with its code | `test_every_holdout_attempt_is_logged_even_when_the_spec_is_invalid` |
| 5 | HOLDOUT evidence passed with a fabricated access id | the grant must exist, be for this artifact, and its trial must be cited | `EvidenceBindingReviewTest.test_holdout_evidence_needs_a_real_grant_for_this_artifact` |
| 6 | The DSR used M = trials with returns, so failures lowered the deflation; DSR evidence M was never checked | `deflated_sharpe(trial_count=...)`; the study passes the registry count; promotion checks M against the registry | `test_registry_trial_count_is_the_m_in_n_hat`, `test_dsr_must_use_the_registry_trial_count` |
| 7 | Policy precedence trusted a caller-supplied evaluation start | effective start = min(claim, earliest cited trial); future starts fail | `test_a_late_claimed_start_cannot_dodge_policy_precedence` |
| 8 | STRESS evidence passed with a subset of the declared cases | every declared stress id must be present | `test_stress_evidence_must_cover_every_declared_case` |
| 9 | Automatic selection could activate an expired artifact | expiry is checked in `_activate` and in the selection pool | `test_automatic_selection_refuses_an_artifact_that_expired_after_becoming_eligible` |
| 10 | `active_in_slot` reported an artifact moved to another slot, so activating a new artifact demoted it | the artifact's latest transition must be `active` in that slot | `test_an_artifact_moved_to_another_slot_is_not_active_in_its_old_slot` |
| 11 | Automatic selection ranked by the looser percentile bound, not the gate's conservative bound | ranks by `conservative_lower` | `test_automatic_selection_ranks_by_the_conservative_bound_the_gate_used` |
| 12 | The study's walk-forward purged 1 session, below the spec's `purge_sessions` (2) and BR-DAT-14 | uses the pre-registered `purge_sessions` | `test_walk_forward_uses_the_preregistered_purge` |
| 13 | The study ignored the protocol's `statistic` and the spec's `selection_metric` (always mean and Sharpe) | `protocol_statistic` and `selection_score` apply them | `test_protocol_statistic_and_selection_metric_are_honoured` |
| 14 | N_EFF gate measured the development returns, not the paired comparison; the BR-DAT-13 non-overlap term was missing | gate on paired OOS differences; `n_eff(label_horizon=)` | `test_n_eff_gate_is_computed_on_the_paired_oos_differences`, `test_gate_form_includes_the_non_overlap_term` |
| 15 | The drawdown stop let explicit BUY orders through and did not flatten when targets were `None` | explicit buys refused; all instruments targeted to zero | `EngineReviewTest.test_drawdown_stop_refuses_explicit_buys_and_flattens_without_targets` |
| 16 | Declared holding rules and `liquidity.min_adv_shares` were silently ignored | holding rules refused; ADV filter on new buys | `test_declared_holding_rules_are_refused_not_ignored`, `test_min_adv_blocks_new_buys_but_never_exits` |
| 17 | Non-USD or non-T+1 instruments were priced with the US schedule and settled T+1 | refused | `test_instruments_without_a_recorded_fee_schedule_or_settlement_cycle_are_refused` |
| 18 | `fill_ratio` summed mantissas of different scales (0.99 instead of 0.5) | exact `Fixed` sums | `test_fill_ratio_is_exact_across_quantity_scales` |
| 19 | The core replay's commission comparison was circular (the core books the engine's fee) | engine commissions recomputed with `risk.fees.estimate` | `test_a_consistently_wrong_engine_commission_is_caught_by_the_core_fee_model` |
| 20 | PIT bridge disagreed with `BarSeries` at 10+ versions (string order "v10" < "v9") | zero-padded publication ids | `test_ten_or_more_versions_agree_with_the_store` |
| 21 | Replay diff did not name a changed interpreter or commit (CAP-RES-11) | environment fields compared | `test_replay_diff_names_a_changed_interpreter_or_commit` |
| 22 | Fixture recipes could not regenerate non-default `sigma`/`mean_volume` | recorded and replayed | `test_a_fixture_recipe_regenerates_non_default_generators` |
| 23 | API trial, trial-list and artifact responses from FIXTURE experiments were not labelled; bad `segment` returned `[]` | FIXTURE label in provenance; 400 on unknown segment | `test_fixture_trials_and_artifacts_are_labelled_and_segments_validated` |
