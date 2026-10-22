# Session orchestration and protected proceeds {#sessions}

A session is a bounded accounting and strategy mandate. It owns an immutable seed allocation, target definition, policy snapshot, eligible strategies, loss budget and lifecycle identifier. It does not own an independent brokerage account unless an actual broker account structure provides one. Multiple sessions sharing an account also share settlement, operational and counterparty dependencies.

## Meaning of a target

Specify whether the target is terminal session equity or profit. In this book, a seed of $100 and target of $5,000 means terminal equity of $5,000, hence $4,900 profit before any separately excluded taxes. The terminal displays both values. A target measured from unrealized marks is a provisional signal to wind down; it is not completed profit. Completion requires flat inventory, no unresolved orders, reconciled net proceeds and sufficiently finalized fees.

The target is subordinate to risk constraints. There is no deadline-based increase in leverage, no doubling after losses and no automatic replenishment of an exhausted loss budget. A session can terminate unsuccessfully, wait indefinitely for an admissible opportunity or be stopped by the owner. Requiring every session to reach a positive goal would make honest loss handling impossible.

## State machine

Use `REQUESTED`, `WAITING_FUNDS`, `READY`, `RUNNING`, `WINDING_DOWN`, `RECONCILING`, `COMPLETED_UNSETTLED`, `COMPLETED_SETTLED`, `STOPPED` and `FAILED_REVIEW`. State transitions are durable commands with identifiers. A successful transition has a single successor request keyed by the completed session identifier. Retries return that request; they do not create an unlimited series of successors.

At target observation, stop new entries for that session and cancel outstanding entry orders. Reduce remaining positions according to the approved execution policy. A price can change while winding down. If final net equity falls below target, record that fact; policy must explicitly decide whether the session resumes or stops. The proposed default is to stop and require the ordinary session evaluation path, rather than oscillating automatically between liquidation and re-entry.

Once completion is reconciled, lock the session's economic value in a protected policy bucket. Unsettled proceeds remain receivables. They are already part of the brokerage account's accounting; the application is not sending profits from an imaginary bot wallet into the broker. A withdrawal to a bank is a separate action and is outside automatic session completion.

## Successor funding

The coordinator creates the successor request when the prior session completes, even if proceeds are unsettled. It funds the successor from independently available, unallocated, eligible cash. If cash is insufficient, the request remains `WAITING_FUNDS`. The rest of the platform continues monitoring and researching. This is the precise implementation of continuous operation without spending money that is not yet available.

Example: account has $300 authorized settled cash. Session A receives $100; $200 remains unallocated. A finishes with $105 net proceeds, still unsettled. Its $105 is protected and a successor B can receive $100 from the untouched $200. Account economic assets are now $305, of which $105 is protected, $100 allocated to B and $100 unallocated. On A's settlement, $105 changes availability class but remains protected. No additional $105 is created.

If the account originally contained only $100, B cannot begin immediately. Even after settlement, automatically reusing protected proceeds requires an owner-approved reinvestment rule. The proposed default keeps completed proceeds protected; funding eventually runs out unless the owner permits a limited seed recycle or adds authorized capital. The UI must explain this trade-off rather than silently reinterpret protection.

## Policy versions and human authority

The default policy is a versioned document. A session snapshots it at creation; ordinary owner changes apply to future sessions. Emergency restrictions can immediately reduce permissions for existing sessions. Widening limits, raising capital or changing a running strategy's parameters requires an explicit owner action with an audit record. Agents can recommend such changes and attach evidence.

Automatic promotion chooses among preapproved strategy artifacts inside a fixed envelope. It does not edit the default policy or install generated production code. A candidate's provenance includes its artifact hash, approved parameter domain, data dependencies and expiry date. If any of these change beyond the envelope, it is a new approval object.

## Aggregate budgets and fairness

Maintain a global authorized-capital ceiling, daily loss trigger, cumulative loss allowance, concurrency limit and reserved operational cash. Session restarts do not reset any aggregate measure. A global halt supersedes every session. Allocation uses an atomic compare-and-reserve transaction so that two simultaneous requests cannot both claim the same $100.

The first implementation runs one active trading session per account. Later concurrency requires a portfolio allocator to resolve competing signals and shared inventory. Independent bots must not churn the same stock against each other or double-count diversification. Netting decisions need explicit attribution so that a strategy's backtest remains comparable with actual portfolio execution.

## Completion evidence

Persist the initial policy hash, seed, all economic events, realized and unrealized P&L histories, target observations, final fees, reconciliation report, protected amount and successor identifier. Display completion and settlement as distinct timestamps. Tests must prove one successor per successful session, no funding from receivables, no hidden budget resets, and conservation across protection, settlement and restart.

