"""Runtime config for the live bot and the backtest harness.

`BOT_CONFIG` is the dict consumed by `core.loop`; the UPPER_SNAKE constants
below are the canonical source and are imported directly by backtest / sizing
code. Keep both in sync.
"""

# ── Risk & sizing (v3 ASYMMETRIC PROFILE — see docs/superpowers/specs/2026-04-26-short-positions-design.md)
RISK_PCT = 0.02

# Per-side SL/TP — restored to working v3 values now that volatility,
# range and MTF filters prevent the bot from sitting in a flat regime
# (the original reason for the May-2026 1.2/1.8 tightening).
STOP_LOSS_PCT_LONG = 0.025
STOP_LOSS_PCT_SHORT = 0.035
TAKE_PROFIT_PCT_LONG = 0.040
TAKE_PROFIT_PCT_SHORT = 0.060

# Time-based exit fallback — used as a last resort. With the new range and
# stalled-position detection, most sideways trades should exit before this.
MAX_HOLD_HOURS = 12

# Stalled-position tightening: when a position has been open this long
# without moving more than STALLED_MOVE_THRESHOLD in either direction,
# halve the SL/TP distances to force a faster exit.
STALLED_HOURS = 6.0
STALLED_MOVE_THRESHOLD = 0.005   # 0.5 % from entry in any direction

# ── Range / quiet-market detection ───────────────────────────────────────────
# Avoid opening when the last RANGE_LOOKBACK_MIN bars span less than this
# fraction of price — typical sideways-grind regime BTC was stuck in.
RANGE_LOOKBACK_MIN = 120          # 2 hours of 1m bars
RANGE_PCT_THRESHOLD = 0.003       # 0.3 %

# ── Leverage ─────────────────────────────────────────────────────────────────
LEVERAGE = 2

# ── ATR-based exits ──────────────────────────────────────────────────────────
USE_ATR_EXITS = False
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0

# ── Volatility window filter ─────────────────────────────────────────────────
# Skip entries when ATR(14, 1m) sits outside the [P20, P80] band of the last
# VOLATILITY_LOOKBACK_HOURS hours. Filters out both dead and chaotic regimes.
USE_VOLATILITY_FILTER = True
VOLATILITY_LOOKBACK_HOURS = 48
VOLATILITY_LOW_PERCENTILE = 20.0
VOLATILITY_HIGH_PERCENTILE = 80.0

# ── Regime filters ───────────────────────────────────────────────────────────
USE_TREND_FILTER = False          # legacy 1h EMA200 — superseded by MTF below
USE_ADX_FILTER = True
ADX_PERIOD = 14
ADX_THRESHOLD = 45.0              # block entries when ADX >= this (over-trended/whippy)
HTF_TREND_TF = '1h'
HTF_TREND_EMA_PERIOD = 200

# ── Multi-timeframe alignment ────────────────────────────────────────────────
USE_MTF_FILTER = True
MTF_15M_PERIOD = 50
MTF_REQUIRE_15M = True
MTF_REQUIRE_1H = False            # 1h alignment is harder to satisfy and not validated yet

# ── Short-side guards ────────────────────────────────────────────────────────
# Even when long is allowed in flat markets, shorts get arrested.
USE_SHORT_TREND_FILTER = True
SHORT_ADX_MIN = 20.0              # require ADX >= 20 to allow short entry
SHORT_SMA_PERIOD = 50             # require close < SMA50 to allow short
ADX_FLAT_THRESHOLD = 18.0         # ADX below this disables shorts entirely

# ── Trailing stop (percentage-based, V7) ────────────────────────────────────
USE_TRAILING_STOP = True
TRAILING_BREAKEVEN_AT_PCT = 0.008  # +0.8 % unrealized → SL to breakeven
TRAILING_TRAIL_AT_PCT = 0.012      # +1.2 % unrealized → start dynamic trail
TRAILING_DISTANCE_PCT = 0.004      # trail SL 0.4 % behind price

# ── Adaptive Kelly ───────────────────────────────────────────────────────────
ADAPTIVE_KELLY = True

# ── Sessions ─────────────────────────────────────────────────────────────────
# Empty tuple = trade all sessions. Backtest output prints per-session metrics
# so this can be tuned to block the worst-performing session.
USE_SESSION_FILTER = True
BLOCKED_SESSIONS: tuple[str, ...] = ('off',)   # 21:00–24:00 UTC by default

