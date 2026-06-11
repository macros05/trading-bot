"""Tests for the in-fold-optimizing walk-forward engine (Spec 2).

The pre-Spec-2 walk-forward harnesses discarded the train slice entirely
(``for _train, test in ...`` / slicing only the test window), so "optimization"
never optimized. These tests prove, on small synthetic data, that:

  (a) the TRAIN slice actually drives candidate selection;
  (b) OOS metrics come only from TEST slices (no train contamination);
  (c) the min-train-trades floor falls back to the family default;
  (d) the aggregate dict is promotion-gate shape-compatible;
  (e) warmup bars extend slices backward only and never leak trades or
      future data.

Plus a regression test: ``walk_forward_v7`` in single-candidate (fixed-params)
mode must produce exactly the trades the pre-refactor loop produced.
"""
from __future__ import annotations

import math
import unittest
from dataclasses import dataclass, replace

import pandas as pd

from backtest.promotion_gate import GateThresholds, evaluate_config
from backtest.wfa import WfaConfig, run_wfa

_HOUR_MS = 3_600_000
_MONTH_MS = 30 * 24 * _HOUR_MS
_START_MS = 1_700_000_000_000
_BARS_PER_MONTH = 30 * 24


def _make_df(n_months: int, regime_flip_month: float | None = None) -> pd.DataFrame:
    """Small hourly synthetic frame; 'regime' flips 0 -> 1 at the given month."""
    n = n_months * _BARS_PER_MONTH
    flip_ms = (_START_MS + int(regime_flip_month * _MONTH_MS)
               if regime_flip_month is not None else None)
    rows = []
    for i in range(n):
        ts = _START_MS + i * _HOUR_MS
        close = 100.0 + 4.0 * math.sin(2 * math.pi * i / 48) + 0.3 * math.sin(i * 0.7)
        regime = 0 if flip_ms is None or ts < flip_ms else 1
        rows.append((ts, close, close * 1.002, close * 0.998, close, 1.0, regime))
    return pd.DataFrame(
        rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'regime'],
    )


@dataclass(frozen=True)
class _FakeParams:
    label: str
    prefers_regime: int = 0
    balance: float = 10_000.0
    sl_pct: float = 0.02
    tp_pct: float = 0.03


