"""Tests for strategy/regime.py — pure regime/market-condition helpers."""
import unittest

from strategy.regime import (
    atr_percentile_bounds,
    is_mtf_aligned,
    is_position_stalled,
    is_quiet_range,
    passes_short_trend_filter,
    passes_volatility_window,
    shorts_disabled_in_flat,
)


class TestIsQuietRange(unittest.TestCase):
    def test_flat_market_detected(self):
        # 0.2% range over the window
        prices = [100.0] * 60 + [100.2] * 60
        self.assertTrue(is_quiet_range(prices, range_pct_threshold=0.003))

    def test_active_market_not_flagged(self):
        prices = [100.0, 100.5, 101.0, 99.5, 100.5]
        self.assertFalse(is_quiet_range(prices, range_pct_threshold=0.003))

    def test_too_short_history_returns_false(self):
        self.assertFalse(is_quiet_range([100.0], range_pct_threshold=0.003))
        self.assertFalse(is_quiet_range([], range_pct_threshold=0.003))

    def test_zero_floor_safe(self):
        self.assertFalse(is_quiet_range([0.0, 0.1], range_pct_threshold=0.003))


class TestIsPositionStalled(unittest.TestCase):
    def test_no_movement_flagged(self):
        # base 100, max move 0.3 % → stalled at 0.5 % threshold
        closes = [100.0, 100.1, 99.9, 100.2, 100.0]
        self.assertTrue(is_position_stalled(closes, move_pct_threshold=0.005))

    def test_movement_above_threshold_not_stalled(self):
        closes = [100.0, 100.5, 101.0, 100.0]
        self.assertFalse(is_position_stalled(closes, move_pct_threshold=0.005))

    def test_empty_returns_false(self):
        self.assertFalse(is_position_stalled([], move_pct_threshold=0.005))


class TestAtrPercentileBounds(unittest.TestCase):
    def test_returns_none_for_short_history(self):
        self.assertIsNone(atr_percentile_bounds([0.5] * 10))

    def test_p20_p80_within_range(self):
        history = [float(i) for i in range(1, 101)]
        bounds = atr_percentile_bounds(history, low_p=20, high_p=80)
        self.assertIsNotNone(bounds)
        low, high = bounds
        self.assertEqual(low, 20.0)
        self.assertEqual(high, 80.0)

    def test_drops_zeros_and_nones(self):
        history = [0.0, None, *[1.0] * 30] + [2.0] * 70
        bounds = atr_percentile_bounds(history, low_p=10, high_p=90)
        self.assertIsNotNone(bounds)


class TestPassesVolatilityWindow(unittest.TestCase):
    def test_inside_range(self):
        self.assertTrue(passes_volatility_window(50.0, (20.0, 80.0)))

    def test_below_range(self):
        self.assertFalse(passes_volatility_window(10.0, (20.0, 80.0)))

    def test_above_range(self):
        self.assertFalse(passes_volatility_window(90.0, (20.0, 80.0)))

    def test_none_bounds_passes(self):
        self.assertTrue(passes_volatility_window(50.0, None))

    def test_none_atr_passes(self):
        self.assertTrue(passes_volatility_window(None, (20.0, 80.0)))


class TestIsMtfAligned(unittest.TestCase):
    def test_long_aligned_with_bullish_15m(self):
        self.assertTrue(is_mtf_aligned('long', htf_bullish_15m=True,
                                       htf_bullish_1h=None))

    def test_short_aligned_with_bearish_15m(self):
        self.assertTrue(is_mtf_aligned('short', htf_bullish_15m=False,
                                       htf_bullish_1h=None))

    def test_long_blocked_by_bearish_15m(self):
        self.assertFalse(is_mtf_aligned('long', htf_bullish_15m=False,
                                        htf_bullish_1h=None))

    def test_warmup_none_does_not_block(self):
        self.assertTrue(is_mtf_aligned('long', htf_bullish_15m=None,
                                       htf_bullish_1h=None))

    def test_1h_only_when_required(self):
        # 1h says bearish but require_1h=False → no block
        self.assertTrue(is_mtf_aligned(
            'long', htf_bullish_15m=True, htf_bullish_1h=False,
            require_15m=True, require_1h=False,
        ))
        # require_1h=True → blocked
        self.assertFalse(is_mtf_aligned(
            'long', htf_bullish_15m=True, htf_bullish_1h=False,
            require_15m=True, require_1h=True,
        ))


class TestPassesShortTrendFilter(unittest.TestCase):
    def test_passes_when_below_sma_and_strong_adx(self):
        self.assertTrue(passes_short_trend_filter(100.0, sma50=110.0,
                                                  adx_val=25.0, adx_min=20.0))

    def test_blocked_when_above_sma(self):
        self.assertFalse(passes_short_trend_filter(110.0, sma50=100.0,
                                                   adx_val=25.0, adx_min=20.0))

    def test_blocked_when_low_adx(self):
        self.assertFalse(passes_short_trend_filter(100.0, sma50=110.0,
                                                   adx_val=15.0, adx_min=20.0))

    def test_warmup_skips_checks(self):
        self.assertTrue(passes_short_trend_filter(100.0, sma50=None,
                                                  adx_val=None, adx_min=20.0))


class TestShortsDisabledInFlat(unittest.TestCase):
    def test_flat_market_disables(self):
        self.assertTrue(shorts_disabled_in_flat(adx_val=15.0,
                                                adx_flat_threshold=18.0))

    def test_trending_market_does_not_disable(self):
        self.assertFalse(shorts_disabled_in_flat(adx_val=20.0,
                                                 adx_flat_threshold=18.0))

    def test_warmup_does_not_disable(self):
        self.assertFalse(shorts_disabled_in_flat(adx_val=None,
                                                 adx_flat_threshold=18.0))


if __name__ == '__main__':
    unittest.main()
