"""
Tests for exchange/client.py.

Run from project root:
    python -m unittest tests.test_exchange_client
"""
import asyncio
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
    """Instantiate BinanceClient with a fully mocked ccxt futures exchange."""
    env = {
        'BINANCE_FUTURES_API_KEY': 'test-key',
        'BINANCE_FUTURES_API_SECRET': 'test-secret',
    }
    with patch.dict(os.environ, env):
        with patch('exchange.client.ccxt.binanceusdm', return_value=mock_exchange):
            import exchange.client as mod
            importlib.reload(mod)
            return mod.BinanceClient()


def _mock_exchange() -> MagicMock:
    ex = MagicMock()
    ex.set_sandbox_mode = MagicMock()
    ex.set_leverage = MagicMock()
    ex.close = MagicMock()
    # fetch_ohlcv is sync — called via run_in_executor
    ex.fetch_ohlcv = MagicMock(return_value=_fake_ohlcv())
    return ex


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------

class TestFetchCandlesReturnShape(unittest.IsolatedAsyncioTestCase):

    async def test_returns_list_of_dicts(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(return_value=_fake_ohlcv(3))
        client = _make_client(ex)

        result = await client.fetch_candles()

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], dict)

    async def test_candle_dict_has_all_fields(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(
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

    async def test_forwards_symbol_timeframe_limit(self):
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(return_value=_fake_ohlcv(10))
        client = _make_client(ex)

        await client.fetch_candles(symbol='ETH/USDT', timeframe='5m', limit=10)

        ex.fetch_ohlcv.assert_called_once_with('ETH/USDT', '5m', limit=10)


# ---------------------------------------------------------------------------
# Retry — RateLimitExceeded
# ---------------------------------------------------------------------------

class TestRetryOnRateLimit(unittest.IsolatedAsyncioTestCase):

    async def test_succeeds_on_second_attempt(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=[
            _ccxt.RateLimitExceeded('rate limit'),
            _fake_ohlcv(2),
        ])
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep') as mock_sleep:
            mock_sleep.return_value = None
            result = await client.fetch_candles()

        self.assertEqual(len(result), 2)
        mock_sleep.assert_called_once()

    async def test_exhausts_all_retries_and_raises(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=_ccxt.RateLimitExceeded('rate limit'))
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep'):
            with self.assertRaises(_ccxt.RateLimitExceeded):
                await client.fetch_candles()

        self.assertEqual(ex.fetch_ohlcv.call_count, 3)

    async def test_exponential_backoff_delays(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=_ccxt.RateLimitExceeded('rate limit'))
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep') as mock_sleep:
            mock_sleep.return_value = None
            with self.assertRaises(_ccxt.RateLimitExceeded):
                await client.fetch_candles()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [1.0, 2.0])


# ---------------------------------------------------------------------------
# Retry — NetworkError
# ---------------------------------------------------------------------------

class TestRetryOnNetworkError(unittest.IsolatedAsyncioTestCase):

    async def test_succeeds_on_third_attempt(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=[
            _ccxt.NetworkError('timeout'),
            _ccxt.NetworkError('timeout'),
            _fake_ohlcv(5),
        ])
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep') as mock_sleep:
            mock_sleep.return_value = None
            result = await client.fetch_candles()

        self.assertEqual(len(result), 5)
        self.assertEqual(mock_sleep.call_count, 2)

    async def test_exhausts_all_retries_and_raises(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=_ccxt.NetworkError('timeout'))
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep'):
            with self.assertRaises(_ccxt.NetworkError):
                await client.fetch_candles()

        self.assertEqual(ex.fetch_ohlcv.call_count, 3)


# ---------------------------------------------------------------------------
# Non-retryable errors
# ---------------------------------------------------------------------------

class TestNonRetryableErrors(unittest.IsolatedAsyncioTestCase):

    async def test_exchange_error_propagates_immediately(self):
        import ccxt as _ccxt
        ex = _mock_exchange()
        ex.fetch_ohlcv = MagicMock(side_effect=_ccxt.ExchangeError('bad symbol'))
        client = _make_client(ex)

        with patch('exchange.client.asyncio.sleep') as mock_sleep:
            with self.assertRaises(_ccxt.ExchangeError):
                await client.fetch_candles()

        self.assertEqual(ex.fetch_ohlcv.call_count, 1)
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

class TestCredentials(unittest.TestCase):

    def test_missing_api_key_raises(self):
        def _getenv_no_key(var: str, default=None):
            return None if var == 'BINANCE_FUTURES_API_KEY' else os.environ.get(var, default)

        with patch('exchange.client.os.getenv', side_effect=_getenv_no_key):
            from exchange.client import BinanceClient
            with self.assertRaises(RuntimeError):
                BinanceClient()

    def test_missing_api_secret_raises(self):
        def _getenv_no_secret(var: str, default=None):
            return None if var == 'BINANCE_FUTURES_API_SECRET' else os.environ.get(var, default)

        with patch('exchange.client.os.getenv', side_effect=_getenv_no_secret):
            from exchange.client import BinanceClient
            with self.assertRaises(RuntimeError):
                BinanceClient()


# ---------------------------------------------------------------------------
# watch_candles — WebSocket stream
# ---------------------------------------------------------------------------

class TestWatchCandles(unittest.IsolatedAsyncioTestCase):
    """Tests for BinanceClient.watch_candles WebSocket streaming.

    The ccxt.pro exchange is injected via _ensure_pro_exchange so tests do
    not hit the network.
    """

    def _make_ws_client(self, pro_exchange) -> 'BinanceClient':  # type: ignore[name-defined]
        client = _make_client(_mock_exchange())
        client._ensure_pro_exchange = MagicMock(return_value=pro_exchange)
        return client

    def _mock_pro(self, side_effect=None, rows=None):
        pro = MagicMock()
        if rows is not None:
            pro.watch_ohlcv = AsyncMock(return_value=rows)
        elif side_effect is not None:
            pro.watch_ohlcv = AsyncMock(side_effect=side_effect)
        return pro

    async def test_callback_receives_candle_dicts(self):
        """watch_ohlcv rows are converted to {ts, open, high, low, close, volume} dicts."""
        rows = [[1_000_000, 100.0, 110.0, 90.0, 105.0, 50.0]]
        pro  = self._mock_pro(rows=rows)
        received: list = []

        async def _callback(candles):
            received.extend(candles)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self._make_ws_client(pro).watch_candles('BTC/USDT', '1m', _callback)

        self.assertEqual(len(received), 1)
        c = received[0]
        self.assertEqual(c['ts'],     1_000_000)
        self.assertEqual(c['open'],   100.0)
        self.assertEqual(c['close'],  105.0)
        self.assertEqual(c['volume'], 50.0)

    async def test_reconnects_after_network_error(self):
        """One NetworkError → reconnect → callback fires on second attempt."""
        import ccxt as _ccxt
        rows     = [[1000, 1, 2, 0.5, 1.5, 10.0]]
        call_count = 0

        async def _fail_then_succeed(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _ccxt.NetworkError('connection lost')
            return rows

        pro = MagicMock()
        pro.watch_ohlcv = _fail_then_succeed
        received: list = []

        async def _callback(candles):
            received.extend(candles)
            raise asyncio.CancelledError

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await self._make_ws_client(pro).watch_candles('BTC/USDT', '1m', _callback)

        self.assertEqual(call_count, 2)
        self.assertEqual(len(received), 1)

    async def test_falls_back_to_rest_after_max_failures(self):
        """_WS_MAX_FAILURES consecutive errors → REST polling callback fires."""
        import ccxt as _ccxt
        from exchange.client import _WS_MAX_FAILURES

        pro = self._mock_pro(side_effect=_ccxt.NetworkError('down'))
        client = self._make_ws_client(pro)
        client._exchange.fetch_ohlcv = MagicMock(return_value=_fake_ohlcv(3))

        received: list = []

        async def _callback(candles):
            received.extend(candles)
            raise asyncio.CancelledError

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await client.watch_candles('BTC/USDT', '1m', _callback, rest_interval=0.0)

        self.assertEqual(pro.watch_ohlcv.call_count, _WS_MAX_FAILURES)
        self.assertEqual(len(received), 3)

    async def test_timeout_triggers_reconnect_not_immediate_fallback(self):
        """A single TimeoutError causes reconnect; does not immediately fall back."""
        rows = [[1000, 1, 2, 0.5, 1.5, 10.0]]
        call_count = 0

        async def _timeout_then_data(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError
            return rows

        pro = MagicMock()
        pro.watch_ohlcv = _timeout_then_data
        received: list = []

        async def _callback(candles):
            received.extend(candles)
            raise asyncio.CancelledError

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await self._make_ws_client(pro).watch_candles('BTC/USDT', '1m', _callback)

        self.assertEqual(call_count, 2)
        self.assertEqual(len(received), 1)

    async def test_cancelled_error_propagates_immediately(self):
        """CancelledError from watch_ohlcv is never swallowed."""
        pro = self._mock_pro(side_effect=asyncio.CancelledError)

        with self.assertRaises(asyncio.CancelledError):
            await self._make_ws_client(pro).watch_candles(
                'BTC/USDT', '1m', lambda c: None,
            )

    async def test_import_error_falls_back_to_rest_immediately(self):
        """If ccxt.pro cannot be imported, fall back to REST without retrying."""
        client = _make_client(_mock_exchange())
        client._ensure_pro_exchange = MagicMock(side_effect=ImportError('no ccxt.pro'))
        client._exchange.fetch_ohlcv = MagicMock(return_value=_fake_ohlcv(2))

        received: list = []

        async def _callback(candles):
            received.extend(candles)
            raise asyncio.CancelledError

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await client.watch_candles('BTC/USDT', '1m', _callback, rest_interval=0.0)

        # _ensure_pro_exchange called exactly once (no WS retries on ImportError)
        client._ensure_pro_exchange.assert_called_once()
        self.assertEqual(len(received), 2)

    async def test_reconnects_after_drop_with_data_received(self):
        """Disconnect after data resets backoff and reconnects without fallback."""
        rows = [[1000, 1, 2, 0.5, 1.5, 10.0]]
        import ccxt as _ccxt
        call_count = 0

        async def _data_then_drop(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return rows                         # first call: success
            if call_count == 2:
                raise _ccxt.NetworkError('drop')    # drop after data
            return rows                             # recovery

        pro = MagicMock()
        pro.watch_ohlcv = _data_then_drop
        received: list = []

        async def _callback(candles):
            received.extend(candles)
            if len(received) >= 2:
                raise asyncio.CancelledError

        with patch('exchange.client.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await self._make_ws_client(pro).watch_candles('BTC/USDT', '1m', _callback)

        self.assertGreaterEqual(len(received), 2)


# ---------------------------------------------------------------------------
# Futures migration — TestBinanceClientFutures
# ---------------------------------------------------------------------------

class TestBinanceClientFutures(unittest.TestCase):
    def setUp(self):
        import os
        os.environ['BINANCE_FUTURES_API_KEY'] = 'fake_key'
        os.environ['BINANCE_FUTURES_API_SECRET'] = 'fake_secret'
        # Also keep spot keys to avoid breaking other tests if they share env.
        os.environ.setdefault('BINANCE_API_KEY', 'fake_spot')
        os.environ.setdefault('BINANCE_API_SECRET', 'fake_spot')

    def test_uses_binanceusdm_class(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2)
            mock_class.assert_called_once()
            ctor_kwargs = mock_class.call_args[0][0]
            self.assertEqual(ctor_kwargs['options']['defaultType'], 'future')

    def test_calls_set_sandbox_mode_true(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2)
            mock_inst.set_sandbox_mode.assert_called_once_with(True)

    def test_calls_set_leverage_with_configured_value(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2, symbol='BTC/USDT')
            mock_inst.set_leverage.assert_called_once_with(2, 'BTC/USDT')

    def test_set_leverage_failure_logged_but_not_fatal(self):
        """set_leverage may fail in dry-run/test contexts; client must still init."""
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_inst.set_leverage.side_effect = Exception('test exchange error')
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            # Should NOT raise — just log a warning
            client = BinanceClient(leverage=2)
            self.assertIsNotNone(client)

    def test_raises_when_futures_credentials_missing(self):
        import os
        from unittest.mock import patch, MagicMock
        # Temporarily remove futures key
        saved = os.environ.pop('BINANCE_FUTURES_API_KEY', None)
        try:
            with patch('ccxt.binanceusdm'):
                from exchange.client import BinanceClient
                with self.assertRaises(RuntimeError):
                    BinanceClient(leverage=2)
        finally:
            if saved is not None:
                os.environ['BINANCE_FUTURES_API_KEY'] = saved


if __name__ == '__main__':
    unittest.main()
