import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

import ccxt
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY: float = 1.0   # seconds; doubles each attempt

_WS_TIMEOUT: float        = 30.0  # seconds without data → reconnect
_WS_RECONNECT_BASE: float = 1.0   # initial reconnect backoff (seconds)
_WS_RECONNECT_CAP: float  = 60.0  # max reconnect backoff (seconds)
_WS_MAX_FAILURES: int     = 5     # consecutive failures before REST fallback


# ── internal exception types ───────────────────────────────────────────────

class _WsFallbackError(Exception):
    """WebSocket exhausted retries; switch to REST polling."""


class _WsDisconnectedError(Exception):
    """WebSocket dropped after receiving at least one update."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


# ── helpers ────────────────────────────────────────────────────────────────

def _row_to_candle(row: list[Any]) -> dict[str, Any]:
    return {
        'ts':     row[0],
        'open':   row[1],
        'high':   row[2],
        'low':    row[3],
        'close':  row[4],
        'volume': row[5],
    }


# ── client ─────────────────────────────────────────────────────────────────

class BinanceClient:
    """Binance client for testnet. HTTP calls run in a thread executor so the
    asyncio event loop is never blocked.

    Credentials are loaded from .env:
        BINANCE_API_KEY
        BINANCE_API_SECRET
    """

    def __init__(self) -> None:
        api_key: str | None    = os.getenv('BINANCE_API_KEY')
        api_secret: str | None = os.getenv('BINANCE_API_SECRET')
        if not api_key or not api_secret:
            raise RuntimeError(
                'BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env'
            )
        self._api_key    = api_key
        self._api_secret = api_secret
        # Sync ccxt — HTTP calls are dispatched via run_in_executor so they
        # never block the event loop.
        self._exchange: ccxt.binance = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'timeout': 10000,
            'options': {'defaultType': 'spot'},
        })
        self._exchange.set_sandbox_mode(True)
        self._pro_exchange: Any | None = None

    # ── REST ───────────────────────────────────────────────────────────────

    async def fetch_candles(
        self,
        symbol: str = 'BTC/USDT',
        timeframe: str = '1m',
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles for *symbol* / *timeframe*.

        Runs the blocking ccxt call in the default thread executor.
        Retries up to _MAX_RETRIES times with exponential backoff on
        RequestTimeout, NetworkError, and RateLimitExceeded.
        Other ExchangeErrors propagate immediately.

        Returns a list of dicts: {ts, open, high, low, close, volume}.
        """
        loop = asyncio.get_running_loop()
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                raw: list[list[Any]] = await loop.run_in_executor(
                    None,
                    lambda: self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit),
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

            except ccxt.RequestTimeout as exc:
                last_exc = exc
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    'RequestTimeout symbol=%s attempt=%d/%d retry_in=%.1fs',
                    symbol, attempt, _MAX_RETRIES, delay,
                )

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

    # ── WebSocket internals ────────────────────────────────────────────────

    def _ensure_pro_exchange(self) -> Any:
        """Lazily create the ccxt.pro exchange for WebSocket streaming."""
        if self._pro_exchange is not None:
            return self._pro_exchange
        try:
            import ccxt.pro as ccxtpro  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                'ccxt.pro required for WebSocket — run: pip install ccxt[pro]'
            ) from exc
        self._pro_exchange = ccxtpro.binance({
            'apiKey':  self._api_key,
            'secret':  self._api_secret,
            'timeout': 10_000,
            'options': {'defaultType': 'spot'},
        })
        self._pro_exchange.set_sandbox_mode(True)
        return self._pro_exchange

    async def _invoke_callback(
        self,
        callback: Callable[[list[dict[str, Any]]], Any],
        candles: list[dict[str, Any]],
    ) -> None:
        result = callback(candles)
        if asyncio.iscoroutine(result):
            await result

    async def _ws_stream(
        self,
        exchange: Any,
        symbol: str,
        timeframe: str,
        callback: Callable[[list[dict[str, Any]]], Any],
        limit: int,
    ) -> None:
        """Inner streaming loop. Raises _WsDisconnectedError when it drops
        after receiving data; re-raises the original exception otherwise."""
        connected = False
        try:
            while True:
                raw = await asyncio.wait_for(
                    exchange.watch_ohlcv(symbol, timeframe, limit=limit),
                    timeout=_WS_TIMEOUT,
                )
                connected = True
                await self._invoke_callback(callback, [_row_to_candle(r) for r in raw])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if connected:
                raise _WsDisconnectedError(exc) from exc
            raise

    async def _ws_error_backoff(
        self,
        symbol: str,
        exc: Exception,
        failures: int,
        delay: float,
    ) -> tuple[int, float]:
        """Log, sleep, return updated (failures, delay). Raises _WsFallbackError at limit."""
        failures += 1
        if failures >= _WS_MAX_FAILURES:
            raise _WsFallbackError() from exc
        logger.warning(
            'ws_error symbol=%s error=%s delay=%.1fs attempt=%d/%d',
            symbol, exc, delay, failures, _WS_MAX_FAILURES,
        )
        await asyncio.sleep(delay)
        return failures, min(delay * 2, _WS_RECONNECT_CAP)

    async def _ws_loop(
        self,
        symbol: str,
        timeframe: str,
        callback: Callable[[list[dict[str, Any]]], Any],
        limit: int,
    ) -> None:
        """Outer reconnect loop with exponential backoff. Raises _WsFallbackError
        after _WS_MAX_FAILURES consecutive connection failures."""
        delay    = _WS_RECONNECT_BASE
        failures = 0
        while True:
            try:
                await self._ws_stream(
                    self._ensure_pro_exchange(), symbol, timeframe, callback, limit,
                )
            except asyncio.CancelledError:
                raise
            except ImportError:
                raise _WsFallbackError()
            except _WsDisconnectedError as exc:
                failures, delay = 0, _WS_RECONNECT_BASE
                logger.warning('ws_disconnected symbol=%s cause=%s reconnecting', symbol, exc.cause)
            except Exception as exc:
                failures, delay = await self._ws_error_backoff(symbol, exc, failures, delay)

    async def _rest_polling_loop(
        self,
        symbol: str,
        timeframe: str,
        callback: Callable[[list[dict[str, Any]]], Any],
        limit: int,
        rest_interval: float,
    ) -> None:
        """REST polling fallback. Runs indefinitely until cancelled."""
        while True:
            try:
                candles = await self.fetch_candles(symbol, timeframe, limit)
                await self._invoke_callback(callback, candles)
            except (ccxt.RateLimitExceeded, ccxt.NetworkError) as exc:
                logger.warning('rest_poll_error symbol=%s error=%s', symbol, exc)
            await asyncio.sleep(rest_interval)

    # ── public WebSocket API ───────────────────────────────────────────────

    async def watch_candles(
        self,
        symbol: str,
        timeframe: str,
        callback: Callable[[list[dict[str, Any]]], Any],
        *,
        limit: int = 200,
        rest_interval: float = 60.0,
    ) -> None:
        """Stream OHLCV candles in real time.

        Calls *callback* with a list of candle dicts on every update.
        Reconnects automatically with exponential backoff (up to
        _WS_RECONNECT_CAP seconds) if the connection drops.
        Falls back to REST polling (every *rest_interval* seconds) after
        _WS_MAX_FAILURES consecutive failures or if ccxt.pro is unavailable.
        Runs indefinitely until the task is cancelled.
        """
        logger.info('watch_candles_start symbol=%s timeframe=%s', symbol, timeframe)
        try:
            await self._ws_loop(symbol, timeframe, callback, limit)
        except _WsFallbackError:
            logger.warning(
                'ws_fallback_to_rest symbol=%s interval=%.1fs', symbol, rest_interval,
            )
            await self._rest_polling_loop(symbol, timeframe, callback, limit, rest_interval)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._pro_exchange is not None:
            await self._pro_exchange.close()
        # Sync ccxt manages HTTP connections internally; nothing to close.
