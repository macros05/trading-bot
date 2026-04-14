import asyncio
import logging
import sys

from config import BOT_CONFIG
from core.loop import trading_loop
from core.state import StateManager
from data.candles import CandleBuffer
from exchange.client import BinanceClient
from risk.manager import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


async def main() -> None:
    client = BinanceClient()
    buffer = CandleBuffer(maxlen=200)
    state_manager = StateManager()
    risk_manager = RiskManager(max_daily_drawdown=0.03)

    logger.info('bot starting config=%s', BOT_CONFIG)
    try:
        await trading_loop(client, buffer, state_manager, risk_manager, BOT_CONFIG)
    except asyncio.CancelledError:
        logger.info('bot stopped')
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
