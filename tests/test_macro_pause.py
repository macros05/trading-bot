"""Tests for analytics/macro_pause.py."""
import unittest

from analytics.macro_pause import should_auto_pause_for_macro


def _trade(event: str, result: str) -> dict:
    return {'macro_event': event, 'result': result, 'pnl_usdt': 0.0}


class TestMacroAutoPause(unittest.TestCase):
    def test_no_pause_when_event_not_high_impact(self):
        trades = [_trade('FOMC', 'LOSS') for _ in range(10)]
        block, _ = should_auto_pause_for_macro(trades, current_event='WEEKEND')
        self.assertFalse(block)

    def test_no_pause_below_min_trades(self):
        trades = [_trade('FOMC', 'LOSS') for _ in range(3)]
        block, _ = should_auto_pause_for_macro(trades, current_event='FOMC')
        self.assertFalse(block)

    def test_pause_when_history_below_floor(self):
        # 5 FOMC trades, all losses → 0% WR < 25% floor
        trades = [_trade('FOMC', 'LOSS') for _ in range(5)]
        block, reason = should_auto_pause_for_macro(trades, current_event='FOMC')
        self.assertTrue(block)
        self.assertIn('FOMC', reason)

    def test_no_pause_when_history_acceptable(self):
        # 5 FOMC trades, 2 wins → 40% > 25% floor
        trades = [_trade('FOMC', 'WIN' if i < 2 else 'LOSS') for i in range(5)]
        block, _ = should_auto_pause_for_macro(trades, current_event='FOMC')
        self.assertFalse(block)

    def test_only_uses_same_event_history(self):
        # 5 CPI losses but FOMC has 2 wins / 3 trades
        trades = [
            _trade('CPI', 'LOSS') for _ in range(5)
        ] + [
            _trade('FOMC', 'WIN'), _trade('FOMC', 'WIN'), _trade('FOMC', 'LOSS'),
        ]
        # current_event=FOMC → only 3 FOMC trades < min 5
        block, _ = should_auto_pause_for_macro(trades, current_event='FOMC')
        self.assertFalse(block)


if __name__ == '__main__':
    unittest.main()
