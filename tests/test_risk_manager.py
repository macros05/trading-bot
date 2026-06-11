"""
Tests for risk/manager.py.

Run from project root:
    python -m unittest tests.test_risk_manager
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from risk.manager import RiskManager


class TestInit(unittest.TestCase):

    def test_default_drawdown(self):
        rm = RiskManager()
        self.assertAlmostEqual(rm._max_daily_drawdown, 0.03)

    def test_custom_drawdown(self):
        rm = RiskManager(max_daily_drawdown=0.05)
        self.assertAlmostEqual(rm._max_daily_drawdown, 0.05)

    def test_invalid_drawdown_raises(self):
        with self.assertRaises(ValueError):
            RiskManager(max_daily_drawdown=0.0)
        with self.assertRaises(ValueError):
            RiskManager(max_daily_drawdown=-0.01)

    def test_starts_with_zero_pnl(self):
        self.assertAlmostEqual(RiskManager().get_daily_pnl(), 0.0)

    def test_circuit_breaker_inactive_on_init(self):
        self.assertFalse(RiskManager().is_circuit_breaker_active())


# ---------------------------------------------------------------------------
# register_trade / get_daily_pnl
# ---------------------------------------------------------------------------

class TestRegisterTrade(unittest.TestCase):

    def test_accumulates_positive(self):
        rm = RiskManager()
        rm.register_trade(0.01)
        rm.register_trade(0.005)
        self.assertAlmostEqual(rm.get_daily_pnl(), 0.015)

    def test_accumulates_negative(self):
        rm = RiskManager()
        rm.register_trade(-0.01)
        rm.register_trade(-0.005)
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.015)

    def test_accumulates_mixed(self):
        rm = RiskManager()
        rm.register_trade(0.02)
        rm.register_trade(-0.05)
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.03)

    def test_zero_pnl_trade_is_noop(self):
        rm = RiskManager()
        rm.register_trade(0.0)
        self.assertAlmostEqual(rm.get_daily_pnl(), 0.0)


# ---------------------------------------------------------------------------
# is_circuit_breaker_active
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.TestCase):

    def test_inactive_below_limit(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.02)
        self.assertFalse(rm.is_circuit_breaker_active())

    def test_active_at_exact_limit(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.03)
        self.assertTrue(rm.is_circuit_breaker_active())

    def test_active_beyond_limit(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        self.assertTrue(rm.is_circuit_breaker_active())

    def test_positive_pnl_never_trips(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(0.10)
        self.assertFalse(rm.is_circuit_breaker_active())

    def test_trips_across_multiple_trades(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.01)
        rm.register_trade(-0.01)
        self.assertFalse(rm.is_circuit_breaker_active())
        rm.register_trade(-0.01)
        self.assertTrue(rm.is_circuit_breaker_active())


# ---------------------------------------------------------------------------
# reset_daily
# ---------------------------------------------------------------------------

class TestResetDaily(unittest.TestCase):

    def test_resets_pnl_to_zero(self):
        rm = RiskManager()
        rm.register_trade(-0.05)
        rm.reset_daily()
        self.assertAlmostEqual(rm.get_daily_pnl(), 0.0)

    def test_deactivates_circuit_breaker(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        self.assertTrue(rm.is_circuit_breaker_active())
        rm.reset_daily()
        self.assertFalse(rm.is_circuit_breaker_active())

    def test_can_accumulate_after_reset(self):
        rm = RiskManager()
        rm.register_trade(-0.05)
        rm.reset_daily()
        rm.register_trade(0.02)
        self.assertAlmostEqual(rm.get_daily_pnl(), 0.02)


# ---------------------------------------------------------------------------
# position_size
# ---------------------------------------------------------------------------

class TestPositionSize(unittest.TestCase):

    def test_volatility_targeted_sizing(self):
        """notional = balance × risk_pct / sl_pct so a stop-out loses risk_pct."""
        rm = RiskManager()
        notional = rm.position_size(10_000.0, risk_pct=0.01, sl_pct=0.025)
        self.assertAlmostEqual(notional, 4_000.0)
        # loss at SL = notional × sl_pct = 100 = 1 % of balance ✓
        self.assertAlmostEqual(notional * 0.025, 100.0)

    def test_tighter_stop_increases_size(self):
        """Halving the stop doubles the notional (same dollar risk)."""
        rm = RiskManager()
        wide = rm.position_size(10_000.0, risk_pct=0.01, sl_pct=0.025)
        tight = rm.position_size(10_000.0, risk_pct=0.01, sl_pct=0.0125)
        self.assertAlmostEqual(tight, 2 * wide)

    def test_defaults_match_config(self):
        """Default sl_pct=0.025 mirrors config.STOP_LOSS_PCT."""
        rm = RiskManager()
        self.assertAlmostEqual(rm.position_size(5_000.0), 5_000.0 * 0.01 / 0.025)

    def test_returns_zero_when_circuit_breaker_active(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        self.assertEqual(rm.position_size(10_000.0), 0.0)

    def test_resumes_after_reset(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        rm.reset_daily()
        self.assertAlmostEqual(
            rm.position_size(10_000.0, risk_pct=0.01, sl_pct=0.025),
            4_000.0,
        )

    def test_invalid_balance_raises(self):
        rm = RiskManager()
        with self.assertRaises(ValueError):
            rm.position_size(0.0)
        with self.assertRaises(ValueError):
            rm.position_size(-100.0)

    def test_invalid_risk_pct_raises(self):
        rm = RiskManager()
        with self.assertRaises(ValueError):
            rm.position_size(10_000.0, risk_pct=0.0)
        with self.assertRaises(ValueError):
            rm.position_size(10_000.0, risk_pct=-0.01)

    def test_invalid_sl_pct_raises(self):
        rm = RiskManager()
        with self.assertRaises(ValueError):
            rm.position_size(10_000.0, risk_pct=0.01, sl_pct=0.0)
        with self.assertRaises(ValueError):
            rm.position_size(10_000.0, risk_pct=0.01, sl_pct=-0.01)


class TestRiskManagerLeverage(unittest.TestCase):
    def test_default_leverage_is_one_unchanged_behavior(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05)
        rm.register_trade(-0.01)  # -1% pct loss
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.01)

    def test_leverage_two_doubles_pnl_impact(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
        rm.register_trade(-0.01)  # -1% raw → -2% with 2x leverage
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.02)

    def test_circuit_breaker_with_leverage(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
        # Three trades of -1% each = -2% × 3 = -6% with 2x; breaker trips at -5%
        rm.register_trade(-0.01)
        rm.register_trade(-0.01)
        self.assertFalse(rm.is_circuit_breaker_active())  # -4% so far
        rm.register_trade(-0.01)
        self.assertTrue(rm.is_circuit_breaker_active())   # -6%, exceeds -5%

    def test_invalid_leverage_raises(self):
        from risk.manager import RiskManager
        with self.assertRaises(ValueError):
            RiskManager(max_daily_drawdown=0.05, leverage=0)

    def test_negative_leverage_raises(self):
        from risk.manager import RiskManager
        with self.assertRaises(ValueError):
            RiskManager(max_daily_drawdown=0.05, leverage=-1)


if __name__ == '__main__':
    unittest.main()
