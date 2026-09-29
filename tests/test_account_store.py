from __future__ import annotations

import base64
import importlib
import importlib.util
import os
import unittest
from datetime import datetime, timezone


def _subject(test: unittest.TestCase):
    spec = importlib.util.find_spec("core.account_store")
    test.assertIsNotNone(spec, "the canonical MySQL account-store owner is missing")
    if spec is None:
        return None
    return importlib.import_module("core.account_store")


class AccountStoreCryptoTests(unittest.TestCase):
    def test_password_ciphertext_round_trips_without_storing_plaintext(self):
        store = _subject(self)
        if store is None:
            return
        encrypt = getattr(store, "encrypt_password", None)
        decrypt = getattr(store, "decrypt_password", None)
        self.assertTrue(callable(encrypt), "account_store.encrypt_password is required")
        self.assertTrue(callable(decrypt), "account_store.decrypt_password is required")
        key = base64.urlsafe_b64encode(os.urandom(32))
        ciphertext = encrypt("a-private-password", key)
        self.assertNotIn(b"a-private-password", ciphertext)
        self.assertEqual(decrypt(ciphertext, key), "a-private-password")

    def test_missing_or_invalid_encryption_key_fails_closed(self):
        store = _subject(self)
        if store is None:
            return
        loader = getattr(store, "load_config", None)
        self.assertTrue(callable(loader), "account_store.load_config is required")
        env = {
            "QR_MYSQL_HOST": "127.0.0.1",
            "QR_MYSQL_PORT": "3306",
            "QR_MYSQL_USER": "root",
            "QR_MYSQL_PASSWORD": "",
            "QR_MYSQL_DATABASE": "account_store_test",
        }
        with self.assertRaisesRegex(ValueError, "QR_ACCOUNT_ENCRYPTION_KEY"):
            loader(env=env)
        with self.assertRaises(ValueError):
            loader(env={**env, "QR_ACCOUNT_ENCRYPTION_KEY": "not-a-fernet-key"})

    def test_config_loads_required_mysql_fields_and_key(self):
        store = _subject(self)
        if store is None:
            return
        loader = getattr(store, "load_config", None)
        self.assertTrue(callable(loader), "account_store.load_config is required")
        key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        config = loader(env={
            "QR_MYSQL_HOST": "db.local",
            "QR_MYSQL_PORT": "3307",
            "QR_MYSQL_USER": "qr_user",
            "QR_MYSQL_PASSWORD": "secret",
            "QR_MYSQL_DATABASE": "quick_register",
            "QR_ACCOUNT_ENCRYPTION_KEY": key,
        })
        self.assertEqual(config.host, "db.local")
        self.assertEqual(config.port, 3307)
        self.assertEqual(config.database, "quick_register")
        self.assertEqual(config.user, "qr_user")


class AccountLocalValidityTests(unittest.TestCase):
    def test_local_validity_reports_missing_session_or_token(self):
        store = _subject(self)
        if store is None:
            return
        check = getattr(store, "check_local_validity", None)
        self.assertTrue(callable(check), "account_store.check_local_validity is required")
        self.assertEqual(check({})["status"], "missing_session")
        self.assertEqual(check({"session": {}})["status"], "missing_token")

    def test_local_validity_uses_only_stored_expiry_and_never_network(self):
        store = _subject(self)
        if store is None:
            return
        check = getattr(store, "check_local_validity", None)
        self.assertTrue(callable(check), "account_store.check_local_validity is required")
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.assertEqual(
            check({"session": {"accessToken": "token", "expires": "2026-09-27T00:00:00Z"}}, now=now)["status"],
            "expired",
        )
        self.assertEqual(
            check({"session": {"accessToken": "token", "expires": "2026-09-29T00:00:00Z"}}, now=now)["status"],
            "not_expired",
        )
        result = check({"session": {"accessToken": "token"}}, now=now)
        self.assertEqual(result["status"], "unknown_expiry")
        self.assertIn("仅依据本地", result["message"])


if __name__ == "__main__":
    unittest.main()
