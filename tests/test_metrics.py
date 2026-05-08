"""Tests for analytics/metrics.py."""
import unittest
from datetime import datetime, timezone

from analytics.metrics import (
    compute_performance, daily_pnl, equity_curve,
    max_drawdown, pnl_drawdown_ratio,
    win_rate_by_session, win_rate_by_side,
)


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def _trade(side, pnl, entry_ts, exit_ts, result=None) -> dict:
    return {
        'side': side,
        'entry_price': 100.0,
        'exit_price':  100.0 + pnl,
        'qty': 1.0,
        'pnl_usdt': pnl,
        'pnl_pct': pnl / 100.0 * 100,
        'result': result or ('WIN' if pnl >= 0 else 'LOSS'),
        'reason': 'take_profit' if pnl >= 0 else 'stop_loss',
        'entry_ts': entry_ts,
        'exit_ts': exit_ts,
    }


class TestDailyPnl(unittest.TestCase):
    def test_groups_by_day(self):
        trades = [
            _trade('long', 5.0, _ts(2026, 5, 1), _ts(2026, 5, 1, 13)),
            _trade('long', 7.0, _ts(2026, 5, 1, 14), _ts(2026, 5, 1, 15)),
            _trade('long', -3.0, _ts(2026, 5, 2), _ts(2026, 5, 2, 13)),
        ]
        result = daily_pnl(trades, days=30)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['date'], '2026-05-01')
        self.assertAlmostEqual(result[0]['pnl_usdt'], 12.0)
        self.assertEqual(result[1]['date'], '2026-05-02')
        self.assertAlmostEqual(result[1]['pnl_usdt'], -3.0)


class TestWinRateBySide(unittest.TestCase):
    def test_separates_long_short(self):
        trades = [
            _trade('long', 10.0, 0, 1),
            _trade('long', -2.0, 0, 1),
            _trade('short', 5.0, 0, 1),
        ]
        out = win_rate_by_side(trades)
        self.assertEqual(out['long']['trades'], 2)
        self.assertEqual(out['long']['wins'], 1)
        self.assertEqual(out['short']['trades'], 1)
        self.assertEqual(out['short']['wins'], 1)


class TestWinRateBySession(unittest.TestCase):
    def test_groups_by_entry_session(self):
        trades = [
            _trade('long', 5.0, _ts(2026, 5, 1, 2),  _ts(2026, 5, 1, 3)),  # asia
            _trade('long', 3.0, _ts(2026, 5, 1, 10), _ts(2026, 5, 1, 11)), # europe
            _trade('long', -1.0, _ts(2026, 5, 1, 15), _ts(2026, 5, 1, 16)), # usa
        ]
        out = win_rate_by_session(trades)
        self.assertIn('asia', out)
        self.assertIn('europe', out)
        self.assertIn('usa', out)
        self.assertEqual(out['asia']['trades'], 1)
        self.assertEqual(out['usa']['win_rate_pct'], 0.0)


class TestEquityCurve(unittest.TestCase):
    def test_cumulative_balance(self):
        trades = [
            _trade('long', 10.0, 0, 1),
            _trade('long', -5.0, 1, 2),
            _trade('long', 3.0, 2, 3),
        ]
        curve = equity_curve(trades, initial_balance=100.0)
        self.assertEqual(len(curve), 4)  # initial + 3 trades
        self.assertEqual(curve[0]['balance'], 100.0)
        self.assertEqual(curve[-1]['balance'], 108.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_computes_peak_to_trough(self):
        trades = [
            _trade('long', 10.0, 0, 1),
            _trade('long', -8.0, 1, 2),
            _trade('long', 5.0, 2, 3),
        ]
        dd = max_drawdown(trades, initial_balance=100.0)
        self.assertGreater(dd['pct'], 0)
        self.assertEqual(dd['usdt'], 8.0)


class TestPnlDdRatio(unittest.TestCase):
    def test_returns_positive_ratio(self):
        self.assertEqual(pnl_drawdown_ratio(20.0, 5.0), 4.0)

    def test_zero_dd_returns_zero(self):
        self.assertEqual(pnl_drawdown_ratio(20.0, 0.0), 0.0)


class TestComputePerformance(unittest.TestCase):
    def test_full_payload_shape(self):
        trades = [_trade('long', 5.0, _ts(2026, 5, 1, 10), _ts(2026, 5, 1, 11))]
        out = compute_performance(trades, initial_balance=10_000.0)
        self.assertIn('total_trades', out)
        self.assertIn('by_side', out)
        self.assertIn('by_session', out)
        self.assertIn('daily_pnl_30d', out)
        self.assertIn('equity_curve', out)
        self.assertIn('last_20_trades', out)
        self.assertEqual(out['total_trades'], 1)

    def test_empty_trades_does_not_crash(self):
        out = compute_performance([], initial_balance=10_000.0)
        self.assertEqual(out['total_trades'], 0)
        self.assertEqual(out['win_rate_pct'], 0.0)
        self.assertEqual(out['final_balance'], 10_000.0)


if __name__ == '__main__':
    unittest.main()
