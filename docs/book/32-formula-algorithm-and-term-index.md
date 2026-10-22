# Formula catalog, algorithms and glossary {#formula-glossary}

## Formula catalog

| Reference | Purpose | Critical condition |
|---|---|---|
| 2.1-2.2 | Quote midpoint/spread and size proxy | Valid contemporaneous quotes |
| 3.1-3.2 | Notional and currency valuation | Compatible units; valuation is not cash conversion |
| 4.1-4.5 | Moments, sample dependence, Bayesian updates, calibration | Distribution and sampling assumptions |
| 5.1-5.4 | Online moments, EWMA, z-score, state space | Causal observations and stated noise model |
| 6.1 | Portfolio mean/variance | Consistent return horizon |
| 6.2 | Cost-aware allocation | Valid covariance and feasible constraints |
| 6.3 | Expected shortfall | Tail quantile definition |
| 6.4 | Position size | Stop distance is not a guaranteed bound |
| 7.1 | Implementation shortfall | Explicit unfilled opportunity cost |
| 9.1 | Spendable cash | Disjoint holds and confirmed balances |
| 14.1 | Latency budget | Measure actual complete event paths |
| 15.1-15.3 | Trend, momentum, ridge prediction | Training-only transformations |
| 16.1 | Pair relationship | Stability/cointegration need evidence |
| 17.1 | DCF | Discount rate exceeds terminal growth |
| 18.1 | Market-making quote approximation | Stylized utility/arrival model |
| 18.2 | Execution optimization | Simplified impact/risk model |
| 19.1 | Cloud-model cost | Current tariff plus retries/tools |
| 21.1 | European option price | Black-Scholes assumptions |
| 21.2 | Duration/convexity | Small compatible yield change |

## Algorithm catalog

**A01, causal ingestion:** validate source identity/sequence, normalize units/time, apply quality gates and publish versioned availability. Chapters 3, 11.

**A02, pretrade/outbox:** validate current intent/policy, reserve atomically, persist, transmit and reconcile uncertain results. Chapters 8, 9, 13.

**A03, execution ingestion:** deduplicate scoped identities, apply fills/corrections, update cash/inventory and reconcile fees. Chapters 9, 13.

**A04, session completion:** wind down, reconcile flat state, protect net proceeds, create one successor and fund from eligible cash. Chapter 10.

**A05, stop/start:** persist stop and cancellation progress; require owner authority and preflight for restart. Chapters 13, 22.

**A06, walk-forward evaluation:** train on past data, purge overlapping labels, evaluate unseen segments, compare paired net outcomes and stress assumptions. Chapter 7.

**A07, agent routing:** deduplicate jobs, enforce tool/deadline/cost budgets, select evaluated model, validate evidence and publish proposal/abstention. Chapter 19.

**A08, promotion:** verify approved envelope, artifact identity and evidence; retain rollback linkage. Chapter 20.

**A15, trend decision:** update causal features, calculate approved score, map exposure, account for current/pending positions and create expiring intent. Chapter 15.

**A16, paired strategy:** validate relationship, calculate synchronized spread, verify joint resources and control failed-leg exposure. Research-only initially. Chapter 16.

## Glossary

**Adverse selection:** fills occurring preferentially before unfavorable price moves.

**Alpha:** return beyond a specified benchmark or exposure model.

**Artifact:** versioned model, executable or dataset with reproducible identity.

**Availability time:** earliest legitimate decision-use time for information.

**Backpressure:** limiting incoming work when downstream capacity is exhausted.

**Buying power:** broker-defined field that may include borrowing.

**Calibration:** agreement between predicted probabilities and observed frequencies.

**Cancel race:** execution during cancellation or delayed cancellation reporting.

**Capacity:** capital/order scale before costs or constraints undermine economics.

**Cointegration:** a stationary relationship among appropriately nonstationary series.

**Corporate action:** issuer event affecting holdings, entitlements or basis.

**Drawdown:** decline from an adjusted high-water mark.

**Embargo:** excluded interval reducing overlap in evaluation information.

**Entitlement:** contractual permission for a dataset or service use.

**Execution:** fill event, distinct from order request or acknowledgment.

**Expected shortfall:** average tail loss using upper loss quantiles.

**Fencing:** enforceable rejection of a stale execution leader.

**Hysteresis:** different entry/exit thresholds to reduce rapid switching.

**Idempotence:** identical retries have one economic effect.

**Inventory:** holdings or exposure carried by an account/strategy.

**Look-ahead bias:** use of information unavailable at the historical decision.

**Mark:** valuation price, not necessarily an executable price.

**Market impact:** execution-associated price/cost effect.

**Outbox:** durable external-command record awaiting transmission or resolution.

**Point-in-time data:** historical information reconstructed as then available.

**Protected proceeds:** policy-restricted completed-session assets, not legal isolation.

**Reconciliation:** resolving local records against authoritative external activity.

**Reservation:** exclusive claim on resources for a pending commitment.

**Settlement:** relevant delivery/payment completion, separate from execution.

**Slippage:** execution difference from a stated benchmark and sign convention.

**Survivorship bias:** distortion from omitting entities that disappeared.

**Tail latency:** high-percentile delay, distinct from mean delay.

**Tax lot:** acquisition quantity and basis for disposal accounting.

**Tick:** permitted minimum price increment in context.

**Unsettled receivable:** expected proceeds not yet confirmed spendable.

**Walk-forward:** chronological training followed by unseen evaluation.
