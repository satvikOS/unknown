# Requirement register

Stable identifiers for the owner's requirements (implementation brief, 2026-10-22) and the book's product contract (R01–R15, Chapter 1). Code and tests cite these IDs. Status vocabulary (ADR-0001), from least to most evidence: `proposed`, `specified`, `implemented`, `tested`, `paper-verified`, `live-verified`. The evidence column names the artifact that justifies the status. The capability register (`docs/capabilities/register.json`) breaks the parity requirements (PAR-*) into vendor-referenced capabilities.

Statuses here are as of the commit that last edited this file; `docs/status/STATUS.md` holds the current summary.

## Domain and identity (book R05, R11)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| DOM-01 | Typed, schema-versioned domain records for instruments, venues, accounts, sessions, orders, executions, events and commands | Ch 3, 30 | specified | ADR-0004, ADR-0005 |
| DOM-02 | Exact money/price/quantity/FX types with explicit currency and scale; no binary floats | Ch 3, 9 | specified | ADR-0004 |
| DOM-03 | Internal immutable instrument identifiers; provider mappings as versioned child records | Ch 3 | specified | ADR-0004 |
| DOM-04 | Event identity, schema version, correlation and causation identifiers on every event | Ch 30 | specified | ADR-0005 |
| DOM-05 | Timestamps: event, receipt, recorded, availability; UTC storage; monotonic durations | Ch 3, 14 | specified | ADR-0005 |
| DOM-06 | Instrument, venue, account and jurisdiction capability records so new asset classes and markets (US, India, EU) can be added without rewriting the core | Ch 3, 21 | proposed | — |

## Ledger and money (R06, R08)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| LED-01 | Durable append-only economic journal, balanced per currency | Ch 9 | specified | ADR-0006 |
| LED-02 | Policy buckets as an owner dimension of economic balances; same money cannot be counted twice | Ch 9 | specified | ADR-0006 |
| LED-03 | Settled vs unsettled cash; receivables never spendable | Ch 9 | specified | ADR-0006 |
| LED-04 | Allocated vs unallocated capital; protected proceeds; operational buffer | Ch 9, 10 | specified | ADR-0006 |
| LED-05 | Order reservations (cash and owned quantity) before submission | Ch 9, 13 | specified | ADR-0006, ADR-0009 |
| LED-06 | Positions and cost basis via lots; FIFO default for session attribution | Ch 9 | specified | ADR-0006 |
| LED-07 | Estimated vs finalized fees; delayed commission handling | Ch 9 | specified | ADR-0006 |
| LED-08 | External deposits, withdrawals, manual trades and adjustments recorded and attributed | Ch 9, 26 | specified | ADR-0006 |
| LED-09 | Corporate actions (splits, cash dividends first; mergers and spin-offs later) | Ch 2, 9 | proposed | — |
| LED-10 | Corrections and busts as reversing plus replacing entries linked to the original | Ch 9 | specified | ADR-0009 |
| LED-11 | Exception journal: genuine broker facts always recorded; violations block new exposure | Ch 9, 28 | specified | ADR-0006 |
| LED-12 | Multicurrency balances; reporting conversion never creates spendable cash | Ch 3, 9 | specified | ADR-0004, ADR-0006 |

## Orders and execution (R02, R08)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| ORD-01 | Persist command identities with payload hash; reject changed payload under a reused id | Ch 8 | specified | ADR-0005 |
| ORD-02 | Reservation and outbox committed atomically before transmission | Ch 8, 13 | specified | ADR-0009 |
| ORD-03 | Submission timeout produces UNKNOWN; reconcile before any retry | Ch 8, 12 | specified | ADR-0009 |
| ORD-04 | Cancel request never releases a reservation; only confirmation or conclusive reconciliation does | Ch 9, 13 | specified | ADR-0009 |
| ORD-05 | Partial fills, cancel/fill races, duplicate and out-of-order callbacks, reconnects, late fills | Ch 2, 13 | specified | ADR-0009 |
| ORD-06 | Execution deduplication by scoped broker key; corrections linked | Ch 13 | specified | ADR-0009 |
| ORD-07 | Order state, transmission state and economic quantities kept separately | Ch 13 | specified | ADR-0009 |
| ORD-08 | Decision record for every accepted and rejected intent, including the first decisive reason | Ch 13, 15 | specified | ADR-0009 |
| ORD-09 | Price-bounded orders only for new entries in the initial profile | Ch 9 | specified | ADR-0009 |

