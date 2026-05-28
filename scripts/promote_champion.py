#!/usr/bin/env python3
"""Promote a backtested sweep config to live champion — behind the anti-overfit
gate (DSR >= 0.95 AND num_trades >= 100 AND win-rate LB > breakeven).

By default the gate is a HARD BLOCK. A config that fails can only be promoted
with an explicit, recorded override:

    python scripts/promote_champion.py --sweep backtest/results/sweep_v7_full_24mo_btc.json \\
        --label mtf_off --override --reason "deliberate paper-only test" --operator marcos

The script writes ``champion_certificate.json`` (the audit record) and prints the
BOT_CONFIG constants to apply. It never edits config.py automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backtest.promotion_gate import GateThresholds, evaluate_config  # noqa: E402
from backtest.promotion_cert import (  # noqa: E402
    build_certificate,
    decide,
    find_result,
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return 'unknown'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sweep', required=True, help='Path to sweep result JSON')
    parser.add_argument('--label', required=True, help='Config label to promote')
    parser.add_argument('--min-dsr', type=float, default=0.95)
    parser.add_argument('--min-trades', type=int, default=100)
    parser.add_argument('--override', action='store_true',
                        help='Allow promoting a config that fails the gate')
    parser.add_argument('--reason', default=None,
                        help='Written justification (required with --override)')
    parser.add_argument('--operator', default=None)
    parser.add_argument('--out', default=os.path.join(_REPO_ROOT,
                        'champion_certificate.json'))
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the certificate without writing it')
    args = parser.parse_args(argv)

    thresholds = GateThresholds(min_dsr=args.min_dsr, min_trades=args.min_trades)

    with open(args.sweep, encoding='utf-8') as handle:
        sweep_data = json.load(handle)

    result = find_result(sweep_data, args.label)
    if result is None:
        labels = [r.get('label') for r in sweep_data.get('results', [])]
        print(f'ERROR: label {args.label!r} not found. Available: {labels}',
              file=sys.stderr)
        return 1

    verdict = evaluate_config(result, thresholds)
    print(f'Gate evaluation for {args.label!r} '
          f'(min_dsr={args.min_dsr}, min_trades={args.min_trades}):')
    print(f'  metrics: {verdict.metrics}')
    if verdict.passed:
        print('  ✅ PASSES gate')
    else:
        print('  ❌ FAILS gate:')
        for reason in verdict.reasons:
            print(f'     - {reason}')

    override_reason = args.reason if args.override else None
    action = decide(verdict, override_reason)

    if action == 'refuse':
        print('\nREFUSED: config failed the gate. To promote anyway, re-run '
              'with --override --reason "<justification>".', file=sys.stderr)
        return 2

    cert = build_certificate(
        sweep_path=os.path.relpath(args.sweep, _REPO_ROOT),
        sweep_data=sweep_data,
        sweep_sha256=_sha256_file(args.sweep),
        label=args.label,
        verdict=verdict,
        thresholds=thresholds,
        git_commit=_git_commit(),
        now_ms=int(time.time() * 1000),
        override_reason=override_reason,
        operator=args.operator,
    )

    if args.dry_run:
        print('\n--- certificate (dry-run, not written) ---')
        print(json.dumps(cert, indent=2))
        return 0

    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(cert, handle, indent=2)
        handle.write('\n')

    if action == 'override':
        print('\n⚠️  OVERRIDE: certified a config that FAILED the gate.')
        print(f'   reason: {override_reason!r}  operator: {args.operator!r}')
    else:
        print('\n✅ Certified cleanly.')
    print(f'   wrote {args.out}')
    print('\nApply these BOT_CONFIG values in config.py, then restart:')
    for key, value in cert['config_params'].items():
        print(f'   {key} = {value!r}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
