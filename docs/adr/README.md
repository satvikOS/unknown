# Architecture decision records

Each record states a decision, the concrete reasons, the alternatives rejected and the evidence that would reopen it. A record changes only by a new record that supersedes it; the old text stays. A dated **Amendment** section at the end of a record may state how an accepted decision was implemented, or decide a case the record left open, without reversing it; the text above it stays unchanged, and the amendment says which earlier behaviour it replaces.

Status vocabulary: `proposed`, `accepted`, `superseded by ADR-NNNN`.

Evidence classes follow the book's reading guide: verified external fact (linked primary source), design decision (this project's choice), research hypothesis (needs measurement), deployment gate (an unset fact or permission).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-decisions-and-status-vocabulary.md) | Record decisions, evidence classes and implementation status | accepted |
| [0002](0002-process-topology-and-languages.md) | Process topology and language responsibilities | accepted |
| [0003](0003-storage-and-durability.md) | Storage engine and durability settings | accepted |
| [0004](0004-exact-numeric-representation.md) | Exact numeric representation for money, prices, quantities and rates | accepted |
| [0005](0005-event-sourced-core.md) | Event-sourced core with same-transaction projections | accepted |
| [0006](0006-ledger-owner-dimension.md) | Economic journal with an owner dimension; holds for reservations | accepted |
| [0007](0007-authority-and-policy-versions.md) | Principals, permissions, policy versions and mandate snapshots | accepted |
| [0008](0008-session-state-machine.md) | Session state machine, completion and successor semantics | accepted |
| [0009](0009-order-lifecycle-and-outbox.md) | Order lifecycle, outbox and unknown outcomes | accepted |
| [0010](0010-control-plane-stop-start-recovery.md) | Red stop, green preflight, recovery gate and resumption | accepted |
| [0011](0011-execution-ownership-and-split-brain.md) | Execution ownership and split-brain prevention | accepted |
| [0012](0012-broker-adapter-contract.md) | Broker adapter contract | accepted |
| [0013](0013-terminal-api-and-owner-authentication.md) | Terminal, API process and owner authentication | accepted |
| [0014](0014-ibkr-adapter-boundary-and-order-identity.md) | IBKR adapter boundary, SDK licence and order identity | accepted; licence decided by owner |
| [0015](0015-data-platform-point-in-time-store.md) | Data platform: provider registry, point-in-time store and lineage | accepted |
| [0016](0016-bounded-agents-and-model-providers.md) | Bounded agents, model-provider adapters and spend control | accepted |
| [0017](0017-strategy-artifacts-promotion-and-runtime.md) | Strategy artifacts, promotion gate and strategy runtime | proposed |
