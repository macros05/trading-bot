"""Regression tests for risk-critical config constants.

These assertions guard against accidental edits to leverage, sizing, and
breaker thresholds. If you intentionally change one of these, update the
expected value AND ensure the spec at
docs/superpowers/specs/2026-04-26-short-positions-design.md is updated too.
"""
import unittest


class TestAggressiveProfileConstants(unittest.TestCase):
    def test_risk_pct(self):
        from config import RISK_PCT
        self.assertEqual(RISK_PCT, 0.02)

    def test_stop_loss_per_side(self):
        from config import STOP_LOSS_PCT_LONG, STOP_LOSS_PCT_SHORT
        self.assertEqual(STOP_LOSS_PCT_LONG, 0.025)
        self.assertEqual(STOP_LOSS_PCT_SHORT, 0.035)

    def test_take_profit_per_side(self):
        from config import TAKE_PROFIT_PCT_LONG, TAKE_PROFIT_PCT_SHORT
        self.assertEqual(TAKE_PROFIT_PCT_LONG, 0.040)
        self.assertEqual(TAKE_PROFIT_PCT_SHORT, 0.060)

    def test_leverage(self):
        from config import LEVERAGE
        self.assertEqual(LEVERAGE, 2)

    def test_circuit_breaker(self):
        from config import CIRCUIT_BREAKER_PCT
        self.assertEqual(CIRCUIT_BREAKER_PCT, 0.05)

    def test_rsi_thresholds(self):
        from config import RSI_LONG_THRESHOLD, RSI_SHORT_THRESHOLD
        self.assertEqual(RSI_LONG_THRESHOLD, 40.0)
        self.assertEqual(RSI_SHORT_THRESHOLD, 55.0)

    def test_protections_defaults_permissive(self):
        from config import COOLDOWN_SECONDS, MAX_SL_PER_DAY
        self.assertEqual(COOLDOWN_SECONDS, 0)
        self.assertEqual(MAX_SL_PER_DAY, 10)


class TestBotConfigKeys(unittest.TestCase):
    def test_all_required_keys_present(self):
        from config import BOT_CONFIG
        required = {
            'symbol', 'timeframe', 'limit', 'interval_seconds', 'paper_balance',
            'risk_pct', 'rsi_threshold', 'rsi_short_threshold',
            'stop_loss_pct_long', 'stop_loss_pct_short',
            'take_profit_pct_long', 'take_profit_pct_short',
            'circuit_breaker_pct',
            'leverage', 'cooldown_seconds', 'max_sl_per_day',
            'use_atr_exits', 'atr_period', 'atr_sl_multiplier', 'atr_tp_multiplier',
            'use_trailing_stop', 'use_adx_filter', 'adx_period',
            'adx_threshold', 'use_trend_filter',
        }
        missing = required - set(BOT_CONFIG.keys())
        self.assertEqual(missing, set(), f'Missing keys: {missing}')

    def test_bot_config_values_match_constants(self):
        from config import (BOT_CONFIG, RISK_PCT, RSI_LONG_THRESHOLD,
                            RSI_SHORT_THRESHOLD,
                            STOP_LOSS_PCT_LONG, STOP_LOSS_PCT_SHORT,
                            TAKE_PROFIT_PCT_LONG, TAKE_PROFIT_PCT_SHORT,
                            CIRCUIT_BREAKER_PCT, LEVERAGE, COOLDOWN_SECONDS,
                            MAX_SL_PER_DAY)
        self.assertEqual(BOT_CONFIG['risk_pct'], RISK_PCT)
        self.assertEqual(BOT_CONFIG['rsi_threshold'], RSI_LONG_THRESHOLD)
        self.assertEqual(BOT_CONFIG['rsi_short_threshold'], RSI_SHORT_THRESHOLD)
        self.assertEqual(BOT_CONFIG['stop_loss_pct_long'], STOP_LOSS_PCT_LONG)
        self.assertEqual(BOT_CONFIG['stop_loss_pct_short'], STOP_LOSS_PCT_SHORT)
        self.assertEqual(BOT_CONFIG['take_profit_pct_long'], TAKE_PROFIT_PCT_LONG)
        self.assertEqual(BOT_CONFIG['take_profit_pct_short'], TAKE_PROFIT_PCT_SHORT)
        self.assertEqual(BOT_CONFIG['circuit_breaker_pct'], CIRCUIT_BREAKER_PCT)
        self.assertEqual(BOT_CONFIG['leverage'], LEVERAGE)
        self.assertEqual(BOT_CONFIG['cooldown_seconds'], COOLDOWN_SECONDS)
        self.assertEqual(BOT_CONFIG['max_sl_per_day'], MAX_SL_PER_DAY)


if __name__ == '__main__':
    unittest.main()
