import logging

logger = logging.getLogger(__name__)


class RiskManager:
    """Daily drawdown guard and position sizing.

    The circuit breaker trips when accumulated daily PnL drops below
    -max_daily_drawdown (expressed as a fraction, e.g. 0.03 = -3 %).
    Once active it stays active until reset_daily() is called.
    """

    def __init__(self, max_daily_drawdown: float = 0.03) -> None:
        if max_daily_drawdown <= 0:
            raise ValueError('max_daily_drawdown must be positive')
        self._max_daily_drawdown = max_daily_drawdown
        self._daily_pnl: float = 0.0
        logger.info('RiskManager ready max_daily_drawdown=%.2f%%', max_daily_drawdown * 100)

    def register_trade(self, pnl: float) -> None:
        """Accumulate *pnl* into the daily counter."""
        self._daily_pnl += pnl
        logger.info(
            'register_trade pnl=%.4f daily_pnl=%.4f circuit_breaker=%s',
            pnl, self._daily_pnl, self.is_circuit_breaker_active(),
        )

    def is_circuit_breaker_active(self) -> bool:
        """Return True when daily loss has reached or exceeded the drawdown limit."""
        return self._daily_pnl <= -self._max_daily_drawdown

    def get_daily_pnl(self) -> float:
        """Return accumulated PnL for the current day."""
        return self._daily_pnl

    def reset_daily(self) -> None:
        """Reset the daily PnL counter. Call at midnight."""
        previous = self._daily_pnl
        self._daily_pnl = 0.0
        logger.info('reset_daily previous_pnl=%.4f', previous)

    def position_size(self, balance: float, risk_pct: float = 0.01) -> float:
        """Return the maximum position size for a given *balance*.

        Returns 0.0 when the circuit breaker is active so no new positions
        can be opened after the daily loss limit is hit.
        """
        if balance <= 0:
            raise ValueError('balance must be positive')
        if risk_pct <= 0:
            raise ValueError('risk_pct must be positive')

        if self.is_circuit_breaker_active():
            logger.warning('position_size blocked circuit_breaker=active balance=%.2f', balance)
            return 0.0

        size = balance * risk_pct
        logger.debug('position_size balance=%.2f risk_pct=%.2f size=%.4f', balance, risk_pct, size)
        return size
