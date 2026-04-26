"""
Backtest engine — replays BTC/USDT 1m data from Binance production through
the strategy, runs two sweeps:
  1. Multi-param entry conditions (RSI threshold × SMA period)
  2. SL/TP optimisation for the best entry config (RSI<40 + SMA20)

Usage (from project root):
    python3 -m backtest.engine
"""

import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import BOT_CONFIG
from strategy.indicators import rsi, sma, volume_sma
from strategy.signals import calc_pnl, check_exit, should_enter_mean_rev

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

# ── fixed parameters ───────────────────────────────────────────────────────
_SYMBOL        = BOT_CONFIG['symbol']
_TIMEFRAME     = BOT_CONFIG['timeframe']
_BALANCE       = float(BOT_CONFIG.get('paper_balance', 10_000.0))
_RISK_PCT      = float(BOT_CONFIG.get('risk_pct', 0.01))
_SL_PCT        = float(BOT_CONFIG.get('stop_loss_pct', 0.02))
_TP_PCT        = float(BOT_CONFIG.get('take_profit_pct', 0.03))
_RSI_PERIOD    = 14
_LOOKBACK_DAYS = 90
_PAGE_SIZE     = 1000
_RESULTS_DIR   = Path(__file__).parent / 'results'
_REPORT_FILE   = _RESULTS_DIR / 'report.json'         # kept for tests
_MULTI_FILE    = _RESULTS_DIR / 'multi_report.json'
_SLTP_FILE     = _RESULTS_DIR / 'sltp_report.json'
_SYMBOL_FILE        = _RESULTS_DIR / 'symbol_report.json'
_STRATEGY_COMP_FILE = _RESULTS_DIR / 'strategy_comp_report.json'
_VOL_COMP_FILE      = _RESULTS_DIR / 'volume_comp_report.json'
_SYMBOLS            = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
_VOL_SMA_PERIOD     = 20
_VOL_FACTOR         = 1.2


# ── parameter set ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParamSet:
    label:         str
    rsi_threshold: float
    sma_period:    int | None   # None = no SMA filter
    sl_pct:        float = field(default=0.02)
    tp_pct:        float = field(default=0.03)
    strategy:      str          = field(default='rsi_sma')  # 'rsi_sma' | 'mean_rev' | 'combined'
    drop_pct:      float        = field(default=0.015)      # mean_rev: entry drop threshold
    lookback:      int          = field(default=10)         # mean_rev: bars to measure drop
    volume_factor: float | None = field(default=None)       # None = no volume filter; e.g. 1.2


# ── sweep 1: entry-condition combinations (SL/TP fixed from config) ────────

_PARAM_SETS: list[ParamSet] = [
    ParamSet('RSI<35 + SMA20',   35.0, 20, _SL_PCT, _TP_PCT),
    ParamSet('RSI<40 + SMA20',   40.0, 20, _SL_PCT, _TP_PCT),
    ParamSet('RSI<45 + SMA20',   45.0, 20, _SL_PCT, _TP_PCT),
    ParamSet('RSI<35 + SMA50',   35.0, 50, _SL_PCT, _TP_PCT),
    ParamSet('RSI<40 + SMA50',   40.0, 50, _SL_PCT, _TP_PCT),
    ParamSet('RSI<35  (no SMA)', 35.0, None, _SL_PCT, _TP_PCT),
]

# ── sweep 2: SL/TP optimisation for RSI<40 + SMA20 ────────────────────────

_SLTP_SETS: list[ParamSet] = [
    ParamSet('SL2.0% / TP3.0% (base)', 40.0, 20, 0.020, 0.030),
    ParamSet('SL1.5% / TP2.5%',        40.0, 20, 0.015, 0.025),
    ParamSet('SL1.5% / TP3.0%',        40.0, 20, 0.015, 0.030),
    ParamSet('SL2.0% / TP3.5%',        40.0, 20, 0.020, 0.035),
    ParamSet('SL2.0% / TP4.0%',        40.0, 20, 0.020, 0.040),
    ParamSet('SL2.5% / TP4.0%',        40.0, 20, 0.025, 0.040),
]

# ── sweep 3: multi-symbol with winning config ──────────────────────────────

_WINNER   = ParamSet('SL2.5% / TP4.0%  RSI<40 SMA20', 40.0, 20, 0.025, 0.040)

