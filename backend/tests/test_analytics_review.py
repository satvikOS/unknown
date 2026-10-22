"""Regression tests for the adversarial review of the portfolio-analytics domain (CAP-PORT-01..13).

One test (or test class) per confirmed defect; each docstring states the defect it pins. Ledger states come
from the real core through ``support.Harness``; every price, quantity and amount is a TEST FIXTURE."""
from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
import types
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import (DAY, F, SEC, SECOND_INSTRUMENT, booted, buy, create_session, db_hash, eod,  # noqa: E402
                                     quotes, register_second, ro, snapshot_store)
from support import ACCOUNT, DEPLOYMENT, T0  # noqa: E402

from unknown.analytics import snapshots  # noqa: E402
from unknown.analytics.allocation import inverse_volatility, mean_variance, propose  # noqa: E402
from unknown.analytics.common import SYSTEM, parse_scope  # noqa: E402
from unknown.analytics.exposure import SicClassifier  # noqa: E402
from unknown.analytics.holdings import holdings, load_book  # noqa: E402
from unknown.analytics.marks import normalize_quotes  # noqa: E402
from unknown.analytics.performance import at_least_one_year, irr, performance_report, risk_statistics  # noqa: E402
from unknown.analytics.snapshots import ValuationPoint, build_valuation, record_eod  # noqa: E402
from unknown.analytics.stress import overnight_gap  # noqa: E402
from unknown.analytics.var import backtest, historical_var, history_as_of, mark_table, research_price_history, \
    returns_from_prices  # noqa: E402
from unknown.analytics.whatif import Sandbox, dry_run  # noqa: E402
from unknown.data.pit import Observation, ResearchStore  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402


def _policy_hash(h, sid: str = "S1") -> str:
    sess = h.row("sessions", "session_id", sid)
    return json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])[
        "policy_hash"]


def _point(run: str, d: str, nav: str, cum: str = "0") -> ValuationPoint:
    return ValuationPoint(run, d, 0, 0, "USD", F(nav), F(cum), F("0"), "complete", False)


# ---------------------------------------------------------------------------------------------- performance
class IrrUniqueRootTest(unittest.TestCase):
    """Defect: a root exactly on the IRR search grid (r = 0, a period with no gain or loss) was counted as two
    brackets, so every flat period reported 'not unique' instead of a money-weighted return of 0."""

    def test_flat_period_has_a_zero_money_weighted_return(self):
        r = irr(F("1000"), F("1500"), [(10, F("500"))], 400)
        self.assertEqual((r["irr_annualized"], r["period_mwr"], r["reason"]), (0.0, 0.0, None))
        r = irr(F("1000"), F("1000"), [], 30)
        self.assertEqual(r["period_mwr"], 0.0)
        self.assertIn("shorter than one year", r["reason"])

    def test_cash_only_report_over_a_year(self):
        pts = [_point("a", "2026-01-02", "100"), _point("b", "2026-06-01", "150", "50"), _point("c", "2027-01-04", "150", "50")]
        mwr = performance_report(pts, scope="system", currency_code="USD", as_of_ns=0)["money_weighted"]
        self.assertEqual(mwr["irr_annualized"], 0.0)

    def test_two_genuine_roots_are_still_refused(self):
        # -100 now, +230 after one year, -132 after two years: roots at 10% and 20%.
        r = irr(F("100"), F("-132"), [(365, F("-230"))], 730)
        self.assertIsNone(r["irr_annualized"])
        self.assertIn("not unique", r["reason"])


