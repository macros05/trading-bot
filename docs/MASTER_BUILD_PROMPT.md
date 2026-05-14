# Prompt-maestro para sesión Claude autónoma

> **Cómo usar:** Abre una sesión nueva de Claude Code en `/root/trading-bot/`. Pega el bloque completo de abajo como primer mensaje. Esa sesión debe ejecutar el plan a lo largo de múltiples sub-sesiones, dejando estado entre ellas en el repo. **Activa Auto Mode** en esa sesión.

---

## INSTRUCCIONES PARA CLAUDE — INICIO DEL BLOQUE A PEGAR

Vas a construir la mejor plataforma de trading cuant retail-accesible posible. Es un proyecto serio de 16 semanas que ejecutarás a lo largo de muchas sesiones. Esta es la sesión inicial.

### Contexto que necesitas saber

- **Usuario:** Marcos. Habla español, trabaja desde Linux/GCP. Email moralesgonzalezmarcos104@gmail.com. Hoy es **2026-05-14**.
- **Capital:** $500 USDT iniciales, plan de añadir capital cada quarter conforme se valide. Horizonte multi-año.
- **Bot anterior:** existe en este mismo repo (`/root/trading-bot/`), versión V6/V7. Diagnóstico: 4 trades paper todos perdedores (−$19.91), `bot_health.json` stale 4 días, branch `feature/lowvol-tuning-telegram-misses` con working tree sucio. Sample size insuficiente para validar edge (8 trades/año/símbolo).
- **Diseño aprobado:** lee `docs/superpowers/specs/2026-05-14-quant-platform-design.md` ANTES de hacer nada más. Es la fuente de verdad. **No modifiques el spec sin consultar a Marcos.**
- **Decisión arquitectónica:** conservar el motor (asyncio, ccxt.pro, state machine, exchange client, telegram, dashboard Next.js, FastAPI). **Reemplazar** la capa de estrategia por completo con un pipeline ML multi-estrategia (6 alfas + régimen + portfolio + execution layer).

### Filosofía de trabajo

1. **Test-Driven Development obligatorio** para todo código nuevo en `core/`, `risk/`, `execution/`, `ml/`, `portfolio/`. Invoca la skill `superpowers:test-driven-development` antes de empezar cualquier feature.
2. **Brainstorming → spec → plan → implementation** para cualquier decisión no triviales no cubierta por el spec maestro. Skill: `superpowers:brainstorming` → `superpowers:writing-plans` → execution.
3. **Verificación obligatoria antes de declarar "hecho":** invoca `superpowers:verification-before-completion` antes de marcar cualquier gate como pasado.
4. **Safety invariants** en cualquier cambio que toque `core/`, `risk/`, `exchange/`, `strategy/`: invoca skill `safety-invariants-audit`.
5. **Honestidad estadística:** los gates del spec son duros. Si un Sharpe OOS sale 1.4 cuando el gate pide 1.5, NO redondees, NO ajustes parámetros para hacer el número pasar (eso es overfitting). Reporta y discute con Marcos.
6. **Commits frecuentes en inglés**, formato `feat`/`fix`/`refactor`/`chore`. Co-author Claude.

### Hard rules — NUNCA violar

- NUNCA hardcodear credenciales (solo `.env`).
- NUNCA hacer `break` en el loop por error de red — siempre reconectar con backoff.
- NUNCA `--no-verify` en commits.
- NUNCA `git push --force` a `main` o `develop`.
- NUNCA deployear cambios a real money sin pasar todos los gates de §11 del spec.
- NUNCA escribir comments que repiten el código; solo el POR QUÉ no obvio.
- Circuit breaker: −3% daily drawdown detiene el bot — no lo desactives.
- `place_order_safe()` siempre confirma la orden en el exchange.

### Tu primera tarea (sesión 0 — esta sesión)

Ejecuta en orden, sin saltarte pasos:

**Paso 1 — Comprensión**
- Lee `docs/superpowers/specs/2026-05-14-quant-platform-design.md` completo.
- Lee `CLAUDE.md` del repo (contexto histórico).
- Lee `IMPROVEMENT_PLAN.md` (qué se intentó antes y por qué falló — secciones 1–10 son obligatorias, el resto opcional).
- Lee `DECISIONES_PENDIENTES.md`.
- Lee `README.md`.
- Lee `config.py`, `core/loop.py`, `core/state.py`, `exchange/client.py`, `risk/manager.py`, `strategy/signals.py`, `strategy/indicators.py` para entender el código existente.

**Paso 2 — Pre-trabajo (§11.0 del spec)**
- Verifica el estado del bot V6 actual: `ps aux | grep main.py`, `systemctl status trading-bot`. Si está corriendo, **antes de tocar nada pídele confirmación a Marcos** para detenerlo limpiamente. Una vez confirmado: detener, esperar que el state se persista, kill PID, verificar que no quedó nada.
- Documenta config V6 actual en `docs/v6-final-config.md` antes de cambiar nada (para rollback potencial).
- Limpia working tree: commit todos los archivos sucios bajo branch `wip/v7-experiments` (no `main`), luego switch a `main`, luego crea `feature/quant-platform-v1` desde `main`.
- Verifica que todos los tests existentes pasan (229/229 esperados): `pytest tests/ -x -q`.

**Paso 3 — Plan detallado de Fase 1**
- Invoca skill `superpowers:writing-plans` con input = §11.1 del spec (Infraestructura de datos, semanas 1–3).
- El plan resultante debe descomponer Fase 1 en tareas individuales testeables. Guárdalo en `docs/superpowers/plans/2026-05-14-phase1-data-infra.md`.
- Comparte el plan con Marcos para aprobación antes de codear.

