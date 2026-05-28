"""Tests for champion-certificate construction and the promote/override/refuse
decision. Pure logic; the CLI (scripts/promote_champion.py) is a thin wrapper.
"""
import unittest

from backtest.promotion_gate import GateThresholds, evaluate_config
from backtest.promotion_cert import (
    build_certificate,
    decide,
    find_result,
    translate_params,
    SCHEMA_VERSION,
)

THR = GateThresholds(min_dsr=0.95, min_trades=100)


def _sweep(label='cand', **overrides):
    result = {
        'label': label,
        'dsr_pvalue': 0.97,
        'num_trades': 150,
        'wr_lower_95': 55.0,
        'breakeven_wr_long': 43.08,
        'breakeven_wr_short': 40.0,
        'sharpe_annual': 1.4,
        'net_pnl_pct': 22.0,
        'win_rate_pct': 60.0,
        'max_drawdown_pct': 8.0,
        'params': {'rsi_threshold': 40.0, 'adx_threshold': 45.0},
    }
    result.update(overrides)
    return {
        'symbol': 'BTC/USDT',
        'period_years': 2.0,
        'results': [result],
    }


class TestFindResult(unittest.TestCase):
    def test_finds_matching_label(self):
        sweep = _sweep(label='target')
        self.assertEqual(find_result(sweep, 'target')['label'], 'target')

    def test_returns_none_when_absent(self):
        self.assertIsNone(find_result(_sweep(label='a'), 'missing'))


class TestDecide(unittest.TestCase):
    def test_promote_when_passing(self):
        v = evaluate_config(_sweep()['results'][0], THR)
        self.assertEqual(decide(v, override_reason=None), 'promote')

    def test_refuse_when_failing_without_override(self):
        v = evaluate_config(_sweep(dsr_pvalue=0.0)['results'][0], THR)
        self.assertEqual(decide(v, override_reason=None), 'refuse')

    def test_override_when_failing_with_reason(self):
        v = evaluate_config(_sweep(dsr_pvalue=0.0)['results'][0], THR)
        self.assertEqual(decide(v, override_reason='deliberate paper test'),
                         'override')


class TestTranslateParams(unittest.TestCase):
    """Sweep params use different key names than BOT_CONFIG for the most
    important strategy params. The certificate must store a config-named copy
    so the runtime guard can compare apples to apples."""

    def test_maps_sweep_names_to_config_names(self):
        sweep_params = {
            'rsi_long_threshold': 40.0,
            'sl_pct_long': 0.025,
            'tp_pct_long': 0.04,
            'use_adx_filter': True,
            'unmapped_internal_key': 999,
        }
        cfg = translate_params(sweep_params)
        self.assertEqual(cfg['rsi_threshold'], 40.0)
        self.assertEqual(cfg['stop_loss_pct_long'], 0.025)
        self.assertEqual(cfg['take_profit_pct_long'], 0.04)
        self.assertEqual(cfg['use_adx_filter'], True)
        # unmapped keys are dropped (not part of the material comparison)
        self.assertNotIn('unmapped_internal_key', cfg)

    def test_certificate_includes_config_params(self):
        sweep = _sweep()
        sweep['results'][0]['params'] = {
            'rsi_long_threshold': 40.0, 'use_adx_filter': True,
        }
        v = evaluate_config(sweep['results'][0], THR)
        cert = build_certificate(
            sweep_path='x.json', sweep_data=sweep, sweep_sha256='s',
            label='cand', verdict=v, thresholds=THR, git_commit='c',
            now_ms=1, override_reason=None, operator=None,
        )
        self.assertEqual(cert['config_params']['rsi_threshold'], 40.0)
        self.assertEqual(cert['config_params']['use_adx_filter'], True)


class TestBuildCertificate(unittest.TestCase):
    def test_passing_certificate_has_no_override_block(self):
        sweep = _sweep()
        v = evaluate_config(sweep['results'][0], THR)
        cert = build_certificate(
            sweep_path='backtest/results/x.json', sweep_data=sweep,
            sweep_sha256='abc123', label='cand', verdict=v,
            thresholds=THR, git_commit='deadbeef', now_ms=1_700_000_000_000,
            override_reason=None, operator=None,
        )
        self.assertEqual(cert['schema_version'], SCHEMA_VERSION)
        self.assertEqual(cert['label'], 'cand')
        self.assertEqual(cert['symbol'], 'BTC/USDT')
        self.assertEqual(cert['source_sweep_sha256'], 'abc123')
        self.assertEqual(cert['git_commit'], 'deadbeef')
        self.assertTrue(cert['gate']['passed'])
        self.assertNotIn('override', cert)
        self.assertEqual(cert['params']['rsi_threshold'], 40.0)

    def test_certificate_carries_expected_metrics_for_validation(self):
        sweep = _sweep()
        v = evaluate_config(sweep['results'][0], THR)
        cert = build_certificate(
            sweep_path='x.json', sweep_data=sweep, sweep_sha256='s',
            label='cand', verdict=v, thresholds=THR, git_commit='c',
            now_ms=1, override_reason=None, operator=None,
        )
        em = cert['expected_metrics']
        self.assertEqual(em['win_rate_pct'], 60.0)
        self.assertEqual(em['net_pnl_pct'], 22.0)
        self.assertEqual(em['num_trades'], 150)
        self.assertEqual(em['max_drawdown_pct'], 8.0)
        # 150 trades over 2.0 years -> 75/yr
        self.assertAlmostEqual(em['trades_per_year'], 75.0)

    def test_certificate_records_validation_caveats(self):
        sweep = _sweep()
        v = evaluate_config(sweep['results'][0], THR)
        cert = build_certificate(
            sweep_path='x.json', sweep_data=sweep, sweep_sha256='s',
            label='cand', verdict=v, thresholds=THR, git_commit='c',
            now_ms=1, override_reason=None, operator=None,
        )
        # Every certificate must state what the backtest did NOT model, so a
        # promotion can never silently hide a live/backtest discrepancy.
        self.assertIn('validation_caveats', cert)
        self.assertTrue(any('macro' in c.lower()
                            for c in cert['validation_caveats']))

    def test_override_certificate_records_reason_and_operator(self):
        sweep = _sweep(dsr_pvalue=0.0)
        v = evaluate_config(sweep['results'][0], THR)
        cert = build_certificate(
            sweep_path='x.json', sweep_data=sweep, sweep_sha256='s',
            label='cand', verdict=v, thresholds=THR, git_commit='c',
            now_ms=42, override_reason='deliberate paper test',
            operator='marcos',
        )
        self.assertFalse(cert['gate']['passed'])
        self.assertIn('override', cert)
        self.assertTrue(cert['override']['is_override'])
        self.assertEqual(cert['override']['reason'], 'deliberate paper test')
        self.assertEqual(cert['override']['operator'], 'marcos')
        self.assertEqual(cert['override']['overridden_at_ms'], 42)
        # the failing reasons must be preserved for the audit trail
        self.assertTrue(cert['gate']['reasons'])

    def test_build_certificate_refuses_failing_without_override(self):
        sweep = _sweep(dsr_pvalue=0.0)
        v = evaluate_config(sweep['results'][0], THR)
        with self.assertRaises(ValueError):
            build_certificate(
                sweep_path='x.json', sweep_data=sweep, sweep_sha256='s',
                label='cand', verdict=v, thresholds=THR, git_commit='c',
                now_ms=1, override_reason=None, operator=None,
            )


if __name__ == '__main__':
    unittest.main()
