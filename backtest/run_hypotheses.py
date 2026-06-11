"""Spec 2 hypothesis pipeline: WFA every family x symbol through the gate.

For each symbol (BTC/ETH/SOL 1m, 24 months) and each hypothesis family
(backtest/hypotheses.py), runs the in-fold-optimizing walk-forward engine
(train 3mo / test 1mo / step 1mo) plus the current live champion params as a
fixed-params baseline, then evaluates every chained OOS series against the
anti-overfit promotion gate with default thresholds.

DSR honesty (the point of the project): pass 1 runs everything with a
provisional DSR; pass 2 recomputes every DSR with
``n_trials`` = the number of DISTINCT (family, candidate, symbol) combos
evaluated anywhere in the search (sum of grid sizes across all runs, counted
programmatically) and ``sr_sample`` = the cross-run list of final OOS
per-trade Sharpes, so the cross-trial sigma_SR never defaults silently.

Integration notes (why, not what):
* Per-family ``warmup_bars``: the donchian regime filter (atr_percentile,
  90d of 1h bars) and the hourly EMA200 trend gate fail closed until their
  windows fill — on a 1-month test slice they would NEVER activate without
  extending the slice backward for indicator warmup only.
* Empirical breakeven: the families mix fixed / ATR / trail / triple-barrier
  exits chosen per fold, so a static sl/tp-derived breakeven is meaningless.
  The realized breakeven WR from OOS net pnl (fees already inside pnl_pct)
  is the actual bar the win rate must clear.
* Per-fold trade lists and equity curves are stripped before writing JSON
  (disk pressure); folds keep idx / chosen label / n trades / pnl only.
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import pickle
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from backtest.hypotheses import HypothesisFamily, all_families
from backtest.promotion_gate import GateThresholds, evaluate_config
from backtest.sweep_v7_full import _cache_path, deflated_sharpe_pvalue
from backtest.tf_sim import TfSimParams, resample_1m, simulate_tf
from backtest.v7_full import V7Params, simulate_v7
from backtest.wfa import WfaConfig, run_wfa

logger = logging.getLogger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'
SYMBOLS = ('BTC/USDT', 'ETH/USDT', 'SOL/USDT')
BASELINE_LABEL = 'baseline_live_v7'
_DAYS_PER_MONTH = 30.0
_DAYS_PER_YEAR = 365.25

# Indicator warmup, in 1m bars, per family (slices extend BACKWARD only):
# rsi_mr_*  -> hourly EMA200 trend gate: ~201h of 1h closes  (~8.4 days)
# donchian  -> atr_percentile regime: 90d window + ATR warmup (~91 days)
# bb_rsi    -> BB(20) + RSI(14) on 15min bars                 (~9 hours)
_FAMILY_WARMUP_1M_BARS = {
    'rsi_mr_5min': 13_000,
    'rsi_mr_15min': 13_000,
    'donchian_breakout_1h': 131_000,
    'bb_rsi_15min': 1_000,
}


def _symbol_slug(symbol: str) -> str:
    return symbol.lower().replace('/', '').replace('usdt', '')


def _load_cache(symbol: str) -> pd.DataFrame:
    cache = _cache_path(symbol)
    if not cache.exists():
        raise SystemExit(f'cache not found: {cache} — run backtest.fetch_24mo')
    with open(cache, 'rb') as handle:
        df = pickle.load(handle)
    logger.info('loaded %s: %d candles %s -> %s', cache.name, len(df),
                pd.Timestamp(int(df['ts'].iloc[0]), unit='ms', tz='UTC'),
                pd.Timestamp(int(df['ts'].iloc[-1]), unit='ms', tz='UTC'))
    return df


def _fold_pnl_summary(result: dict, fold_balance: float) -> dict:
    """Tiny per-fold record: PnL only — never the trade list (disk pressure)."""
    trades = result.get('trades', [])
    return {'fold_pnl_usdt': round(sum(t['pnl_usdt'] for t in trades), 2),
            'fold_start_balance': round(fold_balance, 2)}


def _empirical_breakeven_pct(trades: list[dict]) -> float | None:
    """Realized breakeven WR (pct) from net per-trade returns (fees inside)."""
    wins = [t['pnl_pct'] for t in trades if t['result'] == 'WIN']
    losses = [-t['pnl_pct'] for t in trades if t['result'] == 'LOSS']
    if not wins or not losses:
        return None
    average_win = sum(wins) / len(wins)
    average_loss = sum(losses) / len(losses)
    if average_win + average_loss <= 0:
        return None
    return average_loss / (average_win + average_loss) * 100


def _apply_empirical_breakeven(outcome: dict) -> None:
    """Overwrite derived breakevens with the realized one (long-only families).

    breakeven_wr_short mirrors the long value: the short bucket has zero
    trades by design, and a degenerate 100.0 there would poison the gate's
    blended fallback for low-trade runs.
    """
    breakeven = _empirical_breakeven_pct(outcome['trades'])
    if breakeven is None:
        return
    aggregate = outcome['aggregate']
    aggregate['breakeven_wr_long'] = round(breakeven, 2)
    aggregate['breakeven_wr_short'] = round(breakeven, 2)


def _strip_bulky_fields(outcome: dict) -> None:
    outcome.pop('trades', None)
    outcome.pop('equity', None)


def _caching_simulate_fn(
        family: HypothesisFamily) -> Callable[[pd.DataFrame, TfSimParams], dict]:
    """Memoized variant of ``family.build_simulate_fn()``.

    The WFA engine calls simulate_fn once per candidate on the SAME train
    slice; the 1m->tf resample is candidate-independent, so caching it by
    slice bounds removes the dominant per-fold cost (~len(candidates)x).
    Single-entry cache: slices arrive train -> test -> next fold's train.
    """
    cache: dict[tuple[int, int, int], pd.DataFrame] = {}

    def simulate_fn(df_1m: pd.DataFrame, params: TfSimParams) -> dict:
        key = (int(df_1m['ts'].iloc[0]), int(df_1m['ts'].iloc[-1]), len(df_1m))
        df_tf = cache.get(key)
        if df_tf is None:
            cache.clear()
            df_tf = resample_1m(df_1m, family.timeframe)
            cache[key] = df_tf
        return simulate_tf(df_tf, params, df_1m=df_1m)

    return simulate_fn


def _run_family(df: pd.DataFrame, family: HypothesisFamily,
                symbol: str) -> dict:
    label = f'{family.name}__{_symbol_slug(symbol)}'
    cfg = WfaConfig(warmup_bars=_FAMILY_WARMUP_1M_BARS[family.name])
    started = time.time()
    outcome = run_wfa(df, family.candidates, _caching_simulate_fn(family), cfg,
                      label=label, n_trials_for_dsr=2,
                      fold_summary_fn=_fold_pnl_summary)
    _apply_empirical_breakeven(outcome)
    _strip_bulky_fields(outcome)
    return {'family': family.name, 'symbol': symbol,
            'n_candidates': len(family.candidates),
            'duration_sec': round(time.time() - started, 1), **outcome}


def _run_baseline(df: pd.DataFrame, symbol: str) -> dict:
    """Current champion params, fixed (no in-fold selection), same WFA geometry.

    warmup_bars stays 0 to match the legacy sweep_v7_full numbers exactly.
    """
    params = V7Params(label=f'{BASELINE_LABEL}__{_symbol_slug(symbol)}')
    started = time.time()
    outcome = run_wfa(df, [params], simulate_v7, WfaConfig(),
                      label=params.label, n_trials_for_dsr=2,
                      fold_summary_fn=_fold_pnl_summary)
    _strip_bulky_fields(outcome)
    return {'family': BASELINE_LABEL, 'symbol': symbol, 'n_candidates': 1,
            'duration_sec': round(time.time() - started, 1), **outcome}


def _run_symbol(symbol: str) -> list[dict]:
    df = _load_cache(symbol)
    runs: list[dict] = []
    for family in all_families():
        logger.info('WFA %s x %s (%d candidates)…', family.name, symbol,
                    len(family.candidates))
        run = _run_family(df, family, symbol)
        logger.info('  done in %.1fs: folds=%d trades=%d n_evaluations=%d',
                    run['duration_sec'], run['num_folds'],
                    run['aggregate']['num_trades'], run['n_evaluations'])
        runs.append(run)
    logger.info('WFA %s x %s (fixed params)…', BASELINE_LABEL, symbol)
    runs.append(_run_baseline(df, symbol))
    del df
    gc.collect()
    return runs


def _recompute_dsr(runs: list[dict]) -> tuple[int, list[float]]:
    """Pass 2: honest DSR for every run against the whole search's trials."""
    n_trials = sum(run['n_candidates'] for run in runs)
    sr_sample = [run['aggregate']['sharpe_trade'] for run in runs
                 if run['aggregate']['num_trades'] > 0]
    for run in runs:
        aggregate = run['aggregate']
        if aggregate['num_trades'] == 0:
            continue
        aggregate['dsr_pvalue'] = round(deflated_sharpe_pvalue(
            aggregate['sharpe_trade'], aggregate['num_trades'], n_trials,
            skew=aggregate.get('returns_skew', 0.0),
            kurt=aggregate.get('returns_kurt', 3.0),
            sr_sample=sr_sample), 4)
    return n_trials, sr_sample


