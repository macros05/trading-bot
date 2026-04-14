"""
Tests for exchange/client.py.

Run from project root:
    python -m unittest tests.test_exchange_client
"""
import importlib
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ohlcv(n: int = 5) -> list:
    return [[i * 1000, 100.0, 105.0, 99.0, 102.0, 10.0] for i in range(n)]


def _make_client(mock_exchange: MagicMock):
    """Instantiate BinanceClient with a fully mocked ccxt exchange."""
    env = {'BINANCE_API_KEY': 'test-key', 'BINANCE_API_SECRET': 'test-secret'}
    with patch.dict(os.environ, env):
        with patch('exchange.client.ccxt.binance', return_value=mock_exchange):
            import exchange.client as mod
            importlib.reload(mod)
            return mod.BinanceClient()


def _mock_exchange() -> MagicMock:
    ex = MagicMock()
    ex.set_sandbox_mode = MagicMock()
    ex.close = AsyncMock()
    return ex


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------

class TestFetchCandlesReturnShape(unittest.IsolatedAsyncioTestCase):

    async def test_returns_list_of_dicts(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(return_value=_fake_ohlcv(3))
        client = _make_client(ex)

        result = await client.fetch_candles()

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], dict)
        await client.close()

    async def test_candle_dict_has_all_fields(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(
            return_value=[[1_000_000, 100.0, 110.0, 90.0, 105.0, 50.0]]
        )
        client = _make_client(ex)

        result = await client.fetch_candles()
        candle = result[0]

        self.assertEqual(candle['ts'],     1_000_000)
        self.assertEqual(candle['open'],   100.0)
        self.assertEqual(candle['high'],   110.0)
        self.assertEqual(candle['low'],    90.0)
        self.assertEqual(candle['close'],  105.0)
        self.assertEqual(candle['volume'], 50.0)
        await client.close()

    async def test_forwards_symbol_timeframe_limit(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(return_value=_fake_ohlcv(10))
        client = _make_client(ex)

        await client.fetch_candles(symbol='ETH/USDT', timeframe='5m', limit=10)

        ex.fetch_ohlcv.assert_called_once_with('ETH/USDT', '5m', limit=10)
        await client.close()


# ---------------------------------------------------------------------------
# Retry — RateLimitExceeded
# ---------------------------------------------------------------------------

class TestRetryOnRateLimit(unittest.IsolatedAsyncioTestCase):

    async def test_succeeds_on_second_attempt(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(side_effect=[
            _ccxt.RateLimitExceeded('rate limit'),
            _fake_ohlcv(2),
        ])
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            result = await client.fetch_candles()

        self.assertEqual(len(result), 2)
        mock_sleep.assert_awaited_once()
        await client.close()

    async def test_exhausts_all_retries_and_raises(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(
            side_effect=_ccxt.RateLimitExceeded('rate limit')
        )
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(_ccxt.RateLimitExceeded):
                await client.fetch_candles()

        self.assertEqual(ex.fetch_ohlcv.call_count, 3)
        await client.close()

    async def test_exponential_backoff_delays(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(
            side_effect=_ccxt.RateLimitExceeded('rate limit')
        )
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(_ccxt.RateLimitExceeded):
                await client.fetch_candles()

        # attempts 1→2: delay=1s, attempts 2→3: delay=2s (no sleep after last)
        delays = [c.args[0] for c in mock_sleep.await_args_list]
        self.assertEqual(delays, [1.0, 2.0])
        await client.close()


# ---------------------------------------------------------------------------
# Retry — NetworkError
# ---------------------------------------------------------------------------

class TestRetryOnNetworkError(unittest.IsolatedAsyncioTestCase):

    async def test_succeeds_on_third_attempt(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(side_effect=[
            _ccxt.NetworkError('timeout'),
            _ccxt.NetworkError('timeout'),
            _fake_ohlcv(5),
        ])
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            result = await client.fetch_candles()

        self.assertEqual(len(result), 5)
        self.assertEqual(mock_sleep.await_count, 2)
        await client.close()

    async def test_exhausts_all_retries_and_raises(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(side_effect=_ccxt.NetworkError('timeout'))
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(_ccxt.NetworkError):
                await client.fetch_candles()

        self.assertEqual(ex.fetch_ohlcv.call_count, 3)
        await client.close()


# ---------------------------------------------------------------------------
# Non-retryable errors
# ---------------------------------------------------------------------------

class TestNonRetryableErrors(unittest.IsolatedAsyncioTestCase):

    async def test_exchange_error_propagates_immediately(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = AsyncMock(
            side_effect=_ccxt.ExchangeError('bad symbol')
        )
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(_ccxt.ExchangeError):
                await client.fetch_candles()

        # must not retry
        self.assertEqual(ex.fetch_ohlcv.call_count, 1)
        mock_sleep.assert_not_awaited()
        await client.close()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

class TestCredentials(unittest.TestCase):

    def test_missing_api_key_raises(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('BINANCE_API_KEY', 'BINANCE_API_SECRET')}
        with patch.dict(os.environ, clean, clear=True):
            with patch('exchange.client.ccxt.binance', return_value=_mock_exchange()):
                import exchange.client as mod
                importlib.reload(mod)
                with self.assertRaises(RuntimeError):
                    mod.BinanceClient()

    def test_missing_api_secret_raises(self):
        env = {'BINANCE_API_KEY': 'key-only'}
        env.update({k: v for k, v in os.environ.items()
                    if k not in ('BINANCE_API_KEY', 'BINANCE_API_SECRET')})
        with patch.dict(os.environ, env, clear=True):
            with patch('exchange.client.ccxt.binance', return_value=_mock_exchange()):
                import exchange.client as mod
                importlib.reload(mod)
                with self.assertRaises(RuntimeError):
                    mod.BinanceClient()


if __name__ == '__main__':
    unittest.main()
