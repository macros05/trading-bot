"""
Tests for backtest/engine.py.

Run from project root:
    python -m unittest tests.test_backtest_engine
"""
import json
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd

from backtest.engine import (
    _close_position,
    _compute_metrics,
    _max_drawdown,
    _process_bar,
    _sharpe,
    _simulate,
    _to_dataframe,
    _trade_metrics,
    _ts_to_iso,
    _BALANCE,
    _MIN_CANDLES,
    _RSI_THRESHOLD,
    _SL_PCT,
    _TP_PCT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(
    n: int,
    close: float = 100.0,
    trend: float = 0.0,
) -> pd.DataFrame:
    """Synthesise a DataFrame of *n* candles with optional linear trend.

    Adds a ±0.2 oscillation so close prices are never perfectly flat —
    a flat series yields RSI = 0/0 = NaN, skipping every bar.
    """
    rows = []
    price = close
    for i in range(n):
        price += trend + (0.2 if i % 2 == 0 else -0.2)
        rows.append([i * 60_000, price - 0.1, price + 0.1, price - 0.2, price, 10.0])
    return _to_dataframe(rows)


def _position(entry: float = 100.0, qty: float = 1.0, ts: int = 0) -> dict:
    return {'entry_price': entry, 'qty': qty, 'entry_ts': ts}


# ---------------------------------------------------------------------------
# _to_dataframe
# ---------------------------------------------------------------------------

class TestToDataframe(unittest.TestCase):

    def test_columns_present(self):
        df = _to_dataframe([[0, 1.0, 2.0, 0.5, 1.5, 10.0]])
        self.assertEqual(list(df.columns), ['ts', 'open', 'high', 'low', 'close', 'volume'])

    def test_deduplicates_on_ts(self):
        rows = [[1000, 1, 2, 0.5, 1.5, 10], [1000, 2, 3, 1, 2, 5]]
        df = _to_dataframe(rows)
        self.assertEqual(len(df), 1)

    def test_sorted_by_ts(self):
        rows = [[2000, 1, 2, 0.5, 1.5, 10], [1000, 1, 2, 0.5, 1.5, 10]]
        df = _to_dataframe(rows)
        self.assertEqual(df['ts'].iloc[0], 1000)


# ---------------------------------------------------------------------------
# _close_position
# ---------------------------------------------------------------------------

class TestClosePosition(unittest.TestCase):

    def test_win_trade_positive_pnl(self):
        pos = _position(entry=100.0, qty=1.0)
        trade, new_balance = _close_position(pos, 103.0, 60_000, 'take_profit', 10_000.0)
        self.assertEqual(trade['result'], 'WIN')
        self.assertAlmostEqual(trade['pnl_usdt'], 3.0, places=3)
        self.assertAlmostEqual(new_balance, 10_003.0, places=3)

    def test_loss_trade_negative_pnl(self):
        pos = _position(entry=100.0, qty=1.0)
        trade, new_balance = _close_position(pos, 98.0, 60_000, 'stop_loss', 10_000.0)
        self.assertEqual(trade['result'], 'LOSS')
        self.assertAlmostEqual(trade['pnl_usdt'], -2.0, places=3)
        self.assertAlmostEqual(new_balance, 9_998.0, places=3)

    def test_trade_fields_populated(self):
        pos = _position(entry=100.0, qty=2.0, ts=1000)
        trade, _ = _close_position(pos, 103.0, 2000, 'take_profit', 10_000.0)
        self.assertEqual(trade['entry_price'], 100.0)
        self.assertEqual(trade['exit_price'],  103.0)
        self.assertEqual(trade['qty'],         2.0)
        self.assertEqual(trade['reason'],      'take_profit')
        self.assertEqual(trade['entry_ts'],    1000)
        self.assertEqual(trade['exit_ts'],     2000)


# ---------------------------------------------------------------------------
# _process_bar — entry
# ---------------------------------------------------------------------------

class TestProcessBarEntry(unittest.TestCase):

    def test_opens_position_when_signal_fires(self):
        with patch('backtest.engine.should_enter', return_value=True):
            new_pos, balance, trade = _process_bar(
                100.0, 95.0, 30.0, 1000, None, 10_000.0,
            )
        self.assertIsNotNone(new_pos)
        self.assertAlmostEqual(new_pos['entry_price'], 100.0)
        self.assertIsNone(trade)

    def test_no_position_when_signal_does_not_fire(self):
        with patch('backtest.engine.should_enter', return_value=False):
            new_pos, balance, trade = _process_bar(
                100.0, 95.0, 55.0, 1000, None, 10_000.0,
            )
        self.assertIsNone(new_pos)
        self.assertIsNone(trade)

    def test_qty_is_risk_fraction_of_balance_divided_by_close(self):
        # With _BALANCE=10000 and _RISK_PCT=0.01, risk_usdt=100 → qty=100/100=1.0
        with patch('backtest.engine.should_enter', return_value=True):
            new_pos, _, _ = _process_bar(100.0, 95.0, 30.0, 0, None, _BALANCE)
        expected_qty = (_BALANCE * 0.01) / 100.0
        self.assertAlmostEqual(new_pos['qty'], expected_qty, places=6)


# ---------------------------------------------------------------------------
# _process_bar — exit
# ---------------------------------------------------------------------------

class TestProcessBarExit(unittest.TestCase):

    def test_closes_on_take_profit(self):
        pos = _position(entry=100.0, qty=1.0)
        with patch('backtest.engine.check_exit', return_value='take_profit'):
            new_pos, _, trade = _process_bar(103.0, 100.0, 50.0, 1000, pos, 10_000.0)
        self.assertIsNone(new_pos)
        self.assertIsNotNone(trade)
        self.assertEqual(trade['reason'], 'take_profit')

    def test_closes_on_stop_loss(self):
        pos = _position(entry=100.0, qty=1.0)
        with patch('backtest.engine.check_exit', return_value='stop_loss'):
            new_pos, _, trade = _process_bar(98.0, 100.0, 50.0, 1000, pos, 10_000.0)
        self.assertIsNone(new_pos)
        self.assertIsNotNone(trade)
        self.assertEqual(trade['reason'], 'stop_loss')

    def test_holds_position_when_no_exit(self):
        pos = _position(entry=100.0, qty=1.0)
        with patch('backtest.engine.check_exit', return_value=None):
            new_pos, _, trade = _process_bar(101.0, 100.0, 50.0, 1000, pos, 10_000.0)
        self.assertIs(new_pos, pos)
        self.assertIsNone(trade)


# ---------------------------------------------------------------------------
# _simulate — integration with synthetic data
# ---------------------------------------------------------------------------

class TestSimulate(unittest.TestCase):

    def test_no_trades_when_no_signal(self):
        df = _make_df(100)
        with patch('backtest.engine.should_enter', return_value=False):
            trades, equity = _simulate(df)
        self.assertEqual(len(trades), 0)
        self.assertEqual(equity, [_BALANCE])

    def test_trade_is_recorded_and_equity_updated(self):
        df = _make_df(100)
        # open on first entry call, close on first exit call
        enter_seq = iter([True]  + [False] * 200)
        exit_seq  = iter(['take_profit'] + [None] * 200)

        with patch('backtest.engine.should_enter', side_effect=lambda *_: next(enter_seq)), \
             patch('backtest.engine.check_exit',   side_effect=lambda *_: next(exit_seq)):
            trades, equity = _simulate(df)

        self.assertEqual(len(trades), 1)
        self.assertEqual(len(equity), 2)  # initial + after close

    def test_balance_updates_after_trade(self):
        df = _make_df(100, close=100.0)
        enter_seq = iter([True]  + [False] * 200)
        exit_seq  = iter(['take_profit'] + [None] * 200)

        with patch('backtest.engine.should_enter', side_effect=lambda *_: next(enter_seq)), \
             patch('backtest.engine.check_exit',   side_effect=lambda *_: next(exit_seq)), \
             patch('backtest.engine.calc_pnl', return_value=(50.0, 5.0)):
            trades, equity = _simulate(df)

        self.assertAlmostEqual(equity[-1], _BALANCE + 50.0, places=4)


# ---------------------------------------------------------------------------
# _max_drawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown(unittest.TestCase):

    def test_no_drawdown_on_flat_equity(self):
        self.assertAlmostEqual(_max_drawdown([100.0, 100.0, 100.0]), 0.0)

    def test_no_drawdown_on_rising_equity(self):
        self.assertAlmostEqual(_max_drawdown([100.0, 110.0, 120.0]), 0.0)

    def test_calculates_correct_drawdown(self):
        # Peak=110, trough=88 → dd=(110-88)/110=0.2
        dd = _max_drawdown([100.0, 110.0, 88.0, 95.0])
        self.assertAlmostEqual(dd, (110 - 88) / 110, places=6)

    def test_largest_drawdown_is_returned(self):
        # Two drops: 100→90 (10%) and 110→95 (13.6%)
        dd = _max_drawdown([100.0, 90.0, 110.0, 95.0])
        expected = (110 - 95) / 110
        self.assertAlmostEqual(dd, expected, places=6)


# ---------------------------------------------------------------------------
# _sharpe
# ---------------------------------------------------------------------------

class TestSharpe(unittest.TestCase):

    def test_returns_zero_for_single_trade(self):
        self.assertEqual(_sharpe([0.03]), 0.0)

    def test_returns_zero_for_identical_returns(self):
        # std dev = 0 → Sharpe = 0
        self.assertEqual(_sharpe([0.03, 0.03, 0.03]), 0.0)

    def test_positive_sharpe_for_consistent_wins(self):
        returns = [0.02, 0.03, 0.025, 0.031, 0.028]
        sharpe  = _sharpe(returns)
        self.assertGreater(sharpe, 0)

    def test_negative_sharpe_for_consistent_losses(self):
        returns = [-0.02, -0.03, -0.025]
        sharpe  = _sharpe(returns)
        self.assertLess(sharpe, 0)


# ---------------------------------------------------------------------------
# _trade_metrics
# ---------------------------------------------------------------------------

class TestTradeMetrics(unittest.TestCase):

    def _make_trades(self) -> list[dict]:
        return [
            {'pnl_usdt': 30.0,  'pnl_pct': 3.0,  'result': 'WIN'},
            {'pnl_usdt': -20.0, 'pnl_pct': -2.0, 'result': 'LOSS'},
            {'pnl_usdt': 15.0,  'pnl_pct': 1.5,  'result': 'WIN'},
        ]

    def test_win_rate(self):
        m = _trade_metrics(self._make_trades(), [_BALANCE, _BALANCE + 10, _BALANCE + 5])
        self.assertAlmostEqual(m['win_rate_pct'], 200 / 3, places=1)

    def test_total_pnl(self):
        m = _trade_metrics(self._make_trades(), [_BALANCE])
        self.assertAlmostEqual(m['total_pnl_usdt'], 25.0, places=4)

    def test_best_and_worst(self):
        m = _trade_metrics(self._make_trades(), [_BALANCE])
        self.assertAlmostEqual(m['best_trade_usdt'],  30.0, places=4)
        self.assertAlmostEqual(m['worst_trade_usdt'], -20.0, places=4)


# ---------------------------------------------------------------------------
# _compute_metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics(unittest.TestCase):

    def _make_df(self) -> pd.DataFrame:
        rows = [[i * 60_000, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(50)]
        return _to_dataframe(rows)

    def test_no_trades_returns_note(self):
        df     = self._make_df()
        report = _compute_metrics([], [_BALANCE], df)
        self.assertIn('note', report)
        self.assertEqual(report['num_trades'], 0)

    def test_period_present(self):
        df     = self._make_df()
        report = _compute_metrics([], [_BALANCE], df)
        self.assertIn('from', report['period'])
        self.assertIn('to',   report['period'])

    def test_trades_included_when_present(self):
        df = self._make_df()
        trades = [{'pnl_usdt': 10.0, 'pnl_pct': 1.0, 'result': 'WIN',
                   'entry_price': 100.0, 'exit_price': 101.0, 'qty': 1.0,
                   'reason': 'take_profit', 'entry_ts': 0, 'exit_ts': 60_000}]
        equity = [_BALANCE, _BALANCE + 10.0]
        report = _compute_metrics(trades, equity, df)
        self.assertIn('trades', report)
        self.assertEqual(len(report['trades']), 1)


if __name__ == '__main__':
    unittest.main()
