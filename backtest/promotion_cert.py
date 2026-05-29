"""Champion-certificate construction and the promote/override/refuse decision.

A *certificate* is the audit record that ties the live config to the exact
sweep result and gate verdict that justified promoting it. The runtime guard
(``core/champion_guard.py``) and live validation (``analytics/validation.py``)
both read it. Pure logic only — file I/O and argparse live in the CLI
(``scripts/promote_champion.py``).
"""
from __future__ import annotations

from backtest.promotion_gate import GateThresholds, GateVerdict

SCHEMA_VERSION = 1

# Things the sweep does NOT model. Recorded on every certificate so a promotion
# can never silently hide a live/backtest discrepancy. The MacroFilter is a
# live-only signal (funding + LLM sentiment, not reconstructable historically);
# it is currently dormant (main.py does not pass it to the loop), so live and
# backtest agree today — but if it is ever re-enabled this caveat flags the gap.
VALIDATION_CAVEATS = [
    'backtest does not model MacroFilter (live-only; currently dormant in main.py)',
]

# Sweep ``params`` use different key names than the live BOT_CONFIG for a few
# strategy-deciding params. This bridge maps EVERY material sweep key to its
# BOT_CONFIG equivalent so the runtime guard can compare like-for-like. The
# guard's drift detection is only as complete as this map: any material param
# left out is a blind spot through which a hand-edit to config.py could pass
# unnoticed. The companion EXCLUDED_SWEEP_KEYS set lists the params we
# deliberately do NOT track (pure backtest internals); a unit test asserts that
# every V7Params field is either mapped here or explicitly excluded, so adding a
# new strategy param forces a conscious map/exclude decision.
SWEEP_TO_CONFIG_KEYS = {
    'rsi_long_threshold':     'rsi_threshold',
    'rsi_short_threshold':    'rsi_short_threshold',
    'adx_threshold':          'adx_threshold',
    'adx_flat_threshold':     'adx_flat_threshold',
    'use_adx_filter':         'use_adx_filter',
    'use_mtf_filter':         'use_mtf_filter',
    'mtf_15m_period':         'mtf_15m_period',
    'use_session_filter':     'use_session_filter',
    'blocked_sessions':       'blocked_sessions',
    'use_short_trend_filter': 'use_short_trend_filter',
    'short_adx_min':          'short_adx_min',
    'short_sma_period':       'short_sma_period',
    'use_volatility_filter':  'use_volatility_filter',
    'volatility_lookback_hours': 'volatility_lookback_hours',
    'volatility_low_pct':     'volatility_low_pct',
    'volatility_high_pct':    'volatility_high_pct',
    'use_trailing_stop':      'use_trailing_stop',
    'trailing_breakeven_pct': 'trailing_breakeven_pct',
    'trailing_trail_pct':     'trailing_trail_pct',
    'trailing_distance_pct':  'trailing_distance_pct',
    'sl_pct_long':            'stop_loss_pct_long',
    'sl_pct_short':           'stop_loss_pct_short',
    'tp_pct_long':            'take_profit_pct_long',
    'tp_pct_short':           'take_profit_pct_short',
    'max_hold_hours':         'max_hold_hours',
    'stalled_hours':          'stalled_hours',
    'stalled_move_threshold': 'stalled_move_threshold',
    'range_lookback_min':     'range_lookback_min',
    'range_pct_threshold':    'range_pct_threshold',
    'risk_pct':               'risk_pct',
}

# V7Params fields intentionally NOT tracked by the guard: identity + backtest
# accounting/cost knobs that do not shape the live strategy decision.
EXCLUDED_SWEEP_KEYS = frozenset({
    'label', 'balance', 'apply_costs', 'taker_fee', 'slippage',
})


