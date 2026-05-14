"""Weekly checkpoint for the paper forward-test.

Invoked from host cron every Monday 09:00 UTC. Saves a JSON snapshot to
`paper_forward_test/snapshots/week_N.json` and sends a more detailed
Telegram report than the daily one, including an equity-curve sketch and
gate-violation flags.

    # cron entry (Mondays 09:00 UTC)
    0 9 * * 1 cd /root/trading-bot && venv/bin/python -m paper_forward_test.weekly_checkpoint
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notifications import notify
from paper_forward_test.tracker import current_metrics

_SNAPSHOTS_DIR = Path(__file__).resolve().parent / 'snapshots'
_TEST_START_MS = 1_777_108_920_000   # 2026-04-25T09:22:00Z


def _week_number(now_ms: int) -> int:
    elapsed_days = (now_ms - _TEST_START_MS) / 86_400_000
    return max(1, math.ceil(elapsed_days / 7))


def _save_snapshot(metrics: dict, week: int) -> Path:
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _SNAPSHOTS_DIR / f'week_{week:02d}.json'
    payload = {
        'week':         week,
        'recorded_at':  datetime.now(timezone.utc).isoformat(),
        'metrics':      metrics,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def _equity_sparkline(equity: list[float]) -> str:
    """Tiny ASCII bar chart of the equity curve."""
    if len(equity) < 2:
        return '(insufficient data)'
    blocks = '▁▂▃▄▅▆▇█'
    lo, hi = min(equity), max(equity)
    span = hi - lo
    if span == 0:
        return '─' * min(len(equity), 24)
    sample = equity if len(equity) <= 24 else [equity[i] for i in
        (round(j * (len(equity) - 1) / 23) for j in range(24))]
    return ''.join(blocks[min(7, int((v - lo) / span * 7))] for v in sample)


def _gate_violations(m: dict) -> list[str]:
    """Return human-readable list of gate failures (only relevant once we have data)."""
    if m['status'] == 'no trades yet':
        return []
    issues = []
    g = m['gates']
    if not g['max_dd_lt_5pct']:
        issues.append(f"Max DD {m['max_drawdown_pct']:.2f}% ≥ 5% — risk-control breach")
    if m['n_trades'] >= 10 and not g['wr_ci_lower_gt_42pct']:
        ci_lo = m['win_rate_ci'][0] * 100
        issues.append(f"WR CI lower bound {ci_lo:.1f}% ≤ 42% — edge not significant")
    if m['n_trades'] >= 20 and not g['sharpe_gte_05']:
        issues.append(f"Sharpe (annual) {m['sharpe_annual']:.2f} < 0.5")
    return issues


def _format_message(m: dict, week: int) -> str:
    day = math.floor(m['days_running'])
    if m['status'] == 'no trades yet':
        return (
            f'📊 <b>Weekly Checkpoint — Semana {week}</b>\n'
            f'Día {day} de 30, 0 trades cerrados.\n\n'
            f'El bot está activo con la config V6 (RSI&lt;40 + SMA20 + ADX&lt;45). '
            f'A ~8 trades/año en BTC esto es esperable durante la primera semana.'
        )

    g = m['gates']
    gate_table = '\n'.join([
        f"  {'✅' if g['sharpe_gte_05'] else '❌'} Sharpe (anual) ≥ 0.5  → {m['sharpe_annual']:.3f}",
        f"  {'✅' if g['wr_ci_lower_gt_42pct'] else '❌'} WR CI low &gt; 42%       → {m['win_rate_ci'][0] * 100:.1f}%",
        f"  {'✅' if g['max_dd_lt_5pct'] else '❌'} Max DD &lt; 5%           → {m['max_drawdown_pct']:.2f}%",
        f"  {'✅' if g['n_trades_gte_100'] else '❌'} n ≥ 100 trades         → {m['n_trades']}",
    ])

    issues = _gate_violations(m)
    flag_block = (
        '\n⚠️ <b>Flags</b>\n' + '\n'.join(f'  • {i}' for i in issues)
    ) if issues else ''

    spark = _equity_sparkline(m.get('equity_curve', []))
    pf = m['profit_factor']
    pf_str = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
    verdict = '🟢 READY FOR LIVE' if m['ready_for_live'] else '⏳ Continue paper test'

    return (
        f'📊 <b>Weekly Checkpoint — Semana {week}</b>\n'
        f'📅 Día {day} de 30 | {m["n_trades"]} trades\n\n'
        f'<b>Métricas</b>\n'
        f'  • Win Rate: {m["win_rate"] * 100:.1f}% '
        f'(CI {m["win_rate_ci"][0] * 100:.0f}–{m["win_rate_ci"][1] * 100:.0f}%)\n'
        f'  • PnL: {m["total_pnl"]:+.2f} USDT ({m["total_pnl_pct"]:+.3f}%)\n'
        f'  • Max DD: {m["max_drawdown_pct"]:.2f}%\n'
        f'  • Sharpe (anual / per-trade): {m["sharpe_annual"]:.2f} / {m["sharpe_per_trade"]:.2f}\n'
        f'  • Profit factor: {pf_str}\n'
        f'  • Avg win / loss: {m["avg_win"]:+.2f} / {m["avg_loss"]:+.2f}\n'
        f'  • Trades / año (proyección): {m["trades_per_year_est"]:.1f}\n\n'
        f'<b>Equity</b> <code>{spark}</code>\n\n'
        f'<b>Gates {m["gates_met"]}/{m["gates_total"]}</b>\n{gate_table}'
        f'{flag_block}\n\n'
        f'{verdict}'
    )


async def _run() -> None:
    import time as _time
    m = current_metrics()
    week = _week_number(int(_time.time() * 1000))
    path = _save_snapshot(m, week)
    msg = _format_message(m, week)
    await notify(msg)
    print(f'snapshot_saved path={path}')
    print(msg)


def main() -> None:
    asyncio.run(_run())


if __name__ == '__main__':
    main()
