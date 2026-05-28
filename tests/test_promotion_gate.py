"""Tests for the anti-overfitting champion-promotion gate.

The gate decides whether a backtested sweep config is statistically allowed to
become the live champion. It enforces the two hard rules from the 2026-05-27/28
audit: DSR p-value >= 0.95 (López de Prado backtest-selection-bias test) AND a
trade count large enough to be statistically meaningful, PLUS the win-rate lower
bound must beat the fee-adjusted breakeven (otherwise the edge can be negative).
"""
import unittest

from backtest.promotion_gate import (
    GateThresholds,
    evaluate_config,
    select_champion,
)


def _good_result(**overrides):
    """A synthetic config that passes every gate, before overrides."""
    base = {
        'label': 'synthetic_passing',
        'dsr_pvalue': 0.97,
        'num_trades': 150,
        'wr_lower_95': 55.0,
        'breakeven_wr_long': 43.08,
        'breakeven_wr_short': 40.0,
        'sharpe_annual': 1.4,
        'net_pnl_pct': 22.0,
        'params': {'rsi_threshold': 40.0},
    }
    base.update(overrides)
    return base


class TestEvaluateConfig(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_passes_when_all_thresholds_met(self):
        verdict = evaluate_config(_good_result(), self.thr)
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.reasons, [])

    def test_rejects_low_dsr(self):
        verdict = evaluate_config(_good_result(dsr_pvalue=0.0), self.thr)
        self.assertFalse(verdict.passed)
        self.assertTrue(any('dsr' in r.lower() for r in verdict.reasons))

    def test_rejects_too_few_trades(self):
        verdict = evaluate_config(_good_result(num_trades=16), self.thr)
        self.assertFalse(verdict.passed)
        self.assertTrue(any('trade' in r.lower() for r in verdict.reasons))

    def test_rejects_win_rate_lower_bound_below_breakeven(self):
        # wr_lower_95 below the long breakeven -> the edge may be negative
        verdict = evaluate_config(
            _good_result(wr_lower_95=38.64, breakeven_wr_long=43.08), self.thr
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(
            any('breakeven' in r.lower() or 'win' in r.lower()
                for r in verdict.reasons)
        )

    def test_breakeven_check_uses_strictest_side(self):
        # wr_lb between short(40) and long(43) breakeven must still fail
        verdict = evaluate_config(
            _good_result(wr_lower_95=41.0,
                         breakeven_wr_long=43.08,
                         breakeven_wr_short=40.0),
            self.thr,
        )
        self.assertFalse(verdict.passed)

    def test_missing_dsr_key_is_treated_as_failure_not_crash(self):
        r = _good_result()
        del r['dsr_pvalue']
        verdict = evaluate_config(r, self.thr)
        self.assertFalse(verdict.passed)
        self.assertTrue(any('dsr' in r_.lower() for r_ in verdict.reasons))

    def test_verdict_records_metrics_and_label(self):
        verdict = evaluate_config(_good_result(), self.thr)
        self.assertEqual(verdict.label, 'synthetic_passing')
        self.assertEqual(verdict.metrics['num_trades'], 150)
        self.assertEqual(verdict.metrics['dsr_pvalue'], 0.97)


class TestSelectChampion(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_returns_none_when_no_config_passes(self):
        results = [
            _good_result(label='a', dsr_pvalue=0.0),
            _good_result(label='b', num_trades=10),
        ]
        self.assertIsNone(select_champion(results, self.thr))

    def test_picks_highest_sharpe_among_passing(self):
        results = [
            _good_result(label='lo', sharpe_annual=1.1),
            _good_result(label='hi', sharpe_annual=2.3),
            _good_result(label='bad', dsr_pvalue=0.1),  # fails gate
        ]
        champ = select_champion(results, self.thr)
        self.assertIsNotNone(champ)
        self.assertEqual(champ.label, 'hi')


if __name__ == '__main__':
    unittest.main()
