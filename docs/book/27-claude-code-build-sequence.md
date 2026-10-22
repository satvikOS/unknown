# Building with Claude Code: sequence and acceptance {#build-sequence}

The repository is the working specification. Read `CLAUDE.md`, the product contract, ledger/session chapters and contracts before changing implementation. Claude Code's project-memory mechanism can carry instructions, but it is contextual guidance rather than a security boundary. [S29] Enforce restrictions in credentials, service permissions and code as well.

## First fourteen days

| Window | Deliverable | Exit evidence |
|---|---|---|
| Days 1-2 | Repository, domain types, ledger, configuration | Conservation and duplicate-event tests |
| Days 3-4 | Replay engine, order lifecycle, stop/start | Crash, cancel-race and restart tests |
| Days 5-6 | Data adapter and instrument master | Causal timestamps and correction fixtures |
| Days 7-8 | IBKR paper adapter and reconciliation | Paper orders match broker state |
| Days 9-10 | Terminal overview, sessions, orders, risk | End-to-end paper workflow |
| Days 11-12 | One simple strategy and experiment registry | Baseline and cost-aware evaluation |
| Days 13-14 | Bounded cloud research jobs, recovery drills | Cost caps, injection tests, restore drill |

This is an ambitious sequencing target, not a guaranteed delivery date. Broker access, entitlements and integration failures can extend it. Do not compress evidence gates to meet the calendar. Advanced asset classes, direct-access HFT and full commercial-terminal data breadth are later tracks.

## Task template

Give each coding task a requirement ID, current files, desired behavior, invariants, allowed dependencies, tests and explicit non-goals. Ask the coding agent to inspect the existing implementation before editing. Require a small coherent change and a test report. Avoid a single instruction to build everything, which encourages invented APIs and hidden assumptions.

Example task: implement the paper broker adapter behind `BrokerPort`; support instrument resolution, account snapshots, price-bounded cash-equity orders, cancellation and execution ingestion; preserve stable IDs; never import live credentials; test timeout-after-acceptance and duplicate callbacks; return unsupported capabilities explicitly. This is concrete enough to review.

## Repository structure

`docs/book` contains the editable technical book. `contracts` contains interface/schema examples. `config` contains paper defaults and unset live gates. `reference` contains offline executable examples. `tests` checks those examples. `scripts` contains validation and book-building tools. Future production packages should be added only when their responsibilities and dependencies are clear.

Keep the reference and production implementation distinct. A demonstration's simplified fee finalization or synthetic broker is not a hidden production implementation. When replacing an example, preserve its behavioral tests and add real integration evidence. Document the change in the capability matrix.

## Development loop

Inspect the relevant requirement and source contract; write or update the smallest implementation; run meaningful focused tests; inspect failures; review monetary and authority boundaries; update documentation; commit a reviewable change. Use version-control diffs to detect unrelated edits. Never paste secrets into a model context to make integration easier.

Research-generated code runs in an isolated environment. It may produce a candidate artifact and tests, but cannot merge itself or deploy to production. The owner controls production source changes. Automatic selection among approved strategy artifacts uses the separate promotion policy from Chapter 20.

## Dependency choices

Use official broker SDKs where appropriate, standard numerical libraries for research and a typed frontend framework with a lockfile. Check current package versions and licenses at implementation time. Avoid inventing version numbers in advance. The small offline reference deliberately avoids network and third-party Python packages so its core behavior is inspectable.

## Completion definition

The first paper desk is complete when it can ingest eligible data, generate one approved strategy intent, reserve resources, submit to paper, reconcile fills, display state, stop durably, restart through preflight and reproduce its experiment. A screenshot, a model conversation or a positive simulated trade alone does not meet this definition.

Live activation is a separate owner-authorized milestone after account eligibility, capital, entitlements, security, reconciliation and strategy evidence are resolved. The book and companion do not authorize trades or paid infrastructure on the owner's behalf.

