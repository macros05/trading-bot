"""CLI for the advanced backtest harness.

Usage examples (run from project root):

    # 6-month baseline vs. improvements (BTC only)
    python -m backtest.cli --symbol BTC/USDT --months 6

    # Walk-forward on 24 months, 3-month train / 1-month test
    python -m backtest.cli --symbol BTC/USDT --months 24 --walk-forward

    # Custom exits / filters
    python -m backtest.cli --symbol BTC/USDT --months 6 --atr-exits --trend-filter --adx-filter
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.advanced import AdvancedParams, compute_summary, simulate
from backtest.walk_forward import WalkForwardConfig, run as run_walk_forward

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch(symbol: str, timeframe: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({'timeout': 15_000, 'options': {'defaultType': 'spot'}})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 24 * 60 * 60 * 1000
    rows: list[list] = []
    cursor = since_ms
    pages = 0
    page_size = 1000
    while cursor < now_ms:
        page = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page_size)
        if not page:
            break
        rows.extend(page)
        pages += 1
        cursor = page[-1][0] + 60_000
        if pages % 20 == 0:
            logger.info('fetching pages=%d candles=%d', pages, len(rows))
        if len(page) < page_size:
            break
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    logger.info('fetch_complete symbol=%s pages=%d candles=%d', symbol, pages, len(df))
    return df


# ── configs to compare ────────────────────────────────────────────────────────

def _configs_from_args(args: argparse.Namespace) -> list[AdvancedParams]:
    """Return the list of configs to backtest based on CLI flags."""
    baseline = AdvancedParams(
        label='baseline-live',
        apply_costs=args.apply_costs,
    )
    baseline_no_cost = AdvancedParams(
        label='baseline-no-costs (legacy)',
        apply_costs=False,
    )
    with_costs_only = AdvancedParams(
        label='baseline + fees/slippage',
        apply_costs=True,
    )
    atr_exits = replace(
        baseline, label='ATR exits (1.5/3.0)',
        use_atr_exits=True,
    )
    trailing = replace(
        baseline, label='baseline + trailing stop',
        use_trailing_stop=True,
    )
    trend_only = replace(
        baseline, label='baseline + 1h EMA200 trend filter',
        use_trend_filter=True,
    )
    adx_only = replace(
        baseline, label='baseline + ADX<25 filter',
        use_adx_filter=True,
    )
    full_stack = replace(
        baseline,
        label='FULL: ATR exits + trailing + trend + ADX',
        use_atr_exits=True,
        use_trailing_stop=True,
        use_trend_filter=True,
        use_adx_filter=True,
    )
    # If the user passed specific flags, return a focused pair (baseline vs. requested)
    if any([args.atr_exits, args.trailing_stop, args.trend_filter, args.adx_filter]):
        custom = replace(
            baseline,
            label='custom',
            use_atr_exits=args.atr_exits,
            use_trailing_stop=args.trailing_stop,
            use_trend_filter=args.trend_filter,
            use_adx_filter=args.adx_filter,
        )
        return [baseline, custom]

    # Safe ATR exits: SL floor at 0.5 % + spot leverage cap at 1.0
    atr_exits_safe = replace(
        baseline,
        label='ATR exits (1.5/3.0) + 0.5% SL floor + 1x cap',
        use_atr_exits=True,
        min_sl_pct=0.005,
        max_leverage=1.0,
    )
    # ADX threshold sweep (no trend filter — isolates ADX effect)
    adx_25 = replace(baseline, label='baseline + ADX<25 filter (strict)',
                     use_adx_filter=True, adx_threshold=25.0)
    adx_35 = replace(baseline, label='baseline + ADX<35 filter',
                     use_adx_filter=True, adx_threshold=35.0)
    adx_45 = replace(baseline, label='baseline + ADX<45 filter (loose)',
                     use_adx_filter=True, adx_threshold=45.0)
    # Safe full stack
    full_stack_safe = replace(
        baseline,
        label='FULL-safe: ATR(floor) + trailing + trend + ADX<45',
        use_atr_exits=True, min_sl_pct=0.005, max_leverage=1.0,
        use_trailing_stop=True,
        use_trend_filter=True,
        use_adx_filter=True, adx_threshold=45.0,
    )

    # Default compare list
    return [
        baseline_no_cost,
        with_costs_only,
        atr_exits,
        atr_exits_safe,
        trailing,
        trend_only,
        adx_25,
        adx_35,
        adx_45,
        full_stack,
        full_stack_safe,
    ]


# ── reporting ─────────────────────────────────────────────────────────────────

_HDR = (
    f"{'Config':<42}  {'#':>4}  {'WR%':>5}  {'NetPnL$':>9}  {'NetPnL%':>8}  "
    f"{'Sharpe':>7}  {'MaxDD%':>7}  {'Fees$':>7}  {'Slip$':>7}  {'PF':>6}"
)


def _fmt_row(s: dict) -> str:
    pf = s.get('profit_factor')
    pf_s = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
    return (
        f"{s['label']:<42}  {s['num_trades']:>4}  {s['win_rate_pct']:>5.1f}  "
        f"{s['net_pnl_usdt']:>+9.2f}  {s['net_pnl_pct']:>+8.3f}  "
        f"{s['sharpe_ratio']:>7.4f}  {s['max_drawdown_pct']:>7.3f}  "
        f"{s['fees_paid_usdt']:>7.2f}  {s['slippage_cost_usdt']:>7.2f}  {pf_s:>6}"
    )


def _log_table(summaries: list[dict], title: str) -> None:
    sep = '─' * len(_HDR)
    logger.info(sep)
    logger.info(title)
    logger.info(sep)
    logger.info(_HDR)
    logger.info(sep)
    for s in summaries:
        logger.info(_fmt_row(s))
    logger.info(sep)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='Advanced backtest runner')
    parser.add_argument('--symbol', default='BTC/USDT')
    parser.add_argument('--timeframe', default='1m')
    parser.add_argument('--months', type=int, default=6)
    parser.add_argument('--walk-forward', action='store_true')
    parser.add_argument('--train-months', type=int, default=3)
    parser.add_argument('--test-months', type=int, default=1)
    parser.add_argument('--step-months', type=int, default=1)
    parser.add_argument('--no-costs', dest='apply_costs', action='store_false', default=True)
    parser.add_argument('--atr-exits', action='store_true', default=False)
    parser.add_argument('--trailing-stop', action='store_true', default=False)
    parser.add_argument('--trend-filter', action='store_true', default=False)
    parser.add_argument('--adx-filter', action='store_true', default=False)
    parser.add_argument('--adx-threshold', type=float, default=45.0,
                        help='ADX regime threshold (trades blocked when ADX >= threshold)')
    parser.add_argument('--out', default=None,
                        help='Optional JSON output file (default: backtest/results/advanced_report.json)')
    args = parser.parse_args()

    df = _fetch(args.symbol, args.timeframe, args.months)
    if len(df) < 300:
        logger.error('insufficient_data candles=%d', len(df))
        sys.exit(1)

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else _RESULTS_DIR / 'advanced_report.json'

    period = {
        'from_ms': int(df['ts'].iloc[0]),
        'to_ms':   int(df['ts'].iloc[-1]),
        'candles': len(df),
        'from':    pd.to_datetime(int(df['ts'].iloc[0]), unit='ms', utc=True).isoformat(),
        'to':      pd.to_datetime(int(df['ts'].iloc[-1]), unit='ms', utc=True).isoformat(),
    }

    if args.walk_forward:
        params = AdvancedParams(
            label='walk_forward',
            apply_costs=args.apply_costs,
            use_atr_exits=args.atr_exits,
            use_trailing_stop=args.trailing_stop,
            use_trend_filter=args.trend_filter,
            use_adx_filter=args.adx_filter,
            adx_threshold=args.adx_threshold,
        )
        wf_cfg = WalkForwardConfig(
            train_months=args.train_months,
            test_months=args.test_months,
            step_months=args.step_months,
        )
        result = run_walk_forward(df, params, wf_cfg)
        logger.info('walk_forward_complete folds=%d', result['num_folds'])
        _log_table(result['folds'], f'WALK-FORWARD — {args.symbol}')
        agg = result['aggregate']
        logger.info('aggregate trades=%d win_rate=%.1f%% sharpe=%.4f max_dd=%.3f%% net_pnl=$%.2f (%.3f%%)',
                    agg['num_trades'], agg.get('win_rate_pct', 0.0), agg.get('sharpe_ratio', 0.0),
                    agg.get('max_drawdown_pct', 0.0), agg['net_pnl_usdt'], agg['net_pnl_pct'])
        out_path.write_text(json.dumps({'period': period, 'symbol': args.symbol,
                                        'walk_forward': result}, indent=2, default=str))
    else:
        configs = _configs_from_args(args)
        summaries: list[dict] = []
        for params in configs:
            result = simulate(df, params)
            summaries.append(compute_summary(result))
        summaries.sort(key=lambda s: s.get('sharpe_ratio', -999), reverse=True)
        _log_table(summaries, f'BACKTEST — {args.symbol}  {period["from"]} → {period["to"]}')
        out_path.write_text(json.dumps({'period': period, 'symbol': args.symbol,
                                        'configs': summaries}, indent=2, default=str))

    logger.info('report_saved path=%s', out_path)


if __name__ == '__main__':
    main()
