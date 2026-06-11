import logging
import time

logger = logging.getLogger(__name__)

# Adaptive Kelly bounds — never let runtime adjustments push outside this range.
KELLY_FLOOR_PCT = 0.005   # 0.5 % per trade
KELLY_CAP_PCT   = 0.02    # 2.0 % per trade
KELLY_BOOST_FACTOR  = 1.10   # +10 % when rolling WR > upper threshold
KELLY_DAMPEN_FACTOR = 0.80   # −20 % when rolling WR < lower threshold
KELLY_BOOST_THRESHOLD  = 50.0
KELLY_DAMPEN_THRESHOLD = 35.0
KELLY_WINDOW = 10


class RiskManager:
    """Daily drawdown guard and position sizing.

    The circuit breaker trips when accumulated daily PnL drops below
    -max_daily_drawdown (expressed as a fraction, e.g. 0.03 = -3 %).
    Once active it stays active until reset_daily() is called.
    """

    def __init__(
        self,
        max_daily_drawdown: float = 0.03,
        initial_daily_pnl: float = 0.0,
        leverage: int = 1,
        base_risk_pct: float = 0.02,
        adaptive_kelly: bool = False,
    ) -> None:
        if max_daily_drawdown <= 0:
            raise ValueError('max_daily_drawdown must be positive')
        if leverage < 1:
            raise ValueError('leverage must be >= 1')
        self._max_daily_drawdown = max_daily_drawdown
        self._daily_pnl: float = initial_daily_pnl
        self._leverage = leverage
        self._base_risk_pct = base_risk_pct
        self.effective_risk_pct = base_risk_pct
        self._adaptive_kelly = adaptive_kelly
        logger.info(
            'RiskManager ready max_daily_drawdown=%.2f%% initial_daily_pnl=%.4f '
            'leverage=%d base_risk=%.4f adaptive_kelly=%s',
            max_daily_drawdown * 100, initial_daily_pnl, leverage,
            base_risk_pct, adaptive_kelly,
        )

    def register_trade(self, pnl: float) -> None:
        """Accumulate *pnl* into the daily counter, scaled by leverage."""
        scaled = pnl * self._leverage
        self._daily_pnl += scaled
        logger.info(
            'register_trade pnl=%.4f leverage=%d scaled=%.4f daily_pnl=%.4f circuit_breaker=%s',
            pnl, self._leverage, scaled, self._daily_pnl, self.is_circuit_breaker_active(),
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

    def update_adaptive_kelly(self, recent_trades: list[dict]) -> dict | None:
        """Adjust effective_risk_pct based on rolling win rate.

        Returns a change record (or None if no change). Only acts when there
        are at least KELLY_WINDOW recent trades.
        """
        if not self._adaptive_kelly:
            return None
        if len(recent_trades) < KELLY_WINDOW:
            return None
        last = recent_trades[-KELLY_WINDOW:]
        wins = sum(1 for t in last if t.get('result') == 'WIN')
        win_rate = wins / KELLY_WINDOW * 100.0
        old = self.effective_risk_pct
        if win_rate > KELLY_BOOST_THRESHOLD:
            new = min(KELLY_CAP_PCT, old * KELLY_BOOST_FACTOR)
            reason = f'boost win_rate={win_rate:.1f}% above {KELLY_BOOST_THRESHOLD:.1f}%'
        elif win_rate < KELLY_DAMPEN_THRESHOLD:
            new = max(KELLY_FLOOR_PCT, old * KELLY_DAMPEN_FACTOR)
            reason = f'dampen win_rate={win_rate:.1f}% below {KELLY_DAMPEN_THRESHOLD:.1f}%'
        else:
            return None
        if abs(new - old) < 1e-6:
            return None
        self.effective_risk_pct = new
        change = {
            'ts_ms':            int(time.time() * 1000),
            'old_kelly_pct':    old,
            'new_kelly_pct':    new,
            'rolling_win_rate': win_rate,
            'n_recent_trades':  KELLY_WINDOW,
            'reason':           reason,
        }
        logger.info(
            'adaptive_kelly_change old=%.4f new=%.4f win_rate=%.1f%% reason=%s',
            old, new, win_rate, reason,
        )
        return change

    def position_size(
        self,
        balance: float,
        risk_pct: float = 0.01,
        sl_pct: float = 0.025,
    ) -> float:
        """Return position notional (USDT) sized so a stop-out loses risk_pct of balance.

        Volatility-targeted sizing: notional = balance × risk_pct / sl_pct.

        Example with balance=10 000, risk_pct=0.01, sl_pct=0.025:
            notional = 10 000 × 0.01 / 0.025 = $4 000
            loss at SL = $4 000 × 0.025 = $100 = 1 % of balance ✓

        Returns 0.0 when the circuit breaker is active so no new positions
        can be opened after the daily loss limit is hit.
        """
        if balance <= 0:
            raise ValueError('balance must be positive')
        if risk_pct <= 0:
            raise ValueError('risk_pct must be positive')
        if sl_pct <= 0:
            raise ValueError('sl_pct must be positive')

        if self.is_circuit_breaker_active():
            logger.warning('position_size blocked circuit_breaker=active balance=%.2f', balance)
            return 0.0

        notional = balance * risk_pct / sl_pct
        logger.debug(
            'position_size balance=%.2f risk_pct=%.4f sl_pct=%.4f notional=%.2f',
            balance, risk_pct, sl_pct, notional,
        )
        return notional
