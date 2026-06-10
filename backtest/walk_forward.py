"""Walk-forward out-of-sample validation harness (advanced simulator).

Thin wrapper around the generic ``backtest.wfa`` engine. The historical
implementation iterated ``(train, test)`` folds but discarded the train slice
(``for _train, test in ...``), so it never optimized anything; the engine now
owns the fold loop and the train slice drives in-fold candidate selection when
more than one candidate is supplied. The default single-params call keeps the
fixed-params behavior the CLI exposes.

The aggregate is the shared promotion-gate-compatible dict from
``backtest.wfa.aggregate_oos`` plus the legacy CLI keys (``sharpe_ratio``,
balances, fees/slippage totals) so ``backtest/cli.py`` output stays stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import pandas as pd

from backtest.advanced import AdvancedParams, compute_summary, simulate
from backtest.wfa import WfaConfig, run_wfa


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


def _summarize_fold(result: dict, _initial_balance: float) -> dict:
    return compute_summary(result)


def _legacy_aggregate_view(outcome: dict) -> dict:
    """Shared gate-compatible aggregate + the keys the CLI always printed."""
    aggregate = dict(outcome['aggregate'])
    trades = outcome['trades']
    aggregate['sharpe_ratio'] = aggregate.get('sharpe_trade', 0.0)
    aggregate['initial_balance'] = outcome['initial_balance']
    aggregate['final_balance'] = outcome['final_balance']
    aggregate['fees_paid_usdt'] = round(
        sum(t.get('fees', 0.0) for t in trades), 4)
    aggregate['slippage_cost_usdt'] = round(
        sum(t.get('slippage', 0.0) for t in trades), 4)
    return aggregate


def run(
    df: pd.DataFrame,
    params: AdvancedParams,
    cfg: WalkForwardConfig = WalkForwardConfig(),
    candidates: Sequence[AdvancedParams] | None = None,
) -> dict:
    """Execute walk-forward over *df*.

    With the default ``candidates=None`` this is the fixed-params mode (only
    *params* runs, zero train evaluations). Passing ``candidates`` enables
    real in-fold optimization: every candidate is simulated on each train
    slice and only the winner runs the test slice (see ``backtest.wfa``).
    """
    wfa_cfg = WfaConfig(train_months=cfg.train_months,
                        test_months=cfg.test_months,
                        step_months=cfg.step_months)
    outcome = run_wfa(df, list(candidates) if candidates else [params],
                      simulate, wfa_cfg, label=params.label,
                      initial_balance=params.balance,
                      fold_summary_fn=_summarize_fold)
    return {
        'config': {
            'train_months': cfg.train_months,
            'test_months':  cfg.test_months,
            'step_months':  cfg.step_months,
        },
        'num_folds':     outcome['num_folds'],
        'folds':         outcome['folds'],
        'n_evaluations': outcome['n_evaluations'],
        'aggregate':     _legacy_aggregate_view(outcome),
    }
