"""Tests for analytics/regime_classifier.py."""
import unittest

from analytics.regime_classifier import classify_regime, percentile_of


class TestClassifyRegime(unittest.TestCase):
    def test_trending(self):
        self.assertEqual(classify_regime(adx_val=30, atr_percentile=50,
                                         range_quiet=False), 'trending')

    def test_volatile_high_atr_pct(self):
        self.assertEqual(classify_regime(adx_val=30, atr_percentile=85,
                                         range_quiet=False), 'volatile')

    def test_ranging_low_adx(self):
        self.assertEqual(classify_regime(adx_val=15, atr_percentile=50,
                                         range_quiet=False), 'ranging')

    def test_ranging_when_quiet(self):
        self.assertEqual(classify_regime(adx_val=22, atr_percentile=50,
                                         range_quiet=True), 'ranging')

    def test_unknown_warmup(self):
        self.assertEqual(classify_regime(None, None, False), 'unknown')


class TestPercentileOf(unittest.TestCase):
    def test_returns_none_for_short_history(self):
        self.assertIsNone(percentile_of(5.0, [1, 2, 3]))

    def test_percentile_in_middle(self):
        history = sorted([float(i) for i in range(1, 101)])
        self.assertEqual(percentile_of(50.0, history), 49.0)

    def test_percentile_zero(self):
        history = sorted([float(i) for i in range(1, 101)])
        self.assertEqual(percentile_of(0.5, history), 0.0)

    def test_percentile_max(self):
        history = sorted([float(i) for i in range(1, 101)])
        self.assertEqual(percentile_of(99.5, history), 99.0)


if __name__ == '__main__':
    unittest.main()
