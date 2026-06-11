# Quant Platform — Diseño Completo

**Fecha:** 2026-05-14
**Autor:** Marcos (cliente) + Claude (arquitecto)
**Estado:** Aprobado para implementación (Path B directo)
**Capital inicial:** $500 USDT — escalado progresivo
**Horizonte:** Multi-año, "set & grow"

---

## 0. Resumen ejecutivo

Reemplazar el bot mono-estrategia (RSI+SMA+ADX en BTC 1m, ~8 trades/año/símbolo, sin edge estadísticamente probado vs buy-and-hold) por una **plataforma cuant multi-estrategia, multi-símbolo, multi-timeframe** con pipeline ML supervisado, detección de régimen, gestión de riesgo a nivel portfolio, y ejecución algorítmica.

**Objetivo medible (criterios de éxito):**
- Sharpe ratio **anualizado** OOS ≥ **1.5** sobre walk-forward de 24 meses
- Drawdown máximo OOS ≤ **15%**
- ≥ **200 trades OOS** acumulados antes de scaling real
- Bate buy-and-hold BTC neto de costes en ≥ 70% de ventanas de 90 días rolling
- Bate el backtest V6 actual (64% WR / +9.8% / 12mo / Sharpe per-trade 0.40) en Sharpe anualizado, número de trades, y consistencia multi-símbolo

**Non-goals (lo que NO intentamos):**
- High-frequency trading sub-segundo (latencia retail no permite)
- Market making puro (requiere maker rebates por volumen >$50k/mes)
- Estrategias propietarias de hedge fund (no replicables sin sus datos privados)
- Capacidad >$5M (saturación de alfas en cripto retail)

**Compromiso de honestidad:** Renaissance Medallion hace 66% neto/año por 30 años. No es nuestro objetivo. Apuntamos a **Sharpe 1.5–2.5 OOS sostenido**, lo cual ya supera al 95% de hedge funds publicados. Cualquier promesa por encima es marketing.

---

## 1. Diagnóstico del estado actual (de qué partimos)

| Aspecto | Estado | Acción |
|---|---|---|
| Bot live | V6 deployado, 4 trades paper, 0% WR, −$19.91, `bot_health.json` stale 4 días | Detener antes de empezar (ver §11.0) |
| Stack base | Python 3.13, asyncio, ccxt.pro, FastAPI, Next.js 14 dashboard | **Conservar** — sólido |
| Persistencia | JSON files (frágil, documentado) | Migrar a TimescaleDB |
| Backtest | `backtest/cli.py`, walk-forward harness, costs modeled | Conservar como v1, escribir v2 con order book replay |
| Tests | 229/229 pasando | Conservar disciplina TDD |
| Branch sucio | `feature/lowvol-tuning-telegram-misses` con 25+ untracked | Limpiar antes de empezar |

**Decisión arquitectónica:** El motor (state machine, asyncio loop, exchange client, telegram) se **conserva y se extiende**. La capa de estrategia se **reemplaza por completo** por un pipeline ML modular.

---

## 2. Arquitectura

### 2.1 Vista de capas