# ── sweep 4: mean reversion strategy ──────────────────────────────────────

_MEAN_REV = ParamSet(
    label='MeanRev drop>1.5%/10m SL1%TP1%',
    rsi_threshold=0.0,   # unused for mean_rev strategy
    sma_period=None,     # unused for mean_rev strategy
    sl_pct=0.01,
    tp_pct=0.01,
    strategy='mean_rev',
    drop_pct=0.015,
    lookback=10,
)

# ── sweep 5: combined RSI+SMA+mean-rev filter ──────────────────────────────

_COMBINED = ParamSet(
    label='RSI<40+SMA20+drop>1%/10m',
    rsi_threshold=40.0,
    sma_period=20,
    sl_pct=0.025,
    tp_pct=0.040,
    strategy='combined',
    drop_pct=0.01,    # 1% drop over lookback bars
    lookback=10,
)

# ── sweep 6: volume confirmation ───────────────────────────────────────────

_WINNER_VOL = ParamSet(
    label=f'RSI<40+SMA20+Vol>{_VOL_FACTOR}x',
    rsi_threshold=40.0,
    sma_period=20,
    sl_pct=0.025,
    tp_pct=0.040,
    volume_factor=_VOL_FACTOR,
)


# ── exchange ───────────────────────────────────────────────────────────────

def _make_exchange() -> ccxt.binance:
    """Binance production, no credentials needed for public OHLCV."""
    return ccxt.binance({
        'timeout': 15_000,
        'options': {'defaultType': 'spot'},
    })


# ── data fetching ──────────────────────────────────────────────────────────

