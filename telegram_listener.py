"""Telegram listener for the trading bot.

Long-polling Telegram bot that:
- Wires the existing /stats /pausar /activar commands so the user actually
  gets a reply when they send those in Telegram (previously these were
  defined in telegram_commands.py but never reachable — there was no
  inbound listener at all).
- Answers free-form questions in Spanish using Gemini, with a snapshot of
  the bot's current state injected into the prompt: open position,
  daily PnL, latest trades, watchdog health, recent log lines.

Runs as an asyncio task started from main.py.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "")
_ALLOWED_CHAT_ID = int(_CHAT_ID_RAW) if _CHAT_ID_RAW.isdigit() else None
_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_DATA_DIR = Path("data")
_STATE_FILE = _DATA_DIR / "bot_state.json"
_TRADES_FILE = _DATA_DIR / "trades_history.json"
_HEALTH_FILE = _DATA_DIR / "bot_health.json"
_BOT_LOG = Path("bot.log")
_MAX_TG_LEN = 4000

_SYSTEM_PROMPT = """Eres el asistente del **Trading Bot de Marcos** (BTC/USDT futuros en Binance).

Estrategia activa: RSI<40 + close>SMA20 + ADX<45. Modo paper / live según
configuración. Hay un circuit breaker que pausa el bot si la pérdida diaria
supera el 3%.

Tu trabajo: responder en español preguntas de Marcos sobre el estado del bot
— posición abierta, PnL del día, últimas operaciones, salud del WebSocket,
indicadores recientes. Datos en el bloque `# Estado actual`.

Reglas:
- Responde en **español** y conciso. 1-3 frases si la respuesta cabe ahí.
- No inventes números — si falta un dato, dilo.
- No hagas recomendaciones de trading ni operativa nueva — sólo informa de
  lo que muestra el estado actual.
- Si la pregunta no es sobre este bot, redirige a Marcos a macrosAssistant.
"""


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _tail_log(n: int = 30) -> list[str]:
    if not _BOT_LOG.exists():
        return []
    try:
        with _BOT_LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk, 0)
            data = f.read().decode("utf-8", errors="replace")
        return list(deque(data.splitlines(), maxlen=n))
    except OSError:
        return []


def _build_state_snapshot() -> str:
    state = _read_json(_STATE_FILE, {})
    health = _read_json(_HEALTH_FILE, {})
    trades = _read_json(_TRADES_FILE, [])
    last_trades = trades[-5:] if isinstance(trades, list) else []
    summary = {
        "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bot_state": state.get("state"),
        "open_position": state.get("position"),
        "daily_pnl_usdt": state.get("daily_pnl"),
        "daily_pnl_date": state.get("daily_pnl_date"),
        "paused": Path("data/pause.flag").exists(),
        "health": {
            "last_tick_iso": health.get("last_tick_iso"),
            "last_close": health.get("last_close"),
            "rsi14": health.get("rsi"),
            "sma20": health.get("sma20"),
            "state": health.get("state"),
            "daily_pnl_pct": health.get("daily_pnl_pct"),
        },
        "trades_total": len(trades) if isinstance(trades, list) else 0,
        "last_5_trades": last_trades,
        "log_tail": _tail_log(20),
    }
    return "# Estado actual\n" + json.dumps(summary, indent=2, ensure_ascii=False, default=str)


async def _ai_answer(user_message: str) -> str:
    if not _GEMINI_KEY:
        return "⚠️ Gemini no configurado (falta GEMINI_API_KEY en .env)."
    snapshot = _build_state_snapshot()
    client = genai.Client(api_key=_GEMINI_KEY)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.6,
    )
    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=f"{snapshot}\n\n# Pregunta\n{user_message}")],
        ),
    ]
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=config,
        )
        return (response.text or "").strip() or "(Gemini devolvió respuesta vacía)"
    except Exception as exc:
        logger.exception("ai_answer failed")
        return f"⚠️ Error AI: {exc}"


def _is_authorized(update: Update) -> bool:
    if _ALLOWED_CHAT_ID is None:
        return True
    chat = update.effective_chat
    return chat is not None and chat.id == _ALLOWED_CHAT_ID


async def _cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.effective_message.reply_text(
        "🤖 Trading Bot. Comandos:\n"
        "/stats /pausar /activar\n"
        "O escríbeme una pregunta en lenguaje natural."
    )


async def _cmd_stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from telegram_commands import cmd_stats
    await update.effective_message.reply_html(cmd_stats())


async def _cmd_pause(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from telegram_commands import cmd_pause
    await update.effective_message.reply_html(cmd_pause())


async def _cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from telegram_commands import cmd_resume
    await update.effective_message.reply_html(cmd_resume())


async def _on_free_form(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    logger.info("trading_ai received: %r", msg.text[:80])
    try:
        await msg.chat.send_action("typing")
    except Exception:
        pass
    reply = await _ai_answer(msg.text.strip())
    for i in range(0, max(1, len(reply)), _MAX_TG_LEN):
        await msg.reply_text(reply[i:i + _MAX_TG_LEN])


async def run() -> None:
    """Long-polling listener. Returns when the bot stops."""
    if not _TOKEN:
        logger.warning("trading telegram listener disabled (no TELEGRAM_BOT_TOKEN)")
        return
    app = Application.builder().token(_TOKEN).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("stats", _cmd_stats))
    app.add_handler(CommandHandler(["pausar", "pause"], _cmd_pause))
    app.add_handler(CommandHandler(["activar", "resume"], _cmd_resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_free_form))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("trading telegram listener started (long polling)")
    try:
        # Block forever — the task is cancelled on shutdown.
        import asyncio
        await asyncio.Event().wait()
    finally:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        logger.info("trading telegram listener stopped")
