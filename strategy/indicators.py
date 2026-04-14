import pandas as pd


def sma(df: pd.DataFrame, period: int) -> pd.Series:
    """Simple moving average of the 'close' column over *period* bars.

    Leading values where fewer than *period* bars are available are NaN.
    """
    return df['close'].rolling(window=period, min_periods=period).mean()


def ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential moving average of the 'close' column over *period* bars.

    Uses Wilder / pandas default (adjust=False) so that the EMA is causal
    and consistent with most trading platforms.
    """
    return df['close'].ewm(span=period, adjust=False, min_periods=period).mean()


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
