"""Runtime config for the live bot and the backtest harness.

`BOT_CONFIG` is the dict consumed by `core.loop`; the UPPER_SNAKE constants
below are the canonical source and are imported directly by backtest / sizing
code. Keep both in sync.
"""

# ── Risk & sizing (v3 ASYMMETRIC PROFILE — see docs/superpowers/specs/2026-04-26-short-positions-design.md)
RISK_PCT = 0.02            # was 0.01

# Per-side SL/TP — low-vol regime tuning (May 2026):
# BTC has been pinned in $79.9k–80.1k for days; original 2.5/4 long
# and 3.5/6 short never close trades in this band, so the bot stalls in a
# single position for >48h. Tightened to 1.2/1.8 so a typical 1h–6h move is
# enough to close one cycle and free capital for the next signal.
STOP_LOSS_PCT_LONG = 0.012
STOP_LOSS_PCT_SHORT = 0.012
TAKE_PROFIT_PCT_LONG = 0.018
TAKE_PROFIT_PCT_SHORT = 0.018

# Time-based exit: close any position open longer than this regardless of PnL.
# Prevents zombie positions when BTC enters a tight range that hits neither
# SL nor TP (root cause of the 'no opera' incident May 2026).
MAX_HOLD_HOURS = 12

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
# Loosened from 40/55 because in the May-2026 low-vol regime RSI rarely
# crosses 40↓ or 55↑ — see backtest sweep in backtest/results/lowvol_*.json.
RSI_LONG_THRESHOLD = 45.0
RSI_SHORT_THRESHOLD = 53.0

# ── Near-miss diagnostic alerts ──────────────────────────────────────────────
# Fire a Telegram notification when entry conditions are *close* but not met,
# so the user can see why the bot isn't entering. Rate-limited in
# notifications.notify_near_miss to avoid spam.
NEAR_MISS_RSI_BAND = 5.0    # alert if RSI within this many points of either threshold
NEAR_MISS_SMA_BAND = 0.003  # alert if |close-sma|/sma within this fraction (0.3%)

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
    'stop_loss_pct_long':   STOP_LOSS_PCT_LONG,
    'stop_loss_pct_short':  STOP_LOSS_PCT_SHORT,
    'take_profit_pct_long': TAKE_PROFIT_PCT_LONG,
    'take_profit_pct_short': TAKE_PROFIT_PCT_SHORT,
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
    'max_hold_hours':       MAX_HOLD_HOURS,
    'near_miss_rsi_band':   NEAR_MISS_RSI_BAND,
    'near_miss_sma_band':   NEAR_MISS_SMA_BAND,
}