```
┌──────────────────────────────────────────────────────────────────┐
│  L7 — OBSERVABILITY                                              │
│    Prometheus · Grafana · Sentry · Telegram dead-man             │
├──────────────────────────────────────────────────────────────────┤
│  L6 — EXECUTION                                                  │
│    Smart order router · maker-preferred · slippage budget        │
├──────────────────────────────────────────────────────────────────┤
│  L5 — PORTFOLIO & RISK                                           │
│    Bayesian allocator · Kelly fractional · VaR · correlation gate│
├──────────────────────────────────────────────────────────────────┤
│  L4 — REGIME DETECTOR                                            │
│    HMM gauss · vol regime · funding regime                       │
├──────────────────────────────────────────────────────────────────┤
│  L3 — STRATEGY ENSEMBLE (6 alphas)                               │
│    MR · XS-Mom · Breakout · Funding · Basis · Stat-arb pairs     │
├──────────────────────────────────────────────────────────────────┤
│  L2 — ML PIPELINE                                                │
│    Features · Triple-barrier labels · LightGBM · Meta-labeling   │
├──────────────────────────────────────────────────────────────────┤
│  L1 — FEATURE STORE                                              │
│    Price · OB · Funding · OI · On-chain · Sentiment              │
├──────────────────────────────────────────────────────────────────┤
│  L0 — DATA INGESTION                                             │
│    ccxt.pro WS · Tardis (hist) · Glassnode · LunarCrush · DB     │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 Stack tecnológico

| Capa | Tecnología | Justificación |
|---|---|---|
| Lenguaje | Python 3.13 (async/await first) | Continuidad + ecosistema cuant maduro |
| Async core | asyncio + ccxt.pro | Ya está en uso |
| ML | LightGBM (primary), scikit-learn (utilidades), opcional PyTorch para LSTMs (fase 3) | LightGBM es estándar de facto para finanzas tabulares; rápido, robusto, interpretable |
| Time-series DB | TimescaleDB (Postgres + hypertables) | Compresión nativa, retención automática, soporta queries SQL estándar |
| Hot state | Redis 7 | Cache de últimos features + estado intra-tick |
| Backtest engine | Custom v2 con `vectorbt` opcional para sweeps | Reuso del harness actual + capacidades nuevas |
| Métricas | Prometheus + Grafana | Estándar industrial |
| Alertas | Telegram + Sentry | Telegram ya está integrado |
| Contenedores | Docker Compose | Reproducibilidad |
| Orquestación | systemd (mantener) | No requerimos k8s a este tamaño |
| CI | GitHub Actions con pytest + ruff + mypy strict | Calidad reproducible |

### 2.3 Módulos del repo (estructura final)

```
trading-bot/
├── core/                       # motor existente, extendido
│   ├── loop.py                 # tick loop, ahora multi-símbolo
│   ├── state.py
│   └── orchestrator.py         # NEW: coordina estrategias
├── exchange/                   # existente
│   ├── client.py
│   └── routers/                # NEW: smart order routing
├── data/
│   ├── ingestion/              # NEW
│   │   ├── ws_market.py
│   │   ├── orderbook_capture.py
│   │   ├── funding.py
│   │   ├── onchain_glassnode.py
│   │   └── sentiment_lunarcrush.py
│   ├── store/                  # NEW
│   │   ├── timescale.py
│   │   └── redis_cache.py
│   └── candles.py              # existente, mantenido
├── features/                   # NEW
│   ├── price_features.py
│   ├── orderbook_features.py
│   ├── cross_asset_features.py
│   ├── funding_features.py
│   ├── onchain_features.py
│   ├── sentiment_features.py
│   └── registry.py             # catálogo + tests por feature
├── ml/                         # NEW
│   ├── labeling/
│   │   ├── triple_barrier.py
│   │   └── meta_labels.py
│   ├── models/
│   │   ├── primary_lgbm.py
│   │   ├── meta_lgbm.py
│   │   └── ensemble.py
│   ├── training/
│   │   ├── purged_kfold.py
│   │   ├── walk_forward.py
│   │   └── train_pipeline.py
│   └── inference/
│       └── live_predictor.py
├── strategy/                   # rewrite
│   ├── base.py                 # ABC con interfaz uniforme
│   ├── mean_reversion.py
│   ├── xs_momentum.py
│   ├── breakout.py
│   ├── funding_carry.py
│   ├── basis_spot_perp.py
│   ├── stat_arb_pairs.py
│   └── registry.py
├── regime/                     # NEW
│   ├── hmm_detector.py
│   ├── vol_regime.py
│   └── classifier.py
├── portfolio/                  # NEW
│   ├── allocator.py            # bayesian / risk parity
│   ├── kelly.py                # fractional kelly
│   ├── correlation_gate.py
│   └── var_cvar.py
├── risk/                       # existente, extendido
│   ├── manager.py
│   ├── circuit_breaker.py
│   └── exposure_limits.py
├── execution/                  # NEW
│   ├── smart_router.py
│   ├── limit_ladder.py
│   ├── slippage_tracker.py
│   └── tca.py                  # Transaction Cost Analysis
├── backtest/                   # rewrite v2
│   ├── engine_v2.py            # con order book replay
│   ├── cost_model.py
│   ├── walk_forward_pkf.py
│   ├── statistics.py           # DSR, PSR, bootstrap CI
│   └── reporters/
├── observability/              # NEW
│   ├── metrics.py              # prometheus exporters
│   ├── dashboards/             # grafana JSON
│   └── alerts.py
├── api.py                      # FastAPI existente, extendido
├── main.py                     # entrypoint orchestrator
├── tests/                      # extender a >800 tests
├── docker/
│   ├── docker-compose.yml      # full stack
│   └── Dockerfile.*
└── docs/superpowers/specs/2026-05-14-quant-platform-design.md (this)
```

---

## 3. Data layer (L0–L1)

### 3.1 Fuentes de datos

| Fuente | Tipo | Coste | Fallback |
|---|---|---|---|
| Binance WS (ccxt.pro) | Trades + klines en vivo | Gratis | REST polling |
| Binance L2 order book (depthCache) | OB top-20 | Gratis | Reconstrucción desde diffs |
| Binance funding rates | REST cada 8h | Gratis | — |
| Bybit (secundario) | Misma data, redundancia | Gratis | — |
| Tardis.dev (histórico) | OB tick-by-tick para backtest | Tier gratuito 2 días + capturamos nosotros 90+ días | Capturar todo en vivo desde hoy |
| Glassnode | On-chain BTC/ETH (exchange flows, MVRV, SOPR) | Tier gratuito (lag 24h) | Cubierto: lag aceptable |
| LunarCrush API | Sentiment + social volume | Tier gratuito 100 req/día | Twitter `snscrape` |
| Coinglass | Funding agregado multi-exchange | Gratuito web → scraping | Solo Binance funding |

### 3.2 TimescaleDB schema (esencial)

```sql
CREATE TABLE candles (
  symbol     TEXT NOT NULL,
  timeframe  TEXT NOT NULL,         -- '1m', '5m', '15m', '1h', '4h', '1d'
  ts         TIMESTAMPTZ NOT NULL,
  open       NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC,
  volume     NUMERIC,
  trades     INTEGER,
  PRIMARY KEY (symbol, timeframe, ts)
);
SELECT create_hypertable('candles', 'ts');
SELECT add_compression_policy('candles', INTERVAL '7 days');

