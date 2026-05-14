# Trading Bot — Claude Code Context

## Stack
- Python 3.13.5, asyncio, ccxt, aiohttp, python-dotenv
- Exchange: Binance Testnet (paper) / Binance Real (fase 3)
- Pandas para cálculos de indicadores (strategy/); estructuras nativas Python en el resto
- GCP e2-micro instancia separada
- Dashboard frontend: Next.js 14 + TypeScript + Tailwind + Recharts + SWR (repo aparte, ver abajo)

## Arquitectura
- State machine con `BotState` enum en `core/state.py`
- `deque(maxlen=200)` como buffer de velas en `data/candles.py`
- Estado persistido en `bot_state.json` (reconciliar con exchange al arrancar)
- Loop principal en `core/loop.py` con asyncio
- Datos WebSocket vía `ccxt.pro` (no REST polling)

## Dashboard backend (api.py)
- FastAPI sirve en el puerto configurado en el servicio systemd
- Autenticación por cookie firmada con `itsdangerous` (24 h / 30 días con remember)
- Endpoints JSON/texto — **no** sirve HTML en producción cuando el frontend React está desplegado:
  - `GET /status` → `bot_state.json`
  - `GET /trades` → `trades_history.json`
  - `GET /logs` → últimas 200 líneas de `bot.log` (texto plano)
  - `GET /health` → tick age, last_close, rsi, state, daily_pnl_pct
  - `POST /login` / `GET /logout`
- Las plantillas Jinja y `static/` siguen disponibles como fallback legacy; la UI activa es el app Next.js.

## Dashboard frontend (repo: `/root/trading-bot-dashboard/`)
- Next.js 14 App Router + TypeScript + Tailwind + shadcn-style primitives + Recharts + Framer Motion
- SWR refresca cada 5 s (`/api/status`, `/api/trades`, `/api/logs`, `/api/health`)
- `next.config.js` rewrites `/api/*` → `${BOT_API_URL}` (default `http://127.0.0.1:8000`) para conservar la cookie de sesión
- Estética terminal dark: `#0a0a0a`, profit `#00ff88`, loss `#ff4444`, warning `#ffaa00`, neutral `#888888`
- Componentes (`components/`):
  - `Header` — status badge (verde/rojo pulse), BTC price live, logout
  - `BalanceCard` — balance con counter animado (10 000 USDT + Σ pnl_usdt)
  - `PnLCard` — total PnL verde/rojo con flecha de tendencia
  - `WinRateCard` — progreso circular con Recharts `RadialBarChart`
  - `BotStateCard` — WAITING_SIGNAL / IN_POSITION con pulse animado
  - `RSIGauge` — semicírculo Recharts; zona roja 0–30 (oversold), verde 70–100 (overbought)
  - `SMACard` — precio vs SMA20, badge ABOVE / BELOW
  - `ActivePosition` — entry, qty, unrealized PnL, tiempo abierto
  - `TradeHistory` — últimos 10 trades, badge WIN verde / LOSS rojo
  - `LiveLog` — últimas 20 líneas, monospace, color por level, auto-scroll
- `lib/derive.ts` calcula métricas (balance, winRate, unrealized) desde status + trades + health
- `lib/logParser.ts` parsea líneas de `/logs` y extrae SMA más reciente (fallback hasta que exista un endpoint de indicadores dedicado)

### Scripts
```bash
cd /root/trading-bot-dashboard
npm run dev     # http://localhost:3001
npm run build   # prod bundle
npm run start   # prod server, port 3001
```
Variable: `BOT_API_URL` (default `http://127.0.0.1:8000`) apunta al FastAPI.

## Reglas críticas — NUNCA violar
- NUNCA eliminar manejo de errores existente
- NUNCA hardcodear credenciales (solo `.env`)
- NUNCA hacer `break` en el loop por error de red
- Circuit breaker: −3 % daily drawdown detiene el bot
- Todo cambio en `exchange/client.py` requiere test antes de commitear
- `place_order_safe()` siempre confirma la orden en el exchange, no solo localmente

## Indicadores disponibles (`strategy/indicators.py`)
- `sma(df, period) -> pd.Series`
- `ema(df, period) -> pd.Series`
- `rsi(df, period=14) -> pd.Series`
- `df` tiene columna `close: float`; funciones puras, sin side effects

## Git workflow
- `main`: producción estable
- `develop`: integración
- `feature/*`: experimentos y nuevas features
- Commits en inglés, formato: `feat` / `fix` / `refactor` / `chore`

## Coding standards
- Funciones máximo 20 líneas con type hints siempre
- Naming: `snake_case` funciones, `PascalCase` clases, `UPPER_SNAKE` constantes
- Sin abreviaciones en nombres
- Logging estructurado, nunca `print()`
- Comentarios solo explican POR QUÉ, nunca QUÉ
- Sin código comentado en commits
- Una función, una responsabilidad

