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
    """Return 'stop_loss', 'take_profit', or None based on pct from entry."""
    change = (close - entry_price) / entry_price
    if change <= -stop_loss_pct:
        return 'stop_loss'
    if change >= take_profit_pct:
        return 'take_profit'
    return None


def check_exit_price(
    close: float,
    sl_price: float,
    tp_price: float,
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None based on absolute prices.

    Used whenever the position carries precomputed SL/TP levels (ATR-based or
    trailing-adjusted). Keeps exit logic in one place and unit-testable.
    """
    if close <= sl_price:
        return 'stop_loss'
    if close >= tp_price:
        return 'take_profit'
    return None


def calc_pnl(
    close: float,
    entry_price: float,
    qty: float,
) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct) for a long position."""
    pnl_usdt = (close - entry_price) * qty
    pnl_pct = (close - entry_price) / entry_price * 100
    return pnl_usdt, pnl_pct


def should_enter_mean_rev(drop_pct: float, threshold: float = 0.015) -> bool:
    """Return True when pct_change(lookback) crossed below -threshold.
    drop_pct is the precomputed fractional change (negative = price fell)."""
    return drop_pct <= -threshold


def update_trailing_stop(
    sl_price: float,
    entry_price: float,
    tp_price: float,
    close: float,
    atr_val: float,
) -> float:
    """Return the new SL price after applying the three-stage trailing rule.

    Stages (based on progress from entry towards TP):
      >= 50 %  → move SL to breakeven (entry_price)
      >= 75 %  → trail SL at 1 × ATR below *close*

    The SL only ever moves up — this function returns max(sl_price, new_sl).
    """
    if tp_price <= entry_price:
        return sl_price
    progress = (close - entry_price) / (tp_price - entry_price)
    new_sl = sl_price
    if progress >= 0.75:
        new_sl = max(new_sl, close - atr_val)
    elif progress >= 0.50:
        new_sl = max(new_sl, entry_price)
    return new_sl


def should_enter_short(
    close: float,
    sma20: float,
    rsi14: float,
    rsi_threshold: float = 55.0,
    volume: float | None = None,
    volume_sma20: float | None = None,
    volume_factor: float = 1.2,
) -> bool:
    """Mirror of should_enter for short entries.

    Asymmetric profile (Option B) default threshold 55 — short side aggressive
    while long side stays at the validated <40 (see config.RSI_LONG_THRESHOLD).
    """
    if rsi14 <= rsi_threshold or close >= sma20:
        return False
    if volume is not None and volume_sma20 is not None:
        if volume <= volume_sma20 * volume_factor:
            return False
    return True


def passes_regime_filters(
    trend_bullish: bool | None,
    adx_val: float | None,
    adx_threshold: float = 25.0,
    use_trend_filter: bool = True,
    use_adx_filter: bool = True,
) -> bool:
    """Return True when the higher-TF trend and ADX regime allow an entry.

    - trend_bullish=None skips the trend check (useful for the live bot until
      a 1 h candle stream is wired).
    - adx_val=None skips the ADX check (useful on the very first bars of a run
      before ADX is defined).
    """
    if use_trend_filter and trend_bullish is not None and not trend_bullish:
        return False
    if use_adx_filter and adx_val is not None and adx_val >= adx_threshold:
        return False
    return True
