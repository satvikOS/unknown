"""IBKR Pro Tiered US fee schedule (config/fees; CAP-OMS-11, CAP-RES-05). Prices and quantities are TEST FIXTURES;
the rates are the schedule file's, transcribed from IBKR's pricing pages on 2026-10-23."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from unknown.domain.fixed import Fixed, Rounding
from unknown.domain.money import Money
from unknown.policy.model import Policy
from unknown.research.costs import TIERS, IbkrTieredUS
from unknown.research.sizing import affordable
from unknown.risk import fees as core_fees
from unknown.risk.fee_schedule import IBKR_PRO_TIERED_US, FeeScheduleError, load_schedule, parse_schedule

F = Fixed.parse
REPO = Path(__file__).resolve().parents[2]
PAPER_POLICY = REPO / "config" / "policies" / "paper-default.v1.json"
NOV = date(2026, 11, 2)          # inside the FINRA TAF fee holiday the page states
JAN = date(2027, 1, 4)           # after it: the page states no rate


class TieredMinimumTests(unittest.TestCase):
    def setUp(self):
        self.s = load_schedule()

    def test_a_100_and_a_300_buy_are_charged_the_per_order_minimum(self):
        for qty, price in (("4", "25.00"), ("1", "100.00"), ("50", "2.00"),      # $100
                           ("12", "25.00"), ("1", "300.00"), ("100", "3.00")):  # $300 (100 x 0.0035 = 0.35 exactly)
            with self.subTest(qty=qty, price=price):
                self.assertEqual(self.s.commission(F(qty), F(price)), F("0.35"))
                self.assertEqual(IbkrTieredUS().commission(F(qty), F(price)), F("0.35"))   # research model agrees

    def test_all_in_charge_adds_the_verified_third_party_fees(self):
        c = self.s.charge("BUY", F("4"), F("25.00"), NOV)
        self.assertEqual(c.commission, F("0.35"))
        self.assertEqual({k: v.to_str() for k, v in c.third_party.items()},
                         {"exchange_remove_liquidity": "0.012000", "clearing_nscc_dtc": "0.000800",
                          "nyse_pass_through": "0.000062", "finra_pass_through": "0.000196", "finra_cat": "0.000012"})
        self.assertEqual(c.total, F("0.363070"))
        self.assertEqual(c.unverified, ())
        self.assertEqual(self.s.charge("BUY", F("12"), F("25.00"), NOV).total, F("0.388694"))

    def test_sells_pay_sec_fee_and_taf_only_inside_the_stated_window(self):
        nov = self.s.charge("SELL", F("4"), F("25.00"), NOV)
        self.assertEqual(nov.third_party["sec_transaction_fee"], F("0.002060"))
        self.assertEqual(nov.third_party["finra_taf"], F("0"))
        self.assertEqual(nov.unverified, ())
        jan = self.s.charge("SELL", F("4"), F("25.00"), JAN)
        self.assertEqual(jan.third_party["finra_taf"], F("0.000800"))     # 4 x 0.000200 bound, flagged
        self.assertEqual(jan.unverified, ("finra_taf",))
        self.assertNotIn("sec_transaction_fee", self.s.charge("BUY", F("4"), F("25.00"), NOV).third_party)

    def test_tiers_are_marginal_and_the_cap_wins_below_the_minimum(self):
        self.assertEqual(self.s.base_commission(F("500000")), F("1050") + F("400"))        # page example
        self.assertEqual(self.s.commission(F("10"), F("0.20")), F("0.02"))                 # page: cap < minimum

    def test_research_constants_match_the_schedule_file(self):
        self.assertEqual(tuple((u, r) for u, r in TIERS), self.s.tiers)
        self.assertEqual(IbkrTieredUS().minimum, self.s.minimum)
        self.assertEqual(IbkrTieredUS().max_fraction_of_value, self.s.max_fraction_of_value)


class PretradeBoundTests(unittest.TestCase):
    def setUp(self):
        self.s = load_schedule()
        self.policy = Policy.parse(json.loads(PAPER_POLICY.read_text()))

    def bound(self, side, q, limit, day=NOV):
        return self.s.pretrade_bound(side, q, limit, day)

    def test_bound_is_never_below_a_charge(self):
        for qty, price in (("1", "300.00"), ("4", "25.00"), ("200", "0.50"), ("100", "1.00"), ("400000", "5.00")):
            for month in (0, 299_999, 2_000_000):
                with self.subTest(qty=qty, price=price, month=month):
                    q, p = F(qty), F(price)
                    self.assertGreaterEqual(self.bound("BUY", q, p), self.s.charge("BUY", q, p, NOV, month).total)
                    self.assertGreaterEqual(self.bound("SELL", q, p, JAN), self.s.charge("SELL", q, p, JAN, month).total)

    def test_sizing_nets_the_minimum(self):
        env = lambda side, q, limit: self.bound(side, q, limit)          # noqa: E731
        for budget, limit, want, expect in (("100.00", "25.00", "4", "3"), ("300.00", "25.00", "12", "11"),
                                            ("300.00", "300.00", "1", "0")):
            with self.subTest(budget=budget, limit=limit):
                q = affordable(F(budget), F(limit), F(want), env, 6)
                self.assertEqual(q, F(expect))
                if q.is_positive():
                    spent = q * F(limit) + self.s.charge("BUY", q, F(limit), NOV).total
                    self.assertLessEqual(spent, F(budget))
                    self.assertGreater((q + 1) * F(limit) + self.bound("BUY", q + 1, F(limit)), F(budget))

    def test_the_policy_envelope_covers_tiered_within_the_policy_order_limit(self):
        limit = self.policy.limits.max_order_notional.amount
        for price in ("0.10", "0.25", "0.50", "0.55", "0.99", "1.00", "2.50", "10.00", "25.00", "100.00"):
            p = F(price)
            for n in range(1, int(limit.div(p, 0, Rounding.FLOOR).mantissa) + 1):
                q = Fixed(n)
                reserved = core_fees.envelope(self.policy.fees, q, Money("USD", (q * p).rescale(6))).amount
                self.assertGreaterEqual(reserved, self.bound("BUY", q, p), f"{n} @ {price}")

    def test_above_the_order_limit_the_policy_envelope_is_not_a_bound(self):
        # 300 shares of a USD 1.00 stock (USD 300): Tiered worst case 2.011672 > the policy's 1.80 reservation.
        q, p = F("300"), F("1.00")
        reserved = core_fees.envelope(self.policy.fees, q, Money("USD", F("300.000000"))).amount
        self.assertEqual(reserved, F("1.80"))
        self.assertEqual(self.bound("BUY", q, p), F("2.011672"))


class ScheduleFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.body = json.loads(IBKR_PRO_TIERED_US.read_text())

    def refused(self, mutate, message):
        b = copy.deepcopy(self.body)
        mutate(b)
        with self.assertRaisesRegex(FeeScheduleError, message):
            parse_schedule(b)

    def test_every_verified_value_cites_a_recorded_source_and_quotes_it(self):
        self.refused(lambda b: b["commission"].update(quote=""), "quote the page")
        self.refused(lambda b: b["third_party"][0].update(sources=["NOT-RECORDED"]), "recorded source")
        self.refused(lambda b: b["third_party"][1].update(status="assumed"), "verified or unverified")

    def test_the_upper_bound_can_never_be_marked_verified(self):
        self.refused(lambda b: b["upper_bound"].update(status="verified"), "must be marked unverified")

    def test_tiers_and_rates_are_validated(self):
        self.refused(lambda b: b["commission"]["tiers"].pop(), "open-ended")
        self.refused(lambda b: b["third_party"][0].update(rate="-0.001"), "negative")
        self.refused(lambda b: b["sources"]["IBKR-STK-COMMISSIONS"].update(url="http://example.com"), "https")

    def test_file_records_retrieval_dates_and_the_route_rule(self):
        for s in self.body["sources"].values():
            self.assertLessEqual(date.fromisoformat(s["retrieved_on"]), date.today())
        self.assertEqual(self.body["route"], "SMART")
        self.assertIn("directed API orders cannot use the Tiered", self.body["route_note"])


if __name__ == "__main__":
    unittest.main()