class CalendarYearAnnualizationTest(unittest.TestCase):
    """Defect: 'at least one year' was tested as days >= 365, so 2028-01-01 -> 2028-12-31 (365 days, one day short of a
    calendar year in a leap year) was annualized, which GIPS 2.A.12 forbids."""

    def test_leap_year_span_of_365_days_is_not_annualized(self):
        self.assertFalse(at_least_one_year("2028-01-01", "2028-12-31"))
        self.assertTrue(at_least_one_year("2027-01-01", "2028-01-01"))
        self.assertTrue(at_least_one_year("2028-02-29", "2029-02-28"))
        short = performance_report([_point("a", "2028-01-01", "100"), _point("b", "2028-12-31", "110")], scope="system",
                                   currency_code="USD", as_of_ns=0)
        self.assertEqual(short["period"]["calendar_days"], 365)
        self.assertIsNone(short["twr_annualized"])
        self.assertIsNone(short["money_weighted"]["irr_annualized"])
        full = performance_report([_point("a", "2027-01-01", "100"), _point("b", "2028-01-01", "110")], scope="system",
                                  currency_code="USD", as_of_ns=0)
        self.assertAlmostEqual(full["twr_annualized"], 0.10, places=12)
        self.assertAlmostEqual(full["money_weighted"]["irr_annualized"], 0.10, places=9)


class RiskFreeLabelTest(unittest.TestCase):
    """Defect: a caller-supplied risk-free rate without a series id was labelled 'none configured: Rf = 0'."""

    def test_supplied_rate_is_not_labelled_zero(self):
        rets = [0.001 * ((-1) ** i) + 0.0005 for i in range(30)]
        self.assertIn("caller-supplied", risk_statistics(rets, rf_annual=0.04)["risk_free"]["series_id"])
        self.assertIn("none configured", risk_statistics(rets)["risk_free"]["series_id"])


class EmptiedBucketChainTest(unittest.TestCase):
    """Defect: a sub-period with no capital at risk between two funded ones (a bucket emptied by an outflow and
    refunded later: 'unallocated' after funding a session with all of it, or the system after a full deauthorization
    and re-authorization) is 0/0 and nulled the TWR of the whole history."""

    def test_empty_interior_subperiods_link_as_zero(self):
        pts = [_point("a", "2026-01-05", "100", "100"), _point("b", "2026-01-06", "110", "100"),
               _point("c", "2026-01-07", "0", "-10"), _point("d", "2026-01-08", "0", "-10"),
               _point("e", "2026-01-09", "50", "40"), _point("f", "2026-01-12", "55", "40")]
        for convention in ("start_of_day", "end_of_day"):
            r = performance_report(pts, scope="system", currency_code="USD", as_of_ns=0, convention=convention)
            self.assertAlmostEqual(r["twr"], 1.1 * 1.1 - 1, places=12, msg=convention)
            linked = [s for s in r["subperiods"] if s["reason"] and "no capital at risk" in s["reason"]]
            self.assertEqual(len(linked), 2, convention)
            self.assertTrue(all(s["return"] == 0.0 for s in linked))

    def test_value_from_nothing_still_breaks_the_chain(self):
        # a total loss (r = -1, defined), then value appears in an empty bucket with no inflow: genuinely undefined
        pts = [_point("a", "2026-01-05", "100", "100"), _point("b", "2026-01-06", "0", "100"),
               _point("c", "2026-01-07", "5", "100"), _point("d", "2026-01-08", "6", "100")]
        r = performance_report(pts, scope="system", currency_code="USD", as_of_ns=0)
        self.assertIsNone(r["twr"])
        self.assertIn("chain is broken", r["reason"])


class GapSpanningStatisticsTest(unittest.TestCase):
    """Gap: statistics treated a return spanning skipped incomplete valuations as one ordinary period without saying so."""

    def test_spanning_subperiods_are_counted(self):
        pts = [_point("a", "2026-10-19", "100"), _point("b", "2026-10-20", "101"),
               ValuationPoint("c", "2026-10-21", 0, 0, "USD", None, F("0"), F("0"), "stale_marks", False),
               _point("d", "2026-10-22", "103")]
        st = performance_report(pts, scope="system", currency_code="USD", as_of_ns=0)["statistics"]
        self.assertEqual(st["subperiods_spanning_gaps"], 1)
        self.assertIn("several days of variance", st["gap_note"])


