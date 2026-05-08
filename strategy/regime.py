"""Regime / market-condition helpers (range detection, ATR percentile, MTF alignment).

Pure functions — accept primitive inputs, return primitives. No I/O.
"""
from __future__ import annotations

from collections.abc import Sequence


def is_quiet_range(
    prices: Sequence[float],
    range_pct_threshold: float = 0.003,
) -> bool:
    """True when the (max-min)/min over the window is below threshold.

    Used to detect a flat market where opening a new position is likely to
    stall: the typical caller passes the last ~120 1m closes (≈2 hours) and
    a 0.3 % threshold.
    """
    if len(prices) < 2:
        return False
    lo = min(prices)
    hi = max(prices)
    if lo <= 0:
        return False
    return (hi - lo) / lo < range_pct_threshold


def is_position_stalled(
    closes_during_position: Sequence[float],
    move_pct_threshold: float = 0.005,
) -> bool:
    """True when the maximum absolute move from the first close is below threshold.

    Used to decide whether to tighten SL/TP on a position that has gone
    nowhere for a long time. Caller passes closes from entry up to now.
    """
    if not closes_during_position:
        return False
    base = closes_during_position[0]
    if base <= 0:
        return False
    max_abs_move = max(abs(c - base) / base for c in closes_during_position)
    return max_abs_move < move_pct_threshold


def atr_percentile_bounds(
    atr_history: Sequence[float],
    low_p: float = 20.0,
    high_p: float = 80.0,
) -> tuple[float, float] | None:
    """Return (low, high) ATR thresholds at the given percentiles, or None.

    None is returned if the history is empty or shorter than ~30 samples
    (caller should treat this as "filter inactive — warmup").
    """
    cleaned = [v for v in atr_history if v is not None and v > 0]
    if len(cleaned) < 30:
        return None
    sorted_vals = sorted(cleaned)
    n = len(sorted_vals)
    lo_idx = max(0, min(n - 1, int(round(n * low_p / 100.0)) - 1))
    hi_idx = max(0, min(n - 1, int(round(n * high_p / 100.0)) - 1))
    return sorted_vals[lo_idx], sorted_vals[hi_idx]


def passes_volatility_window(
    atr_now: float | None,
    bounds: tuple[float, float] | None,
) -> bool:
    """True when ATR sits inside the [low, high] window or filter is inactive."""
    if bounds is None or atr_now is None:
        return True
    low, high = bounds
    return low <= atr_now <= high


def is_mtf_aligned(
    side: str,
    htf_bullish_15m: bool | None,
    htf_bullish_1h: bool | None,
    require_15m: bool = True,
    require_1h: bool = False,
) -> bool:
    """Return True when the higher timeframes agree with the proposed side.

    None values mean the higher-TF flag is not yet defined (warmup) and the
    check is skipped — never blocks during early warmup.
    """
    want = side == 'long'
    if require_15m and htf_bullish_15m is not None and htf_bullish_15m != want:
        return False
    if require_1h and htf_bullish_1h is not None and htf_bullish_1h != want:
        return False
    return True


def passes_short_trend_filter(
    close: float,
    sma50: float | None,
    adx_val: float | None,
    adx_min: float,
) -> bool:
    """Short-specific gate: only allow short when ADX>min AND close<sma50.

    None values for sma50/adx_val skip the respective check (warmup).
    """
    if sma50 is not None and close >= sma50:
        return False
    if adx_val is not None and adx_val < adx_min:
        return False
    return True


def shorts_disabled_in_flat(
    adx_val: float | None,
    adx_flat_threshold: float,
) -> bool:
    """True when the market is too flat for shorts (ADX below the cutoff).

    None ADX (warmup) returns False — don't block during warmup.
    """
    if adx_val is None:
        return False
    return adx_val < adx_flat_threshold
