"""Tests for analytics/live_db.py — SQLite storage layer."""
import tempfile
import unittest
from pathlib import Path

from analytics.live_db import (
    count_trades, init_db, insert_kelly_change, insert_live_trade,
    insert_near_miss, insert_shadow_trade, list_kelly_changes,
    list_live_trades, list_near_misses, query_all,
    update_shadow_resolution,
)


class _DBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / 'test.db'
        init_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()


class TestSchema(_DBTestCase):
    def test_init_db_creates_all_tables(self):
        rows = query_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name",
            db_path=self.db_path,
        )
        names = {r['name'] for r in rows}
        self.assertIn('live_trades', names)
        self.assertIn('near_misses', names)
        self.assertIn('kelly_changes', names)
        self.assertIn('shadow_trades', names)


class TestLiveTrades(_DBTestCase):
    def _trade(self, **kwargs) -> dict:
        base = {
            'entry_ts_ms':   1714579200000,
            'exit_ts_ms':    1714582800000,
            'side':          'long',
            'entry_price':   80000.0,
            'exit_price':    81000.0,
            'qty':           0.05,
            'notional_usdt': 4000.0,
            'pnl_usdt':      50.0,
            'pnl_pct':       1.25,
            'result':        'WIN',
            'exit_reason':   'take_profit',
            'duration_min':  60.0,
            'session':       'europe',
            'entry_rsi':     35.0,
            'entry_adx':     22.0,
            'regime':        'trending',
            'macro_event':   '',
            'kelly_used':    0.02,
        }
        base.update(kwargs)
        return base

    def test_insert_and_count(self):
        self.assertEqual(count_trades(self.db_path), 0)
        insert_live_trade(self._trade(), self.db_path)
        self.assertEqual(count_trades(self.db_path), 1)

    def test_extras_flow_into_extra_json(self):
        insert_live_trade(self._trade(custom='value'), self.db_path)
        rows = list_live_trades(db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIn('custom', rows[0]['extra_json'])

    def test_list_orders_by_exit_ts_desc(self):
        insert_live_trade(self._trade(exit_ts_ms=2000), self.db_path)
        insert_live_trade(self._trade(exit_ts_ms=1000), self.db_path)
        rows = list_live_trades(db_path=self.db_path)
        self.assertEqual(rows[0]['exit_ts_ms'], 2000)
        self.assertEqual(rows[1]['exit_ts_ms'], 1000)

    def test_rejects_epoch_zero_timestamp(self):
        """Regression: ids 1-10 in production had entry_ts_ms ∈ {0,1}.
        Guard must refuse them to keep stats clean."""
        rc = insert_live_trade(self._trade(entry_ts_ms=0), self.db_path)
        self.assertEqual(rc, -1)
        self.assertEqual(count_trades(self.db_path), 0)

    def test_rejects_implausibly_old_timestamp(self):
        rc = insert_live_trade(self._trade(entry_ts_ms=1_000_000_000_000), self.db_path)
        self.assertEqual(rc, -1)
        self.assertEqual(count_trades(self.db_path), 0)

    def test_accepts_modern_timestamp(self):
        # Boundary: exactly the floor value is accepted.
        rc = insert_live_trade(
            self._trade(entry_ts_ms=1_700_000_000_000), self.db_path
        )
        self.assertGreater(rc, 0)
        self.assertEqual(count_trades(self.db_path), 1)


class TestNearMisses(_DBTestCase):
    def test_insert_and_list(self):
        insert_near_miss({
            'ts_ms': 1, 'reason': 'long near-miss',
            'close': 80_000, 'rsi': 47.5, 'sma20': 79_900,
            'rsi_distance': 2.5, 'sma_distance_pct': 0.125,
            'side_intended': 'long',
        }, self.db_path)
        rows = list_near_misses(db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason'], 'long near-miss')


class TestKellyChanges(_DBTestCase):
    def test_insert_and_list(self):
        insert_kelly_change({
            'ts_ms': 1, 'old_kelly_pct': 0.02, 'new_kelly_pct': 0.018,
            'rolling_win_rate': 30.0, 'n_recent_trades': 10,
            'reason': 'dampen win_rate=30%',
        }, self.db_path)
        rows = list_kelly_changes(db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['new_kelly_pct'], 0.018)


class TestShadowTrades(_DBTestCase):
    def test_insert_and_resolve(self):
        sid = insert_shadow_trade({
            'decision_ts_ms': 1, 'side': 'long',
            'entry_price': 100, 'sl_price': 98, 'tp_price': 104,
        }, self.db_path)
        update_shadow_resolution(sid, 2, 104, 'take_profit', 4.0, self.db_path)
        rows = query_all(
            'SELECT * FROM shadow_trades WHERE id=?',
            (sid,), db_path=self.db_path,
        )
        self.assertEqual(rows[0]['resolved'], 1)
        self.assertEqual(rows[0]['exit_reason'], 'take_profit')


class TestQueryAllSafety(_DBTestCase):
    def test_rejects_non_select(self):
        with self.assertRaises(ValueError):
            query_all('DELETE FROM live_trades', db_path=self.db_path)


if __name__ == '__main__':
    unittest.main()
