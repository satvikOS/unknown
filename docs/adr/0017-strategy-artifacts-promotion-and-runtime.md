# ADR-0017: Strategy artifacts, promotion gate and strategy runtime

Status: proposed (2026-10-22). Implementation follows the research engine (I5).

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
