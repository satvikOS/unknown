# Project Unknown technical book

Engineering edition 1.0. Research snapshot: October 22, 2026.

Read the chapters in order, or use the implementation and quantitative paths in the reading guide. The PDF adds a page-numbered contents list, bookmarks, subject index and embedded reference listings.

## Contents

- [Reading guide: Reading guide and evidence contract](00-reading-guide.md)
- [Chapter 1: Product contract and requirement traceability](01-product-contract.md)
- [Chapter 2: Market mechanics and the lifecycle of a trade](02-market-mechanics.md)
- [Chapter 3: Instruments, currencies, identifiers and time](03-instruments-and-time.md)
- [Chapter 4: Probability, estimation and calibrated uncertainty](04-probability-and-estimation.md)
- [Chapter 5: Time series, features and regime estimation](05-time-series-and-features.md)
- [Chapter 6: Portfolio construction and risk mathematics](06-portfolio-and-risk.md)
- [Chapter 7: Research design and backtesting](07-research-and-backtesting.md)
- [Chapter 8: Runtime architecture and authority boundaries](08-runtime-architecture.md)
- [Chapter 9: Ledger, reservations and settlement](09-ledger-and-settlement.md)
- [Chapter 10: Session orchestration and protected proceeds](10-sessions-and-capital.md)
- [Chapter 11: Data acquisition, provenance and analytics](11-data-platform.md)
- [Chapter 12: Broker adapters and account capabilities](12-broker-adapters.md)
- [Chapter 13: Order management and execution control](13-order-execution.md)
- [Chapter 14: Latency, concurrency and performance engineering](14-latency-and-concurrency.md)
- [Chapter 15: Trend, momentum and cross-sectional strategies](15-trend-and-cross-sectional-strategies.md)
- [Chapter 16: Mean reversion, pairs and statistical arbitrage](16-mean-reversion-and-pairs.md)
- [Chapter 17: Fundamental, event and macro research](17-fundamental-event-and-macro.md)
- [Chapter 18: Market making and optimal execution](18-market-making-and-optimal-execution.md)
- [Chapter 19: Cloud agents, learned intuition and model routing](19-agents-and-model-routing.md)
- [Chapter 20: Strategy promotion and bounded autonomy](20-promotion-and-governance.md)
- [Chapter 21: Multiasset mathematics and separate eligibility gates](21-multiasset-mathematics.md)
- [Chapter 22: Research terminal and operator experience](22-terminal-and-controls.md)
- [Chapter 23: Security, supply chain and data rights](23-security-and-rights.md)
- [Chapter 24: Mac development and AWS deployment](24-mac-and-aws-operations.md)
- [Chapter 25: Laws, broker rules and jurisdictional eligibility](25-law-and-account-eligibility.md)
- [Chapter 26: Observability, economics and incident runbooks](26-observability-and-runbooks.md)
- [Chapter 27: Building with Claude Code: sequence and acceptance](27-claude-code-build-sequence.md)
- [Chapter 28: Verification, properties and fault injection](28-verification-and-fault-injection.md)
- [Chapter 29: Institutional capabilities and roadmap](29-institutional-track-and-roadmap.md)
- [Appendix A: Contracts, worked examples and acceptance matrix](30-contracts-and-worked-examples.md)
- [Appendix B: Executable reference and code walkthrough](31-executable-companion.md)
- [Appendix C: Formula catalog, algorithms and glossary](32-formula-algorithm-and-term-index.md)
- [Appendix D: Primary-source register](33-primary-source-register.md)

## Build and verification

`python3 scripts/build_book.py` builds the PDF from Markdown and actual companion source files. Requires Pandoc, XeLaTeX, makeindex and Matplotlib. Output is generated under `output/` and is not committed.

`python3 -m unittest discover -s tests -v` runs the offline accounting tests. See `reference/README.md` for native compilation and exact limitations.

Status: architecture/specification plus offline reference. The live broker adapters, terminal, cloud-worker runtime and validated strategies remain implementation work.
