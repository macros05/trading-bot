# Trading Bot — Claude Code Context

## Stack
- Python 3.13.5, asyncio, ccxt, aiohttp, python-dotenv
- Exchange: Binance Testnet (paper) / Binance Real (fase 3)
- Sin Pandas ni NumPy — estructuras nativas Python unicamente
- GCP e2-micro instancia separada, Docker NO en esta VM

## Arquitectura
- State machine con BotState enum en core/state.py
- deque(maxlen=200) como buffer de velas en data/candles.py
- Estado persistido en bot_state.json (reconciliar con exchange al arrancar)
- Loop principal en core/loop.py con asyncio

## Reglas criticas — NUNCA violar
- NUNCA eliminar manejo de errores existente
- NUNCA hardcodear credenciales (solo .env)
- NUNCA hacer break en el loop por error de red
- Circuit breaker: -3% daily drawdown detiene el bot
- Todo cambio en exchange/client.py requiere test antes de commitear
- place_order_safe() siempre confirma la orden en el exchange, no solo localmente

## Indicadores disponibles (strategy/indicators.py)
- sma(series, period) -> float | None
- ema(series, period) -> float | None
- rsi(series, period=14) -> float | None

## Git workflow
- main: produccion estable
- develop: integracion
- feature/*: experimentos y nuevas features
- Commits en ingles, formato: feat/fix/refactor/chore

## Coding standarts
- Funciones máximo 20 líneas con type hints siempre
- Naming: snake_case funciones, PascalCase clases, UPPER_SNAKE constantes
- Sin abreviaciones en nombres
- Logging estructurado, nunca print()
- Comentarios solo explican POR QUÉ, nunca QUÉ
- Sin código comentado en commits
- Una función, una responsabilidad"