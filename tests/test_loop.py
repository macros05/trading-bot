"""
Tests for core/loop.py.

Each test runs exactly one loop iteration by patching asyncio.sleep to raise
CancelledError on the first call, which is the natural way to stop an asyncio
task from outside.

Run from project root:
    python -m unittest tests.test_loop
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.loop import trading_loop
from core.state import BotState
from data.candles import CandleBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config() -> dict:
    return {
        'symbol': 'BTC/USDT',
        'timeframe': '1m',
        'limit': 200,
        'interval_seconds': 60,
    }


def _candles(n: int, close: float = 100.0, rsi_signal: bool = False) -> list[dict]:
    """Return *n* candle dicts.

    When rsi_signal=True the last candle has a low close (20.0) to push RSI
    toward oversold territory while keeping close < sma on a rising series.
    """
    return [
        {'ts': i, 'open': close - 1, 'high': close + 1,
         'low': close - 2, 'close': close, 'volume': 10.0}
        for i in range(n)
    ]


def _mock_client(candles: list[dict]) -> MagicMock:
    client = MagicMock()
    client.fetch_candles = AsyncMock(return_value=candles)
    return client


def _mock_state(state: BotState = BotState.WAITING_SIGNAL) -> MagicMock:
    sm = MagicMock()
    sm.get_state.return_value = state
    return sm


def _mock_risk(circuit_breaker: bool = False, daily_pnl: float = 0.0) -> MagicMock:
    rm = MagicMock()
    rm.is_circuit_breaker_active.return_value = circuit_breaker
    rm.get_daily_pnl.return_value = daily_pnl
    return rm


async def _run_one_tick(client, buffer, state_manager, risk_manager, config=None):
    """Run the loop for exactly one iteration (sleep raises CancelledError)."""
    cfg = config or _config()
    with patch('core.loop.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = asyncio.CancelledError
        with unittest.TestCase().assertRaises(asyncio.CancelledError):
            await trading_loop(client, buffer, state_manager, risk_manager, cfg)
    return mock_sleep


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):

    async def test_skips_fetch_when_active(self):
        client = _mock_client(_candles(25))
        mock_sleep = await _run_one_tick(
            client, CandleBuffer(), _mock_state(),
            _mock_risk(circuit_breaker=True, daily_pnl=-0.05),
        )
        client.fetch_candles.assert_not_called()

    async def test_sleeps_interval_when_active(self):
        mock_sleep = await _run_one_tick(
            _mock_client([]), CandleBuffer(), _mock_state(),
            _mock_risk(circuit_breaker=True, daily_pnl=-0.05),
        )
        mock_sleep.assert_awaited_once_with(60)


# ---------------------------------------------------------------------------
# Network errors — loop must not break
# ---------------------------------------------------------------------------

class TestNetworkErrors(unittest.IsolatedAsyncioTestCase):

    async def _run_with_fetch_error(self, error):
        import ccxt as _ccxt
        client = MagicMock()
        client.fetch_candles = AsyncMock(side_effect=error)
        mock_sleep = await _run_one_tick(client, CandleBuffer(), _mock_state(), _mock_risk())
        return mock_sleep

    async def test_network_error_does_not_propagate(self):
        import ccxt as _ccxt
        # If it propagated, assertRaises(CancelledError) inside _run_one_tick would fail
        await self._run_with_fetch_error(_ccxt.NetworkError('timeout'))

    async def test_rate_limit_does_not_propagate(self):
        import ccxt as _ccxt
        await self._run_with_fetch_error(_ccxt.RateLimitExceeded('rate limit'))

    async def test_sleeps_after_network_error(self):
        import ccxt as _ccxt
        mock_sleep = await self._run_with_fetch_error(_ccxt.NetworkError('timeout'))
        mock_sleep.assert_awaited_once_with(60)


# ---------------------------------------------------------------------------
# Buffer not ready
# ---------------------------------------------------------------------------

class TestBufferNotReady(unittest.IsolatedAsyncioTestCase):

    async def test_sleeps_without_evaluating_signal(self):
        # Only 5 candles — not enough for SMA20 or RSI14
        state_manager = _mock_state()
        await _run_one_tick(
            _mock_client(_candles(5)), CandleBuffer(), state_manager, _mock_risk(),
        )
        # get_state should never be called if buffer is not ready
        state_manager.get_state.assert_not_called()


# ---------------------------------------------------------------------------
# Signal evaluation — WAITING_SIGNAL
# ---------------------------------------------------------------------------

class TestBuySignal(unittest.IsolatedAsyncioTestCase):

    def _oversold_candles(self, n: int = 50) -> list[dict]:
        """Falling prices → RSI oversold. Last close is low, sma is above it."""
        candles = []
        price = 200.0
        for i in range(n):
            price -= 1.0  # constant decline
            candles.append({
                'ts': i, 'open': price + 0.5, 'high': price + 1,
                'low': price - 1, 'close': price, 'volume': 10.0,
            })
        return candles

    def _overbought_candles(self, n: int = 50) -> list[dict]:
        """Rising prices → RSI overbought, close above SMA."""
        candles = []
        price = 100.0
        for i in range(n):
            price += 1.0
            candles.append({
                'ts': i, 'open': price - 0.5, 'high': price + 1,
                'low': price - 1, 'close': price, 'volume': 10.0,
            })
        return candles

    async def test_no_signal_when_state_is_in_position(self):
        state_manager = _mock_state(BotState.IN_POSITION)
        # Even with oversold candles, no signal if not WAITING_SIGNAL
        await _run_one_tick(
            _mock_client(self._oversold_candles()), CandleBuffer(),
            state_manager, _mock_risk(),
        )
        # get_state called during tick but set_state must not be called
        state_manager.set_state.assert_not_called()

    async def test_no_signal_when_rsi_above_threshold(self):
        # Rising candles → RSI high, close > SMA → condition not met
        with patch('core.loop.logger') as mock_logger:
            await _run_one_tick(
                _mock_client(self._overbought_candles()), CandleBuffer(),
                _mock_state(BotState.WAITING_SIGNAL), _mock_risk(),
            )
            info_messages = [str(c) for c in mock_logger.info.call_args_list]
            self.assertFalse(any('buy_signal' in m for m in info_messages))

    async def test_fetch_called_with_config_params(self):
        client = _mock_client(_candles(25))
        await _run_one_tick(client, CandleBuffer(), _mock_state(), _mock_risk())
        client.fetch_candles.assert_called_once_with(
            symbol='BTC/USDT', timeframe='1m', limit=200,
        )

    async def test_buffer_populated_after_fetch(self):
        buffer = CandleBuffer()
        await _run_one_tick(
            _mock_client(_candles(25)), buffer, _mock_state(), _mock_risk(),
        )
        self.assertEqual(len(buffer), 25)

    async def test_sleeps_interval_after_normal_tick(self):
        mock_sleep = await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), _mock_state(), _mock_risk(),
        )
        mock_sleep.assert_awaited_once_with(60)


if __name__ == '__main__':
    unittest.main()
