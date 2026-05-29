"""Runtime champion guard — startup tripwire for config/certificate drift.

Verifies that the live ``BOT_CONFIG`` matches a valid champion certificate
(written by ``scripts/promote_champion.py``). This catches the failure mode from
the audit: a config edited straight into ``config.py`` without passing the
promotion gate.

INVARIANT (Session 0): this guard NEVER raises and NEVER stops the loop. It
returns a :class:`GuardResult`; the caller logs + alerts and proceeds. A guard
that could crash the bot would be worse than the drift it detects.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _reject_non_finite(token: str):
    """``parse_constant`` hook: a NaN/Infinity literal in the certificate is
    corruption, not data — reject it so the guard treats the cert as invalid."""
    raise ValueError(f'non-finite literal {token!r} in certificate JSON')


def load_certificate(path: str) -> dict | None:
    """Load the champion certificate JSON; return ``None`` on any problem.

    Never raises — a missing or corrupt certificate is itself a signal the
    guard reports as CRITICAL, not a reason to crash the bot. NaN/Infinity
    literals are rejected rather than parsed into floats.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle, parse_constant=_reject_non_finite)
    except (OSError, ValueError) as exc:
        logger.warning('Could not load champion certificate %s: %s', path, exc)
        return None


def _norm(value):
    """Normalise sequences to lists so a tuple-valued live param (e.g.
    ``blocked_sessions``) does not register as drift against its JSON-loaded
    (list) certificate counterpart."""
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


@dataclass
class GuardResult:
    level: str        # 'OK' | 'WARNING' | 'CRITICAL'
    message: str

    @property
    def is_ok(self) -> bool:
        return self.level == 'OK'


def verify_champion(bot_config: dict, certificate: dict | None,
                    material_keys: list[str] | None = None) -> GuardResult:
    """Compare ``bot_config`` against ``certificate``; never raises.

    When ``material_keys`` is ``None`` the guard compares EVERY key the
    certificate carries (``config_params``) — that translated set *is* the
    material definition, so no strategy-deciding key can silently drift.
    """
    try:
        if not certificate:
            return GuardResult(
                'CRITICAL',
                'No champion certificate present — running an uncertified '
                'config. Promote via scripts/promote_champion.py.',
            )

        # Prefer the config-named copy; fall back to raw sweep params.
        params = certificate.get('config_params') or certificate.get('params')
        gate = certificate.get('gate')
        if not isinstance(params, dict) or not isinstance(gate, dict):
            return GuardResult(
                'CRITICAL',
                'Champion certificate is malformed (missing config_params/gate) '
                '— cannot verify live config.',
            )

        keys = material_keys if material_keys is not None else list(params.keys())
        shared = [k for k in keys if k in params and k in bot_config]
        if not shared:
            return GuardResult(
                'CRITICAL',
                'Cannot verify champion: certificate params share no material '
                'keys with the live config.',
            )

        mismatches = [
            f'{k}: live={bot_config[k]!r} cert={params[k]!r}'
            for k in shared if _norm(bot_config[k]) != _norm(params[k])
        ]
        if mismatches:
            return GuardResult(
                'CRITICAL',
                'Live config does not match certified champion — '
                + '; '.join(mismatches),
            )

        override = certificate.get('override')
        if isinstance(override, dict) and override.get('is_override'):
            reason = override.get('reason', '<no reason recorded>')
            return GuardResult(
                'WARNING',
                f'Running OVERRIDDEN champion (failed the gate): {reason}',
            )

        if gate.get('passed'):
            return GuardResult(
                'OK', f"Champion {certificate.get('label')!r} verified."
            )

        return GuardResult(
            'CRITICAL',
            'Certificate marks the gate as failed but carries no override — '
            'inconsistent certificate.',
        )
    except Exception as exc:  # never let the guard crash the bot
        return GuardResult(
            'CRITICAL', f'Champion guard error (treating as uncertified): {exc}'
        )
