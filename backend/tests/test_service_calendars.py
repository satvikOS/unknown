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


if __name__ == "__main__":
    unittest.main()