CREATE TABLE orderbook_snapshots (
  symbol TEXT, ts TIMESTAMPTZ,
  bid_price_1 NUMERIC, bid_size_1 NUMERIC, ... bid_20 ...,
  ask_price_1 NUMERIC, ask_size_1 NUMERIC, ... ask_20 ...,
  PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('orderbook_snapshots', 'ts');

CREATE TABLE funding_rates (
  symbol TEXT, ts TIMESTAMPTZ, rate NUMERIC, mark_price NUMERIC,
  PRIMARY KEY (symbol, ts)
);

CREATE TABLE onchain_metrics (
  metric TEXT, asset TEXT, ts TIMESTAMPTZ, value NUMERIC,
  PRIMARY KEY (metric, asset, ts)
);

CREATE TABLE sentiment (
  symbol TEXT, ts TIMESTAMPTZ, source TEXT,
  galaxy_score NUMERIC, social_volume NUMERIC, sentiment_polarity NUMERIC,
  PRIMARY KEY (symbol, ts, source)
);

CREATE TABLE trades_live (
  trade_id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ, symbol TEXT, side TEXT, strategy TEXT,
  entry NUMERIC, exit NUMERIC, qty NUMERIC,
  pnl_gross NUMERIC, pnl_net NUMERIC, fees NUMERIC, slippage NUMERIC,
  features JSONB, prediction NUMERIC, regime TEXT, reason_exit TEXT
);
```

**Retención:** 1m candles 90 días, luego comprimidos. OB snapshots solo 30 días (volumen alto). Trades_live: indefinido.

### 3.3 Universo inicial

8 símbolos en spot (con perp disponible para funding/basis):

```python
UNIVERSE = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
            'AVAX/USDT', 'LINK/USDT', 'MATIC/USDT', 'ATOM/USDT']
```

Criterios de inclusión: market cap top-30, volumen diario >$100M, listado spot+perp en Binance, sin halts recientes, correlación con BTC <0.85 (para diversificación).

---

## 4. Feature engineering (L2)

**Filosofía:** Cada feature debe (1) tener un test unitario, (2) un docstring explicando la hipótesis económica, (3) un test de stationarity / leakage.

### 4.1 Catálogo de features (objetivo: 60–80 por símbolo)

| Familia | Ejemplos | # |
|---|---|---|
| Price/return | log returns 1/5/15/60/240m, realized vol multi-horizon, autocorrelación rolling, skew/kurt | 12 |
| Technical | RSI(7,14,28), MACD hist, Bollinger %B, ATR%, ADX, Stoch, Aroon | 10 |
| Microstructure | OB imbalance top-1/top-5/top-20, spread bps, depth ratio, quote intensity, trade flow imbalance (Kyle's lambda proxy) | 10 |
| Cross-asset | Correlación rolling con BTC, BTC dominance, retorno relativo a ETH, beta rolling | 6 |
| Funding/OI | Funding rate actual, funding rate ma 24h, OI cambio %, basis spot-perp bps | 6 |
| Volatility | Realized vol 24h/7d, vol-of-vol, GARCH(1,1) forecast 1h, vol percentile rolling 30d | 6 |
| On-chain (BTC/ETH) | Exchange netflow 24h, stablecoin supply change 7d, MVRV Z-score, SOPR, active addresses 24h | 8 |
| Sentiment | Galaxy Score delta, social volume z-score 7d, sentiment polarity delta | 4 |
| Time/calendar | Hour of day (cíclico sin/cos), día semana, distancia a funding payout, NFP/CPI proximity flag | 6 |

**Anti-leakage:**
- Todos los features se calculan con `shift(1)` mínimo respecto al instante de decisión.
- Tests: `test_feature_no_lookahead.py` verifica que recomputar el feature en t solo usa data ≤ t−1.
- Datos on-chain: lag de 24h (free tier) modelado explícitamente en backtest.

### 4.2 Feature registry

`features/registry.py` mantiene un dict tipado:

```python
@dataclass
class FeatureSpec:
    name: str
    func: Callable[[pd.DataFrame, ...], pd.Series]
    inputs: list[str]            # qué columnas/symbols necesita
    lookback_bars: int           # historia mínima para evaluar
    family: str
    economic_hypothesis: str     # docstring obligatorio
