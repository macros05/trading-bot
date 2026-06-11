"""Short-term conversation memory for the Telegram AI chat.

Persists the last `MAX_EXCHANGES` exchanges (user message + bot answer) in
``data/ai_chat_history.json`` so Gemini gets conversational context across
messages. Writes go through the repo's bind-mount-safe atomic write helper
(`core.loop._atomic_or_direct_write`) so a crash mid-write can never corrupt
the file. Memory failures are logged and swallowed — chat must keep working
even if the history file is unwritable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.loop import _atomic_or_direct_write

logger = logging.getLogger(__name__)

HISTORY_FILE = Path("data/ai_chat_history.json")
MAX_EXCHANGES = 8


def load_exchanges() -> list[dict]:
    """Stored exchanges, oldest first: [{"ts", "user", "assistant"}, ...]."""
    try:
        payload = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("ai chat history unreadable, starting fresh (%s)", HISTORY_FILE)
        return []
    if not isinstance(payload, list):
        return []
    return [e for e in payload if isinstance(e, dict)][-MAX_EXCHANGES:]


def save_exchange(user_text: str, assistant_text: str) -> None:
    """Append one user/assistant exchange and trim oldest beyond the window."""
    exchanges = load_exchanges()
    exchanges.append({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": user_text,
        "assistant": assistant_text,
    })
    exchanges = exchanges[-MAX_EXCHANGES:]
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_or_direct_write(
            HISTORY_FILE, json.dumps(exchanges, ensure_ascii=False, indent=2)
        )
    except OSError:
        logger.exception("ai chat history save failed (%s)", HISTORY_FILE)
