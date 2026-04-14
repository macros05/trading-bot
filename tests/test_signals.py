"""
Tests for strategy/signals.py.

Run from project root:
    python -m unittest tests.test_signals
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy.signals import calc_pnl, check_exit, should_enter, should_enter_mean_rev


# ---------------------------------------------------------------------------
# should_enter
# ---------------------------------------------------------------------------

class TestShouldEnter(unittest.TestCase):

    def test_returns_true_when_rsi_below_threshold_and_close_above_sma(self):
        self.assertTrue(should_enter(close=105.0, sma20=100.0, rsi14=30.0))

    def test_returns_false_when_rsi_above_threshold(self):
        self.assertFalse(should_enter(close=105.0, sma20=100.0, rsi14=40.0))

    def test_returns_false_when_close_below_sma(self):
        self.assertFalse(should_enter(close=95.0, sma20=100.0, rsi14=30.0))

    def test_returns_false_when_both_conditions_unmet(self):
        self.assertFalse(should_enter(close=95.0, sma20=100.0, rsi14=40.0))

    def test_boundary_rsi_equal_to_threshold_returns_false(self):
        # rsi < threshold is strict, so equality returns False
        self.assertFalse(should_enter(close=105.0, sma20=100.0, rsi14=35.0))

    def test_custom_threshold_respected(self):
        # rsi=28 is below threshold=30 → True
        self.assertTrue(should_enter(close=105.0, sma20=100.0, rsi14=28.0, rsi_threshold=30.0))
        # rsi=31 is above threshold=30 → False
        self.assertFalse(should_enter(close=105.0, sma20=100.0, rsi14=31.0, rsi_threshold=30.0))


# ---------------------------------------------------------------------------
# check_exit
# ---------------------------------------------------------------------------

class TestCheckExit(unittest.TestCase):

    def test_returns_none_when_within_range(self):
        self.assertIsNone(check_exit(close=101.0, entry_price=100.0))

    def test_returns_stop_loss_when_price_falls_by_stop_pct(self):
        result = check_exit(close=98.0, entry_price=100.0, stop_loss_pct=0.02)
        self.assertEqual(result, 'stop_loss')

    def test_returns_stop_loss_when_price_falls_beyond_stop_pct(self):
        result = check_exit(close=95.0, entry_price=100.0, stop_loss_pct=0.02)
        self.assertEqual(result, 'stop_loss')

    def test_returns_take_profit_when_price_rises_by_tp_pct(self):
        result = check_exit(close=103.0, entry_price=100.0, take_profit_pct=0.03)
        self.assertEqual(result, 'take_profit')

    def test_returns_take_profit_when_price_rises_beyond_tp_pct(self):
        result = check_exit(close=110.0, entry_price=100.0, take_profit_pct=0.03)
        self.assertEqual(result, 'take_profit')

    def test_stop_loss_boundary_exact_pct(self):
        # change == -stop_loss_pct → triggers (<=)
        result = check_exit(close=98.0, entry_price=100.0, stop_loss_pct=0.02)
        self.assertEqual(result, 'stop_loss')

    def test_take_profit_boundary_exact_pct(self):
        # change == take_profit_pct → triggers (>=)
        result = check_exit(close=103.0, entry_price=100.0, take_profit_pct=0.03)
        self.assertEqual(result, 'take_profit')

    def test_just_inside_stop_loss_returns_none(self):
        # -1.99% → no stop loss
        self.assertIsNone(check_exit(close=98.01, entry_price=100.0, stop_loss_pct=0.02))

    def test_just_inside_take_profit_returns_none(self):
        # +2.99% → no take profit
        self.assertIsNone(check_exit(close=102.99, entry_price=100.0, take_profit_pct=0.03))


# ---------------------------------------------------------------------------
# calc_pnl
# ---------------------------------------------------------------------------

class TestCalcPnl(unittest.TestCase):

    def test_positive_pnl_on_price_increase(self):
        pnl_usdt, pnl_pct = calc_pnl(close=110.0, entry_price=100.0, qty=1.0)
        self.assertAlmostEqual(pnl_usdt, 10.0)
        self.assertAlmostEqual(pnl_pct, 10.0)

    def test_negative_pnl_on_price_decrease(self):
        pnl_usdt, pnl_pct = calc_pnl(close=90.0, entry_price=100.0, qty=1.0)
        self.assertAlmostEqual(pnl_usdt, -10.0)
        self.assertAlmostEqual(pnl_pct, -10.0)

    def test_zero_pnl_when_price_unchanged(self):
        pnl_usdt, pnl_pct = calc_pnl(close=100.0, entry_price=100.0, qty=1.0)
        self.assertAlmostEqual(pnl_usdt, 0.0)
        self.assertAlmostEqual(pnl_pct, 0.0)

    def test_qty_scales_usdt_pnl(self):
        pnl_usdt, pnl_pct = calc_pnl(close=110.0, entry_price=100.0, qty=2.0)
        self.assertAlmostEqual(pnl_usdt, 20.0)
        self.assertAlmostEqual(pnl_pct, 10.0)  # pct is independent of qty

    def test_returns_tuple_of_two_floats(self):
        result = calc_pnl(close=105.0, entry_price=100.0, qty=1.0)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], float)


# ---------------------------------------------------------------------------
# should_enter_mean_rev
# ---------------------------------------------------------------------------

class TestShouldEnterMeanRev(unittest.TestCase):

    def test_returns_true_when_drop_exceeds_threshold(self):
        self.assertTrue(should_enter_mean_rev(-0.02, threshold=0.015))

    def test_returns_false_when_drop_below_threshold(self):
        self.assertFalse(should_enter_mean_rev(-0.01, threshold=0.015))

    def test_boundary_equal_to_threshold_returns_true(self):
        # drop == -threshold triggers (<=)
        self.assertTrue(should_enter_mean_rev(-0.015, threshold=0.015))

    def test_returns_false_when_price_rose(self):
        self.assertFalse(should_enter_mean_rev(0.02, threshold=0.015))

    def test_returns_false_on_zero_change(self):
        self.assertFalse(should_enter_mean_rev(0.0, threshold=0.015))

    def test_custom_threshold_respected(self):
        self.assertTrue(should_enter_mean_rev(-0.03, threshold=0.025))
        self.assertFalse(should_enter_mean_rev(-0.02, threshold=0.025))


# ---------------------------------------------------------------------------
# should_enter — volume confirmation
# ---------------------------------------------------------------------------

class TestShouldEnterWithVolume(unittest.TestCase):

    def test_volume_confirms_entry(self):
        # vol=150 > sma20=100 × 1.2 → entry allowed
        self.assertTrue(should_enter(
            close=105.0, sma20=100.0, rsi14=30.0,
            volume=150.0, volume_sma20=100.0, volume_factor=1.2,
        ))

    def test_volume_blocks_entry_when_too_low(self):
        # vol=110 < sma20=100 × 1.2 → blocked
        self.assertFalse(should_enter(
            close=105.0, sma20=100.0, rsi14=30.0,
            volume=110.0, volume_sma20=100.0, volume_factor=1.2,
        ))

    def test_volume_boundary_exact_factor_returns_false(self):
        # vol == sma20 * factor is NOT > so blocked
        self.assertFalse(should_enter(
            close=105.0, sma20=100.0, rsi14=30.0,
            volume=120.0, volume_sma20=100.0, volume_factor=1.2,
        ))

    def test_volume_none_skips_volume_check(self):
        # no volume args → volume filter inactive
        self.assertTrue(should_enter(close=105.0, sma20=100.0, rsi14=30.0))

    def test_volume_sma_none_skips_volume_check(self):
        # volume provided but volume_sma20=None → filter inactive
        self.assertTrue(should_enter(
            close=105.0, sma20=100.0, rsi14=30.0,
            volume=50.0, volume_sma20=None, volume_factor=1.2,
        ))

    def test_volume_does_not_override_rsi_block(self):
        # rsi above threshold → blocked regardless of volume
        self.assertFalse(should_enter(
            close=105.0, sma20=100.0, rsi14=40.0,
            volume=200.0, volume_sma20=100.0, volume_factor=1.2,
        ))


if __name__ == '__main__':
    unittest.main()