```

Esto permite generar features por símbolo en paralelo y trackear su contribución al modelo.

---

## 5. Labeling — Triple Barrier (López de Prado)

### 5.1 Definición

Para cada candidato a entrada (long o short) en tiempo $t$ a precio $p_t$:

- **Barrera superior:** $p_t \cdot (1 + k_u \cdot \sigma_t)$
- **Barrera inferior:** $p_t \cdot (1 - k_d \cdot \sigma_t)$
- **Barrera temporal:** $t + H$ bars

Donde $\sigma_t$ = realized volatility 24h, $k_u, k_d$ = multiplicadores (típicamente 2.0 y 1.0 para R:R 2:1), $H$ = horizonte máximo (ej. 24 horas).

**Label:**
- $+1$ si toca barrera superior primero
- $-1$ si toca barrera inferior primero
- $0$ si expira por tiempo

### 5.2 Por qué este enfoque

- Reemplaza "predecir return 1h adelante" (regresión ruidosa) por "predecir si un setup gana antes de perder" (clasificación directamente alineada con el PnL).
- Las barreras escalan con vol → labels comparables entre regímenes.
- Sample weights por unicidad: trades solapados en tiempo reciben peso reducido (evita over-counting).

### 5.3 Meta-labeling

Sobre el modelo primario (binario: "tomar trade?"), entrenamos un segundo modelo (sizing: "qué tan confiado?"). El meta-label es 1 si el primary acertó, 0 si falló. El meta-model produce probabilidad calibrada que multiplica el tamaño base por Kelly:

$$\text{size} = \text{base\_size} \times \frac{p_{\text{meta}} - 0.5}{0.5}$$ (recortado a $[0, 1]$)

---

## 6. ML pipeline

### 6.1 Modelos

- **Primary classifier**: LightGBM binario por estrategia (o multi-clase si tres labels). Features = registry filtrado por estrategia. Target = triple-barrier label.
- **Meta classifier**: LightGBM binario. Features = primary's prediction + un subset de market features. Target = "primary tuvo razón".
- **Ensemble** (opcional fase 3): votación blanda de LightGBM + Random Forest + LSTM. Solo si OOS Sharpe del LightGBM solo es ≥0.8.

### 6.2 Cross-validation: Purged k-fold con embargo

Estándar López de Prado:

1. Partir el dataset en $k=5$ folds temporales contiguos.
2. Para cada fold de test, **purgar** del train cualquier muestra cuya etiqueta concurrent al test (i.e., su barrera temporal cae dentro del test).
3. **Embargo**: descartar también las $h$ barras inmediatamente posteriores al test (en cripto 24h–48h).
4. Reportar métricas agregadas + por fold.

### 6.3 Walk-forward retraining

- **Initial train**: 18 meses.
- **Retrain cadence**: cada 30 días, expanding window.
- **Test slice**: los siguientes 30 días tras cada train.
- **Concept drift detection**: si OOS Sharpe del último mes cae >50% vs media móvil 6m, alerta y bloquea la estrategia hasta investigación.

### 6.4 Estadística rigurosa

- **Deflated Sharpe Ratio (DSR)** de López de Prado: corrige Sharpe por número de configuraciones probadas. Cualquier estrategia reportada debe tener DSR ≥ 0.95 (95% confianza de no ser ruido).
- **Bootstrap CI** sobre todas las métricas (1000 muestras).
- **Reality Check de White**: test multiple-comparison al evaluar el conjunto de estrategias.

---

## 7. Estrategias (L3) — 6 alfas concretas

Cada estrategia implementa la interfaz `Strategy(ABC)`:

```python
class Strategy(ABC):
    name: str
    timeframe: str
    universe: list[str]

    def generate_signal(self, features: pd.DataFrame, ts: datetime) -> Signal | None: ...
    def position_sizing_hint(self, signal: Signal, equity: float) -> float: ...
    def get_required_features(self) -> list[str]: ...
