"""Read-only tools exposed to the Telegram AI chat via Gemini function calling.

Every tool here only READS files / config — none of them can place orders,
pause the bot, or write any state. Tool failures are returned as
``{"error": "..."}`` dicts so the agent loop never crashes on a bad file.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from google.genai import types

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "bot_state.json"
TRADES_FILE = DATA_DIR / "trades_history.json"
HEALTH_FILE = DATA_DIR / "bot_health.json"
PAUSE_FLAG = DATA_DIR / "pause.flag"
BOT_LOG = Path("bot.log")
BACKTEST_SUMMARY = Path("backtest/results/hypotheses_v1_summary.json")

MAX_LOG_LINES = 200


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _load_trades() -> list[dict]:
    trades = _read_json(TRADES_FILE, [])
    return trades if isinstance(trades, list) else []


def _ms_to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _trade_view(trade_id: int, trade: dict) -> dict:
    """Compact, model-friendly view: id, side, prices, pnl, result, exit ts."""
    return {
        "id": trade_id,
        "side": trade.get("side"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "pnl_usdt": trade.get("pnl_usdt"),
        "result": trade.get("result"),
        "reason": trade.get("reason"),
        "exit_ts": trade.get("exit_ts"),
        "exit_iso": _ms_to_iso(trade.get("exit_ts")),
    }


def get_trades(days: int = 30, limit: int = 50, result: str | None = None) -> dict:
    """Closed trades within the last `days` (newest first), optional WIN/LOSS filter."""
    days = max(1, int(days))
    limit = max(1, min(int(limit), 200))
    cutoff_ms = datetime.now(timezone.utc).timestamp() * 1000 - days * 86_400_000
    indexed = list(enumerate(_load_trades()))
    indexed = [(i, t) for i, t in indexed if (t.get("exit_ts") or 0) >= cutoff_ms]
    if result:
        wanted = result.strip().upper()
        indexed = [(i, t) for i, t in indexed if str(t.get("result", "")).upper() == wanted]
    indexed.sort(key=lambda pair: pair[1].get("exit_ts") or 0, reverse=True)
    pnls = [float(t.get("pnl_usdt") or 0.0) for _, t in indexed]
    wins = sum(1 for _, t in indexed if str(t.get("result", "")).upper() == "WIN")
    summary = {
        "count": len(indexed),
        "win_rate_pct": round(100 * wins / len(indexed), 2) if indexed else None,
        "net_pnl_usdt": round(sum(pnls), 4),
        "best_pnl_usdt": round(max(pnls), 4) if pnls else None,
        "worst_pnl_usdt": round(min(pnls), 4) if pnls else None,
    }
    return {
        "window_days": days,
        "result_filter": result,
        "summary": summary,
        "trades": [_trade_view(i, t) for i, t in indexed[:limit]],
    }


def get_daily_pnl(days: int = 30) -> dict:
    """Per-day realized PnL aggregation (date, n_trades, pnl, win rate)."""
    days = max(1, int(days))
    cutoff_ms = datetime.now(timezone.utc).timestamp() * 1000 - days * 86_400_000
    per_day: dict[str, dict] = {}
    for trade in _load_trades():
        exit_ts = trade.get("exit_ts") or 0
        if exit_ts < cutoff_ms:
            continue
        day = datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).date().isoformat()
        bucket = per_day.setdefault(
            day, {"date": day, "n_trades": 0, "wins": 0, "pnl_usdt": 0.0})
        bucket["n_trades"] += 1
        bucket["wins"] += 1 if str(trade.get("result", "")).upper() == "WIN" else 0
        bucket["pnl_usdt"] = round(bucket["pnl_usdt"] + float(trade.get("pnl_usdt") or 0.0), 4)
    rows = sorted(per_day.values(), key=lambda r: r["date"])
    for row in rows:
        row["win_rate_pct"] = round(100 * row.pop("wins") / row["n_trades"], 2)
    return {
        "window_days": days,
        "days_with_trades": len(rows),
        "total_pnl_usdt": round(sum(r["pnl_usdt"] for r in rows), 4),
        "daily": rows,
    }


def _unrealized_pnl(position: dict | None, last_close: Any) -> float | None:
    if not position or not isinstance(last_close, (int, float)):
        return None
    try:
        entry = float(position["entry_price"])
        qty = float(position["qty"])
    except (KeyError, TypeError, ValueError):
        return None
    direction = -1.0 if position.get("side") == "short" else 1.0
    return round((float(last_close) - entry) * qty * direction, 4)


def get_status() -> dict:
    """Current state machine, open position, paused flag, last tick, daily PnL."""
    state = _read_json(STATE_FILE, {})
    health = _read_json(HEALTH_FILE, {})
    position = state.get("position")
    return {
        "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bot_state": state.get("state"),
        "open_position": position,
        "unrealized_pnl_usdt": _unrealized_pnl(position, health.get("last_close")),
        "daily_pnl_usdt": state.get("daily_pnl"),
        "daily_pnl_date": state.get("daily_date") or state.get("daily_pnl_date"),
        "paused": PAUSE_FLAG.exists(),
        "health": {
            "last_tick_iso": _ms_to_iso(health.get("last_tick_ms")),
            "last_close": health.get("last_close"),
            "rsi14": health.get("rsi"),
            "state": health.get("state"),
            "daily_pnl_pct": health.get("daily_pnl_pct"),
        },
    }


def get_strategy_config() -> dict:
    """Live strategy parameters from config.BOT_CONFIG (read-only)."""
    import config

    cfg = dict(config.BOT_CONFIG)
    keys = [
        "symbol", "timeframe", "paper_balance", "risk_pct", "leverage",
        "rsi_threshold", "rsi_short_threshold",
        "stop_loss_pct_long", "stop_loss_pct_short",
        "take_profit_pct_long", "take_profit_pct_short",
        "circuit_breaker_pct", "cooldown_seconds", "max_sl_per_day",
        "use_atr_exits", "use_trailing_stop", "use_adx_filter",
        "adx_threshold", "use_trend_filter", "use_volatility_filter",
        "use_mtf_filter", "use_short_trend_filter",
        "use_session_filter", "blocked_sessions",
        "max_hold_hours", "range_lookback_min", "range_pct_threshold",
    ]
    out = {k: cfg[k] for k in keys if k in cfg}
    out["aggressive_mode"] = bool(getattr(config, "AGGRESSIVE_MODE", False))
    return out


def get_log_tail(lines: int = 40, grep: str | None = None) -> dict:
    """Tail of bot.log, optionally filtered by a case-insensitive substring."""
    lines = max(1, min(int(lines), MAX_LOG_LINES))
    if not BOT_LOG.exists():
        return {"error": "bot.log no existe"}
    with BOT_LOG.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        chunk = min(size, 256 * 1024)
        f.seek(size - chunk, 0)
        data = f.read().decode("utf-8", errors="replace")
    all_lines = data.splitlines()
    if grep:
        needle = grep.lower()
        all_lines = [ln for ln in all_lines if needle in ln.lower()]
    tail = list(deque(all_lines, maxlen=lines))
    return {"lines_returned": len(tail), "grep": grep, "log": tail}


def get_backtest_verdict() -> dict:
    """Gate verdicts of the hypotheses_v1 walk-forward study (why the bot is paused)."""
    summary = _read_json(BACKTEST_SUMMARY, None)
    if not isinstance(summary, dict):
        return {"error": f"{BACKTEST_SUMMARY} no disponible o ilegible"}
    results = summary.get("results", [])
    families = []
    for res in results:
        agg = res.get("aggregate", {})
        gate = res.get("gate", {})
        families.append({
            "label": res.get("label"),
            "family": res.get("family"),
            "symbol": res.get("symbol"),
            "passed_gate": bool(gate.get("passed")),
            "net_pnl_pct": agg.get("net_pnl_pct"),
            "win_rate_pct": agg.get("win_rate_pct"),
            "sharpe_annual": agg.get("sharpe_annual"),
            "dsr_pvalue": agg.get("dsr_pvalue"),
            "fail_reasons": (gate.get("reasons") or [])[:2],
        })
    passed = sum(1 for f in families if f["passed_gate"])
    return {
        "study": "hypotheses_v1 walk-forward (2026-06)",
        "gate_thresholds": summary.get("gate_thresholds", {}),
        "configs_passing_gate": passed,
        "configs_total": len(families),
        "conclusion": (
            "Ninguna de las estrategias evaluadas supera el gate estadístico "
            "(DSR, PnL, profit factor): no hay edge demostrado, por eso el bot "
            "está en paper/pausa y no opera dinero real."
            if passed == 0 else f"{passed}/{len(families)} configs pasan el gate."
        ),
        "families": families,
    }


# ── Registry & Gemini declarations ───────────────────────────────────────────

TOOLS: dict[str, Callable[..., dict]] = {
    "get_trades": get_trades,
    "get_daily_pnl": get_daily_pnl,
    "get_status": get_status,
    "get_strategy_config": get_strategy_config,
    "get_log_tail": get_log_tail,
    "get_backtest_verdict": get_backtest_verdict,
}


def execute_tool(name: str, args: dict[str, Any]) -> dict:
    """Run a registered tool; never raises — errors come back as dicts."""
    func = TOOLS.get(name)
    if func is None:
        return {"error": f"Herramienta desconocida: {name}"}
    try:
        return func(**args)
    except Exception as exc:  # noqa: BLE001 — tool errors must not reach the user
        logger.exception("tool %s failed args=%r", name, args)
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_tool_declarations() -> list[types.FunctionDeclaration]:
    """Gemini-native function declarations for every read-only tool."""
    return [
        types.FunctionDeclaration(
            name="get_trades",
            description=(
                "Operaciones cerradas del bot en los últimos N días, con stats "
                "(nº trades, win rate, PnL neto, mejor y peor trade). Filtra por "
                "result='WIN' o 'LOSS' si se pide solo ganadoras/perdedoras."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "days": {"type": "INTEGER", "description": "Ventana en días (default 30)"},
                    "result": {"type": "STRING", "description": "Filtro opcional: WIN o LOSS"},
                    "limit": {"type": "INTEGER", "description": "Máx trades devueltos (default 50)"},
                },
            },
        ),
        types.FunctionDeclaration(
            name="get_daily_pnl",
            description="PnL realizado agregado por día (UTC) con win rate diario.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "days": {"type": "INTEGER", "description": "Ventana en días (default 30)"},
                },
            },
        ),
        types.FunctionDeclaration(
            name="get_status",
            description=(
                "Estado actual del bot: máquina de estados, posición abierta con PnL "
                "no realizado, flag de pausa, último tick, RSI y PnL del día."
            ),
            parameters={"type": "OBJECT", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="get_strategy_config",
            description=(
                "Parámetros vivos de la estrategia (umbral RSI, SL/TP por lado, "
                "filtros ADX/tendencia/volatilidad on-off, circuit breaker, leverage). "
                "Útil para explicar por qué el bot no entró en una señal."
            ),
            parameters={"type": "OBJECT", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="get_log_tail",
            description="Últimas líneas de bot.log (máx 200), con filtro opcional de texto.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "lines": {"type": "INTEGER", "description": "Nº de líneas (default 40, máx 200)"},
                    "grep": {"type": "STRING", "description": "Subcadena para filtrar (case-insensitive)"},
                },
            },
        ),
        types.FunctionDeclaration(
            name="get_backtest_verdict",
            description=(
                "Veredicto del estudio walk-forward hypotheses_v1: cuántas estrategias "
                "pasan el gate estadístico y por qué el bot está pausado / sin edge."
            ),
            parameters={"type": "OBJECT", "properties": {}},
        ),
    ]
