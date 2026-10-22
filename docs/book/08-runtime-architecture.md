# Runtime architecture and authority boundaries {#runtime-architecture}

Use a modular architecture with three timing domains. The execution domain owns orders, reservations and immediate risk checks. The control domain owns policies, sessions, reconciliation and operator commands. The research domain owns datasets, experiments, news interpretation and cloud-model jobs. They share typed records, not unrestricted object references or administrative credentials.

## Initial implementation stack

Choose C++20 for the eventual native execution kernel and official supported broker SDK integration, Python for research and orchestration, and TypeScript for a browser terminal. Use a local HTTPS control API and server-pushed state updates. A browser is adequate because rendering speed does not determine order-loop latency. A native desktop wrapper may later improve packaging; it must not acquire a second independent trading authority.

For the first personal installation, prefer a few supervised processes to a distributed microservice estate. A single-writer ledger, broker adapter, research worker and web service are sufficient boundaries. SQLite is suitable for the offline reference and some single-host workloads; production durability and concurrency requirements may justify PostgreSQL. Do not introduce a message broker merely to transfer one local command. Adopt a durable queue when independent workers, retries and backpressure actually require it.

The included Python reference demonstrates economic invariants and replayable workflow. It is not the C++ production execution service. The native reference demonstrates deterministic pretrade checks. These two artifacts are deliberately small; the remaining adapters and terminal are implementation tasks with acceptance criteria, not advertised as completed features.

## Ownership and commands

The account coordinator is the sole authority for capital allocation. The order service is the sole authority for external order commands. Research workers return proposals. The risk evaluator returns an approval or structured rejection against a policy version and state version. A proposal cannot reserve cash or transmit an order by changing its own metadata.

The critical sequence is: receive intent; validate freshness and identity; evaluate risk; reserve funds and persist an outbox record atomically; transmit; record acknowledgment; reconcile subsequent events. A crash after transmission but before acknowledgment produces an unknown result. The outbox does not magically make the external broker transaction atomic. Recovery queries the broker using stable identifiers before deciding whether a retry is safe.

Store an immutable command identifier and payload hash. Reusing an identifier with a different payload is an error, not a retry. Commands include a deadline and expected policy version. An expired intent is rejected even if it was profitable when generated. The event log records the resulting decision and the state version that justified it.

## State and consistency

One writer serializes account mutations. Concurrent market analysis is allowed; concurrent unrestricted balance modification is not. Reads may use snapshots, but an order's reservation is evaluated against the writer's current committed state. Optimistic version checks prevent a UI decision based on an old balance from silently overriding a newer state.

Exactly-once delivery across network boundaries is not assumed. Use at-least-once ingestion plus durable deduplication of broker execution identifiers. Handle corrections explicitly as new economic events linked to the original. Never delete history to make a balance appear correct. A stale dashboard is less dangerous than a fabricated coherent one: display its timestamp and uncertainty.

The broker is authoritative for externally executed trades and account restrictions; the local ledger is authoritative for this application's policy allocations and intent history. Neither source alone is sufficient. Reconciliation joins them, exposes differences and disables additional exposure when the difference is material or unexplained.

## Failure isolation

A news parser crash should not stop risk checks. A model outage should not prevent cancellation or reconciliation. Loss of the database writer should prevent new exposure. A lost broker connection should set order state to unknown where appropriate; it must not mark everything canceled. Failure modes are explicit states with recovery criteria.

Maintain bounded queues. Market snapshots may be coalesced when the consumer needs only the latest quote; execution and money events may not be silently dropped. If a lossless queue exceeds its capacity, fail closed and preserve diagnostic state. Slow dashboards consume derived updates and cannot backpressure the order loop.

## Deployment boundary

There must be only one active execution leader for an account. A Mac developer instance and an AWS instance must not simultaneously issue orders under the same ownership assumption. A fencing token checked by the execution gateway provides stronger protection than a lease alone. If a broker cannot enforce fencing, route all account commands through a single credential-owning gateway and require explicit failover reconciliation.

The initial cloud topology is one execution host near the broker's relevant endpoint, durable encrypted storage, a separate research worker when needed, and private administrative access. Multi-region automatic trading failover is deferred until split-brain behavior is proven. Availability is valuable only when the surviving process knows what it owns.

## Acceptance criteria

The architecture passes its first gate when an offline intent travels through validation, reservation, simulated acknowledgment, fills, ledger entries and terminal projection; a crash/restart reproduces the same balances; duplicate events do not duplicate money; and a research worker has no path to bypass the order service. Latency claims require measured distributions on the actual target hardware. Component names and language choices alone prove none of these outcomes.

