# Institutional capability track and long-term roadmap {#roadmap}

HRT, Jane Street and Citadel publish descriptions of quantitative research, machine learning and engineering work, but their private signals, inventories, execution systems and decision rules are not publicly reproducible in full. [S37, S38, S39] The useful goal is to adopt rigorous research, reliable infrastructure and explicit risk control, while measuring the capabilities this project actually achieves.

## Capability parity matrix

| Capability | Personal-desk path | Additional institutional requirements |
|---|---|---|
| Search and analytics | Licensed sources, provenance, formulas | Broader datasets and entitlements |
| Quant research | Reproducible experiments and baselines | Scale, specialists, proprietary data |
| Execution | Broker adapters and measured quality | Direct access, venue-specific infrastructure |
| Risk | Account controls and reconciliation | Firm-wide capital/clearing infrastructure |
| HFT | Separate research and replay track | Colocation, feeds, access, operations |
| AI assistance | Bounded cloud workers and evaluation | Larger corpora and governance capacity |

Bloomberg, LSEG and Capital IQ are also data, licensing, analytics and distribution businesses. Reproducing a screen layout does not reproduce their datasets, agreements or historical coverage. Build a feature-by-feature coverage matrix with source rights, quality and cost. Mark absent capabilities honestly.

## Advanced low-latency track

Before pursuing direct-access HFT, establish a strategy whose opportunity lifetime and expected edge justify it. Then identify venue access, feed protocols, hardware timestamping, sequence recovery, clock synchronization, exchange certification where required and operational staffing. Evaluate FPGA or specialized network acceleration only after software profiling and economic analysis show a need.

Queue position, adverse selection and inventory financing can matter more than a faster arithmetic function. Measure opportunity capture and net execution outcomes, not only nanoseconds. A strategy with no robust edge becomes an unprofitable strategy executed faster.

## Capacity and scaling

Scale capital only after capacity analysis, liquidity stress and owner authorization. Market impact is nonlinear; doubling capital does not necessarily double profit. More agents can increase cost, correlated mistakes and evaluation burden. Add agents when a distinct job and measurable benefit justify them, not to imitate a large organization chart.

As datasets grow, separate immutable research storage from transactional state. As accounts grow, formalize portfolio allocation and reconciliation ownership. As deployment complexity grows, introduce stronger workflow infrastructure and redundancy. Preserve simple interfaces so that each addition has a clear reason.

## Remaining deployment inputs

The owner must still supply actual broker entities/account types, live authorized capital, monthly data/model/cloud budgets, entitlements and residence-related confirmations. These inputs are intentionally unset. The book provides defaults for paper behavior and a place to record live decisions without guessing private facts.

Before changing country or broker entity, review account eligibility and funding rules. Before adding derivatives, complete their distinct risk and settlement implementation. Before enabling a new model provider, verify its current API, price, data terms and evaluation results. The roadmap is governed by evidence and permissions, not by the desire to keep every component busy.

## What this edition delivers

This edition provides an indexed engineering specification, public mathematical foundations, workflow contracts, conservative proposed policies, an offline accounting/session reference, a small native risk example and a staged implementation guide. It does not deliver a finished institutional terminal, a live broker adapter, proprietary firm strategies or a validated profitable system. The repository's capability status must remain consistent with that distinction as implementation progresses.

The next concrete milestone is an integrated paper desk that survives adverse event sequences and explains its state. From there, expand only when the next capability has a clear hypothesis, source rights, implementation contract and acceptance evidence.

