import logging
import os
import time
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.macro_filter import MacroFilter, NO_TRADE
from core.state import BotState, StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from notifications import notify
from risk.manager import RiskManager
from strategy.indicators import adx, atr, rsi, sma, volume_sma
from strategy.signals import (
    calc_pnl,
    check_exit_price,
    passes_regime_filters,
    should_enter,
    update_trailing_stop,
)

logger = logging.getLogger(__name__)

_SMA_PERIOD     = 20
_RSI_PERIOD     = 14
_VOL_SMA_PERIOD = 20
_ATR_PERIOD     = 14
_ADX_PERIOD     = 14
_MIN_CANDLES    = max(_SMA_PERIOD, _RSI_PERIOD, _VOL_SMA_PERIOD, _ATR_PERIOD)
_DATA_DIR       = Path('data')
_TRADES_FILE    = _DATA_DIR / 'trades_history.json'
_HEALTH_FILE    = _DATA_DIR / 'bot_health.json'


# ── config dataclass ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _LoopConfig:
    symbol:               str
    timeframe:            str
    limit:                int
    interval:             float
    balance:              float
    risk_pct:             float
    sl_pct_long:          float
    tp_pct_long:          float
    sl_pct_short:         float
    tp_pct_short:         float
    rsi_threshold:        float           # long entry threshold
    rsi_short_threshold:  float           # short entry threshold
    leverage:             int
    use_atr_exits:        bool
    atr_sl_multiplier:    float
    atr_tp_multiplier:    float
    use_trailing_stop:    bool
    use_adx_filter:       bool
    adx_threshold:        float
    use_trend_filter:     bool


