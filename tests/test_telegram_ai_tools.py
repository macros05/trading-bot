"""Tests for telegram_ai_tools.py — read-only Gemini function-calling tools.

Everything runs against temp fixture files; no real data files are touched
and no network / exchange I/O can happen (the module must not import the
exchange client at all).
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import telegram_ai_tools as ait


def _ms(days_ago: float) -> int:
    return int((datetime.now(timezone.utc).timestamp() - days_ago * 86_400) * 1000)


def _trade(days_ago: float, pnl: float, result: str, side: str = 'long') -> dict:
    return {
        'side': side,
        'entry_price': 60_000.0,
        'exit_price': 60_000.0 + pnl,
        'qty': 0.01,
        'pnl_usdt': pnl,
        'pnl_pct': round(pnl / 600.0, 4),
        'result': result,
        'reason': 'take_profit' if result == 'WIN' else 'stop_loss',
        'entry_ts': _ms(days_ago) - 3_600_000,
        'exit_ts': _ms(days_ago),
    }


class _FixtureBase(unittest.TestCase):
    """Temp data dir wired into the module's path constants."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'data').mkdir()
        self.trades_file = root / 'data' / 'trades_history.json'
        self.state_file = root / 'data' / 'bot_state.json'
        self.health_file = root / 'data' / 'bot_health.json'
        self.pause_flag = root / 'data' / 'pause.flag'
        self.bot_log = root / 'bot.log'
        self.summary_file = root / 'summary.json'
        self._patches = [
            patch.object(ait, 'TRADES_FILE', self.trades_file),
            patch.object(ait, 'STATE_FILE', self.state_file),
            patch.object(ait, 'HEALTH_FILE', self.health_file),
            patch.object(ait, 'PAUSE_FLAG', self.pause_flag),
            patch.object(ait, 'BOT_LOG', self.bot_log),
            patch.object(ait, 'BACKTEST_SUMMARY', self.summary_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class TestGetTrades(_FixtureBase):

    def _write_trades(self, trades: list) -> None:
        self.trades_file.write_text(json.dumps(trades))

    def test_newest_first_compact_and_serializable(self) -> None:
        self._write_trades([_trade(5, -10.0, 'LOSS'), _trade(1, 25.0, 'WIN')])
        out = ait.get_trades()
        self.assertEqual(out['summary']['count'], 2)
        trades = out['trades']
        self.assertEqual(len(trades), 2)
        # newest first
        self.assertGreater(trades[0]['exit_ts'], trades[1]['exit_ts'])
        # compact shape per spec
        for key in ('id', 'side', 'entry_price', 'exit_price',
                    'pnl_usdt', 'result', 'exit_ts'):
            self.assertIn(key, trades[0])
        self.assertNotIn('qty', trades[0])  # compact: internals dropped
        json.dumps(out)  # must be JSON-serializable

    def test_result_filter_and_limit(self) -> None:
        self._write_trades([_trade(3, -5.0, 'LOSS'),
                            _trade(2, 10.0, 'WIN'),
                            _trade(1, 12.0, 'WIN')])
        out = ait.get_trades(result='win', limit=1)
        self.assertEqual(out['summary']['count'], 2)
        self.assertEqual(len(out['trades']), 1)
        self.assertEqual(out['trades'][0]['result'], 'WIN')
        # limit keeps the NEWEST trade
        self.assertEqual(out['trades'][0]['pnl_usdt'], 12.0)

    def test_days_window_excludes_old_trades(self) -> None:
        self._write_trades([_trade(40, 50.0, 'WIN'), _trade(1, -1.0, 'LOSS')])
        out = ait.get_trades(days=30)
        self.assertEqual(out['summary']['count'], 1)
        self.assertEqual(out['trades'][0]['result'], 'LOSS')

    def test_missing_file_returns_empty_not_raise(self) -> None:
        out = ait.get_trades()
        self.assertEqual(out['summary']['count'], 0)
        self.assertEqual(out['trades'], [])
        json.dumps(out)

    def test_win_rate_and_net_pnl(self) -> None:
        self._write_trades([_trade(2, 10.0, 'WIN'), _trade(1, -4.0, 'LOSS')])
        out = ait.get_trades()
        self.assertEqual(out['summary']['win_rate_pct'], 50.0)
        self.assertAlmostEqual(out['summary']['net_pnl_usdt'], 6.0)


class TestGetDailyPnl(_FixtureBase):

    def test_default_window_is_30_days(self) -> None:
        self.trades_file.write_text(json.dumps([_trade(25, 5.0, 'WIN')]))
        out = ait.get_daily_pnl()
        self.assertEqual(out['window_days'], 30)
        self.assertEqual(out['days_with_trades'], 1)

    def test_per_day_rows_have_spec_fields(self) -> None:
        self.trades_file.write_text(json.dumps([
            _trade(1.01, 10.0, 'WIN'), _trade(1.02, -4.0, 'LOSS'),
            _trade(2.5, 7.0, 'WIN'),
        ]))
        out = ait.get_daily_pnl(days=7)
        json.dumps(out)
        rows = out['daily']
        self.assertEqual(len(rows), 2)
        for row in rows:
            for key in ('date', 'n_trades', 'pnl_usdt', 'win_rate_pct'):
                self.assertIn(key, row)
        two_trade_day = next(r for r in rows if r['n_trades'] == 2)
        self.assertAlmostEqual(two_trade_day['pnl_usdt'], 6.0)
        self.assertEqual(two_trade_day['win_rate_pct'], 50.0)

    def test_no_data(self) -> None:
        out = ait.get_daily_pnl()
        self.assertEqual(out['daily'], [])
        self.assertEqual(out['total_pnl_usdt'], 0)


class TestGetStatus(_FixtureBase):

    def test_full_snapshot(self) -> None:
        self.state_file.write_text(json.dumps({
            'state': 'IN_POSITION',
            'position': {'side': 'long', 'entry_price': 60_000.0, 'qty': 0.01},
            'daily_pnl': -1.5,
        }))
        self.health_file.write_text(json.dumps({
            'last_tick_ms': _ms(0), 'last_close': 60_100.0, 'rsi': 35.2,
            'state': 'IN_POSITION', 'daily_pnl_pct': -0.01,
        }))
        self.pause_flag.touch()
        out = ait.get_status()
        json.dumps(out)
        self.assertEqual(out['bot_state'], 'IN_POSITION')
        self.assertTrue(out['paused'])
        self.assertEqual(out['daily_pnl_usdt'], -1.5)
        self.assertEqual(out['open_position']['side'], 'long')
        self.assertEqual(out['health']['last_close'], 60_100.0)
        # long position, price moved +100 on 0.01 qty → +1 USDT unrealized
        self.assertAlmostEqual(out['unrealized_pnl_usdt'], 1.0)

    def test_missing_files_graceful(self) -> None:
        out = ait.get_status()
        json.dumps(out)
        self.assertIsNone(out['bot_state'])
        self.assertFalse(out['paused'])


class TestGetStrategyConfig(unittest.TestCase):

    def test_contains_decision_relevant_params(self) -> None:
        out = ait.get_strategy_config()
        json.dumps(out)
        for key in ('rsi_threshold', 'stop_loss_pct_long', 'take_profit_pct_long',
                    'circuit_breaker_pct', 'use_adx_filter', 'use_trend_filter',
                    'use_trailing_stop', 'use_session_filter', 'blocked_sessions'):
            self.assertIn(key, out)
        self.assertIn('aggressive_mode', out)

    def test_returns_copy_not_live_dict(self) -> None:
        import config
        out = ait.get_strategy_config()
        out['rsi_threshold'] = 'tampered'
        self.assertNotEqual(config.BOT_CONFIG['rsi_threshold'], 'tampered')


class TestGetLogTail(_FixtureBase):

    def test_default_40_lines_and_cap_200(self) -> None:
        self.bot_log.write_text('\n'.join(f'line {i}' for i in range(300)) + '\n')
        out = ait.get_log_tail()
        self.assertEqual(out['lines_returned'], 40)
        self.assertEqual(out['log'][-1], 'line 299')
        out = ait.get_log_tail(lines=999)
        self.assertEqual(out['lines_returned'], 200)
        json.dumps(out)

    def test_grep_filter_case_insensitive(self) -> None:
        self.bot_log.write_text('INFO ok\nERROR boom\ninfo fine\nerror again\n')
        out = ait.get_log_tail(grep='ERROR')
        self.assertEqual(out['lines_returned'], 2)
        self.assertTrue(all('error' in ln.lower() for ln in out['log']))

    def test_missing_log_is_error_dict(self) -> None:
        out = ait.get_log_tail()
        self.assertIn('error', out)


class TestGetBacktestVerdict(_FixtureBase):

    def _write_summary(self) -> None:
        self.summary_file.write_text(json.dumps({
            'gate_thresholds': {'min_dsr': 0.95, 'min_trades': 100},
            'results': [
                {'family': 'rsi_mr_5min', 'symbol': 'BTC/USDT',
                 'label': 'rsi_mr_5min__btc',
                 'aggregate': {'net_pnl_pct': -90.7, 'win_rate_pct': 33.6,
                               'sharpe_annual': -8.1, 'dsr_pvalue': 0.0,
                               'num_trades': 1115, 'noise': list(range(500))},
                 'gate': {'passed': False, 'reasons': ['r1', 'r2', 'r3', 'r4']}},
                {'family': 'donchian', 'symbol': 'ETH/USDT',
                 'label': 'donchian__eth',
                 'aggregate': {'net_pnl_pct': 4.2, 'win_rate_pct': 51.0,
                               'sharpe_annual': 1.2, 'dsr_pvalue': 0.97},
                 'gate': {'passed': True, 'reasons': []}},
            ],
        }))

    def test_digest_is_compact_with_gate_thresholds(self) -> None:
        self._write_summary()
        out = ait.get_backtest_verdict()
        json.dumps(out)
        self.assertIn('gate_thresholds', out)
        self.assertEqual(out['configs_total'], 2)
        self.assertEqual(out['configs_passing_gate'], 1)
        fams = out['families']
        self.assertEqual(fams[0]['family'], 'rsi_mr_5min')
        self.assertEqual(fams[0]['symbol'], 'BTC/USDT')
        self.assertFalse(fams[0]['passed_gate'])
        self.assertTrue(fams[1]['passed_gate'])
        # digest, not the raw file: noise array must not leak through
        self.assertLess(len(json.dumps(out)), 4000)

    def test_missing_summary_is_error_dict(self) -> None:
        out = ait.get_backtest_verdict()
        self.assertIn('error', out)


class TestRegistryAndDispatch(_FixtureBase):

    SPEC_TOOLS = {'get_trades', 'get_daily_pnl', 'get_status',
                  'get_strategy_config', 'get_backtest_verdict', 'get_log_tail'}

    def test_registry_matches_spec(self) -> None:
        self.assertEqual(set(ait.TOOLS), self.SPEC_TOOLS)

    def test_declarations_match_registry(self) -> None:
        names = {d.name for d in ait.build_tool_declarations()}
        self.assertEqual(names, set(ait.TOOLS))

    def test_execute_unknown_tool(self) -> None:
        out = ait.execute_tool('place_order', {})
        self.assertIn('error', out)

    def test_execute_never_raises(self) -> None:
        out = ait.execute_tool('get_trades', {'days': 'not-an-int'})
        self.assertIn('error', out)

    def test_module_never_imports_exchange_client(self) -> None:
        import inspect
        src = inspect.getsource(ait)
        self.assertNotIn('exchange', src.replace('exchange client', ''))


if __name__ == '__main__':
    unittest.main()
