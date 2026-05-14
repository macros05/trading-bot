"""Walk-forward out-of-sample validation harness.

Splits a single large dataframe into rolling (train, test) windows and runs
the advanced simulator against the test slice of each fold. Training is
currently fixed-parameter (no hyperparameter search inside the fold); the
harness is shaped so a sweep can be dropped in later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from backtest.advanced import AdvancedParams, compute_summary, simulate


_APPROX_MS_PER_MONTH = 30 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class WalkForwardConfig:
    train_months: int = 3
    test_months: int = 1
    step_months: int = 1


def _month_ms(n: int) -> int:
    return int(n) * _APPROX_MS_PER_MONTH


def _slice_by_ts(df: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    mask = (df['ts'] >= start_ms) & (df['ts'] < end_ms)
    return df.loc[mask].reset_index(drop=True)


def iter_folds(
    df: pd.DataFrame,
    cfg: WalkForwardConfig,
) -> Iterable[tuple[pd.DataFrame, pd.DataFrame]]:
    """Yield (train_df, test_df) tuples advancing by step_months each fold."""
    if df.empty:
        return
    start = int(df['ts'].iloc[0])
    end = int(df['ts'].iloc[-1])
    train_ms = _month_ms(cfg.train_months)
    test_ms = _month_ms(cfg.test_months)
    step_ms = _month_ms(cfg.step_months)
    cursor = start
    while cursor + train_ms + test_ms <= end:
        train_df = _slice_by_ts(df, cursor, cursor + train_ms)
        test_df = _slice_by_ts(df, cursor + train_ms, cursor + train_ms + test_ms)
        if len(train_df) > 0 and len(test_df) > 0:
            yield train_df, test_df
        cursor += step_ms


def run(
    df: pd.DataFrame,
    params: AdvancedParams,
    cfg: WalkForwardConfig = WalkForwardConfig(),
) -> dict:
    """Execute walk-forward over *df* using fixed *params*.

    Returns a dict with per-fold summaries and an aggregate summary over all
    test trades (as if they were a single strategy run).
    """
    folds: list[dict] = []
    all_trades: list[dict] = []
    combined_equity: list[float] = [params.balance]
    running_balance = params.balance

    for idx, (_train, test) in enumerate(iter_folds(df, cfg)):
        if len(test) < 200:
            continue
        fold_params = params
        result = simulate(test, fold_params)
        summary = compute_summary(result)
        summary['fold'] = idx
        summary['test_from_ms'] = int(test['ts'].iloc[0])
        summary['test_to_ms'] = int(test['ts'].iloc[-1])
        summary['test_candles'] = len(test)
        folds.append(summary)
        for t in result['trades']:
            # rebase each trade's pnl onto the rolling compound equity curve
            running_balance += t['pnl_usdt']
            combined_equity.append(running_balance)
            all_trades.append(t)

    aggregate = _aggregate(all_trades, combined_equity, params.balance)
    return {
        'config':    {
            'train_months': cfg.train_months,
            'test_months':  cfg.test_months,
            'step_months':  cfg.step_months,
        },
        'num_folds': len(folds),
        'folds':     folds,
        'aggregate': aggregate,
    }


def _aggregate(trades: list[dict], equity: list[float], initial: float) -> dict:
    import math
    out = {
        'num_trades':         len(trades),
        'initial_balance':    initial,
        'final_balance':      round(equity[-1], 4),
        'net_pnl_usdt':       round(equity[-1] - initial, 4),
        'net_pnl_pct':        round((equity[-1] - initial) / initial * 100, 4),
    }
    if not trades:
        out.update({'win_rate_pct': 0.0, 'sharpe_ratio': 0.0,
                    'max_drawdown_pct': 0.0, 'profit_factor': 0.0})
        return out
    wins = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total_wins = sum(t['pnl_usdt'] for t in wins)
    total_losses = abs(sum(t['pnl_usdt'] for t in losses))
    pf = total_wins / total_losses if total_losses > 0 else float('inf')

    returns = [t['pnl_usdt'] / initial for t in trades]
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0.0
    sharpe = (mean_r / std_r) if std_r > 0 else 0.0

    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    out.update({
        'win_rate_pct':     round(len(wins) / len(trades) * 100, 2),
        'sharpe_ratio':     round(sharpe, 4),
        'max_drawdown_pct': round(max_dd * 100, 4),
        'profit_factor':    round(pf, 4) if pf != float('inf') else None,
        'fees_paid_usdt':   round(sum(t['fees'] for t in trades), 4),
        'slippage_cost_usdt': round(sum(t['slippage'] for t in trades), 4),
    })
    return out
