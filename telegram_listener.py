"""Telegram listener for the trading bot.

Long-polling Telegram bot that:
- Wires the existing /stats /pausar /activar commands so the user actually
  gets a reply when they send those in Telegram (previously these were
  defined in telegram_commands.py but never reachable — there was no
  inbound listener at all).
- Answers free-form questions in Spanish using Gemini with native function
  calling: a set of strictly read-only tools (telegram_ai_tools.py) lets the
  model pull trade history, daily PnL, live config, logs and the backtest
  verdict on demand, with short-term conversation memory (last 8 exchanges,
  telegram_chat_memory.py → data/ai_chat_history.json).

Runs as an asyncio task started from main.py.
"""

from __future__ import annotations

import asyncio
import logging
import os

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

import telegram_ai_tools
import telegram_chat_memory

logger = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "")
_ALLOWED_CHAT_ID = int(_CHAT_ID_RAW) if _CHAT_ID_RAW.isdigit() else None
_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_MAX_TG_LEN = 4000
_MAX_TOOL_ROUNDS = 4
_AI_UNAVAILABLE_MSG = "⚠️ IA temporalmente no disponible, prueba en unos segundos."

_SYSTEM_PROMPT = """Eres el asistente del **Trading Bot de Marcos** (BTC/USDT futuros en Binance, \
estrategia RSI mean-reversion con filtros, circuit breaker de pérdida diaria).

Tienes HERRAMIENTAS de solo lectura. LLÁMALAS siempre que la pregunta toque datos, \
estado, historial o configuración — nunca respondas de memoria ni digas "no tengo \
acceso" sin haber probado la herramienta adecuada:
- get_status → estado actual, posición abierta + PnL no realizado, pausa, último tick.
- get_trades → historial de trades cerrados (ventana en días, filtro WIN/LOSS) con \
win rate y PnL neto.
- get_daily_pnl → PnL realizado agregado por día con win rate diario.
- get_strategy_config → parámetros vivos (umbral RSI, SL/TP, trailing, filtros, \
circuit breaker, sesiones); explica "por qué entró / no entró".
- get_log_tail → últimas líneas de bot.log (con grep opcional).
- get_backtest_verdict → veredicto del estudio walk-forward (por qué está pausado / si hay edge).

Reglas:
- Responde SIEMPRE en español y conciso (1-4 frases si caben). Nada de Markdown pesado.
- NUNCA inventes números: si una herramienta no devuelve el dato o viene vacía, \
dilo claramente en vez de estimar.
- Solo informas: no recomiendas operaciones nuevas ni puedes ejecutar órdenes, \
pausar o modificar nada.
- Si la pregunta NO trata de este trading bot, responde en UNA sola línea: que para \
eso está @macrosAssistant, y que aquí puedes responder p. ej. "¿cuánto llevo este \
mes?", "¿por qué está pausado el bot?" o "¿qué dice el último backtest?".
"""


def _extract_function_calls(candidate: types.Candidate) -> list[types.Part]:
    if candidate.content is None or not candidate.content.parts:
        return []
    return [p for p in candidate.content.parts if p.function_call is not None]


def _extract_text(candidate: types.Candidate) -> str:
    if candidate.content is None or not candidate.content.parts:
        return ""
    return "".join(p.text for p in candidate.content.parts if p.text)


def _build_contents(user_message: str) -> list[types.Content]:
    contents: list[types.Content] = []
    for exchange in telegram_chat_memory.load_exchanges():
        contents.append(types.Content(
            role="user", parts=[types.Part(text=exchange.get("user", ""))]))
        contents.append(types.Content(
            role="model", parts=[types.Part(text=exchange.get("assistant", ""))]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))
    return contents


async def _agent_loop(contents: list[types.Content]) -> str:
    """Gemini agent loop with read-only function calling, max 4 rounds."""
    client = genai.Client(api_key=_GEMINI_KEY)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=telegram_ai_tools.build_tool_declarations())],
        temperature=0.4,
    )
    for _ in range(_MAX_TOOL_ROUNDS):
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=config,
        )
        candidate = response.candidates[0]
        function_calls = _extract_function_calls(candidate)
        if not function_calls:
            return _extract_text(candidate).strip()

        tool_results: list[types.Part] = []
        for part in function_calls:
            fc = part.function_call
            args = dict(fc.args or {})
            logger.info("trading_ai tool call: %s(%r)", fc.name, args)
            result = await asyncio.to_thread(telegram_ai_tools.execute_tool, fc.name, args)
            tool_results.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                )
            ))
        contents.append(candidate.content)
        contents.append(types.Content(role="user", parts=tool_results))
    return ""


async def _ai_answer(user_message: str) -> str:
    if not _GEMINI_KEY:
        return "⚠️ Gemini no configurado (falta GEMINI_API_KEY en .env)."
    contents = await asyncio.to_thread(_build_contents, user_message)
    try:
        reply = await _agent_loop(contents)
    except Exception:
        logger.exception("ai_answer failed")
        return _AI_UNAVAILABLE_MSG
    if not reply:
        return _AI_UNAVAILABLE_MSG
    await asyncio.to_thread(telegram_chat_memory.save_exchange, user_message, reply)
    return reply


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
        await asyncio.Event().wait()
    finally:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        logger.info("trading telegram listener stopped")