## Sessions and capital (R06)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| SES-01 | Immutable mandate: seed, terminal-equity target, strategy eligibility, risk policy, policy version | Ch 10 | specified | ADR-0007, ADR-0008 |
| SES-02 | Seed and target configurable for future sessions; values are examples, not forecasts or funding authorization | Ch 10, 22 | specified | ADR-0007 |
| SES-03 | Changing defaults never changes an active session or an already-created successor mandate | Ch 10 | specified | ADR-0007 |
| SES-04 | Target observed on unrealized equity starts a controlled wind-down | Ch 10 | specified | ADR-0008 |
| SES-05 | Completion requires flat inventory, resolved orders, final fees and clean reconciliation | Ch 10 | specified | ADR-0008 |
| SES-06 | Defined outcome when realized proceeds fall below the observed target during wind-down | Ch 10 | specified | ADR-0008 |
| SES-07 | Completed proceeds become protected allocations; no invented transfer into the broker | Ch 10 | specified | ADR-0008 |
| SES-08 | Exactly one successor request per successful session | Ch 10 | specified | ADR-0008 |
| SES-09 | Successor funded only from independently eligible, reconciled, unallocated settled cash; never protected, unsettled or margin | Ch 10 | specified | ADR-0006, ADR-0008 |
| SES-10 | Insufficient funds leave a waiting request while monitoring and research continue; no borrowing | Ch 10 | specified | ADR-0008 |
| SES-11 | A new session never resets loss history, restrictions, obligations or the stop latch | Ch 10 | specified | ADR-0008, ADR-0010 |
| SES-12 | Unsuccessful sessions, interrupted wind-down, late fees, corrections after completion, failed settlement, restart and repeated completion all handled | Ch 10 | specified | ADR-0008 |

## Risk and control (R08, R14)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| RSK-01 | Conservative initial eligibility: no borrowing, unowned selling or unapproved products | Ch 6, 21 | specified | ADR-0009 |
| RSK-02 | Account- and session-level limits: concentration, liquidity, freshness, price collars, order rates, exposure, drawdown, cumulative loss | Ch 6, 13 | specified | ADR-0010 |
| RSK-03 | Red stop: durable latch, cancellation workflow, unresolved orders and residual positions visible; liquidation is a separate policy | Ch 13, 22 | specified | ADR-0010 |
| RSK-04 | Green start: authentication, policy, account, funding, health and reconciliation preflight before entries | Ch 13, 22 | specified | ADR-0010 |
| RSK-05 | Restart never clears an owner or risk stop; recovery and auto-resumption policy specified separately | Ch 13, 24 | specified | ADR-0010 |
| RSK-06 | Proposed conservative defaults with documented rationale; live capital never inferred from examples | Ch 6 | proposed | — |
| RSK-07 | No claims of zero risk, guaranteed profit or absolute maximum loss; remaining failure cases exposed | Ch 0, 6 | specified | ADR-0001 |

## Authority and agents (R07, R12, R13)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| AUT-01 | Owner controls capital, risk permissions, production code and policy expansion | Ch 20 | specified | ADR-0007 |
| AUT-02 | Agents research, recommend and operate only within an approved mandate | Ch 19, 20 | specified | ADR-0007 |
| AUT-03 | Automatic strategy selection only with evidence and an explicitly approved selection policy | Ch 20 | proposed | — |
| AUT-04 | No agent may raise its own budget, expand permissions, edit production code, approve its own evaluation or bypass the gateway | Ch 19, 20 | specified | ADR-0007 |
| AI-01 | Narrow agent roles with typed I/O, deadlines, tool allowlists, context limits, retries and budgets | Ch 19 | proposed | — |
| AI-02 | Model routing by measured quality, latency and cost; provider adapters for OpenAI, xAI and others with no hard-coded model names or prices | Ch 19 | proposed | — |
| AI-03 | Caching, deduplication, bounded fan-out, token and spend limits, cancellation and fallback | Ch 19 | proposed | — |
| AI-04 | Retrieved documents are untrusted; tests for prompt injection, fabricated evidence, malformed output, unavailable models and contradictory recommendations | Ch 19, 23 | proposed | — |
| AI-05 | Paid model calls disabled until a spending budget and provider access are explicitly configured | Ch 19 | proposed | — |
| AI-06 | "Intuition" means calibrated learned inference; "pressure" means controlled stress scenarios | Ch 19 | proposed | — |

## Brokers and reconciliation (R05)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| BRK-01 | Genuine adapter contract preserving broker differences in orders, balances, authentication, settlement, pacing and permissions | Ch 12 | specified | ADR-0012 |
| BRK-02 | IBKR paper adapter on official current documentation and an explicitly identified paper account | Ch 12 | proposed | — |
| BRK-03 | Pacing scheduler with reserved cancel and reconciliation capacity | Ch 12 | proposed | — |
| BRK-04 | Authentication-required state, reauthentication and supported restarts; no bypass of broker security | Ch 12, 24 | proposed | — |
| BRK-05 | Deterministic simulated broker clearly labelled as a fixture, with fault injection | Ch 28 | specified | ADR-0012 |
| REC-01 | Startup and periodic reconciliation: snapshot, executions with overlap, dedup, compare orders, positions, cash and fees; typed tolerances; watermark only after durable completion | Ch 9 | specified | ADR-0006 |
| REC-02 | Unknown external trades attributed to an external owner and reducing available capital | Ch 9 | specified | ADR-0006 |
| REC-03 | Reconciliation failure blocks new entries but permits controlled risk reduction | Ch 9 | specified | ADR-0010 |
| REC-04 | Settlement confirmed from broker balance evidence, not timers | Ch 9 | specified | ADR-0006 |
| EXE-01 | One accountable writer; no split-brain execution across Mac and AWS | Ch 8, 24 | specified | ADR-0011 |

