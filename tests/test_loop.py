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
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.loop import trading_loop
from core.state import BotState
from data.candles import CandleBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config() -> dict:
    return {
        'symbol':          'BTC/USDT',
        'timeframe':       '1m',
        'limit':           200,
        'interval_seconds': 60,
        'paper_balance':   10_000.0,
        'risk_pct':        0.01,
        'stop_loss_pct':   0.02,
        'take_profit_pct': 0.03,
    }


def _candles(n: int, close: float = 100.0) -> list[dict]:
    return [
        {'ts': i, 'open': close - 1, 'high': close + 1,
         'low': close - 2, 'close': close, 'volume': 10.0}
        for i in range(n)
    ]


def _mock_client(candles: list[dict]) -> MagicMock:
    client = MagicMock()
    client.fetch_candles = AsyncMock(return_value=candles)
    return client


def _mock_state(
    state: BotState = BotState.WAITING_SIGNAL,
    position: dict | None = None,
) -> MagicMock:
    sm = MagicMock()
    sm.get_state.return_value = state
    sm.get_position.return_value = position
    return sm


def _mock_risk(
    circuit_breaker: bool = False,
    daily_pnl: float = 0.0,
    position_size: float = 100.0,
) -> MagicMock:
    rm = MagicMock()
    rm.is_circuit_breaker_active.return_value = circuit_breaker
    rm.get_daily_pnl.return_value = daily_pnl
    rm.position_size.return_value = position_size
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
        await _run_one_tick(
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
        client = MagicMock()
        client.fetch_candles = AsyncMock(side_effect=error)
        return await _run_one_tick(client, CandleBuffer(), _mock_state(), _mock_risk())

    async def test_network_error_does_not_propagate(self):
        import ccxt as _ccxt
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
        state_manager = _mock_state()
        await _run_one_tick(
            _mock_client(_candles(5)), CandleBuffer(), state_manager, _mock_risk(),
        )
        state_manager.get_state.assert_not_called()


# ---------------------------------------------------------------------------
# WAITING_SIGNAL — entry
# ---------------------------------------------------------------------------

class TestEntrySignal(unittest.IsolatedAsyncioTestCase):

    async def test_opens_position_when_signal_fires(self):
        """When should_enter returns True, state transitions to IN_POSITION."""
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        risk = _mock_risk(position_size=100.0)

        with patch('core.loop.should_enter', return_value=True), \
             patch('core.loop._save_trade'):
            await _run_one_tick(
                _mock_client(_candles(25, close=105.0)),
                CandleBuffer(), state_manager, risk,
            )

        state_manager.set_state.assert_called_once_with(BotState.IN_POSITION)
        state_manager.set_position.assert_called_once()
        position = state_manager.set_position.call_args[0][0]
        self.assertAlmostEqual(position['entry_price'], 105.0)
        self.assertGreater(position['qty'], 0)

    async def test_no_position_when_signal_does_not_fire(self):
        state_manager = _mock_state(BotState.WAITING_SIGNAL)

        with patch('core.loop.should_enter', return_value=False):
            await _run_one_tick(
                _mock_client(_candles(25)), CandleBuffer(),
                state_manager, _mock_risk(),
            )

        state_manager.set_state.assert_not_called()
        state_manager.set_position.assert_not_called()

    async def test_no_position_when_position_size_is_zero(self):
        """Circuit breaker makes position_size return 0; no trade should open."""
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        risk = _mock_risk(position_size=0.0)

        with patch('core.loop.should_enter', return_value=True):
            await _run_one_tick(
                _mock_client(_candles(25, close=105.0)),
                CandleBuffer(), state_manager, risk,
            )

        state_manager.set_state.assert_not_called()

    async def test_qty_computed_from_position_size_and_close(self):
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        risk = _mock_risk(position_size=200.0)  # 200 USDT / 100 close = 2.0 BTC

        with patch('core.loop.should_enter', return_value=True), \
             patch('core.loop._save_trade'):
            await _run_one_tick(
                _mock_client(_candles(25, close=100.0)),
                CandleBuffer(), state_manager, risk,
            )

        position = state_manager.set_position.call_args[0][0]
        self.assertAlmostEqual(position['qty'], 2.0)


# ---------------------------------------------------------------------------
# IN_POSITION — monitoring and exit
# ---------------------------------------------------------------------------

class TestInPosition(unittest.IsolatedAsyncioTestCase):

    def _position(self, entry: float = 100.0, qty: float = 1.0) -> dict:
        return {'entry_price': entry, 'qty': qty, 'ts': 0}

    async def test_logs_unrealized_pnl_each_tick(self):
        state_manager = _mock_state(BotState.IN_POSITION, position=self._position(100.0, 1.0))

        with patch('core.loop.check_exit', return_value=None), \
             patch('core.loop.logger') as mock_logger:
            await _run_one_tick(
                _mock_client(_candles(25, close=101.0)),
                CandleBuffer(), state_manager, _mock_risk(),
            )

        info_calls = ' '.join(str(c) for c in mock_logger.info.call_args_list)
        self.assertIn('unrealized_pnl', info_calls)

    async def test_closes_on_stop_loss(self):
        position = self._position(entry=100.0, qty=1.0)
        state_manager = _mock_state(BotState.IN_POSITION, position=position)
        risk = _mock_risk()

        with patch('core.loop.check_exit', return_value='stop_loss'), \
             patch('core.loop._save_trade') as mock_save:
            await _run_one_tick(
                _mock_client(_candles(25, close=97.0)),
                CandleBuffer(), state_manager, risk,
            )

        state_manager.set_state.assert_called_once_with(BotState.WAITING_SIGNAL)
        state_manager.set_position.assert_called_once_with(None)
        risk.register_trade.assert_called_once()
        mock_save.assert_called_once()

    async def test_closes_on_take_profit(self):
        position = self._position(entry=100.0, qty=1.0)
        state_manager = _mock_state(BotState.IN_POSITION, position=position)
        risk = _mock_risk()

        with patch('core.loop.check_exit', return_value='take_profit'), \
             patch('core.loop._save_trade') as mock_save:
            await _run_one_tick(
                _mock_client(_candles(25, close=104.0)),
                CandleBuffer(), state_manager, risk,
            )

        state_manager.set_state.assert_called_once_with(BotState.WAITING_SIGNAL)
        state_manager.set_position.assert_called_once_with(None)
        risk.register_trade.assert_called_once()
        mock_save.assert_called_once()

    async def test_pnl_registered_as_fraction(self):
        """register_trade receives pnl_pct/100 (fraction, not percentage)."""
        position = self._position(entry=100.0, qty=1.0)
        state_manager = _mock_state(BotState.IN_POSITION, position=position)
        risk = _mock_risk()

        with patch('core.loop.check_exit', return_value='take_profit'), \
             patch('core.loop.calc_pnl', return_value=(3.0, 3.0)), \
             patch('core.loop._save_trade'):
            await _run_one_tick(
                _mock_client(_candles(25, close=103.0)),
                CandleBuffer(), state_manager, risk,
            )

        # pnl_pct=3.0 → register_trade(3.0/100) = 0.03
        risk.register_trade.assert_called_once_with(0.03)

    async def test_no_exit_when_check_exit_returns_none(self):
        position = self._position()
        state_manager = _mock_state(BotState.IN_POSITION, position=position)

        with patch('core.loop.check_exit', return_value=None):
            await _run_one_tick(
                _mock_client(_candles(25, close=101.0)),
                CandleBuffer(), state_manager, _mock_risk(),
            )

        state_manager.set_state.assert_not_called()

    async def test_state_reset_when_position_missing(self):
        """If state is IN_POSITION but position is None, reset to WAITING."""
        state_manager = _mock_state(BotState.IN_POSITION, position=None)

        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(),
            state_manager, _mock_risk(),
        )

        state_manager.set_state.assert_called_once_with(BotState.WAITING_SIGNAL)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

class TestMisc(unittest.IsolatedAsyncioTestCase):

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
