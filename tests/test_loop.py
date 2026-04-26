"""
Tests for core/loop.py.

The loop is now driven by watch_candles (WebSocket). Tests inject candles by
mocking client.watch_candles to call the callback once then raise
CancelledError, which is the natural way to stop an asyncio task.

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
from core.macro_filter import AGGRESSIVE, NORMAL, NO_TRADE
from core.state import BotState
from data.candles import CandleBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config() -> dict:
    return {
        'symbol':                 'BTC/USDT',
        'timeframe':              '1m',
        'limit':                  200,
        'interval_seconds':       60,
        'paper_balance':          10_000.0,
        'risk_pct':               0.01,
        'stop_loss_pct_long':     0.02,
        'take_profit_pct_long':   0.03,
        'stop_loss_pct_short':    0.035,
        'take_profit_pct_short':  0.060,
        'rsi_threshold':          40.0,
        'rsi_short_threshold':    55.0,
        # Keep the legacy test suite focused on WAITING/IN_POSITION transitions;
        # trailing stop, ATR exits and regime filters have their own tests.
        'use_atr_exits':          False,
        'use_trailing_stop':      False,
        'use_adx_filter':         False,
        'use_trend_filter':       False,
    }


def _candles(n: int, close: float = 100.0) -> list[dict]:
    return [
        {'ts': i, 'open': close - 1, 'high': close + 1,
         'low': close - 2, 'close': close, 'volume': 10.0}
        for i in range(n)
    ]


def _mock_client(candles: list[dict]) -> MagicMock:
    """Client whose watch_candles calls the callback once then cancels."""
    client = MagicMock()
    client.fetch_candles = AsyncMock(return_value=candles)

    async def _fake_watch(
        symbol: str,
        timeframe: str,
        callback,
        *,
        limit: int = 200,
        rest_interval: float = 60.0,
    ) -> None:
        await callback(candles)
        raise asyncio.CancelledError

    client.watch_candles = _fake_watch
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


def _mock_macro_filter(mode: str = NORMAL) -> MagicMock:
    mf = MagicMock()
    mf.get_mode = AsyncMock(return_value=mode)
    return mf


async def _run_one_tick(
    client,
    buffer,
    state_manager,
    risk_manager,
    config=None,
    macro_filter=None,
):
    """Run the loop for exactly one callback invocation.

    watch_candles mock calls the callback once then raises CancelledError,
    which propagates out of trading_loop as expected.
    """
    cfg = config or _config()
    with unittest.TestCase().assertRaises(asyncio.CancelledError):
        await trading_loop(
            client, buffer, state_manager, risk_manager, cfg,
            macro_filter=macro_filter,
        )


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):

    async def test_does_not_evaluate_signals_when_active(self):
        """circuit_breaker=True → callback returns early, no state access."""
        state_manager = _mock_state()
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), state_manager,
            _mock_risk(circuit_breaker=True, daily_pnl=-0.05),
        )
        state_manager.get_state.assert_not_called()

    async def test_does_not_open_position_when_active(self):
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        with patch('core.loop.should_enter', return_value=True):
            await _run_one_tick(
                _mock_client(_candles(25)), CandleBuffer(), state_manager,
                _mock_risk(circuit_breaker=True),
            )
        state_manager.set_state.assert_not_called()


# ---------------------------------------------------------------------------
# Buffer not ready
# ---------------------------------------------------------------------------

class TestBufferNotReady(unittest.IsolatedAsyncioTestCase):

    async def test_does_not_evaluate_signal_when_buffer_short(self):
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
        risk = _mock_risk(position_size=200.0)   # 200 USDT / 100 close = 2.0 BTC

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

        with patch('core.loop.check_exit_price', return_value=None), \
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

        with patch('core.loop.check_exit_price', return_value='stop_loss'), \
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

        with patch('core.loop.check_exit_price', return_value='take_profit'), \
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

        with patch('core.loop.check_exit_price', return_value='take_profit'), \
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

        with patch('core.loop.check_exit_price', return_value=None):
            await _run_one_tick(
                _mock_client(_candles(25, close=101.0)),
                CandleBuffer(), state_manager, _mock_risk(),
            )

        state_manager.set_state.assert_not_called()

    async def test_state_reset_when_position_missing(self):
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

    async def test_watch_candles_called_with_config_params(self):
        """trading_loop passes symbol, timeframe, limit, rest_interval to watch_candles."""
        calls: list[tuple] = []

        async def _capture(symbol, timeframe, callback, *, limit, rest_interval):
            calls.append((symbol, timeframe, limit, rest_interval))
            raise asyncio.CancelledError

        client = MagicMock()
        client.watch_candles = _capture

        with unittest.TestCase().assertRaises(asyncio.CancelledError):
            await trading_loop(
                client, CandleBuffer(), _mock_state(), _mock_risk(), _config(),
            )

        self.assertEqual(calls, [('BTC/USDT', '1m', 200, 60)])

    async def test_buffer_populated_after_callback(self):
        buffer = CandleBuffer()
        await _run_one_tick(
            _mock_client(_candles(25)), buffer, _mock_state(), _mock_risk(),
        )
        self.assertEqual(len(buffer), 25)


# ---------------------------------------------------------------------------
# MacroFilter gate in loop
# ---------------------------------------------------------------------------

class TestMacroFilterGate(unittest.IsolatedAsyncioTestCase):

    async def test_no_trade_does_not_evaluate_signals(self):
        """When macro mode is NO_TRADE, callback returns early — no state access."""
        state_manager = _mock_state()
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), state_manager, _mock_risk(),
            macro_filter=_mock_macro_filter(NO_TRADE),
        )
        state_manager.get_state.assert_not_called()

    async def test_normal_mode_evaluates_signals(self):
        """NORMAL macro mode proceeds to signal evaluation."""
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), state_manager, _mock_risk(),
            macro_filter=_mock_macro_filter(NORMAL),
        )
        state_manager.get_state.assert_called_once()

    async def test_aggressive_mode_evaluates_signals(self):
        """AGGRESSIVE macro mode proceeds to signal evaluation."""
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), state_manager, _mock_risk(),
            macro_filter=_mock_macro_filter(AGGRESSIVE),
        )
        state_manager.get_state.assert_called_once()

    async def test_no_macro_filter_evaluates_signals(self):
        """When macro_filter=None (default), loop proceeds without checking mode."""
        state_manager = _mock_state(BotState.WAITING_SIGNAL)
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), state_manager, _mock_risk(),
        )
        state_manager.get_state.assert_called_once()

    async def test_get_mode_called_once_per_tick(self):
        mf = _mock_macro_filter(NORMAL)
        await _run_one_tick(
            _mock_client(_candles(25)), CandleBuffer(), _mock_state(), _mock_risk(),
            macro_filter=mf,
        )
        mf.get_mode.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