# ---------------------------------------------------------------------------------------------- allocation
class InverseVolatilityBudgetTest(unittest.TestCase):
    """Defect: the fix-and-repeat clipping loop froze a name at its floor, then capped the rest, and returned weights
    summing to less than the budget with status 'ok' (raw shares 0.70/0.25/0.05, floor 0.10, cap 0.40 gave 0.90)."""

    def test_reported_case_fills_the_budget(self):
        r = inverse_volatility({"a": 1 / 0.70, "b": 1 / 0.25, "c": 1 / 0.05}, floor="0.10", cap="0.40")
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(sum(r["weights"].values()), 1.0, places=12)
        self.assertAlmostEqual(r["weights"]["a"], 0.4, places=12)
        self.assertAlmostEqual(r["weights"]["b"], 0.4, places=12)
        self.assertAlmostEqual(r["weights"]["c"], 0.2, places=12)
        self.assertEqual(r["binding"], ["a", "b"])

    def test_random_cases_satisfy_every_constraint_and_stay_proportional(self):
        rng = random.Random(4)
        for case in range(300):
            n = rng.randint(2, 9)
            vols = {f"i{k}": rng.uniform(0.005, 0.08) for k in range(n)}
            reserve = rng.choice(["0", "0.05", "0.2"])
            budget = 1 - float(Fraction(reserve))
            floor = rng.uniform(0, budget / n)
            cap = rng.uniform(budget / n, 1.0)
            r = inverse_volatility(vols, cash_reserve=reserve, floor=repr(floor), cap=repr(cap))
            self.assertEqual(r["status"], "ok", case)
            w = r["weights"]
            self.assertAlmostEqual(sum(w.values()), budget, delta=1e-12, msg=case)
            for k, x in w.items():
                self.assertGreaterEqual(x, floor - 1e-15, (case, k))
                self.assertLessEqual(x, cap + 1e-15, (case, k))
            free = [k for k in w if k not in r["binding"]]
            ratios = [w[k] * vols[k] for k in free]   # unclipped names keep w proportional to 1/sigma
            for x in ratios:
                self.assertAlmostEqual(x, ratios[0], delta=1e-12, msg=case)
            ordered = sorted(w, key=lambda k: vols[k])  # a calmer name never gets less weight
            for a, b in zip(ordered, ordered[1:]):
                self.assertGreaterEqual(w[a], w[b] - 1e-15, case)


class OptimiserParameterTest(unittest.TestCase):
    """Defect: a negative or non-finite eta/lambda/tau reached the eq. 6.2 solver (non-convex: a meaningless 'optimal')."""

    def test_invalid_parameters_are_refused(self):
        sigma = [[0.04, 0.0], [0.0, 0.09]]
        for kw in ({"eta": -0.1}, {"eta": float("nan")}, {"lam": -1.0}, {"tau": -0.5}, {"tau": float("inf")}):
            with self.assertRaises(ValueError, msg=kw):
                mean_variance(sigma, **kw)


class ProposalUniverseTest(unittest.TestCase):
    """Defects: (1) current weights kept only the LAST owner's market value of an instrument held by several owners
    (here a session and the ``external`` owner of a manual trade, at account scope); (2) a held instrument the method
    could not weight (no return history) received a target of zero, i.e. a proposal to sell it because data were
    missing."""

    def setUp(self):
        h = self.h = booted(self, initial_cash="1000.00")
        register_second(h)
        buy(h, "S1", "B1", "3", "10.00", bid="10.00", ask="10.02")
        h.broker.external_trade("FIX-EQ-1", "BUY", F("2"), F("10.00"))   # owned by 'external' (ADR-0006)
        h.pump()
        self.assertEqual(h.verify(), [])
        self.conn = ro(self, h)
        self.store = snapshot_store(self)
        rng = random.Random(2)
        price = 10.0
        for i in range(14):
            if i:
                h.clock.advance(DAY)
                price *= 1 + rng.gauss(0, 0.01)
            eod(self, h, self.store, self.conn, {"FIX-EQ-1": f"{price:.2f}"})
        self.price = F(f"{price:.2f}")

    def test_current_weights_sum_every_owner(self):
        scope = parse_scope("account")
        r = propose(self.conn, ACCOUNT, quotes(self.h), self.store, now_ns=self.h.clock.now_ns(), scope=scope, min_obs=10)
        rep = holdings(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns(), scope=scope)
        self.assertEqual(sorted(p["owner"] for p in rep["positions"]), ["external", "session:S1"])
        mv = self.price * F("5")  # 3 in S1 + 2 owned by external
        nav = F(rep["totals"]["USD"]["nav_marked"])
        expected = float(Fraction(mv.mantissa, 10 ** mv.scale) / Fraction(nav.mantissa, 10 ** nav.scale))
        self.assertAlmostEqual(r["mean_variance"]["current_weights"]["FIX-EQ-1"], expected, places=12)

    def test_held_instrument_without_history_is_left_unchanged_not_sold(self):
        h = self.h
        buy(h, "S1", "B3", "2", "8.00", instrument_id=SECOND_INSTRUMENT.instrument_id, bid="8.00", ask="8.02")
        h.quote(self.price.to_str(), (self.price + F("0.02")).to_str())
        self.assertEqual(h.verify(), [])
        before = db_hash(h.db_path)
        r = propose(self.conn, ACCOUNT, quotes(h), self.store, now_ns=h.clock.now_ns(), scope=parse_scope("session:S1"),
                    min_obs=10, cash_reserve="0.05")
        self.assertEqual(db_hash(h.db_path), before)
        tl = r["trade_lists"]["inverse_volatility"]
        self.assertNotIn(SECOND_INSTRUMENT.instrument_id, {t["instrument_id"] for t in tl["trades"]})
        self.assertEqual([u["instrument_id"] for u in tl["left_unchanged"]], [SECOND_INSTRUMENT.instrument_id])
        self.assertIn(SECOND_INSTRUMENT.instrument_id, {e["instrument_id"] for e in r["excluded_from_risk_estimates"]})


