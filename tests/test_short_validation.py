"""Smoke test for the short-positions aggressive-profile gate script.

Verifies the module exposes run_validation() returning the expected three-config
report shape. Does not assert on profitability — that is what the actual gate
run (Step 6 of Task 3 in the plan) is for.
"""
import unittest


class TestShortValidationScript(unittest.TestCase):
    def test_module_exposes_run_validation(self):
        from backtest import short_validation
        self.assertTrue(callable(getattr(short_validation, 'run_validation', None)))

    def test_run_validation_returns_three_configs(self):
        """run_validation() with mock candles returns long_only, short_only, combined."""
        from backtest.short_validation import run_validation
        candles = []
        ts = 1700000000000
        price = 100.0
        for i in range(500):
            candles.append({
                'ts': ts + i * 60_000,
                'open': price, 'high': price + 1, 'low': price - 1,
                'close': price + (1 if i % 4 < 2 else -1),
                'volume': 100.0,
            })
            price = price + (1 if i % 4 < 2 else -1)
        results = run_validation(
            candles, sl_pct=0.035, tp_pct=0.06,
            rsi_long_threshold=45, rsi_short_threshold=55,
        )
        self.assertIn('long_only', results)
        self.assertIn('short_only', results)
        self.assertIn('combined', results)
        for cfg_name, cfg in results.items():
            self.assertIn('trades', cfg)
            self.assertIn('pnl_usdt', cfg)
            self.assertIn('sharpe', cfg)


if __name__ == '__main__':
    unittest.main()
