"""Tests for analytics/validation.py."""
import unittest

from analytics.validation import (
    BACKTEST_BASELINE, evaluate, per_condition_analysis,
    readiness_check, underperforming_buckets,
)


def _trade(side='long', pnl=10.0, result=None, **kwargs) -> dict:
    base = {
        'side':         side,
        'entry_price':  100.0,
        'exit_price':   100.0 + pnl,
        'qty':          1.0,
        'pnl_usdt':     pnl,
        'pnl_pct':      pnl / 100.0,
        'result':       result or ('WIN' if pnl >= 0 else 'LOSS'),
        'exit_reason':  'take_profit' if pnl >= 0 else 'stop_loss',
        'entry_ts_ms':  1714579200000,
        'exit_ts_ms':   1714582800000,
        'session':      'europe',
        'entry_rsi':    35.0,
        'entry_adx':    22.0,
        'entry_atr_pct': 50.0,
        'regime':       'trending',
        'macro_event':  '',
        'mtf_15m_aligned': 1,
    }
    base.update(kwargs)
    return base


class TestEvaluate(unittest.TestCase):
    def test_no_trades_returns_zero_metrics(self):
        out = evaluate([], days_running=7)
        self.assertEqual(out['n_trades'], 0)
        self.assertEqual(out['win_rate_pct'], 0.0)
        # remaining_for_validation = 30
        self.assertEqual(out['remaining_for_validation'], 30)

    def test_overfitting_alert_after_20_trades_below_30pct(self):
        # 20 trades, 4 wins → 20 % WR
        trades = [_trade(pnl=-5) for _ in range(16)] + [_trade(pnl=5) for _ in range(4)]
        out = evaluate(trades, days_running=14)
        types = {a['type'] for a in out['alerts']}
        self.assertIn('overfitting', types)

    def test_no_overfitting_alert_below_minimum_trades(self):
        trades = [_trade(pnl=-5) for _ in range(5)]
        out = evaluate(trades, days_running=2)
        types = {a['type'] for a in out['alerts']}
        self.assertNotIn('overfitting', types)

    def test_rolling_degradation_alert(self):
        # 12 trades, last 10 have only 1 win → 10 % rolling WR
        trades = [_trade(pnl=10) for _ in range(2)]
        trades += [_trade(pnl=-5) for _ in range(9)]
        trades += [_trade(pnl=10) for _ in range(1)]
        out = evaluate(trades, days_running=10)
        types = {a['type'] for a in out['alerts']}
        self.assertIn('rolling_degradation', types)

    def test_no_trades_alert_when_silent_for_8_days(self):
        old_ts = 1
        trades = [_trade(exit_ts_ms=old_ts)]
        out = evaluate(trades, days_running=10, now_ms=old_ts + 9 * 86_400_000)
        types = {a['type'] for a in out['alerts']}
        self.assertIn('no_trades', types)

    def test_rolling_wr_none_below_window(self):
        out = evaluate([_trade()] * 5, days_running=2)
        self.assertIsNone(out['rolling_win_rate'])

    def test_baseline_referenced(self):
        out = evaluate([], days_running=1)
        self.assertEqual(out['baseline'], BACKTEST_BASELINE)


class TestPerConditionAnalysis(unittest.TestCase):
    def test_buckets_by_rsi(self):
        trades = [
            _trade(entry_rsi=25, pnl=10),
            _trade(entry_rsi=33, pnl=10),
            _trade(entry_rsi=42, pnl=-5),
        ]
        out = per_condition_analysis(trades)
        self.assertEqual(out['by_rsi']['<30']['n'], 1)
        self.assertEqual(out['by_rsi']['30–40']['n'], 1)
        self.assertEqual(out['by_rsi']['40–50']['n'], 1)

    def test_buckets_by_adx_and_session(self):
        trades = [
            _trade(entry_adx=15, session='asia'),
            _trade(entry_adx=30, session='usa'),
        ]
        out = per_condition_analysis(trades)
        self.assertIn('<18', out['by_adx'])
        self.assertEqual(out['by_session']['usa']['n'], 1)


class TestUnderperforming(unittest.TestCase):
    def test_flags_buckets_below_floor(self):
        analysis = per_condition_analysis(
            [_trade(entry_rsi=42, pnl=-10) for _ in range(10)]
        )
        weak = underperforming_buckets(analysis, min_trades=5, win_rate_floor=25.0)
        # 0% WR with 10 trades in 40-50 RSI bucket → flagged
        self.assertTrue(any(b['label'] == '40–50' for b in weak))

    def test_skips_buckets_below_min_trades(self):
        analysis = per_condition_analysis([_trade(pnl=-1) for _ in range(2)])
        weak = underperforming_buckets(analysis, min_trades=5, win_rate_floor=25.0)
        self.assertEqual(weak, [])


class TestReadinessCheck(unittest.TestCase):
    def test_all_criteria_met_returns_ready(self):
        # 30 trades, 50 % WR, positive PnL, FOMC seen
        trades = []
        for _ in range(15):
            trades.append(_trade(pnl=10))
        for _ in range(15):
            trades.append(_trade(pnl=-5))
        trades.append(_trade(macro_event='FOMC', pnl=5))
        out = readiness_check(trades, days_running=30)
        self.assertTrue(out['ready'])

    def test_missing_macro_event_blocks(self):
        trades = [_trade(pnl=10) for _ in range(20)] + [_trade(pnl=-2) for _ in range(15)]
        out = readiness_check(trades, days_running=30)
        # Even with great WR, missing macro event → not ready
        if out['checks']['min_30_trades'] and out['checks']['win_rate_above_38pct']:
            self.assertFalse(out['ready'])
            self.assertFalse(out['checks']['survived_macro_event'])

    def test_below_min_trades_blocks(self):
        trades = [_trade(pnl=10) for _ in range(5)]
        out = readiness_check(trades, days_running=5)
        self.assertFalse(out['ready'])
        self.assertFalse(out['checks']['min_30_trades'])

    def test_overfitting_alert_blocks(self):
        # 20 losing trades, 1 win = 4.7 % WR → overfitting alert active
        trades = [_trade(pnl=-5) for _ in range(20)] + [_trade(pnl=5)]
        out = readiness_check(trades, days_running=20)
        self.assertFalse(out['checks']['no_overfitting_alerts'])


if __name__ == '__main__':
    unittest.main()
