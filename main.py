import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import BOT_CONFIG
from core.loop import trading_loop
from core.state import StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from notifications import notify
from risk.manager import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)

_HEALTH_FILE    = Path('data/bot_health.json')
_STALE_SECONDS  = 300.0   # 5 minutes without a tick triggers watchdog restart


async def _watchdog(get_loop_task) -> None:
    """Alert and cancel the trading loop if no tick arrives for >_STALE_SECONDS."""
    while True:
        await asyncio.sleep(60)
        try:
            data = json.loads(_HEALTH_FILE.read_text())
            age = (time.time() * 1000 - data['last_tick_ms']) / 1000
            if age > _STALE_SECONDS:
                task = get_loop_task()
                if task and not task.done():
                    msg = (
                        f'⚠️ <b>BOT STALE</b>\n'
                        f'No tick for {age:.0f}s — restarting WebSocket connection.'
                    )
                    logger.critical('watchdog_stale age=%.0fs restarting_loop', age)
                    await notify(msg)
                    task.cancel()
        except (OSError, json.JSONDecodeError, KeyError):
            pass


async def _midnight_reset(risk_manager: RiskManager, state_manager: StateManager) -> None:
    """Reset the daily PnL counter at midnight UTC so the circuit breaker clears each day."""
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0,
        )
        await asyncio.sleep((next_midnight - now).total_seconds())
        risk_manager.reset_daily()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        state_manager.set_daily_pnl(0.0, today)
        logger.info('midnight_daily_pnl_reset')
        await notify('🕛 <b>Daily reset</b>\nCircuit breaker cleared. New trading day started.')


