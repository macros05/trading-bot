"""Tests for telegram_commands.py."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram_commands


class TestHandleCommand(unittest.TestCase):
    def test_unknown_text_returns_none(self):
        self.assertIsNone(telegram_commands.handle_command('hola'))

    def test_pause_command_sets_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch.object(telegram_commands, '_DATA_DIR', tmpdir), \
                 patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                out = telegram_commands.handle_command('/pause')
                self.assertIn('paused', out.lower())
                self.assertTrue((tmpdir / 'pause.flag').exists())

    def test_resume_command_clears_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            pause_file = tmpdir / 'pause.flag'
            pause_file.touch()
            with patch.object(telegram_commands, '_DATA_DIR', tmpdir), \
                 patch('notifications._PAUSE_FILE', pause_file):
                out = telegram_commands.handle_command('/resume')
                self.assertIn('resumed', out.lower())
                self.assertFalse(pause_file.exists())

    def test_stats_command_with_no_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch.object(telegram_commands, '_TRADES_FILE',
                              tmpdir / 'trades.json'), \
                 patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                out = telegram_commands.handle_command('/stats')
                self.assertIn('Trades: 0', out)

    def test_spanish_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch.object(telegram_commands, '_DATA_DIR', tmpdir), \
                 patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                self.assertIsNotNone(telegram_commands.handle_command('/pausar'))
                self.assertIsNotNone(telegram_commands.handle_command('/activar'))


class TestStatsContent(unittest.TestCase):
    def test_includes_per_side_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            trades = [
                {'side': 'long', 'pnl_usdt': 5.0, 'pnl_pct': 0.5,
                 'result': 'WIN', 'reason': 'tp',
                 'entry_ts': 1714579200000, 'exit_ts': 1714582800000,
                 'entry_price': 100, 'exit_price': 105, 'qty': 1},
                {'side': 'short', 'pnl_usdt': -2.0, 'pnl_pct': -0.2,
                 'result': 'LOSS', 'reason': 'sl',
                 'entry_ts': 1714579200000, 'exit_ts': 1714582800000,
                 'entry_price': 100, 'exit_price': 102, 'qty': 1},
            ]
            (tmpdir / 'trades.json').write_text(json.dumps(trades))
            with patch.object(telegram_commands, '_TRADES_FILE',
                              tmpdir / 'trades.json'), \
                 patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                out = telegram_commands.cmd_stats()
                self.assertIn('Long', out)
                self.assertIn('Short', out)
                self.assertIn('PnL', out)


if __name__ == '__main__':
    unittest.main()
