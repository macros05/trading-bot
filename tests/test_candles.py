"""
Tests for data/candles.py.

Run from project root:
    python -m unittest tests.test_candles
"""
import sys
import os
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from data.candles import CandleBuffer


def _candle(ts: int = 0, close: float = 100.0) -> dict:
    return {'ts': ts, 'open': close - 1, 'high': close + 1,
            'low': close - 2, 'close': close, 'volume': 10.0}


def _candles(n: int, base_close: float = 100.0) -> list[dict]:
    return [_candle(ts=i, close=base_close + i) for i in range(n)]


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestAdd(unittest.TestCase):

    def test_add_increases_len(self):
        buf = CandleBuffer()
        buf.add(_candle())
        self.assertEqual(len(buf), 1)

    def test_add_stores_candle(self):
        buf = CandleBuffer()
        candle = _candle(ts=999, close=50.0)
        buf.add(candle)
        self.assertEqual(list(buf)[0], candle)

    def test_add_evicts_oldest_when_full(self):
        buf = CandleBuffer(maxlen=3)
        for i in range(4):
            buf.add(_candle(ts=i, close=float(i)))
        self.assertEqual(len(buf), 3)
        self.assertEqual(list(buf)[0]['ts'], 1)  # ts=0 evicted


# ---------------------------------------------------------------------------
# add_many
# ---------------------------------------------------------------------------

class TestAddMany(unittest.TestCase):

    def test_add_many_loads_all(self):
        buf = CandleBuffer()
        buf.add_many(_candles(10))
        self.assertEqual(len(buf), 10)

    def test_add_many_respects_maxlen(self):
        buf = CandleBuffer(maxlen=5)
        buf.add_many(_candles(8))
        self.assertEqual(len(buf), 5)

    def test_add_many_preserves_order(self):
        buf = CandleBuffer()
        candles = _candles(3)
        buf.add_many(candles)
        stored = list(buf)
        self.assertEqual(stored[0]['ts'], 0)
        self.assertEqual(stored[2]['ts'], 2)

    def test_add_many_empty_list_is_noop(self):
        buf = CandleBuffer()
        buf.add_many([])
        self.assertEqual(len(buf), 0)

    def test_add_many_accumulates_after_add(self):
        buf = CandleBuffer()
        buf.add(_candle(ts=0))
        buf.add_many(_candles(3, base_close=200.0))
        self.assertEqual(len(buf), 4)


# ---------------------------------------------------------------------------
# to_dataframe
# ---------------------------------------------------------------------------

class TestToDataframe(unittest.TestCase):

    def test_returns_dataframe(self):
        buf = CandleBuffer()
        buf.add_many(_candles(5))
        self.assertIsInstance(buf.to_dataframe(), pd.DataFrame)

    def test_has_correct_columns(self):
        buf = CandleBuffer()
        buf.add(_candle())
        df = buf.to_dataframe()
        self.assertListEqual(list(df.columns), ['ts', 'open', 'high', 'low', 'close', 'volume'])

    def test_row_count_matches_buffer(self):
        buf = CandleBuffer()
        buf.add_many(_candles(7))
        self.assertEqual(len(buf.to_dataframe()), 7)

    def test_values_are_correct(self):
        buf = CandleBuffer()
        buf.add(_candle(ts=42, close=300.0))
        df = buf.to_dataframe()
        self.assertEqual(df.iloc[0]['ts'], 42)
        self.assertEqual(df.iloc[0]['close'], 300.0)

    def test_empty_buffer_returns_empty_dataframe(self):
        buf = CandleBuffer()
        df = buf.to_dataframe()
        self.assertEqual(len(df), 0)
        self.assertListEqual(list(df.columns), ['ts', 'open', 'high', 'low', 'close', 'volume'])

    def test_does_not_mutate_buffer(self):
        buf = CandleBuffer()
        buf.add_many(_candles(3))
        buf.to_dataframe()
        self.assertEqual(len(buf), 3)


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------

class TestIsReady(unittest.TestCase):

    def test_false_when_empty(self):
        self.assertFalse(CandleBuffer().is_ready(14))

    def test_false_when_below_period(self):
        buf = CandleBuffer()
        buf.add_many(_candles(10))
        self.assertFalse(buf.is_ready(14))

    def test_true_when_exactly_period(self):
        buf = CandleBuffer()
        buf.add_many(_candles(14))
        self.assertTrue(buf.is_ready(14))

    def test_true_when_above_period(self):
        buf = CandleBuffer()
        buf.add_many(_candles(50))
        self.assertTrue(buf.is_ready(14))

    def test_period_one_requires_one_candle(self):
        buf = CandleBuffer()
        self.assertFalse(buf.is_ready(1))
        buf.add(_candle())
        self.assertTrue(buf.is_ready(1))


if __name__ == '__main__':
    unittest.main()
