# ADR-0011: Execution ownership and split-brain prevention

Status: accepted (2026-10-22)

## Context

The owner develops on a Mac and may deploy to AWS. Two instances with credentials for the same broker account could both submit orders. That is split brain, and a database lease alone cannot prevent it because the instances have different databases. IBKR's API offers no fencing token that the broker enforces (to be re-verified in the IBKR research record).

## Decision

1. **Per-database writer lease with fencing epoch.** The core acquires `writer_lease` (holder id, host, epoch) at startup. Every write transaction checks that the lease epoch equals the epoch this process acquired. A second process on the same database cannot write, and a stale process is rejected by the epoch check.
2. **Per-broker-account execution ownership.** The `execution_ownership` record (account, deployment id, epoch, granted_at, granted_by) must be held before any outbox row is transmitted. A deployment acquires ownership only through an owner command. Moving it to another deployment is an explicit, audited transfer:
   1. the releasing side latches the stop, waits for no open orders (or records the residuals), reconciles, and emits a signed release certificate containing its final reconciliation;
   2. the acquiring side imports the certificate, reconciles against the broker, and only then holds ownership.
3. **Order tagging.** Every transmitted order carries `<deployment-id>:<epoch>:<client-order-id>` in the broker's order-reference field. Reconciliation treats any open order or execution whose tag names a different deployment, or names none, as foreign activity. Foreign activity opens a blocking exception that latches the stop.
4. **One credential-owning gateway per account.** Only the owning deployment holds the trading credential. The IBKR adapter uses a fixed client id per deployment and refuses to start if the gateway reports the id already in use.

## Consequences

Automatic multi-host failover is not supported. Failover is a manual, reconciled ownership transfer, which the book allows (Chapter 24). A paper account and a live account are distinct accounts with distinct ownership records, credentials and databases.
