# Verification, properties and fault injection {#verification}

Test the invariants that can cause economic harm if broken. Prioritize money conservation, resource ownership, duplicate handling, stop persistence, causal data and broker reconciliation. Avoid thousands of tests that simply repeat implementation details while missing a crash between reservation and transmission.

## Core properties

For the simplified reference, total policy cash plus receivables plus inventory cost equals deposits plus realized net P&L. Reservations do not create or destroy assets. Every position quantity and cash bucket remains nonnegative under the simulator's accepted events. A fill consumes at most the remaining order quantity. A completed session has no position or open order. Protected proceeds cannot fund a new session by default.

In production, real broker anomalies must be ingested through exception accounting rather than discarded to preserve an artificial nonnegative invariant. The command path rejects unsafe actions; the observation path records reality and freezes on inconsistency. Tests must distinguish these roles.

## State-machine testing

Generate legal and illegal sequences: create session, reserve buy, partial fill, cancel request, late fill, cancel confirmation, sell, settlement, completion and successor. At every step check conservation and ownership. Retry every command with the same payload and verify idempotence; retry with changed content and require rejection. Reopen the database between steps to test persistence.

Fuzz malformed quantities, negative fees, invalid price scales, huge integers, stale timestamps and unknown identifiers. Native arithmetic needs explicit overflow checks before multiplication. Test exact boundaries: a one-cent shortage, a quantity equal to owned inventory, and a quote exactly at its freshness limit.

## Fault matrix

| Fault | Expected response | Evidence |
|---|---|---|
| Duplicate fill | No second economic effect | Same balances and execution identity |
| Submit timeout | Unknown outcome, retained reservation | Broker query before retry |
| Cancel/fill race | Apply fill, release only remainder | Correct quantity/cash |
| Crash before commit | No partial local mutation | Transaction rollback |
| Restart after stop | Still stopped | Durable latch |
| Unsettled profit | Not spendable | Successor waits or uses other cash |
| Model injection | No authority escalation | Denied tool access |
| Data gap | Dependent entries blocked | Freshness/sequence alert |
| Disk full | No unjournaled new orders | Controlled halt |
| Split brain | One fenced writer | Rejected stale leader |

## Quantitative verification

Use synthetic processes with known properties to test estimators. Verify Welford against batch moments, Kalman updates against small matrix examples and portfolio variance against direct calculations. Check option prices against parity and boundary conditions. Use finite differences to validate Greeks where numerical stability permits.

These mathematical tests do not validate a trading edge. Separate estimator correctness, causal simulation, statistical inference and economic evaluation. A perfectly implemented model can be misspecified for the market.

## Integration and security gates

Run adapter tests against recorded fixtures, then broker paper accounts with owner-provided access. Validate real account identities and separate environments. Test that research processes cannot read secrets or mutate the ledger, and that the terminal cannot clear a stop without authenticated authority. Dependency scanning supplements, rather than replaces, code review and permission tests.

## Performance and release evidence

Benchmark on target hardware with realistic bursts and report latency distributions and queue behavior. Run recovery drills against a disposable copy and compare restored state with a known event journal. Keep results with artifact hashes so a later release cannot inherit evidence from different code accidentally.

The companion test suite covers offline accounting and native checks only. It does not test live brokers, real market data, legal eligibility, a full terminal or strategy profitability. Those remain explicit gates in the build sequence. This limitation is part of accurate verification, not a reason to skip the useful tests already available.

