"""ADX-threshold sweep on the 24-month BTC walk-forward (IMPROVEMENT_PLAN §30).

Fetches the data once and reuses it across threshold variants so the run
stays under ~10 min total instead of 4×~7 min.

    python -m backtest.adx_sweep
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.advanced import AdvancedParams
from backtest.walk_forward import WalkForwardConfig, run as run_walk_forward

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'
_CACHE_PATH = _RESULTS_DIR / '_cache_btc_1m_24mo.pkl'


def _fetch_once(symbol: str, months: int) -> pd.DataFrame:
    """Fetch + cache candles so repeated runs don't re-download."""
    if _CACHE_PATH.exists():
        logger.info('cache_hit path=%s', _CACHE_PATH)
        df = pd.read_pickle(_CACHE_PATH)
        expected_ms = months * 30 * 24 * 60 * 60 * 1000
        actual_ms = int(df['ts'].iloc[-1]) - int(df['ts'].iloc[0])
        if actual_ms >= expected_ms * 0.95:
            logger.info('cache_usable candles=%d span_days=%.1f', len(df),
                        actual_ms / 86_400_000)
            return df
        logger.info('cache_too_short span_days=%.1f refetching',
                    actual_ms / 86_400_000)

    ex = ccxt.binance({'timeout': 15_000, 'options': {'defaultType': 'spot'}})
    now = int(time.time() * 1000)
    since = now - months * 30 * 24 * 60 * 60 * 1000
    rows: list[list] = []
    cur = since
    pages = 0
    while cur < now:
        page = ex.fetch_ohlcv(symbol, '1m', since=cur, limit=1000)
        if not page:
            break
        rows.extend(page)
        pages += 1
        cur = page[-1][0] + 60_000
        if pages % 40 == 0:
            logger.info('fetch pages=%d candles=%d', pages, len(rows))
        if len(page) < 1000:
            break
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = df[c].astype(float)
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_pickle(_CACHE_PATH)
    logger.info('fetch_complete candles=%d cached=%s', len(df), _CACHE_PATH)
    return df


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95 % CI for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _row(variant_label: str, agg: dict) -> str:
    wr = agg.get('win_rate_pct', 0.0)
    lo, hi = _wilson_ci(wr / 100, agg['num_trades'])
    pf = agg.get('profit_factor')
    pf_s = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
    return (
        f"{variant_label:<16}  {agg['num_trades']:>4}  {wr:>5.1f}  "
        f"{lo * 100:>5.1f}–{hi * 100:>5.1f}  "
        f"{agg['net_pnl_usdt']:>+9.2f}  "
        f"{agg['sharpe_ratio']:>7.4f}  "
        f"{agg['max_drawdown_pct']:>7.3f}  {pf_s:>6}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='BTC/USDT')
    parser.add_argument('--months', type=int, default=24)
    parser.add_argument('--train-months', type=int, default=3)
    parser.add_argument('--test-months', type=int, default=1)
    parser.add_argument('--thresholds', nargs='+', type=float,
                        default=[45.0, 50.0, 55.0, 100.0])
    parser.add_argument('--out', default=str(_RESULTS_DIR / 'adx_sweep_v5.json'))
    args = parser.parse_args()

    df = _fetch_once(args.symbol, args.months)
    wf_cfg = WalkForwardConfig(
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=1,
    )

    variants: list[tuple[str, AdvancedParams]] = []
    base = AdvancedParams(
        label='champion',
        apply_costs=True,
        use_trend_filter=True,
        use_adx_filter=True,
    )
    for t in args.thresholds:
        label = f'ADX<{int(t)}' if t < 100 else 'ADX off'
        use_adx = t < 100
        variants.append((
            label,
            replace(base, label=label, use_adx_filter=use_adx, adx_threshold=t),
        ))
    # also include the regime-filter-off sanity check (neither ADX nor trend)
    variants.append((
        'no filters',
        replace(base, label='no filters', use_adx_filter=False, use_trend_filter=False),
    ))

    sep = '─' * 94
    logger.info(sep)
    logger.info('ADX SWEEP — %s %d mo walk-forward %d/%d', args.symbol,
                args.months, args.train_months, args.test_months)
    logger.info(sep)
    logger.info(
        f"{'Variant':<16}  {'#':>4}  {'WR%':>5}  {'95% CI':>11}  "
        f"{'NetPnL$':>9}  {'Sharpe':>7}  {'MaxDD%':>7}  {'PF':>6}"
    )
    logger.info(sep)

    out: dict = {'symbol': args.symbol, 'months': args.months, 'variants': {}}
    for label, params in variants:
        res = run_walk_forward(df, params, wf_cfg)
        agg = res['aggregate']
        logger.info(_row(label, agg))
        out['variants'][label] = {
            'params': {
                'adx_threshold': params.adx_threshold,
                'use_adx_filter': params.use_adx_filter,
                'use_trend_filter': params.use_trend_filter,
            },
            'num_folds': res['num_folds'],
            'aggregate': agg,
        }

    logger.info(sep)

    # Summarize fold-level coverage for each variant (dead-zone check)
    for label, data in out['variants'].items():
        folds = data['num_folds']
        trades = data['aggregate']['num_trades']
        logger.info(
            'coverage %-16s folds=%d tpm=%.2f (avg trades per test month)',
            label, folds, trades / folds if folds else 0,
        )

    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    logger.info('report_saved path=%s', args.out)


if __name__ == '__main__':
    main()
