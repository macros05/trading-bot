"""Smoke tests for the FastAPI endpoints added in v7."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _setup_env() -> None:
    # Force-set so module reload picks them up regardless of prior state
    os.environ['ADMIN_USERNAME']   = 'admin'
    os.environ['ADMIN_PASSWORD']   = 'admin'
    os.environ['SECRET_KEY']       = 'test'
    os.environ['INTERNAL_API_KEY'] = 'test-internal'


class TestPerformanceEndpoint(unittest.TestCase):
    def setUp(self):
        _setup_env()
        # Re-import api after env is set
        import importlib
        import api as api_mod
        importlib.reload(api_mod)
        self.api_mod = api_mod

    def _make_client_with_auth(self) -> TestClient:
        # Bypass HTTPS-only cookie restriction in test by patching set_cookie
        client = TestClient(self.api_mod.app)
        client.cookies.set(
            self.api_mod._SESSION_COOKIE,
            self.api_mod._serializer.dumps('admin'),
        )
        return client

    def test_performance_returns_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            trades_file = tmpdir / 'trades.json'
            trades_file.write_text(json.dumps([
                {'side': 'long', 'pnl_usdt': 5.0, 'pnl_pct': 0.5,
                 'result': 'WIN', 'reason': 'tp',
                 'entry_ts': 1714579200000, 'exit_ts': 1714582800000,
                 'entry_price': 100, 'exit_price': 105, 'qty': 1},
            ]))
            with patch.object(self.api_mod, '_TRADES_FILE', trades_file):
                client = self._make_client_with_auth()
                resp = client.get('/performance')
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data['total_trades'], 1)
                self.assertIn('by_side', data)
                self.assertIn('by_session', data)
                self.assertIn('equity_curve', data)
                self.assertIn('last_20_trades', data)


class TestControlEndpoints(unittest.TestCase):
    def setUp(self):
        _setup_env()
        import importlib
        import api as api_mod
        importlib.reload(api_mod)
        self.api_mod = api_mod

    def test_pause_then_resume(self):
        client = TestClient(self.api_mod.app)
        client.cookies.set(
            self.api_mod._SESSION_COOKIE,
            self.api_mod._serializer.dumps('admin'),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                resp = client.post('/control/pause')
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()['paused'])
                self.assertTrue((tmpdir / 'pause.flag').exists())

                resp = client.post('/control/resume')
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(resp.json()['paused'])
                self.assertFalse((tmpdir / 'pause.flag').exists())


class TestTelegramWebhook(unittest.TestCase):
    def setUp(self):
        _setup_env()
        import importlib
        import api as api_mod
        importlib.reload(api_mod)
        self.api_mod = api_mod

    def test_unknown_command_returns_handled_false(self):
        client = TestClient(self.api_mod.app)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                resp = client.post(
                    '/telegram/command',
                    json={'text': 'hola que tal'},
                    headers={'X-Internal-Key': 'test-internal'},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(resp.json()['handled'])

    def test_pause_command_returns_reply(self):
        client = TestClient(self.api_mod.app)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with patch('notifications._PAUSE_FILE', tmpdir / 'pause.flag'):
                resp = client.post(
                    '/telegram/command',
                    json={'text': '/pause'},
                    headers={'X-Internal-Key': 'test-internal'},
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data['handled'])
                self.assertIn('paused', data['reply'].lower())

    def test_missing_internal_key_rejected(self):
        client = TestClient(self.api_mod.app)
        resp = client.post('/telegram/command', json={'text': '/pause'})
        self.assertEqual(resp.status_code, 401)


if __name__ == '__main__':
    unittest.main()