---

<!-- Sugerencias auditadas por AIDashboard analyzer (Gemini, score 88/100) -->

## Deployment

Existen **dos rutas de despliegue** coexistiendo en el repo; mantén la distinción explícita al modificar config:

1. **Producción actual — bare-metal + systemd** en GCP e2-micro. El servicio ejecuta `python main.py` directamente. El frontend Next.js corre como servicio systemd separado (`npm run start` en `/root/trading-bot-dashboard`).
2. **Dev / staging — Docker**. El `Dockerfile` en la raíz empaqueta el bot para desarrollo reproducible y CI. No se usa en producción hoy.

Si tocas el `Dockerfile` o introduces `docker-compose.yml`, revisa la skill `docker-compose`.

## Data Persistence

Persistencia actual basada en archivos JSON en la raíz:

- `bot_state.json` — estado de la máquina de estados + posición abierta. Se reconcilia con el exchange al arrancar; el exchange es la fuente de verdad.
- `trades_history.json` — log append-only de trades cerrados.
- `bot_health.json` — snapshot del último tick (timestamp, last_close, RSI, state, daily_pnl_pct).
- Logs: `bot.log` + `api.log` con rotación diaria (hasta `.4.gz`).

**Conocido como frágil**, en la lista de mejoras:
- Un crash mid-write de un JSON grande puede corromper el archivo. La mitigación actual es la reconciliación contra el exchange al arrancar.
- A largo plazo, migrar a Postgres con SQLAlchemy + Alembic (ver skill `sqlalchemy-alembic`).

## Known Issues / Future Improvements

- **Dashboard state via log-parse**: `lib/logParser.ts` en el frontend extrae SMA20 parseando `/logs` porque `api.py` no expone indicadores estructurados. Añadir un endpoint `/indicators` (o exponer métricas Prometheus) eliminaría el regex. Mientras tanto, cualquier cambio al formato de logging del bot debe mantener los campos parseables por `SMA_RE` / `PRICE_RE`.
- **Métricas**: no hay `/metrics` Prometheus. El dashboard depende de `/health` + polling JSON. Ver skill `prometheus-grafana` antes de añadir instrumentación.
- **WebSockets para UI**: el polling de 5 s vía SWR es suficiente para una cuenta single-user; si se añaden clientes simultáneos conviene pasar a WS server-side events.
- **Logs centralizados**: los `.gz` rotados se acumulan localmente. Un sink externo (Loki / ELK) es deseable cuando haya >1 servidor.
- **Diagrama de arquitectura**: el diagrama del `README` está desactualizado tras la migración a WebSocket streaming y frontend React. Pendiente de refresh.

## Skills instaladas (`.claude/skills/`)

- `docker-compose` — HIGH. Reglas para `Dockerfile` / `docker-compose.yml`.
- `sqlalchemy-alembic` — HIGH. Convenciones para la futura migración de JSON a Postgres.
- `prometheus-grafana` — MEDIUM. Naming, registries, alerting cuando se añada instrumentación.
- `pydantic` — MEDIUM. v2, `BaseSettings`, `Decimal` para dinero, validators.
- `fastapi` / `python` — baseline copiados por el analyzer.
- `clean-code-review` — revisión de calidad Python (long functions, type hints, print→logging).
- `safety-invariants-audit` — verifica las 6 reglas NUNCA VIOLAR antes de cerrar cualquier cambio en `core/`, `risk/`, `exchange/`, `strategy/`.
- `run-backtest` — comando de usuario `/run-backtest` con naming consistente (`{runner}_v{N+1}.json`) y delta vs corrida previa.

## Subagents (`.claude/agents/`)

- `trading-safety-reviewer` — revisor especializado en las 6 invariantes de seguridad. Invocar tras cambios a `exchange/client.py`, `risk/manager.py` o `core/loop.py`.
- `log-investigator` — correlaciona `bot.log` con `trades_history.json` y `bot_state.json` para diagnosticar anomalías.

## Hooks (`.claude/settings.json`)

- `PreToolUse` bloquea ediciones a `.env`, `bot_state.json`, `trades_history.json`, `bot_health.json` (archivos gestionados por el bot — editar a mano si es estrictamente necesario).
- `PostToolUse` ejecuta `pytest tests/test_<module>.py -x -q` automáticamente tras editar archivos en `core/`, `risk/`, `exchange/`, `strategy/`.

## Future MCPs

### When to add Postgres MCP
Add when migrating from JSON files to PostgreSQL:
```bash
claude mcp add postgres -- npx -y @modelcontextprotocol/server-postgres "$DATABASE_URL"
```

### When to add Sentry MCP
Add when deploying with real funds (Phase 3):
```bash
claude mcp add sentry -- npx -y @sentry/mcp-server
```