def _trades_per_year(aggregate: dict) -> float:
    oos_years = aggregate['num_folds'] * _DAYS_PER_MONTH / _DAYS_PER_YEAR
    return aggregate['num_trades'] / oos_years if oos_years > 0 else 0.0


def _gate_record(run: dict) -> dict:
    verdict = evaluate_config(run['aggregate'], GateThresholds())
    return {
        'family': run['family'], 'symbol': run['symbol'],
        'label': run['label'], 'n_candidates': run['n_candidates'],
        'n_evaluations': run['n_evaluations'],
        'duration_sec': run['duration_sec'],
        'trades_per_year': round(_trades_per_year(run['aggregate']), 1),
        'aggregate': run['aggregate'],
        'gate': {'passed': verdict.passed, 'reasons': verdict.reasons},
    }


def _write_symbol_json(symbol: str, runs: list[dict]) -> Path:
    path = _RESULTS_DIR / f'hypotheses_v1_{_symbol_slug(symbol)}.json'
    payload = {'ran_at_ms': int(time.time() * 1000), 'symbol': symbol,
               'runs': runs}
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info('saved %s (%.1f KB)', path, path.stat().st_size / 1024)
    return path


def _write_summary_json(records: list[dict], n_trials: int,
                        sr_sample: list[float], runtime_sec: float) -> Path:
    path = _RESULTS_DIR / 'hypotheses_v1_summary.json'
    payload = {
        'ran_at_ms': int(time.time() * 1000),
        'walk_forward': {'train_months': 3, 'test_months': 1, 'step_months': 1},
        'n_trials_for_dsr': n_trials,
        'sr_sample': [round(value, 4) for value in sr_sample],
        'runtime_sec': round(runtime_sec, 1),
        'gate_thresholds': GateThresholds().__dict__,
        'results': records,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info('saved %s (%.1f KB)', path, path.stat().st_size / 1024)
    return path


def _log_table(records: list[dict]) -> None:
    header = (f"{'family':<22} {'sym':<4} {'#tr':>4} {'tr/yr':>5} {'WR%':>5} "
              f"{'WRlb':>5} {'BE%':>5} {'DSRp':>5} {'SRann':>7} {'DD%':>6} "
              f"{'PnL%':>7} {'gate':>4}")
    logger.info(header)
    logger.info('-' * len(header))
    for record in records:
        aggregate = record['aggregate']
        verdict = 'PASS' if record['gate']['passed'] else 'FAIL'
        logger.info(
            f"{record['family']:<22} {_symbol_slug(record['symbol']):<4} "
            f"{aggregate['num_trades']:>4} {record['trades_per_year']:>5.0f} "
            f"{aggregate['win_rate_pct']:>5.1f} {aggregate['wr_lower_95']:>5.1f} "
            f"{aggregate['breakeven_wr_long']:>5.1f} {aggregate['dsr_pvalue']:>5.2f} "
            f"{aggregate['sharpe_annual']:>+7.3f} {aggregate['max_drawdown_pct']:>6.2f} "
            f"{aggregate['net_pnl_pct']:>+7.2f} {verdict:>4}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', nargs='*', default=list(SYMBOLS))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    started = time.time()
    runs_by_symbol: dict[str, list[dict]] = {}
    for symbol in args.symbols:
        runs_by_symbol[symbol] = _run_symbol(symbol)
    all_runs = [run for runs in runs_by_symbol.values() for run in runs]
    n_trials, sr_sample = _recompute_dsr(all_runs)
    logger.info('pass 2: n_trials=%d sr_sample=%d runs', n_trials,
                len(sr_sample))
    records = [_gate_record(run) for run in all_runs]
    for symbol, runs in runs_by_symbol.items():
        _write_symbol_json(symbol, runs)
    _write_summary_json(records, n_trials, sr_sample, time.time() - started)
    _log_table(records)
    passed = [r for r in records if r['gate']['passed']]
    logger.info('%d/%d (family x symbol) runs pass the promotion gate',
                len(passed), len(records))


if __name__ == '__main__':
    main()
