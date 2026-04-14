"""
Backtest engine — replays BTC/USDT 1m data from Binance production through
the strategy and sweeps 6 entry-condition combinations.

Usage (from project root):
    python3 -m backtest.engine
"""

import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import BOT_CONFIG
from strategy.indicators import rsi, sma
from strategy.signals import calc_pnl, check_exit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

# ── fixed parameters (SL/TP/sizing come from config.py) ───────────────────
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
_REPORT_FILE   = _RESULTS_DIR / 'report.json'       # kept for backward compat
_MULTI_FILE    = _RESULTS_DIR / 'multi_report.json'


# ── parameter combinations ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ParamSet:
    label:         str
    rsi_threshold: float
    sma_period:    int | None   # None = no SMA filter


_PARAM_SETS: list[ParamSet] = [
    ParamSet('RSI<35 + SMA20',   35.0, 20),
    ParamSet('RSI<40 + SMA20',   40.0, 20),
    ParamSet('RSI<45 + SMA20',   45.0, 20),
    ParamSet('RSI<35 + SMA50',   35.0, 50),
    ParamSet('RSI<40 + SMA50',   40.0, 50),
    ParamSet('RSI<35  (no SMA)', 35.0, None),
]


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


def _fetch_historical(exchange: ccxt.binance) -> pd.DataFrame:
    now_ms   = int(time.time() * 1000)
    since_ms = now_ms - _LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    rows: list[list] = []
    current  = since_ms
    pages    = 0

    while current < now_ms:
        page = exchange.fetch_ohlcv(_SYMBOL, _TIMEFRAME, since=current, limit=_PAGE_SIZE)
        if not page:
            break
        rows.extend(page)
        pages   += 1
        current  = page[-1][0] + 60_000       # next minute after last bar
        if pages % 10 == 0:
            logger.info('fetching pages=%d candles=%d', pages, len(rows))
        if len(page) < _PAGE_SIZE:
            break
        time.sleep(0.25)                       # stay well under rate limits

    logger.info('fetch_complete pages=%d total_candles=%d', pages, len(rows))
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


def _process_bar_config(
    close: float,
    sma_val: float | None,
    rsi_val: float,
    ts: int,
    position: dict | None,
    balance: float,
    rsi_threshold: float,
) -> tuple[dict | None, float, dict | None]:
    """Return (new_position, new_balance, closed_trade_or_None).

    sma_val=None disables the SMA filter so entry fires on RSI alone.
    """
    if position is None:
        rsi_ok = rsi_val < rsi_threshold
        sma_ok = sma_val is None or close > sma_val
        if rsi_ok and sma_ok:
            qty = (balance * _RISK_PCT) / close
            return {'entry_price': close, 'qty': qty, 'entry_ts': ts}, balance, None
        return None, balance, None

    reason = check_exit(close, position['entry_price'], _SL_PCT, _TP_PCT)
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
        position, balance, trade = _process_bar_config(
            close, sma_val, rsi_val, int(df['ts'].iloc[i]), position, balance, rsi_threshold,
        )
        if trade:
            trades.append(trade)
            equity.append(balance)

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


# ── multi-parameter sweep ──────────────────────────────────────────────────

def _precompute_sma_cache(df: pd.DataFrame) -> dict[int, pd.Series]:
    """Compute each unique SMA period needed by _PARAM_SETS exactly once."""
    periods = {p.sma_period for p in _PARAM_SETS if p.sma_period is not None}
    return {period: sma(df, period) for period in periods}


def _run_one_config(
    df: pd.DataFrame,
    rsi_s: pd.Series,
    sma_cache: dict[int, pd.Series],
    params: ParamSet,
) -> dict:
    sma_s      = sma_cache.get(params.sma_period) if params.sma_period is not None else None
    min_c      = max(_RSI_PERIOD, params.sma_period or 0)
    trades, eq = _simulate_config(df, rsi_s, sma_s, params.rsi_threshold, min_c)
    result: dict = {
        'label':         params.label,
        'parameters':    {'rsi_threshold': params.rsi_threshold, 'sma_period': params.sma_period},
        'num_trades':    len(trades),
        'sharpe_ratio':  0.0,             # default when no trades
    }
    if trades:
        result.update(_trade_metrics(trades, eq))
        result['trades'] = trades
    logger.info('config=%-22s trades=%d', params.label, len(trades))
    return result


def _run_multi_analysis(df: pd.DataFrame) -> list[dict]:
    rsi_s     = rsi(df, _RSI_PERIOD)
    sma_cache = _precompute_sma_cache(df)
    return [_run_one_config(df, rsi_s, sma_cache, p) for p in _PARAM_SETS]


def _log_table_row(r: dict) -> None:
    if r['num_trades'] == 0:
        logger.info('%-22s  %5d  %9s  %9s  %8s  %7s  %7s  %8s  %8s',
                    r['label'], 0, '-', '-', '-', '-', '-', '-', '-')
        return
    sp = '+' if r['total_pnl_usdt'] >= 0 else ''
    logger.info(
        '%-22s  %5d  %8.1f%%  %s%8.2f  %s%6.2f%%  %6.2f%%  %7.4f  %+8.2f  %+8.2f',
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
) -> None:
    ranked = sorted(results, key=lambda r: r.get('sharpe_ratio', -999), reverse=True)
    sep = '─' * 92
    hdr = (f"{'Config':<22}  {'#':>5}  {'WinRate':>8}  {'PnL USDT':>9}  "
           f"{'PnL%':>7}  {'MaxDD%':>6}  {'Sharpe':>7}  {'Best':>8}  {'Worst':>8}")
    logger.info(sep)
    logger.info('MULTI-PARAM BACKTEST  %s → %s  |  %d candles', data_from, data_to, candles)
    logger.info('SL=%.0f%%  TP=%.0f%%  Risk/trade=%.1f%%  Balance=%.0f USDT',
                _SL_PCT * 100, _TP_PCT * 100, _RISK_PCT * 100, _BALANCE)
    logger.info(sep)
    logger.info(hdr)
    logger.info('─' * 92)
    for r in ranked:
        _log_table_row(r)
    logger.info(sep)


def _save_multi_report(results: list[dict], period: dict) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ranked = sorted(results, key=lambda r: r.get('sharpe_ratio', -999), reverse=True)
    out = {
        'period':     period,
        'parameters': {'sl_pct': _SL_PCT, 'tp_pct': _TP_PCT,
                       'risk_pct': _RISK_PCT, 'balance': _BALANCE},
        'configs':    ranked,
    }
    _MULTI_FILE.write_text(json.dumps(out, indent=2))
    logger.info('multi_report_saved path=%s', _MULTI_FILE)


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

    logger.info('running %d param combinations on %d candles...', len(_PARAM_SETS), len(df))
    results = _run_multi_analysis(df)

    period = {
        'from':    _ts_to_iso(int(df['ts'].iloc[0])),
        'to':      _ts_to_iso(int(df['ts'].iloc[-1])),
        'candles': len(df),
    }
    _save_multi_report(results, period)
    _log_comparison_table(results, period['from'], period['to'], period['candles'])


if __name__ == '__main__':
    run()
