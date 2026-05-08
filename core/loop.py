import logging
import os
import time
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.live_db import (
    init_db, insert_kelly_change, insert_live_trade, insert_near_miss,
    insert_shadow_trade, list_live_trades, update_shadow_resolution,
)
from analytics.macro_calendar import is_high_impact_event, macro_event_for_ts
from analytics.regime_classifier import classify_regime, percentile_of
from core.macro_filter import MacroFilter, NO_TRADE
from risk.protections import ProtectionStack
from core.state import BotState, StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from notifications import notify, notify_near_miss, notify_trailing
from risk.manager import RiskManager
from strategy.indicators import (
    adx, atr, higher_tf_trend_ema, higher_tf_trend_sma, rsi, sma, volume_sma,
)
from strategy.regime import (
    atr_percentile_bounds, is_mtf_aligned, is_position_stalled,
    is_quiet_range, passes_short_trend_filter, passes_volatility_window,
    shorts_disabled_in_flat,
)
from strategy.sessions import is_session_allowed, session_for_ts
from strategy.signals import (
    calc_pnl, calc_pnl_short, check_exit_price,
    near_miss_reason, passes_regime_filters,
    should_enter, should_enter_short, should_exit_time,
    tighten_sl_tp_for_stalled, update_trailing_stop_pct,
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
_PAUSE_FILE     = _DATA_DIR / 'pause.flag'

# Buffers for filters that need history beyond _MIN_CANDLES
_ATR_HISTORY_MAXLEN  = 60 * 48 + 10   # ~48h of 1m bars + safety
_POS_CLOSES_MAXLEN   = 60 * 24       # ~24h of 1m bars; bound to MAX_HOLD_HOURS in practice


# ── config dataclass ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _LoopConfig:
    symbol:                   str
    timeframe:                str
    limit:                    int
    interval:                 float
    balance:                  float
    risk_pct:                 float
    sl_pct_long:              float
    tp_pct_long:              float
    sl_pct_short:             float
    tp_pct_short:             float
    rsi_threshold:            float
    rsi_short_threshold:      float
    leverage:                 int
    use_atr_exits:            bool
    atr_sl_multiplier:        float
    atr_tp_multiplier:        float
    use_trailing_stop:        bool
    trailing_breakeven_pct:   float
    trailing_trail_pct:       float
    trailing_distance_pct:    float
    use_adx_filter:           bool
    adx_threshold:            float
    use_trend_filter:         bool
    max_hold_hours:           float
    stalled_hours:            float
    stalled_move_threshold:   float
    range_lookback_min:       int
    range_pct_threshold:      float
    use_volatility_filter:    bool
    volatility_lookback_hours: int
    volatility_low_pct:       float
    volatility_high_pct:      float
    use_mtf_filter:           bool
    mtf_15m_period:           int
    mtf_require_15m:          bool
    mtf_require_1h:           bool
    use_short_trend_filter:   bool
    short_adx_min:            float
    short_sma_period:         int
    adx_flat_threshold:       float
    use_session_filter:       bool
    blocked_sessions:         tuple[str, ...]
    near_miss_rsi_band:       float
    near_miss_sma_band:       float


def _parse_config(raw: dict[str, Any]) -> _LoopConfig:
    return _LoopConfig(
        symbol                    = raw['symbol'],
        timeframe                 = raw['timeframe'],
        limit                     = raw.get('limit', 200),
        interval                  = raw['interval_seconds'],
        balance                   = raw.get('paper_balance', 10_000.0),
        risk_pct                  = raw.get('risk_pct', 0.02),
        sl_pct_long               = raw.get('stop_loss_pct_long', 0.025),
        tp_pct_long               = raw.get('take_profit_pct_long', 0.040),
        sl_pct_short              = raw.get('stop_loss_pct_short', 0.035),
        tp_pct_short              = raw.get('take_profit_pct_short', 0.060),
        rsi_threshold             = raw.get('rsi_threshold', 40.0),
        rsi_short_threshold       = raw.get('rsi_short_threshold', 55.0),
        leverage                  = raw.get('leverage', 1),
        use_atr_exits             = raw.get('use_atr_exits', False),
        atr_sl_multiplier         = raw.get('atr_sl_multiplier', 1.5),
        atr_tp_multiplier         = raw.get('atr_tp_multiplier', 3.0),
        use_trailing_stop         = raw.get('use_trailing_stop', False),
        trailing_breakeven_pct    = raw.get('trailing_breakeven_pct', 0.008),
        trailing_trail_pct        = raw.get('trailing_trail_pct', 0.012),
        trailing_distance_pct     = raw.get('trailing_distance_pct', 0.004),
        use_adx_filter            = raw.get('use_adx_filter', False),
        adx_threshold             = raw.get('adx_threshold', 45.0),
        use_trend_filter          = raw.get('use_trend_filter', False),
        max_hold_hours            = raw.get('max_hold_hours', 0.0),
        stalled_hours             = raw.get('stalled_hours', 0.0),
        stalled_move_threshold    = raw.get('stalled_move_threshold', 0.005),
        range_lookback_min        = raw.get('range_lookback_min', 0),
        range_pct_threshold       = raw.get('range_pct_threshold', 0.003),
        use_volatility_filter     = raw.get('use_volatility_filter', False),
        volatility_lookback_hours = raw.get('volatility_lookback_hours', 48),
        volatility_low_pct        = raw.get('volatility_low_pct', 20.0),
        volatility_high_pct       = raw.get('volatility_high_pct', 80.0),
        use_mtf_filter            = raw.get('use_mtf_filter', False),
        mtf_15m_period            = raw.get('mtf_15m_period', 50),
        mtf_require_15m           = raw.get('mtf_require_15m', True),
        mtf_require_1h            = raw.get('mtf_require_1h', False),
        use_short_trend_filter    = raw.get('use_short_trend_filter', False),
        short_adx_min             = raw.get('short_adx_min', 20.0),
        short_sma_period          = raw.get('short_sma_period', 50),
        adx_flat_threshold        = raw.get('adx_flat_threshold', 18.0),
        use_session_filter        = raw.get('use_session_filter', False),
        blocked_sessions          = tuple(raw.get('blocked_sessions', ())),
        near_miss_rsi_band        = raw.get('near_miss_rsi_band', 0.0),
        near_miss_sma_band        = raw.get('near_miss_sma_band', 0.0),
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
    """Write *data* to *path* atomically, falling back to direct write on bind mounts."""
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(data, encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        if exc.errno in (16, 18):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            path.write_text(data, encoding='utf-8')
        else:
            raise


def _is_paused() -> bool:
    return _PAUSE_FILE.exists()


def _update_health(close: float, rsi_val: float, state: BotState, daily_pnl: float) -> None:
    payload = {
        'last_tick_ms':  int(time.time() * 1000),
        'last_close':    round(close, 2),
        'rsi':           round(rsi_val, 2),
        'state':         state.value,
        'daily_pnl_pct': round(daily_pnl * 100, 4),
        'paused':        _is_paused(),
    }
    try:
        _atomic_or_direct_write(_HEALTH_FILE, json.dumps(payload))
    except OSError as exc:
        logger.debug('health_write_failed error=%s', exc)


# ── SL/TP setup ────────────────────────────────────────────────────────────

def _compute_sl_tp_for_side(
    entry_price: float,
    cfg: _LoopConfig,
    atr_val: float | None,
    side: str,
) -> tuple[float, float]:
    """Side-aware SL/TP. Long: sl<entry<tp. Short: tp<entry<sl."""
    if cfg.use_atr_exits and atr_val is not None and atr_val > 0:
        if side == 'long':
            return (entry_price - cfg.atr_sl_multiplier * atr_val,
                    entry_price + cfg.atr_tp_multiplier * atr_val)
        return (entry_price + cfg.atr_sl_multiplier * atr_val,
                entry_price - cfg.atr_tp_multiplier * atr_val)
    if side == 'long':
        return (entry_price * (1 - cfg.sl_pct_long), entry_price * (1 + cfg.tp_pct_long))
    return (entry_price * (1 + cfg.sl_pct_short), entry_price * (1 - cfg.tp_pct_short))


def _ensure_sl_tp(position: dict, cfg: _LoopConfig) -> dict:
    """Back-fill sl_price/tp_price on positions opened before this upgrade."""
    if 'sl_price' in position and 'tp_price' in position:
        return position
    entry = position['entry_price']
    side = position.get('side', 'long')
    if side == 'long':
        sl_price = entry * (1 - cfg.sl_pct_long)
        tp_price = entry * (1 + cfg.tp_pct_long)
    else:
        sl_price = entry * (1 + cfg.sl_pct_short)
        tp_price = entry * (1 - cfg.tp_pct_short)
    position['sl_price'] = sl_price
    position['tp_price'] = tp_price
    logger.info('backfilled_sl_tp side=%s entry=%.4f sl=%.4f tp=%.4f',
                side, entry, sl_price, tp_price)
    return position


# ── live-trade recorder ────────────────────────────────────────────────────

def _record_live_trade(
    position: dict, close: float, exit_ts: int,
    pnl_usdt: float, pnl_pct: float, reason: str, result: str,
) -> None:
    """Persist a closed paper trade to the SQLite live_trades table.

    Failures are logged but never propagate — the JSON trade history is the
    authoritative record for the dashboard, SQLite is for analysis only.
    """
    try:
        ctx = position.get('entry_context', {}) or {}
        side = position.get('side', 'long')
        entry = float(position['entry_price'])
        qty = float(position['qty'])
        from strategy.sessions import session_for_ts
        record = {
            'entry_ts_ms':     int(position.get('ts', exit_ts)),
            'exit_ts_ms':      exit_ts,
            'side':            side,
            'entry_price':     entry,
            'exit_price':      close,
            'qty':             qty,
            'notional_usdt':   round(entry * qty, 4),
            'pnl_usdt':        round(pnl_usdt, 4),
            'pnl_pct':         round(pnl_pct, 4),
            'result':          result,
            'exit_reason':     reason,
            'duration_min':    round((exit_ts - int(position.get('ts', exit_ts))) / 60_000, 1),
            'session':         ctx.get('session') or session_for_ts(int(position.get('ts', exit_ts))),
            'entry_rsi':       ctx.get('rsi'),
            'entry_adx':       ctx.get('adx'),
            'entry_atr':       ctx.get('atr'),
            'entry_atr_pct':   ctx.get('atr_pct'),
            'entry_sma20':     ctx.get('sma20'),
            'entry_sma50':     ctx.get('sma50'),
            'mtf_15m_aligned': (1 if ctx.get('mtf_15m_aligned') else
                                (0 if ctx.get('mtf_15m_aligned') is False else None)),
            'htf_4h_trend':    ctx.get('htf_4h_trend'),
            'htf_daily_trend': ctx.get('htf_daily_trend'),
            'regime':          ctx.get('regime'),
            'macro_event':     ctx.get('macro_event'),
            'kelly_used':      ctx.get('kelly_used'),
        }
        insert_live_trade(record)
        logger.info('live_trade_recorded id=ok side=%s pnl=%.4f', side, pnl_usdt)
    except Exception as exc:
        logger.warning('live_trade_record_failed error=%s', exc)


# ── position helpers ───────────────────────────────────────────────────────

def _open_position(
    state_manager: StateManager,
    close: float,
    qty: float,
    sl_price: float,
    tp_price: float,
    side: str = 'long',
    context: dict | None = None,
) -> str:
    ts_ms = int(time.time() * 1000)
    position = {
        'side':        side,
        'entry_price': close,
        'qty':         qty,
        'ts':          ts_ms,
        'sl_price':    sl_price,
        'tp_price':    tp_price,
        # Snapshot of conditions at entry — read back at close time.
        'entry_context': dict(context) if context else {},
    }
    state_manager.set_position(position)
    state_manager.set_state(BotState.IN_POSITION)
    logger.info(
        'paper_trade_open side=%s entry_price=%.4f qty=%.6f value_usdt=%.2f sl=%.4f tp=%.4f',
        side, close, qty, close * qty, sl_price, tp_price,
    )
    direction_emoji = '📈' if side == 'long' else '📉'
    msg = (
        f'{direction_emoji} <b>{side.upper()} POSITION OPENED</b>\n'
        f'Entry: ${close:,.2f}\n'
        f'Qty: {qty:.6f} BTC\n'
        f'Value: ${close * qty:,.2f} USDT\n'
        f'SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}'
    )
    if context:
        ctx_lines = []
        if 'session' in context:
            ctx_lines.append(f"Session: {context['session'].upper()}")
        if 'adx' in context and context['adx'] is not None:
            ctx_lines.append(f"ADX: {context['adx']:.1f}")
        if 'trend_15m' in context and context['trend_15m'] is not None:
            ctx_lines.append(f"15m trend: {'UP' if context['trend_15m'] else 'DOWN'}")
        if ctx_lines:
            msg += '\n' + ' | '.join(ctx_lines)
    return msg


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

    side = position.get('side', 'long')
    entry_price: float = position['entry_price']
    qty: float = position['qty']
    if side == 'long':
        pnl_usdt, pnl_pct = calc_pnl(close, entry_price, qty)
    else:
        pnl_usdt, pnl_pct = calc_pnl_short(close, entry_price, qty)
    result = 'WIN' if pnl_usdt >= 0 else 'LOSS'

    exit_ts = int(time.time() * 1000)
    trade = {
        'side':        side,
        'entry_price': entry_price,
        'exit_price':  close,
        'qty':         qty,
        'pnl_usdt':    round(pnl_usdt, 4),
        'pnl_pct':     round(pnl_pct, 4),
        'result':      result,
        'reason':      reason,
        'entry_ts':    position['ts'],
        'exit_ts':     exit_ts,
    }
    _save_trade(trade)
    _record_live_trade(position, close, exit_ts, pnl_usdt, pnl_pct, reason, result)
    risk_manager.register_trade(pnl_pct / 100)

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    state_manager.set_daily_pnl(risk_manager.get_daily_pnl(), today)

    logger.info(
        'paper_trade_close side=%s reason=%s exit_price=%.4f pnl=%.4f pnl_pct=%.2f%% result=%s',
        side, reason, close, pnl_usdt, pnl_pct, result,
    )
    state_manager.set_position(None)
    state_manager.set_state(BotState.WAITING_SIGNAL)

    emoji = '✅' if pnl_usdt >= 0 else '❌'
    return (
        f'{emoji} <b>{side.upper()} CLOSED ({reason.upper()})</b>\n'
        f'Entry: ${entry_price:,.2f}  →  Exit: ${close:,.2f}\n'
        f'PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)\n'
        f'Result: {result}'
    )


# ── per-tick helpers ───────────────────────────────────────────────────────

def _compute_indicators(
    df: pd.DataFrame,
) -> tuple[float, float, float, float, float, float | None, float | None]:
    """Returns (close, sma_val, rsi_val, volume, vol_sma, atr_val, adx_val)."""
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


def _compute_sma_safe(df: pd.DataFrame, period: int) -> float | None:
    """Return last SMA(period) value or None during warmup or insufficient data."""
    if len(df) < period:
        return None
    val = sma(df, period=period).iloc[-1]
    return float(val) if pd.notna(val) else None


def _compute_mtf_15m(df: pd.DataFrame, period: int) -> bool | None:
    """Return last 15m-trend bullish flag, or None during warmup."""
    if len(df) < period * 15:   # need at least period 15m bars
        return None
    try:
        flag = higher_tf_trend_sma(df, tf='15min', period=period).iloc[-1]
    except (ValueError, KeyError):
        return None
    return bool(flag) if pd.notna(flag) else None


def _compute_mtf_1h(df: pd.DataFrame, period: int = 200) -> bool | None:
    """Return last 1h-trend bullish flag, or None during warmup."""
    if len(df) < period * 60:
        return None
    try:
        flag = higher_tf_trend_ema(df, tf='1h', period=period).iloc[-1]
    except (ValueError, KeyError):
        return None
    return bool(flag) if pd.notna(flag) else None


_TF_BARS = {'15min': 15, '1h': 60, '4h': 240, '1D': 1440}


def _compute_htf_trend(df: pd.DataFrame, tf: str, period: int = 50) -> str | None:
    """Resample to *tf* and return 'up' / 'down' / None based on close vs SMA(period)."""
    bars_per_tf = _TF_BARS.get(tf, 0)
    if bars_per_tf == 0 or len(df) < period * bars_per_tf:
        return None
    try:
        flag = higher_tf_trend_sma(df, tf=tf, period=period).iloc[-1]
    except (ValueError, KeyError):
        return None
    if pd.isna(flag):
        return None
    return 'up' if bool(flag) else 'down'


def _update_adaptive_kelly_from_recent(risk_manager: RiskManager) -> None:
    """Refresh effective_risk_pct from the last N live trades (if adaptive)."""
    try:
        recent = list_live_trades(limit=20)
        recent_oldest_first = list(reversed(recent))
        change = risk_manager.update_adaptive_kelly(recent_oldest_first)
        if change is not None:
            try:
                insert_kelly_change(change)
            except Exception as exc:
                logger.debug('kelly_change_db_failed error=%s', exc)
    except Exception as exc:
        logger.debug('adaptive_kelly_update_failed error=%s', exc)


def _record_near_miss_db(
    miss: str, close: float, rsi_val: float, sma_val: float,
    cfg: _LoopConfig, ts_ms: int,
) -> None:
    """Persist a near-miss snapshot to SQLite for later analysis."""
    side_intended = 'long' if 'long near-miss' in miss else (
        'short' if 'short near-miss' in miss else None
    )
    if side_intended == 'long':
        rsi_distance = rsi_val - cfg.rsi_threshold
    elif side_intended == 'short':
        rsi_distance = cfg.rsi_short_threshold - rsi_val
    else:
        rsi_distance = None
    sma_distance_pct = (close - sma_val) / sma_val * 100 if sma_val else None
    try:
        insert_near_miss({
            'ts_ms':            ts_ms,
            'reason':           miss,
            'close':            close,
            'rsi':              rsi_val,
            'sma20':            sma_val,
            'rsi_distance':     rsi_distance,
            'sma_distance_pct': sma_distance_pct,
            'side_intended':    side_intended,
        })
    except Exception as exc:
        logger.debug('near_miss_db_write_failed error=%s', exc)


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


def _entry_filters_block(
    cfg: _LoopConfig,
    side: str,
    close: float,
    sma_val: float,
    sma50: float | None,
    adx_val: float | None,
    atr_val: float | None,
    atr_bounds: tuple[float, float] | None,
    htf_15m: bool | None,
    htf_1h: bool | None,
    last_closes: list[float],
    now_ms: int,
) -> str | None:
    """Return a blocking reason string or None when entry is allowed."""
    if cfg.use_session_filter and not is_session_allowed(now_ms, cfg.blocked_sessions):
        return f'session_blocked={session_for_ts(now_ms)}'
    if not passes_regime_filters(
        trend_bullish=None, adx_val=adx_val,
        adx_threshold=cfg.adx_threshold,
        use_trend_filter=cfg.use_trend_filter,
        use_adx_filter=cfg.use_adx_filter,
    ):
        return f'adx_overheated={adx_val}'
    if cfg.range_lookback_min > 0 and len(last_closes) >= cfg.range_lookback_min:
        window = last_closes[-cfg.range_lookback_min:]
        if is_quiet_range(window, cfg.range_pct_threshold):
            return 'quiet_range'
    if cfg.use_volatility_filter and not passes_volatility_window(atr_val, atr_bounds):
        return f'volatility_outside_window atr={atr_val}'
    if cfg.use_mtf_filter and not is_mtf_aligned(
        side, htf_15m, htf_1h,
        require_15m=cfg.mtf_require_15m, require_1h=cfg.mtf_require_1h,
    ):
        return f'mtf_misaligned side={side} 15m={htf_15m} 1h={htf_1h}'
    if side == 'short':
        if cfg.use_short_trend_filter and not passes_short_trend_filter(
            close, sma50, adx_val, cfg.short_adx_min,
        ):
            return f'short_trend_filter close={close} sma50={sma50} adx={adx_val}'
        if shorts_disabled_in_flat(adx_val, cfg.adx_flat_threshold):
            return f'shorts_disabled_in_flat adx={adx_val}'
    return None


def _handle_waiting_signal(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    sma_val: float,
    rsi_val: float,
    atr_val: float | None,
    adx_val: float | None,
    sma50: float | None = None,
    atr_bounds: tuple[float, float] | None = None,
    htf_15m: bool | None = None,
    htf_1h: bool | None = None,
    last_closes: list[float] | None = None,
    now_ms: int | None = None,
    macro_mode: str | None = None,
    htf_4h_trend: str | None = None,
    htf_daily_trend: str | None = None,
    atr_pct: float | None = None,
    range_quiet: bool = False,
    kelly_used: float | None = None,
) -> str | None:
    long_sig = should_enter(close, sma_val, rsi_val, rsi_threshold=cfg.rsi_threshold)
    short_sig = should_enter_short(close, sma_val, rsi_val, rsi_threshold=cfg.rsi_short_threshold)
    if macro_mode == NO_TRADE and long_sig:
        logger.info('macro_no_trade_blocks_long')
        long_sig = False
    if long_sig and short_sig:
        logger.warning(
            'contradictory_signals close=%.4f rsi=%.2f long_thr=%.1f short_thr=%.1f',
            close, rsi_val, cfg.rsi_threshold, cfg.rsi_short_threshold,
        )
        return None
    if not long_sig and not short_sig:
        logger.info(
            'no_signal close=%.4f sma%d=%.4f rsi%d=%.2f long_thr=%.1f short_thr=%.1f',
            close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val,
            cfg.rsi_threshold, cfg.rsi_short_threshold,
        )
        return None
    side = 'long' if long_sig else 'short'
    closes_view = last_closes if last_closes is not None else []
    ts_now = now_ms if now_ms is not None else int(time.time() * 1000)

    block_reason = _entry_filters_block(
        cfg, side, close, sma_val, sma50, adx_val, atr_val, atr_bounds,
        htf_15m, htf_1h, closes_view, ts_now,
    )
    if block_reason is not None:
        logger.info('entry_blocked side=%s reason=%s', side, block_reason)
        return None

    sl_price, tp_price = _compute_sl_tp_for_side(close, cfg, atr_val, side)
    effective_sl_pct = abs(close - sl_price) / close
    # Adaptive Kelly: read effective_risk_pct from risk_manager (already
    # updated each tick via _update_adaptive_kelly_from_recent).
    risk_for_size = getattr(risk_manager, 'effective_risk_pct', cfg.risk_pct)
    notional = risk_manager.position_size(
        cfg.balance, risk_pct=risk_for_size, sl_pct=effective_sl_pct,
    )
    if notional <= 0:
        return None
    qty = notional / close
    logger.info(
        'entry_signal side=%s close=%.4f sma=%.4f rsi=%.2f sl=%.4f tp=%.4f',
        side, close, sma_val, rsi_val, sl_price, tp_price,
    )
    context = {
        'session':           session_for_ts(ts_now),
        'rsi':               rsi_val,
        'adx':               adx_val,
        'atr':               atr_val,
        'atr_pct':           atr_pct,
        'sma20':             sma_val,
        'sma50':             sma50,
        'mtf_15m_aligned':   None if htf_15m is None else (
            (side == 'long' and htf_15m) or (side == 'short' and not htf_15m)
        ),
        'trend_15m':         htf_15m,
        'htf_4h_trend':      htf_4h_trend,
        'htf_daily_trend':   htf_daily_trend,
        'regime':            classify_regime(adx_val, atr_pct, range_quiet),
        'macro_event':       macro_event_for_ts(ts_now) or None,
        'kelly_used':        kelly_used if kelly_used is not None else cfg.risk_pct,
    }
    # Shadow mode: log the decision (not the position) for offline comparison
    try:
        from analytics.shadow import record_decision
        record_decision({
            'decision_ts_ms': ts_now,
            'side':           side,
            'entry_price':    close,
            'sl_price':       sl_price,
            'tp_price':       tp_price,
        })
    except Exception as exc:
        logger.debug('shadow_decision_record_failed error=%s', exc)
    return _open_position(state_manager, close, qty, sl_price, tp_price,
                          side=side, context=context)


def _maybe_apply_trailing(
    state_manager: StateManager,
    cfg: _LoopConfig,
    position: dict,
    close: float,
) -> tuple[dict, str | None]:
    """Update trailing stop if active. Returns (position, transition)."""
    if not cfg.use_trailing_stop:
        return position, None
    new_sl, transition = update_trailing_stop_pct(
        sl_price=position['sl_price'],
        entry_price=position['entry_price'],
        close=close,
        side=position.get('side', 'long'),
        breakeven_at_pct=cfg.trailing_breakeven_pct,
        trail_at_pct=cfg.trailing_trail_pct,
        trail_distance_pct=cfg.trailing_distance_pct,
    )
    if transition is not None:
        old_sl = position['sl_price']
        position['sl_price'] = new_sl
        state_manager.set_position(position)
        logger.info(
            'trailing_stop_%s side=%s old_sl=%.4f new_sl=%.4f',
            transition, position.get('side', 'long'), old_sl, new_sl,
        )
    return position, transition


def _maybe_tighten_stalled(
    state_manager: StateManager,
    cfg: _LoopConfig,
    position: dict,
    closes_during_position: list[float],
    now_ms: int,
) -> dict:
    """Halve SL/TP distances when the position has gone nowhere for too long."""
    if cfg.stalled_hours <= 0 or position.get('stalled_tightened'):
        return position
    elapsed_h = (now_ms - position.get('ts', now_ms)) / 3_600_000
    if elapsed_h < cfg.stalled_hours:
        return position
    if not is_position_stalled(closes_during_position, cfg.stalled_move_threshold):
        return position
    side = position.get('side', 'long')
    new_sl, new_tp = tighten_sl_tp_for_stalled(
        position['sl_price'], position['tp_price'],
        position['entry_price'], side,
    )
    logger.info(
        'stalled_tightened side=%s entry=%.4f sl_old=%.4f sl_new=%.4f tp_old=%.4f tp_new=%.4f',
        side, position['entry_price'], position['sl_price'], new_sl,
        position['tp_price'], new_tp,
    )
    position['sl_price'] = new_sl
    position['tp_price'] = new_tp
    position['stalled_tightened'] = True
    state_manager.set_position(position)
    return position


def _handle_in_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    atr_val: float | None,
    closes_during_position: list[float] | None = None,
    now_ms: int | None = None,
) -> tuple[str | None, str | None] | str | None:
    """Returns (notification, trailing_transition).

    Backward-compat: when called without closes_during_position/now_ms (legacy
    test signature), returns a single notification string instead of a tuple.
    """
    legacy_call = closes_during_position is None and now_ms is None
    if closes_during_position is None:
        closes_during_position = []
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    position = state_manager.get_position()
    if position is None:
        logger.error('state=IN_POSITION but no position found, resetting')
        state_manager.set_state(BotState.WAITING_SIGNAL)
        return None if legacy_call else (None, None)
    position = _ensure_sl_tp(position, cfg)
    side = position.get('side', 'long')

    position, transition = _maybe_apply_trailing(state_manager, cfg, position, close)
    position = _maybe_tighten_stalled(state_manager, cfg, position,
                                      closes_during_position, now_ms)

    if side == 'long':
        pnl_usdt, pnl_pct = calc_pnl(close, position['entry_price'], position['qty'])
    else:
        pnl_usdt, pnl_pct = calc_pnl_short(close, position['entry_price'], position['qty'])
    logger.info(
        'unrealized_pnl side=%s pnl=%.4f pnl_pct=%.2f%% sl=%.4f tp=%.4f',
        side, pnl_usdt, pnl_pct, position['sl_price'], position['tp_price'],
    )
    reason = check_exit_price(close, position['sl_price'], position['tp_price'], side=side)
    if reason is None and should_exit_time(
        int(position.get('ts', 0)), now_ms, cfg.max_hold_hours,
    ):
        reason = 'time_exit'
        logger.info(
            'time_exit triggered side=%s held_hours=%.1f',
            side, (now_ms - position.get('ts', 0)) / 3_600_000,
        )
    if reason:
        notif = _close_position(state_manager, risk_manager, close, reason)
        return notif if legacy_call else (notif, transition)
    return None if legacy_call else (None, transition)


# ── ATR / closes history (per-process, seeded from buffer each tick) ─────

class _LoopState:
    """Mutable state held across ticks: ATR history, in-position closes."""

    def __init__(self) -> None:
        self.atr_history: deque[float] = deque(maxlen=_ATR_HISTORY_MAXLEN)
        self.position_closes: deque[float] = deque(maxlen=_POS_CLOSES_MAXLEN)
        self.last_pos_ts: int = 0


async def _process_tick(
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    candles: list[dict[str, Any]],
    loop_state: _LoopState,
    macro_mode: str | None = None,
) -> None:
    buffer.add_many(candles)
    if not buffer.is_ready(_MIN_CANDLES):
        logger.debug('buffer_not_ready len=%d required=%d', len(buffer), _MIN_CANDLES)
        return
    df = buffer.to_dataframe()
    close, sma_v, rsi_v, volume, vol_s, atr_v, adx_v = _compute_indicators(df)
    sma50 = _compute_sma_safe(df, cfg.short_sma_period)
    bot_state = state_manager.get_state()
    now_ms = int(time.time() * 1000)
    _log_tick(
        cfg.symbol, close, sma_v, rsi_v,
        volume / vol_s if vol_s > 0 else 0.0, atr_v, adx_v, bot_state,
    )
    _update_health(close, rsi_v, bot_state, risk_manager.get_daily_pnl())

    if atr_v is not None:
        loop_state.atr_history.append(atr_v)
    atr_bounds = atr_percentile_bounds(
        list(loop_state.atr_history),
        low_p=cfg.volatility_low_pct, high_p=cfg.volatility_high_pct,
    ) if cfg.use_volatility_filter else None

    htf_15m = _compute_mtf_15m(df, cfg.mtf_15m_period) if cfg.use_mtf_filter else None
    htf_1h  = _compute_mtf_1h(df) if cfg.use_mtf_filter and cfg.mtf_require_1h else None
    htf_4h_trend = _compute_htf_trend(df, '4h', period=50)
    htf_daily_trend = _compute_htf_trend(df, '1D', period=50)

    last_closes: list[float] = [float(c) for c in df['close'].tolist()]
    atr_pct_now = percentile_of(atr_v, sorted(loop_state.atr_history))
    range_quiet_now = (
        cfg.range_lookback_min > 0
        and len(last_closes) >= cfg.range_lookback_min
        and is_quiet_range(last_closes[-cfg.range_lookback_min:],
                           cfg.range_pct_threshold)
    )

    notification: str | None = None
    transition: str | None = None
    if bot_state == BotState.WAITING_SIGNAL:
        # Refresh adaptive Kelly before each entry decision
        _update_adaptive_kelly_from_recent(risk_manager)
        # Macro-event auto-pause check (does not flip the persistent flag)
        macro_event = macro_event_for_ts(now_ms)
        macro_block = False
        if is_high_impact_event(now_ms):
            try:
                from analytics.macro_pause import should_auto_pause_for_macro
                macro_block, reason = should_auto_pause_for_macro(
                    list_live_trades(limit=200), macro_event,
                )
                if macro_block:
                    logger.info('macro_auto_pause %s', reason)
            except Exception as exc:
                logger.debug('macro_auto_pause_check_failed error=%s', exc)
        if _is_paused() or macro_block:
            logger.info('bot_paused — skipping entry evaluation')
        else:
            notification = _handle_waiting_signal(
                state_manager, risk_manager, cfg, close, sma_v, rsi_v,
                atr_v, adx_v,
                sma50=sma50, atr_bounds=atr_bounds,
                htf_15m=htf_15m, htf_1h=htf_1h,
                last_closes=last_closes, now_ms=now_ms,
                macro_mode=macro_mode,
                htf_4h_trend=htf_4h_trend,
                htf_daily_trend=htf_daily_trend,
                atr_pct=atr_pct_now,
                range_quiet=range_quiet_now,
                kelly_used=getattr(risk_manager, 'effective_risk_pct', None),
            )
            if notification is None and cfg.near_miss_rsi_band > 0:
                miss = near_miss_reason(
                    close, sma_v, rsi_v,
                    rsi_long_threshold=cfg.rsi_threshold,
                    rsi_short_threshold=cfg.rsi_short_threshold,
                    rsi_band=cfg.near_miss_rsi_band,
                    sma_band_frac=cfg.near_miss_sma_band,
                )
                if miss is not None:
                    logger.info('near_miss %s', miss)
                    _record_near_miss_db(miss, close, rsi_v, sma_v, cfg, now_ms)
                    await notify_near_miss(miss, close, rsi_v, sma_v)
        # reset position closes when waiting
        loop_state.position_closes.clear()
        loop_state.last_pos_ts = 0
    elif bot_state == BotState.IN_POSITION:
        position = state_manager.get_position()
        if position is not None:
            pos_ts = int(position.get('ts', 0))
            if pos_ts != loop_state.last_pos_ts:
                # New position — reset the running closes
                loop_state.position_closes.clear()
                loop_state.last_pos_ts = pos_ts
            loop_state.position_closes.append(close)
        notification, transition = _handle_in_position(
            state_manager, risk_manager, cfg, close, atr_v,
            list(loop_state.position_closes), now_ms,
        )

    if transition is not None and notification is None:
        await notify_trailing(transition, close,
                              state_manager.get_position() or {})
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
    protections: ProtectionStack | None = None,
) -> None:
    """Main trading loop. Streams candles via WebSocket; falls back to REST polling."""
    cfg = _parse_config(config)
    logger.info(
        'trading_loop started symbol=%s timeframe=%s interval=%ss balance=%.2f '
        'risk_pct=%.4f use_atr_exits=%s use_trailing_stop=%s '
        'use_adx_filter=%s adx_threshold=%.1f use_mtf_filter=%s '
        'use_volatility_filter=%s use_session_filter=%s',
        cfg.symbol, cfg.timeframe, cfg.interval, cfg.balance,
        cfg.risk_pct, cfg.use_atr_exits, cfg.use_trailing_stop,
        cfg.use_adx_filter, cfg.adx_threshold, cfg.use_mtf_filter,
        cfg.use_volatility_filter, cfg.use_session_filter,
    )

    loop_state = _LoopState()
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
        macro_mode = None
        if macro_filter is not None:
            macro_mode = await macro_filter.get_mode()
        if protections is not None:
            now_ms = int(time.time() * 1000)
            blocked, reason = protections.is_blocked(now_ms, _load_trades())
            if blocked:
                logger.info('protections_blocked reason=%s', reason)
                return
        await _process_tick(buffer, state_manager, risk_manager, cfg, candles,
                            loop_state, macro_mode=macro_mode)

    await client.watch_candles(
        cfg.symbol, cfg.timeframe, _on_candles,
        limit=cfg.limit, rest_interval=cfg.interval,
    )
