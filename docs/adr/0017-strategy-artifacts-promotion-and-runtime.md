# ADR-0017: Strategy artifacts, promotion gate and strategy runtime

Status: proposed (2026-10-22). Implementation follows the research engine (I5). See the [status note](#status-note-2026-10-22) at the end: the runtime is implemented, other decisions are not complete.

## Context

The owner brief asks for a quant research registry and autonomous trading inside owner-set bounds. Book Chapter 20 separates research autonomy from production authority. It also defines artifact statuses and a predeclared evaluation gate. Chapter 15 specifies the first candidate, `TREND_DAILY_V1`, and algorithm A15.

The core already enforces three things:
- a STRATEGY principal may only submit intents and cancel its own orders;
- its `strategy_id` must equal its authenticated principal id;
- the session mandate lists the strategy ids it permits (ADR-0007; review findings 23 and 56).

What is missing:
- an identity for a strategy that cannot drift;
- a registry with a transition policy;
- the process that turns data into intents;
- the record of decisions that produced no trade.

## Decision

1. **Artifact identity.** A strategy artifact is the SHA-256 of a canonical JSON manifest containing:
   - the strategy class (e.g. `TREND_DAILY`);
   - the hash of every source file of the strategy package;
   - the parameter set;
   - the feature definitions;
   - the dataset ids and versions it reads (ADR-0015);
   - the dependency lock hash;
   - the manifest schema version.

   Its id is `<class>@<first 16 hex of the hash>`. The id is what mandates list and what the strategy principal is called. Changing code, parameters or data dependencies produces a new artifact; nothing is updated in place.

2. **Registry and statuses.** The research registry records each artifact with the fields of book Chapter 20: author, training interval, evaluation manifest, allowed markets, horizon, capacity estimate, expiry and rollback predecessor. Statuses are `proposed → reproducible → validated → shadow → paper → eligible → active`, plus `quarantined` and `retired`. Every transition is a durable record carrying an evidence reference and the authority that made it:
   - Up to `validated`: the research engine, by re-running the trial manifest.
   - `shadow`: the owner, or an automatic rule inside an owner-approved envelope (class, parameter domain, instrument set, risk budget, evaluation protocol).
   - `paper`, `eligible`, `active`: the owner only. Becoming active also means adding the id to a mandate, which is an owner action in the core.
   - `quarantined`: any principal, including the system and agents. Quarantine only tightens.

   No agent can approve its own evaluation. The gate parameters (utility, hurdle, block method, confidence level, trial correction) are fixed in the evaluation manifest *before* the evaluation runs, and their hash is recorded at `proposed`.

3. **Gate.** The candidate must improve the predeclared utility over the incumbent, or over the baseline when there is no incumbent (Chapter 15: buy-and-hold and a volatility-capped passive baseline). The test is paired: the lower bound of a block-bootstrap interval must exceed the hurdle, with enough effective sample size. The candidate must also stay robust to the fee, latency, missing-data and parameter perturbations listed in the manifest. A leakage or rights violation rejects it regardless of results. The trial register counts every candidate evaluated under a manifest, so repeated generation stays visible.

4. **Runtime.** A strategy runner is a separate research-side process, like the agent worker. It holds no broker credentials and authenticates to the core socket with a *strategy* token that may assert only `STRATEGY`. The principal id it asserts is the artifact id. Following algorithm A15, on each eligible completed observation it:
   - validates freshness and corporate-action versions;
   - updates causal features and computes the score;
   - maps the score to target exposure;
   - subtracts current and pending exposure, which it reads from the core;
   - applies the no-trade band and minimum size;
   - submits typed intents with an expiry.

   The core's pretrade, reservation, stop and session rules apply unchanged. The runner never talks to the broker.

5. **Decision records.** A new core command `record_strategy_decision` (STRATEGY and OWNER) records the inputs digest, score, target, current exposure and outcome, including "no trade" and "blocked by gate X". It has no economic effect. Restraint and missed opportunities stay auditable (A15), and shadow mode reuses the same record with `mode: shadow` and no intents.

6. **Rollback and quarantine.** Removing an artifact from a mandate, or quarantining it, stops its future entries. Residual positions are handled by the session's wind-down policy. Rollback never reverses trades. Reactivating a predecessor requires a reconciled state and an owner action.

## Alternatives rejected

- **Strategy code inside the core process:** the core stays small, deterministic and standard library only; strategy failures must not stop the ledger.
- **Agents promoting artifacts:** only the owner grants production authority (Chapter 20).
- **Mutable strategy ids with version fields:** a mandate that names a moving target is not an approval of what runs.

## Reopen if

- A strategy needs sub-second decisions. The socket round trip and per-command verification then become the bottleneck, which is the HFT track and out of scope.
- The owner wants automatic promotion to `paper` inside an envelope.

## Status note (2026-10-22)

The status stays `proposed`. Not every decision above is implemented and tested. Evidence words follow ADR-0001.

| Decision | Implemented and tested offline | Missing |
|---|---|---|
| 1. Artifact identity | The id `<strategy_id>@<16 hex>` is derived from the research registry's artifact hash (`research.promotion.register_artifact`); there is no second hashing scheme (`backend/src/unknown/strategy/artifacts.py`). The runner refuses a bundle that no longer hashes to its id, and code that differs from the registered `code_hash` (`CODE_DRIFT`). The class is the spec's `strategy_id` (e.g. `TREND_DAILY_V1`). | The registry bundle covers dataset ids only through the spec hash; it records no dataset versions. Its "dependency lock" is a constant string, not a lock-file hash. Its code hash is supplied by the caller rather than computed by the registry, and it does not cover the runtime package (each decision's inputs digest records the runtime's code hash instead). The registry does not store the ADR id; `python -m unknown.strategy id` derives it. |
| 2. Registry and statuses | Statuses, evidence, owner approvals and quarantine are implemented in `research.promotion` (its own tests). The runner applies the ADR order to modes: shadow needs `shadow` or later, live needs `paper` or later, and `quarantined`/`retired` never run. | `research.promotion` has no `shadow` status and no transition into it (by the owner, or by an automatic rule inside an envelope). Until it does, shadow mode needs `paper`. |
| 3. Gate | Paired bootstrap, cost, stress, DSR, PBO and n_eff gates are recomputed by `research.promotion` (not re-verified by this note). | Not reviewed against every clause of this decision as part of the runtime work. |
| 4. Runtime | `python -m unknown.strategy` (`docs/strategy/README.md`). The runner is a separate process with a strategy token and principal = artifact id, and holds no broker credentials. It decides only on completed point-in-time bars at the backtest's decision instant, calendar-aware (WEEKDAYS, XNYS). Targets come from the research code and equal the backtest's at all 180 decisions of a fixture series; the A15 orders from a flat book equal the backtest's at its first trading decision, and the band, minimum size, cash and aggregate-cap steps have their own tests. Exposure (owned, unreserved, pending) is read read-only from the core. Intents carry an expiry. Ids are deterministic and a write-ahead journal (one per account and artifact) makes restarts, crashes, core outages, a lost journal and a switch to shadow mode safe; an intent past its deadline is never sent. Tested end to end against a real core service (`backend/tests/test_strategy_runner.py`). | Corporate-action version checks: the bar schema has no corporate-action or adjustment version. IBKR historical bars (`mkt_bar`) are not readable through `research.bars`. The engine's strategy drawdown stop is not applied at runtime; the core's session stops apply instead. Exposure is the session's, not the artifact's (the core's ledger owner is the session). The runner's command ids are predictable and another principal can claim one first; the runner never adopts it, but preventing the claim needs a core change. Never run against IBKR paper. |
| 5. Decision records | Every decision is recorded through `record_strategy_decision`: `INTENT_SUBMITTED`, `BLOCKED` (the core's rejection code and failed checks, or a runner gate), `NO_TRADE`, `SKIPPED_STALE_DATA`, in live and shadow mode. | The core vocabulary has no outcome for a shadow decision that would have traded; it is recorded as `NO_TRADE` with a `SHADOW_WOULD_SUBMIT` reason. The mandate cannot list an artifact for shadow only: the listing a strategy needs to record shadow decisions also permits intents. |
| 6. Rollback and quarantine | A quarantined or retired artifact is refused at the next cycle. An artifact the mandate does not list is refused by the core, for both intents and records (tested). | Reactivating a predecessor (reconciled state plus owner action) has no procedure or test. |

