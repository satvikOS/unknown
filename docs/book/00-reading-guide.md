# Reading guide and evidence contract {#reading-guide}

Project Unknown is a personal, multi-market financial desk with a research terminal, quantitative strategy evaluation, cloud AI workers, deterministic execution controls and session accounting. Stocks have first implementation priority. The long-term instrument model includes ETFs, options, futures, foreign exchange, bonds and crypto. Trading access depends on the owner's actual account, residence, broker and permissions; a universal software model does not create universal market access.

This book is written for implementation with Claude Code. Read the architecture and monetary invariants before implementing a strategy. Chapters then progress from market mechanics and probability to data engineering, algorithms, execution, AI, operations, law and a staged build. Appendices contain interfaces, schemas, numerical examples, a glossary, a source register and acceptance checks. The PDF contains a contents page and subject index. The Markdown chapters are the editable source of truth.

## How to interpret statements

Four evidence classes appear throughout the project. **Verified external fact** means a linked primary source supports a dated statement. **Design decision** means this project deliberately chooses a behavior; it does not assert that a named trading firm uses it. **Research hypothesis** means an algorithm merits testing, with no assumed profitability. **Deployment gate** means a necessary fact or permission has not yet been established. These distinctions prevent a plausible explanation from becoming an unearned production claim.

Examples use synthetic numbers and are explicitly educational. A reference implementation can demonstrate conservation of money or a state transition without demonstrating real execution quality. A passing test establishes the tested property under its assumptions. It does not establish that an account cannot lose money, that a provider always responds, or that a strategy will make a profit.

The requested upward performance is an objective, not a system invariant. Software can refuse an order without funding, but it cannot force a market to offer profitable opportunities. The separation between controllable behavior and uncertain outcomes is the central design principle. An idle strategy can be the correct behavior of an active system.

## Authority and autonomy

The owner changes operating parameters and production code. Agents may prepare research, propose changes and notify the owner. They may test new strategies. Automatic promotion is possible only inside an owner-approved promotion policy with a fixed evaluation protocol and eligible strategy classes. A generated executable, a new asset class, a wider risk limit or a changed operating parameter is not silently authorized by passing a profitability test. Chapter 20 specifies the distinction between selecting an approved candidate and rewriting the system.

The platform continuously monitors markets and maintains its state. A red stop latches a durable prohibition on new exposure and invokes a defined cancellation/reduction policy. A green start is a request to run preflight checks; it is not permission to skip them. Closed venues, exhausted budgets, unsettled funds, failed authentication and uncertain positions may prevent trading while the application remains alive.

## Completeness and unknowns

The book covers the end-to-end design for the stated product. It does not contain the private strategies or internal systems of HRT, Jane Street, Citadel or Bloomberg. Public methods, source-backed constraints and original engineering decisions are clearly distinguished. A source register is not a legal opinion or certification of standards compliance.

The following facts remain deliberately unset: live broker entity and account type, actual authorized capital, paid-service budgets, entitlements, residency at launch, live account identifiers and production credentials. They are deployment inputs, not reasons to delay simulation. The executable reference is offline and simulation-only; provider adapters are contracts and build tasks unless a chapter expressly identifies a tested implementation.

The book's scope is broader than a one-to-two-week release. The initial schedule aims for an integrated paper desk, with an honest capability matrix. Full terminal data coverage, institutional execution infrastructure and long-horizon evidence cannot be established merely by shipping screens quickly. Each feature advances through specified acceptance evidence.

## Working method

Keep a change log and decision record for architectural changes. Assign every requirement a stable identifier. Link code changes to requirements and tests. Store source dates, dependency versions, schema versions and dataset fingerprints. A change to instrument eligibility must be traceable to an authoritative rule or verified account capability, not a model's recollection.

Use two reading paths. For implementation, read Chapters 1, 8-14, 22-25 and the contracts before adding strategies. For quantitative research, read Chapters 2-7 and 15-21, then return to execution and legal constraints. Read the jurisdiction chapter before any live connection. The same restriction applies when relocating: an unchanged API key is not evidence of unchanged eligibility.

Every build task ends with an observable outcome, failure tests and a stop condition. If a result cannot be reproduced from its recorded inputs, it cannot be used as promotion evidence. If a ledger cannot be reconciled, further entries are disabled. If a dependency's license is unresolved, it cannot be treated as unrestricted project property.
