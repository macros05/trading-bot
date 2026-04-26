"""
Tests for core/state.py.

Run from project root:
    python -m unittest tests.test_state
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.state import BotState, StateManager


def _manager(tmp_dir: str, filename: str = 'bot_state.json') -> StateManager:
    return StateManager(state_file=Path(tmp_dir) / filename)


# ---------------------------------------------------------------------------
# BotState enum
# ---------------------------------------------------------------------------

class TestBotStateEnum(unittest.TestCase):

    def test_all_values_exist(self):
        names = {s.name for s in BotState}
        self.assertEqual(names, {'WAITING_SIGNAL', 'ORDER_PENDING', 'IN_POSITION', 'ERROR_COOLDOWN'})

    def test_values_are_strings(self):
        for state in BotState:
            self.assertIsInstance(state.value, str)


# ---------------------------------------------------------------------------
# Initialisation — no existing file
# ---------------------------------------------------------------------------

class TestInitWithoutFile(unittest.TestCase):

    def test_starts_in_waiting_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            self.assertEqual(manager.get_state(), BotState.WAITING_SIGNAL)

    def test_starts_with_no_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            self.assertIsNone(manager.get_position())


# ---------------------------------------------------------------------------
# Initialisation — existing valid file
# ---------------------------------------------------------------------------

class TestInitWithValidFile(unittest.TestCase):

    def _write(self, tmp: str, payload: dict) -> Path:
        path = Path(tmp) / 'bot_state.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_loads_state_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {'state': 'IN_POSITION', 'position': None})
            manager = _manager(tmp)
            self.assertEqual(manager.get_state(), BotState.IN_POSITION)

    def test_loads_position_from_disk(self):
        # Position stored without `side` (legacy shape); backfill injects side='long'.
        position = {'symbol': 'BTC/USDT', 'entry_price': 30000.0, 'qty': 0.01}
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {'state': 'IN_POSITION', 'position': position})
            manager = _manager(tmp)
            expected = {**position, 'side': 'long'}
            self.assertEqual(manager.get_position(), expected)

    def test_all_states_round_trip(self):
        for state in BotState:
            with tempfile.TemporaryDirectory() as tmp:
                self._write(tmp, {'state': state.value, 'position': None})
                manager = _manager(tmp)
                self.assertEqual(manager.get_state(), state)


# ---------------------------------------------------------------------------
# Initialisation — corrupt file falls back to defaults
# ---------------------------------------------------------------------------

class TestInitWithCorruptFile(unittest.TestCase):

    def test_invalid_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'bot_state.json').write_text('not json', encoding='utf-8')
            manager = _manager(tmp)
            self.assertEqual(manager.get_state(), BotState.WAITING_SIGNAL)

    def test_unknown_state_value_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'bot_state.json').write_text(
                json.dumps({'state': 'UNKNOWN', 'position': None}), encoding='utf-8'
            )
            manager = _manager(tmp)
            self.assertEqual(manager.get_state(), BotState.WAITING_SIGNAL)

    def test_missing_state_key_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'bot_state.json').write_text(
                json.dumps({'position': None}), encoding='utf-8'
            )
            manager = _manager(tmp)
            self.assertEqual(manager.get_state(), BotState.WAITING_SIGNAL)


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------

class TestSetState(unittest.TestCase):

    def test_updates_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            manager.set_state(BotState.IN_POSITION)
            self.assertEqual(manager.get_state(), BotState.IN_POSITION)

    def test_persists_to_disk_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot_state.json'
            manager = StateManager(state_file=path)
            manager.set_state(BotState.ERROR_COOLDOWN)
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['state'], 'ERROR_COOLDOWN')

    def test_reloaded_manager_sees_new_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            m1 = _manager(tmp)
            m1.set_state(BotState.ORDER_PENDING)
            m2 = _manager(tmp)
            self.assertEqual(m2.get_state(), BotState.ORDER_PENDING)


# ---------------------------------------------------------------------------
# set_position / get_position
# ---------------------------------------------------------------------------

class TestPosition(unittest.TestCase):

    def test_set_position_stores_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            pos = {'symbol': 'BTC/USDT', 'entry_price': 50000.0, 'qty': 0.1}
            manager.set_position(pos)
            self.assertEqual(manager.get_position(), pos)

    def test_set_position_none_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            manager.set_position({'symbol': 'ETH/USDT'})
            manager.set_position(None)
            self.assertIsNone(manager.get_position())

    def test_persists_position_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot_state.json'
            manager = StateManager(state_file=path)
            pos = {'symbol': 'BTC/USDT', 'entry_price': 42000.0}
            manager.set_position(pos)
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['position'], pos)

    def test_state_preserved_when_position_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp)
            manager.set_state(BotState.IN_POSITION)
            manager.set_position({'symbol': 'BTC/USDT'})
            self.assertEqual(manager.get_state(), BotState.IN_POSITION)

    def test_reloaded_manager_sees_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            # set_position stores whatever dict is passed; a dict without `side`
            # will have side='long' backfilled on the next load from disk.
            pos = {'symbol': 'BTC/USDT', 'entry_price': 30000.0}
            m1 = _manager(tmp)
            m1.set_state(BotState.IN_POSITION)
            m1.set_position(pos)
            m2 = _manager(tmp)
            self.assertEqual(m2.get_position(), {**pos, 'side': 'long'})


class TestPositionSideField(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        self._tmp.close()
        from pathlib import Path
        self._path = Path(self._tmp.name)

    def tearDown(self):
        self._path.unlink(missing_ok=True)

    def test_legacy_position_loads_with_side_long(self):
        """A bot_state.json without `side` is treated as a long position."""
        import json
        from core.state import StateManager
        legacy = {
            'state': 'IN_POSITION',
            'position': {'entry_price': 100.0, 'qty': 0.1, 'ts': 1700000000000},
        }
        self._path.write_text(json.dumps(legacy))
        sm = StateManager(state_file=self._path)
        pos = sm.get_position()
        self.assertEqual(pos.get('side'), 'long')

    def test_short_position_roundtrip(self):
        from core.state import StateManager, BotState
        sm = StateManager(state_file=self._path)
        sm.set_position({'side': 'short', 'entry_price': 100.0, 'qty': 0.1,
                         'ts': 1700000000000, 'sl_price': 103.5, 'tp_price': 94.0})
        sm.set_state(BotState.IN_POSITION)
        sm2 = StateManager(state_file=self._path)
        self.assertEqual(sm2.get_position().get('side'), 'short')

    def test_set_position_none_clears(self):
        from core.state import StateManager
        sm = StateManager(state_file=self._path)
        sm.set_position({'side': 'short', 'entry_price': 100.0, 'qty': 0.1, 'ts': 1})
        sm.set_position(None)
        self.assertIsNone(sm.get_position())

    def test_existing_long_position_with_side_field_loads_intact(self):
        """If a state file already has side='long', it must round-trip without modification."""
        import json
        from core.state import StateManager
        with_side = {
            'state': 'IN_POSITION',
            'position': {'side': 'long', 'entry_price': 50.0, 'qty': 0.5,
                         'ts': 1700000000000, 'sl_price': 48.75, 'tp_price': 52.0},
        }
        self._path.write_text(json.dumps(with_side))
        sm = StateManager(state_file=self._path)
        pos = sm.get_position()
        self.assertEqual(pos.get('side'), 'long')
        self.assertEqual(pos.get('entry_price'), 50.0)


if __name__ == '__main__':
    unittest.main()
