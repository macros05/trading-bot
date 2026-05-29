"""Anti-overfitting champion-promotion gate.

A backtested sweep config may only become the live champion if it clears the
hard rules established by the 2026-05-27/28 audit:

  1. ``dsr_pvalue >= min_dsr`` — López de Prado Deflated-Sharpe test: the
     observed Sharpe survives the multiple-comparison correction for the number
     of configs tried. Below 0.95 the result is indistinguishable from luck.
  2. ``num_trades >= min_trades`` — enough samples for the statistics to mean
     anything. The audited champion fired 16 times in 2 years; that is noise.
  3. Each materially-traded side's win-rate 95% lower bound must beat that
     side's fee-adjusted breakeven. Comparing the *blended* lower bound against
     a per-side breakeven lets a one-sided money-loser slip through, so when the
     per-side counts are available we check each side independently and only
     fall back to the blended comparison when neither side has enough trades.
  4. Profitability floor — positive net PnL, positive Sharpe, profit factor > 1.
     The Wilson lower bound is a probabilistic proxy; with asymmetric SL/TP a
     config can clear it yet still realise a net loss, so realised profitability
     is checked directly.
  5. Path-risk ceiling — max drawdown below ``max_drawdown_pct``. DSR and the
     Wilson bound test the return distribution, not interim drawdown; the only
     live protection is the −3%/day breaker, which would not stop a slow bleed.
  6. Out-of-sample fold coverage — the edge must show up across folds, not be
     concentrated in one lucky window (a classic overfit signature).

Non-finite metric values (NaN/inf) are treated as *absent* so they fail closed
rather than silently passing every ``<`` comparison.

This module is pure (no I/O); the CLI and the runtime guard build on it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateThresholds:
    """The hard bars a config must clear to be promotable."""
    min_dsr: float = 0.95
    min_trades: int = 100
    min_side_trades: int = 30
    require_positive_pnl: bool = True
    require_positive_sharpe: bool = True
    min_profit_factor: float = 1.0
    max_drawdown_pct: float = 25.0
    min_fold_coverage: float = 0.6


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
    """Return a finite numeric field, or ``None`` if absent or non-finite.

    NaN/inf are mapped to ``None`` so they flow into the 'missing' reasons and
    fail the gate closed — never silently pass a ``value < threshold`` check.
    """
    value = result.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _check_dsr(result: dict, thr: GateThresholds) -> list[str]:
    dsr = _num(result, 'dsr_pvalue')
    if dsr is None:
        return ['DSR p-value missing or non-finite']
    if dsr < thr.min_dsr:
        return [f'DSR p-value {dsr:.3f} < required {thr.min_dsr:.2f} '
                '(Sharpe indistinguishable from luck)']
    return []


def _check_trades(result: dict, thr: GateThresholds) -> list[str]:
    n = _num(result, 'num_trades')
    if n is None:
        return ['num_trades missing or non-finite']
    if n < thr.min_trades:
        return [f'num_trades {n} < required {thr.min_trades} '
                '(sample too small to be statistically meaningful)']
    return []


def _check_one_side(side: str, sv: dict, breakeven: float,
                    thr: GateThresholds) -> list[str]:
    lb = _num(sv, 'wr_lower_95')
    if lb is None:
        return [f'{side} side WR 95% lower bound missing or non-finite']
    if lb <= breakeven:
        return [f'{side} side WR 95% LB {lb:.2f}% <= breakeven '
                f'{breakeven:.2f}% (side edge may be negative)']
    return []


def _check_win_rate(result: dict, thr: GateThresholds) -> list[str]:
    """Per-side WR-LB vs per-side breakeven; blended fallback when no side
    has enough trades. Avoids passing a config that loses on one side."""
    be_long = _num(result, 'breakeven_wr_long')
    be_short = _num(result, 'breakeven_wr_short')
    by_side = result.get('by_side') or {}
    reasons: list[str] = []
    checked_any = False
    for side, be in (('long', be_long), ('short', be_short)):
        sv = by_side.get(side) or {}
        n_side = _num(sv, 'trades')
        if n_side is not None and n_side >= thr.min_side_trades and be is not None:
            checked_any = True
            reasons.extend(_check_one_side(side, sv, be, thr))
    if checked_any:
        return reasons
    return _check_win_rate_blended(result, be_long, be_short)


def _check_win_rate_blended(result: dict, be_long, be_short) -> list[str]:
    wr_lb = _num(result, 'wr_lower_95')
    breakevens = [b for b in (be_long, be_short) if b is not None]
    if wr_lb is None:
        return ['wr_lower_95 missing or non-finite']
    if not breakevens:
        return ['breakeven win rate missing from result']
    strictest = max(breakevens)
    if wr_lb <= strictest:
        return [f'win-rate 95% lower bound {wr_lb:.2f}% <= breakeven '
                f'{strictest:.2f}% (edge may be negative)']
    return []


def _check_profitability(result: dict, thr: GateThresholds) -> list[str]:
    reasons: list[str] = []
    if thr.require_positive_pnl:
        pnl = _num(result, 'net_pnl_pct')
        if pnl is None:
            reasons.append('net_pnl_pct missing or non-finite')
        elif pnl <= 0:
            reasons.append(f'net_pnl_pct {pnl:.2f}% <= 0 (config lost money)')
    if thr.require_positive_sharpe:
        sr = _num(result, 'sharpe_annual')
        if sr is None:
            reasons.append('sharpe_annual missing or non-finite')
        elif sr <= 0:
            reasons.append(f'sharpe_annual {sr:.3f} <= 0 '
                           '(no positive risk-adjusted return)')
    reasons.extend(_check_profit_factor(result, thr))
    return reasons


def _check_profit_factor(result: dict, thr: GateThresholds) -> list[str]:
    # None means "no losing trades" (sweep maps inf->None) -> pass. _MISSING
    # means malformed -> reject. A non-finite float is also malformed.
    pf = result.get('profit_factor', _MISSING)
    if pf is _MISSING:
        return ['profit_factor missing from result']
    if pf is None:
        return []
    if isinstance(pf, float) and not math.isfinite(pf):
        return ['profit_factor non-finite']
    if pf <= thr.min_profit_factor:
        return [f'profit_factor {pf:.2f} <= {thr.min_profit_factor:.2f} '
                '(gross losses >= gross profits)']
    return []


def _check_drawdown(result: dict, thr: GateThresholds) -> list[str]:
    dd = _num(result, 'max_drawdown_pct')
    if dd is None:
        return ['max_drawdown_pct missing or non-finite']
    if dd > thr.max_drawdown_pct:
        return [f'max_drawdown_pct {dd:.2f}% > ceiling '
                f'{thr.max_drawdown_pct:.2f}% (path risk too high)']
    return []


def _check_fold_coverage(result: dict, thr: GateThresholds) -> list[str]:
    nf = _num(result, 'num_folds')
    fwt = _num(result, 'folds_with_trades')
    if nf is None or fwt is None:
        return ['fold coverage (num_folds/folds_with_trades) missing']
    if nf <= 0:
        return ['num_folds is zero (no out-of-sample folds)']
    coverage = fwt / nf
    if coverage < thr.min_fold_coverage:
        return [f'OOS fold coverage {coverage:.2f} ({int(fwt)}/{int(nf)}) < '
                f'required {thr.min_fold_coverage:.2f} '
                '(edge concentrated — overfit signature)']
    return []


def _fold_coverage(metrics: dict) -> float:
    nf = metrics.get('num_folds') or 0
    fwt = metrics.get('folds_with_trades') or 0
    return fwt / nf if nf else 0.0


def _build_metrics(result: dict) -> dict:
    return {
        'dsr_pvalue': _num(result, 'dsr_pvalue'),
        'num_trades': _num(result, 'num_trades'),
        'wr_lower_95': _num(result, 'wr_lower_95'),
        'breakeven_wr_long': _num(result, 'breakeven_wr_long'),
        'breakeven_wr_short': _num(result, 'breakeven_wr_short'),
        'sharpe_annual': _num(result, 'sharpe_annual'),
        'sharpe_trade': _num(result, 'sharpe_trade'),
        'net_pnl_pct': _num(result, 'net_pnl_pct'),
        'win_rate_pct': _num(result, 'win_rate_pct'),
        'max_drawdown_pct': _num(result, 'max_drawdown_pct'),
        'profit_factor': result.get('profit_factor'),
        'num_folds': _num(result, 'num_folds'),
        'folds_with_trades': _num(result, 'folds_with_trades'),
    }


_CHECKS = (_check_dsr, _check_trades, _check_win_rate,
           _check_profitability, _check_drawdown, _check_fold_coverage)


def evaluate_config(result: dict, thresholds: GateThresholds) -> GateVerdict:
    """Evaluate a single sweep ``result`` dict against ``thresholds``."""
    label = result.get('label', '<unlabelled>')
    reasons: list[str] = []
    for check in _CHECKS:
        reasons.extend(check(result, thresholds))
    return GateVerdict(
        label=label, passed=not reasons, reasons=reasons,
        metrics=_build_metrics(result),
    )


def select_champion(results: list[dict],
                    thresholds: GateThresholds) -> GateVerdict | None:
    """Return the most robust passing config, or ``None`` if none pass.

    Ranking favours robustness over raw return: DSR margin first, then OOS fold
    coverage, then per-trade Sharpe; net PnL is only the final tie-break. Ranking
    by annualised Sharpe would reward higher trade counts (more curve-fit
    surface) — backwards for an anti-overfit gate.
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
            v.metrics.get('dsr_pvalue') or 0.0,
            _fold_coverage(v.metrics),
            v.metrics.get('sharpe_trade') or 0.0,
            v.metrics.get('net_pnl_pct') or 0.0,
        ),
    )
