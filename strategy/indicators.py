import numpy as np
import pandas as pd


def sma(df: pd.DataFrame, period: int) -> pd.Series:
    """Simple moving average of the 'close' column over *period* bars.

    Leading values where fewer than *period* bars are available are NaN.
    """
    return df['close'].rolling(window=period, min_periods=period).mean()


def ema(df_or_series, period: int) -> pd.Series:
    """Exponential moving average, Wilder-compatible (adjust=False).

    Accepts either a DataFrame (uses 'close') or a Series (used directly),
    so callers that already have a close series don't need to wrap it.
    """
    series: pd.Series = (
        df_or_series['close'] if isinstance(df_or_series, pd.DataFrame) else df_or_series
    )
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of the 'volume' column over *period* bars."""
    return df['volume'].rolling(window=period, min_periods=period).mean()


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index of the 'close' column over *period* bars.

    Wilder smoothing (equivalent to EMA with alpha=1/period).
    Returns NaN for the first *period* rows where the indicator is undefined.
    """
    delta: pd.Series = df['close'].diff()
    gain: pd.Series = delta.clip(lower=0)
    loss: pd.Series = (-delta).clip(lower=0)

    avg_gain: pd.Series = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss: pd.Series = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs: pd.Series = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range over *period* bars, Wilder smoothing.

    Requires 'high', 'low', 'close' columns. True Range is the max of:
      - high − low
      - |high − prev_close|
      - |low  − prev_close|
    NaN for the first *period* bars.
    """
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift()
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index, Wilder smoothing.

    Returns a smoothed DX series. Values >25 typically indicate a trending
    regime; <20 indicates a ranging regime. NaN for the first ~2·period bars.
    """
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift()

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr_w = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    # Avoid 0/0 when the bar has no range at all (constant high=low=close).
    safe_atr = atr_w.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / safe_atr

    di_sum = plus_di + minus_di
    # When DI+ and DI- both round to zero (extreme low-vol regimes — no
    # directional movement), define DX = 0 (no measurable trend strength)
    # rather than NaN, so the smoothed ADX stays defined.
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    dx = dx.where(plus_di.notna() & minus_di.notna(), other=np.nan).astype('float64')
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def higher_tf_trend_sma(
    df_1m: pd.DataFrame,
    tf: str = '15min',
    period: int = 50,
) -> pd.Series:
    """Return a 1m-indexed boolean series flagging bullish higher-TF trend via SMA.

    Resamples 1m OHLCV into the given timeframe, computes SMA(period) on the
    higher-TF close, marks each higher-TF bar bullish if close > sma, then
    forward-fills the flag back onto the 1m grid. Bars before the SMA is
    defined are NaN. Used for 15m alignment with 1m signals.
    """
    if 'ts' not in df_1m.columns:
        raise ValueError("df_1m must contain a 'ts' column in ms since epoch")
    idx = pd.to_datetime(df_1m['ts'], unit='ms', utc=True)
    ohlcv = pd.DataFrame(
        {
            'open': df_1m['open'].values,
            'high': df_1m['high'].values,
            'low': df_1m['low'].values,
            'close': df_1m['close'].values,
            'volume': df_1m['volume'].values,
        },
        index=idx,
    )
    htf = ohlcv.resample(tf, label='right', closed='right').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna(subset=['close'])
    htf_sma = htf['close'].rolling(window=period, min_periods=period).mean()
    flag = htf['close'] > htf_sma
    flag = flag.where(~htf_sma.isna())
    return flag.reindex(idx, method='ffill').reset_index(drop=True)


def higher_tf_trend_ema(
    df_1m: pd.DataFrame,
    tf: str = '1h',
    period: int = 200,
) -> pd.Series:
    """Return a 1m-indexed series flagging bullish (True) vs bearish (False) higher-TF trend.

    Resamples 1m OHLCV into the given timeframe, computes EMA(period) on close,
    marks each higher-TF bar as bullish if its close > EMA, then forward-fills
    the flag back onto the 1m grid. Bars before the EMA is defined are NaN.
    """
    if 'ts' not in df_1m.columns:
        raise ValueError("df_1m must contain a 'ts' column in ms since epoch")
    idx = pd.to_datetime(df_1m['ts'], unit='ms', utc=True)
    ohlcv = pd.DataFrame(
        {
            'open': df_1m['open'].values,
            'high': df_1m['high'].values,
            'low': df_1m['low'].values,
            'close': df_1m['close'].values,
            'volume': df_1m['volume'].values,
        },
        index=idx,
    )
    htf = ohlcv.resample(tf, label='right', closed='right').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna(subset=['close'])
    htf_ema = htf['close'].ewm(span=period, adjust=False, min_periods=period).mean()
    flag = htf['close'] > htf_ema
    flag = flag.where(~htf_ema.isna())
    return flag.reindex(idx, method='ffill').reset_index(drop=True)
