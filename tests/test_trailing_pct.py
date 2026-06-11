"""Tests for the percentage-based trailing stop and stalled-position helpers."""
import unittest

from strategy.signals import tighten_sl_tp_for_stalled, update_trailing_stop_pct


class TestUpdateTrailingStopPctLong(unittest.TestCase):
    def test_no_change_below_breakeven_threshold(self):
        new_sl, transition = update_trailing_stop_pct(
            sl_price=98.0, entry_price=100.0, close=100.5, side='long',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        self.assertEqual(new_sl, 98.0)
        self.assertIsNone(transition)

    def test_breakeven_when_gain_reaches_threshold(self):
        # 1.0 % gain — comfortably above 0.8 % breakeven, below 1.2 % trail
        new_sl, transition = update_trailing_stop_pct(
            sl_price=98.0, entry_price=100.0, close=101.0, side='long',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        self.assertEqual(new_sl, 100.0)
        self.assertEqual(transition, 'breakeven')

    def test_trailing_when_gain_reaches_trail_threshold(self):
        new_sl, transition = update_trailing_stop_pct(
            sl_price=98.0, entry_price=100.0, close=101.5, side='long',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        # 101.5 * (1-0.004) = 101.094
        self.assertAlmostEqual(new_sl, 101.094, places=3)
        self.assertEqual(transition, 'trailing')

    def test_sl_only_moves_up_for_long(self):
        new_sl, transition = update_trailing_stop_pct(
            sl_price=101.5, entry_price=100.0, close=101.6, side='long',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        # 101.6 * 0.996 = 101.1936 < existing 101.5 → no change
        self.assertEqual(new_sl, 101.5)
        self.assertIsNone(transition)


class TestUpdateTrailingStopPctShort(unittest.TestCase):
    def test_breakeven_for_short_when_price_drops(self):
        # 1.0 % gain on short — close at 99.0 from entry 100.0
        new_sl, transition = update_trailing_stop_pct(
            sl_price=103.5, entry_price=100.0, close=99.0, side='short',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        self.assertEqual(new_sl, 100.0)
        self.assertEqual(transition, 'breakeven')

    def test_trailing_for_short(self):
        new_sl, transition = update_trailing_stop_pct(
            sl_price=103.5, entry_price=100.0, close=98.5, side='short',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        # 98.5 * 1.004 = 98.894
        self.assertAlmostEqual(new_sl, 98.894, places=3)
        self.assertEqual(transition, 'trailing')

    def test_sl_only_moves_down_for_short(self):
        new_sl, transition = update_trailing_stop_pct(
            sl_price=98.0, entry_price=100.0, close=98.4, side='short',
            breakeven_at_pct=0.008, trail_at_pct=0.012, trail_distance_pct=0.004,
        )
        # candidate 98.4*1.004 = 98.7936 > existing 98.0 → no change
        self.assertEqual(new_sl, 98.0)


class TestTightenSlTp(unittest.TestCase):
    def test_long_halves_distances(self):
        new_sl, new_tp = tighten_sl_tp_for_stalled(
            sl_price=98.0, tp_price=104.0, entry_price=100.0, side='long',
        )
        self.assertEqual(new_sl, 99.0)   # midway between 98 and 100
        self.assertEqual(new_tp, 102.0)  # midway between 100 and 104

    def test_short_halves_distances(self):
        new_sl, new_tp = tighten_sl_tp_for_stalled(
            sl_price=103.5, tp_price=94.0, entry_price=100.0, side='short',
        )
        self.assertEqual(new_sl, 101.75)  # midway between 100 and 103.5
        self.assertEqual(new_tp, 97.0)    # midway between 94 and 100

    def test_invalid_side_raises(self):
        with self.assertRaises(ValueError):
            tighten_sl_tp_for_stalled(98.0, 104.0, 100.0, side='neutral')


if __name__ == '__main__':
    unittest.main()
