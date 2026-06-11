"""Daily Telegram report for the paper forward-test.

Invoked from host cron at 09:00 UTC. Computes current metrics and sends a
single Telegram message via the project's existing notifications.notify().

    # cron entry
    0 9 * * * cd /root/trading-bot && venv/bin/python -m paper_forward_test.daily_report
"""
from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

# Ensure project root is on sys.path so notifications + paper_forward_test resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notifications import notify
from paper_forward_test.tracker import current_metrics

_PAPER_TEST_DURATION_DAYS = 30


def _format_message(m: dict) -> str:
    day = math.floor(m['days_running'])
    if m['status'] == 'no trades yet':
        return (
            f'🤖 <b>Trading Bot — Paper Test Update</b>\n'
            f'📅 Día {day} de {_PAPER_TEST_DURATION_DAYS} | 0 trades cerrados\n\n'
            f'Sin trades cerrados todavía. Bot ticking, filtros activos, esperando señal.\n\n'
            f'⏳ Continuar paper test'
        )

    g = m['gates']
    gate_lines = '\n'.join([
        f"  {'✅' if g['sharpe_gte_05'] else '❌'} Sharpe ≥ 0.5",
        f"  {'✅' if g['wr_ci_lower_gt_42pct'] else '❌'} WR CI lower &gt; 42%",
        f"  {'✅' if g['max_dd_lt_5pct'] else '❌'} Max DD &lt; 5%",
        f"  {'✅' if g['n_trades_gte_100'] else '❌'} n ≥ 100 trades",
    ])

    pf = m['profit_factor']
    pf_str = f'{pf:.2f}' if isinstance(pf, (int, float)) else '∞'
    ci_lo, ci_hi = m['win_rate_ci']
    verdict = '🟢 READY FOR LIVE' if m['ready_for_live'] else '⏳ Continuar paper test'

    return (
        f'🤖 <b>Trading Bot — Paper Test Update</b>\n'
        f'📅 Día {day} de {_PAPER_TEST_DURATION_DAYS} | {m["n_trades"]} trades\n\n'
        f'📊 <b>Métricas actuales</b>\n'
        f'  • Win Rate: {m["win_rate"] * 100:.1f}% '
        f'(CI {ci_lo * 100:.0f}–{ci_hi * 100:.0f}%, BE {m["breakeven_wr"] * 100:.1f}%)\n'
        f'  • PnL total: {m["total_pnl"]:+.2f} USDT ({m["total_pnl_pct"]:+.3f}%)\n'
        f'  • Max Drawdown: {m["max_drawdown_pct"]:.2f}%\n'
        f'  • Sharpe (annual): {m["sharpe_annual"]:.3f}\n'
        f'  • Profit factor: {pf_str}\n'
        f'  • Avg win / loss: {m["avg_win"]:+.2f} / {m["avg_loss"]:+.2f}\n\n'
        f'<b>Gates {m["gates_met"]}/{m["gates_total"]}</b>\n{gate_lines}\n\n'
        f'{verdict}'
    )


async def _send() -> None:
    m = current_metrics()
    msg = _format_message(m)
    await notify(msg)
    # Echo to stdout so cron logs are useful
    print(msg)


def main() -> None:
    asyncio.run(_send())


if __name__ == '__main__':
    main()
