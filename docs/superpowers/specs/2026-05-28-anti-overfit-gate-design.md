# Anti-Overfit Champion-Promotion Gate — Design (Spec 1)

_Fecha: 2026-05-28 · Repo: `trading-bot` · Rama: `feature/anti-overfit-gate`_
_Contexto: respuesta a `AUDIT.md` (2026-05-27/28). Veredicto de la auditoría: ningún bot tiene edge demostrable; el champion del BTC se promovió **ignorando su propio veredicto honesto** (DSR p=0, WR-LB < breakeven). Este spec construye la red de seguridad que hace ese auto-engaño imposible._

## Problema

La promoción a "champion" era **100 % manual**: un humano editaba constantes en `config.py` y reiniciaba. El sweep ya calculaba `dsr_pvalue`, `num_trades`, `wr_lower_95`, `breakeven_wr` e incluso imprimía `✓DSR/✗DSR` (`backtest/sweep_v7_full.py:292`), pero **nada lo aplicaba**. Resultados:
- Se desplegó un champion con `dsr_pvalue=0.00` y `wr_lower_95=38.64 < breakeven=43.08`.
- `analytics/validation.py` comparaba el live contra un baseline **perdedor** (−292 USDT): validación invertida.
- La config viva había derivado del champion nominal sin registro (ver Hallazgos).

## Decomposición (3 specs, en orden)

1. **Spec 1 — Blindaje anti-overfit (ESTE doc).** Gate de promoción + certificado + guard de runtime + fix del baseline. Red de seguridad.
2. **Spec 2 — Walk-forward real + pipeline de hipótesis.** Arreglar `walk_forward.py:76` (`fold_params=params` → optimización in-fold real) y generar hipótesis ≥100 trades/año que pasen el gate de Spec 1. _Pendiente._
3. **Spec 3 — Pivot Polymarket** a tesis tolerante a latencia (MM / arbitraje). _Pendiente._

## Enfoque elegido (A): certificado + auto-verificación al arranque

`config.py` sigue siendo la fuente de verdad del runtime. Se añade:

### Componentes

| Unidad | Fichero | Responsabilidad | Pura |
|---|---|---|---|
| Gate | `backtest/promotion_gate.py` | `evaluate_config(result, thresholds) → GateVerdict`; `select_champion(results)` | sí |
| Puente/certificado | `backtest/promotion_cert.py` | `translate_params`, `build_certificate`, `decide` | sí |
| CLI | `scripts/promote_champion.py` | argparse: carga sweep, aplica gate, escribe certificado, imprime config a aplicar | I/O |
| Guard runtime | `core/champion_guard.py` | `load_certificate`, `verify_champion(bot_config, cert) → GuardResult` | casi |
| Validación | `analytics/validation.py` | `load_baseline(cert)` + baseline neutral; `evaluate(..., baseline=)` | sí |
| Cableado | `main.py` | check al arranque: log + alerta Telegram, **nunca bloquea** | glue |

### Reglas del gate (hard bars)
1. `dsr_pvalue >= 0.95` (DSR de López de Prado: el Sharpe sobrevive a la corrección por nº de configs probadas).
2. `num_trades >= 100` (muestra estadísticamente útil; el champion fallido tenía 16 en 2 años).
3. `wr_lower_95 > max(breakeven_long, breakeven_short)` (el LB del WR debe batir el breakeven del lado más estricto).

### Política de fallo: **bloqueo duro + override con registro** (elección del usuario)
- Por defecto el CLI **rechaza** (exit 2) cualquier config que falle, listando las razones.
- Override consciente: `--override --reason "..." --operator NAME` certifica igual, pero el certificado graba un bloque `override` con razón, operador y timestamp. El gate falla queda preservado en `gate.reasons`.
- El guard de runtime traduce override → `WARNING` (corre pero marcado); sin certificado o con drift → `CRITICAL`.

### Invariante de seguridad (Session 0)
El guard de runtime **nunca lanza ni detiene el loop**. Devuelve un `GuardResult`; `main.py` loguea + alerta por Telegram y continúa. Un guard que pudiera tirar el bot sería peor que la deriva que detecta. Cubierto por `test_guard_never_raises_on_malformed_certificate` y `try/except` total en `main.py`.

### Certificado (`champion_certificate.json`, versionado)
Campos: `schema_version`, `promoted_at_ms`, `label`, `symbol`, `source_sweep_file` + `source_sweep_sha256`, `git_commit`, `params` (sweep), `config_params` (traducidos a nombres BOT_CONFIG), `gate` (thresholds/passed/metrics/reasons), `expected_metrics` (alimenta el baseline de validación), `validation_caveats`, y `override` (solo si aplica).

### Traducción de parámetros (hallazgo crítico)
Los `params` del sweep usan nombres distintos a `BOT_CONFIG` para las claves más importantes (`rsi_long_threshold`→`rsi_threshold`, `sl_pct_long`→`stop_loss_pct_long`, `tp_pct_long`→`take_profit_pct_long`, …). `SWEEP_TO_CONFIG_KEYS` mapea los 15 params materiales; el guard compara **todas** las claves de `config_params` (no un subconjunto hardcodeado), para que ninguna clave decisiva pueda derivar en silencio.

## Verificación empírica
- **0 de 45** variantes (BTC+ETH+SOL, 24 meses) pasan el gate → ninguna config existente es promovible sin override. Forcing function intacta.
- E2E: el guard da `WARNING` al certificar `vol_off` por override (coincide con la config viva) y `CRITICAL` al certificar el champion nominal `live_v7_post_session0` (deriva real en `use_volatility_filter`).
- Suite completa: **493 tests OK** (37 nuevos). Sin regresiones.

## Hallazgos durante la implementación
1. **La config viva derivó del champion nominal.** `BOT_CONFIG` coincide con la variante `vol_off`, no con `live_v7_post_session0` (difiere en `use_volatility_filter`, desactivado en un commit previo). El guard lo detecta como `CRITICAL`.
2. **MacroFilter está dormido en vivo.** `main.py` no pasa `macro_filter` a `trading_loop` (default `None`). No hay discrepancia live↔backtest hoy; rebaja la severidad del hallazgo D4 de la auditoría. Registrado como caveat en cada certificado por si se reactiva.
3. **Baseline invertido eliminado.** `validation.py` ya no compara contra −292 USDT; baseline neutral (PnL esperado 0 → la alerta de divergencia de PnL queda en silencio hasta que exista un champion certificado). Las alertas de WR y degradación rolling siguen activas.

## Fuera de alcance (Spec 2)
- Walk-forward con optimización in-fold real.
- Generación de hipótesis de mayor frecuencia.
- Expected-PnL explícito para reactivar la alerta de divergencia de forma honesta.
