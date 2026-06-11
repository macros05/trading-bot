"""Full v7 backtest: long+short, all new filters, per-session metrics,
distribution analysis, buy-and-hold comparison, CSV export.

Run from project root:
    python -m backtest.v7_full --weeks 4 --symbol BTC/USDT
    python -m backtest.v7_full --weeks 4 --baseline   # v6 settings (no MTF/range/vol filters)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import BOT_CONFIG, SLIPPAGE, TAKER_FEE
from strategy.indicators import (
    adx, atr, higher_tf_trend_sma, rsi, sma, volume_sma,
)
from strategy.regime import (
    atr_percentile_bounds, is_mtf_aligned, is_position_stalled,
    is_quiet_range, passes_short_trend_filter, passes_volatility_window,
    shorts_disabled_in_flat,
)
from strategy.sessions import is_session_allowed, session_for_ts
from strategy.signals import (
    calc_pnl, calc_pnl_short, check_exit_price, passes_regime_filters,
    should_enter, should_enter_short, should_exit_time,
    tighten_sl_tp_for_stalled, update_trailing_stop_pct,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent / 'results'


@dataclass
class V7Params:
    """Strategy params for the full v7 backtest. Defaults read from BOT_CONFIG."""
    label:                    str   = 'v7-full'
    rsi_long_threshold:       float = BOT_CONFIG['rsi_threshold']
    rsi_short_threshold:      float = BOT_CONFIG['rsi_short_threshold']
    sl_pct_long:              float = BOT_CONFIG['stop_loss_pct_long']
    tp_pct_long:              float = BOT_CONFIG['take_profit_pct_long']
    sl_pct_short:             float = BOT_CONFIG['stop_loss_pct_short']
    tp_pct_short:             float = BOT_CONFIG['take_profit_pct_short']
    use_trailing_stop:        bool  = BOT_CONFIG['use_trailing_stop']
    trailing_breakeven_pct:   float = BOT_CONFIG['trailing_breakeven_pct']
    trailing_trail_pct:       float = BOT_CONFIG['trailing_trail_pct']
    trailing_distance_pct:    float = BOT_CONFIG['trailing_distance_pct']
    use_adx_filter:           bool  = BOT_CONFIG['use_adx_filter']
    adx_threshold:            float = BOT_CONFIG['adx_threshold']
    max_hold_hours:           float = BOT_CONFIG['max_hold_hours']
    stalled_hours:            float = BOT_CONFIG['stalled_hours']
    stalled_move_threshold:   float = BOT_CONFIG['stalled_move_threshold']
    range_lookback_min:       int   = BOT_CONFIG['range_lookback_min']
    range_pct_threshold:      float = BOT_CONFIG['range_pct_threshold']
    use_volatility_filter:    bool  = BOT_CONFIG['use_volatility_filter']
    volatility_lookback_hours: int  = BOT_CONFIG['volatility_lookback_hours']
    volatility_low_pct:       float = BOT_CONFIG['volatility_low_pct']
    volatility_high_pct:      float = BOT_CONFIG['volatility_high_pct']
    use_mtf_filter:           bool  = BOT_CONFIG['use_mtf_filter']
    mtf_15m_period:           int   = BOT_CONFIG['mtf_15m_period']
    use_short_trend_filter:   bool  = BOT_CONFIG['use_short_trend_filter']
    short_adx_min:            float = BOT_CONFIG['short_adx_min']
    short_sma_period:         int   = BOT_CONFIG['short_sma_period']
    adx_flat_threshold:       float = BOT_CONFIG['adx_flat_threshold']
    use_session_filter:       bool  = BOT_CONFIG['use_session_filter']
    blocked_sessions:         tuple = field(default_factory=lambda: tuple(BOT_CONFIG['blocked_sessions']))
    risk_pct:                 float = BOT_CONFIG['risk_pct']
    balance:                  float = BOT_CONFIG['paper_balance']
    apply_costs:              bool  = True
    taker_fee:                float = TAKER_FEE
    slippage:                 float = SLIPPAGE


def baseline_v6_params() -> V7Params:
    """V6-style baseline: long+short with ADX filter only — no MTF/vol/session/range/trailing."""
    return V7Params(
        label='v6-baseline',
        use_trailing_stop=False,
        use_volatility_filter=False,
        use_mtf_filter=False,
        use_session_filter=False,
        use_short_trend_filter=False,
        range_lookback_min=0,
        stalled_hours=0.0,
        rsi_long_threshold=40.0,
        rsi_short_threshold=55.0,
        sl_pct_long=0.012,
        tp_pct_long=0.018,
        sl_pct_short=0.012,
        tp_pct_short=0.018,
        max_hold_hours=12.0,
    )


# ── data fetch ───────────────────────────────────────────────────────────────

def fetch_history(symbol: str, weeks: int) -> pd.DataFrame:
    exchange = ccxt.binance({'timeout': 15_000, 'options': {'defaultType': 'spot'}})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - weeks * 7 * 24 * 60 * 60 * 1000
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
        if pages % 20 == 0:
            logger.info('fetching pages=%d candles=%d', pages, len(rows))
        if len(page) < 1000:
            break
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    logger.info('fetch_complete symbol=%s pages=%d candles=%d', symbol, pages, len(df))
    return df


# ── simulator ────────────────────────────────────────────────────────────────

def _entry_blocked(
    p: V7Params, side: str, close: float, sma20: float, sma50: float | None,
    adx_v: float | None, atr_v: float | None,
    atr_bounds: tuple[float, float] | None,
    htf_15m: bool | None, last_closes: list[float], ts_ms: int,
) -> str | None:
    if p.use_session_filter and not is_session_allowed(ts_ms, p.blocked_sessions):
        return f'session_blocked={session_for_ts(ts_ms)}'
    if not passes_regime_filters(
        trend_bullish=None, adx_val=adx_v,
        adx_threshold=p.adx_threshold,
        use_trend_filter=False, use_adx_filter=p.use_adx_filter,
    ):
        return 'adx_overheated'
    if p.range_lookback_min > 0 and len(last_closes) >= p.range_lookback_min:
        if is_quiet_range(last_closes[-p.range_lookback_min:], p.range_pct_threshold):
            return 'quiet_range'
    if p.use_volatility_filter and not passes_volatility_window(atr_v, atr_bounds):
        return 'volatility_outside_window'
    if p.use_mtf_filter and not is_mtf_aligned(side, htf_15m, None,
                                               require_15m=True, require_1h=False):
        return 'mtf_misaligned'
    if side == 'short':
        if p.use_short_trend_filter and not passes_short_trend_filter(
            close, sma50, adx_v, p.short_adx_min,
        ):
            return 'short_trend_filter'
        if shorts_disabled_in_flat(adx_v, p.adx_flat_threshold):
            return 'shorts_disabled_in_flat'
    return None


def simulate_v7(df: pd.DataFrame, p: V7Params) -> dict:
    rsi_s = rsi(df, 14)
    sma20_s = sma(df, 20)
    sma50_s = sma(df, p.short_sma_period)
    atr_s = atr(df, 14)
    adx_s = adx(df, 14) if p.use_adx_filter or p.use_short_trend_filter else None
    htf15_s = (higher_tf_trend_sma(df, '15min', p.mtf_15m_period)
               if p.use_mtf_filter else None)

    balance = p.balance
    equity = [balance]
    trades: list[dict] = []
    position: dict | None = None
    closes_during_pos: list[float] = []
    atr_history: list[float] = []
    max_atr_window = p.volatility_lookback_hours * 60

    warmup = max(60, p.short_sma_period, p.mtf_15m_period * 15 if p.use_mtf_filter else 0)
    n = len(df)
    closes_arr = df['close'].to_numpy()
    ts_arr = df['ts'].to_numpy()

    fees_total = 0.0

    for i in range(warmup, n):
        close = float(closes_arr[i])
        ts_ms = int(ts_arr[i])
        rsi_v = float(rsi_s.iloc[i])
        if pd.isna(rsi_v):
            continue
        sma20_v = float(sma20_s.iloc[i])
        if pd.isna(sma20_v):
            continue
        sma50_raw = sma50_s.iloc[i]
        sma50_v = float(sma50_raw) if pd.notna(sma50_raw) else None
        atr_raw = atr_s.iloc[i]
        atr_v = float(atr_raw) if pd.notna(atr_raw) else None
        if atr_v is not None:
            atr_history.append(atr_v)
            if len(atr_history) > max_atr_window:
                atr_history = atr_history[-max_atr_window:]
        adx_v: float | None = None
        if adx_s is not None:
            adx_raw = adx_s.iloc[i]
            adx_v = float(adx_raw) if pd.notna(adx_raw) else None
        htf15: bool | None = None
        if htf15_s is not None:
            htf_raw = htf15_s.iloc[i]
            htf15 = bool(htf_raw) if pd.notna(htf_raw) else None

        if position is None:
            long_sig = should_enter(close, sma20_v, rsi_v, rsi_threshold=p.rsi_long_threshold)
            short_sig = should_enter_short(close, sma20_v, rsi_v,
                                           rsi_threshold=p.rsi_short_threshold)
            if long_sig and short_sig:
                continue
            if not long_sig and not short_sig:
                continue
            side = 'long' if long_sig else 'short'
            atr_bounds = atr_percentile_bounds(
                atr_history, p.volatility_low_pct, p.volatility_high_pct,
            ) if p.use_volatility_filter else None
            last_closes = [float(c) for c in closes_arr[max(0, i - p.range_lookback_min):i + 1]]
            block = _entry_blocked(
                p, side, close, sma20_v, sma50_v, adx_v, atr_v,
                atr_bounds, htf15, last_closes, ts_ms,
            )
            if block:
                continue
            entry_fill = close * (1 + p.slippage * (1 if side == 'long' else -1)) if p.apply_costs else close
            if side == 'long':
                sl_price = entry_fill * (1 - p.sl_pct_long)
                tp_price = entry_fill * (1 + p.tp_pct_long)
            else:
                sl_price = entry_fill * (1 + p.sl_pct_short)
                tp_price = entry_fill * (1 - p.tp_pct_short)
            effective_sl_pct = abs(entry_fill - sl_price) / entry_fill
            notional = balance * p.risk_pct / max(effective_sl_pct, 0.005)
            qty = notional / entry_fill
            position = {
                'side': side, 'entry_price': entry_fill, 'qty': qty,
                'sl_price': sl_price, 'tp_price': tp_price,
                'entry_ts': ts_ms, 'notional': notional,
                'stalled_tightened': False,
            }
            closes_during_pos = [close]
            continue

        # In position
        closes_during_pos.append(close)
        side = position['side']
        if p.use_trailing_stop:
            new_sl, _ = update_trailing_stop_pct(
                position['sl_price'], position['entry_price'], close, side,
                p.trailing_breakeven_pct, p.trailing_trail_pct, p.trailing_distance_pct,
            )
            position['sl_price'] = new_sl
        # Stalled tightening
        elapsed_h = (ts_ms - position['entry_ts']) / 3_600_000
        if (p.stalled_hours > 0 and not position['stalled_tightened']
            and elapsed_h >= p.stalled_hours
            and is_position_stalled(closes_during_pos, p.stalled_move_threshold)):
            ns, nt = tighten_sl_tp_for_stalled(
                position['sl_price'], position['tp_price'],
                position['entry_price'], side,
            )
            position['sl_price'] = ns
            position['tp_price'] = nt
            position['stalled_tightened'] = True

        reason = check_exit_price(close, position['sl_price'], position['tp_price'], side=side)
        if reason is None and should_exit_time(position['entry_ts'], ts_ms, p.max_hold_hours):
            reason = 'time_exit'
        if reason is None:
            continue

        exit_fill = close * (1 - p.slippage * (1 if side == 'long' else -1)) if p.apply_costs else close
        if side == 'long':
            gross = (exit_fill - position['entry_price']) * position['qty']
        else:
            gross = (position['entry_price'] - exit_fill) * position['qty']
        fees = position['notional'] * 2 * p.taker_fee if p.apply_costs else 0.0
        net_pnl = gross - fees
        fees_total += fees
        balance += net_pnl
        equity.append(balance)
        trades.append({
            'side':         side,
            'entry_price':  position['entry_price'],
            'exit_price':   exit_fill,
            'qty':          position['qty'],
            'pnl_usdt':     round(net_pnl, 4),
            'pnl_pct':      round(net_pnl / position['notional'] * 100, 4),
            'result':       'WIN' if net_pnl >= 0 else 'LOSS',
            'reason':       reason,
            'entry_ts':     position['entry_ts'],
            'exit_ts':      ts_ms,
            'duration_min': round((ts_ms - position['entry_ts']) / 60_000, 1),
            'session':      session_for_ts(position['entry_ts']),
        })
        position = None
        closes_during_pos = []

    return {
        'label':       p.label,
        'trades':      trades,
        'equity':      equity,
        'final_balance': round(balance, 4),
        'total_pnl':   round(balance - p.balance, 4),
        'total_fees':  round(fees_total, 4),
        'params':      asdict(p),
        'period':      {
            'from': datetime.fromtimestamp(int(df['ts'].iloc[0]) / 1000, tz=timezone.utc).isoformat(),
            'to':   datetime.fromtimestamp(int(df['ts'].iloc[-1]) / 1000, tz=timezone.utc).isoformat(),
            'candles': len(df),
        },
    }


# ── analytics ────────────────────────────────────────────────────────────────

def buy_and_hold(df: pd.DataFrame, balance: float = 10_000.0,
                 fee: float = TAKER_FEE) -> dict:
    """Simple buy-and-hold over the period: enter at first candle, exit at last."""
    if df.empty:
        return {'final_balance': balance, 'pnl': 0.0, 'pnl_pct': 0.0}
    entry = float(df['close'].iloc[0])
    exit_p = float(df['close'].iloc[-1])
    qty = balance / entry
    gross = (exit_p - entry) * qty
    fees = balance * 2 * fee
    pnl = gross - fees
    return {
        'final_balance': round(balance + pnl, 4),
        'pnl_usdt':      round(pnl, 4),
        'pnl_pct':       round(pnl / balance * 100, 4),
        'entry_price':   entry,
        'exit_price':    exit_p,
    }


def metrics_summary(result: dict, initial_balance: float = 10_000.0) -> dict:
    trades = result['trades']
    n = len(trades)
    if n == 0:
        return {
            **result,
            'num_trades': 0,
            'win_rate_pct': 0.0,
            'sharpe': 0.0,
        }
    wins = sum(1 for t in trades if t['result'] == 'WIN')
    pnls = [t['pnl_usdt'] for t in trades]
    durations = [t['duration_min'] for t in trades]
    by_side = defaultdict(lambda: {'n': 0, 'wins': 0, 'pnl': 0.0})
    by_session = defaultdict(lambda: {'n': 0, 'wins': 0, 'pnl': 0.0})
    by_reason = defaultdict(int)
    for t in trades:
        by_side[t['side']]['n'] += 1
        by_side[t['side']]['wins'] += int(t['result'] == 'WIN')
        by_side[t['side']]['pnl'] += t['pnl_usdt']
        by_session[t['session']]['n'] += 1
        by_session[t['session']]['wins'] += int(t['result'] == 'WIN')
        by_session[t['session']]['pnl'] += t['pnl_usdt']
        by_reason[t['reason']] += 1

    # Sharpe (per-trade, non-annualized)
    sharpe = 0.0
    if n >= 2:
        returns = [t['pnl_pct'] / 100 for t in trades]
        mean = sum(returns) / n
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = mean / std if std > 0 else 0.0

    # Drawdown
    peak = initial_balance
    max_dd_pct = 0.0
    for v in result['equity']:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    side_view = {
        s: {'trades': v['n'],
            'wins': v['wins'],
            'win_rate_pct': round(v['wins'] / v['n'] * 100, 2) if v['n'] else 0.0,
            'pnl': round(v['pnl'], 4)}
        for s, v in by_side.items()
    }
    session_view = {
        s: {'trades': v['n'],
            'wins': v['wins'],
            'win_rate_pct': round(v['wins'] / v['n'] * 100, 2) if v['n'] else 0.0,
            'pnl': round(v['pnl'], 4)}
        for s, v in by_session.items()
    }
    return {
        **{k: result[k] for k in ('label', 'final_balance', 'total_pnl', 'total_fees', 'period')},
        'num_trades':        n,
        'wins':              wins,
        'losses':            n - wins,
        'win_rate_pct':      round(wins / n * 100, 2),
        'best_trade_usdt':   round(max(pnls), 4),
        'worst_trade_usdt':  round(min(pnls), 4),
        'avg_trade_usdt':    round(sum(pnls) / n, 4),
        'median_pnl_usdt':   round(statistics.median(pnls), 4),
        'avg_duration_min':  round(sum(durations) / n, 1),
        'median_duration_min': round(statistics.median(durations), 1),
        'max_drawdown_pct':  round(max_dd_pct * 100, 4),
        'sharpe':            round(sharpe, 4),
        'by_side':           side_view,
        'by_session':        session_view,
        'by_reason':         dict(by_reason),
    }


# ── output ───────────────────────────────────────────────────────────────────

def export_trades_csv(trades: list[dict], path: Path) -> None:
    if not trades:
        return
    fieldnames = list(trades[0].keys())
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)


def comparative_table(baseline: dict, new: dict, bh: dict) -> str:
    def _fmt(v, fmt='{:+.2f}'):
        return fmt.format(v) if isinstance(v, (int, float)) else str(v)
    rows = [
        ('Trades',            baseline['num_trades'],          new['num_trades']),
        ('Win rate %',        baseline['win_rate_pct'],        new['win_rate_pct']),
        ('Total PnL (USDT)',  baseline['total_pnl'],           new['total_pnl']),
        ('Best trade',        baseline['best_trade_usdt'],     new['best_trade_usdt']),
        ('Worst trade',       baseline['worst_trade_usdt'],    new['worst_trade_usdt']),
        ('Avg PnL/trade',     baseline['avg_trade_usdt'],      new['avg_trade_usdt']),
        ('Avg duration (min)', baseline['avg_duration_min'],   new['avg_duration_min']),
        ('Max drawdown %',    baseline['max_drawdown_pct'],    new['max_drawdown_pct']),
        ('Sharpe (per trade)', baseline['sharpe'],             new['sharpe']),
        ('Total fees',        baseline['total_fees'],          new['total_fees']),
    ]
    lines = [
        f"{'Metric':<22}  {'Baseline (v6)':>14}  {'New (v7)':>14}  {'Δ':>10}",
        '─' * 68,
    ]
    for name, b, n in rows:
        delta = (n - b) if isinstance(b, (int, float)) else ''
        lines.append(f'{name:<22}  {_fmt(b):>14}  {_fmt(n):>14}  {_fmt(delta):>10}')
    lines.append('─' * 68)
    lines.append(f"Buy & Hold same period: {bh['pnl_usdt']:+.2f} USDT ({bh['pnl_pct']:+.2f}%)")
    return '\n'.join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='BTC/USDT')
    parser.add_argument('--weeks', type=int, default=4)
    parser.add_argument('--out', default='backtest/results/v7_compare.json')
    parser.add_argument('--csv-out', default='backtest/results/v7_trades.csv')
    args = parser.parse_args()

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df = fetch_history(args.symbol, args.weeks)
    if len(df) < 100:
        logger.error('insufficient data candles=%d', len(df))
        return 1

    new_p = V7Params()
    base_p = baseline_v6_params()

    logger.info('running v7 (new) on %d candles', len(df))
    new_res = simulate_v7(df, new_p)
    new_summary = metrics_summary(new_res)
    logger.info('running v6 (baseline) on %d candles', len(df))
    base_res = simulate_v7(df, base_p)
    base_summary = metrics_summary(base_res)

    bh = buy_and_hold(df, new_p.balance)
    table = comparative_table(base_summary, new_summary, bh)
    print('\n' + table + '\n')

    out = {
        'period':   new_res['period'],
        'baseline': base_summary,
        'new':      new_summary,
        'buy_and_hold': bh,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    logger.info('saved %s', args.out)
    export_trades_csv(new_res['trades'], Path(args.csv_out))
    logger.info('saved %s', args.csv_out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
