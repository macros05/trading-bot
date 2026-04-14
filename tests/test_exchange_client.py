"""
Tests for exchange/client.py.

Run from project root:
    python -m pytest tests/test_exchange_client.py   # if pytest installed
    python -m unittest tests.test_exchange_client    # stdlib only
"""
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from data.candles import CandleBuffer


# ---------------------------------------------------------------------------
# CandleBuffer unit tests (no network, no credentials)
# ---------------------------------------------------------------------------

class TestCandleBuffer(unittest.TestCase):
    def _make_row(self, ts: int, close: float) -> list:
        return [ts, close - 1, close + 1, close - 2, close, 100.0]

    def test_append_and_len(self):
        buf = CandleBuffer(maxlen=5)
        for i in range(3):
            buf.append(self._make_row(i, float(i)))
        self.assertEqual(len(buf), 3)

    def test_maxlen_evicts_oldest(self):
        buf = CandleBuffer(maxlen=3)
        for i in range(5):
            buf.append(self._make_row(i, float(i)))
        self.assertEqual(len(buf), 3)
        self.assertEqual(buf[0]['close'], 2.0)  # oldest kept

    def test_closes(self):
        buf = CandleBuffer()
        for i in range(4):
            buf.append(self._make_row(i, float(i * 10)))
        self.assertEqual(buf.closes(), [0.0, 10.0, 20.0, 30.0])

    def test_clear(self):
        buf = CandleBuffer()
        buf.append(self._make_row(0, 1.0))
        buf.clear()
        self.assertEqual(len(buf), 0)

    def test_fields_parsed(self):
        buf = CandleBuffer()
        buf.append([1_000_000, 100.0, 110.0, 90.0, 105.0, 50.0])
        candle = buf[0]
        self.assertEqual(candle['ts'], 1_000_000)
        self.assertEqual(candle['open'], 100.0)
        self.assertEqual(candle['high'], 110.0)
        self.assertEqual(candle['low'], 90.0)
        self.assertEqual(candle['close'], 105.0)
        self.assertEqual(candle['volume'], 50.0)


# ---------------------------------------------------------------------------
# BinanceClient unit tests (exchange mocked — no real network calls)
# ---------------------------------------------------------------------------

class TestBinanceClientFetchCandles(unittest.IsolatedAsyncioTestCase):
    def _fake_ohlcv(self, n: int = 5) -> list:
        return [[i, 100.0, 105.0, 99.0, 102.0, 10.0] for i in range(n)]

    def _make_client(self, mock_exchange):
        """Build a BinanceClient with a fully mocked ccxt exchange."""
        env = {
            'BINANCE_API_KEY': 'test-key',
            'BINANCE_API_SECRET': 'test-secret',
        }
        with patch.dict(os.environ, env):
            with patch('exchange.client.ccxt.binance', return_value=mock_exchange):
                from exchange.client import BinanceClient
                return BinanceClient()

    async def test_fetch_candles_populates_buffer(self):
        mock_ex = MagicMock()
        mock_ex.set_sandbox_mode = MagicMock()
        mock_ex.fetch_ohlcv = AsyncMock(return_value=self._fake_ohlcv(10))
        mock_ex.close = AsyncMock()

        client = self._make_client(mock_ex)
        buf = await client.fetch_candles(limit=10)

        self.assertIsInstance(buf, CandleBuffer)
        self.assertEqual(len(buf), 10)
        await client.close()

    async def test_fetch_candles_clears_previous_data(self):
        import ccxt
        mock_ex = MagicMock()
        mock_ex.set_sandbox_mode = MagicMock()
        mock_ex.fetch_ohlcv = AsyncMock(return_value=self._fake_ohlcv(3))
        mock_ex.close = AsyncMock()

        client = self._make_client(mock_ex)
        # First fetch
        await client.fetch_candles(limit=3)
        # Second fetch with different data — buffer must not accumulate
        mock_ex.fetch_ohlcv = AsyncMock(return_value=self._fake_ohlcv(2))
        buf = await client.fetch_candles(limit=2)

        self.assertEqual(len(buf), 2)
        await client.close()

    async def test_network_error_propagates(self):
        import ccxt as _ccxt
        mock_ex = MagicMock()
        mock_ex.set_sandbox_mode = MagicMock()
        mock_ex.fetch_ohlcv = AsyncMock(
            side_effect=_ccxt.NetworkError('timeout')
        )
        mock_ex.close = AsyncMock()

        client = self._make_client(mock_ex)
        with self.assertRaises(_ccxt.NetworkError):
            await client.fetch_candles()
        await client.close()

    def test_missing_credentials_raises(self):
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ('BINANCE_API_KEY', 'BINANCE_API_SECRET')}
        mock_ex = MagicMock()
        with patch.dict(os.environ, clean_env, clear=True):
            with patch('exchange.client.ccxt.binance', return_value=mock_ex):
                from exchange import client as _mod
                import importlib
                importlib.reload(_mod)
                with self.assertRaises(RuntimeError):
                    _mod.BinanceClient()


if __name__ == '__main__':
    unittest.main()
