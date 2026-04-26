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


if __name__ == '__main__':
    unittest.main()
