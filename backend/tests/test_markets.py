"""India and EU venue calendars and the venue registry (CAP-INS-07, CAP-INS-08, CAP-OMS-09).

Expected values come from the venues' own published schedules (cited in the
calendar files); in particular the Muhurat settlement dates are those NSE
Clearing published in NCL/CMPT/77037 (2026-10-21).
"""
import json
import re
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from unknown.domain.calendar import (
    BusinessCalendar,
    CalendarHorizonError,
    SessionTimeUnknown,
    load_calendar,
)
from unknown.domain.instruments import Venue
from unknown.domain.markets import (
    NoSettlementRule,
    NotATradingDay,
    UnknownVenue,
    load_calendars,
    load_registry,
)

REPO = Path(__file__).resolve().parents[2]
CALENDARS = REPO / "config" / "calendars"
VENUES = REPO / "config" / "markets" / "venues.json"
EURONEXT = ("XPAR", "XAMS", "XBRU", "XLIS")
EU = EURONEXT + ("XETR", "XMIL", "XMAD")
INDIA = ("XNSE", "XBOM")
URL = re.compile(r"^https?://\S+$")


def easter(year: int) -> date:
    """Anonymous Gregorian computus, an independent check on the transcribed Easter dates."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


class CalendarFilesTests(unittest.TestCase):
    def setUp(self):
        self.files = sorted(CALENDARS.glob("*.json"))
        self.cals = load_calendars(CALENDARS)

    def test_all_calendars_load(self):
        ids = {p.stem for p in self.files}
        self.assertTrue({"XNYS", "XNSE", "XBOM", "NCL-CM", "ICCL-EQ", "XETR", "XPAR", "XAMS", "XBRU", "XLIS",
                         "XMIL", "XMAD", "T2S-EUR"} <= ids)
        self.assertEqual(set(self.cals), ids)

    def test_every_calendar_cites_a_source_url_and_retrieval_date(self):
        for path in self.files:
            with self.subTest(path.name):
                body = json.loads(path.read_text())
                self.assertRegex(body["source"], URL)
                retrieved = date.fromisoformat(body["retrieved_on"])
                self.assertLessEqual(retrieved, date.today())
                self.assertTrue(body.get("statement"), "each calendar quotes or summarises its source")
                for extra in body.get("sources", []):
                    self.assertRegex(extra["url"], URL)
                    date.fromisoformat(extra["retrieved_on"])
                for special in body.get("special_sessions", []):
                    self.assertRegex(special["source"], URL)

    def test_xnys_file_still_loads_with_original_semantics(self):
        cal = load_calendar(CALENDARS / "XNYS.json")
        self.assertFalse(cal.is_business_day(date(2026, 11, 26)))          # Thanksgiving
        self.assertEqual(cal.early_close(date(2026, 11, 27)), (True, "13:00"))
        self.assertEqual(cal.regular_session, (time(9, 30), time(16, 0)))
        self.assertEqual(cal.add_business_days(date(2026, 11, 25), 1), date(2026, 11, 27))
        with self.assertRaises(CalendarHorizonError):
            cal.is_business_day(date(2029, 1, 2))

    def test_kinds(self):
        for cal_id in INDIA + EU:
            self.assertEqual(self.cals[cal_id].kind, "trading")
        for cal_id in ("NCL-CM", "ICCL-EQ", "T2S-EUR"):
            self.assertEqual(self.cals[cal_id].kind, "settlement")

    def test_easter_dates_match_the_computus(self):
        for cal_id, years in (("XETR", range(2026, 2033)), ("T2S-EUR", range(2026, 2033)), ("XPAR", [2026]),
                              ("XMIL", [2026]), ("XMAD", [2026])):
            holidays = self.cals[cal_id].holidays
            for year in years:
                e = easter(year)
                with self.subTest(cal_id, year=year):
                    self.assertIn(e - timedelta(days=2), holidays)    # Good Friday
                    self.assertIn(e + timedelta(days=1), holidays)    # Easter Monday


class BusinessDayArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.cals = load_calendars(CALENDARS)

    def test_india_trading_holidays(self):
        nse, bse = self.cals["XNSE"], self.cals["XBOM"]
        self.assertEqual(nse.add_business_days(date(2026, 10, 19), 1), date(2026, 10, 21))   # Dussehra 20 Oct
        self.assertEqual(nse.add_business_days(date(2026, 3, 2), 1), date(2026, 3, 4))       # Holi 3 Mar
        self.assertEqual(bse.add_business_days(date(2026, 1, 14), 1), date(2026, 1, 16))     # election closure 15 Jan
        self.assertEqual(bse.add_business_days(date(2026, 11, 6), 2), date(2026, 11, 11))    # Muhurat Sunday and 10 Nov
        for cal in (nse, bse):
            self.assertFalse(cal.is_business_day(date(2026, 1, 15)))
            self.assertTrue(cal.is_business_day(date(2026, 2, 19)))     # trading day ...

    def test_india_settlement_holidays_differ_from_trading(self):
        for cal_id in ("NCL-CM", "ICCL-EQ"):
            cal = self.cals[cal_id]
            with self.subTest(cal_id):
                for d in (date(2026, 2, 19), date(2026, 3, 19), date(2026, 4, 1), date(2026, 8, 26)):
                    self.assertFalse(cal.is_business_day(d))           # ... but not a settlement day
                self.assertEqual(cal.add_business_days(date(2026, 3, 30), 1), date(2026, 4, 2))  # 31 Mar and 1 Apr
                self.assertEqual(cal.add_business_days(date(2026, 2, 18), 1), date(2026, 2, 20))

    def test_xetra_holidays(self):
        cal = self.cals["XETR"]
        self.assertEqual(cal.add_business_days(date(2026, 12, 23), 1), date(2026, 12, 28))
        self.assertEqual(cal.add_business_days(date(2027, 3, 25), 1), date(2027, 3, 30))
        self.assertEqual(cal.add_business_days(date(2032, 12, 23), 1), date(2032, 12, 27))
        self.assertTrue(cal.is_business_day(date(2026, 5, 14)))     # Ascension Day: Xetra trades
        self.assertTrue(cal.is_business_day(date(2026, 10, 5)))

    def test_euronext_holidays_and_half_days(self):
        for mic in EURONEXT:
            cal = self.cals[mic]
            with self.subTest(mic):
                self.assertEqual(cal.add_business_days(date(2026, 4, 2), 1), date(2026, 4, 7))
                self.assertEqual(cal.add_business_days(date(2026, 4, 30), 1), date(2026, 5, 4))
                self.assertTrue(cal.is_business_day(date(2026, 12, 24)))   # half trading day, not a holiday
                self.assertEqual(cal.early_close(date(2026, 12, 24)), (True, None))
                self.assertFalse(cal.is_business_day(date(2026, 12, 25)))

    def test_milan_and_madrid(self):
        mil, mad = self.cals["XMIL"], self.cals["XMAD"]
        self.assertEqual(mil.add_business_days(date(2026, 12, 23), 1), date(2026, 12, 28))
        self.assertFalse(mil.is_business_day(date(2026, 12, 31)))
        with self.assertRaises(CalendarHorizonError):      # next Milan day after 30 Dec is in unpublished 2027
            mil.add_business_days(date(2026, 12, 30), 1)
        self.assertTrue(mad.is_business_day(date(2026, 12, 24)))
        self.assertEqual(mad.early_close(date(2026, 12, 24)), (True, "14:00"))
        self.assertEqual(mad.add_business_days(date(2026, 4, 30), 1), date(2026, 5, 4))
        self.assertTrue(mad.is_business_day(date(2026, 1, 6)))       # Epiphany is not in BME's dated 2026 list

    def test_t2s_settlement_days(self):
        t2s = self.cals["T2S-EUR"]
        self.assertTrue(t2s.is_business_day(date(2026, 12, 24)))      # Xetra closed, settlement open
        self.assertTrue(t2s.is_business_day(date(2026, 12, 31)))
        self.assertEqual(t2s.add_business_days(date(2026, 12, 24), 1), date(2026, 12, 28))
        self.assertEqual(t2s.add_business_days(date(2028, 12, 22), 1), date(2028, 12, 27))   # 25 and 26 Dec
        self.assertTrue(self.cals["XETR"].is_business_day(date(2026, 10, 5)) and t2s.is_business_day(date(2026, 10, 5)))


class HorizonTests(unittest.TestCase):
    def setUp(self):
        self.cals = load_calendars(CALENDARS)
        self.registry = load_registry(VENUES, CALENDARS)

    def test_calendars_refuse_dates_beyond_their_source(self):
        cases = {cal_id: date(2027, 1, 4) for cal_id in INDIA + ("NCL-CM", "ICCL-EQ") + EURONEXT + ("XMIL", "XMAD")}
        cases.update({"XETR": date(2033, 1, 3), "T2S-EUR": date(2033, 1, 3)})
        for cal_id, beyond in cases.items():
            cal = self.cals[cal_id]
            with self.subTest(cal_id):
                for d in (beyond, date(2025, 12, 31)):
                    with self.assertRaises(CalendarHorizonError):
                        cal.is_business_day(d)
                    with self.assertRaises(CalendarHorizonError):
                        cal.is_trading_day(d)
                    with self.assertRaises(CalendarHorizonError):
                        cal.session(d)
                with self.assertRaises(CalendarHorizonError):
                    cal.add_business_days(cal.valid_through - timedelta(days=1), 5)

    def test_settlement_refuses_to_cross_the_horizon(self):
        # T+1 from the last NSE trading day of 2026 lands in 2027, for which NSE Clearing has published nothing.
        with self.assertRaises(CalendarHorizonError):
            self.registry.expected_settlement_date("XNSE", date(2026, 12, 31))
        with self.assertRaises(CalendarHorizonError):
            self.registry.expected_settlement_date("XBOM", date(2026, 12, 31))
        # Euronext publishes 2026 only: a 2027 trade date cannot be validated.
        with self.assertRaises(CalendarHorizonError):
            self.registry.expected_settlement_date("XPAR", date(2027, 10, 11))
        # but a late-2026 Paris trade settles into 2027 on T2S, which is sourced through 2032
        self.assertEqual(self.registry.expected_settlement_date("XPAR", date(2026, 12, 30)), date(2027, 1, 4))


class MuhuratTests(unittest.TestCase):
    def setUp(self):
        self.cals = load_calendars(CALENDARS)
        self.muhurat = date(2026, 11, 8)

    def test_nse_muhurat_is_an_explicit_special_session(self):
        nse = self.cals["XNSE"]
        self.assertFalse(nse.is_business_day(self.muhurat))   # Sunday and a published holiday
        self.assertTrue(nse.is_trading_day(self.muhurat))
        special = nse.special_session(self.muhurat)
        self.assertEqual(special.kind, "muhurat")
        self.assertEqual((special.open, special.close), (time(14, 20), time(15, 30)))
        self.assertIs(special.settles, True)
        self.assertIn("CMTR76941", special.source)
        window = nse.session(self.muhurat)
        self.assertEqual(window.kind, "special")
        start, end = nse.session_utc(self.muhurat)
        self.assertEqual((start, end), (datetime(2026, 11, 8, 8, 50, tzinfo=timezone.utc),
                                        datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)))
        # the special session never counts as an ordinary business day
        self.assertEqual(nse.add_business_days(date(2026, 11, 6), 1), date(2026, 11, 9))
        self.assertIsNone(nse.special_session(date(2026, 11, 9)))

    def test_bse_muhurat_times_unknown_not_assumed(self):
        bse = self.cals["XBOM"]
        self.assertTrue(bse.is_trading_day(self.muhurat))
        special = bse.special_session(self.muhurat)
        self.assertFalse(special.times_known)
        self.assertIsNone(special.settles)
        with self.assertRaises(SessionTimeUnknown):
            bse.session(self.muhurat)

    def test_only_india_declares_special_sessions(self):
        for cal_id, cal in self.cals.items():
            for s in cal.special_sessions:
                self.assertIn(cal_id, INDIA)
                self.assertTrue(s.day.weekday() >= 5 or s.day in cal.holidays)

    def test_loader_refuses_a_special_session_on_an_ordinary_business_day(self):
        body = json.loads((CALENDARS / "XNSE.json").read_text())
        body["special_sessions"][0]["date"] = "2026-11-11"     # an ordinary Wednesday
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "XNSE.json"
            path.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, "ordinary business day"):
                load_calendar(path)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.cals = load_calendars(CALENDARS)

    def test_regular_sessions(self):
        day = date(2026, 7, 1)
        self.assertEqual(self.cals["XNSE"].session(day).open, time(9, 15))
        self.assertEqual(self.cals["XNSE"].session(day).close, time(15, 30))
        for mic in EU:
            with self.subTest(mic):
                w = self.cals[mic].session(day)
                self.assertEqual((w.open, w.close, w.kind), (time(9, 0), time(17, 30), "regular"))
        self.assertIsNone(self.cals["XETR"].session(date(2026, 12, 24)))

    def test_lisbon_hours_are_stated_in_cet(self):
        lis, par = self.cals["XLIS"], self.cals["XPAR"]
        for day in (date(2026, 1, 15), date(2026, 7, 1)):
            self.assertEqual(lis.session_utc(day), par.session_utc(day))
        start, _ = lis.session_utc(date(2026, 1, 15))
        self.assertEqual(start, datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc))   # 08:00 in Lisbon

    def test_unpublished_hours_raise(self):
        with self.assertRaises(SessionTimeUnknown):
            self.cals["XPAR"].session(date(2026, 12, 24))       # half day, close not yet announced
        with self.assertRaises(SessionTimeUnknown):
            self.cals["XETR"].session(date(2026, 12, 30))       # Deutsche Boerse: hours may differ
        with self.assertRaises(SessionTimeUnknown):
            self.cals["XBOM"].session(date(2026, 7, 1))         # no sourced BSE hours
        w = self.cals["XMAD"].session(date(2026, 12, 24))
        self.assertEqual((w.close, w.kind), (time(14, 0), "early_close"))

    def test_fixture_calendar_is_unchanged(self):
        cal = BusinessCalendar("TEST", "1", holidays=frozenset({date(2026, 10, 26)}), source="test fixture")
        self.assertEqual(cal.add_business_days(date(2026, 10, 23), 1), date(2026, 10, 27))
        self.assertEqual(cal.special_sessions, ())


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(VENUES, CALENDARS)

    def settle(self, mic, trade_date):
        return self.registry.expected_settlement_date(mic, trade_date)

    def test_sample_trade_per_venue(self):
        cases = [
            ("XNYS", date(2026, 11, 25), date(2026, 11, 27)),   # T+1 over Thanksgiving
            ("XNSE", date(2026, 2, 18), date(2026, 2, 20)),     # T+1 over an NCL bank holiday that is a trading day
            ("XBOM", date(2026, 3, 30), date(2026, 4, 2)),      # T+1 over ICCL 31 Mar and 1 Apr
            ("XETR", date(2026, 12, 23), date(2026, 12, 28)),   # T+2: 24 Dec settles though Xetra is closed
            ("XPAR", date(2026, 4, 2), date(2026, 4, 8)),       # T+2 over Easter
            ("XAMS", date(2026, 4, 2), date(2026, 4, 8)),
            ("XBRU", date(2026, 4, 2), date(2026, 4, 8)),
            ("XLIS", date(2026, 4, 2), date(2026, 4, 8)),
            ("XMIL", date(2026, 12, 23), date(2026, 12, 28)),
            ("XMAD", date(2026, 12, 30), date(2027, 1, 4)),     # T+2 over New Year on T2S
        ]
        for mic, trade, expected in cases:
            with self.subTest(mic):
                self.assertEqual(self.settle(mic, trade), expected)

    def test_muhurat_settlement_matches_nse_clearing_schedule(self):
        # NCL/CMPT/77037: 06-Nov and 08-Nov trades settle 09-Nov; 09-Nov trades settle 11-Nov.
        self.assertEqual(self.settle("XNSE", date(2026, 11, 6)), date(2026, 11, 9))
        self.assertEqual(self.settle("XNSE", date(2026, 11, 8)), date(2026, 11, 9))
        self.assertEqual(self.settle("XNSE", date(2026, 11, 9)), date(2026, 11, 11))
        self.assertEqual(self.settle("XBOM", date(2026, 11, 8)), date(2026, 11, 9))

    def test_eu_moves_to_t_plus_1_on_trade_date_2027_10_11(self):
        xetr = self.registry.venue("XETR")
        self.assertEqual(xetr.settlement_cycle(date(2027, 10, 8)).business_days, 2)
        self.assertEqual(xetr.settlement_cycle(date(2027, 10, 11)).business_days, 1)
        self.assertEqual(self.settle("XETR", date(2027, 10, 8)), date(2027, 10, 12))    # last T+2 trade date
        self.assertEqual(self.settle("XETR", date(2027, 10, 11)), date(2027, 10, 12))   # first T+1 trade date
        for mic in EU:
            cycle = self.registry.venue(mic).settlement_cycle(date(2027, 10, 11))
            self.assertEqual((cycle.business_days, cycle.from_trade_date), (1, date(2027, 10, 11)))
            self.assertTrue(any(s.key == "EU-2025-2075" and "11 October 2027" in s.quote for s in cycle.sources))

    def test_us_and_india_are_t_plus_1(self):
        self.assertEqual(self.registry.venue("XNYS").settlement_cycle(date(2026, 1, 2)).business_days, 1)
        for mic in INDIA:
            cycle = self.registry.venue(mic).settlement_cycle(date(2026, 1, 2))
            self.assertEqual(cycle.business_days, 1)
            self.assertTrue(any("January 27, 2023" in s.quote for s in cycle.sources))

    def test_refusals(self):
        with self.assertRaises(NotATradingDay):
            self.settle("XETR", date(2026, 12, 24))
        with self.assertRaises(NotATradingDay):
            self.settle("XNSE", date(2026, 11, 10))
        with self.assertRaises(NoSettlementRule):
            self.registry.venue("XNSE").settlement_cycle(date(2020, 1, 1))
        with self.assertRaises(UnknownVenue):
            self.registry.venue("XLON")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(VENUES, CALENDARS)

    def test_venues_and_identity(self):
        self.assertEqual(set(self.registry.mics), {"XNYS", "XNSE", "XBOM", "XETR", "XPAR", "XAMS", "XBRU", "XLIS", "XMIL", "XMAD"})
        expect = {"XNYS": ("US", "USD"), "XNSE": ("IN", "INR"), "XBOM": ("IN", "INR"), "XETR": ("DE", "EUR"),
                  "XPAR": ("FR", "EUR"), "XAMS": ("NL", "EUR"), "XBRU": ("BE", "EUR"), "XLIS": ("PT", "EUR"),
                  "XMIL": ("IT", "EUR"), "XMAD": ("ES", "EUR")}
        for mic, (country, ccy) in expect.items():
            v = self.registry.venue(mic)
            self.assertEqual((v.country, v.currency), (country, ccy))
            self.assertEqual(v.timezone, self.registry.trading_calendar(mic).timezone)
            self.assertEqual(v.as_venue(), Venue(mic, v.name, country, v.timezone, mic))
        self.assertEqual(self.registry.venue("XMAD").operating_mic, "BMEX")
        self.assertEqual(self.registry.venue("XMIL").segment_mic, "MTAA")

    def test_every_rule_and_convention_is_sourced(self):
        for mic in self.registry.mics:
            v = self.registry.venue(mic)
            refs = list(v.sources)
            for cycle in v.settlement_cycles:
                self.assertTrue(cycle.sources)
                refs += cycle.sources
            for conv in (v.lot, v.tick):
                if conv is not None:
                    self.assertTrue(conv.sources)
                    refs += conv.sources
            for ref in refs:
                self.assertRegex(ref.url, URL)
                self.assertLessEqual(ref.retrieved_on, date.today())
        self.assertIsNone(self.registry.venue("XNYS").lot)
        self.assertIsNone(self.registry.venue("XBOM").tick)
        self.assertEqual(self.registry.venue("XNSE").lot.rule, "per_instrument")

    def test_broker_codes_document_reality_without_granting_eligibility(self):
        self.assertNotIn("ibkr", self.registry.venue("XBOM").provider_codes)
        self.assertEqual(self.registry.venue("XETR").provider_codes["ibkr"], "IBIS")
        policy = json.loads((REPO / "config" / "policies" / "paper-default.v1.json").read_text())
        self.assertNotIn("XNSE", json.dumps(policy))          # registry data never widens trading eligibility

    def test_invalid_registries_are_refused(self):
        body = json.loads(VENUES.read_text())

        def broken(mutate):
            b = json.loads(json.dumps(body))
            mutate(b)
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            path = Path(tmp.name) / "venues.json"
            path.write_text(json.dumps(b))
            return path

        def first(b):
            return next(v for v in b["venues"] if v["mic"] == "XETR")

        mutations = {
            "MIC": lambda b: first(b).update(mic="xetr"),
            "unknown source": lambda b: first(b)["settlement_cycles"][0].update(sources=["NOPE"]),
            "increasing": lambda b: first(b)["settlement_cycles"].reverse(),
            "not found": lambda b: first(b).update(settlement_calendar="MISSING"),
            "time zone": lambda b: first(b).update(timezone="Europe/Paris"),
            "url": lambda b: b["sources"]["EU-2025-2075"].update(url="not a url"),
            "at least one source": lambda b: first(b).update(tick={"rule": "x", "statement": "y", "sources": []}),
        }
        for message, mutate in mutations.items():
            with self.subTest(message):
                with self.assertRaisesRegex(ValueError, message):
                    load_registry(broken(mutate), CALENDARS)
        with self.assertRaises(ValueError):    # currency the platform does not account in
            load_registry(broken(lambda b: first(b).update(currency="XYZ")), CALENDARS)

    def test_calendar_loader_validation(self):
        body = json.loads((CALENDARS / "XMAD.json").read_text())
        cases = {
            "outside the data horizon": lambda b: b["holidays"].append(["2027-01-06", "x"]),
            "duplicate holiday": lambda b: b["holidays"].append(["2026-01-01", "again"]),
            "both a holiday and an early close": lambda b: b["early_closes"].append(["2026-12-25", "13:00", "x"]),
        }
        for message, mutate in cases.items():
            b = json.loads(json.dumps(body))
            mutate(b)
            with self.subTest(message), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "XMAD.json"
                path.write_text(json.dumps(b))
                with self.assertRaisesRegex(ValueError, message):
                    load_calendar(path)


if __name__ == "__main__":
    unittest.main()
