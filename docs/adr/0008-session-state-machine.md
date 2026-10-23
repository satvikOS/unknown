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

## Amendment (2026-10-22): late fills, busts and corrections after a session ended; owner disposal

Implemented on `impl/foundation` at 8b98d00 (book-review findings BR-LED-03, BR-SES-02). The rule "the completion stands" above is unchanged; this section decides what it left open.

**Who bears a late effect.** A late fill, a bust (a correction to quantity zero) or a price or quantity correction for an order of a session that has ended is booked to whoever holds that session's balances now: `protected:<id>` after `COMPLETED_UNSETTLED` or `COMPLETED_SETTLED`, `unallocated` after `CLOSED` or `CANCELLED`. The session never re-opens; its state, outcome, protected value and successor are not changed. A bust reverses the original execution onto that owner and records the bust row with no booking of its own. Shares a busted sell had delivered come back to that owner with their cost, and a bust of a buy whose shares were already sold restates the sale's cost there.

**Cases.** Each such execution, bust or correction opens one blocking `POST_COMPLETION_ADJUSTMENT` case per execution, naming the owner that bore it. This now also applies after `CLOSED`. A later fee change on the same execution joins that case. A late fee on its own keeps the rule above: a non-blocking case, escalated to blocking when the owner's cash would go negative. A shortfall moves to `exception:<case>` as an `OVERCOMMITTED` case (ADR-0006). A fill on an order that is already terminal also opens the usual `EXECUTION_ANOMALY` ("fill after terminal state"). A bust while the order is still working re-reserves the order's remaining quantity and opens an `EXECUTION_ANOMALY` asking for the broker's remaining quantity to be confirmed.

**Owner disposal.** Inventory that lands in `protected:<id>` or `unallocated` cannot be sold by the ended session, because pretrade refuses orders for a terminal session. The owner exits it with `submit_intent` purpose `DISPOSAL`: an OWNER-only SELL that names the ended session, reserves and sells only that owner's lots, and books the fill, fee and settlement to the same owner. It runs the full pretrade sequence; the owner's disposal stands in for the `SESSION_STATE` check, and the recorded decision says so. Every other check still applies, including owned unreserved quantity, quotes, collar and position-related exceptions (an open `EXECUTION_ANOMALY` refuses the disposal until it is resolved). A DISPOSAL for a session that has not ended is refused `SESSION_NOT_ENDED`. A disposal's own fills and first commission are expected activity and open no new case. A bust, correction or later fee change of a disposal fill opens a `POST_COMPLETION_ADJUSTMENT`. Reconciliation confirms the position and settlement like any other trade. The runbook is RB-14.
