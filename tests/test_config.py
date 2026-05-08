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
        # Loosened thresholds (45/53) used since the May-2026 low-vol regime;
        # the new MTF + volatility filters compensate for the wider band.
        self.assertEqual(RSI_LONG_THRESHOLD, 45.0)
        self.assertEqual(RSI_SHORT_THRESHOLD, 53.0)

    def test_protections_defaults_permissive(self):
        from config import COOLDOWN_SECONDS, MAX_SL_PER_DAY
        self.assertEqual(COOLDOWN_SECONDS, 0)
        self.assertEqual(MAX_SL_PER_DAY, 10)


class TestNewFilterConstants(unittest.TestCase):
    def test_volatility_filter_defaults(self):
        from config import (USE_VOLATILITY_FILTER, VOLATILITY_LOOKBACK_HOURS,
                            VOLATILITY_LOW_PERCENTILE, VOLATILITY_HIGH_PERCENTILE)
        self.assertTrue(USE_VOLATILITY_FILTER)
        self.assertEqual(VOLATILITY_LOOKBACK_HOURS, 48)
        self.assertEqual(VOLATILITY_LOW_PERCENTILE, 20.0)
        self.assertEqual(VOLATILITY_HIGH_PERCENTILE, 80.0)

    def test_range_quiet_market_defaults(self):
        from config import RANGE_LOOKBACK_MIN, RANGE_PCT_THRESHOLD
        self.assertEqual(RANGE_LOOKBACK_MIN, 120)
        self.assertEqual(RANGE_PCT_THRESHOLD, 0.003)

    def test_mtf_filter_defaults(self):
        from config import (USE_MTF_FILTER, MTF_15M_PERIOD,
                            MTF_REQUIRE_15M, MTF_REQUIRE_1H)
        self.assertTrue(USE_MTF_FILTER)
        self.assertEqual(MTF_15M_PERIOD, 50)
        self.assertTrue(MTF_REQUIRE_15M)
        self.assertFalse(MTF_REQUIRE_1H)

    def test_short_trend_filter_defaults(self):
        from config import (USE_SHORT_TREND_FILTER, SHORT_ADX_MIN,
                            SHORT_SMA_PERIOD, ADX_FLAT_THRESHOLD)
        self.assertTrue(USE_SHORT_TREND_FILTER)
        self.assertEqual(SHORT_ADX_MIN, 20.0)
        self.assertEqual(SHORT_SMA_PERIOD, 50)
        self.assertEqual(ADX_FLAT_THRESHOLD, 18.0)

    def test_trailing_stop_defaults(self):
        from config import (USE_TRAILING_STOP, TRAILING_BREAKEVEN_AT_PCT,
                            TRAILING_TRAIL_AT_PCT, TRAILING_DISTANCE_PCT)
        self.assertTrue(USE_TRAILING_STOP)
        self.assertEqual(TRAILING_BREAKEVEN_AT_PCT, 0.008)
        self.assertEqual(TRAILING_TRAIL_AT_PCT, 0.012)
        self.assertEqual(TRAILING_DISTANCE_PCT, 0.004)

    def test_stalled_position_defaults(self):
        from config import STALLED_HOURS, STALLED_MOVE_THRESHOLD
        self.assertEqual(STALLED_HOURS, 6.0)
        self.assertEqual(STALLED_MOVE_THRESHOLD, 0.005)

    def test_session_filter_defaults(self):
        from config import USE_SESSION_FILTER, BLOCKED_SESSIONS
        self.assertTrue(USE_SESSION_FILTER)
        self.assertIn('off', BLOCKED_SESSIONS)


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
            'use_volatility_filter', 'volatility_lookback_hours',
            'use_mtf_filter', 'use_session_filter', 'blocked_sessions',
            'use_short_trend_filter', 'short_adx_min', 'short_sma_period',
            'adx_flat_threshold', 'stalled_hours', 'stalled_move_threshold',
            'range_lookback_min', 'range_pct_threshold',
            'trailing_breakeven_pct', 'trailing_trail_pct', 'trailing_distance_pct',
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
