"""Pure signal functions — no side effects, no I/O."""


def should_exit_time(entry_ts_ms: int, now_ms: int, max_hold_hours: float) -> bool:
    """True when the position has been open longer than max_hold_hours."""
    if max_hold_hours <= 0:
        return False
    return (now_ms - entry_ts_ms) >= max_hold_hours * 3_600_000


def near_miss_reason(
    close: float,
    sma20: float,
    rsi14: float,
    rsi_long_threshold: float,
    rsi_short_threshold: float,
    rsi_band: float,
    sma_band_frac: float,
) -> str | None:
    """Return a short reason string if entry conditions are CLOSE but not met.

    Long needs rsi < rsi_long_threshold AND close > sma20.
    Short needs rsi > rsi_short_threshold AND close < sma20.
    Returns None when not in any near-miss band.
    """
    sma_dist_frac = abs(close - sma20) / sma20 if sma20 > 0 else 1.0
    rsi_to_long = rsi14 - rsi_long_threshold
    rsi_to_short = rsi_short_threshold - rsi14

    if 0 <= rsi_to_long <= rsi_band and close > sma20:
        return f'long near-miss: rsi {rsi14:.1f} above {rsi_long_threshold:.1f} by {rsi_to_long:.1f}'
    if rsi14 < rsi_long_threshold and 0 < (sma20 - close) <= sma20 * sma_band_frac:
        return f'long near-miss: close {close:.2f} below sma20 {sma20:.2f} ({sma_dist_frac * 100:.2f}%)'
    if 0 <= rsi_to_short <= rsi_band and close < sma20:
        return f'short near-miss: rsi {rsi14:.1f} below {rsi_short_threshold:.1f} by {rsi_to_short:.1f}'
    if rsi14 > rsi_short_threshold and 0 < (close - sma20) <= sma20 * sma_band_frac:
        return f'short near-miss: close {close:.2f} above sma20 {sma20:.2f} ({sma_dist_frac * 100:.2f}%)'
    return None


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
    side: str = 'long',
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None based on absolute prices.

    Long convention: sl_price < entry < tp_price; SL trips when close <= sl_price.
    Short convention: tp_price < entry < sl_price; SL trips when close >= sl_price.
    """
    if side == 'long':
        if close <= sl_price:
            return 'stop_loss'
        if close >= tp_price:
            return 'take_profit'
        return None
    if side == 'short':
        if close >= sl_price:
            return 'stop_loss'
        if close <= tp_price:
            return 'take_profit'
        return None
    raise ValueError(f'invalid side: {side!r}')


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


def calc_pnl_short(
    close: float,
    entry_price: float,
    qty: float,
) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct) for a short position.

    Positive when close < entry (price fell after we sold short).
    """
    pnl_usdt = (entry_price - close) * qty
    pnl_pct = (entry_price - close) / entry_price * 100
    return pnl_usdt, pnl_pct


def check_exit_short(
    close: float,
    entry_price: float,
    stop_loss_pct: float = 0.035,
    take_profit_pct: float = 0.06,
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None for a short.

    SL trips when price rises above entry; TP trips when price falls below.
    Aggressive-profile defaults (3.5% / 6%) per spec v3.
    """
    change = (entry_price - close) / entry_price
    if change <= -stop_loss_pct:
        return 'stop_loss'
    if change >= take_profit_pct:
        return 'take_profit'
    return None


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


def update_trailing_stop_short(
    sl_price: float,
    entry_price: float,
    tp_price: float,
    close: float,
    atr_val: float,
) -> float:
    """Mirror of update_trailing_stop for short positions.

    Short SLs sit ABOVE entry. Tightening means moving the SL DOWN toward entry.
    Stages (progress from entry toward TP):
      >= 50%  → move SL to breakeven (entry_price)
      >= 75%  → trail SL at 1 × ATR above *close*
    SL only ever moves down — returns min(sl_price, new_sl).
    """
    if tp_price >= entry_price:
        return sl_price
    progress = (entry_price - close) / (entry_price - tp_price)
    new_sl = sl_price
    if progress >= 0.75:
        new_sl = min(new_sl, close + atr_val)
    elif progress >= 0.50:
        new_sl = min(new_sl, entry_price)
    return new_sl


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