def _normalize(value):
    """Coerce sequences to lists so tuple-vs-list (JSON round-trip) does not
    register as drift. ``blocked_sessions`` is a tuple live but a list in the
    certificate after JSON load."""
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def translate_params(sweep_params: dict) -> dict:
    """Translate sweep-named material params into BOT_CONFIG key names.

    Unmapped keys are dropped — they are backtest internals, not live config.
    Sequence values are normalised to lists so the guard compares like-for-like.
    """
    return {
        config_key: _normalize(sweep_params[sweep_key])
        for sweep_key, config_key in SWEEP_TO_CONFIG_KEYS.items()
        if sweep_key in sweep_params
    }


def find_result(sweep_data: dict, label: str) -> dict | None:
    """Return the sweep result whose ``label`` matches, or ``None``."""
    for result in sweep_data.get('results', []):
        if result.get('label') == label:
            return result
    return None


def decide(verdict: GateVerdict, override_reason: str | None) -> str:
    """Map a gate verdict + optional override to an action.

    Returns ``'promote'`` (passed cleanly), ``'override'`` (failed but a written
    reason was supplied), or ``'refuse'`` (failed and no reason).
    """
    if verdict.passed:
        return 'promote'
    if override_reason:
        return 'override'
    return 'refuse'


def _expected_metrics(result: dict, period_years: float | None) -> dict:
    n_trades = result.get('num_trades')
    trades_per_year = None
    if n_trades is not None and period_years:
        trades_per_year = n_trades / period_years
    num_folds = result.get('num_folds')
    folds_with_trades = result.get('folds_with_trades')
    fold_coverage = None
    if num_folds:
        fold_coverage = folds_with_trades / num_folds
    return {
        'win_rate_pct': result.get('win_rate_pct'),
        'net_pnl_pct': result.get('net_pnl_pct'),
        'num_trades': n_trades,
        'max_drawdown_pct': result.get('max_drawdown_pct'),
        'profit_factor': result.get('profit_factor'),
        'trades_per_year': trades_per_year,
        'fold_coverage': fold_coverage,
    }


def build_certificate(*, sweep_path: str, sweep_data: dict, sweep_sha256: str,
                      label: str, verdict: GateVerdict,
                      thresholds: GateThresholds, git_commit: str, now_ms: int,
                      override_reason: str | None,
                      operator: str | None) -> dict:
    """Build the certificate dict for ``label``.

    Raises ``ValueError`` if the config failed the gate and no override reason
    was supplied — refusing to certify is the whole point of the gate.
    """
    action = decide(verdict, override_reason)
    if action == 'refuse':
        raise ValueError(
            f'config {label!r} failed the promotion gate and no override '
            f'reason was supplied; refusing to certify. Reasons: '
            + '; '.join(verdict.reasons)
        )

    result = find_result(sweep_data, label) or {}
    cert = {
        'schema_version': SCHEMA_VERSION,
        'promoted_at_ms': now_ms,
        'label': label,
        'symbol': sweep_data.get('symbol'),
        'source_sweep_file': sweep_path,
        'source_sweep_sha256': sweep_sha256,
        'git_commit': git_commit,
        'params': result.get('params', {}),
        'config_params': translate_params(result.get('params', {})),
        'gate': {
            'thresholds': {
                'min_dsr': thresholds.min_dsr,
                'min_trades': thresholds.min_trades,
                'min_side_trades': thresholds.min_side_trades,
                'require_positive_pnl': thresholds.require_positive_pnl,
                'require_positive_sharpe': thresholds.require_positive_sharpe,
                'min_profit_factor': thresholds.min_profit_factor,
                'max_drawdown_pct': thresholds.max_drawdown_pct,
                'min_fold_coverage': thresholds.min_fold_coverage,
            },
            'passed': verdict.passed,
            'metrics': verdict.metrics,
            'reasons': verdict.reasons,
        },
        'expected_metrics': _expected_metrics(
            result, sweep_data.get('period_years')
        ),
        'validation_caveats': list(VALIDATION_CAVEATS),
    }
    if action == 'override':
        cert['override'] = {
            'is_override': True,
            'reason': override_reason,
            'operator': operator,
            'overridden_at_ms': now_ms,
        }
    return cert
