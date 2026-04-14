import asyncio
import logging
from typing import Any

import ccxt.async_support as ccxt

from core.state import BotState, StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from risk.manager import RiskManager
from strategy.indicators import rsi, sma

logger = logging.getLogger(__name__)

_SMA_PERIOD = 20
_RSI_PERIOD = 14
_RSI_OVERSOLD = 30
_MIN_CANDLES = max(_SMA_PERIOD, _RSI_PERIOD)


async def trading_loop(
    client: BinanceClient,
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    config: dict[str, Any],
) -> None:
    """Main trading loop. Runs indefinitely until cancelled.

    Expected config keys:
        symbol          str    e.g. 'BTC/USDT'
        timeframe       str    e.g. '1m'
        limit           int    candles to fetch per tick (default 200)
        interval_seconds float  sleep between iterations
    """
    symbol: str = config['symbol']
    timeframe: str = config['timeframe']
    limit: int = config.get('limit', 200)
    interval: float = config['interval_seconds']

    logger.info(
        'trading_loop started symbol=%s timeframe=%s interval=%ss',
        symbol, timeframe, interval,
    )

    while True:
        if risk_manager.is_circuit_breaker_active():
            logger.warning(
                'circuit_breaker=active daily_pnl=%.4f sleeping %ss',
                risk_manager.get_daily_pnl(), interval,
            )
            await asyncio.sleep(interval)
            continue

        try:
            candles: list[dict[str, Any]] = await client.fetch_candles(
                symbol=symbol, timeframe=timeframe, limit=limit,
            )
        except (ccxt.RateLimitExceeded, ccxt.NetworkError) as exc:
            logger.warning('fetch_failed error=%s sleeping %ss', exc, interval)
            await asyncio.sleep(interval)
            continue

        buffer.add_many(candles)

        if not buffer.is_ready(_MIN_CANDLES):
            logger.debug(
                'buffer_not_ready len=%d required=%d sleeping %ss',
                len(buffer), _MIN_CANDLES, interval,
            )
            await asyncio.sleep(interval)
            continue

        df = buffer.to_dataframe()
        current_close: float = float(df['close'].iloc[-1])
        current_sma: float = float(sma(df, period=_SMA_PERIOD).iloc[-1])
        current_rsi: float = float(rsi(df, period=_RSI_PERIOD).iloc[-1])

        logger.info(
            'tick symbol=%s close=%.2f sma%d=%.2f rsi%d=%.1f state=%s',
            symbol, current_close, _SMA_PERIOD, current_sma,
            _RSI_PERIOD, current_rsi, state_manager.get_state().value,
        )

        if state_manager.get_state() == BotState.WAITING_SIGNAL:
            if current_rsi < _RSI_OVERSOLD and current_close > current_sma:
                logger.info(
                    'buy_signal symbol=%s close=%.4f sma%d=%.4f rsi%d=%.2f',
                    symbol, current_close, _SMA_PERIOD, current_sma,
                    _RSI_PERIOD, current_rsi,
                )

        await asyncio.sleep(interval)
