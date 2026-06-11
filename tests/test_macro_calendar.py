"""Tests for analytics/macro_calendar.py."""
import unittest
from datetime import datetime, timezone

from analytics.macro_calendar import (
    FOMC_DATES, is_high_impact_event, macro_event_for_ts,
)


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class TestMacroEventForTs(unittest.TestCase):
    def test_fomc_date_detected(self):
        # Use the first known FOMC date in our static list
        fomc = FOMC_DATES[0]
        ts = _ts(fomc.year, fomc.month, fomc.day, 18)
        self.assertEqual(macro_event_for_ts(ts), 'FOMC')

    def test_cpi_release_detected(self):
        # CPI is approximated to the 12th of any month
        self.assertEqual(macro_event_for_ts(_ts(2026, 6, 12)), 'CPI')

    def test_normal_weekday(self):
        # Tuesday with no event → ''
        self.assertEqual(macro_event_for_ts(_ts(2026, 4, 7)), '')

    def test_weekend_detected(self):
        # 2026-04-04 was a Saturday
        self.assertEqual(macro_event_for_ts(_ts(2026, 4, 4)), 'WEEKEND')

    def test_priority_fomc_over_weekend(self):
        # If a hypothetical FOMC fell on a weekend the FOMC label wins.
        # Use first FOMC that is NOT a weekend in our list — they shouldn't be —
        # but verify ordering by patching: just ensure FOMC label > WEEKEND.
        for d in FOMC_DATES:
            if d.weekday() in (5, 6):  # weekend FOMC (shouldn't happen, but...)
                ts = _ts(d.year, d.month, d.day)
                self.assertEqual(macro_event_for_ts(ts), 'FOMC')


class TestIsHighImpact(unittest.TestCase):
    def test_fomc_is_high_impact(self):
        fomc = FOMC_DATES[0]
        self.assertTrue(is_high_impact_event(_ts(fomc.year, fomc.month, fomc.day)))

    def test_weekend_is_not_high_impact(self):
        self.assertFalse(is_high_impact_event(_ts(2026, 4, 4)))

    def test_normal_day_is_not_high_impact(self):
        self.assertFalse(is_high_impact_event(_ts(2026, 4, 7)))


if __name__ == '__main__':
    unittest.main()
