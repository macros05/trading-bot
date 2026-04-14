import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import ccxt

from core.state import BotState, StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from risk.manager import RiskManager
from strategy.indicators import rsi, sma
from strategy.signals import calc_pnl, check_exit, should_enter

logger = logging.getLogger(__name__)

_SMA_PERIOD = 20
_RSI_PERIOD = 14
_MIN_CANDLES = max(_SMA_PERIOD, _RSI_PERIOD)
_TRADES_FILE = Path('trades_history.json')


def _load_trades() -> list[dict]:
    if _TRADES_FILE.exists():
        try:
            return json.loads(_TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_trade(trade: dict) -> None:
    trades = _load_trades()
    trades.append(trade)
    _TRADES_FILE.write_text(json.dumps(trades, indent=2))


def _open_position(
    state_manager: StateManager,
    close: float,
    qty: float,
) -> None:
    position = {
        'entry_price': close,
        'qty':         qty,
        'ts':          int(time.time() * 1000),
    }
    state_manager.set_position(position)
    state_manager.set_state(BotState.IN_POSITION)
    logger.info(
        'paper_trade_open entry_price=%.4f qty=%.6f value_usdt=%.2f',
        close, qty, close * qty,
    )


def _close_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    close: float,
    reason: str,
) -> None:
    position = state_manager.get_position()
    if position is None:
        logger.error('close_position called but no position in state')
        return

    entry_price: float = position['entry_price']
    qty: float = position['qty']
    pnl_usdt, pnl_pct = calc_pnl(close, entry_price, qty)
    result = 'WIN' if pnl_usdt >= 0 else 'LOSS'

    trade = {
        'entry_price': entry_price,
        'exit_price':  close,
        'qty':         qty,
        'pnl_usdt':    round(pnl_usdt, 4),
        'pnl_pct':     round(pnl_pct, 4),
        'result':      result,
        'reason':      reason,
        'entry_ts':    position['ts'],
        'exit_ts':     int(time.time() * 1000),
    }
    _save_trade(trade)
    risk_manager.register_trade(pnl_pct / 100)

    logger.info(
        'paper_trade_close reason=%s exit_price=%.4f pnl=%.4f pnl_pct=%.2f%% result=%s',
        reason, close, pnl_usdt, pnl_pct, result,
    )

    state_manager.set_position(None)
    state_manager.set_state(BotState.WAITING_SIGNAL)


async def trading_loop(
    client: BinanceClient,
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    config: dict[str, Any],
) -> None:
    """Main trading loop. Runs indefinitely until cancelled.

    Expected config keys:
        symbol            str    e.g. 'BTC/USDT'
        timeframe         str    e.g. '1m'
        limit             int    candles to fetch per tick (default 200)
        interval_seconds  float  sleep between iterations
        paper_balance     float  simulated USDT balance
        risk_pct          float  fraction of balance to risk per trade
        stop_loss_pct     float  stop loss threshold (default 0.02)
        take_profit_pct   float  take profit threshold (default 0.03)
    """
    symbol: str       = config['symbol']
    timeframe: str    = config['timeframe']
    limit: int        = config.get('limit', 200)
    interval: float   = config['interval_seconds']
    balance: float    = config.get('paper_balance', 10_000.0)
    risk_pct: float   = config.get('risk_pct', 0.01)
    sl_pct: float     = config.get('stop_loss_pct', 0.02)
    tp_pct: float     = config.get('take_profit_pct', 0.03)

    logger.info(
        'trading_loop started symbol=%s timeframe=%s interval=%ss balance=%.2f',
        symbol, timeframe, interval, balance,
    )

    while True:
        if risk_manager.is_circuit_breaker_active():
            logger.warning(
                'circuit_breaker=active daily_pnl=%.4f sleeping %ss',
                risk_manager.get_daily_pnl(), interval,
            )
            await asyncio.sleep(interval)
            continue

        try:
            candles: list[dict[str, Any]] = await client.fetch_candles(
                symbol=symbol, timeframe=timeframe, limit=limit,
            )
        except (ccxt.RateLimitExceeded, ccxt.NetworkError) as exc:
            logger.warning('fetch_failed error=%s sleeping %ss', exc, interval)
            await asyncio.sleep(interval)
            continue

        buffer.add_many(candles)

        if not buffer.is_ready(_MIN_CANDLES):
            logger.debug(
                'buffer_not_ready len=%d required=%d sleeping %ss',
                len(buffer), _MIN_CANDLES, interval,
            )
            await asyncio.sleep(interval)
            continue

        df = buffer.to_dataframe()
        current_close: float = float(df['close'].iloc[-1])
        current_sma: float   = float(sma(df, period=_SMA_PERIOD).iloc[-1])
        current_rsi: float   = float(rsi(df, period=_RSI_PERIOD).iloc[-1])
        bot_state            = state_manager.get_state()

        logger.info(
            'tick symbol=%s close=%.2f sma%d=%.2f rsi%d=%.1f state=%s',
            symbol, current_close, _SMA_PERIOD, current_sma,
            _RSI_PERIOD, current_rsi, bot_state.value,
        )

        if bot_state == BotState.WAITING_SIGNAL:
            if should_enter(current_close, current_sma, current_rsi, rsi_threshold=35.0):
                logger.info(
                    'buy_signal symbol=%s close=%.4f sma%d=%.4f rsi%d=%.2f',
                    symbol, current_close, _SMA_PERIOD, current_sma,
                    _RSI_PERIOD, current_rsi,
                )
                qty = risk_manager.position_size(balance, risk_pct=risk_pct) / current_close
                if qty > 0:
                    _open_position(state_manager, current_close, qty)

        elif bot_state == BotState.IN_POSITION:
            position = state_manager.get_position()
            if position is None:
                logger.error('state=IN_POSITION but no position found, resetting')
                state_manager.set_state(BotState.WAITING_SIGNAL)
            else:
                pnl_usdt, pnl_pct = calc_pnl(current_close, position['entry_price'], position['qty'])
                logger.info(
                    'unrealized_pnl=%.4f unrealized_pnl_pct=%.2f%%',
                    pnl_usdt, pnl_pct,
                )
                exit_reason = check_exit(
                    current_close,
                    position['entry_price'],
                    stop_loss_pct=sl_pct,
                    take_profit_pct=tp_pct,
                )
                if exit_reason:
                    _close_position(state_manager, risk_manager, current_close, exit_reason)

        await asyncio.sleep(interval)
