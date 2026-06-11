"""Tests for strategy/sessions.py."""
import unittest
from datetime import datetime, timezone

from strategy.sessions import (
    ASIA, EUROPE, OFF, USA,
    is_session_allowed, session_for_ts,
)


def _ts_at(hour: int, minute: int = 0) -> int:
    """Build a UTC ts (ms) for 2026-01-01 hour:minute."""
    return int(datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


class TestSessionForTs(unittest.TestCase):
    def test_asia_window(self):
        self.assertEqual(session_for_ts(_ts_at(0)), ASIA)
        self.assertEqual(session_for_ts(_ts_at(7, 59)), ASIA)

    def test_europe_window(self):
        self.assertEqual(session_for_ts(_ts_at(8)), EUROPE)
        self.assertEqual(session_for_ts(_ts_at(12, 59)), EUROPE)

    def test_usa_window(self):
        self.assertEqual(session_for_ts(_ts_at(13)), USA)
        self.assertEqual(session_for_ts(_ts_at(20, 59)), USA)

    def test_off_window(self):
        self.assertEqual(session_for_ts(_ts_at(21)), OFF)
        self.assertEqual(session_for_ts(_ts_at(23, 59)), OFF)


class TestIsSessionAllowed(unittest.TestCase):
    def test_blocked_session(self):
        self.assertFalse(is_session_allowed(_ts_at(22), blocked=(OFF,)))

    def test_allowed_session(self):
        self.assertTrue(is_session_allowed(_ts_at(15), blocked=(OFF,)))

    def test_empty_blocked_allows_all(self):
        self.assertTrue(is_session_allowed(_ts_at(22), blocked=()))
        self.assertTrue(is_session_allowed(_ts_at(22), blocked=None))

    def test_multiple_blocked(self):
        self.assertFalse(is_session_allowed(_ts_at(2), blocked=(ASIA, OFF)))
        self.assertTrue(is_session_allowed(_ts_at(10), blocked=(ASIA, OFF)))


if __name__ == '__main__':
    unittest.main()
