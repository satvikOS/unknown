# Executable reference and code walkthrough {#executable-appendix}

The PDF embeds the repository files below directly, so its listings match the tested companion. Markdown readers can open the corresponding files in `reference`, `tests` and `contracts`. The build script expands the include markers.

## Running and interpreting the examples

Run `python3 -m reference.engine` and `python3 -m unittest discover -s tests -v` from the repository root. Compile the native example with `c++ -std=c++20 -O2 -Wall -Wextra -Werror -pedantic reference/risk_kernel.cpp -o risk_kernel`, then execute it. Python uses only the standard library. The document build separately needs Pandoc, TeX and Matplotlib.

No example connects to a broker, calls a model or spends money. Boolean owner, reconciliation, fee-finalization and settlement fields are synthetic assertions, not trusted production facts. The complete proposed configuration is broader than the reference implementation and is not automatically loaded by it.

`Engine.apply` begins a transaction, compares command identity and canonical payload, loads state, applies one domain transition, checks invariants, writes state/result/audit linkage and commits. Exceptions roll back. An identical retry returns the original result. This is local idempotence; a real broker is outside the transaction and needs the outbox/reconciliation protocol.

Buying consumes cash and adds inventory; selling creates a receivable and recognizes net P&L. Completion protects proceeds and requests one successor funded only from unallocated cash. The average-cost pool is not tax-lot accounting. Unexpected real broker events require exception ingestion rather than the simulator's synthetic-input rejection.

The stop is durable. Reopening the database requires fresh process reconciliation before orders. Risk-stopped sessions cannot resume through the ordinary start path. The reference omits automatic liquidation, a complete daily loss policy, multicurrency settlement, calendars, corporate actions, live adapters and production security. Its hash chain is diagnostic, not protection against a database administrator.

## Accounting engine

<!-- INCLUDE: reference/engine.py -->

## Native pretrade arithmetic

This independent function checks stop, freshness, input validity, integer overflow, notional and cash. It does not replace portfolio state, reservations or the complete risk service.

<!-- INCLUDE: reference/risk_kernel.cpp -->

## Accounting test suite

Nineteen scenarios cover command/execution duplicates, cash and inventory reservations, partial fills during cancellation, settlement, successor funding, stop persistence, reconciliation after restart, invalid-fill rollback, marked loss and randomized round-trip conservation. Eleven native assertions cover arithmetic/limit boundaries. These are offline results, not live execution or profitability evidence.

<!-- INCLUDE: tests/test_engine.py -->

## Adapter contract

The protocol is an interface specification, not a tested provider adapter.

<!-- INCLUDE: contracts/broker_port.py -->

## Proposed relational schema

Cross-posting journal balance, authority and external-command recovery still require service logic and tests.

<!-- INCLUDE: contracts/ledger_schema.sql -->
