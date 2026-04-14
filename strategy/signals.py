"""Pure signal functions — no side effects, no I/O."""


def should_enter(
    close: float,
    sma20: float,
    rsi14: float,
    rsi_threshold: float = 35.0,
    volume: float | None = None,
    volume_sma20: float | None = None,
    volume_factor: float = 1.2,
) -> bool:
    """Return True when all active entry conditions are met.

    Volume confirmation is optional: only applied when both *volume* and
    *volume_sma20* are provided. Requires volume > volume_sma20 * volume_factor.
    """
    if rsi14 >= rsi_threshold or close <= sma20:
        return False
    if volume is not None and volume_sma20 is not None:
        if volume <= volume_sma20 * volume_factor:
            return False
    return True


def check_exit(
    close: float,
    entry_price: float,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.03,
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None."""
    change = (close - entry_price) / entry_price
    if change <= -stop_loss_pct:
        return 'stop_loss'
    if change >= take_profit_pct:
        return 'take_profit'
    return None


def calc_pnl(
    close: float,
    entry_price: float,
    qty: float,
) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct) for a long position."""
    pnl_usdt = (close - entry_price) * qty
    pnl_pct  = (close - entry_price) / entry_price * 100
    return pnl_usdt, pnl_pct


def should_enter_mean_rev(drop_pct: float, threshold: float = 0.015) -> bool:
    """Return True when pct_change(lookback) crossed below -threshold.
    drop_pct is the precomputed fractional change (negative = price fell)."""
    return drop_pct <= -threshold
