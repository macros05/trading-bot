"""4-week dual-side backtest for the May-2026 low-vol tuning.

Validates the new RSI/SL/TP/time-exit parameters against the last ~28 days
of BTC/USDT 1m public OHLCV from Binance. Long-only, short-only, and
combined runs are emitted so the user can see the regime contribution of
each side.

Output: backtest/results/lowvol_validation.json

Usage:
    python -m backtest.lowvol_validation
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    MAX_HOLD_HOURS,
    RSI_LONG_THRESHOLD,
    RSI_SHORT_THRESHOLD,
    STOP_LOSS_PCT_LONG,
    STOP_LOSS_PCT_SHORT,
    TAKE_PROFIT_PCT_LONG,
    TAKE_PROFIT_PCT_SHORT,
)
from strategy.indicators import rsi, sma
from strategy.signals import should_enter, should_enter_short, should_exit_time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

_OUTPUT = Path(__file__).resolve().parent / 'results' / 'lowvol_validation.json'
_RSI_PERIOD = 14
_SMA_PERIOD = 20
_MIN_BARS = max(_RSI_PERIOD, _SMA_PERIOD)
_LOOKBACK_DAYS = 28
_PAGE = 1000
_BAR_MS = 60_000


def _fetch_4w(symbol: str = 'BTC/USDT', timeframe: str = '1m') -> list[list[Any]]:
    ex = ccxt.binance({'timeout': 15_000, 'options': {'defaultType': 'spot'}})
    end_ms = ex.milliseconds()
    start_ms = end_ms - _LOOKBACK_DAYS * 24 * 60 * _BAR_MS
    rows: list[list[Any]] = []
    since = start_ms
    while since < end_ms:
        page = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=_PAGE)
        if not page:
            break
        rows.extend(page)
        since = page[-1][0] + _BAR_MS
        time.sleep(ex.rateLimit / 1000)
        if len(page) < _PAGE:
            break
    logger.info('fetched candles=%d days=%d', len(rows), _LOOKBACK_DAYS)
    return rows


def _to_df(rows: list[list[Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])


def _check_exit(close: float, position: dict) -> str | None:
    side = position['side']
    entry = position['entry_price']
    if side == 'long':
        if close <= entry * (1 - position['sl_pct']):
            return 'stop_loss'
        if close >= entry * (1 + position['tp_pct']):
            return 'take_profit'
        return None
    if close >= entry * (1 + position['sl_pct']):
        return 'stop_loss'
    if close <= entry * (1 - position['tp_pct']):
        return 'take_profit'
    return None


def _pnl_pct(close: float, position: dict) -> float:
    entry = position['entry_price']
    if position['side'] == 'long':
        return (close - entry) / entry
    return (entry - close) / entry


def _run_one(
    df: pd.DataFrame,
    *,
    enable_long: bool,
    enable_short: bool,
    long_rsi: float,
    short_rsi: float,
    long_sl: float,
    long_tp: float,
    short_sl: float,
    short_tp: float,
    max_hold_hours: float,
) -> dict[str, Any]:
    sma_s = sma(df, period=_SMA_PERIOD)
    rsi_s = rsi(df, period=_RSI_PERIOD)
    position: dict | None = None
    pnls: list[float] = []
    reasons: dict[str, int] = {'stop_loss': 0, 'take_profit': 0, 'time_exit': 0}
    durations_h: list[float] = []
    for i in range(_MIN_BARS, len(df)):
        ts_ms = int(df['ts'].iloc[i])
        close = float(df['close'].iloc[i])
        sma_v = float(sma_s.iloc[i])
        rsi_v = float(rsi_s.iloc[i])
        if pd.isna(sma_v) or pd.isna(rsi_v):
            continue
        if position is not None:
            reason = _check_exit(close, position)
            if reason is None and should_exit_time(
                int(position['entry_ts']), ts_ms, max_hold_hours,
            ):
                reason = 'time_exit'
            if reason is not None:
                pnls.append(_pnl_pct(close, position))
                reasons[reason] += 1
                durations_h.append((ts_ms - position['entry_ts']) / 3_600_000)
                position = None
            continue
        long_sig = enable_long and should_enter(close, sma_v, rsi_v, rsi_threshold=long_rsi)
        short_sig = enable_short and should_enter_short(close, sma_v, rsi_v, rsi_threshold=short_rsi)
        if long_sig and short_sig:
            continue
        if long_sig:
            position = {'side': 'long', 'entry_price': close, 'entry_ts': ts_ms,
                        'sl_pct': long_sl, 'tp_pct': long_tp}
        elif short_sig:
            position = {'side': 'short', 'entry_price': close, 'entry_ts': ts_ms,
                        'sl_pct': short_sl, 'tp_pct': short_tp}

    n = len(pnls)
    pnl_total_pct = sum(pnls) * 100
    wins = sum(1 for p in pnls if p > 0)
    if n >= 2:
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = mean / sd if sd > 0 else 0.0
    else:
        sharpe = 0.0
    avg_h = sum(durations_h) / len(durations_h) if durations_h else 0.0
    return {
        'trades':         n,
        'win_rate':       round((wins / n) if n > 0 else 0.0, 4),
        'pnl_pct_total':  round(pnl_total_pct, 4),
        'sharpe':         round(sharpe, 4),
        'avg_hold_hours': round(avg_h, 2),
        'exits':          reasons,
    }


def main() -> None:
    rows = _fetch_4w()
    df = _to_df(rows)

    new_params = dict(
        long_rsi=RSI_LONG_THRESHOLD,
        short_rsi=RSI_SHORT_THRESHOLD,
        long_sl=STOP_LOSS_PCT_LONG,
        long_tp=TAKE_PROFIT_PCT_LONG,
        short_sl=STOP_LOSS_PCT_SHORT,
        short_tp=TAKE_PROFIT_PCT_SHORT,
        max_hold_hours=MAX_HOLD_HOURS,
    )
    old_params = dict(
        long_rsi=40.0, short_rsi=55.0,
        long_sl=0.025, long_tp=0.040,
        short_sl=0.035, short_tp=0.060,
        max_hold_hours=0.0,  # off
    )

    def run_set(params: dict[str, Any]) -> dict[str, Any]:
        return {
            'long_only':  _run_one(df, enable_long=True, enable_short=False, **params),
            'short_only': _run_one(df, enable_long=False, enable_short=True, **params),
            'combined':   _run_one(df, enable_long=True, enable_short=True, **params),
        }

    new_results = run_set(new_params)
    old_results = run_set(old_params)

    payload = {
        'symbol': 'BTC/USDT',
        'timeframe': '1m',
        'lookback_days': _LOOKBACK_DAYS,
        'candles': len(df),
        'params_new': new_params,
        'params_old': old_params,
        'results_new': new_results,
        'results_old': old_results,
    }
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(payload, indent=2))
    logger.info('wrote_report path=%s', _OUTPUT)
    print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    main()
