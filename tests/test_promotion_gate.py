"""Tests for the anti-overfitting champion-promotion gate.

The gate decides whether a backtested sweep config is statistically allowed to
become the live champion. It enforces the audit's hard rules: DSR p-value >=
0.95, a statistically meaningful trade count, per-side win-rate lower bound
above per-side breakeven, a profitability floor (positive PnL/Sharpe, PF>1), a
max-drawdown ceiling, and out-of-sample fold coverage.
"""
import math
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
        'sharpe_trade': 0.3,
        'net_pnl_pct': 22.0,
        'profit_factor': 1.6,
        'max_drawdown_pct': 8.0,
        'num_folds': 21,
        'folds_with_trades': 18,
        'params': {'rsi_threshold': 40.0},
    }
    base.update(overrides)
    return base


class TestEvaluateConfig(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_passes_when_all_thresholds_met(self):
        verdict = evaluate_config(_good_result(), self.thr)
        self.assertTrue(verdict.passed, verdict.reasons)
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
        verdict = evaluate_config(
            _good_result(wr_lower_95=38.64, breakeven_wr_long=43.08), self.thr
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(
            any('breakeven' in r.lower() or 'win' in r.lower()
                for r in verdict.reasons)
        )

    def test_breakeven_check_uses_strictest_side(self):
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


class TestNonFiniteMetrics(unittest.TestCase):
    """NaN/inf must fail closed, not silently pass every `<` comparison."""

    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_nan_dsr_fails(self):
        v = evaluate_config(_good_result(dsr_pvalue=float('nan')), self.thr)
        self.assertFalse(v.passed)
        self.assertTrue(any('dsr' in r.lower() for r in v.reasons))

    def test_inf_trades_fails(self):
        v = evaluate_config(_good_result(num_trades=float('inf')), self.thr)
        self.assertFalse(v.passed)
        self.assertTrue(any('trade' in r.lower() for r in v.reasons))

    def test_nan_wr_lower_fails(self):
        v = evaluate_config(_good_result(wr_lower_95=float('nan')), self.thr)
        self.assertFalse(v.passed)


class TestProfitabilityFloor(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_negative_pnl_negative_sharpe_low_pf_all_rejected(self):
        v = evaluate_config(
            _good_result(sharpe_annual=-2.0, net_pnl_pct=-30.0,
                         profit_factor=0.7),
            self.thr,
        )
        self.assertFalse(v.passed)
        self.assertTrue(any('net_pnl' in r.lower() for r in v.reasons))
        self.assertTrue(any('sharpe' in r.lower() for r in v.reasons))
        self.assertTrue(any('profit_factor' in r.lower() for r in v.reasons))

    def test_all_wins_profit_factor_none_passes(self):
        # The sweep writes profit_factor=None when there are zero losses; that
        # means "no losses", which should PASS, not be treated as missing.
        v = evaluate_config(_good_result(profit_factor=None), self.thr)
        self.assertTrue(v.passed, v.reasons)

    def test_missing_profit_factor_is_rejected(self):
        r = _good_result()
        del r['profit_factor']
        v = evaluate_config(r, self.thr)
        self.assertFalse(v.passed)
        self.assertTrue(any('profit_factor' in r_.lower() for r_ in v.reasons))


class TestPerSideBreakeven(unittest.TestCase):
    """A config strong on one side and a guaranteed loser on the other must
    fail even when the blended WR lower bound clears the strictest breakeven."""

    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_one_sided_loser_rejected_despite_good_blend(self):
        v = evaluate_config(
            _good_result(
                wr_lower_95=58.63,  # blended LB clears 45
                breakeven_wr_long=45.0, breakeven_wr_short=45.0,
                by_side={
                    'long': {'trades': 200, 'wins': 150, 'wr_lower_95': 68.0},
                    'short': {'trades': 60, 'wins': 18, 'wr_lower_95': 19.9},
                },
            ),
            self.thr,
        )
        self.assertFalse(v.passed)
        self.assertTrue(any('short' in r.lower() for r in v.reasons))

    def test_both_sides_healthy_passes(self):
        v = evaluate_config(
            _good_result(
                breakeven_wr_long=45.0, breakeven_wr_short=45.0,
                by_side={
                    'long': {'trades': 200, 'wins': 150, 'wr_lower_95': 68.0},
                    'short': {'trades': 80, 'wins': 56, 'wr_lower_95': 60.0},
                },
            ),
            self.thr,
        )
        self.assertTrue(v.passed, v.reasons)

    def test_thin_side_falls_back_to_blended(self):
        # short side below min_side_trades -> per-side ignored, blended used.
        v = evaluate_config(
            _good_result(
                wr_lower_95=55.0,
                breakeven_wr_long=43.0, breakeven_wr_short=40.0,
                by_side={
                    'long': {'trades': 200, 'wins': 150, 'wr_lower_95': 68.0},
                    'short': {'trades': 5, 'wins': 1, 'wr_lower_95': 5.0},
                },
            ),
            self.thr,
        )
        self.assertTrue(v.passed, v.reasons)


class TestDrawdownAndFolds(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_excessive_drawdown_rejected(self):
        v = evaluate_config(_good_result(max_drawdown_pct=99.0), self.thr)
        self.assertFalse(v.passed)
        self.assertTrue(any('drawdown' in r.lower() for r in v.reasons))

    def test_low_fold_coverage_rejected(self):
        # earned everything in 1 of 21 OOS folds -> overfit signature
        v = evaluate_config(
            _good_result(num_folds=21, folds_with_trades=1), self.thr
        )
        self.assertFalse(v.passed)
        self.assertTrue(any('fold' in r.lower() for r in v.reasons))

    def test_missing_fold_fields_rejected(self):
        r = _good_result()
        del r['num_folds']
        v = evaluate_config(r, self.thr)
        self.assertFalse(v.passed)


class TestSelectChampion(unittest.TestCase):
    def setUp(self):
        self.thr = GateThresholds(min_dsr=0.95, min_trades=100)

    def test_returns_none_when_no_config_passes(self):
        results = [
            _good_result(label='a', dsr_pvalue=0.0),
            _good_result(label='b', num_trades=10),
        ]
        self.assertIsNone(select_champion(results, self.thr))

    def test_picks_most_robust_passer_by_dsr_margin(self):
        # Both pass; the higher DSR margin (more robust) wins, even though the
        # other has a higher annualised Sharpe (which we deliberately ignore).
        results = [
            _good_result(label='high_sharpe', dsr_pvalue=0.96,
                         sharpe_annual=3.0, sharpe_trade=0.2),
            _good_result(label='robust', dsr_pvalue=0.99,
                         sharpe_annual=1.2, sharpe_trade=0.2),
            _good_result(label='bad', dsr_pvalue=0.1),  # fails gate
        ]
        champ = select_champion(results, self.thr)
        self.assertIsNotNone(champ)
        self.assertEqual(champ.label, 'robust')


if __name__ == '__main__':
    unittest.main()