# ---------------------------------------------------------------------------------------------- marks / provenance
class ConsolidationLabelTest(unittest.TestCase):
    """Defect: rebuilding a quote from the core's JSON dropped its feed label (consolidated / non_consolidated), so a
    snapshot recorded the wrong feed and the what-if's decision record differed from the live gate's."""

    def test_label_survives_into_marks_snapshots_and_the_what_if_decision(self):
        h = booted(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        conn = ro(self, h)
        q = quotes(h)
        q["FIX-EQ-1"] = {**q["FIX-EQ-1"], "consolidation": "non_consolidated"}
        self.assertEqual(normalize_quotes(q)["FIX-EQ-1"].consolidation, "non_consolidated")
        store = snapshot_store(self)
        run = record_eod(conn, store, ACCOUNT, q, now_ns=h.clock.now_ns())
        (row,) = store.conn.execute("SELECT quote FROM run_quote WHERE run_id=?", (run["run_id"],)).fetchall()
        self.assertEqual(json.loads(row["quote"])["consolidation"], "non_consolidated")
        r = dry_run(conn, ACCOUNT, {"session_id": "S1", "instrument_id": "FIX-EQ-1", "side": "BUY", "quantity": "1",
                                    "limit_price": "9.02"}, q, now_ns=h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertTrue(r["approved"], r)
        self.assertEqual(r["quote_used"]["consolidation"], "non_consolidated")


class SettlementUnknownTest(unittest.TestCase):
    """Defect: a lot whose opening execution had no settlement record was silently counted as settled."""

    def test_missing_settlement_record_is_unknown_not_settled(self):
        h = booted(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        conn = ro(self, h)
        book = load_book(conn, ACCOUNT)
        book.settlement_status.clear()
        r = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM, book=book)
        (p,) = r["positions"]
        self.assertEqual((p["settled_quantity"], p["unsettled_quantity"], p["settlement_unknown_quantity"]), ("0", "0", "5"))
        normal = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)["positions"][0]
        self.assertEqual((normal["settled_quantity"], normal["unsettled_quantity"], normal["settlement_unknown_quantity"]),
                         ("0", "5", "0"))


class MalformedCikTest(unittest.TestCase):
    """Defect: a non-numeric 'sec.cik' instrument attribute raised and took the exposure route down."""

    def test_malformed_cik_is_unclassified_with_a_reason(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "research.sqlite"
        ResearchStore(path).close()
        clf = SicClassifier(path)
        self.addCleanup(clf.close)
        r = clf.classify("X", "XYZ", {"sec.cik": "not-a-cik"}, T0)
        self.assertEqual((r["sector"], r["status"]), ("unknown", "unclassified"))
        self.assertIn("not a CIK", r["reason"])

    def test_a_file_without_the_entity_tables_is_a_reason_not_a_crash(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "research.sqlite"
        sqlite3.connect(path).execute("CREATE TABLE unrelated (x)").connection.close()
        clf = SicClassifier(path)
        self.addCleanup(clf.close)
        r = clf.classify("X", "XYZ", {}, T0)
        self.assertEqual(r["status"], "unclassified")
        self.assertIn("unreadable", r["reason"])


# ---------------------------------------------------------------------------------------------- snapshots
class AtomicValuationReadTest(unittest.TestCase):
    """Defect: the back-dating check, the projections and the flows were three separate reads of the live core; an
    event committed between them entered one and not the other (here: a capital authorization in the flows but not in
    the balances), so the day's P&L and TWR flow adjustment were wrong."""

    def test_an_event_committed_mid_valuation_is_in_neither_the_balances_nor_the_flows(self):
        h = booted(self, authorize="200.00")
        conn = ro(self, h)
        real = snapshots.load_book

        def load_then_commit(c, key):
            book = real(c, key)
            self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="50.00").accepted)
            return book

        with mock.patch.object(snapshots, "load_book", load_then_commit):
            v = build_valuation(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())
        self.assertEqual(h.verify(), [])
        system = next(s for s in v["scopes"] if s["scope"] == "system")
        self.assertEqual((system["nav_fresh"], system["cum_external_flow"]), ("200.000000", "200.000000"))
        after = next(s for s in build_valuation(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())["scopes"]
                     if s["scope"] == "system")
        self.assertEqual((after["nav_fresh"], after["cum_external_flow"]), ("250.000000", "250.000000"))


# ---------------------------------------------------------------------------------------------- VaR backtest
class BacktestLookAheadTest(unittest.TestCase):
    """Defect: the forecast for day t used the creation time of the LATEST run of day t-1 as its data cutoff; a
    revision of t-1 recorded later (SnapshotStore.record(created_at_ns=...)) put day t's own return, and every later
    one, into the scenario set."""

    def test_a_late_revision_of_the_prior_day_does_not_leak_later_returns(self):
        h = booted(self)
        conn = ro(self, h)
        store = snapshot_store(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        rng = random.Random(8)
        price, times, valuations = 9.0, [], []
        for i in range(16):
            if i:
                h.clock.advance(DAY)
                price *= 1 + rng.gauss(0, 0.03)
            text = f"{price:.2f}"
            h.quote(text, f"{float(text) + 0.02:.2f}")
            valuations.append(build_valuation(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns()))
            record_eod(conn, store, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())
            times.append(h.clock.now_ns())
        scope = parse_scope("session:S1")
        kw = dict(alpha="0.9", lookback=250, min_scenarios=5)
        before = backtest(store, ACCOUNT, scope, "USD", as_of_ns=times[-1], **kw)
        k = 7
        revised = copy.deepcopy(valuations[k])
        revised["as_of_ns"] = times[k] + 30 * SEC                       # same valuation date, identical content
        store.record(revised, created_at_ns=times[-1] + 10 * SEC)        # ...but recorded after the whole history
        after = backtest(store, ACCOUNT, scope, "USD", as_of_ns=times[-1] + 10 * SEC, **kw)
        self.assertEqual(after["days"], before["days"])
        self.assertEqual(after["exceptions"], before["exceptions"])
        # The test has teeth: a forecast for day k+1 made with hindsight differs from the ex-ante one.
        row = next(r for r in before["days"] if r["date"] == valuations[k + 1]["valuation_date"])
        dates, hist = history_as_of(mark_table(store, ACCOUNT, as_of_ns=times[-1]), times[-1])
        pos = [{"instrument_id": p["instrument_id"], "owner": p["owner"], "quantity": p["quantity"], "mark": p["mark"],
                "multiplier": p["multiplier"]} for p in valuations[k]["positions"]]
        hindsight = historical_var(pos, returns_from_prices(hist, dates, 1), **kw)["var"]
        self.assertNotAlmostEqual(row["var_forecast"], hindsight, places=9)

    def test_a_prior_day_recorded_only_after_the_realized_day_has_no_forecast(self):
        h = booted(self)
        conn = ro(self, h)
        store = snapshot_store(self)
        v0 = build_valuation(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())
        t0 = h.clock.now_ns()
        h.clock.advance(DAY)
        record_eod(conn, store, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())
        store.record(v0, created_at_ns=h.clock.now_ns() + SEC)   # day 0 backfilled after day 1 was valued
        bt = backtest(store, ACCOUNT, SYSTEM, "USD", as_of_ns=h.clock.now_ns() + SEC, min_scenarios=1)
        self.assertEqual(bt["observations"], 0)
        self.assertIn("no ex-ante forecast", bt["skipped"][0]["reason"])
        self.assertGreater(h.clock.now_ns(), t0)


class PartialVarCoverageTest(unittest.TestCase):
    """Gap: a VaR computed on the covered subset of positions said so only through the 'uncovered' list."""

    def test_partial_coverage_is_explicit(self):
        dates = [f"d{i:02d}" for i in range(25)]
        rng = random.Random(1)
        rets = {"A": {d: Fraction(rng.randint(-30, 30), 1000) for d in dates}}
        pos = [{"instrument_id": "A", "quantity": "10", "mark": "10.00", "multiplier": "1"},
               {"instrument_id": "B", "quantity": "5", "mark": "20.00", "multiplier": "1"}]
        r = historical_var(pos, rets, alpha="0.95", min_scenarios=20)
        self.assertIsNotNone(r["var"])
        self.assertFalse(r["coverage_complete"])
        self.assertIn("1 of 2 positions", r["partial_note"])
        full = historical_var(pos[:1], rets, alpha="0.95", min_scenarios=20)
        self.assertTrue(full["coverage_complete"])
        self.assertIsNone(full["partial_note"])


class ResearchHistoryTest(unittest.TestCase):
    """Untested branch: the point-in-time research-store price loader. A close published after the decision instant
    is invisible; two datasets pricing the same date are refused instead of the last one silently winning."""

    def _store(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "research.sqlite"
        rs = ResearchStore(path)
        rs.upsert_entity("FIXTURE-E1", "issuer", "fixture issuer (test only)", {})
        for i, (d, px) in enumerate((("2026-10-19", "10.00"), ("2026-10-20", "10.50"), ("2026-10-21", "9.80"))):
            avail = T0 + i * DAY
            rs.record(Observation("FIXTURE.close", "FIXTURE-E1", "close", "USD/share", avail, avail, "fixture",
                                  value=F(px), period_end=d, period_kind="DAY"), avail)
        rs.close()
        return path

    def test_point_in_time_and_ambiguity(self):
        path = self._store()
        early = research_price_history(path, {"FIX-EQ-1": "FIXTURE-E1"}, as_of_ns=T0 + DAY)
        self.assertEqual([d for d, _ in early["FIX-EQ-1"]], ["2026-10-19", "2026-10-20"])
        full = research_price_history(path, {"FIX-EQ-1": "FIXTURE-E1"}, as_of_ns=T0 + 2 * DAY)
        self.assertEqual(full["FIX-EQ-1"][-1], ("2026-10-21", Fraction("9.80")))
        rs = ResearchStore(path)
        rs.record(Observation("FIXTURE.other_close", "FIXTURE-E1", "close", "USD/share", T0, T0, "fixture",
                              value=F("10.01"), period_end="2026-10-19", period_kind="DAY"), T0)
        rs.close()
        with self.assertRaises(ValueError):
            research_price_history(path, {"FIX-EQ-1": "FIXTURE-E1"}, as_of_ns=T0 + 2 * DAY)
        named = research_price_history(path, {"FIX-EQ-1": "FIXTURE-E1"}, as_of_ns=T0 + 2 * DAY, dataset="FIXTURE.close")
        self.assertEqual(len(named["FIX-EQ-1"]), 3)


# ---------------------------------------------------------------------------------------------- stress
class StressFixture(unittest.TestCase):
    """Session S1 holds 4 FIXA bought at 10.00 and 3 FIXB at 8.00; core valuation at bid 10.50 / 8.40 (P&L +1.20)."""

    def setUp(self):
        h = self.h = booted(self)
        register_second(h)
        buy(h, "S1", "B1", "4", "10.00", bid="10.00", ask="10.02")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2", bid="8.00", ask="8.02")
        h.quote("10.50", "10.52")
        h.quote("8.40", "8.42", "FIX-EQ-2")
        h.tick()
        self.assertEqual(h.verify(), [])
        self.conn = ro(self, h)
        self.scope = parse_scope("session:S1")

    def gap(self, g: str) -> dict:
        return overnight_gap(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns(), gap=g, scope=self.scope,
                             deployment_id=DEPLOYMENT)


class DailyLossBaselineTest(StressFixture):
    """Defect: the daily-loss threshold compared the shocked P&L with the first valuation of whatever risk day the core
    last valued (an older day) and with the P&L of the last core valuation (missing every move since), so it reported
    breaches the core does not have and missed moves the core would see."""

    def test_new_risk_day_follows_the_core_rule(self):
        h = self.h
        h.clock.advance(DAY)                        # next New York risk day; no core valuation yet today
        h.quote("10.50", "10.52")
        h.quote("8.40", "8.42", "FIX-EQ-2")
        before = db_hash(h.db_path)
        r = self.gap("-0.10")
        self.assertEqual(db_hash(h.db_path), before)
        t = {x["threshold"]: x for x in r["thresholds"]}
        daily = t["daily_loss_trigger"]
        self.assertFalse(daily["crossed"])
        self.assertEqual(daily["loss_after"], "0.000000")
        self.assertIn("none yet on this risk day", daily["day_start_source"])
        self.assertTrue(daily["crossed_if_window_started_at_last_core_valuation"])   # 6.72 since the last valuation
        self.assertEqual(daily["loss_since_last_core_valuation"], "6.720000")
        self.assertNotEqual(r["core_reaction"]["breach_after"], "DAILY_LOSS")      # agrees with the core's own reaction

    def test_baseline_uses_current_marks_and_matches_the_core_measurement(self):
        h = self.h
        h.quote("11.00", "11.02")                   # +2.00 since the core valued; same risk day, no new core valuation
        r = self.gap("-0.10")
        t = {x["threshold"]: x for x in r["thresholds"]}
        daily = t["daily_loss_trigger"]
        self.assertEqual(daily["system_pnl_before"], "3.200000")
        self.assertEqual(r["pnl_by_currency"]["USD"], "-6.920000")                # 10% of 44.00 + 25.20
        self.assertEqual((daily["day_start_pnl"], daily["loss_after"]), ("1.200000", "4.920000"))
        authorized = F(load_book(self.conn, ACCOUNT).risk["authorized_total"])
        core_value = F(r["core_reaction"]["valuation_result"]["system_value"])
        self.assertEqual(F(daily["system_pnl_after"]) + authorized, core_value)  # the core measured the same P&L
        self.assertEqual(r["core_reaction"]["breach_after"], "DAILY_LOSS")
        self.assertTrue(daily["crossed"])
        self.assertEqual(r["core_reaction"]["computed_on_core_seq"], r["core_seq"])  # same core state for both


class GapSellFillTest(StressFixture):
    """Defect: a resting sell that fills through a gap up was reported as filling 'at its limit' while the scenario P&L
    still marked the sold shares at the gapped bid, overstating the gain by q * (bid' - limit)."""

    def test_sold_quantity_earns_its_limit(self):
        h = self.h
        res = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="S1", order_id="RS", instrument_id="FIX-EQ-1",
                    side="SELL", quantity="2", limit_price="10.60", deadline_ns=h.clock.now_ns() + 60 * SEC,
                    policy_hash=_policy_hash(h))
        self.assertTrue(res.accepted, res.body)
        h.pump()
        self.assertEqual(h.verify(), [])
        r = self.gap("0.05")                        # FIXA bid 10.50 -> 11.025: the 10.60 sell fills at 10.60
        (o,) = [x for x in r["resting_orders"] if x["order_id"] == "RS"]
        self.assertEqual(o["effect"], "fills at its limit")
        self.assertEqual(o["immediate_marked_pnl"], "-0.850000")                 # 2 x (10.60 - 11.025)
        self.assertEqual(r["pnl_by_currency"]["USD"], "2.510000")                # 3.36 revaluation - 0.85


# ---------------------------------------------------------------------------------------------- what-if
class SandboxOwnershipTest(unittest.TestCase):
    """Defect: the sandbox took the FIRST account's execution owner whatever account it evaluated."""

    def test_deployment_comes_from_the_evaluated_account(self):
        h = booted(self)
        conn = ro(self, h)
        with Sandbox(conn, now_ns=h.clock.now_ns(), account_key=ACCOUNT) as sb:
            self.assertEqual(sb.desk.deployment_id, DEPLOYMENT)
        with Sandbox(conn, now_ns=h.clock.now_ns(), account_key="NO-SUCH-ACCOUNT") as sb:
            self.assertEqual(sb.desk.deployment_id, "whatif-sandbox")
        r = dry_run(conn, ACCOUNT, {"session_id": "S1", "instrument_id": "FIX-EQ-1", "side": "BUY", "quantity": "1",
                                    "limit_price": "10.01"}, quotes(h), now_ns=h.clock.now_ns())
        self.assertEqual(r["evaluated_as_deployment"], DEPLOYMENT)
        unknown = dry_run(conn, ACCOUNT, {"session_id": "NOPE", "instrument_id": "FIX-EQ-1", "side": "BUY", "quantity": "1",
                                          "limit_price": "10.01"}, quotes(h), now_ns=h.clock.now_ns())
        self.assertFalse(unknown["approved"])
        self.assertIn("reason", unknown["impact"])


# ---------------------------------------------------------------------------------------------- routes
class RouteParameterTest(unittest.TestCase):
    """Defects: non-finite or negative float parameters reached the analytics; an explicit max_age_ms=0 was replaced
    by the policy budget; the performance lineage always said nav_fresh even when nav_marked was requested."""

    def setUp(self):
        from test_analytics_routes import Ctx, FakeCore, call
        from unknown.api.routes import portfolio
        self.call = call
        self.h = h = booted(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        self.conn = ro(self, h)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = Ctx(self.root, FakeCore(quotes(h)))
        patcher = mock.patch.object(portfolio, "time", types.SimpleNamespace(time_ns=lambda: self.h.clock.now_ns()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_float_parameters_are_finite_and_in_range(self):
        for q in ({"eta": "nan"}, {"eta": "-0.1"}, {"tau": "-1"}, {"tau": "inf"}):
            with self.assertRaises(ValueError, msg=q):
                self.call(self.ctx, self.conn, "portfolio/allocation", scope="session:S1", **q)

    def test_explicit_zero_freshness_budget_is_honoured(self):
        self.h.clock.advance(SEC)
        default = self.call(self.ctx, self.conn, "portfolio/holdings", scope="system")
        zero = self.call(self.ctx, self.conn, "portfolio/holdings", scope="system", max_age_ms="0")
        self.assertEqual(default["positions"][0]["mark"]["status"], "fresh")
        self.assertEqual(zero["positions"][0]["mark"]["status"], "stale")
        self.assertEqual(zero["mark_policy"]["max_age_ms"], 0)

    def test_performance_lineage_names_the_nav_field_used(self):
        store = snapshots.SnapshotStore(snapshots.default_path(self.root))
        try:
            for i in range(3):
                if i:
                    self.h.clock.advance(DAY)
                eod(self, self.h, store, self.conn, {"FIX-EQ-1": f"{9.0 + 0.1 * i:.2f}"})
        finally:
            store.close()
        self.ctx.core.quote_map = quotes(self.h)
        marked = self.call(self.ctx, self.conn, "portfolio/performance", scope="session:S1", nav="nav_marked")
        self.assertEqual(marked["lineage"]["inputs"]["nav_field"], "nav_marked")
        fresh = self.call(self.ctx, self.conn, "portfolio/performance", scope="session:S1")
        self.assertEqual(fresh["lineage"]["inputs"]["nav_field"], "nav_fresh")


if __name__ == "__main__":
    unittest.main()
