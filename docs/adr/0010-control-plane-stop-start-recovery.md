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
