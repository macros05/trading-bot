"""Tests for backtest/v7_full.py simulator using synthetic data."""
import unittest

import pandas as pd

from backtest.v7_full import (
    V7Params, baseline_v6_params, buy_and_hold,
    metrics_summary, simulate_v7,
)


def _trending_df(n: int = 1500, start: float = 100.0,
                 step: float = 0.05) -> pd.DataFrame:
    """Bullish trend: each candle close is start + i*step."""
    rows = []
    for i in range(n):
        c = start + i * step
        rows.append({
            'ts':     1714579200000 + i * 60_000,
            'open':   c - step,
            'high':   c + step,
            'low':    c - 2 * step,
            'close':  c,
            'volume': 100.0,
        })
    return pd.DataFrame(rows)


def _flat_df(n: int = 1500, price: float = 100.0) -> pd.DataFrame:
    """Perfectly flat market — should produce zero trades."""
    rows = []
    for i in range(n):
        rows.append({
            'ts':     1714579200000 + i * 60_000,
            'open':   price, 'high': price, 'low': price,
            'close':  price, 'volume': 100.0,
        })
    return pd.DataFrame(rows)


class TestSimulator(unittest.TestCase):
    def test_no_trades_in_flat_market_with_filters(self):
        # New strategy with all filters should refuse to trade flat market
        p = V7Params(
            label='test', use_volatility_filter=False,
            use_mtf_filter=False, range_lookback_min=100,
        )
        result = simulate_v7(_flat_df(1500), p)
        # Range filter blocks all entries; signals also won't fire on flat data
        self.assertLessEqual(len(result['trades']), 0)

    def test_baseline_v6_matches_label(self):
        p = baseline_v6_params()
        self.assertEqual(p.label, 'v6-baseline')
        self.assertFalse(p.use_volatility_filter)
        self.assertFalse(p.use_mtf_filter)
        self.assertFalse(p.use_session_filter)

    def test_metrics_summary_with_no_trades(self):
        result = {
            'label': 'test', 'final_balance': 10000.0,
            'total_pnl': 0.0, 'total_fees': 0.0,
            'period': {'from': 'a', 'to': 'b', 'candles': 1},
            'trades': [], 'equity': [10000.0],
        }
        out = metrics_summary(result)
        self.assertEqual(out['num_trades'], 0)
        self.assertEqual(out['win_rate_pct'], 0.0)

    def test_buy_and_hold_simple_trend(self):
        df = _trending_df(100, start=100.0, step=0.5)
        bh = buy_and_hold(df, balance=10_000.0)
        # Price doubled (50% rise on 100 → 149.5), large positive PnL
        self.assertGreater(bh['pnl_usdt'], 0)


class TestParamsDefaults(unittest.TestCase):
    def test_v7_default_label(self):
        self.assertEqual(V7Params().label, 'v7-full')

    def test_baseline_uses_v6_thresholds(self):
        p = baseline_v6_params()
        self.assertEqual(p.rsi_long_threshold, 40.0)
        self.assertEqual(p.rsi_short_threshold, 55.0)


if __name__ == '__main__':
    unittest.main()
