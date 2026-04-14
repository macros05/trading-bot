import logging
import os

import ccxt.async_support as ccxt
from dotenv import load_dotenv

from data.candles import CandleBuffer

load_dotenv()

logger = logging.getLogger(__name__)

SYMBOL = 'BTC/USDT'
TIMEFRAME = '1m'


class BinanceClient:
    """Async Binance client (testnet by default).

    Credentials are loaded from .env:
        BINANCE_API_KEY
        BINANCE_API_SECRET
    """

    def __init__(self) -> None:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            raise RuntimeError(
                'BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env'
            )

        self._exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'options': {'defaultType': 'spot'},
        })
        self._exchange.set_sandbox_mode(True)
        self.candles: CandleBuffer = CandleBuffer(maxlen=200)

    async def fetch_candles(self, limit: int = 200) -> CandleBuffer:
        """Fetch the last *limit* 1-minute candles and refresh the buffer.

        Raises ccxt.NetworkError / ccxt.ExchangeError on failure — callers
        must not swallow these; the main loop handles retries.
        """
        try:
            raw = await self._exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
        except ccxt.NetworkError as exc:
            logger.warning('Network error fetching candles: %s', exc)
            raise
        except ccxt.ExchangeError as exc:
            logger.error('Exchange error fetching candles: %s', exc)
            raise

        self.candles.clear()
        for row in raw:
            self.candles.append(row)

        logger.debug('Fetched %d candles for %s', len(self.candles), SYMBOL)
        return self.candles

    async def close(self) -> None:
        await self._exchange.close()
