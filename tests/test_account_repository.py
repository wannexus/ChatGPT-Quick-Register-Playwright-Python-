from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from datetime import datetime, timezone


class _MemoryDatabase:
    def __init__(self):
        self.rows = {}
        self.next_id = 1
        self.sql = []

    def connect(self, **_kwargs):
        return _MemoryConnection(self)


class _MemoryConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return _MemoryCursor(self.database)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _MemoryCursor:
    def __init__(self, database):
        self.database = database
        self.result = None
        self.lastrowid = 0
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        params = tuple(params or ())
        self.database.sql.append((sql, params))
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select get_lock"):
            self.result = {"acquired": 1}
        elif normalized.startswith("select release_lock"):
            self.result = {"released": 1}
        elif "count(distinct id) as account_count" in normalized:
            phone = params[0]
            self.result = {"account_count": sum(
                row.get("account_data", {}).get("codexPhoneNumber") == phone
                for row in self.database.rows.values()
            )}
        elif normalized.startswith("select") and "where id = %s for update" in normalized:
            self.result = self.database.rows.get(int(params[0]))
            self.result = dict(self.result) if self.result else None
        elif normalized.startswith("insert into accounts"):
            email, password_ciphertext, account_data, relogin_required = params
            account_data = json.loads(account_data)
            row = next((row for row in self.database.rows.values() if row["email"] == email), None)
            if row is None:
                row = {"id": self.database.next_id, "created_at": datetime.now(timezone.utc)}
                self.database.next_id += 1
            elif "json_set(values(account_data)" in normalized:
                old_phone = row["account_data"].get("codexPhoneNumber")
                if old_phone:
                    account_data["codexPhoneNumber"] = old_phone
            row.update({
                "email": email,
                "password_ciphertext": password_ciphertext,
                "account_data": account_data,
                "relogin_required": relogin_required,
                "validity_status": "unknown_expiry",
                "validity_checked_at": None,
                "updated_at": datetime.now(timezone.utc),
            })
            self.database.rows[row["id"]] = row
            self.lastrowid = row["id"]
            self.result = dict(row)
        elif normalized.startswith("select") and "where email = %s" in normalized:
            email = params[0]
            self.result = next((dict(row) for row in self.database.rows.values() if row["email"] == email), None)
        elif normalized.startswith("select") and "where id = %s" in normalized:
            self.result = self.database.rows.get(int(params[0]))
            self.result = dict(self.result) if self.result else None
        elif normalized.startswith("select") and "from accounts" in normalized:
            self.result = [dict(row) for row in self.database.rows.values()]
        elif normalized.startswith("delete from accounts"):
            account_id = int(params[0])
            existed = self.database.rows.pop(account_id, None) is not None
            self.rowcount = 1 if existed else 0
            self.result = None
        elif normalized.startswith("update accounts set account_data = json_set"):
            payload, email, _payload_again = params
            incoming = json.loads(payload)
            row = next((r for r in self.database.rows.values() if r["email"] == email), None)
            if row and incoming.get("codexPhoneNumber") and not row["account_data"].get("codexPhoneNumber"):
                row["account_data"]["codexPhoneNumber"] = incoming["codexPhoneNumber"]
            self.result = None
        elif normalized.startswith("update accounts set account_data = %s"):
            payload, account_id = params
            self.database.rows[int(account_id)]["account_data"] = json.loads(payload)
            self.result = None
        elif normalized.startswith("update accounts"):
            status, checked_at, account_id = params
            row = self.database.rows.get(int(account_id))
            self.rowcount = 1 if row else 0
            if row:
                row["validity_status"] = status
                row["validity_checked_at"] = checked_at
            self.result = None
        else:
            raise AssertionError(f"Unexpected SQL in fake DB: {sql}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        if isinstance(self.result, list):
            return self.result
        return []


class _BootstrapDatabase:
    def __init__(self):
        self.sql = []
        self.connections = []
        self.table_exists = False
        self.created = False

    def connect(self, **options):
        self.connections.append(options)
        return _BootstrapConnection(self)


class _BootstrapConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return _BootstrapCursor(self.database)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _BootstrapCursor:
    def __init__(self, database):
        self.database = database
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.database.sql.append((sql, tuple(params or ())))
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create database"):
            self.database.created = True
        elif normalized.startswith("create table"):
            self.database.table_exists = True
        elif normalized.startswith("select 1"):
            self.result = {"ready": 1}
        elif normalized.startswith("show tables"):
            self.result = {"table": "accounts"} if self.database.table_exists else None
        elif normalized.startswith("use "):
            pass
        else:
            raise AssertionError(f"Unexpected bootstrap SQL: {sql}")

    def fetchone(self):
        return self.result


class AccountRepositoryTests(unittest.TestCase):
    def _subject(self):
        spec = importlib.util.find_spec("core.account_store")
        self.assertIsNotNone(spec, "the canonical MySQL account-store owner is missing")
        if spec is None:
            return None
        module = importlib.import_module("core.account_store")
        store_type = getattr(module, "AccountStore", None)
        self.assertTrue(callable(store_type), "account_store.AccountStore is required")
        for method in ("save_account", "get_account", "list_accounts"):
            self.assertTrue(callable(getattr(store_type, method, None)), f"AccountStore.{method} is required")
        return module, store_type

    def _config(self, module):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        return module.AccountStoreConfig(
            host="localhost",
            port=3306,
            user="qr_user",
            password="",
            database="quick_register",
            encryption_key=key,
        )

    def test_initialize_creates_safe_schema_and_readiness_check_is_read_only(self):
        subject = self._subject()
        if subject is None:
            return
        module, store_type = subject
        initializer = getattr(store_type, "initialize", None)
        checker = getattr(store_type, "check_ready", None)
        self.assertTrue(callable(initializer), "AccountStore.initialize is required")
        self.assertTrue(callable(checker), "AccountStore.check_ready is required")
        if not callable(initializer) or not callable(checker):
            return

        db = _BootstrapDatabase()
        store = store_type(self._config(module), connection_factory=db.connect)
        result = store.initialize()
        self.assertTrue(result["ok"])
        self.assertEqual(result["database"], "quick_register")
        self.assertTrue(db.created)
        self.assertTrue(db.table_exists)
        create_table = next(sql for sql, _params in db.sql if sql.lower().startswith("create table"))
        self.assertIn("password_ciphertext VARBINARY", create_table)
        self.assertIn("account_data JSON", create_table)
        self.assertIn("UNIQUE KEY uq_accounts_email", create_table)
        self.assertNotIn("password VARCHAR", create_table)

        db.sql.clear()
        self.assertTrue(store.check_ready()["ok"])
        self.assertFalse(any(sql.lower().startswith(("create database", "create table")) for sql, _ in db.sql))

    def test_cli_init_and_check_dispatch_without_printing_secrets(self):
        subject = self._subject()
        if subject is None:
            return
        module, _store_type = subject
        entry = getattr(module, "main", None)
        self.assertTrue(callable(entry), "core.account_store.main is required")
        if not callable(entry):
            return

        calls = []

        class ReadyStore:
            def initialize(self):
                calls.append("init")
                return {"ok": True, "database": "quick_register", "table": "accounts"}

            def check_ready(self):
                calls.append("check")
                return {"ok": True, "database": "quick_register", "table": "accounts"}

        with patch.object(module, "AccountStore", ReadyStore):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(entry(["init"]), 0)
                self.assertEqual(entry(["check"]), 0)
        self.assertEqual(calls, ["init", "check"])
        self.assertIn("quick_register", output.getvalue())
        self.assertNotIn("password", output.getvalue().lower())
        self.assertNotIn("encryption_key", output.getvalue().lower())

    def test_save_get_and_list_keep_password_ciphertext_only(self):
        subject = self._subject()
        if subject is None:
            return
        module, store_type = subject
        db = _MemoryDatabase()
        store = store_type(self._config(module), connection_factory=db.connect)
        password = "private-'password"
        data = {
            "email": " person@example.com ",
            "password": "must-not-be-copied-into-json",
            "session": {"accessToken": "local-token"},
            "reloginRequired": False,
        }

        saved = store.save_account(" person@example.com ", password, data)
        self.assertEqual(saved["email"], "person@example.com")
        self.assertEqual(saved["id"], 1)
        self.assertNotIn("password", saved)
        row = db.rows[1]
        self.assertNotIn(password.encode(), row["password_ciphertext"])
        self.assertNotIn("password", row["account_data"])
        self.assertNotIn("must-not-be-copied-into-json", json.dumps(row["account_data"]))

        public_record = store.get_account(1)
        self.assertNotIn("password", public_record)
        secret_record = store.get_account(1, include_password=True)
        self.assertEqual(secret_record["password"], password)
        self.assertNotIn("password", store.list_accounts()[0])

    def test_save_uses_parameterized_sql_and_keeps_stable_id_on_upsert(self):
        subject = self._subject()
        if subject is None:
            return
        module, store_type = subject
        db = _MemoryDatabase()
        store = store_type(self._config(module), connection_factory=db.connect)
        first = store.save_account("x@example.com' OR 1=1 --", "secret", {"session": {}})
        second = store.save_account("x@example.com' OR 1=1 --", "new-secret", {"session": {"accessToken": "t"}})
        self.assertEqual(first["id"], second["id"])
        insert_sql = next(sql for sql, _params in db.sql if sql.lower().startswith("insert into accounts"))
        self.assertIn("%s", insert_sql)
        self.assertNotIn("x@example.com", insert_sql)
        self.assertNotIn("new-secret", insert_sql)

    def test_delete_account_removes_only_the_requested_row(self):
        subject = self._subject()
        if subject is None:
            return
        module, store_type = subject
        subject_deleter = getattr(store_type, "delete_account", None)
        self.assertTrue(callable(subject_deleter), "AccountStore.delete_account is required")
        if not callable(subject_deleter):
            return

        db = _MemoryDatabase()
        store = store_type(self._config(module), connection_factory=db.connect)
        first = store.save_account("one@example.com", "pw", {"session": {"accessToken": "t"}})
        second = store.save_account("two@example.com", "pw", {"session": {"accessToken": "t"}})

        self.assertTrue(store.delete_account(second["id"]))
        self.assertIsNone(store.get_account(second["id"]))
        self.assertEqual(store.get_account(first["id"])["email"], "one@example.com")
        self.assertFalse(store.delete_account(second["id"]))
        delete_sql = next(sql for sql, _params in db.sql if sql.lower().startswith("delete from accounts"))
        self.assertIn("%s", delete_sql)

    def test_record_validity_persists_status_and_timestamp_locally(self):
        subject = self._subject()
        if subject is None:
            return
        module, store_type = subject
        recorder = getattr(store_type, "record_validity", None)
        self.assertTrue(callable(recorder), "AccountStore.record_validity is required")
        if not callable(recorder):
            return

        db = _MemoryDatabase()
        store = store_type(self._config(module), connection_factory=db.connect)
        saved = store.save_account(
            "check@example.com",
            "pw",
            {"session": {"accessToken": "t", "expires": "2030-01-01T00:00:00Z"}},
        )
        validity = module.check_local_validity(
            {"session": {"accessToken": "t", "expires": "2030-01-01T00:00:00Z"}},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        updated = store.record_validity(saved["id"], validity)
        self.assertEqual(updated["localValidity"]["status"], "not_expired")
        self.assertIsNotNone(updated["localValidity"]["checkedAt"])
        self.assertEqual(db.rows[saved["id"]]["validity_status"], "not_expired")
        self.assertIsNone(store.record_validity(9999, validity), "unknown account IDs must not create rows")

    def test_local_validity_makes_no_network_request(self):
        subject = self._subject()
        if subject is None:
            return
        module, _store_type = subject
        account = {"email": "local@example.com", "session": {"accessToken": "t", "expires": "2030-01-01T00:00:00Z"}}
        with patch("socket.socket.connect", side_effect=AssertionError("local check must not open a socket")):
            result = module.check_local_validity(account, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "not_expired")
        self.assertIn("不访问远端", module.check_local_validity({}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))["message"])


if __name__ == "__main__":
    unittest.main()
