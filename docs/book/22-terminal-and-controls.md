# Research terminal and operator experience {#terminal}

The terminal is a view of authoritative system state and an authenticated command surface. It must help one person understand what the system knows, owns, risks and is doing. Dense financial screens are useful only when timestamps, units, evidence and uncertainty remain legible. Browser rendering is the initial choice; execution remains in independent services.

## Workspace map

| Workspace | Essential content | Primary action |
|---|---|---|
| Overview | Mode, account health, capital, exposure, stop state | Inspect blockers |
| Markets | Watchlists, quotes, charts, market status | Open instrument |
| Instrument | Reference data, filings, news, analytics | Inspect evidence |
| Research | Experiments, baselines, manifests, uncertainty | Compare candidates |
| Strategies | Approved artifacts, drift, decisions | Propose or review change |
| Sessions | Seed, goal, P&L, protection, settlement, successor | Inspect lifecycle |
| Orders | Intent, broker status, fills, cancel state | Controlled cancel |
| Risk | Cash, positions, limits, aggregate loss | Tighten or stop |
| Agents | Jobs, evidence, latency, token cost | Inspect failed job |
| Operations | Reconciliation, alerts, service health | Follow runbook |

Provide a command palette and keyboard navigation. Use semantic labels and accessible contrast, not color alone. Display paper/live mode persistently in text and color. Quotes show source and age. Financial values show currency, scale and whether they are realized, unrealized, settled or protected.

## Start and stop

The red control is always reachable and uses a short authenticated command path. On activation, show the durable command identifier and progress: entries blocked, cancel requests sent, outstanding unknown orders, residual positions and final reconciliation. Do not display flat merely because the local order list is empty.

The green control opens preflight status and requests activation. Blockers identify concrete causes such as unresolved position difference, expired broker authentication, unset live capital or unavailable market data. The owner can resolve the cause; the interface does not offer a generic bypass. Starting after a stop requires owner authentication and a fresh state version.

Stopping new entries and liquidating holdings are separate policies. Label them clearly. A stop should not unexpectedly fire unrestricted market sells. The configured emergency policy and its limitations are visible before activation.

## Session editor

Show seed, terminal-equity goal, implied required return, currency, maximum active allocation, loss triggers and protected-proceeds rule. Editing defaults creates a new policy version for future sessions. Show the difference and affected sessions. A $1-to-$10,000 request displays its 999,900% required net return and any minimum-size/fee infeasibility; it does not present the goal as a forecast.

The session timeline separates goal observed, wind-down, reconciliation, completed proceeds and settlement. A waiting successor states exactly why it lacks funding. The owner can see that continuous application operation is compatible with no current trade.

## Evidence-first analytics

Every performance chart links to its manifest, cost assumptions and benchmark. Separate simulated, broker-paper and live results. Show gross, net trading and all-in economics distinctly. A single equity curve can hide deposits and changing exposure; include external-flow adjustments and capital utilization.

An AI explanation displays source links, factual claims, assumptions and confidence-calibration context. A model's prose cannot overwrite a numeric risk calculation. If an explanation is unavailable, the terminal still shows the underlying deterministic decision record.

## Frontend implementation contract

Use a typed API client, normalized state store and incremental server events with sequence numbers. On a gap, fetch a fresh snapshot before applying further updates. Client-side validation improves usability; the server repeats all authority and risk checks. Use optimistic updates only for harmless preferences, never for fills, funds or successful stops.

Virtualize large tables and aggregate chart data for display without altering source records. Keep rendering work off critical service threads. Log command failures with correlation identifiers while redacting credentials and personal data. Test stale tabs, reconnects, two simultaneous browser sessions and rejected policy changes.

The book specifies these screens; the delivered companion does not claim a completed web terminal. The build plan provides their acceptance sequence so Claude Code can implement them without inventing financial semantics.

