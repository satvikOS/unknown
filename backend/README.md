# backend — Project Unknown core

Single-writer core service (ADR-0002): exact arithmetic, event-sourced store, journal with owner dimension,
policy and session state machines, order lifecycle with outbox, pretrade risk, red/green control,
broker port, deterministic simulated broker and reconciliation. Standard library only; Python 3.12+.

```bash
cd backend
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Everything under `tests/` uses deterministic fixtures (`SimulatedBroker`, `ManualClock`, fixture
instruments and quotes). Fixture results are not broker, market or profitability evidence.
