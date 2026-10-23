# ADR-0010: Red stop, green preflight, recovery gate and resumption

Status: accepted (2026-10-22)

## Decision

### Red stop

The red stop is one command (`OWNER`, or `SYSTEM` on a risk trigger). It durably sets the account `stop_latch`, with the actor, reason and command id. It then:

1. blocks every exposure-increasing command (buy intents, session starts, fundings that lead to entries);
2. enqueues an outbox `CANCEL` for every open buy order;
3. publishes progress as an `entries_blocked` flag, plus counts of open buys, cancels pending, `UNKNOWN` orders, open sells and residual positions per owner.

Liquidation is a separate policy, `on_stop_liquidation`. Its default, `NONE`, means sells already working stay working (they reduce exposure) and nothing new is sold automatically. "Flat" is reported only after a reconciliation shows zero positions, not because the local order list is empty.

### Green start

Green start is an `OWNER` command, with an authentication timestamp no older than the policy's `reauth_max_age`. It runs **preflight** and clears the latch only if every check passes. Each blocker is reported by name.

Preflight checks:

- the active policy is valid and its mode matches the broker account mode (simulated, paper or live);
- the deployment record satisfies that mode's gates; live has hard gates that are unset today;
- this process holds the execution-ownership token (ADR-0011);
- the broker session is connected and authenticated;
- the last reconciliation is clean and newer than both the process start and `max_reconciliation_age`;
- no open blocking exception and no `UNKNOWN` order;
- daily and cumulative loss budgets are not breached;
- market data required by running sessions is fresh;
- clock skew against the broker's server time is within bound;
- database writable and free disk above a threshold.

### Recovery gate

Every process start sets a process-local `recovery_gate`. New exposure is blocked until a clean reconciliation completes in this process. A restart never clears a stop latch.

Automatic resumption after a restart, meaning the recovery gate clearing with no owner command, is allowed only when **all** of these hold:

- no stop latch was set;
- the active policy has `auto_resume_after_clean_reconciliation = true` (default `false`);
- the preflight subset that needs no owner action passes.

A risk stop (loss budget, drawdown or reconciliation failure) sets the latch. Clearing it always requires a green start by the owner. A loss budget can only be widened by publishing a new policy.

## Consequence

The system can stay alive (monitoring, research, reconciliation) while correctly refusing to trade. The terminal shows the reason.

## Amendment (2026-10-22): a red stop is never blocked by persistence

Implemented on `impl/foundation` at 8b98d00 (book-review findings BR-SES-13, BR-REF-19, BR-ORD-20). The latch above stays per broker account and stays in the fenced writer's control row; this section decides what happens when that row cannot be written.

**Invariant failures.** The stop is recorded even when a ledger invariant check fails after it (`RECORD_DESPITE_INVARIANTS`). The failure opens a blocking `LEDGER_INVARIANT` case and latches the stop, so damage in a projection the stop touches never blocks it.

**Memory latch.** If the database cannot record a stop at all (disk full, I/O error, a locked or damaged store, or a control row the desk cannot read), the core service (`service/core.py`) latches it in memory. Only an OWNER or SYSTEM stop of the core's own account, with a well-formed command id, can do this. Then:

1. exposure-increasing commands are refused at the socket (`start`, `start_session`, `resume_session`, `create_session`, `amend_session_request`, `authorize_capital`, and `submit_intent` for a buy);
2. the gateway records a claimed buy as `REJECTED` (`NOT_TRANSMITTED`) instead of sending it, while sells and cancels still go out;
3. every open buy, from the ledger and from the broker's open orders carrying our client order id, gets a best-effort cancel sent to the broker directly;
4. the failure is logged CRITICAL and repeated every 10 s while it lasts; the health query shows `memory_latch`, and preflight reports `STOP_LATCHED_IN_MEMORY`;
5. the latch is mirrored in an fsync'd file, `<UNKNOWN_HOME>/data/<account>.stop-latch.json`, so a restart keeps it. A latch file that cannot be read, or that holds anything but stops of this account, makes the service start latched with its own SYSTEM stop (`UNREADABLE_STOP_LATCH_FILE`).

The caller's answer says `persisted: false`. As soon as the database accepts writes (the first command or loop cycle), the service records each stop under its own command id. It then records a SYSTEM stop with reason `MEMORY_LATCH_PERSISTED`, whose `StopReasserted` event carries the evidence: the error, the cancels sent and the ids it stood in for. Only then is the memory latch released and its file removed. Clearing the stop still needs the owner's green start. If the latch file cannot be written either, the latch holds in that process only (logged CRITICAL).

**Other latch reasons added by this release:** `ORDER_TRANSITION_REFUSED` (ADR-0009 amendment) and `EVENT_JOURNAL_UNWRITABLE: <error>` (the broker-event journal cannot be written or trusted; ADR-0012 amendment). The runbooks are RB-12 and RB-15.
