"""Telegram notification helper. No-op when env vars are not configured."""

import logging
import os
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

_NEAR_MISS_COOLDOWN_S = 1800.0
_TRAILING_COOLDOWN_S  = 30.0     # avoid spam when SL ratchets every minute
_last_near_miss_ts: float = 0.0
_last_trailing_ts: float = 0.0

_PAUSE_FILE = Path('data/pause.flag')


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
    """Send a diagnostic Telegram alert when entry conditions are close to firing."""
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


async def notify_trailing(transition: str, close: float, position: dict) -> None:
    """Send a Telegram alert when the trailing stop transitions to a new state."""
    global _last_trailing_ts
    now = time.time()
    if now - _last_trailing_ts < _TRAILING_COOLDOWN_S:
        return
    _last_trailing_ts = now
    side = position.get('side', 'long').upper()
    entry = position.get('entry_price', 0.0)
    sl    = position.get('sl_price', 0.0)
    label = '🔒 BREAKEVEN' if transition == 'breakeven' else '🪜 TRAILING'
    text = (
        f'{label} <b>{side}</b> trailing stop\n'
        f'Entry: ${entry:,.2f}  →  Now: ${close:,.2f}\n'
        f'New SL: ${sl:,.2f}'
    )
    await notify(text)


def is_paused() -> bool:
    return _PAUSE_FILE.exists()


def set_paused(paused: bool) -> None:
    """Toggle the pause flag — checked by the trading loop before entries."""
    _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        _PAUSE_FILE.touch()
    elif _PAUSE_FILE.exists():
        _PAUSE_FILE.unlink()
