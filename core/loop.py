import logging
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.macro_filter import MacroFilter, NO_TRADE
from core.state import BotState, StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from risk.manager import RiskManager
from strategy.indicators import rsi, sma, volume_sma
from strategy.signals import calc_pnl, check_exit, should_enter

logger = logging.getLogger(__name__)

_SMA_PERIOD     = 20
_RSI_PERIOD     = 14
_VOL_SMA_PERIOD = 20
_MIN_CANDLES    = max(_SMA_PERIOD, _RSI_PERIOD, _VOL_SMA_PERIOD)
_TRADES_FILE    = Path('trades_history.json')


# ── config dataclass ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _LoopConfig:
    symbol:    str
    timeframe: str
    limit:     int
    interval:  float
    balance:   float
    risk_pct:  float
    sl_pct:    float
    tp_pct:    float


def _parse_config(raw: dict[str, Any]) -> _LoopConfig:
    return _LoopConfig(
        symbol    = raw['symbol'],
        timeframe = raw['timeframe'],
        limit     = raw.get('limit', 200),
        interval  = raw['interval_seconds'],
        balance   = raw.get('paper_balance', 10_000.0),
        risk_pct  = raw.get('risk_pct', 0.01),
        sl_pct    = raw.get('stop_loss_pct', 0.02),
        tp_pct    = raw.get('take_profit_pct', 0.03),
    )


# ── trade persistence ──────────────────────────────────────────────────────

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


# ── position helpers ───────────────────────────────────────────────────────

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


# ── per-tick helpers ───────────────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame) -> tuple[float, float, float, float, float]:
    """Returns (close, sma_val, rsi_val, volume, vol_sma20)."""
    close = float(df['close'].iloc[-1])
    sma_v = float(sma(df, period=_SMA_PERIOD).iloc[-1])
    rsi_v = float(rsi(df, period=_RSI_PERIOD).iloc[-1])
    vol   = float(df['volume'].iloc[-1])
    vol_s = float(volume_sma(df, period=_VOL_SMA_PERIOD).iloc[-1])
    return close, sma_v, rsi_v, vol, vol_s


def _log_tick(
    symbol: str,
    close: float,
    sma_val: float,
    rsi_val: float,
    vol_ratio: float,
    bot_state: BotState,
) -> None:
    logger.info(
        'tick symbol=%s close=%.2f sma%d=%.2f rsi%d=%.1f vol_ratio=%.2f state=%s',
        symbol, close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val, vol_ratio, bot_state.value,
    )


def _handle_waiting_signal(
    state_manager: StateManager,
    risk_manager: RiskManager,
    balance: float,
    risk_pct: float,
    close: float,
    sma_val: float,
    rsi_val: float,
) -> None:
    # Volume filter (1.2× vol_sma20) was tested and discarded:
    # collapsed 10 → 1 entry over 90 days, Sharpe 0.19 → 0.00.
    if not should_enter(close, sma_val, rsi_val, rsi_threshold=35.0):
        return
    logger.info(
        'buy_signal close=%.4f sma%d=%.4f rsi%d=%.2f',
        close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val,
    )
    qty = risk_manager.position_size(balance, risk_pct=risk_pct) / close
    if qty > 0:
        _open_position(state_manager, close, qty)


def _handle_in_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    close: float,
    sl_pct: float,
    tp_pct: float,
) -> None:
    position = state_manager.get_position()
    if position is None:
        logger.error('state=IN_POSITION but no position found, resetting')
        state_manager.set_state(BotState.WAITING_SIGNAL)
        return
    pnl_usdt, pnl_pct = calc_pnl(close, position['entry_price'], position['qty'])
    logger.info('unrealized_pnl=%.4f unrealized_pnl_pct=%.2f%%', pnl_usdt, pnl_pct)
    reason = check_exit(close, position['entry_price'], stop_loss_pct=sl_pct, take_profit_pct=tp_pct)
    if reason:
        _close_position(state_manager, risk_manager, close, reason)


async def _process_tick(
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    candles: list[dict[str, Any]],
) -> None:
    buffer.add_many(candles)
    if not buffer.is_ready(_MIN_CANDLES):
        logger.debug('buffer_not_ready len=%d required=%d', len(buffer), _MIN_CANDLES)
        return
    df = buffer.to_dataframe()
    close, sma_v, rsi_v, volume, vol_s = _compute_indicators(df)
    bot_state = state_manager.get_state()
    _log_tick(cfg.symbol, close, sma_v, rsi_v, volume / vol_s if vol_s > 0 else 0.0, bot_state)
    if bot_state == BotState.WAITING_SIGNAL:
        _handle_waiting_signal(state_manager, risk_manager, cfg.balance, cfg.risk_pct,
                               close, sma_v, rsi_v)
    elif bot_state == BotState.IN_POSITION:
        _handle_in_position(state_manager, risk_manager, close, cfg.sl_pct, cfg.tp_pct)


# ── main loop ──────────────────────────────────────────────────────────────

async def trading_loop(
    client: BinanceClient,
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    config: dict[str, Any],
    macro_filter: MacroFilter | None = None,
) -> None:
    """Main trading loop. Streams candles via WebSocket; falls back to REST polling.

    Expected config keys:
        symbol            str    e.g. 'BTC/USDT'
        timeframe         str    e.g. '1m'
        limit             int    candles per fetch/stream page (default 200)
        interval_seconds  float  REST fallback polling interval
        paper_balance     float  simulated USDT balance
        risk_pct          float  fraction of balance to risk per trade
        stop_loss_pct     float  stop loss threshold (default 0.02)
        take_profit_pct   float  take profit threshold (default 0.03)
    """
    cfg = _parse_config(config)
    logger.info(
        'trading_loop started symbol=%s timeframe=%s interval=%ss balance=%.2f',
        cfg.symbol, cfg.timeframe, cfg.interval, cfg.balance,
    )

    async def _on_candles(candles: list[dict[str, Any]]) -> None:
        if risk_manager.is_circuit_breaker_active():
            logger.warning('circuit_breaker=active daily_pnl=%.4f', risk_manager.get_daily_pnl())
            return
        if macro_filter is not None:
            mode = await macro_filter.get_mode()
            if mode == NO_TRADE:
                logger.info('macro_mode=NO_TRADE skipping_signal_evaluation')
                return
        await _process_tick(buffer, state_manager, risk_manager, cfg, candles)

    await client.watch_candles(
        cfg.symbol, cfg.timeframe, _on_candles,
        limit=cfg.limit, rest_interval=cfg.interval,
    )
