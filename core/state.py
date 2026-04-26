import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_FILE = Path('data/bot_state.json')
_DEFAULT_STATE = {
    'state': 'WAITING_SIGNAL',
    'position': None,
}


class BotState(Enum):
    WAITING_SIGNAL  = 'WAITING_SIGNAL'
    ORDER_PENDING   = 'ORDER_PENDING'
    IN_POSITION     = 'IN_POSITION'
    ERROR_COOLDOWN  = 'ERROR_COOLDOWN'


class StateManager:
    """Persists bot state and open position to bot_state.json.

    On instantiation, loads existing state from disk. If the file does not
    exist, starts in WAITING_SIGNAL with no open position.
    """

    def __init__(self, state_file: Path = _STATE_FILE) -> None:
        self._path = state_file
        payload = self._load()
        self._state = BotState(payload['state'])
        self._position: dict | None = payload['position']
        self._daily_pnl: float = float(payload.get('daily_pnl', 0.0))
        self._daily_date: str  = str(payload.get('daily_date', ''))
        logger.info(
            'StateManager loaded state=%s has_position=%s daily_pnl=%.4f daily_date=%s',
            self._state.value, self._position is not None, self._daily_pnl, self._daily_date,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_state(self) -> BotState:
        return self._state

    def set_state(self, state: BotState) -> None:
        previous = self._state
        self._state = state
        self._persist()
        logger.info('state_transition from=%s to=%s', previous.value, state.value)

    def get_position(self) -> dict | None:
        return self._position

    def set_position(self, position: dict | None) -> None:
        self._position = position
        self._persist()
        logger.info('position_updated has_position=%s', position is not None)

    def get_daily_pnl(self) -> tuple[float, str]:
        """Return (daily_pnl_fraction, date_str) for circuit-breaker persistence across restarts."""
        return self._daily_pnl, self._daily_date

    def set_daily_pnl(self, pnl: float, date: str) -> None:
        """Persist the daily PnL fraction so the circuit breaker survives restarts."""
        self._daily_pnl = pnl
        self._daily_date = date
        self._persist()
        logger.debug('daily_pnl_persisted pnl=%.4f date=%s', pnl, date)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            logger.debug('state_file_not_found path=%s starting fresh', self._path)
            return dict(_DEFAULT_STATE)

        try:
            raw = self._path.read_text(encoding='utf-8')
            payload: dict[str, Any] = json.loads(raw)
            # Backwards-compat: legacy positions had no `side` field. Default to 'long'
            # since that is the only direction the bot supported pre-shorts.
            pos = payload.get('position')
            if pos is not None and 'side' not in pos:
                pos['side'] = 'long'
                logger.info('legacy_position_backfilled side=long entry_price=%.4f',
                            pos.get('entry_price', 0.0))
            # Validate enum value before accepting
            BotState(payload['state'])
            logger.debug('state_file_loaded path=%s state=%s', self._path, payload['state'])
            return payload
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning('state_file_corrupt path=%s error=%s — starting fresh', self._path, exc)
            return dict(_DEFAULT_STATE)

    def _persist(self) -> None:
        payload: dict[str, Any] = {
            'state':      self._state.value,
            'position':   self._position,
            'daily_pnl':  self._daily_pnl,
            'daily_date': self._daily_date,
        }
        data = json.dumps(payload, indent=2)
        # Prefer atomic write; fall back to direct write on Docker bind-mounted
        # single files where rename fails with EBUSY (errno 16) or EXDEV (18).
        tmp = self._path.with_suffix('.tmp')
        try:
            tmp.write_text(data, encoding='utf-8')
            os.replace(tmp, self._path)
        except OSError as exc:
            if exc.errno in (16, 18):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._path.write_text(data, encoding='utf-8')
            else:
                raise
        logger.debug('state_persisted path=%s state=%s', self._path, self._state.value)
