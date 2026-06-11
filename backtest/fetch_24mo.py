"""Fetch 24 months of 1-minute OHLCV for a symbol and cache to pkl.

Usage:
    python -m backtest.fetch_24mo --symbol ETH/USDT
    python -m backtest.fetch_24mo --symbol SOL/USDT

Public Binance endpoint, no API key needed. Cache filename matches the
pattern sweep_v7_full uses: _cache_<sym>_1m_24mo.pkl.
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'


def fetch(symbol: str, months: int = 24) -> pd.DataFrame:
    exchange = ccxt.binance({'timeout': 15_000, 'options': {'defaultType': 'spot'}})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 24 * 60 * 60 * 1000
    rows: list[list] = []
    cursor = since_ms
    pages = 0
    while cursor < now_ms:
        page = exchange.fetch_ohlcv(symbol, '1m', since=cursor, limit=1000)
        if not page:
            break
        rows.extend(page)
        pages += 1
        cursor = page[-1][0] + 60_000
        if pages % 50 == 0:
            print(f'  pages={pages} candles={len(rows)}', file=sys.stderr)
        if len(page) < 1000:
            break
        time.sleep(0.18)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    return df


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', required=True)
    p.add_argument('--months', type=int, default=24)
    args = p.parse_args()
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sym_safe = args.symbol.lower().replace('/', '').replace('usdt', '')
    out = _RESULTS_DIR / f'_cache_{sym_safe}_1m_24mo.pkl'
    print(f'fetching {args.symbol} 1m × {args.months} months…')
    df = fetch(args.symbol, args.months)
    if len(df) < 100:
        print(f'  insufficient data: candles={len(df)}', file=sys.stderr)
        return 1
    print(f'  fetched {len(df):,} candles  range='
          f'{pd.to_datetime(df["ts"].iloc[0], unit="ms", utc=True)} → '
          f'{pd.to_datetime(df["ts"].iloc[-1], unit="ms", utc=True)}')
    with open(out, 'wb') as f:
        pickle.dump(df, f)
    print(f'saved: {out} ({out.stat().st_size / 1_000_000:.1f} MB)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
