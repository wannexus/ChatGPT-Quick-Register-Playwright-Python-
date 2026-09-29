"""Opt-in SQL smoke check using a connection-local temporary table only.

Run: ./.venv/bin/python tests/mysql_phone_smoke.py
No production account row or persistent schema is modified.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.account_store import AccountStore


def main():
    live_store = AccountStore()
    connection = live_store._connect()
    table = "qr_phone_check_" + uuid.uuid4().hex

    class TemporaryCursor:
        def __init__(self):
            self.cursor = connection.cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.cursor.close()

        def execute(self, sql, params=None):
            return self.cursor.execute(re.sub(r"\baccounts\b", f"`{table}`", sql), params)

        def fetchone(self):
            return self.cursor.fetchone()

    class TemporaryConnection:
        cursor = TemporaryCursor
        commit = staticmethod(connection.commit)
        rollback = staticmethod(connection.rollback)

        def close(self):
            # Each repository operation borrows this one temporary-table session.
            pass

    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TEMPORARY TABLE `{table}` LIKE accounts")
            cursor.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = 9000000000")
        store = AccountStore(live_store.config, connection_factory=lambda **_kwargs: TemporaryConnection())
        ids = []
        for index in range(4):
            saved = store.save_account(f"fixture-{index}@example.invalid", "fixture", {"fixture": True})
            ids.append(saved["id"])
        phone = "+15550009999"
        results = [store.bind_codex_phone(account_id, phone)["ok"] for account_id in ids]
        assert results == [True, True, True, False], results
        assert store.bind_codex_phone(ids[0], phone)["already_bound"]
        store.save_account("fixture-0@example.invalid", "fixture", {"fixture": "updated"})
        saved = store.get_account(ids[0])
        assert saved["codexPhoneNumber"] == phone
        assert saved["fixture"] == "updated"
        with store.reserve_codex_phone(ids[3], "+15550008888"):
            pass
        assert store.count_codex_phone_accounts("+15550008888") == 0
        other_connection = live_store._connect()
        lock_name = "qr-lock-check:" + uuid.uuid4().hex
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
                assert cursor.fetchone()["acquired"] == 1
            with other_connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
                assert cursor.fetchone()["acquired"] == 0
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            other_connection.close()
        print("MySQL temporary-table checks OK: cap, idempotence, binding preservation, rollback, cross-session locks")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
