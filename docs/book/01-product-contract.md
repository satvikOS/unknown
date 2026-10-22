# Product contract and requirement traceability {#product-contract}

The desk serves one owner. There are no customers, pooled funds, external account onboarding or investment-advice distribution in the initial scope. This removes unnecessary product complexity but does not remove market conduct, broker, tax, data-license or residency constraints. Cross-broker support means independent adapters and separately reconciled accounts, not instant movement of funds between them.

## Requirement register

| ID | Requirement | Authoritative component | Evidence |
|---|---|---|---|
| R01 | Markets, research and analytics terminal | Read models and UI | Feature matrix, source lineage, screen tests |
| R02 | Fast numerical trading decisions | Native strategy and risk runtime | Tail-latency benchmark under specified load |
| R03 | Rigorous quantitative foundations | Research laboratory | Reproducible experiments and untouched evaluation |
| R04 | Rules plus learned context | Feature/model registry | Ablation against rules-only baseline |
| R05 | Multiple data sources and brokers | Adapter registry | Capability and recovery conformance |
| R06 | Configurable sessions and rollover | Ledger and session coordinator | Funding conservation and rollover tests |
| R07 | Specialized AI responsibilities | Job scheduler and capability policy | Tool-access and deadline tests |
| R08 | Capital protection and automatic operation | Independent risk supervisor | Rejection, fault and reconciliation tests |
| R09 | Continuous operation | Watchdog and durable state | Restart, outage and restore exercises |
| R10 | Performance under pressure | Scenario laboratory | Held-out stress results |
| R11 | Lean maintainable application | Build system and architecture | Dependency inventory and performance budgets |
| R12 | Measurable intelligence | Evaluation and model router | Calibration, cost and task-quality scores |
| R13 | Human controls settings and code | Signed policy registry | Unauthorized mutation tests |
| R14 | Red stop and green start | Control service | Persistent latch and preflight evidence |
| R15 | US, India and Europe eligibility | Eligibility policy | Dated account and jurisdiction verification |

Use this register as a traceability graph. A requirement may map to several chapters and tests. A test without a requirement can be useful, but a requirement without evidence remains incomplete. Track statuses as proposed, specified, implemented, tested, paper-verified and live-verified. These are not interchangeable labels.

## Terminal parity as a measurable contract

Benchmark workflows rather than visual resemblance. A company-research workflow requires correct instrument mapping, filings, dated financial statements, period alignment, currency/unit normalization, comparable companies, estimates where licensed, source citations and export. A watchlist requires accurate quote status and timestamps, not merely a table of numbers. A portfolio view requires reconciled holdings, prices, cash and corporate actions.

For each reference workflow, record its inputs, outputs, latency expectation, coverage, data rights, failure states and test dataset. Mark unavailable datasets as unavailable. Do not silently replace a licensed real-time feed with a delayed public quote. Do not infer proprietary financial estimates from incomplete filings while labeling them consensus estimates.

Functional parity and coverage parity are separate columns. The application can implement a ratio calculator before it licenses a complete global fundamentals dataset. It can implement order entry before supporting every broker order type. The UI must expose these distinctions, especially when a screen mixes historical, delayed and live information.

## Default operating profile

The repository defaults to simulation. Paid calls and live routing are disabled. Session policy snapshots are immutable after a session starts. Changes apply to future sessions, except a more restrictive emergency control can stop existing activity immediately. A new session never clears account-level loss history, fees, pending orders or a stop latch.

For initial live planning, the proposed eligible universe is fully paid long-only cash equities and unleveraged ETFs. Other asset classes are researched and modeled in the book but require explicit capability and risk releases. This is a release order, not a removal of the eventual scope. A one-dollar session can be accepted as a configuration while having zero eligible orders after minimum quantities and fees are evaluated.

## Unknown deployment inputs

Create a deployment record with `owner_residency`, `broker_entity`, `account_type`, `base_currency`, `permissions`, `market_data_entitlements`, `authorized_capital`, `operating_budget`, `policy_version`, `verified_at` and `evidence_reference`. Unknown fields are not interpreted as permissive defaults. An account identifier must not be placed in the public repository.

The architecture permits a Mac development environment and a Linux production host on AWS. Production operations require a tested gateway, verified network path, durable storage and a restore procedure. A laptop or desktop can run paper trading, but a sleeping workstation is not a continuously available execution host.

## Acceptance contract

A first integrated release must ingest a deterministic market replay, generate at least one transparent numerical signal, reject an unfunded order, simulate a partial fill, reconcile a cancellation race, close a session, delay reuse of unsettled proceeds, create a separately funded successor, survive restart and preserve the red stop. Each operation must be visible in the terminal and reconstructible from the event history.

The release can be successful even if the test strategy loses money. That release establishes plumbing and controls. Strategy viability is a separate research gate. Conversely, a profitable simulation that double counts cash fails the product contract.
