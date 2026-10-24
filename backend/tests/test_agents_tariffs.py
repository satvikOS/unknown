"""Effective-dated tariffs, band rules and exact cost (CAP-MODEL-05, ADR-0016 section 4)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import book, tariff_row  # noqa: E402
from unknown.agents.common import CONFIG_MODELS  # noqa: E402
from unknown.agents.tariffs import (BandUnverified, NoTariff, TariffBook, TariffError, TariffIncomplete,  # noqa: E402
                                    TariffNotApproved, Usage, parse_row, per_million)
from unknown.domain.clock import utc_ns  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

AT = utc_ns(2026, 10, 23, 12, 0, 0)


class ShippedTariffFileTest(unittest.TestCase):
    def setUp(self):
        self.book = TariffBook.load()

    def test_only_the_owner_authorized_row_is_approved(self):
        """The owner authorized USD 200 of Anthropic spend on 2026-10-22; only claude-opus-5-5 is approved.
        Every other row stays unapproved, so worst_case() refuses it."""
        self.assertTrue(self.book.rows)
        approved = [(r.provider, r.model_id) for r in self.book.rows if r.approved_by is not None]
        self.assertEqual(approved, [("anthropic", "claude-opus-5-5")])
        self.assertTrue(self.book.worst_case("anthropic", "claude-opus-5-5", at_ns=AT, prompt_tokens=10,
                                             max_output_tokens=10).amount.is_positive())
        with self.assertRaises(TariffNotApproved):
            self.book.worst_case("anthropic", "claude-sonnet-5-5", at_ns=AT, prompt_tokens=10, max_output_tokens=10)
        self.assertFalse(self.book.is_routable("xai", "grok-4.7", at_ns=AT))

    def test_rates_are_decimal_strings_never_json_numbers(self):
        doc = json.loads((CONFIG_MODELS / "tariffs.json").read_text())
        for row in doc["rows"]:
            for v in row["per_million"].values():
                self.assertTrue(v is None or isinstance(v, str))

    def test_grok_200k_all_tokens_rule(self):
        below = Usage("xai", "grok-4.7", input_tokens=199_999, output_tokens=1_000)
        self.assertEqual(self.book.cost(below, at_ns=AT).amount,
                         per_million(199_999, Fixed.parse("2.00")) + per_million(1_000, Fixed.parse("6.00")))
        above = Usage("xai", "grok-4.7", input_tokens=200_000, cached_read_tokens=50_000, output_tokens=1_000)
        # 250,000 prompt tokens: every token class billed at the upper band (CAP-MODEL-05 acceptance)
        self.assertEqual(self.book.cost(above, at_ns=AT).amount, Fixed.parse("0.80") + Fixed.parse("0.05") + Fixed.parse("0.012"))
        self.assertEqual(str(self.book.cost(above, at_ns=AT)), "0.86200000 USD")

    def test_grok_threshold_reading_of_k_is_refused_not_guessed(self):
        for tokens in (200_000, 204_799):
            with self.subTest(tokens=tokens), self.assertRaises(BandUnverified):
                self.book.cost(Usage("xai", "grok-4.7", input_tokens=tokens), at_ns=AT)
        self.book.cost(Usage("xai", "grok-4.7", input_tokens=204_800), at_ns=AT)

    def test_openai_long_context_is_refused(self):
        short = Usage("openai", "gpt-6.1-sol", input_tokens=272_000, output_tokens=100)
        self.assertEqual(self.book.cost(short, at_ns=AT).amount, Fixed.parse("0.544") + Fixed.parse("0.001"))
        with self.assertRaises(BandUnverified):
            self.book.cost(Usage("openai", "gpt-6.1-sol", input_tokens=272_001), at_ns=AT)

    def test_unrecorded_rates_refuse(self):
        with self.assertRaises(TariffIncomplete):  # luna cached rate not recorded
            self.book.cost(Usage("openai", "gpt-6-luna", input_tokens=10, cached_read_tokens=5), at_ns=AT)
        with self.assertRaises(TariffIncomplete):  # sonnet cached rate: sources conflict
            self.book.cost(Usage("anthropic", "claude-sonnet-5-5", input_tokens=10, cached_read_tokens=5), at_ns=AT)
        with self.assertRaises(TariffIncomplete):  # cache-write multipliers recorded as unverified
            self.book.cost(Usage("anthropic", "claude-haiku-5-5", input_tokens=10, cache_write_tokens={"5m": 5}), at_ns=AT)
        with self.assertRaises(NoTariff):
            self.book.cost(Usage("anthropic", "claude-opus-5-5", input_tokens=10, region="us"), at_ns=AT)
        with self.assertRaises(NoTariff):
            self.book.cost(Usage("xai", "grok-4.7", input_tokens=10), at_ns=utc_ns(2026, 10, 21, 0, 0, 0))

    def test_opus_cache_writes_are_verified_for_the_analysts_prompt_cache(self):
        # 5-minute writes at 1.25x and 1-hour writes at 2x of the 4.00 input rate (re-checked 2026-10-24)
        cost = self.book.cost(Usage("anthropic", "claude-opus-5-5", input_tokens=10, cache_write_tokens={"5m": 1_000_000}), at_ns=AT)
        self.assertEqual(cost.amount, per_million(10, Fixed.parse("4.00")) + Fixed.parse("5.00"))
        hour = self.book.cost(Usage("anthropic", "claude-opus-5-5", cache_write_tokens={"1h": 1_000_000}), at_ns=AT)
        self.assertEqual(hour.amount, Fixed.parse("8.00"))

    def test_haiku_band_counts_cache_reads(self):
        lower = self.book.cost(Usage("anthropic", "claude-haiku-5-5", input_tokens=100_000, output_tokens=1000), at_ns=AT)
        self.assertEqual(lower.amount, Fixed.parse("0.0100") + Fixed.parse("0.0005"))
        upper = self.book.cost(Usage("anthropic", "claude-haiku-5-5", input_tokens=100_001, output_tokens=1000), at_ns=AT)
        self.assertEqual(upper.amount, per_million(100_001, Fixed.parse("0.50")) + Fixed.parse("0.0025"))


class TariffMathTest(unittest.TestCase):
    def test_book_equation_19_1(self):
        """N=1000 jobs, I=2000, O=200: $6.00/day at (2, 10) and $5.20/day at (2, 6)."""
        for out, expected in (("10", "6.00"), ("6", "5.20")):
            b = book(tariff_row(provider="openai", model="fixture-model", inp="2", out=out, cached=None))
            per_call = b.cost(Usage("openai", "fixture-model", input_tokens=2000, output_tokens=200), at_ns=AT)
            self.assertEqual(per_call.amount * 1000, Fixed.parse(expected))

    def test_effective_dating(self):
        b = book(tariff_row(provider="gemini", model="fixture-argon", inp="2.00", out="12.00", frm="2026-10-01", until="2026-12-01"),
                 tariff_row(provider="gemini", model="fixture-argon", inp="4.00", out="12.00", frm="2026-12-01"))
        u = Usage("gemini", "fixture-argon", input_tokens=1_000_000)
        self.assertEqual(b.cost(u, at_ns=utc_ns(2026, 11, 30, 23, 59, 59)).amount, Fixed.parse("2.00"))
        self.assertEqual(b.cost(u, at_ns=utc_ns(2026, 12, 1, 0, 0, 0)).amount, Fixed.parse("4.00"))

    def test_overlapping_rows_are_refused(self):
        with self.assertRaises(TariffError):
            book(tariff_row(frm="2026-10-01"), tariff_row(frm="2026-11-01"))
        book(tariff_row(lo=0, hi=100), tariff_row(lo=101, hi=None, inp="5", out="25"))

    def test_rows_reject_floats_and_incomplete_verified_bands(self):
        bad = tariff_row()
        bad["per_million"]["input"] = 4.0
        with self.assertRaises(TariffError):
            parse_row(bad, 0)
        bad = tariff_row()
        bad["per_million"]["output"] = None
        with self.assertRaises(TariffError):
            parse_row(bad, 0)

    def test_worst_case_bounds_cost(self):
        b = book(tariff_row(provider="openai", model="fx", inp="2.00", out="10.00", cached="0.10",
                            cache_write={"multipliers": {"30m": "1.25", "12h": "2.5"}, "status": "unverified"}, hi=1000),
                 tariff_row(provider="openai", model="fx", status="unverified", lo=1001))
        wc = b.worst_case("openai", "fx", at_ns=AT, prompt_tokens=1000, max_output_tokens=500)
        self.assertEqual(wc.amount, per_million(1000, Fixed.parse("2.00")) + per_million(500, Fixed.parse("10.00")))
        wc_cache = b.worst_case("openai", "fx", at_ns=AT, prompt_tokens=1000, max_output_tokens=500, cache_write_ttls=("12h",))
        self.assertEqual(wc_cache.amount, per_million(1000, Fixed.parse("5.000")) + per_million(500, Fixed.parse("10.00")))
        with self.assertRaises(TariffIncomplete):
            b.worst_case("openai", "fx", at_ns=AT, prompt_tokens=10, max_output_tokens=5, cache_write_ttls=("1h",))
        with self.assertRaises(BandUnverified):
            b.worst_case("openai", "fx", at_ns=AT, prompt_tokens=1001, max_output_tokens=5)
        unapproved = book(tariff_row(approved=None))
        with self.assertRaises(TariffNotApproved):
            unapproved.worst_case("anthropic", "claude-opus-5-5", at_ns=AT, prompt_tokens=1, max_output_tokens=1)
        # an actual usage at or below the reservation inputs never costs more than the worst case
        two_band = book(tariff_row(provider="xai", model="fx", inp="2", out="6", cached="0.5", hi=199_999),
                        tariff_row(provider="xai", model="fx", inp="4", out="12", cached="1", lo=200_000))
        wc = two_band.worst_case("xai", "fx", at_ns=AT, prompt_tokens=250_000, max_output_tokens=1000)
        for inp, cached, out in ((150_000, 0, 1000), (200_000, 50_000, 1000), (250_000, 0, 1000)):
            cost = two_band.cost(Usage("xai", "fx", input_tokens=inp, cached_read_tokens=cached, output_tokens=out), at_ns=AT)
            self.assertLessEqual(cost.amount, wc.amount)


if __name__ == "__main__":
    unittest.main()
