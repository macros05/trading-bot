"""
Tests for strategy/indicators.py.

Run from project root:
    python -m unittest tests.test_indicators
"""
import math
import sys
import os
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy.indicators import sma, ema, rsi


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({'close': closes})


class TestSma(unittest.TestCase):

    def test_returns_series(self):
        result = sma(_df([1.0, 2.0, 3.0, 4.0, 5.0]), period=3)
        self.assertIsInstance(result, pd.Series)

    def test_correct_value(self):
        result = sma(_df([1.0, 2.0, 3.0, 4.0, 5.0]), period=3)
        self.assertAlmostEqual(result.iloc[2], 2.0)
        self.assertAlmostEqual(result.iloc[4], 4.0)

    def test_leading_values_are_nan(self):
        result = sma(_df([1.0, 2.0, 3.0, 4.0]), period=3)
        self.assertTrue(math.isnan(result.iloc[0]))
        self.assertTrue(math.isnan(result.iloc[1]))
        self.assertFalse(math.isnan(result.iloc[2]))

    def test_period_equals_length(self):
        result = sma(_df([2.0, 4.0, 6.0]), period=3)
        self.assertAlmostEqual(result.iloc[2], 4.0)

    def test_does_not_mutate_input(self):
        df = _df([1.0, 2.0, 3.0])
        original_values = df['close'].tolist()
        sma(df, period=2)
        self.assertEqual(df['close'].tolist(), original_values)


class TestEma(unittest.TestCase):

    def test_returns_series(self):
        result = ema(_df([1.0, 2.0, 3.0, 4.0, 5.0]), period=3)
        self.assertIsInstance(result, pd.Series)

    def test_leading_values_are_nan(self):
        result = ema(_df([1.0, 2.0, 3.0, 4.0]), period=3)
        self.assertTrue(math.isnan(result.iloc[0]))
        self.assertTrue(math.isnan(result.iloc[1]))
        self.assertFalse(math.isnan(result.iloc[2]))

    def test_ema_reacts_faster_than_sma_on_uptrend(self):
        # With rising prices EMA (weighted toward recent) > SMA at the last bar
        closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
        df = _df(closes)
        period = 3
        ema_val = ema(df, period=period).iloc[-1]
        sma_val = sma(df, period=period).iloc[-1]
        self.assertGreater(ema_val, sma_val)

    def test_does_not_mutate_input(self):
        df = _df([1.0, 2.0, 3.0])
        original_values = df['close'].tolist()
        ema(df, period=2)
        self.assertEqual(df['close'].tolist(), original_values)


class TestRsi(unittest.TestCase):

    def _rising(self, n: int = 30) -> pd.DataFrame:
        return _df([float(i) for i in range(1, n + 1)])

    def _falling(self, n: int = 30) -> pd.DataFrame:
        return _df([float(n - i) for i in range(n)])

    def _flat(self, n: int = 30) -> pd.DataFrame:
        return _df([50.0] * n)

    def test_returns_series(self):
        result = rsi(self._rising(), period=14)
        self.assertIsInstance(result, pd.Series)

    def test_leading_values_are_nan(self):
        result = rsi(_df([1.0] * 20), period=14)
        for i in range(14):
            self.assertTrue(math.isnan(result.iloc[i]), f'index {i} should be NaN')

    def test_bounds_on_steady_rise(self):
        result = rsi(self._rising(30), period=14)
        valid = result.dropna()
        self.assertTrue((valid >= 0).all())
        self.assertTrue((valid <= 100).all())

    def test_overbought_on_pure_uptrend(self):
        # Constant daily gains → RSI should be very high (> 90)
        result = rsi(self._rising(30), period=14)
        self.assertGreater(result.iloc[-1], 90.0)

    def test_oversold_on_pure_downtrend(self):
        result = rsi(self._falling(30), period=14)
        self.assertLess(result.iloc[-1], 10.0)

    def test_flat_prices_produce_nan(self):
        # No gains and no losses → 0/0 → RSI undefined (NaN)
        result = rsi(self._flat(30), period=14)
        self.assertTrue(result.dropna().isna().all() or result.iloc[-1] != result.iloc[-1])

    def test_does_not_mutate_input(self):
        df = self._rising()
        original_values = df['close'].tolist()
        rsi(df, period=14)
        self.assertEqual(df['close'].tolist(), original_values)

    def test_default_period_is_14(self):
        df = self._rising(30)
        self.assertTrue(rsi(df).equals(rsi(df, period=14)))


if __name__ == '__main__':
    unittest.main()
