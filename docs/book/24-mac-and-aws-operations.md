# Mac development and AWS deployment {#deployment}

Develop on the Mac Studio with isolated environments, pinned dependencies and paper-only credentials. Keep the local reference runnable without cloud services. AWS is a deployment option once the same artifacts pass tests; it is not a requirement for every development cycle. Infrastructure spending remains disabled until the owner supplies a budget.

## Local layout

Use separate directories for source, generated output, permitted datasets and runtime state. Keep secrets outside the repository. The Python reference uses only the standard library and a local SQLite file; the native check compiles with a C++20 compiler. The terminal and broker adapter will add dependencies through explicit lockfiles when implemented.

Run supervised processes with restart policies that preserve the stop latch. A restart begins in reconciliation-required mode. Health checks distinguish process alive, database writable, data fresh, broker authenticated and trading eligible. A green process indicator is not proof that orders are safe to submit.

## Cloud topology

Start with one credential-owning execution gateway, a durable database/storage volume, private administrative access and optional separate research workers. Choose region based on account/provider eligibility, data residency, latency and cost. Use least-privilege instance roles, restrictive security groups, protected metadata access and encrypted backups. Do not expose broker management interfaces or the control API directly to the public internet.

Use infrastructure as code, but require owner approval before provisioning paid resources. Separate paper and live accounts/environments. Production configuration comes from a validated deployment record, not a developer's unreviewed local file. Store immutable artifact identifiers and configuration hashes in every launch record.

## Durability and recovery

SQLite WAL has specific single-host and checkpoint behavior; it is not a shared network-filesystem database. Its current documentation also includes a WAL-reset bug advisory, so version selection must include current patch review. [S35] The educational reference uses rollback-journal mode with full synchronous settings to keep its assumptions narrow. Neither setting alone guarantees durability on faulty storage.

Back up using a database-consistent mechanism, encrypt the result and test restoration into an isolated environment. Define recovery-point and recovery-time objectives from acceptable economic uncertainty, not marketing targets. A restored ledger must reconcile with broker activity that occurred after the backup before it can resume trading.

## Failure scenarios

On lost internet, block new entries and mark affected order outcomes unknown. On disk-full, stop economic writes and new submissions; do not continue trading without a durable journal. On cloud-instance termination, preserve durable state and require leader fencing before replacement. On broker authentication expiry, notify the owner and keep research alive.

Automatic failover must avoid two active writers. A database lease alone can fail during a partition if the old process retains broker access. Prefer a single credential-owning gateway with fencing or require manual transfer of execution ownership after reconciliation. Multi-region active-active order submission is outside the initial design.

## Always alive

Continuous operation means the supervisor maintains service state, monitoring, research and recovery workflows. It does not mean every exchange is open or every broker session remains authenticated. Schedule venue calendars, maintenance windows, token renewal and data refresh. Explain inactivity reasons in the terminal instead of treating inactivity as failure.

Use alert severity based on actionability: immediate for unknown exposure or compromised authority, urgent for reconciliation failure, informational for a routine closed market. Rate-limit duplicate alerts and attach a runbook. The owner remains the escalation endpoint when a provider requires human authentication or a policy decision.

## Release and rollback

Build an immutable artifact, run offline tests, deploy to paper, verify migrations on a copy and only then consider an approved live release. Database migrations need forward/backward compatibility or a documented outage plan. Rolling back code does not automatically reverse a schema change or an executed trade.

Record software version, dependency versions, schema version, policy hash, account mode and source entitlements. A release is complete when its health, reconciliation and rollback checks pass. An automatically restarted process with unknown state is a recovery incident, not a successful deployment.

