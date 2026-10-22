"""The core service loads every sourced calendar (config/calendars): a policy whose settlement_calendar is XNYS
must settle on NYSE business days, not weekdays (found 2026-10-22: the service loaded WEEKDAYS only, so the paper
policy's XNYS silently fell back to weekdays and ignored holidays such as Thanksgiving)."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.domain.calendar import WEEKDAYS_ONLY, load_calendars  # noqa: E402
from unknown.service.core import CALENDAR_DIR  # noqa: E402

import json  # noqa: E402


class ServiceCalendarTests(unittest.TestCase):
    def test_service_loads_every_sourced_calendar(self):
        cals = load_calendars(CALENDAR_DIR)
        files = {json.loads(f.read_text())["calendar_id"] for f in CALENDAR_DIR.glob("*.json")}
        self.assertEqual(set(cals), files | {"WEEKDAYS"})
        self.assertIs(cals["WEEKDAYS"], WEEKDAYS_ONLY)

    def test_policies_name_calendars_the_service_loads(self):
        cals = load_calendars(CALENDAR_DIR)
        for f in (CALENDAR_DIR.parents[0] / "policies").glob("*.json"):
            with self.subTest(policy=f.name):
                self.assertIn(json.loads(f.read_text())["settlement_calendar"], cals)

    def test_xnys_settlement_skips_thanksgiving(self):
        xnys = load_calendars(CALENDAR_DIR)["XNYS"]
        self.assertEqual(xnys.add_business_days(date(2026, 11, 25), 1), date(2026, 11, 27))
        self.assertEqual(WEEKDAYS_ONLY.add_business_days(date(2026, 11, 25), 1), date(2026, 11, 26))


class ServiceDeskCalendarTests(unittest.TestCase):
    def test_the_service_desk_settles_on_xnys_business_days(self):
        """The desk the running service opens (CoreService.open_desk, used by run) carries the sourced calendars,
        so a paper policy's settlement_calendar XNYS settles a 2026-11-25 trade on 2026-11-27, not Thanksgiving."""
        import tempfile
        from unknown.broker.simulated import SimulatedBroker
        from unknown.domain.clock import ManualClock
        from unknown.domain.fixed import Fixed
        from unknown.service.core import CoreService
        from unknown.service.paths import Paths
        with tempfile.TemporaryDirectory() as tmp:
            clock = ManualClock(1_792_504_800_000_000_000)
            svc = CoreService(paths=Paths(Path(tmp)), account_key="IBKR-PAPER-1", deployment_id="t", clock=clock,
                              broker=SimulatedBroker(clock, initial_settled_cash=Fixed.parse("1000"), calendar=WEEKDAYS_ONLY))
            desk = svc.open_desk()
            try:
                self.assertIn("XNYS", desk.calendars)
                self.assertEqual(desk.calendars["XNYS"].add_business_days(date(2026, 11, 25), 1), date(2026, 11, 27))
                self.assertEqual(set(desk.calendars), set(load_calendars(CALENDAR_DIR)))
            finally:
                desk.store.close()


if __name__ == "__main__":
    unittest.main()
