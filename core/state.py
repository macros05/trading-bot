import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_FILE = Path('bot_state.json')
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
        logger.info('StateManager loaded state=%s has_position=%s', self._state.value, self._position is not None)

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
            # Validate enum value before accepting
            BotState(payload['state'])
            logger.debug('state_file_loaded path=%s state=%s', self._path, payload['state'])
            return payload
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning('state_file_corrupt path=%s error=%s — starting fresh', self._path, exc)
            return dict(_DEFAULT_STATE)

    def _persist(self) -> None:
        payload: dict[str, Any] = {
            'state': self._state.value,
            'position': self._position,
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        logger.debug('state_persisted path=%s state=%s', self._path, self._state.value)
