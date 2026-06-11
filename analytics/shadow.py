"""Shadow trading mode — log decisions without execution.

When BINANCE_MODE=paper and SHADOW_MODE=on, every entry signal is recorded
to shadow_trades and resolved retroactively from candle history. The recorder
preserves the exact entry/exit/SL/TP that the live decision pathway computed
so that, if the user later switches paper → demo, the in-flight decisions
have a track record.
"""
from __future__ import annotations

import logging
import os

from analytics.live_db import insert_shadow_trade, update_shadow_resolution

logger = logging.getLogger(__name__)


def is_shadow_enabled() -> bool:
    return os.getenv('SHADOW_MODE', 'off').lower() in ('on', '1', 'true')


def record_decision(decision: dict) -> int | None:
    """Persist a shadow decision. Returns the row id, or None if disabled."""
    if not is_shadow_enabled():
        return None
    try:
        return insert_shadow_trade(decision)
    except Exception as exc:
        logger.debug('shadow_record_failed error=%s', exc)
        return None


def resolve_shadow(
    shadow_id: int, exit_ts_ms: int, exit_price: float,
    side: str, sl_price: float, tp_price: float, exit_reason: str,
) -> None:
    """Compute hypothetical PnL when the shadow trade hits SL/TP/timeout."""
    if side == 'long':
        ref = sl_price if exit_reason == 'stop_loss' else tp_price
        # qty=1 placeholder — caller should ideally compute the same notional
        # the real trade would have used; we log the price diff and let the
        # weekly analysis attach correct sizing.
        pnl = exit_price - ref
    else:
        pnl = ref_short = (sl_price if exit_reason == 'stop_loss' else tp_price) - exit_price
    try:
        update_shadow_resolution(shadow_id, exit_ts_ms, exit_price,
                                 exit_reason, pnl)
    except Exception as exc:
        logger.debug('shadow_resolve_failed error=%s', exc)
