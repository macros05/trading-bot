"""Classify the live market into trending / ranging / volatile.

Pure: takes ADX, ATR percentile and the close window, returns a label.
"""
from __future__ import annotations


def classify_regime(
    adx_val: float | None,
    atr_percentile: float | None,
    range_quiet: bool,
) -> str:
    """Return one of 'trending', 'ranging', 'volatile' or 'unknown'.

    - 'trending'  ADX > 25 and not range-quiet
    - 'volatile'  ATR percentile > 80 (top quintile of last 48h)
    - 'ranging'   ADX < 20 or range_quiet flag
    - 'unknown'   warmup (any input None)
    """
    if adx_val is None or atr_percentile is None:
        return 'unknown'
    if atr_percentile > 80.0:
        return 'volatile'
    if adx_val > 25.0 and not range_quiet:
        return 'trending'
    if adx_val < 20.0 or range_quiet:
        return 'ranging'
    return 'unknown'


def percentile_of(value: float | None, sorted_history: list[float]) -> float | None:
    """Return the percentile of *value* within sorted_history (linear, 0..100).

    Returns None if value is None or history is too short.
    """
    if value is None or len(sorted_history) < 30:
        return None
    n = len(sorted_history)
    # Bisect-style position
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_history[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return round(lo / n * 100.0, 2)
