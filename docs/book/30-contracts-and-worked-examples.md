# Contracts, worked examples and acceptance matrix {#contracts-appendix}

## Event and command contracts

A production event envelope includes event ID/type, schema version, source, account and instrument identities where relevant, event/receipt/recording timestamps, source sequence, correlation ID, causation ID, payload hash and typed payload. If a source lacks a timestamp or sequence, represent absence explicitly. A payload hash is not always a unique event identity: two legitimate equal-sized fills can share economic fields. Retain scoped broker execution identities and correction links.

A command includes authenticated identity, immutable command ID, expected state version, policy hash, deadline and typed intent. Authentication supplies identity; it is not accepted from a caller-controlled string. An order adds instrument, side, scaled quantity/limit, currency, time-in-force, session, strategy artifact and decision evidence. Reject unsupported combinations. Return accepted, rejected or unknown with a durable reason/correlation record.

Schema evolution is versioned. A changed price scale is not a harmless field rename. Replay tests cover prior versions and explicit migrations. Broker-specific fields remain namespaced and visible.

## Double-entry and session example

An account begins with $300 settled cash. Assigning $100 to a session changes policy allocation only. Buying ten shares at $9 with a $0.10 fee produces $90 inventory, $209.90 total cash and $0.10 expense. Selling all ten at $9.60 with a $0.10 fee removes $90 inventory, creates a $95.90 receivable and recognizes $6 gross gain plus $0.10 sale expense. Net trading profit is $5.80.

The session retains $9.90 cash plus $95.90 receivable, totaling $105.80. Completion protects both components. A successor receives $100 from the original unallocated $200, leaving $100 unallocated. After settlement, protected settled cash is $105.80 and the successor has $100. Total assets are $305.80. No deposit or withdrawal occurred; settlement only reclassified the receivable.

The offline demo implements this example. It reports cents: protected 10,580; successor 10,000; unallocated 10,000; realized net P&L 580. These are synthetic values, not evidence of a tradable opportunity.

## Feasibility example

If an instrument requires whole shares at $12, expected round-trip fees are $0.40 and an execution buffer is $0.10, a $1 seed cannot fund one share. Fractional support changes quantity feasibility only if verified; it does not create positive economics. If expected gross gain is $0.03 and expected costs are $0.06, reject the trade. The minimum viable seed depends on price, lot size, fees, diversification and strategy risk, rather than a universal dollar amount.

## Reference-to-production map

| Concern | Reference | Required extension |
|---|---|---|
| Money | Integer USD cents | Multicurrency journal and scales |
| Inventory | Whole shares, average cost | Lots, actions and corrections |
| Authority | Synthetic assertions | Authenticated identities and permissions |
| Fees | Fill-time envelope | Delayed finalization and adjustments |
| Settlement | Synthetic confirmation | Calendars and broker reconciliation |
| Submission | Simulated record | Outbox and uncertain-outcome recovery |
| Risk | Simple marked triggers | Full daily/aggregate portfolio policy |
| Storage | Atomic JSON state | Evaluated production storage |
| Audit | Diagnostic hash chain | Protected independent retention |

## Requirement traceability

| ID | Main chapters | Acceptance evidence |
|---|---|---|
| R01 | 11, 17, 22 | Reconciled terminal and source drill-down |
| R02 | 8, 13, 14, 29 | Target-hardware latency distributions |
| R03 | 4-7, 15-18, 20 | Reproducible causal experiments |
| R04 | 4, 19, 20 | Calibration plus independent controls |
| R05 | 11, 12, 25 | Rights and adapter capability records |
| R06 | 9, 10 | Conservation and one-successor tests |
| R07 | 19 | Bounded jobs, deadlines and quotas |
| R08 | 6, 9, 13 | Reservations, triggers and reconciliation |
| R09 | 24, 26 | Restart and authentication runbooks |
| R10 | 19, 28 | Adversarial/fault evaluation |
| R11 | 8, 14, 27 | Dependency and bottleneck review |
| R12 | 4, 7, 19, 20 | Held-out task/economic evaluation |
| R13 | 10, 20, 23 | Denied unauthorized changes |
| R14 | 13, 22 | Durable stop and gated restart |
| R15 | 12, 21, 25 | Verified account/market eligibility |

## Conservation proof sketch

Let $C$ be all policy cash, $U$ unsettled receivables, $I$ inventory cost, $D$ deposits and $P$ realized net P&L. The reference invariant is $C+U+I=D+P$. Buying changes cash by $-(pq+f)$, inventory by $pq$ and P&L by $-f$. Selling changes inventory by $-b$, receivables by $pq-f$ and P&L by $pq-f-b$. Settlement changes cash by $+u$ and receivables by $-u$. Allocation/protection only move policy ownership. Each operation preserves equality.

The proof assumes accepted synthetic events and exact arithmetic. Market value can change without an accounting event, so marked risk requires a separate valuation layer. Production adds external flows, corporate actions, corrections and currencies with explicit conservation rules.

## Authority topology

<!-- FIGURE: architecture -->

## Session lifecycle

<!-- FIGURE: sessions -->
