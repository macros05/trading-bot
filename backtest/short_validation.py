"""Aggressive-profile gate script for short-positions feature.

Runs three backtest configs over the supplied candles and emits a JSON
report. The deployment gate (per the spec) is:
  Sharpe(combined) >= 0 AND PnL(combined) >= 0

Usage:
    python -m backtest.short_validation
"""
import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.engine import simulate_tick
from strategy.indicators import rsi, sma
from strategy.signals import should_enter

logger = logging.getLogger(__name__)

_OUTPUT = Path('backtest/results/short_validation_aggressive.json')
_RSI_PERIOD = 14
_SMA_PERIOD = 20
_MIN_BARS = max(_RSI_PERIOD, _SMA_PERIOD)


def _should_enter_short(close: float, sma20: float, rsi14: float,
                        rsi_threshold: float) -> bool:
    """Inline mirror of strategy/signals.should_enter_short to keep this
    script standalone for the gate run (which precedes Task 4 implementation)."""
    return rsi14 > rsi_threshold and close < sma20


def _run_one_config(
    candles: list[dict[str, Any]],
    *,
    enable_long: bool,
    enable_short: bool,
    sl_pct: float,
    tp_pct: float,
    rsi_long_threshold: float,
    rsi_short_threshold: float,
    qty_per_trade: float = 1.0,
) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    sma_series = sma(df, period=_SMA_PERIOD)
    rsi_series = rsi(df, period=_RSI_PERIOD)
    position: dict | None = None
    pnls: list[float] = []
    for i in range(_MIN_BARS, len(df)):
        close = float(df['close'].iloc[i])
        sma_v = float(sma_series.iloc[i])
        rsi_v = float(rsi_series.iloc[i])
        if pd.isna(sma_v) or pd.isna(rsi_v):
            continue
        if position is not None:
            res = simulate_tick(close, position)
            if res['exit_reason'] is not None:
                pnls.append(res['pnl_usdt'])
                position = None
            continue
        long_sig = (enable_long and
                    should_enter(close, sma_v, rsi_v, rsi_threshold=rsi_long_threshold))
        short_sig = (enable_short and
                     _should_enter_short(close, sma_v, rsi_v, rsi_short_threshold))
        if long_sig and short_sig:
            continue
        if long_sig:
            position = {'side': 'long', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': sl_pct, 'tp_pct': tp_pct}
        elif short_sig:
            position = {'side': 'short', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': sl_pct, 'tp_pct': tp_pct}
    n = len(pnls)
    pnl_total = sum(pnls)
    if n >= 2:
        mean = pnl_total / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = mean / sd if sd > 0 else 0.0
    else:
        sharpe = 0.0
    wins = sum(1 for p in pnls if p > 0)
    return {
        'trades':   n,
        'win_rate': (wins / n) if n > 0 else 0.0,
        'pnl_usdt': round(pnl_total, 4),
        'sharpe':   round(sharpe, 4),
    }


def run_validation(
    candles: list[dict[str, Any]],
    *,
    sl_pct: float = 0.035,
    tp_pct: float = 0.06,
    rsi_long_threshold: float = 45.0,
    rsi_short_threshold: float = 55.0,
) -> dict[str, dict[str, Any]]:
    long_only = _run_one_config(
        candles, enable_long=True, enable_short=False,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    short_only = _run_one_config(
        candles, enable_long=False, enable_short=True,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    combined = _run_one_config(
        candles, enable_long=True, enable_short=True,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    return {'long_only': long_only, 'short_only': short_only, 'combined': combined}


def _run_one_config_asymmetric(
    candles: list[dict[str, Any]],
    *,
    enable_long: bool,
    enable_short: bool,
    long_sl: float,
    long_tp: float,
    short_sl: float,
    short_tp: float,
    long_rsi: float,
    short_rsi: float,
    qty_per_trade: float = 1.0,
) -> dict[str, Any]:
    """Side-aware variant: each side carries its own SL/TP and RSI threshold."""
    df = pd.DataFrame(candles)
    sma_series = sma(df, period=_SMA_PERIOD)
    rsi_series = rsi(df, period=_RSI_PERIOD)
    position: dict | None = None
    pnls: list[float] = []
    for i in range(_MIN_BARS, len(df)):
        close = float(df['close'].iloc[i])
        sma_v = float(sma_series.iloc[i])
        rsi_v = float(rsi_series.iloc[i])
        if pd.isna(sma_v) or pd.isna(rsi_v):
            continue
        if position is not None:
            res = simulate_tick(close, position)
            if res['exit_reason'] is not None:
                pnls.append(res['pnl_usdt'])
                position = None
            continue
        long_sig = (enable_long and
                    should_enter(close, sma_v, rsi_v, rsi_threshold=long_rsi))
        short_sig = (enable_short and
                     _should_enter_short(close, sma_v, rsi_v, short_rsi))
        if long_sig and short_sig:
            continue
        if long_sig:
            position = {'side': 'long', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': long_sl, 'tp_pct': long_tp}
        elif short_sig:
            position = {'side': 'short', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': short_sl, 'tp_pct': short_tp}
    n = len(pnls)
    pnl_total = sum(pnls)
    if n >= 2:
        mean = pnl_total / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = mean / sd if sd > 0 else 0.0
    else:
        sharpe = 0.0
    wins = sum(1 for p in pnls if p > 0)
    return {
        'trades':   n,
        'win_rate': (wins / n) if n > 0 else 0.0,
        'pnl_usdt': round(pnl_total, 4),
        'sharpe':   round(sharpe, 4),
    }


def run_validation_asymmetric(
    candles: list[dict[str, Any]],
    *,
    long_rsi: float = 40.0,
    long_sl: float = 0.025,
    long_tp: float = 0.040,
    short_rsi: float = 55.0,
    short_sl: float = 0.035,
    short_tp: float = 0.060,
) -> dict[str, dict[str, Any]]:
    long_only = _run_one_config_asymmetric(
        candles, enable_long=True, enable_short=False,
        long_sl=long_sl, long_tp=long_tp, short_sl=short_sl, short_tp=short_tp,
        long_rsi=long_rsi, short_rsi=short_rsi,
    )
    short_only = _run_one_config_asymmetric(
        candles, enable_long=False, enable_short=True,
        long_sl=long_sl, long_tp=long_tp, short_sl=short_sl, short_tp=short_tp,
        long_rsi=long_rsi, short_rsi=short_rsi,
    )
    combined = _run_one_config_asymmetric(
        candles, enable_long=True, enable_short=True,
        long_sl=long_sl, long_tp=long_tp, short_sl=short_sl, short_tp=short_tp,
        long_rsi=long_rsi, short_rsi=short_rsi,
    )
    return {'long_only': long_only, 'short_only': short_only, 'combined': combined}


def _gate_passed(results: dict[str, dict[str, Any]]) -> bool:
    c = results['combined']
    return c['sharpe'] >= 0.0 and c['pnl_usdt'] >= 0.0


async def _main() -> None:
    from exchange.client import BinanceClient
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    client = BinanceClient()
    try:
        candles = await client.fetch_candles('BTC/USDT', '1m', limit=1000)
        results = run_validation(candles)
    finally:
        await client.close()
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(results, indent=2))
    logger.info('gate_results=%s passed=%s', results, _gate_passed(results))


if __name__ == '__main__':
    import asyncio
    asyncio.run(_main())
