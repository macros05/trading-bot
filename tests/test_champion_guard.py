"""Tests for the runtime champion guard.

The guard runs at startup and verifies that the live BOT_CONFIG actually matches
a valid champion certificate. It is a TRIPWIRE, not a kill-switch: it NEVER
raises and NEVER stops the loop (Session 0 invariant). It returns a structured
result; main.py decides to log + Telegram-alert and then proceed.
"""
import unittest

import json
import tempfile
import os

from core.champion_guard import verify_champion, GuardResult, load_certificate


def _cert(passed=True, override=None, config_params=None):
    cert = {
        'label': 'cand',
        # config_params is the config-named copy the guard compares against
        'config_params': config_params or {'rsi_threshold': 40.0,
                                           'use_adx_filter': True},
        'gate': {'passed': passed, 'reasons': [] if passed else ['DSR too low']},
    }
    if override:
        cert['override'] = {'is_override': True, 'reason': override,
                            'operator': 'marcos'}
    return cert


KEYS = ['rsi_threshold', 'use_adx_filter']


class TestVerifyChampion(unittest.TestCase):
    def test_no_certificate_is_critical(self):
        res = verify_champion({'rsi_threshold': 40.0}, None, material_keys=KEYS)
        self.assertEqual(res.level, 'CRITICAL')
        self.assertFalse(res.is_ok)
        self.assertIn('certificate', res.message.lower())

    def test_matching_passing_certificate_is_ok(self):
        bot = {'rsi_threshold': 40.0, 'use_adx_filter': True}
        res = verify_champion(bot, _cert(passed=True), material_keys=KEYS)
        self.assertEqual(res.level, 'OK')
        self.assertTrue(res.is_ok)

    def test_param_mismatch_is_critical_and_names_the_key(self):
        bot = {'rsi_threshold': 50.0, 'use_adx_filter': True}  # drifted
        res = verify_champion(bot, _cert(passed=True), material_keys=KEYS)
        self.assertEqual(res.level, 'CRITICAL')
        self.assertIn('rsi_threshold', res.message)

    def test_override_certificate_is_warning_not_ok(self):
        bot = {'rsi_threshold': 40.0, 'use_adx_filter': True}
        res = verify_champion(
            bot, _cert(passed=False, override='deliberate paper test'),
            material_keys=KEYS,
        )
        self.assertEqual(res.level, 'WARNING')
        self.assertFalse(res.is_ok)
        self.assertIn('deliberate paper test', res.message)

    def test_no_overlapping_material_keys_is_critical(self):
        bot = {'something_else': 1}
        res = verify_champion(bot, _cert(passed=True), material_keys=KEYS)
        self.assertEqual(res.level, 'CRITICAL')

    def test_guard_never_raises_on_malformed_certificate(self):
        # A certificate missing gate/config_params must degrade to CRITICAL, not crash
        res = verify_champion({'rsi_threshold': 40.0}, {}, material_keys=KEYS)
        self.assertIsInstance(res, GuardResult)
        self.assertEqual(res.level, 'CRITICAL')

    def test_default_compares_all_certificate_params_not_a_subset(self):
        # Regression: drift on a material key (use_volatility_filter) that was
        # NOT in the old hardcoded subset must still be caught. With no explicit
        # material_keys the guard compares EVERY key the certificate carries.
        bot = {'rsi_threshold': 40.0, 'use_volatility_filter': False}
        cert = _cert(passed=True, config_params={
            'rsi_threshold': 40.0, 'use_volatility_filter': True,  # drifted
        })
        res = verify_champion(bot, cert)  # no material_keys -> compare all
        self.assertEqual(res.level, 'CRITICAL')
        self.assertIn('use_volatility_filter', res.message)

    def test_falls_back_to_raw_params_when_no_config_params(self):
        # Legacy/edge cert with only 'params' (sweep-named) still verifies
        bot = {'rsi_threshold': 40.0, 'use_adx_filter': True}
        cert = {
            'label': 'cand',
            'params': {'rsi_threshold': 40.0, 'use_adx_filter': True},
            'gate': {'passed': True, 'reasons': []},
        }
        res = verify_champion(bot, cert, material_keys=KEYS)
        self.assertEqual(res.level, 'OK')


class TestLoadCertificate(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(load_certificate('/nonexistent/path/cert.json'))

    def test_valid_file_returns_dict(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'label': 'x'}, f)
            path = f.name
        try:
            self.assertEqual(load_certificate(path)['label'], 'x')
        finally:
            os.unlink(path)

    def test_malformed_json_returns_none_not_crash(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            f.write('{not valid json')
            path = f.name
        try:
            self.assertIsNone(load_certificate(path))
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