def _to_dataframe(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    return df


def _fetch_historical(
    exchange: ccxt.binance,
    symbol: str = _SYMBOL,
) -> pd.DataFrame:
    now_ms   = int(time.time() * 1000)
    since_ms = now_ms - _LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    rows: list[list] = []
    current  = since_ms
    pages    = 0

    while current < now_ms:
        page = exchange.fetch_ohlcv(symbol, _TIMEFRAME, since=current, limit=_PAGE_SIZE)
        if not page:
            break
        rows.extend(page)
        pages   += 1
        current  = page[-1][0] + 60_000
        if pages % 10 == 0:
            logger.info('fetching symbol=%s pages=%d candles=%d', symbol, pages, len(rows))
        if len(page) < _PAGE_SIZE:
            break
        time.sleep(0.25)

    logger.info('fetch_complete symbol=%s pages=%d total_candles=%d', symbol, pages, len(rows))
    return _to_dataframe(rows)


# ── simulation ─────────────────────────────────────────────────────────────

def _close_position(
    position: dict,
    exit_price: float,
    exit_ts: int,
    reason: str,
    balance: float,
) -> tuple[dict, float]:
    pnl_usdt, pnl_pct = calc_pnl(exit_price, position['entry_price'], position['qty'])
    trade = {
        'entry_price': position['entry_price'],
        'exit_price':  exit_price,
        'qty':         position['qty'],
        'pnl_usdt':    round(pnl_usdt, 6),
        'pnl_pct':     round(pnl_pct, 6),
        'result':      'WIN' if pnl_usdt >= 0 else 'LOSS',
        'reason':      reason,
        'entry_ts':    position['entry_ts'],
        'exit_ts':     exit_ts,
    }
    return trade, balance + pnl_usdt


def simulate_tick(close: float, state: dict) -> dict:
    """Pure per-tick exit decision. Side-aware.

    state keys: side ('long'|'short'), entry_price, qty, sl_pct, tp_pct
    Returns: {'exit_reason': 'take_profit'|'stop_loss'|None, 'pnl_usdt': float}
    """
    side = state['side']
    entry = state['entry_price']
    qty = state['qty']
    sl_pct = state['sl_pct']
    tp_pct = state['tp_pct']

    if side == 'long':
        change = (close - entry) / entry
        pnl_usdt = (close - entry) * qty
        if change <= -sl_pct:
            return {'exit_reason': 'stop_loss', 'pnl_usdt': pnl_usdt}
        if change >= tp_pct:
            return {'exit_reason': 'take_profit', 'pnl_usdt': pnl_usdt}
    elif side == 'short':
        change = (entry - close) / entry
        pnl_usdt = (entry - close) * qty
        if change <= -sl_pct:
            return {'exit_reason': 'stop_loss', 'pnl_usdt': pnl_usdt}
        if change >= tp_pct:
            return {'exit_reason': 'take_profit', 'pnl_usdt': pnl_usdt}
    else:
        raise ValueError(f'invalid side: {side!r}')

    return {'exit_reason': None, 'pnl_usdt': 0.0}


def _process_bar_config(
    close: float,
    sma_val: float | None,
    rsi_val: float,
    ts: int,
    position: dict | None,
    balance: float,
    rsi_threshold: float,
    sl_pct: float,
    tp_pct: float,
    volume_val: float | None = None,
    volume_sma_val: float | None = None,
    volume_factor: float | None = None,
) -> tuple[dict | None, float, dict | None]:
    """Return (new_position, new_balance, closed_trade_or_None).
    sma_val=None disables the SMA filter. volume_factor=None disables volume filter."""
    if position is None:
        rsi_ok = rsi_val < rsi_threshold
        sma_ok = sma_val is None or close > sma_val
        vol_ok = (volume_factor is None or volume_val is None or volume_sma_val is None
                  or volume_val > volume_sma_val * volume_factor)
        if rsi_ok and sma_ok and vol_ok:
            qty = (balance * _RISK_PCT) / close
            return {'entry_price': close, 'qty': qty, 'entry_ts': ts}, balance, None
        return None, balance, None

    reason = check_exit(close, position['entry_price'], sl_pct, tp_pct)
    if reason:
        trade, new_balance = _close_position(position, close, ts, reason, balance)
        return None, new_balance, trade
    return position, balance, None


def _simulate_config(
    df: pd.DataFrame,
    rsi_s: pd.Series,
    sma_s: pd.Series | None,
    rsi_threshold: float,
    min_candles: int,
    sl_pct: float,
    tp_pct: float,
    vol_sma_s: pd.Series | None = None,
    volume_factor: float | None = None,
) -> tuple[list[dict], list[float]]:
    balance  = _BALANCE
    trades:   list[dict]  = []
    equity:   list[float] = [balance]
    position: dict | None = None

    for i in range(min_candles, len(df)):
        close   = float(df['close'].iloc[i])
        rsi_val = float(rsi_s.iloc[i])
        sma_val = float(sma_s.iloc[i]) if sma_s is not None else None
        if pd.isna(rsi_val) or (sma_val is not None and pd.isna(sma_val)):
            continue
        vol_sma_val = float(vol_sma_s.iloc[i]) if vol_sma_s is not None else None
        position, balance, trade = _process_bar_config(
            close, sma_val, rsi_val, int(df['ts'].iloc[i]),
            position, balance, rsi_threshold, sl_pct, tp_pct,
            float(df['volume'].iloc[i]) if vol_sma_s is not None else None,
            vol_sma_val,
            volume_factor,
        )
        if trade:
            trades.append(trade)
            equity.append(balance)

    return trades, equity


def _simulate_mean_rev(
    df: pd.DataFrame,
    params: ParamSet,
) -> tuple[list[dict], list[float]]:
    """Simulate mean-reversion strategy: enter when price drops > drop_pct over lookback bars."""
    drop_s    = df['close'].pct_change(params.lookback)
    balance   = _BALANCE
    trades:   list[dict]  = []
    equity:   list[float] = [balance]
    position: dict | None = None

    for i in range(params.lookback, len(df)):
        close    = float(df['close'].iloc[i])
        drop_val = float(drop_s.iloc[i])
        if pd.isna(drop_val):
            continue
        ts = int(df['ts'].iloc[i])
        if position is None:
            if should_enter_mean_rev(drop_val, params.drop_pct):
                qty      = (balance * _RISK_PCT) / close
                position = {'entry_price': close, 'qty': qty, 'entry_ts': ts}
            continue
        reason = check_exit(close, position['entry_price'], params.sl_pct, params.tp_pct)
        if reason:
            trade, balance = _close_position(position, close, ts, reason, balance)
            trades.append(trade)
            equity.append(balance)
            position = None

    return trades, equity


def _simulate_combined(
    df: pd.DataFrame,
    rsi_s: pd.Series,
    sma_s: pd.Series | None,
    params: ParamSet,
) -> tuple[list[dict], list[float]]:
    """RSI<threshold AND close>SMA AND price dropped >drop_pct over lookback bars."""
    drop_s    = df['close'].pct_change(params.lookback)
    min_c     = max(_RSI_PERIOD, params.sma_period or 0, params.lookback)
    balance   = _BALANCE
    trades:   list[dict]  = []
    equity:   list[float] = [balance]
    position: dict | None = None

    for i in range(min_c, len(df)):
        close    = float(df['close'].iloc[i])
        rsi_val  = float(rsi_s.iloc[i])
        sma_val  = float(sma_s.iloc[i]) if sma_s is not None else None
        drop_val = float(drop_s.iloc[i])
        if pd.isna(rsi_val) or pd.isna(drop_val) or (sma_val is not None and pd.isna(sma_val)):
            continue
        ts = int(df['ts'].iloc[i])
        if position is None:
            rsi_ok  = rsi_val < params.rsi_threshold
            sma_ok  = sma_val is None or close > sma_val
            drop_ok = should_enter_mean_rev(drop_val, params.drop_pct)
            if rsi_ok and sma_ok and drop_ok:
                qty      = (balance * _RISK_PCT) / close
                position = {'entry_price': close, 'qty': qty, 'entry_ts': ts}
            continue
        reason = check_exit(close, position['entry_price'], params.sl_pct, params.tp_pct)
        if reason:
            trade, balance = _close_position(position, close, ts, reason, balance)
            trades.append(trade)
            equity.append(balance)
            position = None

    return trades, equity


# ── metrics ────────────────────────────────────────────────────────────────

def _max_drawdown(equity: list[float]) -> float:
    peak   = equity[0]
    max_dd = 0.0
    for value in equity:
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(returns: list[float]) -> float:
    """Per-trade Sharpe ratio (non-annualized)."""
    if len(returns) < 2:
        return 0.0
    n        = len(returns)
    mean_r   = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r    = math.sqrt(variance) if variance > 0 else 0.0
    return mean_r / std_r if std_r > 0 else 0.0


def _ts_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _make_period(df: pd.DataFrame) -> dict:
    return {
        'from':    _ts_to_iso(int(df['ts'].iloc[0])),
        'to':      _ts_to_iso(int(df['ts'].iloc[-1])),
        'candles': len(df),
    }


def _trade_metrics(trades: list[dict], equity: list[float]) -> dict:
    wins        = [t for t in trades if t['result'] == 'WIN']
    total_pnl   = sum(t['pnl_usdt'] for t in trades)
    returns     = [t['pnl_pct'] / 100 for t in trades]
    best_trade  = max(trades, key=lambda t: t['pnl_usdt'])
    worst_trade = min(trades, key=lambda t: t['pnl_usdt'])
    return {
        'total_pnl_usdt':   round(total_pnl, 4),
        'total_pnl_pct':    round(total_pnl / _BALANCE * 100, 4),
        'num_wins':         len(wins),
        'num_losses':       len(trades) - len(wins),
        'win_rate_pct':     round(len(wins) / len(trades) * 100, 2),
        'max_drawdown_pct': round(_max_drawdown(equity) * 100, 4),
        'sharpe_ratio':     round(_sharpe(returns), 4),
        'best_trade_usdt':  round(best_trade['pnl_usdt'], 4),
        'worst_trade_usdt': round(worst_trade['pnl_usdt'], 4),
    }


def _compute_metrics(
    trades: list[dict],
    equity: list[float],
    df: pd.DataFrame,
) -> dict:
    """Single-config metrics report. Kept for backward compatibility."""
    data_from = _ts_to_iso(int(df['ts'].iloc[0]))
    data_to   = _ts_to_iso(int(df['ts'].iloc[-1]))
    report: dict = {
        'period':     {'from': data_from, 'to': data_to, 'candles': len(df)},
        'parameters': {'symbol': _SYMBOL, 'timeframe': _TIMEFRAME,
                       'sl_pct': _SL_PCT, 'tp_pct': _TP_PCT, 'risk_pct': _RISK_PCT},
        'initial_balance': _BALANCE,
        'final_balance':   round(equity[-1], 4),
        'num_trades':      len(trades),
    }
    if trades:
        report.update(_trade_metrics(trades, equity))
        report['trades'] = trades
    else:
        report['note'] = 'no trades executed during this period'
    return report


# ── single-run output (kept for backward compat / tests) ──────────────────

def _save_report(report: dict) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _REPORT_FILE.write_text(json.dumps(report, indent=2))
    logger.info('report_saved path=%s', _REPORT_FILE)


def _log_summary(report: dict) -> None:
    sep = '─' * 52
    logger.info(sep)
    logger.info('BACKTEST  %s  →  %s', report['period']['from'], report['period']['to'])
    logger.info('Candles : %d   |   Trades: %d', report['period']['candles'], report['num_trades'])
    logger.info(sep)
    if report.get('note'):
        logger.info('Note: %s', report['note'])
        logger.info(sep)
        return
    sign = '+' if report['total_pnl_usdt'] >= 0 else ''
    logger.info('PnL         : %s%.4f USDT  (%s%.2f%%)',
                sign, report['total_pnl_usdt'], sign, report['total_pnl_pct'])
    logger.info('Win rate    : %.1f%%  (%d W / %d L)',
                report['win_rate_pct'], report['num_wins'], report['num_losses'])
    logger.info('Max drawdown: %.4f%%', report['max_drawdown_pct'])
    logger.info('Sharpe ratio: %.4f  (per-trade, non-annualized)', report['sharpe_ratio'])
    logger.info('Best trade  : +%.4f USDT', report['best_trade_usdt'])
    logger.info('Worst trade :  %.4f USDT', report['worst_trade_usdt'])
    logger.info(sep)


# ── generic param sweep ────────────────────────────────────────────────────

def _precompute_sma_cache(
    df: pd.DataFrame,
    param_sets: list[ParamSet],
) -> dict[int, pd.Series]:
    """Compute each unique SMA period needed by param_sets exactly once."""
    periods = {p.sma_period for p in param_sets if p.sma_period is not None}
    return {period: sma(df, period) for period in periods}


def _run_simulation(
    df: pd.DataFrame,
    rsi_s: pd.Series,
    sma_cache: dict[int, pd.Series],
    params: ParamSet,
) -> tuple[list[dict], list[float]]:
    """Dispatch to the correct simulate function based on params.strategy."""
    if params.strategy == 'mean_rev':
        return _simulate_mean_rev(df, params)
    sma_s = sma_cache.get(params.sma_period) if params.sma_period is not None else None
    if params.strategy == 'combined':
        return _simulate_combined(df, rsi_s, sma_s, params)
    min_c = max(_RSI_PERIOD, params.sma_period or 0)
    vol_sma_s = volume_sma(df, _VOL_SMA_PERIOD) if params.volume_factor is not None else None
    return _simulate_config(
        df, rsi_s, sma_s, params.rsi_threshold, min_c, params.sl_pct, params.tp_pct,
        vol_sma_s, params.volume_factor,
    )


def _run_one_config(
    df: pd.DataFrame,
    rsi_s: pd.Series,
    sma_cache: dict[int, pd.Series],
    params: ParamSet,
) -> dict:
    trades, eq = _run_simulation(df, rsi_s, sma_cache, params)
    result: dict = {
        'label':        params.label,
        'parameters':   {'strategy': params.strategy,
                         'rsi_threshold': params.rsi_threshold, 'sma_period': params.sma_period,
                         'sl_pct': params.sl_pct, 'tp_pct': params.tp_pct,
                         'drop_pct': params.drop_pct, 'lookback': params.lookback},
        'num_trades':   len(trades),
        'sharpe_ratio': 0.0,
    }
    if trades:
        result.update(_trade_metrics(trades, eq))
        result['trades'] = trades
    logger.info('config=%-40s trades=%d', params.label, len(trades))
    return result


def _run_param_sweep(
    df: pd.DataFrame,
    param_sets: list[ParamSet],
) -> list[dict]:
    rsi_s     = rsi(df, _RSI_PERIOD)
    sma_cache = _precompute_sma_cache(df, param_sets)
    return [_run_one_config(df, rsi_s, sma_cache, p) for p in param_sets]


# ── output ─────────────────────────────────────────────────────────────────

def _log_table_row(r: dict) -> None:
    if r['num_trades'] == 0:
        logger.info('%-26s  %5d  %9s  %9s  %8s  %7s  %7s  %8s  %8s',
                    r['label'], 0, '-', '-', '-', '-', '-', '-', '-')
        return
    sp = '+' if r['total_pnl_usdt'] >= 0 else ''
    logger.info(
        '%-26s  %5d  %8.1f%%  %s%8.2f  %s%6.2f%%  %6.2f%%  %7.4f  %+8.2f  %+8.2f',
        r['label'], r['num_trades'], r['win_rate_pct'],
        sp, r['total_pnl_usdt'], sp, r['total_pnl_pct'],
        r['max_drawdown_pct'], r['sharpe_ratio'],
        r['best_trade_usdt'], r['worst_trade_usdt'],
    )


def _log_comparison_table(
    results: list[dict],
    data_from: str,
    data_to: str,
    candles: int,
    title: str = 'MULTI-PARAM BACKTEST',
    subtitle: str = '',
) -> None:
    ranked = sorted(results, key=lambda r: r.get('sharpe_ratio', -999), reverse=True)
    sep = '─' * 96
    hdr = (f"{'Config':<26}  {'#':>5}  {'WinRate':>8}  {'PnL USDT':>9}  "
           f"{'PnL%':>7}  {'MaxDD%':>6}  {'Sharpe':>7}  {'Best':>8}  {'Worst':>8}")
    logger.info(sep)
    logger.info('%s  %s → %s  |  %d candles', title, data_from, data_to, candles)
    if subtitle:
        logger.info(subtitle)
    logger.info(sep)
    logger.info(hdr)
    logger.info('─' * 96)
    for r in ranked:
        _log_table_row(r)
    logger.info(sep)


def _save_report_to(
    results: list[dict],
    period: dict,
    path: Path,
) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ranked = sorted(results, key=lambda r: r.get('sharpe_ratio', -999), reverse=True)
    out = {
        'period':   period,
        'risk_pct': _RISK_PCT,
        'balance':  _BALANCE,
        'configs':  ranked,
    }
    path.write_text(json.dumps(out, indent=2))
    logger.info('report_saved path=%s', path)


def _report_sweep(
    results: list[dict],
    period: dict,
    path: Path,
    title: str,
    subtitle: str,
) -> None:
    _save_report_to(results, period, path)
    _log_comparison_table(
        results, period['from'], period['to'], period['candles'],
        title=title, subtitle=subtitle,
    )


# ── multi-symbol functions ─────────────────────────────────────────────────

def _run_symbol(exchange: ccxt.binance, symbol: str) -> tuple[dict, dict]:
    """Fetch 90-day data for symbol, run _WINNER config, return (result, period)."""
    df     = _fetch_historical(exchange, symbol)
    period = _make_period(df)
    rsi_s  = rsi(df, _RSI_PERIOD)
    sma_c  = _precompute_sma_cache(df, [_WINNER])
    result = _run_one_config(df, rsi_s, sma_c, _WINNER)
    result['symbol'] = symbol
    return result, period


def _combined_result(per_symbol: list[dict]) -> dict:
    """Merge all per-symbol trades into aggregate metrics."""
    all_trades: list[dict] = []
    for r in per_symbol:
        all_trades.extend(r.get('trades', []))
    base: dict = {'num_trades': len(all_trades), 'sharpe_ratio': 0.0}
    if not all_trades:
        return base
    all_trades.sort(key=lambda t: t['entry_ts'])
    balance = _BALANCE * len(per_symbol)
    equity  = [balance]
    for t in all_trades:
        balance += t['pnl_usdt']
        equity.append(balance)
    base.update(_trade_metrics(all_trades, equity))
    return base


def _log_symbol_table(
    per_symbol: list[dict],
    combined: dict,
    data_from: str,
    data_to: str,
) -> None:
    sep = '─' * 96
    hdr = (f"{'Symbol':<26}  {'#':>5}  {'WinRate':>8}  {'PnL USDT':>9}  "
           f"{'PnL%':>7}  {'MaxDD%':>6}  {'Sharpe':>7}  {'Best':>8}  {'Worst':>8}")
    logger.info(sep)
    logger.info('MULTI-SYMBOL BACKTEST  config: %s  |  %s → %s', _WINNER.label, data_from, data_to)
    logger.info(sep)
    logger.info(hdr)
    logger.info('─' * 96)
    for r in per_symbol:
        _log_table_row(r)
    logger.info('─' * 96)
    _log_table_row(combined)
    logger.info(sep)


def _run_multi_symbol(exchange: ccxt.binance) -> None:
    per_symbol: list[dict] = []
    periods:    list[dict] = []
    for sym in _SYMBOLS:
        result, period = _run_symbol(exchange, sym)
        result['label'] = sym
        per_symbol.append(result)
        periods.append(period)
    combined         = _combined_result(per_symbol)
    combined['label'] = 'COMBINED'
    data_from = min(p['from'] for p in periods)
    data_to   = max(p['to']   for p in periods)
    _log_symbol_table(per_symbol, combined, data_from, data_to)
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        'config':   {'rsi_threshold': _WINNER.rsi_threshold, 'sma_period': _WINNER.sma_period,
                     'sl_pct': _WINNER.sl_pct, 'tp_pct': _WINNER.tp_pct},
        'symbols':  [{'symbol': r['symbol'], 'period': p, 'result': r}
                     for r, p in zip(per_symbol, periods)],
        'combined': combined,
    }
    _SYMBOL_FILE.write_text(json.dumps(out, indent=2))
    logger.info('report_saved path=%s', _SYMBOL_FILE)


