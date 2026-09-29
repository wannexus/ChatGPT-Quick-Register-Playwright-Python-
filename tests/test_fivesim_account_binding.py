from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


class _MemoryBindingStore:
    """Simple contract model; SQL-path tests below exercise the real owner."""
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {1: {}, 2: {}, 3: {}, 4: {}}

    def bind_phone(self, account_id, phone, *, max_accounts=3):
        with self.lock:
            phone = phone.strip()
            owners = {account_id for account_id, data in self.rows.items()
                      if data.get("codexPhoneNumber") == phone}
            if account_id in owners:
                return {"ok": True, "already_bound": True, "account_count": len(owners)}
            if len(owners) >= max_accounts:
                return {"ok": False, "already_bound": False, "account_count": len(owners)}
            self.rows[account_id]["codexPhoneNumber"] = phone
            return {"ok": True, "already_bound": False, "account_count": len(owners) + 1}

    def available_phone_count(self, phone):
        return sum(data.get("codexPhoneNumber") == phone for data in self.rows.values())


class _SqlBindingDatabase:
    """Thread-safe MySQL SQL fake exercising AccountStore's actual transaction path."""
    def __init__(self):
        self.mutex = threading.RLock()
        self.phone_locks = {}
        self.rows = {}
        self.sql = []
        self.next_id = 1

    def connect(self, **_kwargs):
        return _SqlBindingConnection(self)


class _SqlBindingConnection:
    def __init__(self, database):
        self.database = database
        self.pending = {}
        self.closed = False
        self.held_phone_locks = set()

    def cursor(self):
        return _SqlBindingCursor(self)

    def commit(self):
        with self.database.mutex:
            self.database.rows.update(self.pending)
            self.pending.clear()

    def rollback(self):
        self.pending.clear()

    def close(self):
        with self.database.mutex:
            for name in tuple(self.held_phone_locks):
                self.database.phone_locks[name].release()
            self.held_phone_locks.clear()
        self.closed = True


