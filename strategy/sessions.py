"""Market session classification and helpers.

BTC behaves differently across regional sessions; we classify each timestamp
so per-session metrics and filters can be applied. Pure functions, no I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone

ASIA   = 'asia'    # 00:00–08:00 UTC
EUROPE = 'europe'  # 08:00–13:00 UTC (overlap with USA from 13:00)
USA    = 'usa'     # 13:00–21:00 UTC
OFF    = 'off'     # 21:00–24:00 UTC (lowest activity, often skipped)

ALL_SESSIONS: tuple[str, ...] = (ASIA, EUROPE, USA, OFF)


def session_for_ts(ts_ms: int) -> str:
    """Return the session label for a given UTC timestamp (ms since epoch).

    Boundaries are inclusive on the left, exclusive on the right.
    """
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    if 0 <= hour < 8:
        return ASIA
    if 8 <= hour < 13:
        return EUROPE
    if 13 <= hour < 21:
        return USA
    return OFF


def is_session_allowed(ts_ms: int, blocked: tuple[str, ...] | None) -> bool:
    """Return True when the current session is not in the blocked tuple."""
    if not blocked:
        return True
    return session_for_ts(ts_ms) not in blocked
