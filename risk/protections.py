"""Composable pre-entry guards on top of the circuit breaker.

The circuit breaker (in risk/manager.py) is the absolute hard stop: once the
daily PnL exceeds -circuit_breaker_pct, no entries are allowed.

Protections are softer, additive gates: cooldown periods, max-SL-per-day, etc.
They never override the breaker — they only ADD reasons to refuse an entry.
"""
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Protection(Protocol):
    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        """Return (True, reason) if entry should be blocked, else (False, None)."""
        ...


class CooldownPeriod:
    """Block entries for `cooldown_seconds` after the most recent stop_loss exit.

    Aggressive-profile default cooldown_seconds=0 disables this guard entirely.
    """

    def __init__(self, cooldown_seconds: int = 0) -> None:
        if cooldown_seconds < 0:
            raise ValueError('cooldown_seconds must be >= 0')
        self._cooldown_ms = cooldown_seconds * 1000

    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        if self._cooldown_ms == 0:
            return False, None
        last_sl_ts = 0
        for trade in trades_history:
            if trade.get('reason') == 'stop_loss':
                ts = int(trade.get('exit_ts', 0))
                if ts > last_sl_ts:
                    last_sl_ts = ts
        if last_sl_ts == 0:
            return False, None
        elapsed = now_ms - last_sl_ts
        if elapsed < self._cooldown_ms:
            remaining = (self._cooldown_ms - elapsed) // 1000
            return True, f'cooldown active, {remaining}s remaining since last stop_loss'
        return False, None


class StoplossGuard:
    """Block entries when SL hits in the last `lookback_seconds` >= `max_sl`.

    Aggressive-profile default max_sl=10 / lookback_seconds=86_400 (24h) is
    effectively permissive for a strategy expected to do <10 SL/day.
    """

    def __init__(self, max_sl: int = 10, lookback_seconds: int = 86_400) -> None:
        if max_sl < 1:
            raise ValueError('max_sl must be >= 1')
        if lookback_seconds <= 0:
            raise ValueError('lookback_seconds must be positive')
        self._max_sl = max_sl
        self._lookback_ms = lookback_seconds * 1000

    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        cutoff = now_ms - self._lookback_ms
        sl_count = sum(
            1 for trade in trades_history
            if trade.get('reason') == 'stop_loss'
            and int(trade.get('exit_ts', 0)) >= cutoff
        )
        if sl_count >= self._max_sl:
            return True, f'stoploss_guard: {sl_count} SL hits in last {self._lookback_ms // 1000}s'
        return False, None