def _fake_simulate(df_slice: pd.DataFrame, params: _FakeParams) -> dict:
    """One trade per 24 bars; profitable only in the candidate's regime."""
    trades = []
    balance = params.balance
    equity = [balance]
    for i in range(0, len(df_slice), 24):
        row = df_slice.iloc[i]
        win = int(row['regime']) == params.prefers_regime
        jitter = 0.1 * ((i // 24) % 3 - 1)  # avoid zero-variance Sharpe
        pnl_pct = (1.0 if win else -1.0) + jitter
        pnl_usdt = balance * pnl_pct / 100
        balance += pnl_usdt
        equity.append(balance)
        trades.append({
            'side': 'long' if (i // 24) % 2 == 0 else 'short',
            'pnl_usdt': round(pnl_usdt, 4),
            'pnl_pct': round(pnl_pct, 4),
            'result': 'WIN' if pnl_usdt >= 0 else 'LOSS',
            'entry_ts': int(row['ts']),
            'exit_ts': int(row['ts']) + _HOUR_MS,
        })
    return {'trades': trades, 'equity': equity,
            'final_balance': balance, 'label': params.label}


class TestTrainDrivenSelection(unittest.TestCase):
    """(a) The train slice must actually decide which candidate runs OOS."""

    def test_chosen_candidate_switches_when_train_regime_flips(self):
        df = _make_df(8, regime_flip_month=4.0)
        candidates = [_FakeParams('A', prefers_regime=0),
                      _FakeParams('B', prefers_regime=1)]
        cfg = WfaConfig(min_train_trades=15, min_test_candles=10)
        out = run_wfa(df, candidates, _fake_simulate, cfg)
        labels = [f['chosen_label'] for f in out['folds']]
        self.assertGreaterEqual(len(labels), 3)
        # train [0,3) is pure regime 0 -> A; train [3,6) is 2/3 regime 1 -> B
        self.assertEqual(labels[0], 'A')
        self.assertEqual(labels[-1], 'B')
        self.assertIn('A', labels)
        self.assertIn('B', labels)
        self.assertTrue(all(f['selection_applied'] for f in out['folds']))

    def test_winner_train_metrics_recorded(self):
        df = _make_df(5, regime_flip_month=None)
        candidates = [_FakeParams('A', prefers_regime=0),
                      _FakeParams('B', prefers_regime=1)]
        cfg = WfaConfig(min_train_trades=15, min_test_candles=10)
        out = run_wfa(df, candidates, _fake_simulate, cfg)
        for fold in out['folds']:
            self.assertIsNotNone(fold['train'])
            self.assertGreaterEqual(fold['train']['num_trades'], 15)
            self.assertIn('sharpe_trade', fold['train'])
            self.assertIn('net_pnl_usdt', fold['train'])


class TestNoTrainContamination(unittest.TestCase):
    """(b) OOS trades must come exclusively from test windows."""

    def test_all_oos_trades_inside_some_test_window(self):
        df = _make_df(7, regime_flip_month=3.5)
        candidates = [_FakeParams('A', prefers_regime=0),
                      _FakeParams('B', prefers_regime=1)]
        cfg = WfaConfig(min_train_trades=5, min_test_candles=10)
        out = run_wfa(df, candidates, _fake_simulate, cfg)
        self.assertGreater(len(out['trades']), 0)
        windows = [(f['test_from_ms'], f['test_to_ms']) for f in out['folds']]
        first_test_start = _START_MS + 3 * _MONTH_MS
        for trade in out['trades']:
            self.assertGreaterEqual(trade['entry_ts'], first_test_start)
            self.assertTrue(any(lo <= trade['entry_ts'] < hi for lo, hi in windows))

    def test_simulate_fn_never_sees_data_past_fold_end(self):
        df = _make_df(6, regime_flip_month=None)
        seen: list[tuple[int, int]] = []

        def recording_simulate(df_slice: pd.DataFrame, params: _FakeParams) -> dict:
            seen.append((int(df_slice['ts'].iloc[0]), int(df_slice['ts'].iloc[-1])))
            return _fake_simulate(df_slice, params)

        candidates = [_FakeParams('A', prefers_regime=0),
                      _FakeParams('B', prefers_regime=1)]
        cfg = WfaConfig(min_train_trades=5, min_test_candles=10)
        out = run_wfa(df, candidates, recording_simulate, cfg)
        bounds = []
        for fold in out['folds']:
            bounds.append((fold['train_from_ms'], fold['train_to_ms']))
            bounds.append((fold['test_from_ms'], fold['test_to_ms']))
        self.assertGreater(len(seen), 0)
        for first_ts, last_ts in seen:
            self.assertTrue(
                any(first_ts >= lo and last_ts < hi for lo, hi in bounds),
                f'slice [{first_ts}, {last_ts}] escapes every fold window',
            )


class TestFallbackOnFloor(unittest.TestCase):
    """(c) No candidate clears min_train_trades -> family default, no bias."""

    def test_falls_back_to_first_candidate(self):
        df = _make_df(6, regime_flip_month=2.0)
        # ~90 train trades per fold << floor 1000 -> nobody clears.
        candidates = [_FakeParams('default_first', prefers_regime=0),
                      _FakeParams('better_but_unproven', prefers_regime=1)]
        cfg = WfaConfig(min_train_trades=1000, min_test_candles=10)
        out = run_wfa(df, candidates, _fake_simulate, cfg)
        self.assertGreater(out['num_folds'], 0)
        for fold in out['folds']:
            self.assertEqual(fold['chosen_label'], 'default_first')
            self.assertFalse(fold['selection_applied'])
        # candidates were still evaluated on train -> counted honestly
        self.assertEqual(out['n_evaluations'], 2 * out['num_folds'])


class TestGateCompatibleAggregate(unittest.TestCase):
    """(d) The aggregate dict must have every promotion-gate-required key."""

    _REQUIRED_KEYS = (
        'dsr_pvalue', 'num_trades', 'wr_lower_95', 'breakeven_wr_long',
        'breakeven_wr_short', 'net_pnl_pct', 'sharpe_annual', 'sharpe_trade',
        'profit_factor', 'max_drawdown_pct', 'num_folds', 'folds_with_trades',
        'label', 'by_side', 'win_rate_pct',
    )

    def test_aggregate_has_all_gate_keys_and_gate_runs(self):
        df = _make_df(6, regime_flip_month=None)
        out = run_wfa(df, [_FakeParams('solo', prefers_regime=0)],
                      _fake_simulate, WfaConfig(min_test_candles=10))
        aggregate = out['aggregate']
        for key in self._REQUIRED_KEYS:
            self.assertIn(key, aggregate, f'missing gate key: {key}')
        for side in ('long', 'short'):
            self.assertIn('trades', aggregate['by_side'][side])
            self.assertIn('wr_lower_95', aggregate['by_side'][side])
        verdict = evaluate_config(aggregate, GateThresholds())
        # The gate may fail on MERIT, never on SHAPE (missing/non-finite).
        shape_failures = [r for r in verdict.reasons
                          if 'missing' in r or 'non-finite' in r]
        self.assertEqual(shape_failures, [], verdict.reasons)

    def test_balance_compounds_across_folds(self):
        df = _make_df(6, regime_flip_month=None)
        out = run_wfa(df, [_FakeParams('solo', prefers_regime=0)],
                      _fake_simulate, WfaConfig(min_test_candles=10))
        expected = out['initial_balance'] + sum(t['pnl_usdt'] for t in out['trades'])
        self.assertAlmostEqual(out['final_balance'], expected, places=2)
        self.assertGreater(out['final_balance'], out['initial_balance'])
        self.assertEqual(len(out['equity']), len(out['trades']) + 1)

    def test_default_annualization_uses_oos_span_not_df_span(self):
        # 6 months of data, train 3 / test 1 / step 1 -> 3 test windows
        # spanning exactly 3 months. Annualizing over the 6-month df span
        # would understate trades/year by 2x (sharpe_annual by sqrt(2)).
        df = _make_df(6, regime_flip_month=None)
        out = run_wfa(df, [_FakeParams('solo', prefers_regime=0)],
                      _fake_simulate, WfaConfig(min_test_candles=10))
        aggregate = out['aggregate']
        folds = out['folds']
        oos_ms = (max(f['test_to_ms'] for f in folds)
                  - min(f['test_from_ms'] for f in folds))
        oos_years = oos_ms / (365.25 * 86_400 * 1_000)
        n = aggregate['num_trades']
        expected = aggregate['sharpe_trade'] * math.sqrt(n / oos_years)
        # sharpe_trade is rounded to 4dp in the aggregate -> compare relatively
        self.assertAlmostEqual(aggregate['sharpe_annual'], expected,
                               delta=abs(expected) * 1e-3)
        df_span_years = (int(df['ts'].iloc[-1]) - int(df['ts'].iloc[0])) / (
            365.25 * 86_400 * 1_000)
        wrong = aggregate['sharpe_trade'] * math.sqrt(n / df_span_years)
        self.assertGreater(abs(aggregate['sharpe_annual'] - wrong),
                           abs(expected) * 0.05)


class TestWarmupNoLeak(unittest.TestCase):
    """(e) warmup_bars extends slices backward only; warmup trades dropped."""

    def test_warmup_extends_backward_and_trades_are_filtered(self):
        df = _make_df(6, regime_flip_month=None)
        warmup_bars = 48
        seen: list[tuple[int, int]] = []

        def warmup_simulate(df_slice: pd.DataFrame, params: _FakeParams) -> dict:
            seen.append((int(df_slice['ts'].iloc[0]), int(df_slice['ts'].iloc[-1])))
            return _fake_simulate(df_slice, params)

        cfg = WfaConfig(min_test_candles=10, warmup_bars=warmup_bars)
        out = run_wfa(df, [_FakeParams('solo', prefers_regime=0)],
                      warmup_simulate, cfg)
        self.assertGreater(len(out['trades']), 0)
        for fold in out['folds']:
            # warmup trades (entry before the test window) must be excluded
            fold_trades = [t for t in out['trades']
                           if fold['test_from_ms'] <= t['entry_ts'] < fold['test_to_ms']]
            self.assertEqual(fold['num_trades'], len(fold_trades))
        for trade in out['trades']:
            self.assertGreaterEqual(trade['entry_ts'], _START_MS + 3 * _MONTH_MS)
        # every slice starts exactly warmup_bars before its window and
        # never extends past the window end (backward-only extension)
        for first_ts, last_ts in seen:
            matched = False
            for fold in out['folds']:
                for lo, hi in ((fold['test_from_ms'], fold['test_to_ms']),):
                    if last_ts < hi and first_ts == lo - warmup_bars * _HOUR_MS:
                        matched = True
            self.assertTrue(matched,
                            f'slice [{first_ts}, {last_ts}] not warmup-aligned')

    def test_fake_sim_emits_trades_in_warmup_region(self):
        """Sanity: the fake sim DOES trade on warmup bars, so the engine
        filtering (not the sim) is what keeps them out of the OOS series."""
        df = _make_df(6, regime_flip_month=None)
        window_start = _START_MS + 3 * _MONTH_MS
        warmup_slice = df[(df['ts'] >= window_start - 48 * _HOUR_MS)
                          & (df['ts'] < window_start + _MONTH_MS)].reset_index(drop=True)
        result = _fake_simulate(warmup_slice, _FakeParams('solo'))
        self.assertTrue(any(t['entry_ts'] < window_start for t in result['trades']))


class TestEvaluationAccounting(unittest.TestCase):
    """n_evaluations: train-slice candidate simulations, for honest DSR."""

    def test_fixed_params_mode_performs_zero_train_evaluations(self):
        df = _make_df(6, regime_flip_month=None)
        out = run_wfa(df, [_FakeParams('solo', prefers_regime=0)],
                      _fake_simulate, WfaConfig(min_test_candles=10))
        self.assertEqual(out['n_evaluations'], 0)
        for fold in out['folds']:
            self.assertFalse(fold['selection_applied'])
            self.assertEqual(fold['chosen_label'], 'solo')

    def test_multi_candidate_counts_every_train_simulation(self):
        df = _make_df(6, regime_flip_month=None)
        candidates = [_FakeParams('A', 0), _FakeParams('B', 1),
                      _FakeParams('C', 0)]
        cfg = WfaConfig(min_train_trades=5, min_test_candles=10)
        out = run_wfa(df, candidates, _fake_simulate, cfg)
        self.assertEqual(out['n_evaluations'], 3 * out['num_folds'])


class TestWalkForwardV7Regression(unittest.TestCase):
    """Default (single-candidate) walk_forward_v7 must replay the old loop
    bit-for-bit: same trades, same compounded balance."""

    @classmethod
    def setUpClass(cls):
        from backtest.v7_full import baseline_v6_params
        cls.df = _make_df(6, regime_flip_month=None)
        cls.params = replace(baseline_v6_params(), label='regression',
                             use_adx_filter=False)

    def _reference_walk_forward(self) -> dict:
        """Verbatim port of the pre-Spec-2 walk_forward_v7 loop."""
        from backtest.v7_full import simulate_v7
        df, p = self.df, self.params
        train_ms, test_ms, step_ms = 3 * _MONTH_MS, _MONTH_MS, _MONTH_MS
        cursor = int(df['ts'].iloc[0])
        end = int(df['ts'].iloc[-1])
        balance = p.balance
        all_trades: list[dict] = []
        num_folds = 0
        while cursor + train_ms + test_ms <= end:
            lo, hi = cursor + train_ms, cursor + train_ms + test_ms
            test_slice = df[(df['ts'] >= lo) & (df['ts'] < hi)].reset_index(drop=True)
            if len(test_slice) >= 200:
                result = simulate_v7(test_slice, replace(p, balance=balance))
                num_folds += 1
                for trade in result['trades']:
                    balance += trade['pnl_usdt']
                    all_trades.append(trade)
            cursor += step_ms
        return {'trades': all_trades, 'final_balance': round(balance, 4),
                'num_folds': num_folds}

    def test_single_candidate_mode_matches_old_loop(self):
        from backtest.sweep_v7_full import walk_forward_v7
        reference = self._reference_walk_forward()
        self.assertGreater(len(reference['trades']), 0,
                           'synthetic data produced no trades — test is vacuous')
        new = walk_forward_v7(self.df, self.params)
        self.assertEqual(new['num_folds'], reference['num_folds'])
        self.assertEqual(new['trades'], reference['trades'])
        self.assertEqual(new['final_balance'], reference['final_balance'])
        self.assertEqual(new['initial_balance'], self.params.balance)
        self.assertEqual(new['n_evaluations'], 0)

    def test_in_fold_candidates_routes_through_engine(self):
        from backtest.sweep_v7_full import walk_forward_v7
        candidates = [self.params,
                      replace(self.params, label='wider_tp', tp_pct_long=0.03)]
        new = walk_forward_v7(self.df, self.params,
                              in_fold_candidates=candidates,
                              min_train_trades=1)
        self.assertGreater(new['num_folds'], 0)
        self.assertEqual(new['n_evaluations'],
                         len(candidates) * new['num_folds'])

    def test_aggregate_wrapper_is_gate_shape_compatible(self):
        from backtest.sweep_v7_full import aggregate, walk_forward_v7
        wf = walk_forward_v7(self.df, self.params)
        agg = aggregate(wf, self.params, period_years=0.5, n_trials_for_dsr=15)
        verdict = evaluate_config({'label': self.params.label, **agg},
                                  GateThresholds())
        shape_failures = [r for r in verdict.reasons
                          if 'missing' in r or 'non-finite' in r]
        self.assertEqual(shape_failures, [], verdict.reasons)


class TestAdvancedWalkForwardCli(unittest.TestCase):
    """walk_forward.run must keep its CLI-facing shape, gate keys included."""

    def test_run_produces_gate_keys_and_legacy_aliases(self):
        from backtest.advanced import AdvancedParams
        from backtest.walk_forward import WalkForwardConfig, run
        df = _make_df(6, regime_flip_month=None)
        params = AdvancedParams(label='wf_cli', use_adx_filter=False)
        result = run(df, params, WalkForwardConfig())
        self.assertIn('num_folds', result)
        self.assertIn('folds', result)
        aggregate = result['aggregate']
        for key in ('num_trades', 'net_pnl_usdt', 'net_pnl_pct',
                    'sharpe_ratio', 'dsr_pvalue', 'wr_lower_95',
                    'breakeven_wr_long', 'breakeven_wr_short', 'by_side',
                    'num_folds', 'folds_with_trades'):
            self.assertIn(key, aggregate, f'missing key: {key}')
        for fold in result['folds']:
            for key in ('label', 'num_trades', 'win_rate_pct', 'net_pnl_usdt',
                        'net_pnl_pct', 'sharpe_ratio', 'max_drawdown_pct',
                        'fees_paid_usdt', 'slippage_cost_usdt',
                        'test_from_ms', 'test_to_ms'):
                self.assertIn(key, fold, f'missing fold key: {key}')


if __name__ == '__main__':
    unittest.main()
