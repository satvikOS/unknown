"""Quote consolidation label (CAP-MKT-02, CAP-DATA-10; owner decision 2026-10-22): the owner's free IBKR feed is
real-time but non-consolidated (Cboe One + IEX). Such quotes may validate SIMULATED and PAPER orders and are
labelled in the decision record; a LIVE order requires a consolidated (national best bid/offer) quote."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.market.quotes import Quote  # noqa: E402
from unknown.policy.model import Eligibility  # noqa: E402
from unknown.risk.pretrade import PretradeInput, evaluate  # noqa: E402

F = Fixed.parse
T0 = 1_792_504_800_000_000_000
ELIG = Eligibility(asset_classes=("EQUITY",), venues=("XNAS",), order_types=("LIMIT",), time_in_force=("DAY",),
                   outside_regular_hours=False, allow_short=False, allow_margin=False, allow_derivatives=False,
                   allow_leveraged_or_inverse=False, allow_fractional=False, allow_fx_conversion=False,
                   allow_transfers=False)


def quote(consolidation: str) -> Quote:
    return Quote("INS-1", F("99.99"), F("100.01"), None, "USD", f"ibkr:live:{consolidation}", None, T0,
                 consolidation=consolidation)


def checks(mode: str, q: Quote) -> dict[str, dict]:
    inp = PretradeInput(
        now_ns=T0, side="BUY", order_type="LIMIT", time_in_force="DAY", quantity=F("1"), limit_price=F("100.00"),
        order_currency="USD", deadline_ns=T0 + 10**9, intent_policy_hash="h", account_mode=mode, stop_latched=False,
        resume_requires_owner=False, reconciled_this_epoch=True, ownership_held=True, blocking_exception_kinds=(),
        loss_budget_breached=None, session_state="RUNNING", mandate_policy_hash="h", strategy_permitted=True,
        instrument=None, eligibility=ELIG, quote=q, quote_max_age_ms=2000, price_collar_bps=100,
        required_cash=F("0"), available_cash=F("0"), unreserved_quantity=F("0"), order_notional=F("0"),
        max_order_notional=F("0"), position_notional_after=F("0"), session_equity=F("0"), max_position_bps=0,
        gross_exposure_after=F("0"), max_gross_exposure=F("0"), orders_last_minute=0, max_orders_per_minute=10,
        average_daily_volume=None, max_participation_bps_of_adv=0, require_liquidity_data=False)
    return {c["check"]: c for c in evaluate(inp).checks}


class QuoteConsolidationTests(unittest.TestCase):
    def test_unknown_is_the_default_and_values_are_closed(self):
        q = Quote("INS-1", F("1"), F("2"), None, "USD", "x", None, T0)
        self.assertEqual(q.consolidation, "unknown")
        self.assertEqual(q.to_json(T0)["consolidation"], "unknown")
        with self.assertRaises(ValueError):
            Quote("INS-1", F("1"), F("2"), None, "USD", "x", None, T0, consolidation="nbbo")

    def test_non_consolidated_quote_validates_paper_but_not_live(self):
        paper = checks("PAPER", quote("non_consolidated"))
        self.assertNotIn("QUOTE_CONSOLIDATED", paper)            # not a gate outside LIVE
        self.assertTrue(paper["QUOTE_NOT_DELAYED"]["passed"])
        live = checks("LIVE", quote("non_consolidated"))
        self.assertFalse(live["QUOTE_CONSOLIDATED"]["passed"])
        self.assertIn("non_consolidated", live["QUOTE_CONSOLIDATED"]["detail"])
        self.assertFalse(checks("LIVE", quote("unknown"))["QUOTE_CONSOLIDATED"]["passed"])
        self.assertTrue(checks("LIVE", quote("consolidated"))["QUOTE_CONSOLIDATED"]["passed"])
        self.assertFalse(live["MODE"]["passed"], "LIVE routing stays refused in this release regardless")


if __name__ == "__main__":
    unittest.main()
