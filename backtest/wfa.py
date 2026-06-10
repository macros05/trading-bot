"""Generic in-fold-optimizing walk-forward engine (Spec 2).

The pre-Spec-2 harnesses (``backtest.walk_forward.run`` and
``backtest.sweep_v7_full.walk_forward_v7``) discarded the train slice, so
"walk-forward optimization" never optimized — every fold ran fixed params and
the train window was decoration. This engine makes the train slice load-bearing:

  per fold:  simulate EVERY candidate on the TRAIN slice only
             -> select the winner (per-trade Sharpe, min-train-trade floor,
                net-PnL tiebreak)
             -> run ONLY the winner on the TEST slice
             -> chain test trades into one compounding OOS series.

Fallback semantics: when no candidate reaches ``min_train_trades`` on the
train slice, the FIRST candidate (the family default) runs the test slice and
the fold is marked ``selection_applied=False``. No selection happened, so no
selection bias was introduced — the fold is equivalent to the old fixed-params
behavior, which is also what a single-candidate ``candidates`` list gives you
for before/after comparisons.

Honest DSR accounting: the engine never touches ``deflated_sharpe_pvalue``;
it counts every train-slice candidate simulation in ``n_evaluations`` so the
caller can pass an honest ``n_trials`` (external variants + in-fold
evaluations) and keep the two-pass cross-trial ``sigma_SR`` pattern intact.

No lookahead: fold slices end at the fold window's end. ``warmup_bars``
extends a slice BACKWARD only (indicator warmup); trades whose entry falls
before the window start are dropped from metrics and from the balance chain.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, is_dataclass, replace
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from backtest.sweep_v7_full import (
    _APPROX_MS_PER_MONTH,
    annualize,
    breakeven_wr,
    deflated_sharpe_pvalue,
    wilson_lower,
)

logger = logging.getLogger(__name__)

SimulateFn = Callable[[pd.DataFrame, Any], dict]
FoldSummaryFn = Callable[[dict, float], dict]

_MS_PER_YEAR = 365.25 * 86_400 * 1_000


@dataclass(frozen=True)
class WfaConfig:
    """Walk-forward window geometry and in-fold selection policy."""
    train_months: int = 3
    test_months: int = 1
    step_months: int = 1
    min_train_trades: int = 15
    min_test_candles: int = 200
    warmup_bars: int = 0


@dataclass(frozen=True)
class _Window:
    from_ms: int
    to_ms: int


def _window_positions(ts: np.ndarray, window: _Window) -> tuple[int, int]:
    low = int(np.searchsorted(ts, window.from_ms, side='left'))
    high = int(np.searchsorted(ts, window.to_ms, side='left'))
    return low, high


def _slice_window(df: pd.DataFrame, ts: np.ndarray, window: _Window,
                  warmup_bars: int) -> pd.DataFrame:
    low, high = _window_positions(ts, window)
    return df.iloc[max(0, low - warmup_bars):high].reset_index(drop=True)


def _with_balance(params: Any, balance: float) -> Any:
    """Carry the compounding balance into the fold params when supported."""
    if is_dataclass(params) and hasattr(params, 'balance'):
        return replace(params, balance=balance)
    return params


def _trades_inside(result: dict, window: _Window) -> list[dict]:
    """Drop trades opened outside the window (i.e. on warmup bars)."""
    return [t for t in result.get('trades', [])
            if window.from_ms <= t['entry_ts'] < window.to_ms]


def _candidate_label(candidate: Any, index: int) -> str:
    return str(getattr(candidate, 'label', f'candidate_{index}'))


def _train_metrics(trades: list[dict]) -> dict:
    """Per-trade Sharpe + net PnL of one candidate on the train slice."""
    n = len(trades)
    net_pnl = sum(t['pnl_usdt'] for t in trades)
    sharpe = 0.0
    if n >= 2:
        returns = [t['pnl_pct'] / 100 for t in trades]
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = variance ** 0.5 if variance > 0 else 0.0
        sharpe = mean / std if std > 0 else 0.0
    return {'num_trades': n, 'sharpe_trade': round(sharpe, 4),
            'net_pnl_usdt': round(net_pnl, 4)}


def _select_winner(train_df: pd.DataFrame, train_window: _Window,
                   candidates: Sequence[Any], balance: float,
                   simulate_fn: SimulateFn,
                   cfg: WfaConfig) -> tuple[Any, str, dict, bool]:
    """Simulate every candidate on the train slice; pick by Sharpe with a
    trade-count floor (tiebreak: net PnL). Returns
    ``(winner, winner_label, train_metrics, selection_applied)``."""
    evaluated: list[tuple[Any, str, dict]] = []
    for index, candidate in enumerate(candidates):
        result = simulate_fn(train_df, _with_balance(candidate, balance))
        metrics = _train_metrics(_trades_inside(result, train_window))
        evaluated.append((candidate, _candidate_label(candidate, index), metrics))
    eligible = [entry for entry in evaluated
                if entry[2]['num_trades'] >= cfg.min_train_trades]
    if not eligible:
        # No candidate cleared the floor: no selection happened, hence no
        # selection bias — fall back to the family default (first candidate).
        candidate, candidate_label, metrics = evaluated[0]
        return candidate, candidate_label, metrics, False
    winner, winner_label, metrics = max(
        eligible,
        key=lambda entry: (entry[2]['sharpe_trade'], entry[2]['net_pnl_usdt']),
    )
    return winner, winner_label, metrics, True


def _derive_breakevens(params: Any) -> tuple[float, float]:
    """Per-side fee-adjusted breakeven WR (pct) from the params' SL/TP.

    Falls back from V7-style per-side fields to AdvancedParams-style single
    sl_pct/tp_pct; when neither exists returns the conservative 100.0 so the
    promotion gate fails closed on merit, never on shape.
    """
    sl_long = getattr(params, 'sl_pct_long', None)
    tp_long = getattr(params, 'tp_pct_long', None)
    if sl_long is None or tp_long is None:
        sl_long = getattr(params, 'sl_pct', None)
        tp_long = getattr(params, 'tp_pct', None)
    if sl_long is None or tp_long is None:
        return 100.0, 100.0
    sl_short = getattr(params, 'sl_pct_short', sl_long)
    tp_short = getattr(params, 'tp_pct_short', tp_long)
    return (breakeven_wr(sl_long, tp_long) * 100,
            breakeven_wr(sl_short, tp_short) * 100)


def _empty_aggregate(num_folds: int, breakeven_long_pct: float,
                     breakeven_short_pct: float, label: str) -> dict:
    side_zero = {'trades': 0, 'wins': 0, 'win_rate_pct': 0.0,
                 'wr_lower_95': 0.0, 'pnl_usdt': 0.0}
    return {
        'label': label, 'num_trades': 0, 'num_folds': num_folds,
        'folds_with_trades': 0, 'net_pnl_usdt': 0.0, 'net_pnl_pct': 0.0,
        'total_fees': 0.0, 'win_rate_pct': 0.0, 'sharpe_trade': 0.0,
        'sharpe_annual': 0.0, 'max_drawdown_pct': 0.0, 'wr_lower_95': 0.0,
        'profit_factor': 0.0, 'dsr_pvalue': 0.0,
        'breakeven_wr_long': round(breakeven_long_pct, 2),
        'breakeven_wr_short': round(breakeven_short_pct, 2),
        'by_side': {'long': dict(side_zero), 'short': dict(side_zero)},
    }


def _returns_moments(returns: list[float]) -> tuple[float, float, float, float]:
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / max(1, n - 1)
    std = variance ** 0.5 if variance > 0 else 0.0
    if std > 0 and n >= 3:
        m3 = sum((r - mean) ** 3 for r in returns) / n
        m4 = sum((r - mean) ** 4 for r in returns) / n
        return mean, std, m3 / std ** 3, m4 / std ** 4
    return mean, std, 0.0, 3.0


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        dd = (peak - value) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def _by_side_view(trades: list[dict]) -> dict:
    counters = {'long': {'n': 0, 'w': 0, 'pnl': 0.0},
                'short': {'n': 0, 'w': 0, 'pnl': 0.0}}
    for trade in trades:
        side = trade.get('side', 'long')
        if side in counters:
            counters[side]['n'] += 1
            counters[side]['w'] += int(trade['result'] == 'WIN')
            counters[side]['pnl'] += trade['pnl_usdt']
    return {
        side: {'trades': c['n'], 'wins': c['w'],
               'win_rate_pct': round(c['w'] / c['n'] * 100, 2) if c['n'] else 0.0,
               'wr_lower_95': round(wilson_lower(c['w'] / c['n'], c['n']) * 100, 2)
                              if c['n'] else 0.0,
               'pnl_usdt': round(c['pnl'], 4)}
        for side, c in counters.items()
    }


def aggregate_oos(wf: dict, period_years: float, n_trials_for_dsr: int,
                  breakeven_long_pct: float, breakeven_short_pct: float,
                  sr_sample: list[float] | None = None,
                  label: str = 'wfa') -> dict:
    """Promotion-gate-compatible aggregate over a chained OOS series.

    This is the single home of the aggregation math previously inlined in
    ``sweep_v7_full.aggregate`` (which now wraps this function). ``wf`` needs
    ``trades`` / ``equity`` / ``folds`` / ``num_folds`` / ``initial_balance``
    / ``final_balance``. Breakevens arrive precomputed (pct) so the math stays
    independent of any particular params dataclass.
    """
    trades = wf['trades']
    n = len(trades)
    if n == 0:
        out = _empty_aggregate(wf['num_folds'], breakeven_long_pct,
                               breakeven_short_pct, label)
        out['folds_with_trades'] = sum(
            1 for f in wf['folds'] if f['num_trades'] > 0)
        return out
    net_pnl = wf['final_balance'] - wf['initial_balance']
    wins = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total_won = sum(t['pnl_usdt'] for t in wins)
    total_lost = abs(sum(t['pnl_usdt'] for t in losses))
    profit_factor = total_won / total_lost if total_lost > 0 else float('inf')
    mean, std, skew, kurt = _returns_moments([t['pnl_pct'] / 100 for t in trades])
    sharpe = mean / std if std > 0 else 0.0
    win_rate = len(wins) / n * 100
    return {
        'label': label,
        'num_trades': n,
        'num_folds': wf['num_folds'],
        'folds_with_trades': sum(1 for f in wf['folds'] if f['num_trades'] > 0),
        'net_pnl_usdt': round(net_pnl, 4),
        'net_pnl_pct': round(net_pnl / wf['initial_balance'] * 100, 4),
        'total_fees': 0.0,  # placeholder kept for output compatibility
        'win_rate_pct': round(win_rate, 2),
        'wr_lower_95': round(wilson_lower(win_rate / 100, n) * 100, 2),
        'breakeven_wr_long': round(breakeven_long_pct, 2),
        'breakeven_wr_short': round(breakeven_short_pct, 2),
        'sharpe_trade': round(sharpe, 4),
        'sharpe_annual': round(annualize(sharpe, n, period_years), 4),
        'max_drawdown_pct': round(_max_drawdown(wf['equity']) * 100, 4),
        'profit_factor': (round(profit_factor, 4)
                          if profit_factor != float('inf') else None),
        'dsr_pvalue': round(deflated_sharpe_pvalue(
            sharpe, n, n_trials_for_dsr, skew=skew, kurt=kurt,
            sr_sample=sr_sample), 4),
        'returns_skew': round(skew, 4),
        'returns_kurt': round(kurt, 4),
        'by_side': _by_side_view(trades),
    }


def _build_fold_record(fold_index: int, train_window: _Window,
                       test_window: _Window, test_candles: int,
                       winner_label: str, selection_applied: bool,
                       train_metrics: dict | None, kept_trades: list[dict],
                       summary: dict | None) -> dict:
    record = dict(summary) if summary else {}
    record.update({
        'fold': fold_index,
        'train_from_ms': train_window.from_ms,
        'train_to_ms': train_window.to_ms,
        'test_from_ms': test_window.from_ms,
        'test_to_ms': test_window.to_ms,
        'test_candles': test_candles,
        'chosen_label': winner_label,
        'selection_applied': selection_applied,
        'train': train_metrics,
        'num_trades': len(kept_trades),
    })
    return record


def run_wfa(df: pd.DataFrame, candidates: Sequence[Any],
            simulate_fn: SimulateFn, cfg: WfaConfig = WfaConfig(), *,
            label: str | None = None,
            initial_balance: float | None = None,
            period_years: float | None = None,
            n_trials_for_dsr: int | None = None,
            sr_sample: list[float] | None = None,
            breakeven_long_pct: float | None = None,
            breakeven_short_pct: float | None = None,
            fold_summary_fn: FoldSummaryFn | None = None) -> dict:
    """Walk-forward with real in-fold optimization over ``candidates``.

    ``simulate_fn(df_slice, params) -> result`` must return a dict with a
    ``trades`` list whose items carry ``pnl_usdt``, ``pnl_pct``, ``result``,
    ``entry_ts`` (and optionally ``side``) — both ``v7_full.simulate_v7`` and
    the hypothesis simulators satisfy this. A single-element ``candidates``
    list is the fixed-params mode: train slices are skipped entirely (zero
    evaluations), reproducing the legacy behavior for comparisons.

    ``n_evaluations`` in the returned dict counts every train-slice candidate
    simulation performed; callers must add it to their external trial count to
    pass an honest ``n_trials`` to the DSR. When ``n_trials_for_dsr`` is None
    the engine uses ``max(2, n_evaluations + 1)`` (the ``+1`` is the chained
    OOS series itself; the floor of 2 keeps the untouched
    ``deflated_sharpe_pvalue`` defined and is conservative for fixed-params).
    """
    if not candidates:
        raise ValueError('run_wfa requires at least one candidate')
    state = _run_folds(df, list(candidates), simulate_fn, cfg,
                       initial_balance, fold_summary_fn)
    result_label = label or _candidate_label(candidates[0], 0)
    if period_years is None:
        span_ms = (int(df['ts'].iloc[-1]) - int(df['ts'].iloc[0])) if len(df) else 0
        period_years = span_ms / _MS_PER_YEAR
    if n_trials_for_dsr is None:
        n_trials_for_dsr = max(2, state['n_evaluations'] + 1)
    if breakeven_long_pct is None or breakeven_short_pct is None:
        derived_long, derived_short = _derive_breakevens(candidates[0])
        breakeven_long_pct = (breakeven_long_pct if breakeven_long_pct
                              is not None else derived_long)
        breakeven_short_pct = (breakeven_short_pct if breakeven_short_pct
                               is not None else derived_short)
    aggregate = aggregate_oos(state, period_years, n_trials_for_dsr,
                              breakeven_long_pct, breakeven_short_pct,
                              sr_sample=sr_sample, label=result_label)
    return {
        'config': asdict(cfg), 'label': result_label,
        'num_folds': state['num_folds'], 'folds': state['folds'],
        'trades': state['trades'], 'equity': state['equity'],
        'initial_balance': state['initial_balance'],
        'final_balance': state['final_balance'],
        'n_evaluations': state['n_evaluations'],
        'aggregate': aggregate,
    }


def _run_folds(df: pd.DataFrame, candidates: list[Any],
               simulate_fn: SimulateFn, cfg: WfaConfig,
               initial_balance: float | None,
               fold_summary_fn: FoldSummaryFn | None) -> dict:
    balance = (initial_balance if initial_balance is not None
               else float(getattr(candidates[0], 'balance', 10_000.0)))
    initial = balance
    equity = [balance]
    folds: list[dict] = []
    all_trades: list[dict] = []
    n_evaluations = 0
    ts = df['ts'].to_numpy() if len(df) else np.array([], dtype='int64')
    train_ms = cfg.train_months * _APPROX_MS_PER_MONTH
    test_ms = cfg.test_months * _APPROX_MS_PER_MONTH
    step_ms = cfg.step_months * _APPROX_MS_PER_MONTH
    cursor = int(ts[0]) if len(ts) else 0
    end = int(ts[-1]) if len(ts) else 0
    while len(ts) and cursor + train_ms + test_ms <= end:
        train_window = _Window(cursor, cursor + train_ms)
        test_window = _Window(cursor + train_ms, cursor + train_ms + test_ms)
        cursor += step_ms
        test_low, test_high = _window_positions(ts, test_window)
        if test_high - test_low < cfg.min_test_candles:
            continue
        winner, winner_label, train_metrics, selection_applied = (
            candidates[0], _candidate_label(candidates[0], 0), None, False)
        if len(candidates) > 1:
            train_df = _slice_window(df, ts, train_window, cfg.warmup_bars)
            if len(train_df) > 0:
                winner, winner_label, train_metrics, selection_applied = (
                    _select_winner(train_df, train_window, candidates,
                                   balance, simulate_fn, cfg))
                n_evaluations += len(candidates)
        fold_balance = balance
        test_df = _slice_window(df, ts, test_window, cfg.warmup_bars)
        result = simulate_fn(test_df, _with_balance(winner, balance))
        kept = _trades_inside(result, test_window)
        for trade in kept:
            balance += trade['pnl_usdt']
            equity.append(balance)
            all_trades.append(trade)
        summary = None
        if fold_summary_fn is not None:
            filtered = dict(result)
            filtered['trades'] = kept
            summary = fold_summary_fn(filtered, fold_balance)
        folds.append(_build_fold_record(
            len(folds), train_window, test_window, test_high - test_low,
            winner_label, selection_applied, train_metrics, kept, summary))
    logger.info('wfa_complete folds=%d trades=%d n_evaluations=%d',
                len(folds), len(all_trades), n_evaluations)
    return {'folds': folds, 'trades': all_trades, 'equity': equity,
            'num_folds': len(folds), 'initial_balance': initial,
            'final_balance': round(balance, 4), 'n_evaluations': n_evaluations}