def _next_run_at(hour: int, minute: int, weekday: int | None = None) -> float:
    """Seconds from now until the next UTC fire-time at hour:minute.

    weekday: 0=Mon .. 6=Sun. None = any day.
    """
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    if weekday is not None:
        days_ahead = (weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
    return (candidate - now).total_seconds()


async def _paper_test_daily_report() -> None:
    """Fire the paper-test daily Telegram report at 09:00 UTC every day."""
    while True:
        await asyncio.sleep(_next_run_at(9, 0))
        try:
            from paper_forward_test.daily_report import _send
            await _send()
            logger.info('paper_daily_report_sent')
        except Exception as exc:
            logger.warning('paper_daily_report_failed error=%s', exc)


async def _paper_test_weekly_checkpoint() -> None:
    """Fire the weekly checkpoint Mondays 09:05 UTC (5 min after the daily)."""
    while True:
        await asyncio.sleep(_next_run_at(9, 5, weekday=0))
        try:
            from paper_forward_test.weekly_checkpoint import _run
            await _run()
            logger.info('paper_weekly_checkpoint_sent')
        except Exception as exc:
            logger.warning('paper_weekly_checkpoint_failed error=%s', exc)


async def _daily_summary_22utc() -> None:
    """Send the day-end summary every day at 22:00 UTC."""
    while True:
        await asyncio.sleep(_next_run_at(22, 0))
        try:
            from telegram_commands import send_daily_summary
            await send_daily_summary()
            logger.info('daily_summary_sent')
        except Exception as exc:
            logger.warning('daily_summary_failed error=%s', exc)


async def _weekly_report_monday_8utc() -> None:
    """Send the live-vs-backtest weekly report Mondays 08:00 UTC."""
    while True:
        await asyncio.sleep(_next_run_at(8, 0, weekday=0))
        try:
            from analytics.weekly_report import send_weekly_report
            await send_weekly_report()
            logger.info('weekly_report_sent')
        except Exception as exc:
            logger.warning('weekly_report_failed error=%s', exc)


async def _readiness_check_loop() -> None:
    """Check demo-trading readiness every 6 hours; fire one-time alert."""
    while True:
        try:
            from analytics.weekly_report import maybe_alert_ready
            await maybe_alert_ready()
        except Exception as exc:
            logger.debug('readiness_check_failed error=%s', exc)
        await asyncio.sleep(6 * 3600)


async def _send_startup_notification() -> None:
    """One-time confirmation that paper monitoring is armed."""
    try:
        from paper_forward_test.tracker import current_metrics
        m = current_metrics()
        days = max(0, int(m['days_running']))
        await notify(
            f'🤖 <b>Paper forward-test monitoring active — día {days} de 30</b>\n'
            f'Strategy: RSI&lt;40 + close&gt;SMA20 + ADX&lt;45\n'
            f'Reports: daily 09:00 UTC, weekly Mondays 09:05 UTC\n'
            f'Trades so far: {m["n_trades"]}'
        )
    except Exception as exc:
        logger.warning('startup_notification_failed error=%s', exc)


async def main() -> None:
    from analytics.live_db import init_db
    init_db()
    client = BinanceClient(
        leverage=BOT_CONFIG.get('leverage', 1),
        symbol=BOT_CONFIG.get('symbol', 'BTC/USDT'),
    )
    buffer = CandleBuffer(maxlen=200)
    state_manager = StateManager()

    # Restore daily PnL from disk so the circuit breaker persists across process restarts.
    daily_pnl, daily_date = state_manager.get_daily_pnl()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    initial_pnl = daily_pnl if daily_date == today else 0.0
    if initial_pnl != 0.0:
        logger.info('restored daily_pnl=%.4f from previous session', initial_pnl)

    risk_manager = RiskManager(
        max_daily_drawdown=BOT_CONFIG.get('circuit_breaker_pct', 0.03),
        initial_daily_pnl=initial_pnl,
        leverage=BOT_CONFIG.get('leverage', 1),
        base_risk_pct=BOT_CONFIG.get('risk_pct', 0.02),
        adaptive_kelly=BOT_CONFIG.get('adaptive_kelly', True),
    )

    from risk.protections import CooldownPeriod, ProtectionStack, StoplossGuard
    protections = ProtectionStack([
        CooldownPeriod(cooldown_seconds=BOT_CONFIG.get('cooldown_seconds', 0)),
        StoplossGuard(
            max_sl=BOT_CONFIG.get('max_sl_per_day', 10),
            lookback_seconds=86_400,
        ),
    ])
    logger.info('protections active count=%d', 2)

    logger.info('bot starting config=%s', BOT_CONFIG)

    loop_task: asyncio.Task | None = None
    restart_requested = False

    def _get_loop_task() -> asyncio.Task | None:
        return loop_task

    wd_task          = asyncio.create_task(_watchdog(_get_loop_task))
    mid_task         = asyncio.create_task(_midnight_reset(risk_manager, state_manager))
    daily_task       = asyncio.create_task(_paper_test_daily_report())
    weekly_task      = asyncio.create_task(_paper_test_weekly_checkpoint())
    daily_sum_task   = asyncio.create_task(_daily_summary_22utc())
    weekly_rpt_task  = asyncio.create_task(_weekly_report_monday_8utc())
    readiness_task   = asyncio.create_task(_readiness_check_loop())
    asyncio.create_task(_send_startup_notification())

    try:
        while True:
            restart_requested = False
            loop_task = asyncio.create_task(
                trading_loop(client, buffer, state_manager, risk_manager, BOT_CONFIG,
                             protections=protections)
            )
            try:
                await loop_task
                break  # clean exit (e.g. CancelledError from outside the while)
            except asyncio.CancelledError:
                # Watchdog cancels loop_task → restart_requested will be set by the watchdog
                # before the cancel; any other CancelledError re-raises to stop the bot.
                if loop_task.cancelled():
                    logger.info('bot_loop_restarted by watchdog')
                    await asyncio.sleep(5)
                    continue
                raise
            except Exception as exc:
                logger.error('bot_loop_error error=%s restarting_in_10s', exc)
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        logger.info('bot stopped')
    finally:
        wd_task.cancel()
        mid_task.cancel()
        daily_task.cancel()
        weekly_task.cancel()
        daily_sum_task.cancel()
        weekly_rpt_task.cancel()
        readiness_task.cancel()
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
