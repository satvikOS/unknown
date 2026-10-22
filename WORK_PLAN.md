# Authoring and validation plan

1. Verify public primary sources for brokers, jurisdictions, market mechanics, APIs and model costs.
2. Write a numbered technical book with requirement, equation, algorithm, glossary and subject indexes.
3. Add a simulation-only reference implementation, interface contracts, schemas, policy defaults and a Claude Code build sequence.
4. Verify accounting, order lifecycle and conservative risk invariants with meaningful tests; do not make live brokerage or paid model calls.
5. Render an indexed PDF, inspect layout and verify cross references.
6. Commit the source package to a review branch in satvikOS/unknown and provide the PDF separately.

Confirmed scope: one owner; stocks first; US, India and Europe; future multi-asset and multi-broker support; Mac development and AWS availability; cloud models; human-controlled settings and production code; evidence-gated promotion; persistent research and monitoring; session rollover requires available unallocated funds; a persistent red stop and validated green start.

Unresolved deployment facts are explicit gates, not invented answers: broker entity/account type, actual capital, permissions, spend ceilings, exchange entitlements and residency at launch. Paper mode permits useful implementation without those facts.