def _parse_config(raw: dict[str, Any]) -> _LoopConfig:
    return _LoopConfig(
        symbol               = raw['symbol'],
        timeframe            = raw['timeframe'],
        limit                = raw.get('limit', 200),
        interval             = raw['interval_seconds'],
        balance              = raw.get('paper_balance', 10_000.0),
        risk_pct             = raw.get('risk_pct', 0.02),
        sl_pct_long          = raw.get('stop_loss_pct_long', 0.025),
        tp_pct_long          = raw.get('take_profit_pct_long', 0.040),
        sl_pct_short         = raw.get('stop_loss_pct_short', 0.035),
        tp_pct_short         = raw.get('take_profit_pct_short', 0.060),
        rsi_threshold        = raw.get('rsi_threshold', 40.0),
        rsi_short_threshold  = raw.get('rsi_short_threshold', 55.0),
        leverage             = raw.get('leverage', 1),
        use_atr_exits        = raw.get('use_atr_exits', False),
        atr_sl_multiplier    = raw.get('atr_sl_multiplier', 1.5),
        atr_tp_multiplier    = raw.get('atr_tp_multiplier', 3.0),
        use_trailing_stop    = raw.get('use_trailing_stop', False),
        use_adx_filter       = raw.get('use_adx_filter', False),
        adx_threshold        = raw.get('adx_threshold', 45.0),
        use_trend_filter     = raw.get('use_trend_filter', False),
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
    _atomic_or_direct_write(_TRADES_FILE, json.dumps(trades, indent=2))


def _atomic_or_direct_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically, falling back to direct write on bind mounts.

    Docker bind-mounts of single files cause os.replace to fail with EBUSY
    because the target inode is held by the mount. In that case write
    directly — less crash-safe but the only option for bind-mounted files.
    """
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(data, encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        if exc.errno in (16, 18):  # EBUSY or EXDEV
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            path.write_text(data, encoding='utf-8')
        else:
            raise


def _update_health(close: float, rsi_val: float, state: BotState, daily_pnl: float) -> None:
    payload = {
        'last_tick_ms':  int(time.time() * 1000),
        'last_close':    round(close, 2),
        'rsi':           round(rsi_val, 2),
        'state':         state.value,
        'daily_pnl_pct': round(daily_pnl * 100, 4),
    }
    try:
        _atomic_or_direct_write(_HEALTH_FILE, json.dumps(payload))
    except OSError as exc:
        logger.debug('health_write_failed error=%s', exc)


# ── SL/TP setup ────────────────────────────────────────────────────────────

def _compute_sl_tp(
    entry_price: float,
    cfg: _LoopConfig,
    atr_val: float | None,
) -> tuple[float, float]:
    """Decide absolute SL and TP prices at entry time.

    ATR exits only apply when the flag is on and a valid ATR is available;
    otherwise falls back to fixed-percentage exits.
    """
    if cfg.use_atr_exits and atr_val is not None and atr_val > 0:
        sl_price = entry_price - cfg.atr_sl_multiplier * atr_val
        tp_price = entry_price + cfg.atr_tp_multiplier * atr_val
    else:
        sl_price = entry_price * (1 - cfg.sl_pct_long)
        tp_price = entry_price * (1 + cfg.tp_pct_long)
    return sl_price, tp_price


def _ensure_sl_tp(position: dict, cfg: _LoopConfig) -> dict:
    """Back-fill sl_price/tp_price on positions opened before this upgrade.

    Older positions persisted in bot_state.json do not carry SL/TP prices.
    Synthesize them from fixed-pct config so the trailing-stop / exit logic
    can run without KeyError.
    """
    if 'sl_price' in position and 'tp_price' in position:
        return position
    entry = position['entry_price']
    sl_price = entry * (1 - cfg.sl_pct_long)
    tp_price = entry * (1 + cfg.tp_pct_long)
    position['sl_price'] = sl_price
    position['tp_price'] = tp_price
    logger.info(
        'backfilled_sl_tp entry=%.4f sl=%.4f tp=%.4f',
        entry, sl_price, tp_price,
    )
    return position


# ── position helpers ───────────────────────────────────────────────────────

def _open_position(
    state_manager: StateManager,
    close: float,
    qty: float,
    sl_price: float,
    tp_price: float,
) -> str:
    position = {
        'entry_price': close,
        'qty':         qty,
        'ts':          int(time.time() * 1000),
        'sl_price':    sl_price,
        'tp_price':    tp_price,
    }
    state_manager.set_position(position)
    state_manager.set_state(BotState.IN_POSITION)
    logger.info(
        'paper_trade_open entry_price=%.4f qty=%.6f value_usdt=%.2f sl=%.4f tp=%.4f',
        close, qty, close * qty, sl_price, tp_price,
    )
    return (
        f'📈 <b>POSITION OPENED</b>\n'
        f'Entry: ${close:,.2f}\n'
        f'Qty: {qty:.6f} BTC\n'
        f'Value: ${close * qty:,.2f} USDT\n'
        f'SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}'
    )


def _close_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    close: float,
    reason: str,
) -> str | None:
    position = state_manager.get_position()
    if position is None:
        logger.error('close_position called but no position in state')
        return None

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
    # Daily PnL tracks fraction-of-balance loss; with volatility-targeted sizing
    # the stop-out loss per trade equals risk_pct, not the raw pct move.
    # register_trade receives pnl_pct / 100 for continuity with prior behaviour.
    risk_manager.register_trade(pnl_pct / 100)

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    state_manager.set_daily_pnl(risk_manager.get_daily_pnl(), today)

    logger.info(
        'paper_trade_close reason=%s exit_price=%.4f pnl=%.4f pnl_pct=%.2f%% result=%s',
        reason, close, pnl_usdt, pnl_pct, result,
    )

    state_manager.set_position(None)
    state_manager.set_state(BotState.WAITING_SIGNAL)

    emoji = '✅' if pnl_usdt >= 0 else '❌'
    return (
        f'{emoji} <b>POSITION CLOSED ({reason.upper()})</b>\n'
        f'Entry: ${entry_price:,.2f}  →  Exit: ${close:,.2f}\n'
        f'PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)\n'
        f'Result: {result}'
    )


# ── per-tick helpers ───────────────────────────────────────────────────────

def _compute_indicators(
    df: pd.DataFrame,
) -> tuple[float, float, float, float, float, float | None, float | None]:
    """Returns (close, sma_val, rsi_val, volume, vol_sma, atr_val, adx_val).

    atr_val and adx_val are None during warmup (Wilder smoothing produces NaN
    until ~period bars have accumulated).
    """
    close = float(df['close'].iloc[-1])
    sma_v = float(sma(df, period=_SMA_PERIOD).iloc[-1])
    rsi_v = float(rsi(df, period=_RSI_PERIOD).iloc[-1])
    vol   = float(df['volume'].iloc[-1])
    vol_s = float(volume_sma(df, period=_VOL_SMA_PERIOD).iloc[-1])
    atr_v_raw = atr(df, period=_ATR_PERIOD).iloc[-1]
    atr_v = float(atr_v_raw) if pd.notna(atr_v_raw) else None
    adx_v_raw = adx(df, period=_ADX_PERIOD).iloc[-1]
    adx_v = float(adx_v_raw) if pd.notna(adx_v_raw) else None
    return close, sma_v, rsi_v, vol, vol_s, atr_v, adx_v


def _log_tick(
    symbol: str,
    close: float,
    sma_val: float,
    rsi_val: float,
    vol_ratio: float,
    atr_val: float | None,
    adx_val: float | None,
    bot_state: BotState,
) -> None:
    atr_str = f'{atr_val:.2f}' if atr_val is not None else 'n/a'
    adx_str = f'{adx_val:.1f}' if adx_val is not None else 'n/a'
    logger.info(
        'tick symbol=%s close=%.2f sma%d=%.2f rsi%d=%.1f vol_ratio=%.2f '
        'atr%d=%s adx%d=%s state=%s',
        symbol, close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val,
        vol_ratio, _ATR_PERIOD, atr_str, _ADX_PERIOD, adx_str, bot_state.value,
    )


def _handle_waiting_signal(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    sma_val: float,
    rsi_val: float,
    atr_val: float | None,
    adx_val: float | None,
) -> str | None:
    if not should_enter(close, sma_val, rsi_val, rsi_threshold=cfg.rsi_threshold):
        logger.info(
            'no_signal close=%.4f sma%d=%.4f rsi%d=%.2f threshold=%.1f close_above_sma=%s',
            close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val, cfg.rsi_threshold,
            close > sma_val,
        )
        return None
    if not passes_regime_filters(
        trend_bullish=None,
        adx_val=adx_val,
        adx_threshold=cfg.adx_threshold,
        use_trend_filter=cfg.use_trend_filter,
        use_adx_filter=cfg.use_adx_filter,
    ):
        logger.info(
            'regime_blocked adx%d=%s threshold=%.1f use_adx=%s',
            _ADX_PERIOD, f'{adx_val:.1f}' if adx_val is not None else 'n/a',
            cfg.adx_threshold, cfg.use_adx_filter,
        )
        return None
    logger.info(
        'buy_signal close=%.4f sma%d=%.4f rsi%d=%.2f threshold=%.1f adx%d=%s',
        close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val, cfg.rsi_threshold,
        _ADX_PERIOD, f'{adx_val:.1f}' if adx_val is not None else 'n/a',
    )
    sl_price, tp_price = _compute_sl_tp(close, cfg, atr_val)
    # After the risk-sizing bug fix, notional is balance × risk_pct / sl_pct
    # where sl_pct is the *fractional* stop distance used for this entry.
    effective_sl_pct = (close - sl_price) / close
    notional = risk_manager.position_size(
        cfg.balance, risk_pct=cfg.risk_pct, sl_pct=effective_sl_pct,
    )
    if notional <= 0:
        return None
    qty = notional / close
    return _open_position(state_manager, close, qty, sl_price, tp_price)


def _handle_in_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    atr_val: float | None,
) -> str | None:
    position = state_manager.get_position()
    if position is None:
        logger.error('state=IN_POSITION but no position found, resetting')
        state_manager.set_state(BotState.WAITING_SIGNAL)
        return None
    position = _ensure_sl_tp(position, cfg)

    if cfg.use_trailing_stop and atr_val is not None and atr_val > 0:
        new_sl = update_trailing_stop(
            sl_price=position['sl_price'],
            entry_price=position['entry_price'],
            tp_price=position['tp_price'],
            close=close,
            atr_val=atr_val,
        )
        if new_sl > position['sl_price']:
            logger.info(
                'trailing_stop_raised old_sl=%.4f new_sl=%.4f close=%.4f entry=%.4f',
                position['sl_price'], new_sl, close, position['entry_price'],
            )
            position['sl_price'] = new_sl
            state_manager.set_position(position)

    pnl_usdt, pnl_pct = calc_pnl(close, position['entry_price'], position['qty'])
    logger.info(
        'unrealized_pnl=%.4f unrealized_pnl_pct=%.2f%% sl=%.4f tp=%.4f',
        pnl_usdt, pnl_pct, position['sl_price'], position['tp_price'],
    )
    reason = check_exit_price(close, position['sl_price'], position['tp_price'])
    if reason:
        return _close_position(state_manager, risk_manager, close, reason)
    return None


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
    close, sma_v, rsi_v, volume, vol_s, atr_v, adx_v = _compute_indicators(df)
    bot_state = state_manager.get_state()
    _log_tick(
        cfg.symbol, close, sma_v, rsi_v,
        volume / vol_s if vol_s > 0 else 0.0, atr_v, adx_v, bot_state,
    )
    _update_health(close, rsi_v, bot_state, risk_manager.get_daily_pnl())

    notification: str | None = None
    if bot_state == BotState.WAITING_SIGNAL:
        notification = _handle_waiting_signal(
            state_manager, risk_manager, cfg, close, sma_v, rsi_v, atr_v, adx_v,
        )
    elif bot_state == BotState.IN_POSITION:
        notification = _handle_in_position(state_manager, risk_manager, cfg, close, atr_v)

    if notification:
        await notify(notification)


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
        risk_pct          float  fraction of balance *at risk per trade*
                                  (notional = balance × risk_pct / sl_pct)
        rsi_threshold     float  RSI entry threshold (default 40.0)
        stop_loss_pct     float  fixed-pct stop loss (default 0.025)
        take_profit_pct   float  fixed-pct take profit (default 0.040)
        use_atr_exits     bool   if True use ATR-scaled SL/TP instead of fixed %
        atr_sl_multiplier float  SL distance in ATRs when use_atr_exits
        atr_tp_multiplier float  TP distance in ATRs when use_atr_exits
        use_trailing_stop bool   enable three-stage trailing stop
        circuit_breaker_pct float daily drawdown limit (default 0.03)
    """
    cfg = _parse_config(config)
    logger.info(
        'trading_loop started symbol=%s timeframe=%s interval=%ss balance=%.2f '
        'risk_pct=%.4f use_atr_exits=%s use_trailing_stop=%s '
        'use_adx_filter=%s adx_threshold=%.1f use_trend_filter=%s',
        cfg.symbol, cfg.timeframe, cfg.interval, cfg.balance,
        cfg.risk_pct, cfg.use_atr_exits, cfg.use_trailing_stop,
        cfg.use_adx_filter, cfg.adx_threshold, cfg.use_trend_filter,
    )

    _circuit_breaker_notified = False

    async def _on_candles(candles: list[dict[str, Any]]) -> None:
        nonlocal _circuit_breaker_notified
        if risk_manager.is_circuit_breaker_active():
            daily_pnl = risk_manager.get_daily_pnl()
            logger.warning('circuit_breaker=active daily_pnl=%.4f', daily_pnl)
            if not _circuit_breaker_notified:
                _circuit_breaker_notified = True
                await notify(
                    f'🛑 <b>CIRCUIT BREAKER TRIPPED</b>\n'
                    f'Daily PnL: {daily_pnl * 100:+.2f}%\n'
                    f'No new trades until midnight reset.'
                )
            return
        _circuit_breaker_notified = False
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
