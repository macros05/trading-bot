"""Validate live paper-trading metrics against the v7 backtest baseline.

Compares win rate, PnL and trade cadence; emits structured alert dicts that
the weekly report and dashboard consume.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import Counter, defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Neutral baseline: zero asserted edge. Used when NO champion is certified.
# The audit (2026-05-28) removed the previous hardcoded reference (7 trades,
# WR 42.86 %, pnl -292 USDT) because comparing live performance against a
# LOSING backtest only raised alerts when live did worse than a money-loser —
# an inverted validation. With a neutral baseline the expected PnL is 0, so the
# (broken) pnl-divergence alert stays silent until a real champion is certified;
# the win-rate and rolling-degradation alerts keep working regardless.
NEUTRAL_BASELINE = {
    'win_rate_pct':       0.0,
    'pnl_usdt':           0.0,
    'avg_trade_per_week': 0.0,
    'max_drawdown_pct':   0.0,
}


def load_baseline(certificate: dict | None) -> dict:
    """Derive the validation baseline from a champion certificate.

    Returns the neutral baseline when no certificate (or no expected metrics)
    is available — never a losing reference.
    """
    if not certificate:
        return dict(NEUTRAL_BASELINE)
    expected = certificate.get('expected_metrics') or {}
    trades_per_year = expected.get('trades_per_year')
    return {
        'win_rate_pct':       expected.get('win_rate_pct') or 0.0,
        # Expected PnL stays neutral: converting a backtest % into a 28-day USDT
        # figure needs assumptions out of scope here. Wired in Spec 2.
        'pnl_usdt':           0.0,
        'avg_trade_per_week': (trades_per_year / 52.0) if trades_per_year else 0.0,
        'max_drawdown_pct':   expected.get('max_drawdown_pct') or 0.0,
    }


# Default reference when no explicit baseline is passed to ``evaluate``.
BACKTEST_BASELINE = dict(NEUTRAL_BASELINE)

# Severity thresholds — all configurable so tests can stress them
THRESH_OVERFITTING_WR     = 30.0   # below this with 20+ trades → overfitting alert
THRESH_PNL_DIVERGENCE_PCT = 50.0   # PnL drift > 50 % vs expected
THRESH_NO_TRADES_DAYS     = 7
THRESH_ROLLING_WR         = 30.0   # rolling 10-trade WR below this → degradation
ROLLING_WINDOW            = 10
MIN_TRADES_FOR_VALIDATION = 30
MIN_TRADES_FOR_OVERFITTING = 20


def _rolling_win_rate(trades: list[dict], window: int = ROLLING_WINDOW) -> float | None:
    if len(trades) < window:
        return None
    last = trades[-window:]
    wins = sum(1 for t in last if t.get('result') == 'WIN')
    return wins / window * 100.0


def _trades_sorted(trades: list[dict]) -> list[dict]:
    return sorted(trades, key=lambda t: int(t.get('exit_ts_ms', t.get('exit_ts', 0))))


def evaluate(
    live_trades: list[dict],
    days_running: int,
    now_ms: int | None = None,
    baseline: dict | None = None,
) -> dict[str, Any]:
    """Return a structured evaluation: alerts list + computed metrics.

    ``baseline`` defaults to the neutral baseline; pass ``load_baseline(cert)``
    to compare against the certified champion's expected metrics.
    """
    now_ms = now_ms or int(time.time() * 1000)
    baseline = baseline if baseline is not None else BACKTEST_BASELINE
    sorted_t = _trades_sorted(live_trades)
    n = len(sorted_t)
    wins = sum(1 for t in sorted_t if t.get('result') == 'WIN')
    pnl = sum(float(t.get('pnl_usdt', 0)) for t in sorted_t)
    wr = (wins / n * 100.0) if n else 0.0
    rolling_wr = _rolling_win_rate(sorted_t)

    last_trade_ts = int(sorted_t[-1].get('exit_ts_ms', sorted_t[-1].get('exit_ts', 0))) if n else 0
    days_since_last = (now_ms - last_trade_ts) / 86_400_000 if last_trade_ts else float('inf')

    expected_trades = baseline['avg_trade_per_week'] / 7 * max(days_running, 1)
    expected_pnl = baseline['pnl_usdt'] / 28 * max(days_running, 1)
    pnl_divergence_pct = (
        abs(pnl - expected_pnl) / abs(expected_pnl) * 100.0
        if abs(expected_pnl) > 0.01 else 0.0
    )

    alerts: list[dict] = []
    if n >= MIN_TRADES_FOR_OVERFITTING and wr < THRESH_OVERFITTING_WR:
        alerts.append({
            'level': 'warning', 'type': 'overfitting',
            'message': f'Win rate {wr:.1f}% < {THRESH_OVERFITTING_WR}% after {n} trades. '
                       f'Possible overfitting — review filters.',
        })
    if n > 0 and pnl_divergence_pct > THRESH_PNL_DIVERGENCE_PCT:
        alerts.append({
            'level': 'warning', 'type': 'pnl_divergence',
            'message': f'PnL {pnl:+.2f} diverges {pnl_divergence_pct:.0f}% from '
                       f'expected {expected_pnl:+.2f}.',
        })
    if days_since_last > THRESH_NO_TRADES_DAYS:
        alerts.append({
            'level': 'warning', 'type': 'no_trades',
            'message': f'No trades for {days_since_last:.1f} days — '
                       f'filters may be too restrictive.',
        })
    if rolling_wr is not None and rolling_wr < THRESH_ROLLING_WR:
        alerts.append({
            'level': 'critical', 'type': 'rolling_degradation',
            'message': f'Rolling WR (last {ROLLING_WINDOW}): {rolling_wr:.1f}% '
                       f'below {THRESH_ROLLING_WR}%.',
        })

    # Drawdown
    peak = 10_000.0
    bal = 10_000.0
    max_dd_pct = 0.0
    for t in sorted_t:
        bal += float(t.get('pnl_usdt', 0))
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    return {
        'n_trades':           n,
        'win_rate_pct':       round(wr, 2),
        'rolling_win_rate':   None if rolling_wr is None else round(rolling_wr, 2),
        'pnl_usdt':           round(pnl, 4),
        'expected_pnl_usdt':  round(expected_pnl, 4),
        'pnl_divergence_pct': round(pnl_divergence_pct, 2),
        'expected_trades':    round(expected_trades, 2),
        'days_since_last':    round(days_since_last, 1) if days_since_last != float('inf') else None,
        'max_drawdown_pct':   round(max_dd_pct * 100, 2),
        'alerts':             alerts,
        'baseline':           baseline,
        'remaining_for_validation': max(0, MIN_TRADES_FOR_VALIDATION - n),
    }


# ── Per-condition analysis ───────────────────────────────────────────────────

def _bucket_rsi(v: float | None) -> str:
    if v is None:
        return 'na'
    if v < 30:
        return '<30'
    if v < 40:
        return '30–40'
    if v < 50:
        return '40–50'
    if v < 60:
        return '50–60'
    if v < 70:
        return '60–70'
    return '>=70'


def _bucket_adx(v: float | None) -> str:
    if v is None:
        return 'na'
    if v < 18:
        return '<18'
    if v < 25:
        return '18–25'
    if v < 35:
        return '25–35'
    return '>=35'


def _bucket_atr_pct(v: float | None) -> str:
    if v is None:
        return 'na'
    if v < 20:
        return 'p<20'
    if v < 50:
        return 'p20–50'
    if v < 80:
        return 'p50–80'
    return 'p>=80'


def _summarise(group: list[dict]) -> dict:
    n = len(group)
    wins = sum(1 for t in group if t.get('result') == 'WIN')
    pnl = sum(float(t.get('pnl_usdt', 0)) for t in group)
    return {
        'n':            n,
        'wins':         wins,
        'losses':       n - wins,
        'win_rate_pct': round(wins / n * 100, 2) if n else 0.0,
        'total_pnl':    round(pnl, 4),
    }


def per_condition_analysis(trades: list[dict]) -> dict[str, Any]:
    """Group trades by RSI / ADX / ATR-percentile / session / MTF buckets."""
    by_rsi = defaultdict(list)
    by_adx = defaultdict(list)
    by_atr = defaultdict(list)
    by_session = defaultdict(list)
    by_mtf = defaultdict(list)
    by_regime = defaultdict(list)
    by_macro = defaultdict(list)
    for t in trades:
        by_rsi[_bucket_rsi(t.get('entry_rsi'))].append(t)
        by_adx[_bucket_adx(t.get('entry_adx'))].append(t)
        by_atr[_bucket_atr_pct(t.get('entry_atr_pct'))].append(t)
        by_session[t.get('session', 'na')].append(t)
        mtf = t.get('mtf_15m_aligned')
        by_mtf['aligned' if mtf else ('misaligned' if mtf == 0 else 'na')].append(t)
        by_regime[t.get('regime', 'unknown')].append(t)
        by_macro[t.get('macro_event') or 'normal'].append(t)
    return {
        'by_rsi':     {k: _summarise(v) for k, v in by_rsi.items()},
        'by_adx':     {k: _summarise(v) for k, v in by_adx.items()},
        'by_atr_pct': {k: _summarise(v) for k, v in by_atr.items()},
        'by_session': {k: _summarise(v) for k, v in by_session.items()},
        'by_mtf':     {k: _summarise(v) for k, v in by_mtf.items()},
        'by_regime':  {k: _summarise(v) for k, v in by_regime.items()},
        'by_macro':   {k: _summarise(v) for k, v in by_macro.items()},
    }


def underperforming_buckets(
    analysis: dict, min_trades: int = 5, win_rate_floor: float = 25.0,
) -> list[dict]:
    """Return buckets with at least *min_trades* trades and win rate < floor."""
    out: list[dict] = []
    for category, groups in analysis.items():
        for label, stats in groups.items():
            if stats['n'] >= min_trades and stats['win_rate_pct'] < win_rate_floor:
                out.append({
                    'category':     category, 'label': label,
                    'n':            stats['n'],
                    'win_rate_pct': stats['win_rate_pct'],
                    'total_pnl':    stats['total_pnl'],
                })
    return out


# ── Readiness for demo trading ───────────────────────────────────────────────

READINESS_MIN_TRADES = 30
READINESS_MIN_WR     = 38.0
READINESS_MAX_LOSS_USDT = -500.0
READINESS_MAX_DD_PCT = 10.0


def readiness_check(
    live_trades: list[dict], days_running: int, now_ms: int | None = None,
) -> dict[str, Any]:
    """Return readiness report — checks all six demo-trading criteria."""
    eval_ = evaluate(live_trades, days_running, now_ms)
    n = eval_['n_trades']
    wr = eval_['win_rate_pct']
    pnl = eval_['pnl_usdt']
    dd = eval_['max_drawdown_pct']
    alerts = eval_['alerts']

    macro_seen = any(
        (t.get('macro_event') or '') in ('FOMC', 'CPI', 'NFP')
        for t in live_trades
    )

    checks = {
        'min_30_trades':         n >= READINESS_MIN_TRADES,
        'win_rate_above_38pct':  wr >= READINESS_MIN_WR,
        'pnl_above_minus_500':   pnl >= READINESS_MAX_LOSS_USDT,
        'no_overfitting_alerts': not any(a['type'] == 'overfitting' for a in alerts),
        'drawdown_below_10pct':  dd <= READINESS_MAX_DD_PCT,
        'survived_macro_event':  macro_seen,
    }
    ready = all(checks.values())
    return {
        'ready':    ready,
        'checks':   checks,
        'metrics':  eval_,
    }
