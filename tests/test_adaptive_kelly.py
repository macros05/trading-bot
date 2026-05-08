"""Tests for the adaptive Kelly logic in risk/manager.py."""
import unittest

from risk.manager import (
    KELLY_BOOST_FACTOR, KELLY_BOOST_THRESHOLD, KELLY_CAP_PCT,
    KELLY_DAMPEN_FACTOR, KELLY_DAMPEN_THRESHOLD, KELLY_FLOOR_PCT,
    RiskManager,
)


def _t(result: str) -> dict:
    return {'result': result, 'pnl_usdt': 1.0 if result == 'WIN' else -1.0}


class TestAdaptiveKelly(unittest.TestCase):
    def test_disabled_by_default(self):
        rm = RiskManager(adaptive_kelly=False, base_risk_pct=0.02)
        self.assertIsNone(rm.update_adaptive_kelly([_t('WIN')] * 10))
        self.assertEqual(rm.effective_risk_pct, 0.02)

    def test_no_change_below_window(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=0.02)
        # 9 trades < window of 10 → no-op
        self.assertIsNone(rm.update_adaptive_kelly([_t('WIN')] * 9))

    def test_boost_when_win_rate_above_threshold(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=0.015)
        # 10 wins → 100 % WR > 50 % threshold
        change = rm.update_adaptive_kelly([_t('WIN')] * 10)
        self.assertIsNotNone(change)
        self.assertAlmostEqual(change['new_kelly_pct'], 0.015 * KELLY_BOOST_FACTOR)
        self.assertAlmostEqual(rm.effective_risk_pct, 0.015 * KELLY_BOOST_FACTOR)

    def test_dampen_when_win_rate_below_threshold(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=0.02)
        # 7 losses + 3 wins → 30 % WR < 35 % threshold
        trades = [_t('LOSS')] * 7 + [_t('WIN')] * 3
        change = rm.update_adaptive_kelly(trades)
        self.assertIsNotNone(change)
        self.assertAlmostEqual(change['new_kelly_pct'], 0.02 * KELLY_DAMPEN_FACTOR)

    def test_does_not_exceed_cap(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=KELLY_CAP_PCT)
        change = rm.update_adaptive_kelly([_t('WIN')] * 10)
        # Already at cap → boost factor would exceed but is clamped
        self.assertEqual(rm.effective_risk_pct, KELLY_CAP_PCT)
        # Either no change record or change record at cap
        if change is not None:
            self.assertEqual(change['new_kelly_pct'], KELLY_CAP_PCT)

    def test_does_not_drop_below_floor(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=KELLY_FLOOR_PCT)
        trades = [_t('LOSS')] * 9 + [_t('WIN')]
        rm.update_adaptive_kelly(trades)
        self.assertGreaterEqual(rm.effective_risk_pct, KELLY_FLOOR_PCT)

    def test_neutral_win_rate_no_change(self):
        rm = RiskManager(adaptive_kelly=True, base_risk_pct=0.02)
        trades = [_t('WIN')] * 4 + [_t('LOSS')] * 6  # 40 %
        # Between 35 and 50 → no change
        self.assertIsNone(rm.update_adaptive_kelly(trades))


if __name__ == '__main__':
    unittest.main()
