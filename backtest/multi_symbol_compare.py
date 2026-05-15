"""Aggregate sweep_v7_full results across multiple symbols.

Reads `backtest/results/sweep_v7_full_24mo_<sym>.json` for each symbol in
the universe and produces a combined per-variant table: total trades,
weighted Sharpe, per-symbol Sharpe dispersion, PnL across the basket.

Usage:
    python -m backtest.multi_symbol_compare --symbols BTC ETH SOL
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_RESULTS = Path(__file__).resolve().parent / 'results'


def _load(sym: str) -> dict:
    p = _RESULTS / f'sweep_v7_full_24mo_{sym.lower()}.json'
    if not p.exists():
        raise SystemExit(f'missing: {p}  (run: python -m backtest.sweep_v7_full --symbol {sym.upper()}/USDT)')
    with open(p) as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', nargs='+', default=['btc', 'eth', 'sol'])
    args = ap.parse_args()
    data = {sym: _load(sym) for sym in args.symbols}

    # Index by label
    by_label: dict[str, dict[str, dict]] = {}
    for sym, payload in data.items():
        for r in payload['results']:
            by_label.setdefault(r['label'], {})[sym] = r

    print(f"\nMulti-symbol comparison ({', '.join(s.upper() for s in args.symbols)}) — 24mo walk-forward")
    sym_hdrs = '  '.join(f"{s.upper():>22}" for s in args.symbols)
    hdr = f"{'label':<32}  " + sym_hdrs + f"  {'Σtrades':>7}  {'SR_avg':>7}  {'PnL_sum%':>8}"
    print(hdr)
    print('-' * len(hdr))
    rows = []
    for label, per_sym in by_label.items():
        if len(per_sym) < len(args.symbols):
            continue
        row_parts = []
        sum_trades = 0
        sum_pnl_pct = 0.0
        sum_sharpe = 0.0
        for sym in args.symbols:
            r = per_sym[sym]
            row_parts.append(f"t={r['num_trades']:3d} WR={r['win_rate_pct']:5.1f} SR={r['sharpe_annual']:+.2f}")
            sum_trades += r['num_trades']
            sum_pnl_pct += r['net_pnl_pct']
            sum_sharpe += r['sharpe_annual']
        avg_sharpe = sum_sharpe / len(args.symbols)
        line = f"{label:<32}  " + '  '.join(f"{p:>22}" for p in row_parts) + f"  {sum_trades:>7d}  {avg_sharpe:>+7.3f}  {sum_pnl_pct:>+8.2f}"
        rows.append((avg_sharpe, sum_trades, line, label))
        print(line)
    print('-' * len(hdr))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    # Top all (small samples are real here)
    print('\nTop 5 by average annualized Sharpe (any trade count — sample is what it is):')
    for i, (avg_sr, sum_trades, _, label) in enumerate(rows[:5], 1):
        print(f'  {i}. {label:<32}  avg_SR={avg_sr:+.3f}  Σtrades={sum_trades:>4d}')
    eligible = [r for r in rows if r[1] >= 10]
    if eligible:
        print('\nTop 3 with ≥10 trades (slightly more statistical heft):')
        for i, (avg_sr, sum_trades, _, label) in enumerate(eligible[:3], 1):
            print(f'  {i}. {label:<32}  avg_SR={avg_sr:+.3f}  Σtrades={sum_trades:>4d}')


if __name__ == '__main__':
    main()
