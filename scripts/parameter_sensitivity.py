"""Sweep ±10 % around each strategy parameter and rerun a 4-week backtest.

For each parameter, compares PnL impact vs the v7 baseline on the same data
fetched once. Reports parameters with PnL impact > 20 % as "critical".

Usage:
    python -m scripts.parameter_sensitivity [--weeks 4] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.v7_full import V7Params, fetch_history, metrics_summary, simulate_v7

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)


# (param_name, low_factor, high_factor) — 0.9/1.1 = ±10 %.
SWEEP_PARAMS: tuple[tuple[str, float, float], ...] = (
    ('rsi_long_threshold',    0.9, 1.1),
    ('rsi_short_threshold',   0.9, 1.1),
    ('sl_pct_long',           0.9, 1.1),
    ('tp_pct_long',           0.9, 1.1),
    ('sl_pct_short',          0.9, 1.1),
    ('tp_pct_short',          0.9, 1.1),
    ('adx_threshold',         0.9, 1.1),
    ('range_pct_threshold',   0.9, 1.1),
    ('short_adx_min',         0.9, 1.1),
    ('adx_flat_threshold',    0.9, 1.1),
    ('trailing_breakeven_pct', 0.9, 1.1),
    ('trailing_trail_pct',     0.9, 1.1),
    ('trailing_distance_pct',  0.9, 1.1),
    ('stalled_hours',          0.9, 1.1),
    ('stalled_move_threshold', 0.9, 1.1),
)


def run_with(params: V7Params, df) -> dict:
    res = simulate_v7(df, params)
    return metrics_summary(res)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--weeks', type=int, default=4)
    p.add_argument('--symbol', default='BTC/USDT')
    p.add_argument('--out', default='backtest/results/sensitivity.json')
    args = p.parse_args()

    df = fetch_history(args.symbol, args.weeks)
    base = V7Params(label='baseline')
    base_metrics = run_with(base, df)
    base_pnl = base_metrics['total_pnl']

    results: list[dict] = []
    for name, lo_f, hi_f in SWEEP_PARAMS:
        original = getattr(base, name)
        try:
            lo_value = type(original)(original * lo_f)
            hi_value = type(original)(original * hi_f)
        except (TypeError, ValueError):
            logger.warning('skipping %s — non-numeric', name)
            continue
        lo_params = replace(base, label=f'{name}={lo_value:.4g}', **{name: lo_value})
        hi_params = replace(base, label=f'{name}={hi_value:.4g}', **{name: hi_value})
        lo_m = run_with(lo_params, df)
        hi_m = run_with(hi_params, df)
        delta_lo = lo_m['total_pnl'] - base_pnl
        delta_hi = hi_m['total_pnl'] - base_pnl
        max_abs_delta = max(abs(delta_lo), abs(delta_hi))
        impact_pct = (max_abs_delta / abs(base_pnl) * 100) if abs(base_pnl) > 0.01 else 0.0
        results.append({
            'param':         name,
            'baseline_value': original,
            'low_value':     lo_value,
            'high_value':    hi_value,
            'baseline_pnl':  base_pnl,
            'low_pnl':       lo_m['total_pnl'],
            'high_pnl':      hi_m['total_pnl'],
            'delta_low':     round(delta_lo, 4),
            'delta_high':    round(delta_hi, 4),
            'max_impact_pct': round(impact_pct, 2),
            'critical':       impact_pct > 20.0,
        })
        logger.info('%-26s delta_lo=%+.2f delta_hi=%+.2f impact=%.1f%% %s',
                    name, delta_lo, delta_hi, impact_pct,
                    'CRITICAL' if impact_pct > 20.0 else '')

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        'baseline_metrics': base_metrics,
        'results': sorted(results, key=lambda r: -r['max_impact_pct']),
    }, indent=2, default=str))
    logger.info('saved %s', args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
