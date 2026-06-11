import unittest


class TestCooldownPeriod(unittest.TestCase):
    def test_zero_cooldown_never_blocks(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=0)
        trades = [{'reason': 'stop_loss', 'exit_ts': 1000}]
        blocked, _ = cp.is_blocked(now_ms=2000, trades_history=trades)
        self.assertFalse(blocked)

    def test_blocks_within_cooldown_window(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        trades = [{'reason': 'stop_loss', 'exit_ts': 1_000_000}]
        blocked, reason = cp.is_blocked(now_ms=1_030_000, trades_history=trades)
        self.assertTrue(blocked)
        self.assertIn('cooldown', reason.lower())

    def test_releases_after_cooldown(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        trades = [{'reason': 'stop_loss', 'exit_ts': 1_000_000}]
        blocked, _ = cp.is_blocked(now_ms=1_061_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_only_stop_loss_triggers_cooldown_not_take_profit(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        trades = [{'reason': 'take_profit', 'exit_ts': 1_000_000}]
        blocked, _ = cp.is_blocked(now_ms=1_010_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_no_trades_never_blocks(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        blocked, _ = cp.is_blocked(now_ms=1_000_000, trades_history=[])
        self.assertFalse(blocked)


class TestStoplossGuard(unittest.TestCase):
    def _make_sl_trades(self, count: int, base_ts: int = 1_000_000_000):
        return [{'reason': 'stop_loss', 'exit_ts': base_ts + i * 60_000}
                for i in range(count)]

    def test_permits_below_threshold(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=10, lookback_seconds=86_400)
        trades = self._make_sl_trades(9)
        blocked, _ = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_blocks_at_threshold(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=10, lookback_seconds=86_400)
        trades = self._make_sl_trades(10)
        blocked, reason = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertTrue(blocked)
        self.assertIn('stoploss', reason.lower())

    def test_only_counts_within_lookback(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=3, lookback_seconds=600)
        old = [{'reason': 'stop_loss', 'exit_ts': 1_000_000_000 + i * 60_000}
               for i in range(5)]
        blocked, _ = guard.is_blocked(now_ms=1_000_000_000 + 1_500_000, trades_history=old)
        self.assertFalse(blocked)

    def test_only_counts_stop_loss_not_take_profit(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=2, lookback_seconds=86_400)
        trades = [{'reason': 'take_profit', 'exit_ts': 1_000_000_000 + i * 60_000}
                  for i in range(10)]
        blocked, _ = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertFalse(blocked)


class TestProtectionStack(unittest.TestCase):
    def test_empty_stack_never_blocks(self):
        from risk.protections import ProtectionStack
        stack = ProtectionStack([])
        blocked, _ = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertFalse(blocked)

    def test_short_circuits_on_first_block(self):
        from risk.protections import ProtectionStack

        class _AlwaysBlocks:
            def __init__(self, name): self.name = name
            def is_blocked(self, now_ms, trades_history):
                return True, f'block from {self.name}'

        class _NeverBlocks:
            def is_blocked(self, now_ms, trades_history):
                raise AssertionError('should not be called')

        stack = ProtectionStack([_AlwaysBlocks('first'), _NeverBlocks()])
        blocked, reason = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertTrue(blocked)
        self.assertEqual(reason, 'block from first')

    def test_passes_when_all_protections_allow(self):
        from risk.protections import ProtectionStack

        class _NeverBlocks:
            def is_blocked(self, now_ms, trades_history):
                return False, None

        stack = ProtectionStack([_NeverBlocks(), _NeverBlocks()])
        blocked, _ = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertFalse(blocked)


if __name__ == '__main__':
    unittest.main()
