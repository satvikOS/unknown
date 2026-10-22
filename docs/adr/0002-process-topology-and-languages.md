# ADR-0002: Process topology and language responsibilities

Status: accepted (2026-10-22)

## Context

Book Chapter 8 proposes C++20 for the eventual native execution kernel and the official broker SDK, Python for research and orchestration, and TypeScript for a browser terminal. It also requires one authority over money and orders, no duplicated business logic, and latency claims measured on target hardware rather than inferred from a language.

The first broker is IBKR, reached over the internet through TWS or IB Gateway. Order acknowledgments therefore involve a local socket hop to the gateway, the gateway's link to IBKR servers, and IBKR's routing. Those latencies are measured in milliseconds or more (to be measured; see `docs/benchmarks`). The book's reference artifacts already duplicate pretrade arithmetic: `reference/engine.py` and `reference/risk_kernel.cpp` each implement a cash/notional check.

## Decision

1. **One authoritative core service in Python (3.12+, standard library only)** owns the economic journal, policy and session state machines, the order lifecycle and outbox, pretrade risk, the control latch and reconciliation. It runs as one single-writer process per execution account (ADR-0011). It is the only component that can create a reservation or an outbound order command.
2. **Broker adapters run in-process behind the typed port** (ADR-0012), on their own I/O threads. They deliver observations to the core through a bounded queue, and the core applies every observation through its single writer. IBKR's official Python client `ibapi` is used; its version is pinned once verified against official IBKR downloads.
3. **The terminal is a TypeScript browser application** served by a separate API process. The API process holds no broker credentials and no database write handle. It forwards authenticated owner commands to the core and streams projections. It cannot form a second authority.
4. **Research, data and model workers are separate Python processes.** They hold no broker credentials or ledger write access, and they emit typed proposals and datasets only.
5. **C++20 is not on the initial order path.** It is reserved for (a) a measured hotspot whose removal has an economic benefit, and (b) the separate direct-access/HFT research track (book Chapter 14, "Real HFT boundary"). The book's `reference/risk_kernel.cpp` stays as reference material. It is not wired into the product, because a second implementation of pretrade rules would be duplicated business logic with two authorities.

## Reasons

- IBKR-mediated round trips are dominated by gateway and network time. A deterministic Python pretrade path that takes microseconds to low milliseconds, including the durable commit, does not bind the opportunity window of daily or intraday stock strategies routed through a retail gateway. This is a hypothesis until `docs/benchmarks` contains distributions measured on this Mac and on the AWS target. It is revisited if p99 core latency becomes a material share of measured broker acknowledgment latency.
- A standard-library core has no third-party runtime dependencies in the money path, which reduces supply-chain exposure (book Chapter 23).
- One language for core and adapters keeps a single implementation of each rule.

## Rejected alternatives

- C++ core with Python research: two languages on the money path, FFI complexity and slower iteration for one person, with no measured latency need.
- Microservices per domain (ledger, OMS, risk): splits one transaction across processes and needs distributed coordination over money. The book explicitly warns against this.
- Node/TypeScript core shared with the UI: weaker exact-decimal ecosystem, and it would couple UI release cadence to the money authority.

## Reopen when

Measured core p99 exceeds 10% of measured broker acknowledgment p50 on the target host; a direct-access venue is approved; or the single-process core cannot sustain measured event rates with headroom.
