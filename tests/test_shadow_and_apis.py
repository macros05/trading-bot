"""Tests for shadow mode and the new API endpoints."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _setup_env() -> None:
    os.environ['ADMIN_USERNAME']   = 'admin'
    os.environ['ADMIN_PASSWORD']   = 'admin'
    os.environ['SECRET_KEY']       = 'test'
    os.environ['INTERNAL_API_KEY'] = 'test-internal'


class TestShadowMode(unittest.TestCase):
    def test_disabled_by_default(self):
        os.environ.pop('SHADOW_MODE', None)
        from analytics.shadow import is_shadow_enabled, record_decision
        self.assertFalse(is_shadow_enabled())
        # record_decision returns None when disabled
        self.assertIsNone(record_decision({
            'decision_ts_ms': 1, 'side': 'long',
            'entry_price': 100, 'sl_price': 98, 'tp_price': 104,
        }))

    def test_enabled_writes_to_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            os.environ['SHADOW_MODE'] = 'on'
            from analytics import live_db, shadow
            with patch.object(live_db, '_DEFAULT_PATH', tmpdir / 'live.db'):
                live_db.init_db(tmpdir / 'live.db')
                with patch.object(shadow, 'insert_shadow_trade',
                                  side_effect=lambda r: live_db.insert_shadow_trade(
                                      r, tmpdir / 'live.db')):
                    sid = shadow.record_decision({
                        'decision_ts_ms': 1, 'side': 'long',
                        'entry_price': 100, 'sl_price': 98, 'tp_price': 104,
                    })
                    self.assertIsNotNone(sid)
            os.environ['SHADOW_MODE'] = 'off'


class TestNewApiEndpoints(unittest.TestCase):
    def setUp(self):
        _setup_env()
        import importlib
        import api as api_mod
        importlib.reload(api_mod)
        self.api_mod = api_mod

    def _client(self) -> TestClient:
        client = TestClient(self.api_mod.app)
        client.cookies.set(
            self.api_mod._SESSION_COOKIE,
            self.api_mod._serializer.dumps('admin'),
        )
        return client

    def test_live_trades_endpoint_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / 'live.db'
            from analytics import live_db
            live_db.init_db(db_path)
            with patch.object(live_db, '_DEFAULT_PATH', db_path):
                resp = self._client().get('/api/live-trades')
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data['total'], 0)
                self.assertEqual(data['trades'], [])

    def test_validation_endpoint_returns_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / 'live.db'
            from analytics import live_db
            live_db.init_db(db_path)
            with patch.object(live_db, '_DEFAULT_PATH', db_path):
                resp = self._client().get('/api/validation')
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn('evaluation', data)
                self.assertIn('conditions', data)
                self.assertIn('alerts', data['evaluation'])

    def test_readiness_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / 'live.db'
            from analytics import live_db
            live_db.init_db(db_path)
            with patch.object(live_db, '_DEFAULT_PATH', db_path):
                resp = self._client().get('/api/readiness')
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn('ready', data)
                self.assertIn('checks', data)
                self.assertFalse(data['ready'])  # no trades → not ready


if __name__ == '__main__':
    unittest.main()
