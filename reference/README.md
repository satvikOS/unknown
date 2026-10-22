# Offline reference limitations

Run `python3 -m reference.engine` from the repository root. Run tests with `python3 -m unittest discover -s tests -v`.

The Python example has integer USD cents, whole shares, one active session, synthetic price-bounded fills, fee envelopes, reservations, durable command deduplication, partial fills during cancellation, receivables, protected completion, automatic successor funding from unallocated cash, a persistent stop and marked-loss triggers. SQLite stores each command atomically with a state projection and audit hash chain.

The native example is an independent C++20 pretrade arithmetic function. Build with `c++ -std=c++20 -O2 -Wall -Wextra -Werror -pedantic reference/risk_kernel.cpp -o risk_kernel` and run it. It has no broker capability.

Neither example implements production authentication, real broker reconciliation, an outbox sender, calendars, corporate actions, fractional/multicurrency accounting, tax lots, late broker corrections, daily loss-budget resets, source freshness for Python orders, a complete loss policy, live adapters, cloud model calls or a UI. `owner=True`, `reconciled=True`, `fees_final=True` and `broker_confirmed=True` are simulation assertions, not trusted production facts. The full proposed defaults file is not loaded by the example.

Synthetic fills outside an expected envelope are rejected. A real adapter must record unexpected broker facts through exception accounting and stop new exposure instead. The hash chain is a diagnostic, not protection against an attacker with full database access. The reference holds full state in one JSON record for clarity; it is not a high-volume storage design.

No tests demonstrate profitability, zero risk, live compatibility or legal eligibility.
