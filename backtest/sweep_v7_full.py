"""Full V7 sweep — long+short, all live filters, 24-month walk-forward.

Builds on backtest/v7_full.py (the only simulator that includes shorts +
MTF + volatility + session + range filters + pct-based trailing). Replaces
backtest/sweep_v7.py which was long-only via AdvancedParams.

Honesty rules:
- All variants apply fees (0.10%/side) + slippage (5 bps/side).
- Sharpe annualized from per-trade × √(trades/year).
- Wilson 95% lower bound on WR; compared to fee-adjusted breakeven per side.
- DSR (Deflated Sharpe Ratio, López de Prado) computed against the number
  of variants tested — corrects for backtest selection bias.
- Per-side breakdown so we can tell whether shorts add or subtract edge.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from backtest.v7_full import V7Params, metrics_summary, simulate_v7

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'


def _cache_path(symbol: str) -> Path:
    sym_safe = symbol.lower().replace('/', '').replace('usdt', '')
    return _RESULTS_DIR / f'_cache_{sym_safe}_1m_24mo.pkl'

_APPROX_MS_PER_MONTH = 30 * 24 * 60 * 60 * 1000


def wilson_lower(p: float, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def breakeven_wr(sl_pct: float, tp_pct: float,
                 fee_rt: float = 0.002, slip_rt: float = 0.001) -> float:
    eff_sl = sl_pct + fee_rt + slip_rt
    eff_tp = tp_pct - fee_rt - slip_rt
    if eff_tp <= 0:
        return 1.0
    return eff_sl / (eff_sl + eff_tp)


def annualize(sr_trade: float, n_trades: int, period_years: float) -> float:
    if n_trades <= 1 or period_years <= 0:
        return 0.0
    return sr_trade * math.sqrt(n_trades / period_years)


def deflated_sharpe_pvalue(sr_obs: float, n_returns: int, n_trials: int,
                           skew: float = 0.0, kurt: float = 3.0) -> float:
    """López de Prado DSR-style p-value approximation.

    Returns the probability that the *observed* per-trade Sharpe could come
    from a true Sharpe of zero, given we tested *n_trials* configs and the
    sample has *n_returns* observations. Numbers above 0.95 indicate we can
    reject the null with 95% confidence after the multiple-comparison
    correction.
    """
    if n_returns < 4 or n_trials < 1:
        return 0.0
    # SR_0: expected maximum of n_trials independent SRs under the null
    # (López de Prado 2018 eq. 6.10 approximation)
    import statistics
    z_e = statistics.NormalDist().inv_cdf(1 - 1.0 / n_trials)
    z_n = statistics.NormalDist().inv_cdf(1 - 1.0 / (n_trials * math.e))
    EULER = 0.5772156649
    sr0 = math.sqrt(max(1e-9, (1 - EULER) * z_e * z_e + EULER * z_n * z_n))
    # Normalised variance of SR estimator
    denom_var = max(1e-9,
                    1 - skew * sr_obs + (kurt - 1) / 4 * sr_obs * sr_obs)
    z = (sr_obs - sr0) * math.sqrt(max(1, n_returns - 1)) / math.sqrt(denom_var)
    return statistics.NormalDist().cdf(z)


def _slice_by_ts(df: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    mask = (df['ts'] >= start_ms) & (df['ts'] < end_ms)
    return df.loc[mask].reset_index(drop=True)


def walk_forward_v7(df: pd.DataFrame, p: V7Params,
                    train_months: int = 3, test_months: int = 1,
                    step_months: int = 1) -> dict:
    """Walk-forward over *df* using simulate_v7. Train slice currently unused
    (fixed-params); harness shape allows a parameter search to drop in later."""
    train_ms = train_months * _APPROX_MS_PER_MONTH
    test_ms = test_months * _APPROX_MS_PER_MONTH
    step_ms = step_months * _APPROX_MS_PER_MONTH
    start = int(df['ts'].iloc[0])
    end = int(df['ts'].iloc[-1])
    cursor = start
    all_trades: list[dict] = []
    folds: list[dict] = []
    balance = p.balance
    equity = [balance]
    while cursor + train_ms + test_ms <= end:
        test_slice = _slice_by_ts(df, cursor + train_ms, cursor + train_ms + test_ms)
        if len(test_slice) >= 200:
            # Use a per-fold V7Params with the slice's starting balance so
            # the simulator's sizing reflects compounding across folds.
            fold_params = replace(p, balance=balance)
            r = simulate_v7(test_slice, fold_params)
            summary = metrics_summary(r, initial_balance=balance)
            folds.append(summary)
            for t in r['trades']:
                balance += t['pnl_usdt']
                equity.append(balance)
                all_trades.append(t)
        cursor += step_ms
    return {
        'folds': folds, 'trades': all_trades, 'equity': equity,
        'num_folds': len(folds),
        'initial_balance': p.balance, 'final_balance': round(balance, 4),
    }


def aggregate(wf: dict, params: V7Params, period_years: float,
              n_trials_for_dsr: int) -> dict:
    trades = wf['trades']
    n = len(trades)
    initial = wf['initial_balance']
    final = wf['final_balance']
    net_pnl = final - initial
    base = {
        'num_trades': n,
        'num_folds': wf['num_folds'],
        'folds_with_trades': sum(1 for f in wf['folds'] if f['num_trades'] > 0),
        'net_pnl_usdt': round(net_pnl, 4),
        'net_pnl_pct': round(net_pnl / initial * 100, 4),
        'total_fees':  round(sum(t['pnl_usdt'] for t in trades) * 0, 4),  # placeholder
    }
    if n == 0:
        base.update({'win_rate_pct': 0.0, 'sharpe_trade': 0.0,
                     'sharpe_annual': 0.0, 'max_drawdown_pct': 0.0,
                     'wr_lower_95': 0.0, 'profit_factor': 0.0,
                     'dsr_pvalue': 0.0,
                     'breakeven_wr_long':  round(breakeven_wr(params.sl_pct_long, params.tp_pct_long) * 100, 2),
                     'breakeven_wr_short': round(breakeven_wr(params.sl_pct_short, params.tp_pct_short) * 100, 2),
                     'by_side': {'long':  {'trades': 0, 'wins': 0, 'win_rate_pct': 0.0, 'pnl_usdt': 0.0},
                                 'short': {'trades': 0, 'wins': 0, 'win_rate_pct': 0.0, 'pnl_usdt': 0.0}}})
        return base
    wins = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total_w = sum(t['pnl_usdt'] for t in wins)
    total_l = abs(sum(t['pnl_usdt'] for t in losses))
    pf = total_w / total_l if total_l > 0 else float('inf')
    returns = [t['pnl_pct'] / 100 for t in trades]
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / max(1, n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    sr = mean / std if std > 0 else 0.0
    # Skew and kurtosis for DSR
    if std > 0 and n >= 3:
        m3 = sum((r - mean) ** 3 for r in returns) / n
        m4 = sum((r - mean) ** 4 for r in returns) / n
        skew = m3 / std ** 3
        kurt = m4 / std ** 4
    else:
        skew, kurt = 0.0, 3.0
    sr_ann = annualize(sr, n, period_years)
    dsr_p = deflated_sharpe_pvalue(sr, n, n_trials_for_dsr, skew=skew, kurt=kurt)
    # Drawdown over the compounding equity curve
    peak = wf['equity'][0]
    max_dd = 0.0
    for v in wf['equity']:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    wr = len(wins) / n * 100
    # Per-side breakdown
    by_side = {'long': {'n': 0, 'w': 0, 'pnl': 0.0},
               'short': {'n': 0, 'w': 0, 'pnl': 0.0}}
    for t in trades:
        s = t['side']
        if s in by_side:
            by_side[s]['n'] += 1
            by_side[s]['w'] += int(t['result'] == 'WIN')
            by_side[s]['pnl'] += t['pnl_usdt']
    be_long = breakeven_wr(params.sl_pct_long, params.tp_pct_long) * 100
    be_short = breakeven_wr(params.sl_pct_short, params.tp_pct_short) * 100
    base.update({
        'win_rate_pct':     round(wr, 2),
        'wr_lower_95':      round(wilson_lower(wr / 100, n) * 100, 2),
        'breakeven_wr_long':  round(be_long, 2),
        'breakeven_wr_short': round(be_short, 2),
        'sharpe_trade':     round(sr, 4),
        'sharpe_annual':    round(sr_ann, 4),
        'max_drawdown_pct': round(max_dd * 100, 4),
        'profit_factor':    round(pf, 4) if pf != float('inf') else None,
        'dsr_pvalue':       round(dsr_p, 4),
        'returns_skew':     round(skew, 4),
        'returns_kurt':     round(kurt, 4),
        'by_side': {
            s: {'trades': v['n'], 'wins': v['w'],
                'win_rate_pct': round(v['w'] / v['n'] * 100, 2) if v['n'] else 0.0,
                'pnl_usdt': round(v['pnl'], 4)}
            for s, v in by_side.items()
        },
    })
    return base


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', default='BTC/USDT')
    p.add_argument('--out', default=None)
    args = p.parse_args()
    cache = _cache_path(args.symbol)
    if not cache.exists():
        raise SystemExit(f'cache not found: {cache} — run: python -m backtest.fetch_24mo --symbol {args.symbol}')
    print(f'loading {cache}…')
    with open(cache, 'rb') as f:
        df = pickle.load(f)
    period_years = (int(df['ts'].iloc[-1]) - int(df['ts'].iloc[0])) / (365.25 * 86400 * 1000)
    print(f'  symbol={args.symbol}  candles={len(df):,}  period_years={period_years:.2f}')
    sym_safe = args.symbol.lower().replace('/', '').replace('usdt', '')
    out_path = Path(args.out) if args.out else _RESULTS_DIR / f'sweep_v7_full_24mo_{sym_safe}.json'

    # Build the live-equivalent V7 baseline + filter-ablation variants.
    # First sweep showed MTF is the dominant blocker (1 trade → 7 with MTF off).
    # Focus: which subset of filters maximizes trade count without destroying WR.
    from backtest.v7_full import baseline_v6_params

    base = V7Params(label='live_v7_post_session0')   # RSI=40 (already in BOT_CONFIG)

    variants = [
        base,
        # filter ablation — single
        replace(base, label='mtf_off',                use_mtf_filter=False),
        replace(base, label='vol_off',                use_volatility_filter=False),
        replace(base, label='session_off',            use_session_filter=False, blocked_sessions=()),
        replace(base, label='adx_off',                use_adx_filter=False),
        # filter ablation — pairs/triples
        replace(base, label='mtf_off+vol_off',        use_mtf_filter=False, use_volatility_filter=False),
        replace(base, label='mtf_off+vol_off+sess_off', use_mtf_filter=False, use_volatility_filter=False,
                use_session_filter=False, blocked_sessions=()),
        replace(base, label='all_filters_off',        use_mtf_filter=False, use_volatility_filter=False,
                use_session_filter=False, blocked_sessions=(), use_adx_filter=False,
                range_lookback_min=0, stalled_hours=0.0, use_short_trend_filter=False),
        # V6 baseline (no V7 filters, both directions, V6 thresholds)
        baseline_v6_params(),
        # mtf_off + parameter tuning
        replace(base, label='mtf_off+rsi_short_50',   use_mtf_filter=False, rsi_short_threshold=50.0),
        replace(base, label='mtf_off+adx_55',         use_mtf_filter=False, adx_threshold=55.0),
        replace(base, label='mtf_off+short_trend_off', use_mtf_filter=False, use_short_trend_filter=False),
        replace(base, label='mtf_off+wider_tp_long_050', use_mtf_filter=False, tp_pct_long=0.050),
        replace(base, label='mtf_off+tighter_sl_long_020', use_mtf_filter=False, sl_pct_long=0.020),
        replace(base, label='mtf_off+trailing_off',   use_mtf_filter=False, use_trailing_stop=False),
    ]

    results = []
    print(f'\nrunning {len(variants)} V7-full variants × walk-forward (24mo, 3/1/1)…\n')
    hdr = (f"{'label':<32}  {'#':>4}  {'WR%':>5}  {'WR_lo%':>6}  "
           f"{'Long':>9}  {'Short':>9}  "
           f"{'SR_tr':>7}  {'SR_ann':>7}  {'DSR_p':>5}  "
           f"{'DD%':>6}  {'PnL%':>7}  {'PF':>5}  {'t_s':>5}")
    print(hdr)
    print('-' * len(hdr))
    for v in variants:
        t0 = time.time()
        wf = walk_forward_v7(df, v)
        agg = aggregate(wf, v, period_years, n_trials_for_dsr=len(variants))
        long_view  = agg['by_side']['long']
        short_view = agg['by_side']['short']
        long_summary = f"{long_view['trades']}/{long_view['win_rate_pct']:.0f}%"
        short_summary = f"{short_view['trades']}/{short_view['win_rate_pct']:.0f}%"
        pf_s = f"{agg['profit_factor']:.2f}" if agg.get('profit_factor') is not None else '∞'
        rec = {'label': v.label, 'params': asdict(v), **agg,
               'duration_sec': round(time.time() - t0, 2)}
        results.append(rec)
        print(
            f"{v.label:<32}  {agg['num_trades']:>4}  {agg['win_rate_pct']:>5.1f}  {agg['wr_lower_95']:>6.1f}  "
            f"{long_summary:>9}  {short_summary:>9}  "
            f"{agg.get('sharpe_trade',0):>+7.4f}  {agg.get('sharpe_annual',0):>+7.4f}  {agg.get('dsr_pvalue',0):>5.2f}  "
            f"{agg.get('max_drawdown_pct',0):>6.2f}  {agg.get('net_pnl_pct',0):>+7.2f}  {pf_s:>5}  {rec['duration_sec']:>5.1f}"
        )
    print('-' * len(hdr))

    # Top 5 by annualized Sharpe with DSR ≥ 0.95
    eligible = [r for r in results if r['dsr_pvalue'] >= 0.95 and r['num_trades'] > 0]
    by_sharpe = sorted(results, key=lambda r: r['sharpe_annual'], reverse=True)
    print('\nTOP 5 by annualized Sharpe (regardless of DSR — honest sample):')
    for i, r in enumerate(by_sharpe[:5], 1):
        flag = '✓DSR' if r['dsr_pvalue'] >= 0.95 else '✗DSR'
        print(f"  {i}. {r['label']:<32}  trades={r['num_trades']:3d}  "
              f"SR_ann={r['sharpe_annual']:+.3f}  WR={r['win_rate_pct']:.1f}% (lo={r['wr_lower_95']:.1f}%)  "
              f"DD={r['max_drawdown_pct']:.2f}%  PnL={r['net_pnl_pct']:+.2f}%  DSR_p={r['dsr_pvalue']:.2f} {flag}")

    if eligible:
        print(f'\n{len(eligible)} variants pass DSR ≥ 0.95 (López de Prado backtest-selection bias gate)')
    else:
        print('\nNone pass DSR ≥ 0.95. Edge not significant after correcting for backtest-selection bias.')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        'ran_at_ms': int(time.time() * 1000),
        'symbol': args.symbol,
        'period_years': round(period_years, 4),
        'data': {'candles': len(df), 'from_ms': int(df['ts'].iloc[0]),
                 'to_ms': int(df['ts'].iloc[-1])},
        'walk_forward': {'train_months': 3, 'test_months': 1, 'step_months': 1},
        'n_variants': len(variants),
        'results': results,
    }, indent=2, default=str))
    print(f'\nsaved: {out_path}')


if __name__ == '__main__':
    main()
