"""Auto-pause logic on macro-event days when historical performance is weak.

When live trades on FOMC/CPI/NFP days have a win rate below MACRO_WR_FLOOR
across at least MACRO_MIN_TRADES, today (if it is one of those events) is
considered too risky to trade. The check is conservative: it never *unpauses*
on its own.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MACRO_WR_FLOOR = 25.0
MACRO_MIN_TRADES = 5
HIGH_IMPACT_EVENTS = ('FOMC', 'CPI', 'NFP')


def should_auto_pause_for_macro(
    live_trades: list[dict], current_event: str | None,
) -> tuple[bool, str | None]:
    """Decide whether the bot should auto-pause for *current_event* today.

    Returns (should_pause, reason). Pauses only when:
      - current_event is in HIGH_IMPACT_EVENTS, AND
      - live_trades on past instances of that event have win rate < MACRO_WR_FLOOR
        with at least MACRO_MIN_TRADES samples.
    """
    if current_event not in HIGH_IMPACT_EVENTS:
        return False, None
    same = [t for t in live_trades if t.get('macro_event') == current_event]
    if len(same) < MACRO_MIN_TRADES:
        return False, None
    wins = sum(1 for t in same if t.get('result') == 'WIN')
    wr = wins / len(same) * 100.0
    if wr < MACRO_WR_FLOOR:
        reason = (f'auto-pause: {current_event} historical win_rate={wr:.1f}% '
                  f'(n={len(same)}) below {MACRO_WR_FLOOR}%')
        logger.info(reason)
        return True, reason
    return False, None
