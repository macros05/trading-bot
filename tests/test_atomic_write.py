"""Tests for the bind-mount-safe write fallback in core/loop.py and core/state.py.

The fallback path matters because single-file Docker bind mounts on overlayfs
sever the inode link the first time O_TRUNC is applied. Using a tmp + rename
strategy fails with EBUSY (errno 16) or EXDEV (errno 18); the fallback must
write in place WITHOUT triggering a copy-up. We verify:

  1. Normal path: tmp + os.replace works in the happy case.
  2. EBUSY/EXDEV fallback: writes the new content to the original file.
  3. Other OSError values still raise (no silent data loss).
  4. The fallback uses ftruncate-in-place (preserves inode).
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.loop import _atomic_or_direct_write
from core.state import BotState, StateManager


class TestAtomicOrDirectWrite(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / 'health.json'
        self.path.write_text('{"old": true}', encoding='utf-8')

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_happy_path_atomic_rename(self) -> None:
        _atomic_or_direct_write(self.path, '{"new": 1}')
        self.assertEqual(self.path.read_text(), '{"new": 1}')

    def test_ebusy_fallback_writes_data(self) -> None:
        real_replace = os.replace

        def fake_replace(src, dst):
            err = OSError('Device or resource busy')
            err.errno = 16  # EBUSY
            raise err

        with patch('core.loop.os.replace', side_effect=fake_replace):
            _atomic_or_direct_write(self.path, '{"new": 2}')
        self.assertEqual(self.path.read_text(), '{"new": 2}')

    def test_exdev_fallback_writes_data(self) -> None:
        def fake_replace(src, dst):
            err = OSError('Cross-device link')
            err.errno = 18  # EXDEV
            raise err

        with patch('core.loop.os.replace', side_effect=fake_replace):
            _atomic_or_direct_write(self.path, '{"new": 3}')
        self.assertEqual(self.path.read_text(), '{"new": 3}')

    def test_other_oserror_re_raises(self) -> None:
        def fake_replace(src, dst):
            err = OSError('Permission denied')
            err.errno = 13  # EACCES
            raise err

        with patch('core.loop.os.replace', side_effect=fake_replace):
            with self.assertRaises(OSError):
                _atomic_or_direct_write(self.path, '{"new": 4}')

    def test_fallback_preserves_inode(self) -> None:
        original_inode = self.path.stat().st_ino

        def fake_replace(src, dst):
            err = OSError('Device or resource busy')
            err.errno = 16
            raise err

        with patch('core.loop.os.replace', side_effect=fake_replace):
            _atomic_or_direct_write(self.path, '{"new": 5}')
        self.assertEqual(self.path.stat().st_ino, original_inode,
                         'fallback path must not change the file inode (bind-mount safety)')

    def test_fallback_truncates_existing_content(self) -> None:
        self.path.write_text('{"old": "a much longer string than the new content"}', encoding='utf-8')

        def fake_replace(src, dst):
            err = OSError('Device or resource busy')
            err.errno = 16
            raise err

        with patch('core.loop.os.replace', side_effect=fake_replace):
            _atomic_or_direct_write(self.path, '{"x": 1}')
        self.assertEqual(self.path.read_text(), '{"x": 1}')
        # No trailing garbage from the old content
        self.assertEqual(self.path.stat().st_size, len('{"x": 1}'))


class TestStateManagerBindMountSafe(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / 'state.json'

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_state_persist_falls_back_on_ebusy(self) -> None:
        sm = StateManager(state_file=self.path)
        sm.set_state(BotState.WAITING_SIGNAL)
        original_inode = self.path.stat().st_ino

        def fake_replace(src, dst):
            err = OSError('Device or resource busy')
            err.errno = 16
            raise err

        with patch('core.state.os.replace', side_effect=fake_replace):
            sm.set_state(BotState.IN_POSITION)

        self.assertEqual(self.path.stat().st_ino, original_inode)
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload['state'], 'IN_POSITION')


if __name__ == '__main__':
    unittest.main()