## Data and analytics (R01, R03)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| DAT-01 | Provider registry: capabilities, entitlements, usage rights, costs, limits, freshness, failure behaviour | Ch 11 | proposed | — |
| DAT-02 | Source identity, event/receipt/availability time, correction history, dataset versions; point-in-time reconstruction | Ch 11 | proposed | — |
| DAT-03 | Market, reference, corporate-action, filings, statements, news, transcripts, macro releases and vintages | Ch 11 | proposed | — |
| DAT-04 | Guards against look-ahead, survivorship, restatement leakage, unit, currency and symbol-history errors | Ch 7, 11 | proposed | — |
| DAT-05 | Every displayed or model-consumed metric keeps formula, inputs, units, source dates, missingness and provenance | Ch 11 | proposed | — |
| DAT-06 | Missing, delayed, stale, unauthorized or unavailable data shown honestly; generated values never labelled live | Ch 11 | proposed | — |

## Quantitative research (R03, R04, R10, R12)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| RES-01 | Strategy and experiment registry with hypothesis, equations, variables and units, causal features, rules, costs, capacity, risk limits, failure conditions, splits, baselines, sensitivity, stress and promotion evidence | Ch 7, 15, 20 | proposed | — |
| RES-02 | Research families: trend, cross-sectional factors, mean reversion, pairs, fundamental/event, macro, microstructure, execution optimization | Ch 15–18 | proposed | — |
| RES-03 | Realistic fees, spreads, slippage, latency, order constraints, partial fills and settlement in simulation | Ch 7 | proposed | — |
| RES-04 | Multiple-testing and selection-bias accounting; meaningful out-of-sample evidence | Ch 4, 7, 20 | proposed | — |
| RES-05 | Deterministic simulation, historical replay, shadow, broker paper and live kept explicitly distinct | Ch 7, 20 | proposed | — |

## Terminal (R01, R14)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| TRM-01 | Dense professional workspace: navigation, linked instruments, inspectable sources, keyboard controls, persistent layouts, accessible tables, clear state transitions | Ch 22 | proposed | — |
| TRM-02 | Every enabled control connected to implemented behaviour; no dead navigation | Ch 22 | proposed | — |
| TRM-03 | Every financial number has real data lineage or an unmistakable test-fixture label | Ch 22 | proposed | — |
| TRM-04 | Workspaces for research, markets, analytics, portfolio and risk, strategies, sessions, blotters, agents, audit and operations, added as backing services exist | Ch 22 | proposed | — |
| TRM-05 | Browser vs native packaging chosen on workflow, rendering, accessibility, distribution and operations; execution performance measured separately from rendering | Ch 8, 22 | proposed | — |

## Security, operations and law (R09, R15)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| SEC-01 | Authentication, least privilege, credential isolation, encryption, dependency controls, audit records | Ch 23 | proposed | — |
| SEC-02 | Research workers never hold unrestricted production broker credentials | Ch 23 | specified | ADR-0002, ADR-0007 |
| SEC-03 | Backups, restore procedures and incident handling | Ch 23, 24, 26 | proposed | — |
| OPS-01 | Development on Apple Silicon Mac | Ch 24 | proposed | — |
| OPS-02 | Documented AWS deployment path; no paid resources provisioned by assumption | Ch 24 | proposed | — |
| OPS-03 | Stays alive through recoverable failures; stops trading when data, authentication, reconciliation, funding or risk require it | Ch 24 | specified | ADR-0010 |
| OPS-04 | Gateway restarts, reauthentication, supervision, alerting, clock handling, recovery and restore drills documented | Ch 24, 26 | proposed | — |
| LAW-01 | Broker rules, API conditions, market-data rights and US/India/EU requirements checked against the actual account entity, type, residence, instruments and hosting; unresolved questions recorded | Ch 25 | proposed | — |
| GATE-01 | No live orders, fund transfers, data purchases, paid cloud or paid inference without explicit authorization for that action | Ch 0, 27 | specified | ADR-0010 |

## Performance (R02, R11)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| PRF-01 | Latency distributions measured on actual hardware and representative workloads, with workload conditions | Ch 14 | proposed | — |
| PRF-02 | No exchange-level HFT claims through an internet brokerage; separate HFT research track | Ch 14, 29 | specified | ADR-0002 |
| PRF-03 | Cloud inference outside the immediate order and risk path | Ch 14, 19 | specified | ADR-0002 |

## Parity (R01)

| ID | Requirement | Book | Status | Evidence |
|---|---|---|---|---|
| PAR-01 | Persistent capability register from current official vendor documentation (Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro, IBKR) with all required fields | Ch 1, 29 | proposed | — |
| PAR-02 | Functional parity, data coverage, data quality, latency, usability and operational reliability tracked separately | Ch 1 | proposed | — |
| PAR-03 | Unavailable capabilities and lawful alternatives recorded; no copying of proprietary code, interface assets, licensed data or closed-network functionality | Ch 23, 29 | proposed | — |
