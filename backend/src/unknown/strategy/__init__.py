"""Strategy runtime (ADR-0017 #1, #4, #5; book ch. 15 algorithm A15, ch. 20).

A strategy runner is a separate research-side process that turns point-in-time data into typed intents for
ONE registered artifact. It never holds broker credentials and never writes to the core database: it reads the
research registry, the point-in-time store and the core database read-only, and writes to the core only through
the command socket with an owner-issued token bound to its artifact id.

Modules
-------
artifacts  ADR-0017 artifact id derived from the research registry's artifact hash; status/mode eligibility
sizing     A15 mapping from target weights to limit intents (same steps as the backtest engine)
coreview   read-only snapshot of the session's exposure, cash and mandate from the core database
journal    write-ahead decision journal with deterministic command and order ids
runner     the run cycle (``run_once``) and the scheduled loop (``run_loop``)

Run it with ``python -m unknown.strategy`` (see docs/strategy/README.md). These are research candidates run
under the owner's mandate in SIMULATED or PAPER accounts; nothing here is evidence of profitability."""
