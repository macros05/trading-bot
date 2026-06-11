"""FastAPI dashboard API for the trading bot.

Endpoints
---------
GET  /            → dashboard.html
GET  /status      → bot_state.json contents
GET  /trades      → trades_history.json contents (JSON array)
GET  /logs        → last N lines of bot.log as plain text
GET  /performance → aggregated metrics for the dashboard
POST /control/pause  → flip pause flag on
POST /control/resume → flip pause flag off
GET  /login       → login page
POST /login       → authenticate
GET  /logout      → clear session
"""

import hmac
import json
import math
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("trading.api")

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_USERNAME   = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD   = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY       = os.getenv("SECRET_KEY", "changeme")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

_BASE           = Path(__file__).parent
_DATA_DIR       = _BASE / "data"
_STATE_FILE     = _DATA_DIR / "bot_state.json"
_TRADES_FILE    = _DATA_DIR / "trades_history.json"
_LOG_FILE       = _BASE / "bot.log"
_HEALTH_FILE    = _DATA_DIR / "bot_health.json"
_LOG_LINES      = 200   # enough for signal/position history in the dashboard
_STALE_SECONDS  = 300   # 5 minutes without a tick → consider bot stale

_serializer       = URLSafeTimedSerializer(SECRET_KEY, salt="trading-session")
_SESSION_COOKIE   = "trading_session"
_SESSION_MAX_AGE  = 86_400        # 24 h
_REMEMBER_MAX_AGE = 86_400 * 30   # 30 days

_PUBLIC_PREFIXES = ("/login", "/internal", "/telegram", "/health")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Trading Bot Dashboard")
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(_BASE / "templates"))

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=_REMEMBER_MAX_AGE)
        return True
    except (SignatureExpired, BadSignature):
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if not _is_authenticated(request):
        return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)


# ── Login / logout ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "username": ""},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request:  Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form(default=""),
    next:     str = Form(default="/"),
):
    user_ok = hmac.compare_digest(username.strip(), ADMIN_USERNAME)
    pass_ok = hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Usuario o contraseña incorrectos.", "username": username},
            status_code=401,
        )

    max_age = _REMEMBER_MAX_AGE if remember == "1" else _SESSION_MAX_AGE
    token   = _serializer.dumps(username.strip())
    redirect_to = next if next.startswith("/") else "/"
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE, token,
        max_age=max_age, httponly=True, secure=True, samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ── Dashboard routes ──────────────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/status")
async def status():
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "UNKNOWN", "position": None}


