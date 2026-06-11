"""Multi-symbol validation: compare baseline vs. champion on BTC / ETH / SOL.

Run:
    python -m backtest.multi_symbol --months 6
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.advanced import AdvancedParams, compute_summary, simulate

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'
_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']


def _fetch(symbol: str, months: int) -> pd.DataFrame:
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
        if pages % 30 == 0:
            logger.info('fetching %s pages=%d candles=%d', symbol, pages, len(rows))
        if len(page) < 1000:
            break
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = df[c].astype(float)
    logger.info('fetch_complete %s candles=%d', symbol, len(df))
    return df


_HDR = (
    f"{'Symbol':<10}  {'Config':<22}  {'#':>3}  {'WR%':>5}  "
    f"{'NetPnL$':>9}  {'NetPnL%':>8}  {'Sharpe':>7}  {'MaxDD%':>7}  {'PF':>6}"
)


def _row(sym: str, label: str, s: dict) -> str:
    pf = s.get('profit_factor')
    pf_s = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
    return (
        f"{sym:<10}  {label:<22}  {s['num_trades']:>3}  "
        f"{s['win_rate_pct']:>5.1f}  {s['net_pnl_usdt']:>+9.2f}  "
        f"{s['net_pnl_pct']:>+8.3f}  {s['sharpe_ratio']:>7.4f}  "
        f"{s['max_drawdown_pct']:>7.3f}  {pf_s:>6}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--months', type=int, default=6)
    parser.add_argument('--symbols', nargs='+', default=_SYMBOLS)
    parser.add_argument('--adx-thresholds', nargs='+', type=float, default=[55.0],
                        help='One or more ADX thresholds; each yields its own champion variant')
    parser.add_argument('--out', default=str(_RESULTS_DIR / 'multi_symbol_v3.json'))
    args = parser.parse_args()

    baseline = AdvancedParams(label='baseline', apply_costs=True)
    champions: list[AdvancedParams] = [
        AdvancedParams(
            label=f'champion-ADX<{int(t)}', apply_costs=True,
            use_trend_filter=True, use_adx_filter=True, adx_threshold=t,
        )
        for t in args.adx_thresholds
    ]

    all_results: dict = {'months': args.months, 'symbols': {},
                         'thresholds': args.adx_thresholds}

    sep = '─' * len(_HDR)
    logger.info(sep)
    labels = ', '.join(c.label for c in champions)
    logger.info(
        'MULTI-SYMBOL VALIDATION — %d months — baseline vs. %s',
        args.months, labels,
    )
    logger.info(sep)
    logger.info(_HDR)
    logger.info(sep)

    for sym in args.symbols:
        df = _fetch(sym, args.months)
        if len(df) < 300:
            logger.warning('%s too short, skipping', sym)
            continue
        sym_data: dict = {'candles': len(df), 'baseline': None, 'champions': {}}
        b = compute_summary(simulate(df, baseline))
        sym_data['baseline'] = b
        logger.info(_row(sym, baseline.label, b))
        for ch in champions:
            c = compute_summary(simulate(df, ch))
            sym_data['champions'][ch.label] = c
            logger.info(_row(sym, ch.label, c))
        logger.info('─' * len(_HDR))
        all_results['symbols'][sym] = sym_data

    # combined aggregate per config
    def aggregate_summaries(summaries: list[dict]) -> dict:
        total = {'net_pnl_usdt': 0.0, 'num_trades': 0, 'wins': 0,
                 'fees_paid_usdt': 0.0, 'slippage_cost_usdt': 0.0}
        for r in summaries:
            total['net_pnl_usdt'] += r['net_pnl_usdt']
            total['num_trades'] += r['num_trades']
            total['wins'] += round(r['num_trades'] * r['win_rate_pct'] / 100)
            total['fees_paid_usdt'] += r['fees_paid_usdt']
            total['slippage_cost_usdt'] += r['slippage_cost_usdt']
        wr = (total['wins'] / total['num_trades'] * 100) if total['num_trades'] else 0.0
        total['win_rate_pct'] = round(wr, 2)
        return total

    combined: dict = {}
    base_summaries = [s['baseline'] for s in all_results['symbols'].values()]
    combined['baseline'] = aggregate_summaries(base_summaries)
    logger.info(
        'COMBINED      %-22s %3d  %5.1f  %+9.2f  (fees %.2f  slip %.2f)',
        baseline.label,
        combined['baseline']['num_trades'], combined['baseline']['win_rate_pct'],
        combined['baseline']['net_pnl_usdt'],
        combined['baseline']['fees_paid_usdt'], combined['baseline']['slippage_cost_usdt'],
    )
    for ch in champions:
        ch_summaries = [s['champions'][ch.label] for s in all_results['symbols'].values()]
        agg = aggregate_summaries(ch_summaries)
        combined[ch.label] = agg
        logger.info(
            'COMBINED      %-22s %3d  %5.1f  %+9.2f  (fees %.2f  slip %.2f)',
            ch.label,
            agg['num_trades'], agg['win_rate_pct'], agg['net_pnl_usdt'],
            agg['fees_paid_usdt'], agg['slippage_cost_usdt'],
        )
    logger.info(sep)
    all_results['combined'] = combined

    Path(args.out).write_text(json.dumps(all_results, indent=2, default=str))
    logger.info('report_saved path=%s', args.out)


if __name__ == '__main__':
    main()
