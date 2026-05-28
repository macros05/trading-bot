"""Anti-overfitting champion-promotion gate.

A backtested sweep config may only become the live champion if it clears the
hard statistical rules established by the 2026-05-27/28 audit:

  1. ``dsr_pvalue >= min_dsr``  — López de Prado Deflated-Sharpe test: the
     observed Sharpe survives the multiple-comparison correction for the number
     of configs tried. p < 0.95 means the result is indistinguishable from luck.
  2. ``num_trades >= min_trades`` — enough samples for the statistics to mean
     anything. The audited champion fired 16 times in 2 years; that is noise.
  3. ``wr_lower_95 > breakeven`` — the 95% lower bound on the win rate must beat
     the fee-adjusted breakeven of the *strictest* side, else the true edge can
     be negative even when the point estimate looks positive.

This module is pure (no I/O); the CLI and the runtime guard build on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateThresholds:
    """The hard bars a config must clear to be promotable."""
    min_dsr: float = 0.95
    min_trades: int = 100


@dataclass
class GateVerdict:
    """Outcome of evaluating one config against the gate."""
    label: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


# Sentinel for absent numeric fields: guarantees the relevant check fails
# rather than raising, so a malformed result is rejected, never promoted.
_MISSING = object()


def _num(result: dict, key: str):
    value = result.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    return value


def evaluate_config(result: dict, thresholds: GateThresholds) -> GateVerdict:
    """Evaluate a single sweep ``result`` dict against ``thresholds``."""
    label = result.get('label', '<unlabelled>')
    reasons: list[str] = []

    dsr = _num(result, 'dsr_pvalue')
    if dsr is None:
        reasons.append('DSR p-value missing from result')
    elif dsr < thresholds.min_dsr:
        reasons.append(
            f'DSR p-value {dsr:.3f} < required {thresholds.min_dsr:.2f} '
            '(Sharpe indistinguishable from luck)'
        )

    n_trades = _num(result, 'num_trades')
    if n_trades is None:
        reasons.append('num_trades missing from result')
    elif n_trades < thresholds.min_trades:
        reasons.append(
            f'num_trades {n_trades} < required {thresholds.min_trades} '
            '(sample too small to be statistically meaningful)'
        )

    wr_lb = _num(result, 'wr_lower_95')
    be_long = _num(result, 'breakeven_wr_long')
    be_short = _num(result, 'breakeven_wr_short')
    breakevens = [b for b in (be_long, be_short) if b is not None]
    if wr_lb is None:
        reasons.append('wr_lower_95 missing from result')
    elif not breakevens:
        reasons.append('breakeven win rate missing from result')
    else:
        strictest = max(breakevens)
        if wr_lb <= strictest:
            reasons.append(
                f'win-rate 95% lower bound {wr_lb:.2f}% <= breakeven '
                f'{strictest:.2f}% (edge may be negative)'
            )

    metrics = {
        'dsr_pvalue': dsr,
        'num_trades': n_trades,
        'wr_lower_95': wr_lb,
        'breakeven_wr_long': be_long,
        'breakeven_wr_short': be_short,
        'sharpe_annual': _num(result, 'sharpe_annual'),
        'net_pnl_pct': _num(result, 'net_pnl_pct'),
        'win_rate_pct': _num(result, 'win_rate_pct'),
        'max_drawdown_pct': _num(result, 'max_drawdown_pct'),
    }
    return GateVerdict(
        label=label, passed=not reasons, reasons=reasons, metrics=metrics
    )


def select_champion(results: list[dict],
                    thresholds: GateThresholds) -> GateVerdict | None:
    """Return the best passing config, or ``None`` if none pass.

    Ranking: highest annualised Sharpe, tie-broken by net PnL %.
    """
    passing = [
        v for v in (evaluate_config(r, thresholds) for r in results)
        if v.passed
    ]
    if not passing:
        return None
    return max(
        passing,
        key=lambda v: (
            v.metrics.get('sharpe_annual') or 0.0,
            v.metrics.get('net_pnl_pct') or 0.0,
        ),
    )