# ── entry point ────────────────────────────────────────────────────────────

def run() -> None:
    logger.info('backtest_start symbol=%s timeframe=%s lookback_days=%d',
                _SYMBOL, _TIMEFRAME, _LOOKBACK_DAYS)
    exchange = _make_exchange()
    logger.info('downloading %d-day data from Binance production (no auth)...', _LOOKBACK_DAYS)
    df = _fetch_historical(exchange)
    if len(df) < 51:
        logger.error('insufficient_data candles=%d required_minimum=51', len(df))
        return
    period   = _make_period(df)
    multi_sub = (f'SL={_SL_PCT*100:.0f}%  TP={_TP_PCT*100:.0f}%  '
                 f'Risk={_RISK_PCT*100:.1f}%  Balance={_BALANCE:.0f} USDT')
    sltp_sub  = f'RSI<40 + SMA20  |  Risk/trade={_RISK_PCT*100:.1f}%  Balance={_BALANCE:.0f} USDT'
    logger.info('running entry-condition sweep (%d configs)...', len(_PARAM_SETS))
    _report_sweep(_run_param_sweep(df, _PARAM_SETS), period, _MULTI_FILE,
                  'MULTI-PARAM BACKTEST', multi_sub)
    logger.info('running SL/TP optimisation sweep (%d configs)...', len(_SLTP_SETS))
    _report_sweep(_run_param_sweep(df, _SLTP_SETS), period, _SLTP_FILE,
                  'SL/TP OPTIMISATION  RSI<40 + SMA20', sltp_sub)
    logger.info('running multi-symbol analysis (%s)...', ', '.join(_SYMBOLS))
    _run_multi_symbol(exchange)
    logger.info('running strategy comparison: rsi_sma vs mean_rev vs combined on BTC/USDT...')
    comp_sub = (f'Risk={_RISK_PCT*100:.1f}%  Balance={_BALANCE:.0f} USDT  |  '
                f'RSI+SMA SL2.5%/TP4.0%  |  MeanRev SL1%/TP1%  |  Combined SL2.5%/TP4.0%')
    _report_sweep(
        _run_param_sweep(df, [_WINNER, _MEAN_REV, _COMBINED]), period,
        _STRATEGY_COMP_FILE, 'STRATEGY COMPARISON  BTC/USDT', comp_sub,
    )
    logger.info('running volume confirmation comparison...')
    vol_sub = (f'Risk={_RISK_PCT*100:.1f}%  Balance={_BALANCE:.0f} USDT  |  '
               f'Both SL2.5%/TP4.0%  |  Vol filter={_VOL_FACTOR}x vol_sma{_VOL_SMA_PERIOD}')
    _report_sweep(
        _run_param_sweep(df, [_WINNER, _WINNER_VOL]), period,
        _VOL_COMP_FILE, 'VOLUME CONFIRMATION  BTC/USDT', vol_sub,
    )


if __name__ == '__main__':
    run()