@app.get("/trades")
async def trades():
    if _TRADES_FILE.exists():
        try:
            return json.loads(_TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


@app.get("/logs", response_class=PlainTextResponse)
async def logs():
    """Return the last N lines of bot.log as plain text for the dashboard JS parser."""
    if not _LOG_FILE.exists():
        return ""
    try:
        lines = _LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(lines[-_LOG_LINES:])
    except OSError:
        return ""


@app.get("/api/live-trades")
async def api_live_trades(limit: int = 200):
    """Return paginated live trades from SQLite with rich metadata."""
    from analytics.live_db import list_live_trades, count_trades
    return {
        'total':  count_trades(),
        'trades': list_live_trades(limit=max(1, min(limit, 1000))),
    }


@app.get("/api/validation")
async def api_validation():
    """Live vs backtest validation report — used by the dashboard panel."""
    from analytics.live_db import list_live_trades
    from analytics.validation import evaluate, per_condition_analysis
    from datetime import datetime, timezone
    from pathlib import Path
    import os

    trades = list_live_trades()
    # days_running: time since the bot db was created
    db_path = Path('data/live_trades.db')
    if db_path.exists():
        days_running = max(1, int(
            (datetime.now(timezone.utc).timestamp() - os.path.getmtime(db_path)) / 86_400
        ))
    else:
        days_running = 0
    return {
        'evaluation': evaluate(trades, days_running),
        'conditions': per_condition_analysis(trades),
    }


@app.get("/api/readiness")
async def api_readiness():
    """Demo-trading readiness gate report."""
    from analytics.live_db import list_live_trades
    from analytics.validation import readiness_check
    from datetime import datetime, timezone
    from pathlib import Path
    import os

    trades = list_live_trades()
    db_path = Path('data/live_trades.db')
    if db_path.exists():
        days_running = max(1, int(
            (datetime.now(timezone.utc).timestamp() - os.path.getmtime(db_path)) / 86_400
        ))
    else:
        days_running = 0
    return readiness_check(trades, days_running)


@app.get("/api/near-misses")
async def api_near_misses(limit: int = 200):
    """Recent near-miss snapshots for the dashboard distribution chart."""
    from analytics.live_db import list_near_misses
    return {'misses': list_near_misses(limit=max(1, min(limit, 1000)))}


@app.get("/api/kelly-history")
async def api_kelly_history():
    """Adaptive Kelly adjustment history."""
    from analytics.live_db import list_kelly_changes
    return {'changes': list_kelly_changes()}


@app.get("/performance")
async def performance():
    """Aggregated performance metrics: PnL/day, per-side, per-session, equity, last 20 trades."""
    from analytics.metrics import compute_performance
    trades = []
    if _TRADES_FILE.exists():
        try:
            trades = json.loads(_TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            trades = []
    return compute_performance(trades, initial_balance=10_000.0)


@app.post("/telegram/command")
async def telegram_command(request: Request):
    """Process a Telegram command forwarded from personal_assistant.

    Body: {"text": "/stats"} — returns either a payload to send back to the
    user's chat, or {"handled": false} when text is not a recognised command.
    """
    _require_internal_key(request)
    payload = await request.json()
    text = str(payload.get('text', ''))
    from telegram_commands import handle_command
    response = handle_command(text)
    if response is None:
        return {"handled": False}
    return {"handled": True, "reply": response}


@app.post("/control/pause")
async def control_pause():
    """Set the pause flag — bot keeps running but stops opening new positions."""
    from notifications import set_paused
    set_paused(True)
    return {"paused": True}


@app.post("/control/resume")
async def control_resume():
    """Clear the pause flag — bot resumes opening positions on signals."""
    from notifications import set_paused
    set_paused(False)
    return {"paused": False}


@app.get("/health")
async def health():
    """Machine-readable health check: last tick age, state, daily PnL."""
    if not _HEALTH_FILE.exists():
        return {"status": "starting", "last_tick_age_seconds": None}
    try:
        data = json.loads(_HEALTH_FILE.read_text())
        age = (time.time() * 1000 - data["last_tick_ms"]) / 1000
        # Coerce non-finite floats to None: a NaN here would make Starlette's
        # JSONResponse (allow_nan=False) raise during encoding → 500, which the
        # bots-watchdog reads as "unreachable" and restarts the container.
        def _safe(value):
            return None if isinstance(value, float) and not math.isfinite(value) else value
        return {
            "status":                "stale" if age > _STALE_SECONDS else "ok",
            "last_tick_age_seconds": round(age, 1),
            "last_close":            _safe(data.get("last_close")),
            "rsi":                   _safe(data.get("rsi")),
            "state":                 data.get("state"),
            "daily_pnl_pct":         _safe(data.get("daily_pnl_pct")),
        }
    except (json.JSONDecodeError, OSError, KeyError):
        return {"status": "error", "last_tick_age_seconds": None}


# ── Internal endpoints (service-to-service, X-Internal-Key auth) ──────────────

_INITIAL_BALANCE = 10_000.0
_STALE_CIRCUIT   = 20  # lines of bot.log scanned for circuit breaker signal


def _require_internal_key(request: Request) -> None:
    if not INTERNAL_API_KEY:
        raise HTTPException(status_code=503, detail="Internal API disabled")
    supplied = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(supplied, INTERNAL_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid internal key")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _today_midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _tail_log(n: int) -> list[str]:
    if not _LOG_FILE.exists():
        return []
    try:
        return _LOG_FILE.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _circuit_breaker_active(lines: list[str]) -> bool:
    for line in reversed(lines[-_STALE_CIRCUIT:]):
        if "circuit_breaker=active" in line:
            return True
        if "tick symbol=" in line or "paper_trade_open" in line:
            return False
    return False


@app.get("/internal/status")
async def internal_status(request: Request):
    _require_internal_key(request)
    state  = _read_json(_STATE_FILE,  {"state": "UNKNOWN", "position": None})
    health = _read_json(_HEALTH_FILE, {})
    last_tick_age = None
    if "last_tick_ms" in health:
        last_tick_age = round((time.time() * 1000 - health["last_tick_ms"]) / 1000, 1)
    return {
        "state":                 state.get("state"),
        "position":               state.get("position"),
        "daily_pnl":              state.get("daily_pnl", 0),
        "daily_pnl_pct":          health.get("daily_pnl_pct"),
        "last_close":             health.get("last_close"),
        "rsi":                    health.get("rsi"),
        "last_tick_age_seconds":  last_tick_age,
    }


@app.get("/internal/trades")
async def internal_trades(request: Request):
    _require_internal_key(request)
    trades     = _read_json(_TRADES_FILE, [])
    today_ms   = _today_midnight_ms()
    total_pnl  = sum(t.get("pnl_usdt", 0) for t in trades)
    today      = [t for t in trades if t.get("exit_ts", 0) >= today_ms]
    today_pnl  = sum(t.get("pnl_usdt", 0) for t in today)
    wins       = sum(1 for t in trades if t.get("result") == "WIN")
    win_rate   = wins / len(trades) if trades else 0.0
    return {
        "trades":           trades,
        "total_trades":     len(trades),
        "wins":             wins,
        "losses":           len(trades) - wins,
        "win_rate_pct":     round(win_rate * 100, 1),
        "total_pnl_usdt":   round(total_pnl, 4),
        "balance_usdt":     round(_INITIAL_BALANCE + total_pnl, 2),
        "today_trades":     len(today),
        "today_pnl_usdt":   round(today_pnl, 4),
    }


@app.get("/internal/logs")
async def internal_logs(request: Request, lines: int = 20):
    _require_internal_key(request)
    tail = _tail_log(max(1, min(lines, 500)))
    return {
        "logs":                    tail,
        "count":                   len(tail),
        "circuit_breaker_active":  _circuit_breaker_active(tail),
    }


@app.post("/internal/restart")
async def internal_restart(request: Request):
    """Reset the circuit breaker without restarting the container."""
    _require_internal_key(request)
    state = _read_json(_STATE_FILE, {"state": "UNKNOWN", "position": None})
    state["daily_pnl"]  = 0
    state["daily_date"] = ""
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write state file: {exc}")
    log.info("circuit_breaker reset via /internal/restart")
    return {"reset": True, "message": "Circuit breaker reset"}
