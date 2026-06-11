"""Tests for telegram_chat_memory.py — JSON conversation memory.

Spec: last 8 exchanges (user msg + bot answer) persisted in
data/ai_chat_history.json via the repo's atomic write helper
(core.loop._atomic_or_direct_write). Oldest beyond 8 are trimmed.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram_chat_memory as mem


class TestChatMemory(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history_file = Path(self.tmp.name) / 'data' / 'ai_chat_history.json'
        self.original_path = mem.HISTORY_FILE
        patcher = patch.object(mem, 'HISTORY_FILE', self.history_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_path_is_spec_file(self) -> None:
        self.assertEqual(Path('data/ai_chat_history.json'), Path(self.original_path))

    def test_roundtrip_single_exchange(self) -> None:
        mem.save_exchange('¿cómo va el bot?', 'Pausado, sin posición.')
        history = mem.load_exchanges()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['user'], '¿cómo va el bot?')
        self.assertEqual(history[0]['assistant'], 'Pausado, sin posición.')

    def test_file_is_valid_json_on_disk(self) -> None:
        mem.save_exchange('q', 'a')
        payload = json.loads(self.history_file.read_text())
        self.assertIsInstance(payload, list)

    def test_trims_to_last_8_exchanges(self) -> None:
        for i in range(12):
            mem.save_exchange(f'q{i}', f'a{i}')
        history = mem.load_exchanges()
        self.assertEqual(len(history), mem.MAX_EXCHANGES)
        self.assertEqual(mem.MAX_EXCHANGES, 8)
        self.assertEqual(history[0]['user'], 'q4')   # oldest beyond 8 trimmed
        self.assertEqual(history[-1]['user'], 'q11')

    def test_corrupt_file_recovers_empty(self) -> None:
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_file.write_text('{not json')
        self.assertEqual(mem.load_exchanges(), [])
        mem.save_exchange('q', 'a')  # must not raise
        self.assertEqual(len(mem.load_exchanges()), 1)

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(mem.load_exchanges(), [])

    def test_uses_repo_atomic_write_helper(self) -> None:
        with patch.object(mem, '_atomic_or_direct_write') as mock_write:
            mem.save_exchange('q', 'a')
        mock_write.assert_called_once()
        path_arg, data_arg = mock_write.call_args[0]
        self.assertEqual(Path(path_arg), self.history_file)
        json.loads(data_arg)

    def test_save_failure_does_not_raise(self) -> None:
        with patch.object(mem, '_atomic_or_direct_write',
                          side_effect=OSError('disk full')):
            mem.save_exchange('q', 'a')  # swallowed + logged


if __name__ == '__main__':
    unittest.main()
