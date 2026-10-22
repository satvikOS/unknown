# Order management and execution control {#order-management}

An order has intent, transmission and economic state. These must not be collapsed into one status string. A local intent may be approved but never transmitted; a transmitted order may have an unknown outcome; a canceled order may still have earlier fills arriving late. Positions are derived from executions and corrections, not from an optimistic interpretation of order status.

## States and identifiers

Use local states `CREATED`, `RESERVED`, `SUBMIT_PENDING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED` and `UNKNOWN`. Store cumulative filled quantity, remaining quantity, average execution price, reservation and broker identity independently. Terminal status does not mean every fee or correction has arrived.

Every intent has a globally unique client identifier and immutable payload hash. Each broker execution is deduplicated by its documented stable key, including account scope. Store replacement chains for amendments. Increasing a buy's limit or quantity requires a new risk check and extra reservation before the change is sent. A cancel-and-replace can temporarily leave both orders exposed if the broker does not provide atomic replacement semantics.

## Pretrade sequence

Validate account mode, owner authority, session state, instrument eligibility, market state, quote freshness, price/quantity scale, price collar, cash, reserved inventory, concentration, aggregate exposure, loss triggers, order rate and duplicate identity. Evaluate against a coherent state snapshot. A rejection records the first decisive reason plus relevant diagnostics; it must not be reinterpreted as permission to try a slightly different order indefinitely.

A price collar bounds deviation from a validated reference. If reference midpoint is $m$, a buy cap might be $m(1+c)$, additionally constrained by absolute ticks and venue rules. The collar must not use an untrusted or stale quote. Volatile instruments and auctions require separate policies rather than arbitrarily wide universal collars.

After approval, commit reservation and durable outbox in one transaction. The sender records attempt time and broker result. On restart, pending attempts are reconciled, not blindly replayed. This architecture minimizes duplicate risk but cannot create a distributed transaction with an external broker.

## Cancellation and stopping

The red button first persists a global stop latch and prevents new exposure-increasing commands. It then requests cancellation of open entries and records each outcome. A separate approved liquidation policy may submit risk-reducing orders. The UI distinguishes stop requested, entries blocked, cancels pending, residual positions and reconciled flat. A stop is not a guarantee that a disconnected or halted market will execute.

The green button requires an explicit owner action, authenticated session, clean reconciliation, eligible account, adequate funds, fresh policy and healthy critical services. It clears the latch only after the checks pass. An automatic process restart cannot clear it. Agents cannot press green or loosen its checks.

Risk-reducing orders still need safeguards. Selling more than the reconciled owned quantity can create a short. A stale position can make a nominal reduction increase risk. In derivatives, reducing one leg can increase portfolio risk. The initial cash-equity implementation defines reduction narrowly as selling confirmed unreserved long inventory within approved price constraints.

## Execution scheduling

For medium- or low-frequency strategies, simple limit orders, time-weighted schedules and participation caps are useful baselines. A schedule specifies parent quantity, horizon, child-size limits, price bounds, cancellation policy and terminal behavior. It must reserve the full economic commitment, not only the current child order, unless the allocator explicitly controls the remainder.

Avoid endless cancel/replace loops. Each amendment can lose queue priority, consume pacing capacity and incur adverse selection. Measure the trade-off between urgency, fill probability and cost. An execution algorithm should have an explicit condition to stop chasing a price when the original expected edge no longer exceeds costs.

## Fill attribution

Allocate fills to parent, strategy, session and tax-lot policy using recorded rules. If the portfolio nets opposing signals before execution, allocate the economic benefit and opportunity cost consistently. Do not attribute favorable fills to one model and unfavorable residuals to an external bucket after observing the outcome.

Track decision price, submission reference, acknowledgment time, first/last fill, realized spread at several horizons, fees and unfilled quantity. Realized spread can diagnose adverse selection, but needs synchronized timestamps and an appropriate future midpoint. Execution quality must be separated from whether the underlying market subsequently rose.

## Acceptance evidence

Replay a fill arriving during cancellation, an acknowledgment lost after acceptance, a duplicated execution, an amendment rejected after the old order filled, and a process crash between reservation and transmission. Verify no invented cash, no negative owned quantity under the simulated assumptions, and no duplicate exposure from retries. Production adds broker corrections and unexpected external events through the exception path described in Chapter 9.

