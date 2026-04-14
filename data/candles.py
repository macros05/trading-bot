import logging
from collections import deque

import pandas as pd

logger = logging.getLogger(__name__)

_COLUMNS = ('ts', 'open', 'high', 'low', 'close', 'volume')


class CandleBuffer:
    """Ring buffer of OHLCV candles backed by deque(maxlen=200)."""

    def __init__(self, maxlen: int = 200) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)

    def add(self, candle: dict) -> None:
        """Append a single candle dict to the buffer."""
        self._buf.append(candle)
        logger.debug(
            'add ts=%s close=%s buf_len=%d',
            candle.get('ts'), candle.get('close'), len(self._buf),
        )

    def add_many(self, candles: list[dict]) -> None:
        """Bulk-load candles; oldest are evicted when maxlen is exceeded."""
        for candle in candles:
            self._buf.append(candle)
        logger.debug('add_many added=%d buf_len=%d', len(candles), len(self._buf))

    def to_dataframe(self) -> pd.DataFrame:
        """Return buffer contents as a DataFrame with columns: ts, open, high, low, close, volume."""
        return pd.DataFrame(list(self._buf), columns=list(_COLUMNS))

    def is_ready(self, period: int) -> bool:
        """Return True if the buffer holds at least *period* candles."""
        return len(self._buf) >= period

    def __len__(self) -> int:
        return len(self._buf)

    def __iter__(self):
        return iter(self._buf)

    def __repr__(self) -> str:
        return f'CandleBuffer(len={len(self._buf)}, maxlen={self._buf.maxlen})'
