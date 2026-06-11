"""Direct tests for the Deflated-Sharpe-Ratio statistic.

This statistic is the keystone of the anti-overfit promotion gate, yet it
shipped (commit 7867ccf) with zero direct coverage and a degenerate bug: the
expected-maximum-Sharpe bar omitted the cross-trial σ_SR scaling, so DSR ≥ 0.95
was unreachable for ANY real strategy. These tests lock in that the bar is both
*reachable* by a genuinely strong edge and *unforgiving* of luck / small
samples — i.e. that it discriminates rather than rejecting everything.
"""
import statistics
import unittest

from backtest.sweep_v7_full import deflated_sharpe_pvalue as dsr

# A realistic spread of per-trial Sharpes across a sweep; σ_SR ≈ 0.23, matching
# the ~0.244 measured on the real BTC sweep in the audit.
SR_SAMPLE = [-0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.15, 0.2,
             0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6]


class TestReachability(unittest.TestCase):
    """The whole point of the fix: a real strong edge CAN clear the bar."""

    def test_strong_edge_large_sample_passes(self):
        # per-trade SR 0.6, 300 trades, 15 trials — a genuinely strong but not
        # physically-impossible edge. Must clear 0.95.
        self.assertGreaterEqual(dsr(0.6, 300, 15, sr_sample=SR_SAMPLE), 0.95)

    def test_old_degenerate_behaviour_is_gone(self):
        # The bug WAS: σ_SR implicitly 1.0 -> bar ~4x too high -> even a strong
        # edge scored 0.0. The σ_SR=1.0 fallback (no sample) reproduces it,
        # proving the sample-driven path is what makes the bar reachable.
        self.assertEqual(dsr(0.6, 300, 15), 0.0)
        self.assertGreater(dsr(0.6, 300, 15, sr_sample=SR_SAMPLE), 0.0)


class TestTrueNegatives(unittest.TestCase):
    def test_losing_series_scores_zero(self):
        self.assertLess(dsr(-0.2, 300, 15, sr_sample=SR_SAMPLE), 0.05)

    def test_flat_series_scores_low(self):
        self.assertLess(dsr(0.05, 300, 15, sr_sample=SR_SAMPLE), 0.5)

    def test_small_sample_fails_on_power(self):
        # Same strong per-trade edge but only 20 trades -> cannot clear the bar.
        self.assertLess(dsr(0.6, 20, 15, sr_sample=SR_SAMPLE), 0.95)


class TestMonotonicity(unittest.TestCase):
    def test_nondecreasing_in_sample_size(self):
        ps = [dsr(0.5, n, 15, sr_sample=SR_SAMPLE)
              for n in (50, 100, 200, 400, 800)]
        self.assertEqual(ps, sorted(ps))

    def test_nonincreasing_in_trials(self):
        # More configs tried => stricter selection-bias correction => lower p.
        ps = [dsr(0.5, 300, k, sr_sample=SR_SAMPLE)
              for k in (2, 5, 15, 50, 200)]
        self.assertEqual(ps, sorted(ps, reverse=True))


class TestGuards(unittest.TestCase):
    def test_too_few_returns_returns_zero(self):
        self.assertEqual(dsr(0.6, 3, 15, sr_sample=SR_SAMPLE), 0.0)

    def test_sigma_fallback_is_conservative_not_fail_open(self):
        # With <2 trial Sharpes we cannot estimate σ_SR; fall back to 1.0, which
        # OVER-blocks (the safe direction), never silently passes.
        self.assertEqual(dsr(0.6, 300, 15, sr_sample=[0.6]), 0.0)
        self.assertEqual(dsr(0.6, 300, 15, sr_sample=None), 0.0)


if __name__ == '__main__':
    unittest.main()
