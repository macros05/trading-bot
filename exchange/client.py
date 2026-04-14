import asyncio
import logging
import os
from typing import Any

import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY: float = 1.0  # seconds; doubles each attempt


class BinanceClient:
    """Async Binance client connected to testnet.

    Credentials are loaded from .env:
        BINANCE_API_KEY
        BINANCE_API_SECRET
    """

    def __init__(self) -> None:
        api_key: str | None = os.getenv('BINANCE_API_KEY')
        api_secret: str | None = os.getenv('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            raise RuntimeError(
                'BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env'
            )

        self._exchange: ccxt.binance = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'options': {'defaultType': 'spot'},
        })
        self._exchange.set_sandbox_mode(True)

    async def fetch_candles(
        self,
        symbol: str = 'BTC/USDT',
        timeframe: str = '1m',
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles for *symbol* / *timeframe*.

        Retries up to _MAX_RETRIES times with exponential backoff on
        RateLimitExceeded and NetworkError. Other ExchangeErrors propagate
        immediately. Raises the final exception when all retries are exhausted.

        Returns a list of dicts: {ts, open, high, low, close, volume}.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                raw: list[list[Any]] = await self._exchange.fetch_ohlcv(
                    symbol, timeframe, limit=limit
                )
                candles: list[dict[str, Any]] = [
                    {
                        'ts':     row[0],
                        'open':   row[1],
                        'high':   row[2],
                        'low':    row[3],
                        'close':  row[4],
                        'volume': row[5],
                    }
                    for row in raw
                ]
                logger.debug(
                    'fetch_candles symbol=%s timeframe=%s limit=%d count=%d attempt=%d',
                    symbol, timeframe, limit, len(candles), attempt,
                )
                return candles

            except ccxt.RateLimitExceeded as exc:
                last_exc = exc
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    'RateLimitExceeded symbol=%s attempt=%d/%d retry_in=%.1fs error=%s',
                    symbol, attempt, _MAX_RETRIES, delay, exc,
                )

            except ccxt.NetworkError as exc:
                last_exc = exc
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    'NetworkError symbol=%s attempt=%d/%d retry_in=%.1fs error=%s',
                    symbol, attempt, _MAX_RETRIES, delay, exc,
                )

            except ccxt.ExchangeError as exc:
                logger.error('ExchangeError symbol=%s error=%s', symbol, exc)
                raise

            if attempt < _MAX_RETRIES:
                await asyncio.sleep(delay)  # type: ignore[possibly-undefined]

        logger.error(
            'fetch_candles exhausted %d retries symbol=%s timeframe=%s last_error=%s',
            _MAX_RETRIES, symbol, timeframe, last_exc,
        )
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        await self._exchange.close()
