"""Advanced backtest simulator: fees, slippage, ATR-scaled exits, trailing
stop, higher-TF trend filter, ADX regime filter.

Kept separate from `backtest.engine` so the legacy sweeps continue to run
unchanged while the new harness and CLI drive this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

import pandas as pd

from config import SLIPPAGE, TAKER_FEE
from strategy.indicators import adx, atr, higher_tf_trend_ema, rsi, sma, volume_sma
from strategy.signals import (
    check_exit_price,
    passes_regime_filters,
    should_enter,
    update_trailing_stop,
)

_COST_ROUND_TRIP_FEE_FRAC = 2 * TAKER_FEE


@dataclass(frozen=True)
class AdvancedParams:
    """Parameter bundle for the advanced simulator.

    Defaults mirror the current live config so flipping a single flag is
    the minimal-change way to run a targeted experiment.
    """
    label:             str     = 'advanced'
    # entry
    rsi_period:        int     = 14
    rsi_threshold:     float   = 40.0
    sma_period:        int     = 20
    # exits (one mode wins: use_atr_exits=True beats fixed pct)
    sl_pct:            float   = 0.025
    tp_pct:            float   = 0.040
    use_atr_exits:     bool    = False
    atr_period:        int     = 14
    atr_sl_multiplier: float   = 1.5
    atr_tp_multiplier: float   = 3.0
    # trailing
    use_trailing_stop: bool    = False
    # regime filters
    use_trend_filter:  bool    = False
    htf_tf:            str     = '1h'
    htf_ema_period:    int     = 200
    use_adx_filter:    bool    = False
    adx_period:        int     = 14
    adx_threshold:     float   = 25.0
    # costs
    apply_costs:       bool    = True
    taker_fee:         float   = TAKER_FEE
    slippage:          float   = SLIPPAGE
    # sizing
    balance:           float   = 10_000.0
    risk_pct:          float   = 0.01
    # sizing guardrails (prevent ATR-exit blow-ups from demanding impossible leverage)
    min_sl_pct:        float   = 0.005   # 0.5 % floor on the effective SL distance
    max_leverage:      float   = 1.0     # notional / equity cap (1.0 = spot, no margin)


def _sl_tp_prices(
    entry: float,
    params: AdvancedParams,
    atr_at_entry: float | None,
) -> tuple[float, float]:
    """Compute absolute SL/TP with an SL-distance floor.

    The floor prevents microscopic ATR-derived stops on fine timeframes from
    inflating position size to impossible leverage (see IMPROVEMENT_PLAN §8.2).
    """
    if params.use_atr_exits and atr_at_entry is not None and atr_at_entry > 0:
        sl_dist = params.atr_sl_multiplier * atr_at_entry
        tp_dist = params.atr_tp_multiplier * atr_at_entry
        min_dist = params.min_sl_pct * entry
        if sl_dist < min_dist:
            # Preserve the chosen R:R by scaling both SL and TP distances up.
            scale = min_dist / sl_dist
            sl_dist *= scale
            tp_dist *= scale
        return entry - sl_dist, entry + tp_dist
    sl_pct = max(params.sl_pct, params.min_sl_pct)
    return entry * (1 - sl_pct), entry * (1 + params.tp_pct)


def _notional(
    balance: float,
    risk_pct: float,
    effective_sl_pct: float,
    max_leverage: float,
) -> float:
    """Volatility-targeted notional with a leverage cap."""
    if effective_sl_pct <= 0:
        return 0.0
    raw = balance * risk_pct / effective_sl_pct
    return min(raw, balance * max_leverage)


def simulate(
    df: pd.DataFrame,
    params: AdvancedParams,
) -> dict:
    """Run the advanced simulator over *df* and return a metrics dict.

    *df* must have ts, open, high, low, close, volume columns.
    """
    rsi_s = rsi(df, params.rsi_period)
    sma_s = sma(df, params.sma_period) if params.sma_period else None
    atr_s = atr(df, params.atr_period) if (params.use_atr_exits or params.use_trailing_stop) else None
    adx_s = adx(df, params.adx_period) if params.use_adx_filter else None
    trend_s = (
        higher_tf_trend_ema(df, tf=params.htf_tf, period=params.htf_ema_period)
        if params.use_trend_filter else None
    )

    balance = params.balance
    equity: list[float] = [balance]
    trades: list[dict] = []
    position: dict | None = None

    warmup = max(
        params.rsi_period,
        params.sma_period or 0,
        params.atr_period if atr_s is not None else 0,
        params.adx_period * 2 if adx_s is not None else 0,
    )

    gross_total = 0.0
    fees_total = 0.0
    slippage_total = 0.0

    n = len(df)
    close_col = df['close'].to_numpy()
    ts_col = df['ts'].to_numpy()

    for i in range(warmup, n):
        close = float(close_col[i])
        rsi_v = float(rsi_s.iloc[i])
        if pd.isna(rsi_v):
            continue
        sma_v = float(sma_s.iloc[i]) if sma_s is not None else None
        if sma_v is not None and pd.isna(sma_v):
            continue
        atr_v_raw = atr_s.iloc[i] if atr_s is not None else None
        atr_v = float(atr_v_raw) if atr_v_raw is not None and not pd.isna(atr_v_raw) else None
        adx_v_raw = adx_s.iloc[i] if adx_s is not None else None
        adx_v = float(adx_v_raw) if adx_v_raw is not None and not pd.isna(adx_v_raw) else None
        trend_v_raw = trend_s.iloc[i] if trend_s is not None else None
        trend_v: bool | None = None
        if trend_v_raw is not None and not pd.isna(trend_v_raw):
            trend_v = bool(trend_v_raw)

        if position is None:
            if not should_enter(close, sma_v if sma_v is not None else close - 1,
                                rsi_v, rsi_threshold=params.rsi_threshold):
                continue
            if not passes_regime_filters(
                trend_bullish=trend_v,
                adx_val=adx_v,
                adx_threshold=params.adx_threshold,
                use_trend_filter=params.use_trend_filter,
                use_adx_filter=params.use_adx_filter,
            ):
                continue
            entry_fill = close * (1 + params.slippage) if params.apply_costs else close
            sl_price, tp_price = _sl_tp_prices(entry_fill, params, atr_v)
            effective_sl_pct = (entry_fill - sl_price) / entry_fill
            notional = _notional(
                balance, params.risk_pct, effective_sl_pct, params.max_leverage,
            )
            if notional <= 0:
                continue
            qty = notional / entry_fill
            position = {
                'entry_price': entry_fill,
                'entry_price_raw': close,
                'qty': qty,
                'notional': notional,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'entry_ts': int(ts_col[i]),
                'atr_at_entry': atr_v,
            }
            if params.apply_costs:
                entry_slip = (entry_fill - close) * qty
                slippage_total += entry_slip
            continue

        if params.use_trailing_stop and atr_v is not None and atr_v > 0:
            new_sl = update_trailing_stop(
                sl_price=position['sl_price'],
                entry_price=position['entry_price'],
                tp_price=position['tp_price'],
                close=close,
                atr_val=atr_v,
            )
            if new_sl > position['sl_price']:
                position['sl_price'] = new_sl

        reason = check_exit_price(close, position['sl_price'], position['tp_price'])
        if reason is None:
            continue

        exit_fill = close * (1 - params.slippage) if params.apply_costs else close
        gross_pnl = (exit_fill - position['entry_price']) * position['qty']
        fees = position['notional'] * _COST_ROUND_TRIP_FEE_FRAC if params.apply_costs else 0.0
        exit_slip = (close - exit_fill) * position['qty'] if params.apply_costs else 0.0
        slippage_total += exit_slip
        fees_total += fees

        net_pnl = gross_pnl - fees
        gross_total += gross_pnl
        balance += net_pnl

        trade = {
            'entry_price':   round(position['entry_price'], 6),
            'exit_price':    round(exit_fill, 6),
            'qty':           position['qty'],
            'notional':      round(position['notional'], 4),
            'gross_pnl':     round(gross_pnl, 6),
            'fees':          round(fees, 6),
            'slippage':      round(exit_slip, 6),
            'pnl_usdt':      round(net_pnl, 6),
            'pnl_pct':       round(net_pnl / position['notional'] * 100, 6)
                             if position['notional'] > 0 else 0.0,
            'result':        'WIN' if net_pnl >= 0 else 'LOSS',
            'reason':        reason,
            'entry_ts':      position['entry_ts'],
            'exit_ts':       int(ts_col[i]),
            'atr_at_entry':  position['atr_at_entry'],
        }
        trades.append(trade)
        equity.append(balance)
        position = None

    return {
        'label':          params.label,
        'params':         _params_to_dict(params),
        'num_trades':     len(trades),
        'initial_balance': params.balance,
        'final_balance':  round(balance, 4),
        'gross_pnl':      round(gross_total, 4),
        'fees_paid':      round(fees_total, 4),
        'slippage_cost':  round(slippage_total, 4),
        'net_pnl':        round(balance - params.balance, 4),
        'trades':         trades,
        'equity':         [round(e, 4) for e in equity],
    }


def _params_to_dict(p: AdvancedParams) -> dict:
    return {
        'rsi_period': p.rsi_period,
        'rsi_threshold': p.rsi_threshold,
        'sma_period': p.sma_period,
        'sl_pct': p.sl_pct,
        'tp_pct': p.tp_pct,
        'use_atr_exits': p.use_atr_exits,
        'atr_period': p.atr_period,
        'atr_sl_multiplier': p.atr_sl_multiplier,
        'atr_tp_multiplier': p.atr_tp_multiplier,
        'use_trailing_stop': p.use_trailing_stop,
        'use_trend_filter': p.use_trend_filter,
        'htf_tf': p.htf_tf,
        'htf_ema_period': p.htf_ema_period,
        'use_adx_filter': p.use_adx_filter,
        'adx_period': p.adx_period,
        'adx_threshold': p.adx_threshold,
        'apply_costs': p.apply_costs,
        'taker_fee': p.taker_fee,
        'slippage': p.slippage,
        'balance': p.balance,
        'risk_pct': p.risk_pct,
        'min_sl_pct': p.min_sl_pct,
        'max_leverage': p.max_leverage,
    }


def compute_summary(result: dict) -> dict:
    """Derive standard metrics from a simulate() result."""
    import math

    trades = result['trades']
    equity = result['equity']
    out = {
        'label':          result['label'],
        'num_trades':     len(trades),
        'net_pnl_usdt':   result['net_pnl'],
        'net_pnl_pct':    round(result['net_pnl'] / result['initial_balance'] * 100, 4),
        'gross_pnl_usdt': result['gross_pnl'],
        'fees_paid_usdt': result['fees_paid'],
        'slippage_cost_usdt': result['slippage_cost'],
    }
    if not trades:
        out.update({
            'win_rate_pct': 0.0,
            'max_drawdown_pct': 0.0,
            'sharpe_ratio': 0.0,
            'profit_factor': 0.0,
        })
        return out

    wins = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total_wins = sum(t['pnl_usdt'] for t in wins)
    total_losses = abs(sum(t['pnl_usdt'] for t in losses))
    profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

    returns = [t['pnl_usdt'] / result['initial_balance'] for t in trades]
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0.0
    sharpe = (mean_r / std_r) if std_r > 0 else 0.0

    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    out.update({
        'win_rate_pct':     round(len(wins) / len(trades) * 100, 2),
        'max_drawdown_pct': round(max_dd * 100, 4),
        'sharpe_ratio':     round(sharpe, 4),
        'profit_factor':    round(profit_factor, 4) if profit_factor != float('inf') else None,
        'avg_win_usdt':     round(total_wins / len(wins), 4) if wins else 0.0,
        'avg_loss_usdt':    round(-total_losses / len(losses), 4) if losses else 0.0,
        'best_trade':       round(max(t['pnl_usdt'] for t in trades), 4),
        'worst_trade':      round(min(t['pnl_usdt'] for t in trades), 4),
    })
    return out


def baseline_params(**overrides) -> AdvancedParams:
    """Return the baseline (live) config with optional overrides."""
    return AdvancedParams(label=overrides.pop('label', 'baseline'), **overrides)
