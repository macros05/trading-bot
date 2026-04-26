"""Runtime config for the live bot and the backtest harness.

`BOT_CONFIG` is the dict consumed by `core.loop`; the UPPER_SNAKE constants
below are the canonical source and are imported directly by backtest / sizing
code. Keep both in sync.
"""

# ── Risk & sizing (AGGRESSIVE PROFILE — see docs/superpowers/specs/2026-04-26-short-positions-design.md)
RISK_PCT = 0.02            # was 0.01
STOP_LOSS_PCT = 0.035      # was 0.02
TAKE_PROFIT_PCT = 0.060    # was 0.03

# ── Leverage (futures only) ──────────────────────────────────────────────────
LEVERAGE = 2               # new — applied at init via exchange.set_leverage()

# ── ATR-based exits (live: OFF; ATR(1m) sized stops are too tight, see §13.2)
USE_ATR_EXITS = False
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0

# ── Regime filters (V6 deploy: ADX live, trend deferred to V7) ───────────────
USE_TREND_FILTER = False   # 1 h EMA200 — needs 1h candle stream (V7 follow-up)
USE_ADX_FILTER = True      # ✅ V6 champion: ADX < 45 active live (paper)
ADX_PERIOD = 14
ADX_THRESHOLD = 45.0       # V6 champion (multi-symbol p<0.01, see IMPROVEMENT_PLAN §39)
HTF_TREND_TF = '1h'
HTF_TREND_EMA_PERIOD = 200

# ── Trailing stop ────────────────────────────────────────────────────────────
# V6 finding (§13.3): trailing reduces Sharpe when regime filters are active —
# already-filtered trades are high-quality, trailing clips winners early.
USE_TRAILING_STOP = False

# ── Circuit breaker ──────────────────────────────────────────────────────────
CIRCUIT_BREAKER_PCT = 0.05 # was 0.03

# ── Signal thresholds ────────────────────────────────────────────────────────
RSI_LONG_THRESHOLD = 45.0  # was 35.0 (loop hardcoded; config had no constant)
RSI_SHORT_THRESHOLD = 55.0 # new — short mirror

# ── Protections (permissive aggressive defaults — framework present, not active)
COOLDOWN_SECONDS = 0       # new — 0 = disabled
MAX_SL_PER_DAY = 10        # new — effectively disabled

# ── Backtest-only cost model ─────────────────────────────────────────────────
TAKER_FEE = 0.001          # 0.10 % per side (Binance spot taker)
SLIPPAGE = 0.0005          # 0.05 % per side, conservative estimate

# ── Live loop config (consumed by core.loop) ─────────────────────────────────
BOT_CONFIG = {
    'symbol':               'BTC/USDT',
    'timeframe':            '1m',
    'limit':                200,
    'interval_seconds':     60,
    'paper_balance':        10_000.0,
    'risk_pct':             RISK_PCT,
    'rsi_threshold':        RSI_LONG_THRESHOLD,
    'rsi_short_threshold':  RSI_SHORT_THRESHOLD,
    'stop_loss_pct':        STOP_LOSS_PCT,
    'take_profit_pct':      TAKE_PROFIT_PCT,
    'circuit_breaker_pct':  CIRCUIT_BREAKER_PCT,
    'leverage':             LEVERAGE,
    'cooldown_seconds':     COOLDOWN_SECONDS,
    'max_sl_per_day':       MAX_SL_PER_DAY,
    'use_atr_exits':        USE_ATR_EXITS,
    'atr_period':           ATR_PERIOD,
    'atr_sl_multiplier':    ATR_SL_MULTIPLIER,
    'atr_tp_multiplier':    ATR_TP_MULTIPLIER,
    'use_trailing_stop':    USE_TRAILING_STOP,
    'use_adx_filter':       USE_ADX_FILTER,
    'adx_period':           ADX_PERIOD,
    'adx_threshold':        ADX_THRESHOLD,
    'use_trend_filter':     USE_TREND_FILTER,
}
