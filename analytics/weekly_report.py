"""Weekly Telegram report — fired Mondays 08:00 UTC by main.py."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from analytics.live_db import list_live_trades
from analytics.validation import (
    evaluate, per_condition_analysis, readiness_check, underperforming_buckets,
)
from notifications import notify

logger = logging.getLogger(__name__)


def _fmt_recommendation(eval_: dict, readiness: dict) -> str:
    if readiness['ready']:
        return '✅ <b>Ready for demo trading</b>'
    if any(a['type'] == 'rolling_degradation' for a in eval_['alerts']):
        return '🔴 <b>REVIEW</b>: rolling win rate dropping'
    if any(a['type'] == 'overfitting' for a in eval_['alerts']):
        return '🟠 <b>REVIEW</b>: possible overfitting'
    if any(a['type'] == 'no_trades' for a in eval_['alerts']):
        return '🟡 <b>ADJUST</b>: filters may be too tight'
    if eval_['n_trades'] < 30:
        return '🟢 <b>CONTINUE</b>: gathering more data'
    return '🟢 <b>CONTINUE</b>'


def _days_running() -> int:
    db = Path('data/live_trades.db')
    if not db.exists():
        return 0
    age = (datetime.now(timezone.utc).timestamp() - db.stat().st_mtime) / 86_400
    return max(1, int(age))


async def send_weekly_report() -> None:
    trades = list_live_trades()
    days = _days_running()
    eval_ = evaluate(trades, days)
    conditions = per_condition_analysis(trades)
    weak = underperforming_buckets(conditions)
    readiness = readiness_check(trades, days)

    week_trades = [
        t for t in trades
        if (datetime.now(timezone.utc).timestamp() * 1000 - int(t['exit_ts_ms'])) <= 7 * 86_400_000
    ]
    n_week = len(week_trades)
    week_pnl = sum(float(t['pnl_usdt']) for t in week_trades)
    week_wins = sum(1 for t in week_trades if t['result'] == 'WIN')
    week_wr = week_wins / n_week * 100 if n_week else 0.0

    best = max(week_trades, key=lambda t: float(t['pnl_usdt'])) if week_trades else None
    worst = min(week_trades, key=lambda t: float(t['pnl_usdt'])) if week_trades else None

    lines = [
        '📅 <b>Weekly report (Monday 08:00 UTC)</b>',
        f'Trades this week: {n_week} ({week_wins}W / {n_week - week_wins}L = {week_wr:.1f}%)',
        f'Week PnL: <b>${week_pnl:+,.2f}</b>',
        f'Total trades: {eval_["n_trades"]} '
        f'(WR {eval_["win_rate_pct"]}% vs backtest {eval_["baseline"]["win_rate_pct"]}%)',
        f'Total PnL: ${eval_["pnl_usdt"]:+,.2f} (expected ${eval_["expected_pnl_usdt"]:+,.2f})',
        f'Rolling WR (last 10): '
        f'{eval_["rolling_win_rate"] if eval_["rolling_win_rate"] is not None else "n/a"}',
        f'Max drawdown: {eval_["max_drawdown_pct"]}%',
    ]
    if best:
        lines.append(
            f'🏆 Best: {best["side"]} '
            f'${best["pnl_usdt"]:+.2f} ({best["session"]}, {best.get("regime", "n/a")})'
        )
    if worst:
        lines.append(
            f'💩 Worst: {worst["side"]} '
            f'${worst["pnl_usdt"]:+.2f} ({worst["session"]}, {worst.get("regime", "n/a")})'
        )
    if weak:
        lines.append('⚠ Underperforming buckets:')
        for w in weak[:4]:
            lines.append(f'  • {w["category"]}/{w["label"]}: '
                         f'WR {w["win_rate_pct"]}% (n={w["n"]})')
    if eval_['alerts']:
        lines.append('🚨 Alerts:')
        for a in eval_['alerts']:
            lines.append(f'  • [{a["level"]}] {a["message"]}')
    lines.append(f'Recommendation: {_fmt_recommendation(eval_, readiness)}')
    if eval_['remaining_for_validation'] > 0:
        lines.append(f'⏳ {eval_["remaining_for_validation"]} more trades '
                     'to reach statistical validation (30 minimum)')
    await notify('\n'.join(lines))


_READINESS_ALERT_FLAG = Path('data/readiness_alerted.flag')


async def maybe_alert_ready() -> None:
    """Fire a one-time alert when the bot first meets all readiness criteria."""
    if _READINESS_ALERT_FLAG.exists():
        return
    trades = list_live_trades()
    days = _days_running()
    rep = readiness_check(trades, days)
    if not rep['ready']:
        return
    _READINESS_ALERT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _READINESS_ALERT_FLAG.touch()
    eval_ = rep['metrics']
    lines = [
        '🚀 <b>Sistema listo para demo trading</b>',
        f'Trades: {eval_["n_trades"]} (WR {eval_["win_rate_pct"]}%)',
        f'PnL: ${eval_["pnl_usdt"]:+.2f} | Max DD: {eval_["max_drawdown_pct"]}%',
        'Validación completa:',
    ]
    for k, v in rep['checks'].items():
        lines.append(f'  ✅ {k}' if v else f'  ❌ {k}')
    lines.append('Set BINANCE_MODE=demo and restart to switch.')
    await notify('\n'.join(lines))
