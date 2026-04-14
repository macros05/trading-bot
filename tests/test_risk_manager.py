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

    def test_basic_calculation(self):
        rm = RiskManager()
        self.assertAlmostEqual(rm.position_size(10_000.0, risk_pct=0.01), 100.0)

    def test_default_risk_pct(self):
        rm = RiskManager()
        self.assertAlmostEqual(rm.position_size(5_000.0), 50.0)

    def test_returns_zero_when_circuit_breaker_active(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        self.assertEqual(rm.position_size(10_000.0), 0.0)

    def test_resumes_after_reset(self):
        rm = RiskManager(max_daily_drawdown=0.03)
        rm.register_trade(-0.05)
        rm.reset_daily()
        self.assertAlmostEqual(rm.position_size(10_000.0, risk_pct=0.01), 100.0)

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


if __name__ == '__main__':
    unittest.main()
