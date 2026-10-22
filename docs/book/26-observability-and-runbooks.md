# Observability, economics and incident runbooks {#observability}

Observability answers three questions: what happened, why did it happen and what remains uncertain? Collect structured events, metrics and traces using stable account/session/order/job identifiers. Do not log credentials or unrestricted licensed content. Economic event retention and high-volume diagnostic retention have different requirements.

## Service and trading metrics

Track data age, sequence gaps, queue depth, decision latency, order acknowledgments, unknown orders, cancel completion, reconciliation differences, database failures and authentication expiry. Track cash, protected proceeds, exposure, realized/unrealized P&L, drawdown and loss-budget consumption. Track agent success, unsupported claims, token usage, retries and cost by job class.

Define service objectives with a measurement window and denominator. For example, the proportion of eligible control commands durably acknowledged within a measured threshold differs from market-order fill latency. Exclude neither outages nor slow requests merely because they are inconvenient. A readiness objective should count time blocked by unresolved state separately from process uptime.

## Economic ledger

Report trading P&L after direct execution costs and all-in P&L after allocated data, model, compute and software costs. Allocation rules should be stable: per-job costs can be charged directly, shared subscriptions by a documented period or strategy usage basis. Do not count unrealized gains as spendable profit or ignore idle infrastructure cost.

A break-even calculation is $E[net\ trade\ profit]\times expected\ trades > fixed\ cost$ under the assumed distribution. Both terms are uncertain and trading opportunities are not guaranteed. For very small session capital, service subscriptions and minimum commissions can dominate. The platform should show this explicitly before enabling paid services.

## Runbook: unknown order

Latch new-entry prohibition for the affected account, retain reservation, query broker open/completed orders and executions with overlap, compare stable identifiers, and ingest confirmed results. Do not resubmit solely because the original request timed out. If the outcome remains unresolved, escalate to the owner and broker support through authorized channels. Record every query and resolution.

## Runbook: position or cash mismatch

Block new entries, capture both snapshots with scope and timestamps, ingest missing fills/fees/corporate actions and identify manual trades. Classify valuation-only differences separately from quantity or money differences. Apply corrective economic events, never direct balance edits without an audit trail. Resume only after a fresh reconciliation meets the approved tolerances.

## Runbook: stale data or model outage

Disable strategies whose required inputs exceed their freshness budget. Existing positions follow their approved fallback risk policy. Cancel vulnerable resting entries if appropriate and supported. Model outages disable model-dependent new decisions; they do not disable deterministic stops, reconciliation or risk reduction. Resume only after warm-up and feature-state consistency checks.

## Runbook: excessive loss

Persist the aggregate stop, cancel entry orders and follow the approved reduction policy. Capture market data, strategy artifact, policy, fills and account state. A new session cannot reset the consumed loss allowance. The owner reviews whether the loss came from ordinary market outcomes, model failure, execution or software defects before changing any budget.

## Review cadence

Daily review covers reconciliation, fees, anomalies and source health. Periodic research review covers calibration, drift, capacity and economic performance. Release review covers dependency advisories, permissions and reproducibility. Legal/eligibility review is triggered by residence, account, instrument or rule changes rather than only a calendar reminder.

The terminal should turn each alert into a concrete status and next action. Avoid thousands of repeated messages that hide one unknown exposure. An incident closes only when its economic and operational consequences are reconciled, not when the error log stops growing.

