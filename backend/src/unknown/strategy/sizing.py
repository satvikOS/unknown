"""Algorithm A15 at runtime: target weights -> price-bounded limit intents (book ch. 15; ADR-0017 #4; BR-DAT-04).

The mapping is ``research.sizing.plan_orders``, the SAME function the backtest engine's ``_Run.plan`` calls, so a
runtime decision and a backtest decision with the same inputs produce the same orders by construction (the parity
tests in ``tests/test_strategy_runner.py`` and ``tests/test_strategy_risk_sizing.py`` also check it on one state).
See that module for the steps, including the risk term: the loss at the spec's adverse move plus the entry and exit
fees, computed with ``risk.fees.envelope`` (the core's reservation function) under the active policy's fee model,
must fit the spec's risk budget; when it cannot, the buy is sized to zero.

What the engine takes from its own simulated ledger, the runtime takes from the core (read-only): owned and
unreserved quantity, pending orders of the session, the session's available cash (``ledger.available_cash``,
which already nets reservations and payables) as the budget, and the active policy's fee model and fee envelope,
so the estimate matches the core's own reservation check. The core's pretrade checks remain the authority."""
from __future__ import annotations

from typing import Iterable

from ..domain.fixed import Fixed
from ..domain.instruments import Instrument
from ..research.engine import Sizing
from ..research.sizing import (FeeEnvelope, Holding, InstrumentPlan, OrderPlan, RiskTerm, affordable,  # noqa: F401
                               plan_orders)

__all__ = ["FeeEnvelope", "Holding", "InstrumentPlan", "OrderPlan", "RiskTerm", "plan"]


def plan(targets: dict[str, float], *, sizing: Sizing, limit_offset_bps: int, equity: Fixed, marks: dict[str, Fixed | None],
         holdings: dict[str, Holding], instruments: dict[str, Instrument], budget: Fixed, fee_envelope: FeeEnvelope,
         adv: dict[str, Fixed | None], ledger_scale: int, tradable: Iterable[str] | None = None,
         risk: RiskTerm | None = None) -> tuple[dict[str, InstrumentPlan], dict]:
    """``research.sizing.plan_orders`` with the runtime's inputs. ``tradable`` (default: every target) names the
    instruments that may get an order at this decision; the weights are always capped and scaled over every target,
    so an instrument the runtime may not trade now still holds its share of the aggregate cap (BR-DAT-15)."""
    return plan_orders(targets, sizing=sizing, limit_offset_bps=limit_offset_bps, equity=equity, marks=marks,
                       holdings=holdings, instruments=instruments, budget=budget, fee_envelope=fee_envelope, adv=adv,
                       ledger_scale=ledger_scale, risk=risk, tradable=tradable)
