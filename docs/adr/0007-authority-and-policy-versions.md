# ADR-0007: Principals, permissions, policy versions and mandate snapshots

Status: accepted (2026-10-22)

## Decision

### Principals

Every command carries an authenticated `Principal(id, role)`. The trusted API layer supplies it after authenticating the owner, or a service supplies its own fixed identity. The core never accepts identity from a caller-controlled payload field.

| Role | May do | May never do |
|---|---|---|
| `OWNER` | Publish and activate policies; authorize capital; create, end and resume sessions; red stop; green start; resolve exceptions; accept fee estimates; transfer execution ownership | (nothing withheld) |
| `SYSTEM` | Record broker observations and reconciliation results; raise exceptions; latch stops on risk triggers; run session transitions that policy defines as automatic (wind-down on target, successor creation and funding) | Clear a stop; widen a policy; authorize capital |
| `STRATEGY` | Submit order intents and cancels for a session whose mandate lists its artifact | Anything outside that session's mandate; control commands |
| `AGENT` | Submit research proposals (typed, non-executable) | Every core mutation, including stop/start, intents and policy |

A permission table in code enforces this, and tests cover every role/command pair. Agents can never increase a budget, widen permissions, approve their own evaluation or bypass the gateway, because no command for those actions exists for their role.

### Policy documents

A policy is a canonical JSON document with a schema version. Its SHA-256 over the canonical form is the `policy_hash`. Publishing is append-only. Activating a policy as the account default is a separate owner command. A policy contains:

- mode;
- eligibility profile;
- account risk limits;
- loss budgets;
- the session template (the default mandate);
- the successor rule;
- the wind-down policy;
- the stop-liquidation policy (default `NONE`);
- the auto-resume rule (default off);
- the fee model reference.

Unknown or missing fields fail validation. They are never read as permissive defaults.

### Mandates

A session's mandate is an immutable snapshot, hashed and stored at session creation. It contains seed, terminal-equity target, currency, strategy eligibility, the session risk policy and the `policy_hash` it came from. Activating a new default never changes an existing mandate. A successor's mandate is snapshotted from the default that is active **when the successor request is created**, and it is frozen from then on.

An owner can explicitly amend a not-yet-running request by issuing a new mandate version (audited). Defaults never propagate silently. Emergency tightening is a separate control: the stop latch and account blocks act on every session at once without editing their mandates.