```

### 7.1 Mean-Reversion intraday (MR)
- **Timeframe**: 15m
- **Universo**: todos los 8 símbolos
- **Setup primario**: precio cruza por debajo de Bollinger lower band(20, 2σ) AND RSI(14) < 30
- **Filtro régimen**: solo en régimen `ranging` (ver §8)
- **ML primario**: predice si toca TP (2×σ_24h arriba) antes que SL (1×σ_24h abajo) en 12h
- **Exit**: triple-barrera 2σ/-1σ/12h
- **Frecuencia esperada**: 20–40 trades/mes/símbolo

### 7.2 Cross-sectional momentum (XS-Mom)
- **Timeframe**: 1d (rebalanceo diario)
- **Universo**: 8 símbolos
- **Setup**: rankea por retorno 7d. Long top-2, short bottom-2 (en perp). Equal-weight.
- **Filtro régimen**: solo en régimen `trending` (alta dispersión cross-sectional)
- **ML meta**: predice si la rotación gana neto fees vs holding
- **Exit**: rebalanceo diario o stop -5% por leg
- **Frecuencia**: ~30 rebalances/mes

### 7.3 Breakout (BO)
- **Timeframe**: 1h
- **Universo**: 8 símbolos
- **Setup**: Donchian(20) breakout + volume(1.5×avg20) + ATR percentile > 70 (expansión vol)
- **Filtro régimen**: `trending` o `high_vol`
- **ML primario**: predice continuación 4–24h
- **Exit**: trailing stop 2×ATR, time stop 48h
- **Frecuencia**: 5–15 trades/mes/símbolo

### 7.4 Funding carry (FC)
- **Timeframe**: 8h (alineado a payout)
- **Universo**: BTC, ETH (más líquidos para hedge)
- **Setup**: si funding rate anualizado > +20% (long perp pagando) → short perp + long spot (capture funding, delta-neutral). Si < -20% → reverse.
- **Exit**: cuando funding regresa a ±5% anualizado o tras 7 días
- **Riesgo**: basis risk + fee de fund spot. Modelado en backtest.
- **Frecuencia**: 2–5 setups/mes

### 7.5 Basis spot-perp (BSP)
- **Timeframe**: 15m
- **Universo**: BTC, ETH, SOL
- **Setup**: basis (perp − spot) / spot > +0.3% (contango exagerado) → short perp + long spot. Inversa para backwardation.
- **Exit**: convergencia <0.05% o tiempo expira (perp settlement / 24h).
- **Frecuencia**: 5–15 setups/mes
- **Caveat**: bajo capital, la fee de transferir entre spot y perp wallet erosiona márgenes — requiere balance pre-asignado en ambos.

### 7.6 Stat-arb pairs (SA)
- **Timeframe**: 1h
- **Universo**: pares cointegrados — ETH/BTC, SOL/BTC, AVAX/SOL, LINK/MATIC (validar cointegración mensualmente vía Johansen test)
- **Setup**: z-score del residual > +2 → short leader, long laggard. < -2 → reverso.
- **ML meta**: filtra falsos quiebres de cointegración
- **Exit**: z-score regresa a 0 o stop a |z| > 4
- **Frecuencia**: 3–8 trades/mes/par

### 7.7 Correlaciones esperadas (target post-construcción)

Matriz objetivo de correlación de retornos diarios entre estrategias:

| | MR | XS-Mom | BO | FC | BSP | SA |
|---|---|---|---|---|---|---|
| MR | 1.0 | -0.2 | -0.3 | 0.1 | 0.0 | 0.2 |
| XS-Mom | | 1.0 | 0.5 | 0.0 | 0.0 | -0.1 |
| BO | | | 1.0 | 0.0 | -0.1 | -0.2 |
| FC | | | | 1.0 | 0.3 | 0.0 |
| BSP | | | | | 1.0 | 0.0 |
| SA | | | | | | 1.0 |

Si una estrategia muestra correlación >0.7 con otra en OOS, se elimina la peor o se fusionan. **Suma de Sharpes correlacionados ≠ Sharpe combinado** — esto es el corazón del enfoque.

---

## 8. Régimen de mercado (L4)

### 8.1 Definición de estados

```
states = {
    'bull_trending',    # tendencia alcista clara
    'bear_trending',    # tendencia bajista clara
    'ranging_lowvol',   # consolidación baja vol
    'ranging_highvol',  # choppy, vol alta
    'crash',            # cola izquierda extrema
    'rally'             # cola derecha extrema
}
```

### 8.2 Detector — HMM Gaussiano

Features de entrada al HMM:
- Realized vol 24h log
- Retorno BTC 7d
- Funding rate medio 24h
- ATR percentile rolling 30d

Entrenamos un HMM de 4–6 estados ocultos sobre 36 meses de data BTC. Asignamos manualmente cada estado oculto a una de las 6 categorías post-hoc por características.

**Actualización**: re-fit mensual, predicción Viterbi cada hora.

### 8.3 Asignación estrategia × régimen

| Régimen | MR | XS-Mom | BO | FC | BSP | SA |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| bull_trending | ⊘ | ✓ | ✓ | ✓ | ✓ | ✓ |
| bear_trending | ⊘ | ✓ | ✓ | ✓ | ✓ | ✓ |
| ranging_lowvol | ✓ | ⊘ | ⊘ | ✓ | ✓ | ✓ |
| ranging_highvol | ✓ | ⊘ | ✓ | ✓ | ✓ | ✓ |
| crash | ⊘ | ⊘ | ⊘ | ⊘ | ⊘ | ⊘ | ← circuit-breaker global
| rally | ⊘ | ✓ | ✓ | ⊘ | ✓ | ⊘ |

⊘ = disabled, ✓ = enabled. Estos pesos arrancan binarios; pasan a continuos (0–1) con datos OOS.

---

## 9. Portfolio & risk (L5)

### 9.1 Allocator

**Algoritmo**: Hierarchical Risk Parity (HRP) de López de Prado sobre la matriz de covarianza de retornos por estrategia, **rolling 60 días**.

- Sin invertir matriz (estable a pocos datos).
- Asigna pesos $w_i$ tal que cada estrategia contribuye igual al riesgo total.

**Pesos finales por trade**:
$$\text{size}_i = w_i \times \text{kelly}_i \times \text{regime}_{i,r} \times \text{meta}_i$$

### 9.2 Kelly fractional

Por estrategia, sobre últimos 50 trades cerrados:
$$f_i = \text{shrink}\left( \hat{p}_i - \frac{1-\hat{p}_i}{\hat{b}_i},\ \text{toward}\ 0,\ \alpha = 1 - \frac{n}{100} \right)$$

Cap absoluto: **0.25 × Kelly** (¼-Kelly) y máximo 2% del equity por trade.

### 9.3 Riesgo agregado

| Métrica | Límite | Acción al violar |
|---|---|---|
| Gross exposure | ≤ 100% del equity (no leverage en fase 1) | Bloquear nuevas entries |
| VaR 95% diario | ≤ 5% del equity | Reducir tamaños globales 50% |
| CVaR 95% diario | ≤ 8% del equity | Reducir tamaños globales 50% |
| Daily drawdown | −3% intra-día | Circuit breaker total 24h |
| Drawdown semanal | −7% acumulado | Pausar trading + análisis |
| Correlación intra-portfolio | max pairwise <0.7 | No abrir trade si correl con posiciones abiertas >0.7 |
| Max posiciones simultáneas | 5 | Cola hasta que cierre alguna |

### 9.4 Sizing con leverage (fase 3+, solo en perp para FC/BSP)

Hasta 2× en estrategias delta-neutral (funding, basis). Nunca leverage en direccionales hasta capital >$10k.

---

## 10. Execution (L6)

### 10.1 Smart router

Para cada orden:

1. **Default**: limit post-only at best-bid (long) o best-ask (short). Si rechaza por crosspread, retry 1 tick adentro.
2. **Cancel/replace cada 30s** si no fillea: avanza 1 tick hacia el mid.
3. **Conversión a taker** si no fillea en 5 min Y la estrategia es time-sensitive (MR, BO). FC/BSP/SA esperan indefinidamente hasta fill o cancelación por nueva señal.
4. **Slippage budget**: si slippage realizado >2× expectativa del backtest en 20 trades consecutivos → alerta + freeze ejecución para revisión.

### 10.2 Transaction Cost Analysis (TCA)

Por cada trade ejecutado, log:
- Implementation shortfall vs decision price
- Comisión real (Binance VIP tier aware)
- Mercado vs limit fill
- Tiempo a fill
- Latencia round-trip

Reporte semanal en Grafana. Slippage acumulado >0.5% sobre el modelado = pause + investigation.

### 10.3 Smart routing entre exchanges (fase 3)

Cuando capital >$5k, evaluar enrutar a Bybit/OKX si:
- Spread más estrecho > diferencia de fee
- Latencia mejor
- Liquidez para tamaño de orden

Hasta entonces, solo Binance.

---

## 11. Plan de implementación (16 semanas)

### 11.0 Pre-trabajo (días 1–2)
- [ ] Parar bot V6 actual de forma limpia (commit estado, `systemctl stop` si activo, kill PID).
- [ ] Crear branch `feature/quant-platform-v1` desde `main`.
- [ ] Limpiar working tree: stash o commit los 25 archivos sucios bajo `wip/v7-experiments`.
- [ ] Documentar config V6 final en `docs/v6-final-config.md` para rollback.

### 11.1 Fase 1 — Infraestructura de datos (semanas 1–3)

**Objetivos:**
- TimescaleDB corriendo en Docker compose
- Ingesta WS de 8 símbolos × multi-TF guardando en TimescaleDB
- Captura de orderbook L2 top-20 (snapshots cada 1s)
- Captura de funding rates cada 8h
- Backfill histórico 24 meses desde Binance REST + Tardis para OB

**Gates:**
- [ ] Test de integridad: `count(*)` por símbolo/TF coincide con expectativa
- [ ] Zero data gaps en última semana de ingesta live
- [ ] Latencia ingesta WS→DB <500ms p99

### 11.2 Fase 2 — Feature store (semanas 3–5)

**Objetivos:**
- 60–80 features implementadas con tests
- Feature registry funcional
- Compute pipeline: bulk (histórico) + streaming (live)
- Tests de no-leakage automáticos

**Gates:**
- [ ] 100% features con docstring de hipótesis económica
- [ ] 100% features con test unitario
- [ ] Backtest de generación: compute 2 años × 8 símbolos en <30 min

### 11.3 Fase 3 — ML pipeline (semanas 5–8)

**Objetivos:**
- Triple-barrier labeling implementado
- Purged k-fold + walk-forward harness
- Entrenar primer LightGBM por estrategia (MR primero)
- Meta-labeling funcionando

**Gates:**
- [ ] OOS Sharpe walk-forward de MR ≥ 0.8 (antes de costes)
- [ ] DSR de MR ≥ 0.95
- [ ] Predicciones calibradas (Brier score < 0.22)

### 11.4 Fase 4 — Estrategias 2–6 (semanas 8–11)

Replicar §11.3 para XS-Mom, BO, FC, BSP, SA. Cada una pasa los mismos gates antes de continuar.

**Gate por estrategia**: OOS Sharpe ≥ 0.5 individual (las correlaciones bajas hacen que un Sharpe individual modesto sea valioso).

### 11.5 Fase 5 — Régimen + portfolio (semanas 11–13)

**Objetivos:**
- HMM entrenado y validado vs eventos conocidos (May 2022 LUNA, Nov 2022 FTX, Mar 2024 ATH, etc.)
- Allocator HRP funcionando
- Kelly fractional + caps + circuit breakers
- Backtest portfolio completo 24 meses con todas las estrategias

**Gates:**
- [ ] Portfolio Sharpe **anualizado** ≥ 1.5 OOS
- [ ] Max DD ≤ 15%
- [ ] ≥ 200 trades OOS
- [ ] Bate buy-and-hold BTC en ≥70% ventanas 90d rolling
- [ ] DSR del portfolio ≥ 0.95

### 11.6 Fase 6 — Execution + observability (semanas 13–15)

**Objetivos:**
- Smart router operativo en testnet
- Prometheus + Grafana dashboards
- Telegram alertas + dead-man's switch (5 min)
- TCA reportando

**Gates:**
- [ ] Slippage realizado en testnet ≤ 1.5× modelo
- [ ] Tasa de fills post-only ≥ 60%
- [ ] Dead-man's switch dispara correctamente al matar el proceso

### 11.7 Fase 7 — Paper forward-test (semanas 15–19)

**90 días de paper trading** con todo el stack. No empezar hasta gates de fase 5 pasados.

**Gates para producción real:**
- [ ] ≥ 100 trades paper
- [ ] Live Sharpe dentro de ±30% del backtest equivalente
- [ ] Slippage realizado consistente con modelo
- [ ] Cero crashes que requieran intervención manual
- [ ] Drawdown paper ≤ backtest max DD + 30%

### 11.8 Fase 8 — Deployment real escalonado

El usuario tiene $500 disponibles desde el inicio. La estrategia de ramp expone gradualmente capital real, manteniendo el resto en reserva fuera del exchange hasta que cada etapa valide.

| Etapa | Capital live (en exchange) | Reserva (off-exchange) | Duración | Criterio para avanzar |
|---|---|---|---|---|
| 8.1 | $50 | $450 | 30 días | Live Sharpe ≥70% paper Sharpe |
| 8.2 | $150 | $350 | 30 días | Drawdown live ≤ backtest |
| 8.3 | $300 | $200 | 60 días | ≥50 trades live, métricas en rango |
| 8.4 | $500 (capital inicial completo) | $0 | indefinido | — |
| 8.5+ | +$500/quarter (aporte nuevo del usuario) | — | indefinido | Mantener todas las métricas |

Cualquier degradación >1σ sostenida 7 días → freeze scaling, investigar.

---

## 12. Criterios go/no-go por fase

Cada fase tiene gates duros. **NO se avanza** sin pasarlos. Documentar en `docs/gates_log.md` cada paso o falla.

Adicionalmente, gate global de deployment real:
1. Walk-forward OOS aggregated Sharpe ≥ 1.5
2. ≥ 200 OOS trades
3. WR 95% CI lower bound > break-even fee-adjusted
4. Max DD < 15% sobre walk-forward completo
5. Profitable en ≥ 5 de 8 símbolos
6. Paper forward-test 90d dentro de ±30% del backtest
7. DSR ≥ 0.95 para portfolio agregado

---

## 13. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Overfitting masivo (muchos features, pocos trades) | Alta | Catastrófico | Purged k-fold + DSR + walk-forward + cap de #features por modelo (≤20 después de selección) |
| Datos on-chain con lag mata el alfa | Media | Alto | Modelar lag explícitamente; tener estrategias que no dependan de on-chain |
| Free tier APIs rate-limit en producción | Media | Medio | Cache agresivo + presupuestar pago tier modesto desde fase 2 ($30/mes) |
| Binance testnet ≠ producción (liquidez, fees) | Alta | Medio | Paper trading en mainnet con $50 antes de scaling |
| Drawdown psicológicamente intolerable | Media | Alto | Circuit breakers automáticos + dashboards calmados + reporting diario |
| Black swan no modelado (FTX, LUNA) | Baja-media | Catastrófico | Crash regime detector + position kill switch + correlación gate |
| Bug en order placement con dinero real | Media | Catastrófico | TDD obligatorio + safety-invariants-audit + ramp escalonado |
| Cambio de fees Binance | Alta (anual) | Bajo | Cost model parametrizado, alerta al cambiar |
| Concept drift no detectado | Alta | Alto | Drift detector en producción + retraining mensual |
| Latencia/ws drops | Media | Medio | Redundancia Bybit + reconciliación cada minuto |

---

## 14. Equipo y tiempo realista

**Desarrollador:** Claude (sesión autónoma o subagentes), dirigido por Marcos.

**Tiempo de elapsed:** 16 semanas en agenda, asumiendo:
- 2–4 horas/día efectivas de trabajo de Claude
- Marcos disponible para gates y decisiones críticas (1h/día promedio)
- Sin bloqueos externos críticos

**Costes operativos mensuales (estimados):**
- VPS GCP e2-small (escalado de e2-micro): $15/mes
- Glassnode Advanced (si free tier insuficiente): $30/mes — diferir hasta validar necesidad
- Tardis.dev (después de captura propia): $0 (capturamos nosotros)
- LunarCrush: $0 (free tier alcanza)
- Sentry: $0 (free tier)
- **Total**: $15–$45/mes

---

## 15. Anexo — bibliografía y referencias

- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. — Triple barrier, meta-labeling, purged k-fold, DSR.
- López de Prado, M. (2020). *Machine Learning for Asset Managers*. Cambridge UP. — Feature importance, hierarchical clustering.
- Chan, E. (2013). *Algorithmic Trading*. Wiley. — Mean-reversion y pairs trading.
- Pardo, R. (2008). *The Evaluation and Optimization of Trading Strategies*. Wiley. — Walk-forward methodology.
- Moskowitz, T., Ooi, Y., Pedersen, L. (2012). "Time series momentum". *J. Financial Economics*. — XS-Mom evidence.
- Easley, D. et al. (2012). "Flow toxicity and liquidity in a high-frequency world". *Review of Financial Studies*. — VPIN / microstructure.
- Avellaneda, M., Lee, J. (2010). "Statistical arbitrage in the US equities market". *Quantitative Finance*. — Stat-arb framework.
- MacLean, Thorp, Ziemba (2011). *The Kelly Capital Growth Investment Criterion*. World Scientific. — Fractional Kelly.

---

## 16. Apéndice — fórmulas clave

- **Triple barrier label**: $L_t = \text{sign}(\text{first hit})$ con barreras vol-scaled.
- **Wilson CI sobre WR**: $\text{CI}_{95} = \frac{p + z^2/2n \pm z\sqrt{p(1-p)/n + z^2/4n^2}}{1 + z^2/n}$
- **Kelly fraction**: $f^* = p - (1-p)/b$, $b = W/L$.
- **DSR (Deflated Sharpe)**: $\text{DSR} = \Phi\left( \frac{(\text{SR} - \text{SR}_0)\sqrt{N-1}}{\sqrt{1 - \gamma_3 \text{SR} + (\gamma_4 - 1)\text{SR}^2/4}} \right)$
- **HRP weights**: recursive bisection sobre dendrograma de distancia $d_{ij} = \sqrt{(1 - \rho_{ij})/2}$.
- **VaR 95%**: percentil 5 de retornos simulados Monte Carlo del portfolio.
- **Funding annualized**: $(1 + r_{8h})^{3 \cdot 365} - 1$, donde $r_{8h}$ es la tasa por payout.

---

## 17. Definition of Done de la plataforma

Se considera **completada la implementación** cuando:

1. Todos los gates de fases 1–7 documentados como pasados en `docs/gates_log.md`.
2. Bot operando en modo paper sobre mainnet con $0 capital real durante 90 días, métricas dentro de tolerancia.
3. Suite de tests con ≥800 tests, cobertura ≥85% en `core/`, `risk/`, `execution/`, `ml/`, `portfolio/`.
4. Dashboards Grafana mostrando todas las métricas L5–L7 en tiempo real.
5. Documentación operativa en `docs/runbook.md`: cómo arrancar, parar, recuperar, escalar.
6. Postmortem de cualquier incidente durante paper en `docs/incidents/`.

Una vez completado, se entra en fase 8 (deployment real escalonado).
