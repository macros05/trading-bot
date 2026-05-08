"""Telegram command handlers — invoked by personal_assistant Telegram webhook.

Each handler returns a string ready to send with notify(). Pure-ish: reads
JSON files but does not mutate any state except for set_paused.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from analytics.metrics import compute_performance
from notifications import is_paused, notify, set_paused
from strategy.sessions import session_for_ts

logger = logging.getLogger(__name__)

_DATA_DIR    = Path('data')
_TRADES_FILE = _DATA_DIR / 'trades_history.json'
_STATE_FILE  = _DATA_DIR / 'bot_state.json'
_HEALTH_FILE = _DATA_DIR / 'bot_health.json'
_INITIAL_BALANCE = 10_000.0


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _format_pause_status(paused: bool) -> str:
    return '⏸ <b>PAUSED</b>' if paused else '▶ <b>RUNNING</b>'


def cmd_stats() -> str:
    trades = _load_json(_TRADES_FILE, [])
    perf = compute_performance(trades, _INITIAL_BALANCE)
    lines = [
        f'📊 <b>Bot stats</b>',
        f'Status: {_format_pause_status(is_paused())}',
        f'Trades: {perf["total_trades"]} '
        f'({perf["wins"]}W / {perf["losses"]}L = {perf["win_rate_pct"]}%)',
        f'Total PnL: <b>${perf["total_pnl_usdt"]:+,.2f}</b>',
        f'Balance: ${perf["final_balance"]:,.2f}',
        f'Max DD: {perf["max_drawdown"]["pct"]:.2f}% (${perf["max_drawdown"]["usdt"]:,.2f})',
        f'PnL/DD ratio: {perf["pnl_dd_ratio"]:.2f}',
    ]
    side = perf['by_side']
    lines.append(
        f'Long: {side["long"]["trades"]} '
        f'({side["long"]["win_rate_pct"]}% wr, '
        f'${side["long"]["total_pnl"]:+,.2f})'
    )
    lines.append(
        f'Short: {side["short"]["trades"]} '
        f'({side["short"]["win_rate_pct"]}% wr, '
        f'${side["short"]["total_pnl"]:+,.2f})'
    )
    sess = perf['by_session']
    if sess:
        for label, data in sess.items():
            lines.append(
                f'{label.upper()}: {data["trades"]} trades, '
                f'{data["win_rate_pct"]}% wr, ${data["total_pnl"]:+,.2f}'
            )
    return '\n'.join(lines)


def cmd_pause() -> str:
    set_paused(True)
    return '⏸ <b>Bot paused</b>\nNo new entries until /activar.'


def cmd_resume() -> str:
    set_paused(False)
    return '▶ <b>Bot resumed</b>\nEntries enabled.'


_COMMANDS = {
    '/stats':    cmd_stats,
    '/pausar':   cmd_pause,
    '/pause':    cmd_pause,
    '/activar':  cmd_resume,
    '/resume':   cmd_resume,
}


def handle_command(text: str) -> str | None:
    """Dispatch a Telegram message to a command handler. Returns None if not a command."""
    if not text:
        return None
    cmd = text.strip().split()[0].lower()
    handler = _COMMANDS.get(cmd)
    if handler is None:
        return None
    try:
        return handler()
    except Exception as exc:
        logger.exception('telegram_command_failed cmd=%s error=%s', cmd, exc)
        return f'❌ Error: {exc}'


def _today_trades() -> list[dict]:
    trades = _load_json(_TRADES_FILE, [])
    today_midnight_ms = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp() * 1000)
    return [t for t in trades if int(t.get('exit_ts', 0)) >= today_midnight_ms]


async def send_daily_summary() -> None:
    """Compose and send the 22:00 UTC daily report."""
    todays = _today_trades()
    state = _load_json(_STATE_FILE, {})
    health = _load_json(_HEALTH_FILE, {})
    open_pos = state.get('position')

    lines = ['🌙 <b>Daily summary (22:00 UTC)</b>']
    if not todays:
        lines.append('No trades today.')
    else:
        wins = sum(1 for t in todays if t.get('result') == 'WIN')
        pnl = sum(float(t.get('pnl_usdt', 0)) for t in todays)
        lines.append(
            f'Trades: {len(todays)} ({wins}W / {len(todays)-wins}L = '
            f'{wins/len(todays)*100:.1f}% wr)'
        )
        lines.append(f'Day PnL: <b>${pnl:+,.2f}</b>')
        sides = {'long': 0, 'short': 0}
        for t in todays:
            sides[t.get('side', 'long')] += 1
        lines.append(f'Long/Short: {sides["long"]} / {sides["short"]}')
    if open_pos:
        side = open_pos.get('side', 'long').upper()
        entry = open_pos.get('entry_price', 0.0)
        last_close = health.get('last_close', entry)
        if open_pos.get('side', 'long') == 'long':
            unreal_pct = (last_close - entry) / entry * 100
        else:
            unreal_pct = (entry - last_close) / entry * 100
        lines.append(
            f'Open: {side} @ ${entry:,.2f} (now ${last_close:,.2f}, '
            f'{unreal_pct:+.2f}% unrealized)'
        )
    else:
        lines.append('No open position.')
    lines.append(f'Pause flag: {_format_pause_status(is_paused())}')
    await notify('\n'.join(lines))
