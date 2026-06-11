"""Champion-config sweep over 24-month BTC walk-forward.

Honesty rules followed (per Session 0 prompt §honestidad estadística):
- All runs include fees + slippage (apply_costs=True).
- Sharpe reported per-trade AND annualized (≈ SR_trade × sqrt(trades_per_year)).
- Wilson 95% CI for win rate.
- Lower bound of WR-CI is compared against fee-adjusted breakeven (sl/tp specific).
- Per-fold dispersion reported alongside aggregate (a high-variance aggregate
  Sharpe over 16 trades is not the same as a stable edge).

NOTE: the AdvancedParams simulator is long-only and lacks the V7 short side,
MTF, volatility, and session filters. This sweep validates the long-only
backbone of V7. Short and MTF will get their own simulators in a later pass.
"""
from __future__ import annotations

import json
import math
import pickle
import time
from dataclasses import asdict, replace
from pathlib import Path

from backtest.advanced import AdvancedParams
from backtest.walk_forward import WalkForwardConfig, run as run_walk_forward

CACHE = Path(__file__).resolve().parent / 'results' / '_cache_btc_1m_24mo.pkl'
OUT = Path(__file__).resolve().parent / 'results' / 'sweep_v7_24mo.json'


def wilson_lower(p: float, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def breakeven_wr(sl_pct: float, tp_pct: float, fee_round_trip: float = 0.002, slip_round_trip: float = 0.001) -> float:
    eff_sl = sl_pct + fee_round_trip + slip_round_trip
    eff_tp = tp_pct - fee_round_trip - slip_round_trip
    if eff_tp <= 0:
        return 1.0
    return eff_sl / (eff_sl + eff_tp)


def annualize_per_trade_sharpe(sr_trade: float, n_trades: int, period_years: float = 2.0) -> float:
    if n_trades <= 1 or period_years <= 0:
        return 0.0
    trades_per_year = n_trades / period_years
    return sr_trade * math.sqrt(trades_per_year)


def main() -> None:
    if not CACHE.exists():
        raise SystemExit(f'cache not found: {CACHE} — run backtest.cli first to generate')
    print(f'loading {CACHE}…')
    with open(CACHE, 'rb') as f:
        df = pickle.load(f)
    print(f'  candles={len(df):,}  range={df["ts"].iloc[0]}→{df["ts"].iloc[-1]}')

    base = AdvancedParams(
        label='base_champion',
        rsi_threshold=40.0,
        sma_period=20,
        sl_pct=0.025,
        tp_pct=0.040,
        use_atr_exits=False,
        use_trailing_stop=False,
        use_trend_filter=True,
        use_adx_filter=True,
        adx_threshold=45.0,
        apply_costs=True,
        risk_pct=0.01,
    )

    variants = [
        base,
        replace(base, label='rsi_45',            rsi_threshold=45.0),
        replace(base, label='rsi_35',            rsi_threshold=35.0),
        replace(base, label='adx_25',            adx_threshold=25.0),
        replace(base, label='adx_35',            adx_threshold=35.0),
        replace(base, label='adx_55',            adx_threshold=55.0),
        replace(base, label='adx_65',            adx_threshold=65.0),
        replace(base, label='adx_off',           use_adx_filter=False),
        replace(base, label='no_trend',          use_trend_filter=False),
        replace(base, label='trailing_on',       use_trailing_stop=True),
        replace(base, label='sl_020_tp_040',     sl_pct=0.020),
        replace(base, label='sl_030_tp_050',     sl_pct=0.030, tp_pct=0.050),
        replace(base, label='sl_015_tp_030',     sl_pct=0.015, tp_pct=0.030),
        replace(base, label='sl_025_tp_050',     tp_pct=0.050),
        replace(base, label='sl_025_tp_060',     tp_pct=0.060),
        replace(base, label='rsi45_adx55',       rsi_threshold=45.0, adx_threshold=55.0),
        replace(base, label='rsi45_adx55_trail', rsi_threshold=45.0, adx_threshold=55.0, use_trailing_stop=True),
        replace(base, label='rsi45_no_trend',    rsi_threshold=45.0, use_trend_filter=False),
        replace(base, label='rsi45_adx_off',     rsi_threshold=45.0, use_adx_filter=False),
        replace(base, label='rsi45_adx55_tp050', rsi_threshold=45.0, adx_threshold=55.0, tp_pct=0.050),
    ]

    wf = WalkForwardConfig(train_months=3, test_months=1, step_months=1)
    PERIOD_YEARS = 2.0
    records: list[dict] = []
    print(f'\nrunning {len(variants)} variants × walk-forward (24mo, 3/1/1)…\n')
    print(f"{'label':<28}  {'#':>4}  {'WR%':>5}  {'WR_lo%':>6}  {'BE_WR%':>6}  "
          f"{'Sharpe':>7}  {'SR_ann':>7}  {'MaxDD%':>7}  {'PnL%':>7}  {'Fees$':>6}  {'PF':>5}  "
          f"{'folds_tr':>8}  {'t_s':>4}")
    print('-' * 130)
    for v in variants:
        t0 = time.time()
        r = run_walk_forward(df, v, wf)
        agg = r['aggregate']
        n = agg['num_trades']
        wr = agg.get('win_rate_pct', 0.0) / 100
        wr_lo = wilson_lower(wr, n) * 100 if n > 0 else 0.0
        sr_trade = agg.get('sharpe_ratio', 0.0)
        sr_ann = annualize_per_trade_sharpe(sr_trade, n, PERIOD_YEARS)
        be = breakeven_wr(v.sl_pct, v.tp_pct) * 100
        pf = agg.get('profit_factor')
        pf_s = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
        folds_with_trades = sum(1 for f in r['folds'] if f['num_trades'] > 0)
        rec = {
            'label':           v.label,
            'params':          asdict(v),
            'num_folds':       r['num_folds'],
            'folds_with_trades': folds_with_trades,
            'num_trades':      n,
            'win_rate_pct':    agg.get('win_rate_pct', 0.0),
            'wr_lower_95':     round(wr_lo, 2),
            'breakeven_wr_pct': round(be, 2),
            'sharpe_trade':    sr_trade,
            'sharpe_annual':   round(sr_ann, 4),
            'max_drawdown_pct': agg.get('max_drawdown_pct', 0.0),
            'net_pnl_pct':     agg.get('net_pnl_pct', 0.0),
            'net_pnl_usdt':    agg.get('net_pnl_usdt', 0.0),
            'fees_paid_usdt':  agg.get('fees_paid_usdt', 0.0),
            'profit_factor':   pf,
            'fold_sharpes':    [f.get('sharpe_ratio', 0.0) for f in r['folds']],
            'fold_pnls':       [f['net_pnl_usdt'] for f in r['folds']],
            'fold_trades':     [f['num_trades'] for f in r['folds']],
            'duration_sec':    round(time.time() - t0, 2),
        }
        records.append(rec)
        print(
            f"{v.label:<28}  {n:>4}  {wr*100:>5.1f}  {wr_lo:>6.1f}  {be:>6.1f}  "
            f"{sr_trade:>+7.4f}  {sr_ann:>+7.4f}  {agg.get('max_drawdown_pct',0):>7.3f}  "
            f"{agg.get('net_pnl_pct',0):>+7.3f}  {agg.get('fees_paid_usdt',0):>6.0f}  {pf_s:>5}  "
            f"{folds_with_trades:>8}  {rec['duration_sec']:>4.1f}"
        )

    print('-' * 130)
    # Sort by annualized Sharpe descending and report top 5
    by_sharpe = sorted(records, key=lambda r: r['sharpe_annual'], reverse=True)
    print('\nTOP 5 by annualized Sharpe (with sample-size guard):')
    for i, r in enumerate(by_sharpe[:5], 1):
        stat = 'OK' if r['wr_lower_95'] > r['breakeven_wr_pct'] else 'WR_lo BELOW breakeven (not significant)'
        print(f"  {i}. {r['label']:<28}  trades={r['num_trades']:3d}  "
              f"SR_ann={r['sharpe_annual']:+.3f}  WR={r['win_rate_pct']:.1f}% (lo={r['wr_lower_95']:.1f}, be={r['breakeven_wr_pct']:.1f})  "
              f"DD={r['max_drawdown_pct']:.2f}%  PnL={r['net_pnl_pct']:+.2f}%  [{stat}]")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'ran_at_ms':    int(time.time() * 1000),
        'period_years': PERIOD_YEARS,
        'walk_forward': {'train_months': wf.train_months, 'test_months': wf.test_months, 'step_months': wf.step_months},
        'data':         {'candles': len(df), 'from_ms': int(df['ts'].iloc[0]), 'to_ms': int(df['ts'].iloc[-1])},
        'configs':      len(variants),
        'results':      records,
    }, indent=2, default=str))
    print(f'\nsaved: {OUT}')


if __name__ == '__main__':
    main()
