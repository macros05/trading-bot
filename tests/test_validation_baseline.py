"""Tests for the certificate-driven validation baseline.

The audit found analytics/validation.py compared live trades against a
hardcoded backtest that LOSES money (-292 USDT) — an inverted reference that
only alerts when live does worse than a losing strategy. The fix: the baseline
comes from the certified champion's expected metrics, and defaults to NEUTRAL
(zero asserted edge) when no champion is certified — never to a losing number.
"""
import unittest

from analytics.validation import (
    BACKTEST_BASELINE,
    NEUTRAL_BASELINE,
    load_baseline,
    evaluate,
)


def _cert(win_rate=58.0, max_dd=9.0, trades_per_year=156.0):
    return {
        'label': 'cand',
        'expected_metrics': {
            'win_rate_pct': win_rate,
            'net_pnl_pct': 18.0,
            'num_trades': 312,
            'max_drawdown_pct': max_dd,
            'trades_per_year': trades_per_year,
        },
    }


class TestBaselineNotInverted(unittest.TestCase):
    def test_default_baseline_is_not_a_losing_pnl(self):
        # The whole bug was a negative expected PnL. Neutral must be >= 0.
        self.assertGreaterEqual(BACKTEST_BASELINE['pnl_usdt'], 0.0)
        self.assertGreaterEqual(NEUTRAL_BASELINE['pnl_usdt'], 0.0)

    def test_load_baseline_none_returns_neutral(self):
        self.assertEqual(load_baseline(None), NEUTRAL_BASELINE)

    def test_load_baseline_from_certificate(self):
        b = load_baseline(_cert(win_rate=58.0, max_dd=9.0, trades_per_year=156.0))
        self.assertEqual(b['win_rate_pct'], 58.0)
        self.assertEqual(b['max_drawdown_pct'], 9.0)
        self.assertAlmostEqual(b['avg_trade_per_week'], 3.0)  # 156/52
        self.assertGreaterEqual(b['pnl_usdt'], 0.0)


class TestEvaluateWithNeutralBaseline(unittest.TestCase):
    def _winning_trades(self, n):
        return [{'result': 'WIN', 'pnl_usdt': 5.0,
                 'exit_ts_ms': 1_000 + i} for i in range(n)]

    def test_no_pnl_divergence_alert_against_neutral_baseline(self):
        out = evaluate(self._winning_trades(25), days_running=10)
        types = {a['type'] for a in out['alerts']}
        self.assertNotIn('pnl_divergence', types)

    def test_evaluate_accepts_explicit_baseline(self):
        out = evaluate(self._winning_trades(5), days_running=3,
                       baseline=load_baseline(_cert()))
        self.assertEqual(out['baseline']['win_rate_pct'], 58.0)


if __name__ == '__main__':
    unittest.main()