# ── Circuit breaker ──────────────────────────────────────────────────────────
CIRCUIT_BREAKER_PCT = 0.05

# ── Signal thresholds ────────────────────────────────────────────────────────
RSI_LONG_THRESHOLD = 45.0
RSI_SHORT_THRESHOLD = 53.0

# ── Near-miss diagnostic alerts ──────────────────────────────────────────────
NEAR_MISS_RSI_BAND = 5.0
NEAR_MISS_SMA_BAND = 0.003

# ── Protections ──────────────────────────────────────────────────────────────
COOLDOWN_SECONDS = 0
MAX_SL_PER_DAY = 10

# ── Backtest cost model ──────────────────────────────────────────────────────
TAKER_FEE = 0.001
SLIPPAGE = 0.0005

# ── Live loop config (consumed by core.loop) ─────────────────────────────────
BOT_CONFIG = {
    'symbol':                   'BTC/USDT',
    'timeframe':                '1m',
    'limit':                    200,
    'interval_seconds':         60,
    'paper_balance':            10_000.0,
    'risk_pct':                 RISK_PCT,
    'rsi_threshold':            RSI_LONG_THRESHOLD,
    'rsi_short_threshold':      RSI_SHORT_THRESHOLD,
    'stop_loss_pct_long':       STOP_LOSS_PCT_LONG,
    'stop_loss_pct_short':      STOP_LOSS_PCT_SHORT,
    'take_profit_pct_long':     TAKE_PROFIT_PCT_LONG,
    'take_profit_pct_short':    TAKE_PROFIT_PCT_SHORT,
    'circuit_breaker_pct':      CIRCUIT_BREAKER_PCT,
    'leverage':                 LEVERAGE,
    'cooldown_seconds':         COOLDOWN_SECONDS,
    'max_sl_per_day':           MAX_SL_PER_DAY,
    'use_atr_exits':            USE_ATR_EXITS,
    'atr_period':               ATR_PERIOD,
    'atr_sl_multiplier':        ATR_SL_MULTIPLIER,
    'atr_tp_multiplier':        ATR_TP_MULTIPLIER,
    'use_trailing_stop':        USE_TRAILING_STOP,
    'trailing_breakeven_pct':   TRAILING_BREAKEVEN_AT_PCT,
    'trailing_trail_pct':       TRAILING_TRAIL_AT_PCT,
    'trailing_distance_pct':    TRAILING_DISTANCE_PCT,
    'use_adx_filter':           USE_ADX_FILTER,
    'adx_period':               ADX_PERIOD,
    'adx_threshold':            ADX_THRESHOLD,
    'use_trend_filter':         USE_TREND_FILTER,
    'max_hold_hours':           MAX_HOLD_HOURS,
    'stalled_hours':            STALLED_HOURS,
    'stalled_move_threshold':   STALLED_MOVE_THRESHOLD,
    'range_lookback_min':       RANGE_LOOKBACK_MIN,
    'range_pct_threshold':      RANGE_PCT_THRESHOLD,
    'use_volatility_filter':    USE_VOLATILITY_FILTER,
    'volatility_lookback_hours': VOLATILITY_LOOKBACK_HOURS,
    'volatility_low_pct':       VOLATILITY_LOW_PERCENTILE,
    'volatility_high_pct':      VOLATILITY_HIGH_PERCENTILE,
    'use_mtf_filter':           USE_MTF_FILTER,
    'mtf_15m_period':           MTF_15M_PERIOD,
    'mtf_require_15m':          MTF_REQUIRE_15M,
    'mtf_require_1h':           MTF_REQUIRE_1H,
    'use_short_trend_filter':   USE_SHORT_TREND_FILTER,
    'short_adx_min':            SHORT_ADX_MIN,
    'short_sma_period':         SHORT_SMA_PERIOD,
    'adx_flat_threshold':       ADX_FLAT_THRESHOLD,
    'use_session_filter':       USE_SESSION_FILTER,
    'blocked_sessions':         BLOCKED_SESSIONS,
    'adaptive_kelly':           ADAPTIVE_KELLY,
    'near_miss_rsi_band':       NEAR_MISS_RSI_BAND,
    'near_miss_sma_band':       NEAR_MISS_SMA_BAND,
    # Telegram: when False, only daily/weekly summaries and critical alerts
    # (circuit breaker, watchdog stale, daily reset) are sent. Per-trade
    # opens/closes, trailing transitions and near-miss diagnostics are silenced.
    'notify_per_trade':         False,
}
