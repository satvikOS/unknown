# Strategy promotion and bounded autonomy {#promotion}

Research autonomy and production authority are different permissions. An agent can generate hypotheses, run approved experiments and recommend a candidate. Automatic promotion is allowed only for artifacts that fit an owner-approved class, parameter domain, instrument set, risk budget and evaluation protocol. Changes to code, broader permissions or increased capital remain owner decisions.

## Artifact registry

Every candidate has a unique hash over executable artifact or approved configuration, dependency lock, feature definitions and strategy schema. Its registry entry includes author, training interval, evaluation manifest, allowed markets, expected horizon, capacity estimate, expiration and rollback predecessor. A model alias that silently changes upstream is insufficient identity; retain provider version information when available and reevaluate behavior when it changes.

Statuses are proposed, reproducible, validated, paper, eligible, active, quarantined and retired. Transitions require evidence references and an authority identity. A dashboard cannot change a status by editing its display record. A promotion service verifies the transition policy and records it durably.

## Evaluation criterion

Predeclare a utility such as net return minus penalties for drawdown, tail loss, turnover and operational failures. The coefficients express policy choices and must not be tuned after seeing the candidate's results. A candidate should improve utility relative to an incumbent under paired evaluation while satisfying hard limits independently.

One example gate requires the lower bound of a paired block-bootstrap improvement interval to exceed a positive economic hurdle, with sufficient effective sample size and acceptable stress outcomes. The confidence level, block method, hurdle and trial correction must be fixed before evaluation. This is an example protocol, not a universal statistically sufficient recipe. A tiny sample cannot support strong claims merely because its point estimate is large.

Require robustness to plausible fees, latency, missing data and parameter perturbations. Reject data leakage and rights violations regardless of profitability. Maintain a complete trial register so that repeated candidate generation is visible. Stopping research only when a favorable p-value appears invalidates a naive fixed-sample test.

## Shadow and paper stages

In shadow mode, the candidate observes live eligible data and records intended actions without submitting them. Compare feature freshness, decision frequency and hypothetical exposure with the validated model. Broker paper mode then tests actual API integration, acknowledging simulated-fill limitations. Neither stage proves live execution quality.

Any later limited live stage requires the owner to authorize capital and account permissions separately. It begins with conservative exposure and an explicit stop condition. The book does not authorize that stage. The first one-to-two-week milestone remains paper operation because sample size and market regimes cannot be manufactured by working faster.

## Selection within an approved envelope

Automatic selection may switch between already-approved trend and cash states according to a fixed regime rule, or choose a validated parameter artifact inside a specified domain. It may not expand the domain, add shorting, increase capital or modify production source. A policy update takes effect for future sessions unless it tightens emergency restrictions.

The promotion decision records candidate and incumbent hashes, evidence, policy version, expected benefits, known limitations and rollback trigger. No agent can approve its own evaluation by changing the pass threshold. The evaluator has read access to results and write access to evidence, while the promotion service enforces a separate immutable policy.

## Rollback and quarantine

Rollback does not mean reversing trades. It disables future candidate entries, manages residual positions under their existing policy and activates a previously approved artifact only when state is reconciled. A rollback during an open position must preserve attribution and avoid duplicate exits.

Quarantine on unexplained feature drift, reconciliation errors, repeated operational failures, unexpected model behavior or violated performance-monitoring rules. Distinguish statistical underperformance from software defects. A healthy strategy may lose money; a profitable strategy with broken accounting is still ineligible.

## Human notifications

An agent may say that evidence supports increasing a budget or changing a parameter, with uncertainty and downside scenarios. The notification is advisory. Only the authenticated owner can modify the policy. Record acceptance or rejection without repeatedly pressuring the owner. A missed notification leaves the existing limits intact.

