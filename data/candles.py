from collections import deque

_FIELDS = ('ts', 'open', 'high', 'low', 'close', 'volume')


class CandleBuffer:
    """Ring buffer of OHLCV candles backed by deque(maxlen=200)."""

    def __init__(self, maxlen: int = 200) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)

    def append(self, ohlcv: list) -> None:
        """Accept a raw ccxt row: [timestamp, open, high, low, close, volume]."""
        self._buf.append(dict(zip(_FIELDS, ohlcv)))

    def closes(self) -> list[float]:
        return [c['close'] for c in self._buf]

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)

    def __iter__(self):
        return iter(self._buf)

    def __getitem__(self, idx):
        return list(self._buf)[idx]

    def __repr__(self) -> str:
        return f'CandleBuffer(len={len(self._buf)}, maxlen={self._buf.maxlen})'
