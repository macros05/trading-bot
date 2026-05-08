"""Telegram notification helper. No-op when env vars are not configured."""

import logging
import os
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

_NEAR_MISS_COOLDOWN_S = 1800.0   # 30 min between near-miss alerts
_last_near_miss_ts: float = 0.0


async def notify(text: str) -> None:
    """Send a Telegram message. Silently does nothing if token/chat not configured."""
    if not _TOKEN or not _CHAT_ID:
        return
    url = f'https://api.telegram.org/bot{_TOKEN}/sendMessage'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={'chat_id': _CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning('telegram_error status=%d', resp.status)
    except Exception as exc:
        logger.warning('telegram_send_failed error=%s', exc)


async def notify_near_miss(reason: str, close: float, rsi: float, sma: float) -> None:
    """Send a diagnostic Telegram alert when entry conditions are close to firing.

    Rate-limited to one message per _NEAR_MISS_COOLDOWN_S seconds to avoid spam
    when the bot sits in a near-miss band for many consecutive ticks.
    """
    global _last_near_miss_ts
    now = time.time()
    if now - _last_near_miss_ts < _NEAR_MISS_COOLDOWN_S:
        return
    _last_near_miss_ts = now
    text = (
        f'🔎 <b>Near-miss signal</b>\n'
        f'{reason}\n'
        f'close=${close:,.2f} sma20=${sma:,.2f} rsi14={rsi:.1f}'
    )
    await notify(text)
