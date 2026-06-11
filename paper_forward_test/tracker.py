"""Paper forward-test metrics tracker.

Reads `data/trades_history.json`, filters to trades that entered after the
paper-test start timestamp, and computes the gate metrics defined in
IMPROVEMENT_PLAN §43. Pure stdlib — no scipy/numpy.

Runnable as a script for ad-hoc checks:

    python -m paper_forward_test.tracker
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 2026-04-25T09:22:00Z — V6 deploy / paper test start
DEFAULT_START_MS = 1_777_108_920_000

# Match the live config (config.py)
INITIAL_BALANCE = 10_000.0
RISK_PCT = 0.01
SL_PCT = 0.025

# Fee-adjusted break-even WR (IMPROVEMENT_PLAN §2.8)
BREAK_EVEN_WR = 0.415

# Repo paths
_REPO = Path(__file__).resolve().parent.parent
_TRADES_FILE = _REPO / 'data' / 'trades_history.json'


@dataclass
class GateResults:
    sharpe_gte_05:        bool
    wr_ci_lower_gt_42pct: bool
    max_dd_lt_5pct:       bool
    n_trades_gte_100:     bool

    @property
    def met(self) -> int:
        return sum(asdict(self).values())

    @property
    def total(self) -> int:
        return len(asdict(self))


# ── stdlib stats ──────────────────────────────────────────────────────────────

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95 % CI for a binomial proportion. Returns (lo, hi) in [0, 1]."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var) if var > 0 else 0.0


# ── data loading ──────────────────────────────────────────────────────────────

def load_trades(path: Path = _TRADES_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def filter_paper_test(trades: list[dict], start_ms: int) -> list[dict]:
    """Keep only trades that *entered* on or after the paper-test start."""
    return [t for t in trades if t.get('entry_ts', 0) >= start_ms]


# ── metrics ───────────────────────────────────────────────────────────────────

def calculate_metrics(
    trades: list[dict],
    start_ms: int = DEFAULT_START_MS,
    initial_balance: float = INITIAL_BALANCE,
) -> dict[str, Any]:
    """Compute paper-test metrics. Trades MUST already be filtered to the test window."""
    now_ms = int(time.time() * 1000)
    elapsed_seconds = max(0.0, (now_ms - start_ms) / 1000.0)
    days_running = elapsed_seconds / 86_400

    n = len(trades)
    if n == 0:
        return {
            'status':       'no trades yet',
            'days_running': round(days_running, 2),
            'n_trades':     0,
            'gates':        asdict(_empty_gates()),
            'gates_met':    0,
            'gates_total':  4,
            'ready_for_live': False,
            'paper_test_start_ms': start_ms,
        }

    wins = [t for t in trades if t.get('pnl_usdt', 0.0) > 0]
    losses = [t for t in trades if t.get('pnl_usdt', 0.0) <= 0]
    win_rate = len(wins) / n
    avg_win = sum(t['pnl_usdt'] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t['pnl_usdt'] for t in losses) / len(losses) if losses else 0.0

    # Equity curve + max drawdown
    balance = initial_balance
    peak = initial_balance
    max_dd = 0.0
    equity_curve: list[float] = [balance]
    for t in trades:
        balance += t.get('pnl_usdt', 0.0)
        peak = max(peak, balance)
        if peak > 0:
            dd = (peak - balance) / peak
            if dd > max_dd:
                max_dd = dd
        equity_curve.append(balance)

    total_pnl = balance - initial_balance
    total_pnl_pct = total_pnl / initial_balance * 100

    # Per-trade Sharpe → annualized using actual realized trade-rate.
    pct_returns = [t.get('pnl_pct', 0.0) / 100.0 for t in trades]
    mean_r = sum(pct_returns) / n
    std_r = _stdev(pct_returns)
    sharpe_pt = mean_r / std_r if std_r > 0 else 0.0
    trades_per_year = (n / days_running * 365.25) if days_running > 0 else 0.0
    sharpe_annual = sharpe_pt * math.sqrt(trades_per_year) if trades_per_year > 0 else 0.0

    # Profit factor
    gross_wins = sum(t['pnl_usdt'] for t in wins)
    gross_losses = abs(sum(t['pnl_usdt'] for t in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    ci_lo, ci_hi = wilson_ci(len(wins), n)

    gates = GateResults(
        sharpe_gte_05        = sharpe_annual >= 0.5,
        wr_ci_lower_gt_42pct = ci_lo > 0.42,
        max_dd_lt_5pct       = max_dd < 0.05,
        n_trades_gte_100     = n >= 100,
    )

    return {
        'status':              'tracking',
        'days_running':        round(days_running, 2),
        'n_trades':            n,
        'win_rate':            round(win_rate, 4),
        'win_rate_ci':         (round(ci_lo, 4), round(ci_hi, 4)),
        'win_rate_ci_lower':   round(ci_lo, 4),
        'breakeven_wr':        BREAK_EVEN_WR,
        'avg_win':             round(avg_win, 4),
        'avg_loss':            round(avg_loss, 4),
        'profit_factor':       round(profit_factor, 4) if profit_factor != float('inf') else None,
        'total_pnl':           round(total_pnl, 4),
        'total_pnl_pct':       round(total_pnl_pct, 4),
        'max_drawdown':        round(max_dd, 6),
        'max_drawdown_pct':    round(max_dd * 100, 4),
        'sharpe_per_trade':    round(sharpe_pt, 4),
        'sharpe_annual':       round(sharpe_annual, 4),
        'trades_per_year_est': round(trades_per_year, 2),
        'current_balance':     round(balance, 4),
        'initial_balance':     initial_balance,
        'gates':               asdict(gates),
        'gates_met':           gates.met,
        'gates_total':         gates.total,
        'ready_for_live':      gates.met == gates.total,
        'paper_test_start_ms': start_ms,
        'equity_curve':        [round(e, 2) for e in equity_curve],
    }


def _empty_gates() -> GateResults:
    return GateResults(False, False, False, False)


# ── public API for callers ────────────────────────────────────────────────────

def current_metrics(
    start_ms: int | None = None,
    trades_path: Path = _TRADES_FILE,
) -> dict[str, Any]:
    """Single-call helper: load + filter + compute."""
    s = start_ms if start_ms is not None else int(
        os.getenv('PAPER_TEST_START_MS', str(DEFAULT_START_MS))
    )
    all_trades = load_trades(trades_path)
    paper_trades = filter_paper_test(all_trades, s)
    return calculate_metrics(paper_trades, start_ms=s)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _format_summary(m: dict[str, Any]) -> str:
    if m['status'] == 'no trades yet':
        return (
            f"Paper test día {m['days_running']:.1f} de 30 — "
            f"sin trades cerrados todavía"
        )
    g = m['gates']
    lines = [
        f"Paper test día {m['days_running']:.2f}",
        f"  Trades:        {m['n_trades']}",
        f"  Win rate:      {m['win_rate'] * 100:.1f}%  "
        f"(95% CI {m['win_rate_ci'][0] * 100:.1f}%-{m['win_rate_ci'][1] * 100:.1f}%, "
        f"break-even {m['breakeven_wr'] * 100:.1f}%)",
        f"  Avg win:       {m['avg_win']:+.2f}",
        f"  Avg loss:      {m['avg_loss']:+.2f}",
        f"  Profit factor: {m['profit_factor']}",
        f"  Total PnL:     {m['total_pnl']:+.2f} USDT  ({m['total_pnl_pct']:+.3f}%)",
        f"  Max DD:        {m['max_drawdown_pct']:.3f}%",
        f"  Sharpe (trade):{m['sharpe_per_trade']:.3f}",
        f"  Sharpe (annual ~ {m['trades_per_year_est']:.1f}/yr): {m['sharpe_annual']:.3f}",
        f"  Balance:       {m['current_balance']:.2f}",
        f"  Gates:         {m['gates_met']}/{m['gates_total']}",
        f"    Sharpe ≥ 0.5:        {'✅' if g['sharpe_gte_05'] else '❌'}",
        f"    WR CI low > 42%:     {'✅' if g['wr_ci_lower_gt_42pct'] else '❌'}",
        f"    Max DD < 5%:         {'✅' if g['max_dd_lt_5pct'] else '❌'}",
        f"    n ≥ 100 trades:      {'✅' if g['n_trades_gte_100'] else '❌'}",
        f"  Ready for live: {'YES 🟢' if m['ready_for_live'] else 'NO ⏳'}",
    ]
    return '\n'.join(lines)


def main() -> None:
    m = current_metrics()
    print(_format_summary(m))


if __name__ == '__main__':
    main()