**Paso 4 — Ejecución Fase 1**
- Una vez aprobado el plan: implementar tarea por tarea con TDD. Cada tarea = un commit. Tests primero.
- Al finalizar Fase 1: pasar los gates de §11.1 del spec. Documentar resultados en `docs/gates_log.md`.

**Paso 5 — Continuación**
- Cuando Fase 1 esté completa y validada, repetir Paso 3–4 para Fase 2 (Feature store).
- Y así sucesivamente hasta Fase 7.
- Fase 8 (deployment real escalonado) requiere autorización explícita de Marcos en cada etapa.

### Cómo navegar incertidumbre

- **Decisión técnica menor** (qué librería, qué nombre de variable): decide y avanza, documenta brevemente.
- **Decisión técnica con trade-offs reales** (qué algoritmo de allocation, qué timeframe para una estrategia): brainstorming corto en mensaje al usuario con 2–3 opciones y recomendación.
- **Decisión que altera el spec o el budget**: STOP, plantea a Marcos, espera respuesta.
- **Decisión que pone capital en riesgo**: STOP, plantea a Marcos, espera respuesta explícita.

### Reporte a Marcos

Al final de cada sesión, escribe un resumen corto en `docs/sessions/YYYY-MM-DD-sessionN.md` con:
- Qué se hizo
- Qué quedó pendiente
- Bloqueos / preguntas
- Próximo paso

Esto permite que sesiones subsecuentes (tú mismo, otra instancia, o cualquiera) recojan el hilo sin contexto previo.

### Recursos del entorno

- VPS: GCP e2-micro actual. Si necesitas escalar a e2-small ($15/mes), pide permiso explícito.
- APIs gratuitas iniciales: Binance, Bybit (backup), Glassnode free tier, LunarCrush free tier.
- APIs de pago aprobadas (cuando se necesiten): Glassnode Advanced $30/mes — diferir hasta que el feature de on-chain lo justifique con OOS Sharpe contribution.
- Docker disponible. TimescaleDB se desplegará en docker-compose desde Fase 1.
- Skills instaladas relevantes: `superpowers:*`, `safety-invariants-audit`, `run-backtest`, `clean-code-review`, `sqlalchemy-alembic`, `prometheus-grafana`, `docker-compose`, `pydantic`, `fastapi`, `python`.
- Subagents disponibles: `trading-safety-reviewer`, `log-investigator`.

### Métrica de éxito del proyecto

La plataforma está completa cuando:
1. Todos los gates §11.1 a §11.7 documentados como pasados.
2. 90 días paper trading en mainnet con todas las métricas en tolerancia.
3. ≥ 800 tests, ≥ 85% cobertura en módulos críticos.
4. Sharpe OOS ≥ 1.5, max DD ≤ 15%, ≥ 200 trades OOS, DSR ≥ 0.95.
5. Documentación operativa completa.

Sólo entonces se considera lista para deployment real escalonado (§11.8).

### Empezar

Cuando hayas leído todo esto y el spec, responde con:
1. Un resumen de 5 líneas confirmando que entendiste.
2. Una lista de cualquier ambigüedad o conflicto que detectes entre este prompt y el spec.
3. Tu plan inmediato para el Paso 1–2.
4. Cualquier pregunta crítica que necesites resuelta antes de empezar.

Espera la respuesta de Marcos antes de proceder al Paso 2.

---

## INSTRUCCIONES PARA CLAUDE — FIN DEL BLOQUE A PEGAR

---

## Notas operativas para Marcos (no pegar, solo leer)

- Esta sesión nueva debe arrancar con suficiente contexto para operar sola. Si ves que se descarrila, intervén con un mensaje correctivo claro — no la dejes "improvisar" en decisiones de riesgo.
- Los gates de fases son **duros**. Si la sesión los rebaja por su cuenta sin pasar por ti, eso es una señal de mal comportamiento — corrige.
- Cualquier decisión que afecte capital real (parámetros de sizing, leverage, switch a mainnet con dinero) DEBE pasar por ti explícitamente.
- Las sesiones autónomas son buenas para implementación; las decisiones estratégicas siguen siendo tuyas.
- Revisión humana recomendada: al final de cada fase, antes de empezar la siguiente. ~30 min de tu tiempo por fase. Aprox 7 puntos de revisión total en 16 semanas.
- Si el progreso se atasca (más de 5 días sin avanzar gate), pausa, lee `docs/sessions/`, identifica bloqueo, corrige.

## Costes esperados (Claude API + infra)

- Claude Opus 4.7: la mayoría del trabajo ejecutable con Sonnet 4.6 para implementación, Opus 4.7 para diseño/debug complejo. Estimación: $50–150/mes de Claude API durante construcción activa.
- VPS GCP: $5–15/mes.
- APIs externas: $0–30/mes.
- **Total construcción activa**: $55–195/mes durante 16 semanas ≈ $220–780 inversión one-time hasta paper-trading completo.
- Post-deployment: $20–45/mes operativo.

## Cuándo este proyecto vale la pena

- Si validas Sharpe OOS ≥1.5 sobre walk-forward y luego ±30% paper de 90 días, vale.
- Si tras Fase 5 los gates no pasan, debe haber autopsia honesta: o (a) tuning conservador con pérdida de aspiración, o (b) abortar y volver a un overlay tipo Path C del spec.
- No es proyecto para "ya quiero ver dinero". Es proyecto para construir una capacidad seria que crece contigo.
