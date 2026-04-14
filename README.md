# trading-bot

Bot de trading asíncrono para Binance escrito en Python. Opera en testnet (paper trading) con una estrategia basada en indicadores técnicos (SMA, EMA, RSI), persistencia de estado en disco y circuit breaker por drawdown diario. Diseñado para correr en una instancia GCP e2-micro sin Docker.

---

## Stack

| Componente | Librería |
|---|---|
| Runtime | Python 3.13.5, asyncio |
| Exchange | ccxt 4.5.48 |
| HTTP | aiohttp 3.13.5 |
| Indicadores | pandas 3.0.2 |
| Config | python-dotenv 1.2.2 |
| Tests | unittest (stdlib) + pytest 9.0.3 |

---

## Instalación

```bash
git clone https://github.com/macros05/trading-bot.git
cd trading-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuración del .env

Crea un archivo `.env` en la raíz del proyecto (nunca lo commitees):

```env
BINANCE_API_KEY=tu_api_key_de_testnet
BINANCE_API_SECRET=tu_api_secret_de_testnet
```

Las credenciales de Binance Testnet se obtienen en [testnet.binance.vision](https://testnet.binance.vision).

---

## Correr los tests

```bash
# Todos los tests
python -m unittest discover -s tests

# Por módulo
python -m unittest tests.test_exchange_client
python -m unittest tests.test_indicators
python -m unittest tests.test_candles
python -m unittest tests.test_state
```

Todos los tests son unitarios y no requieren conexión a red ni credenciales reales.

---

## Estructura de carpetas

```
trading-bot/
├── core/
│   ├── state.py        # BotState enum + StateManager (persiste en bot_state.json)
│   └── loop.py         # Loop principal asyncio (pendiente)
├── data/
│   └── candles.py      # CandleBuffer — deque(maxlen=200) con conversión a DataFrame
├── exchange/
│   └── client.py       # BinanceClient — fetch_candles con retry exponencial (3 intentos)
├── strategy/
│   └── indicators.py   # sma(), ema(), rsi() — funciones puras sobre pd.DataFrame
├── risk/               # Circuit breaker y gestión de riesgo (pendiente)
├── tests/
│   ├── test_exchange_client.py
│   ├── test_indicators.py
│   ├── test_candles.py
│   └── test_state.py
├── main.py             # Punto de entrada (pendiente)
├── config.py           # Constantes globales (pendiente)
├── .env.example        # Plantilla de variables de entorno
├── CLAUDE.md           # Contexto y reglas para Claude Code
└── bot_state.json      # Estado persistido en runtime (generado automáticamente, en .gitignore)
```

---

## Estado actual

### Implementado

| Módulo | Descripción |
|---|---|
| `exchange/client.py` | Conexión a Binance Testnet, `fetch_candles(symbol, timeframe, limit)`, retry exponencial en `RateLimitExceeded` y `NetworkError` |
| `data/candles.py` | `CandleBuffer` con `add()`, `add_many()`, `to_dataframe()`, `is_ready(period)` |
| `strategy/indicators.py` | `sma()`, `ema()`, `rsi()` — Wilder smoothing, valores NaN hasta tener datos suficientes |
| `core/state.py` | `BotState` enum, `StateManager` con persistencia JSON, recuperación ante archivo corrupto |

### Pendiente

| Módulo | Descripción |
|---|---|
| `core/loop.py` | Loop principal asyncio — tick, fetch, evaluar señal, ejecutar orden |
| `risk/` | Circuit breaker (-3% drawdown diario detiene el bot), sizing de posición |
| `exchange/client.py` | `place_order_safe()` — colocación y confirmación de órdenes en el exchange |
| `config.py` | Constantes globales (símbolo, timeframe, parámetros de estrategia) |
| `main.py` | Punto de entrada, configuración de logging, arranque del loop |
| Fase 3 | Migración a Binance Real (producción) |