class _SqlBindingCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.lock_key = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        params = tuple(params or ())
        normalized = " ".join(sql.lower().split())
        db = self.connection.database
        db.sql.append((normalized, params))
        if normalized.startswith("select get_lock"):
            self.lock_key = params[0]
            with db.mutex:
                lock = db.phone_locks.setdefault(self.lock_key, threading.Lock())
            acquired = lock.acquire(timeout=0.25)
            if acquired:
                self.connection.held_phone_locks.add(self.lock_key)
            self.result = {"acquired": int(acquired)}
        elif normalized.startswith("select release_lock"):
            lock_key = params[0]
            if lock_key in self.connection.held_phone_locks:
                with db.mutex:
                    db.phone_locks[lock_key].release()
                self.connection.held_phone_locks.remove(lock_key)
            self.result = {"released": 1}
        elif "count(distinct id) as account_count" in normalized:
            phone = params[0]
            with db.mutex:
                rows = list(db.rows.values()) + list(self.connection.pending.values())
            self.result = {"account_count": sum(
                (row.get("account_data") or {}).get("codexPhoneNumber") == phone
                for row in rows
            )}
        elif normalized.startswith("select") and "where id = %s for update" in normalized:
            account_id = int(params[0])
            with db.mutex:
                row = db.rows.get(account_id)
                self.result = dict(row) if row else None
        elif normalized.startswith("update accounts set account_data = %s"):
            payload, account_id = params
            account_id = int(account_id)
            with db.mutex:
                row = dict(db.rows[account_id])
                row["account_data"] = json.loads(payload)
                self.connection.pending[account_id] = row
            self.result = None
        elif normalized.startswith("insert into accounts"):
            email, password_ciphertext, payload, relogin_required = params
            data = json.loads(payload)
            with db.mutex:
                old = next((row for row in db.rows.values() if row["email"] == email), None)
                if old:
                    account_id = old["id"]
                    old_data = dict(old["account_data"])
                    if "json_set(values(account_data)" in normalized and old_data.get("codexPhoneNumber"):
                        data["codexPhoneNumber"] = old_data["codexPhoneNumber"]
                else:
                    account_id = db.next_id
                    db.next_id += 1
                row = {
                    "id": account_id,
                    "email": email,
                    "password_ciphertext": password_ciphertext,
                    "account_data": data,
                    "relogin_required": relogin_required,
                    "validity_status": "unknown_expiry",
                    "validity_checked_at": None,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
                self.connection.pending[account_id] = row
                self.lastrowid = account_id
            self.result = None
        elif normalized.startswith("select") and "where email = %s" in normalized:
            email = params[0]
            with db.mutex:
                row = next((row for row in db.rows.values() if row["email"] == email), None)
                if row is None:
                    row = next((row for row in self.connection.pending.values() if row["email"] == email), None)
                self.result = dict(row) if row else None
        elif normalized.startswith("select") and "where id = %s" in normalized:
            account_id = int(params[0])
            with db.mutex:
                row = self.connection.pending.get(account_id) or db.rows.get(account_id)
                self.result = dict(row) if row else None
        elif normalized.startswith("update accounts set account_data = json_set"):
            self.result = None
        else:
            raise AssertionError(f"Unexpected SQL in AccountStore fake: {sql}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result if isinstance(self.result, list) else []


class PhoneBindingContractTests(unittest.TestCase):
    def _store(self):
        from core.account_store import AccountStore, AccountStoreConfig

        key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        db = _SqlBindingDatabase()
        config = AccountStoreConfig("localhost", 3306, "u", "", "db", key)
        return AccountStore(config, connection_factory=db.connect), db

    def test_account_store_exposes_atomic_distinct_account_binding_contract(self):
        from core.account_store import AccountStore

        self.assertTrue(callable(getattr(AccountStore, "bind_codex_phone", None)))
        self.assertTrue(callable(getattr(AccountStore, "count_codex_phone_accounts", None)))

    def test_same_account_is_idempotent_and_does_not_consume_another_slot(self):
        store, _db = self._store()
        store.save_account("one@example.com", "pw", {"email": "one@example.com"})
        first = store.bind_codex_phone(1, "+15550000001")
        repeated = store.bind_codex_phone(1, "+15550000001")
        self.assertTrue(first["ok"])
        self.assertTrue(repeated["already_bound"])
        self.assertEqual(repeated["account_count"], 1)
        self.assertEqual(store.count_codex_phone_accounts("+15550000001"), 1)

    def test_actual_account_store_binding_uses_advisory_lock_and_caps_three_accounts(self):
        store, db = self._store()
        for account_id in (1, 2, 3, 4):
            email = f"user{account_id}@example.com"
            store.save_account(email, "pw", {"email": email})

        results = [store.bind_codex_phone(i, "+15550000009") for i in (1, 2, 3, 4)]

        self.assertEqual([r["ok"] for r in results], [True, True, True, False])
        self.assertEqual(store.count_codex_phone_accounts("+15550000009"), 3)
        self.assertTrue(any(sql.startswith("select get_lock") for sql, _ in db.sql))
        self.assertTrue(any(sql.startswith("select release_lock") for sql, _ in db.sql))

    def test_actual_account_store_concurrent_claims_never_exceed_three(self):
        store, _db = self._store()
        for account_id in range(1, 5):
            email = f"parallel{account_id}@example.com"
            store.save_account(email, "pw", {"email": email})
        barrier = threading.Barrier(4)

        def claim(account_id):
            barrier.wait(timeout=2)
            return store.bind_codex_phone(account_id, "+15550000010")

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim, range(1, 5)))
        self.assertEqual(sum(bool(result["ok"]) for result in results), 3)
        self.assertEqual(store.count_codex_phone_accounts("+15550000010"), 3)

    def test_normal_account_upsert_does_not_erase_existing_phone_binding(self):
        store, _db = self._store()
        store.save_account("preserve@example.com", "pw", {"email": "preserve@example.com"})
        store.bind_codex_phone(1, "+15550000011")

        store.save_account("preserve@example.com", "pw2", {"email": "preserve@example.com", "session": {"accessToken": "new"}})

        self.assertEqual(store.get_account(1)["codexPhoneNumber"], "+15550000011")

    def test_stale_snapshot_cannot_replace_binding_or_bypass_the_limit(self):
        store, _db = self._store()
        store.save_account("owner@example.com", "pw", {"codexPhoneNumber": "+15550000012"})
        self.assertNotIn("codexPhoneNumber", store.get_account(1))
        store.bind_codex_phone(1, "+15550000013")
        store.save_account("owner@example.com", "pw", {"codexPhoneNumber": "+15550000012"})
        self.assertEqual(store.get_account(1)["codexPhoneNumber"], "+15550000013")

    def test_reservation_serializes_verification_before_binding(self):
        store, _db = self._store()
        for account_id in range(1, 5):
            store.save_account(f"reserved{account_id}@example.com", "pw", {})
        barrier = threading.Barrier(4)

        def verify(account_id):
            barrier.wait(timeout=2)
            with store.reserve_codex_phone(account_id, "+15550000014") as bind:
                return bool(bind and bind()["ok"])

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(verify, range(1, 5)))
        self.assertEqual(sum(results), 3)

    def test_failed_verification_releases_reservation_without_binding(self):
        store, _db = self._store()
        store.save_account("failed@example.com", "pw", {})
        with self.assertRaisesRegex(RuntimeError, "OTP rejected"):
            with store.reserve_codex_phone(1, "+15550000015") as bind:
                self.assertIsNotNone(bind)
                raise RuntimeError("OTP rejected")
        self.assertEqual(store.count_codex_phone_accounts("+15550000015"), 0)
        with store.reserve_codex_phone(1, "+15550000015") as bind:
            self.assertTrue(bind()["ok"])

    def test_reservation_keeps_normal_updates_and_normalizes_phone(self):
        store, _db = self._store()
        store.save_account("updated@example.com", "pw", {})
        with store.reserve_codex_phone(1, "15550000016") as bind:
            store.save_account("updated@example.com", "pw", {"newMetadata": True})
            bind()
        self.assertTrue(store.get_account(1)["newMetadata"])
        self.assertEqual(store.count_codex_phone_accounts("15550000016"), 1)

    def test_invalid_phone_or_missing_account_cannot_claim_a_slot(self):
        store, _db = self._store()
        for phone in ("", "bad", "+0", "+1' OR 1=1"):
            with self.assertRaises(ValueError):
                store.bind_codex_phone(1, phone)
        with self.assertRaises(KeyError):
            store.bind_codex_phone(99, "+15550000017")
        store.save_account("after-error@example.com", "pw", {})
        self.assertTrue(store.bind_codex_phone(1, "+15550000017")["ok"])

    def test_four_concurrent_accounts_can_claim_only_three_distinct_slots_in_contract_model(self):
        store = _MemoryBindingStore()
        barrier = threading.Barrier(4)

        def claim(account_id):
            barrier.wait(timeout=2)
            return store.bind_phone(account_id, "+15550000002")

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim, (1, 2, 3, 4)))
        self.assertEqual(sum(result["ok"] for result in results), 3)
        self.assertEqual(store.available_phone_count("+15550000002"), 3)


if __name__ == "__main__":
    unittest.main()
