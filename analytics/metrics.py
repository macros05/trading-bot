"""Performance metric computations from trades_history.json.

Pure functions over a list of trade dicts; no I/O.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from strategy.sessions import session_for_ts


def _date_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


def daily_pnl(trades: list[dict], days: int = 30) -> list[dict]:
    """Return up to `days` of daily PnL totals, oldest first."""
    by_day: defaultdict[str, float] = defaultdict(float)
    for t in trades:
        by_day[_date_key(int(t.get('exit_ts', 0)))] += float(t.get('pnl_usdt', 0.0))
    items = sorted(by_day.items())[-days:]
    return [{'date': d, 'pnl_usdt': round(v, 4)} for d, v in items]


def win_rate_by_side(trades: list[dict]) -> dict[str, dict]:
    """Per-side stats: long/short → {trades, wins, losses, win_rate, total_pnl}."""
    out: dict[str, dict] = {}
    for side in ('long', 'short'):
        sub = [t for t in trades if t.get('side', 'long') == side]
        wins = sum(1 for t in sub if t.get('result') == 'WIN')
        n = len(sub)
        out[side] = {
            'trades':       n,
            'wins':         wins,
            'losses':       n - wins,
            'win_rate_pct': round(wins / n * 100, 2) if n else 0.0,
            'total_pnl':    round(sum(t.get('pnl_usdt', 0.0) for t in sub), 4),
        }
    return out


def win_rate_by_session(trades: list[dict]) -> dict[str, dict]:
    """Per-session stats keyed by entry_ts."""
    sessions: defaultdict[str, list[dict]] = defaultdict(list)
    for t in trades:
        sessions[session_for_ts(int(t.get('entry_ts', 0)))].append(t)
    out: dict[str, dict] = {}
    for label, sub in sessions.items():
        wins = sum(1 for t in sub if t.get('result') == 'WIN')
        n = len(sub)
        out[label] = {
            'trades':       n,
            'wins':         wins,
            'losses':       n - wins,
            'win_rate_pct': round(wins / n * 100, 2) if n else 0.0,
            'total_pnl':    round(sum(t.get('pnl_usdt', 0.0) for t in sub), 4),
        }
    return out


def equity_curve(trades: list[dict], initial_balance: float = 10_000.0) -> list[dict]:
    """Cumulative balance after each trade, ordered by exit_ts."""
    sorted_t = sorted(trades, key=lambda x: int(x.get('exit_ts', 0)))
    bal = initial_balance
    points: list[dict] = [{'ts': 0, 'balance': round(bal, 4)}]
    for t in sorted_t:
        bal += float(t.get('pnl_usdt', 0.0))
        points.append({'ts': int(t.get('exit_ts', 0)), 'balance': round(bal, 4)})
    return points


def max_drawdown(trades: list[dict], initial_balance: float = 10_000.0) -> dict:
    """Return {pct, usdt, peak_balance, trough_balance}."""
    curve = equity_curve(trades, initial_balance)
    peak = initial_balance
    trough = initial_balance
    max_dd_pct = 0.0
    max_dd_usdt = 0.0
    for p in curve:
        bal = p['balance']
        if bal > peak:
            peak = bal
            trough = bal
        if bal < trough:
            trough = bal
        dd = (peak - bal) / peak if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_usdt = peak - bal
    return {
        'pct':             round(max_dd_pct * 100, 4),
        'usdt':            round(max_dd_usdt, 4),
        'peak_balance':    round(peak, 4),
        'trough_balance':  round(trough, 4),
    }


def pnl_drawdown_ratio(total_pnl: float, max_dd_usdt: float) -> float:
    """Return total_pnl / max_drawdown — higher is better. Returns 0 when DD≈0."""
    if max_dd_usdt <= 0.01:
        return 0.0
    return round(total_pnl / max_dd_usdt, 4)


def compute_performance(
    trades: list[dict], initial_balance: float = 10_000.0,
) -> dict:
    """Aggregate everything the dashboard needs in a single payload."""
    total_pnl = sum(float(t.get('pnl_usdt', 0.0)) for t in trades)
    wins = sum(1 for t in trades if t.get('result') == 'WIN')
    n = len(trades)
    dd = max_drawdown(trades, initial_balance)
    last_20 = sorted(trades, key=lambda x: int(x.get('exit_ts', 0)))[-20:]
    last_20_view = [
        {
            'side':         t.get('side', 'long'),
            'entry_price':  t.get('entry_price'),
            'exit_price':   t.get('exit_price'),
            'pnl_usdt':     t.get('pnl_usdt'),
            'pnl_pct':      t.get('pnl_pct'),
            'reason':       t.get('reason'),
            'result':       t.get('result'),
            'duration_min': max(0,
                round((int(t.get('exit_ts', 0)) - int(t.get('entry_ts', 0))) / 60_000, 1),
            ),
            'entry_ts':     int(t.get('entry_ts', 0)),
            'exit_ts':      int(t.get('exit_ts', 0)),
            'session':      session_for_ts(int(t.get('entry_ts', 0))),
        }
        for t in last_20
    ]
    return {
        'total_trades':       n,
        'wins':               wins,
        'losses':             n - wins,
        'win_rate_pct':       round(wins / n * 100, 2) if n else 0.0,
        'total_pnl_usdt':     round(total_pnl, 4),
        'final_balance':      round(initial_balance + total_pnl, 4),
        'max_drawdown':       dd,
        'pnl_dd_ratio':       pnl_drawdown_ratio(total_pnl, dd['usdt']),
        'by_side':            win_rate_by_side(trades),
        'by_session':         win_rate_by_session(trades),
        'daily_pnl_30d':      daily_pnl(trades, 30),
        'equity_curve':       equity_curve(trades, initial_balance),
        'last_20_trades':     last_20_view,
    }
