# ADR-0008: Session state machine, completion and successor semantics

Status: accepted (2026-10-22)

## Context

Book Chapter 10 lists ten states: REQUESTED, WAITING_FUNDS, READY, RUNNING, WINDING_DOWN, RECONCILING, COMPLETED_UNSETTLED, COMPLETED_SETTLED, STOPPED and FAILED_REVIEW. It does not define the transitions. It also leaves several cases open:

- what happens when realized proceeds fall below the target after wind-down;
- how an unsuccessful session ends and what happens to its capital;
- whether STOPPED is terminal while positions remain;
- interrupted wind-down;
- late fees after completion;
- failed settlement.

The reference engine implements only RUNNING, RISK_STOP and COMPLETED.

## Decision

The book's `STOPPED` splits into `SUSPENDED` (non-terminal, entries blocked, positions held) and `CLOSED` (terminal, flat, capital released). `CANCELLED` covers requests that never ran.

| From | To | Trigger (principal) | Conditions |
|---|---|---|---|
| — | REQUESTED | create (OWNER) or successor (SYSTEM) | mandate valid under its policy |
| REQUESTED | READY | fund (SYSTEM) | seed ≤ eligible unallocated cash (ADR-0006); account reconciled; no blocking exception |
| REQUESTED | WAITING_FUNDS | fund attempt fails (SYSTEM) | reason recorded |
| WAITING_FUNDS | READY | funding retry (SYSTEM) | as above; retried after every settlement, capital authorization, or session close/cancel |
| REQUESTED/WAITING_FUNDS/READY | CANCELLED | cancel (OWNER) | allocated seed returned to `unallocated` |
| READY | RUNNING | start (OWNER), or SYSTEM if mandate `auto_start` | control green; preflight passes (ADR-0010) |
| RUNNING | SUSPENDED | session risk trigger (SYSTEM) or owner suspend | entries blocked; positions held |
| SUSPENDED | RUNNING | resume (OWNER only) | preflight passes; budgets not breached |
| RUNNING/SUSPENDED | WINDING_DOWN | target observed (SYSTEM), end (OWNER), risk end (SYSTEM), mandate expiry (SYSTEM) | reason and evidence recorded |
| WINDING_DOWN | RECONCILING | (SYSTEM) | no open orders; inventory flat |
| RECONCILING | COMPLETED_UNSETTLED | (SYSTEM) | reason = TARGET_OBSERVED; all fills fee-final; post-flat reconciliation clean; **realized net terminal equity ≥ target** |
| RECONCILING | CLOSED | (SYSTEM) | any other outcome: BELOW_TARGET, OWNER_ENDED, RISK_ENDED, EXPIRED |
| COMPLETED_UNSETTLED | COMPLETED_SETTLED | settlement confirmations (SYSTEM) | every protected receivable settled |
| any non-terminal | FAILED_REVIEW | (SYSTEM) | an open exception is attributed to this session |
| FAILED_REVIEW | previous state or WINDING_DOWN | resolve (OWNER) | exception resolved |

**Terminal-equity target** means session equity, not profit. Session equity = owner balances (cash + receivables − payables − accrued fees) plus the marked value of the owner's lots. Marks use the conservative executable side (bid for long inventory) from a fresh, attributed quote.

**Target observation on unrealized equity** starts a controlled wind-down. It is not completion.

**Falling below target during wind-down.** Under the default policy `below_target_after_target_observed = CLOSE`, the session ends `CLOSED` with outcome `BELOW_TARGET`. It does not re-enter. This avoids oscillating between liquidation and re-entry (book Chapter 10). The alternative value `RESUME` is permitted only with a positive `max_resumes` written into the mandate.

**Completion** moves every remaining balance of `session:<id>` to `protected:<id>`, receivables included. It creates exactly one successor request, keyed by the predecessor's id; a database uniqueness constraint backs this. It attempts funding immediately. A repeated completion command returns the original result.

**Closing an unsuccessful session** moves its cash to `unallocated` and re-owns its receivables to `unallocated`. Aggregate daily and cumulative loss budgets are account-level and survive across sessions, restarts and policy versions. A new session never resets them.

**Interrupted wind-down.** After a restart the session stays in `WINDING_DOWN`. Its reduction orders are recovered through reconciliation, and wind-down continues only after the recovery gate clears (ADR-0010).

**Late fees and corrections after completion** post against `protected:<id>`. If they push protected value below the target, the completion stands, because it was decided on the evidence available. A non-blocking exception records the change for owner review. If they would make protected cash negative, a blocking exception is opened.

**Failed settlement.** A protected or unallocated receivable that is not confirmed by its expected date plus a grace period opens an exception. Funding that would depend on it is not attempted (receivables are never available anyway). The session stays `COMPLETED_UNSETTLED`.

**Protected proceeds** never fund another session. That would require an explicit owner reinvestment rule, which the initial policy schema does not offer.
