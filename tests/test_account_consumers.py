from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core import session
import register


class SessionSnapshotTests(unittest.TestCase):
    def test_build_snapshot_is_in_memory_and_excludes_password(self):
        builder = getattr(session, "build_account_snapshot", None)
        self.assertTrue(callable(builder), "session.build_account_snapshot is required")
        if not callable(builder):
            return
        captured_at = datetime(2026, 1, 2, tzinfo=session.timezone.utc)
        session_result = {
            "ok": True,
            "status": 200,
            "text": '{"accessToken":"local"}',
            "parsed": {"accessToken": "local", "expires": "2030-01-01T00:00:00Z"},
        }
        extras = {"authMode": "password", "codexAuth": {"refresh_token": "oauth"}}
        with patch.object(session, "datetime") as clock:
            clock.now.return_value = captured_at
            snapshot = builder("person@example.com", session_result=session_result, extras=extras)

        self.assertEqual(snapshot["email"], "person@example.com")
        self.assertEqual(snapshot["session"], session_result["parsed"])
        self.assertEqual(snapshot["refresh_token"], "oauth")
        self.assertEqual(snapshot["authMode"], "password")
        self.assertNotIn("password", snapshot)
        self.assertEqual(snapshot["savedAt"], captured_at.isoformat())


class RegistrationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_used_email_index_reads_mysql_instead_of_json_files(self):
        store_type = getattr(register, "AccountStore", None)
        self.assertTrue(callable(store_type), "register must use the canonical AccountStore")
        if not callable(store_type):
            return
        store = Mock()
        store.list_accounts.return_value = [
            {"email": " Used@Example.com "},
            {"email": "other@example.com"},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(register, "AccountStore", return_value=store):
            result = register._load_used_account_emails(Path(tmp))
        self.assertEqual(result, {"used@example.com", "other@example.com"})
        store.list_accounts.assert_called_once_with()

    def test_relogin_selection_uses_mysql_account_ids(self):
        store_type = getattr(register, "AccountStore", None)
        self.assertTrue(callable(store_type), "register must use the canonical AccountStore")
        if not callable(store_type):
            return
        store = Mock()
        store.list_accounts.return_value = [
            {"id": 1, "email": "one@example.com", "reloginRequired": True},
            {"id": 2, "email": "two@example.com", "reloginRequired": False},
        ]
        args = SimpleNamespace(relogin_id=[2], relogin_marked=False, relogin_limit=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(register, "AccountStore", return_value=store):
            targets = register._load_relogin_targets(args, Path(tmp))
        self.assertEqual([account["id"] for account in targets], [2])
        store.list_accounts.assert_called_once_with()

    def test_relogin_cli_accepts_an_account_id(self):
        try:
            args = register.parse_args(["--mode", "relogin", "--relogin-id", "42"])
        except SystemExit as exc:
            self.fail(f"--relogin-id must be accepted by the CLI (exit {exc.code})")
        self.assertEqual(args.relogin_id, [42])

    async def test_selected_relogin_updates_only_the_mysql_account_record(self):
        store_type = getattr(register, "AccountStore", None)
        self.assertTrue(callable(store_type), "register must use the canonical AccountStore")
        if not callable(store_type):
            return
        original_session = {"accessToken": "old-local", "expires": "2025-01-01T00:00:00Z"}
        original = {
            "id": 7, "email": "person@example.com", "password": "private-password",
            "authMode": "password", "session": original_session,
            "reloginRequired": True, "reloginReason": "expired locally",
        }
        saved = {}

        class StubStore:
            def get_account(self, account_id, *, include_password=False):
                self.requested = (account_id, include_password)
                return dict(original)

            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, account_data=dict(account_data))
                return {"id": 7, "email": email}

        store = StubStore()
        context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
        args = SimpleNamespace(
            no_clear_tokens=True, relogin_timeout=120, codex_oauth=True,
            no_codex_oauth=False, code_source="manual", proxy="",
            proxy_insecure=False, ac_check=False,
        )
        new_session = {"ok": True, "status": 200, "parsed": {"accessToken": "new-local", "expires": "2031-01-01T00:00:00Z"}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register.flow, "step_login_existing_account", AsyncMock(return_value="123456")), \
             patch.object(register.session, "fetch_session", AsyncMock(return_value=new_session)), \
             patch.object(register, "_build_5sim_phone_verifier", Mock(return_value=None)) as build, \
             patch.object(register.codex_oauth_module, "run_codex_oauth", AsyncMock(return_value={"access_token": "fresh"})):
            try:
                result = await register._run_one_relogin(args, context, 7, out_dir=Path(tmp), label="test")
            except Exception as exc:
                self.fail(f"selected-account re-login should use the repository ID, not a JSON path: {exc}")
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

        self.assertEqual(store.requested, (7, True))
        self.assertEqual(build.call_args.kwargs["account_id"], 7)
        self.assertIs(build.call_args.kwargs["account_store"], store)
        self.assertEqual(saved["email"], original["email"])
        self.assertEqual(saved["password"], "private-password")
        self.assertEqual(saved["account_data"]["session"]["accessToken"], "new-local")
        self.assertFalse(saved["account_data"]["reloginRequired"])
        self.assertEqual(result.account_id, 7)

    async def test_failed_relogin_preserves_old_session_and_marks_same_record(self):
        original_session = {"accessToken": "preserve-me", "expires": "2025-01-01T00:00:00Z"}
        original = {
            "id": 9, "email": "retry@example.com", "password": "private-password",
            "authMode": "password", "session": original_session,
            "reloginRequired": True,
        }
        saved = {}

        class StubStore:
            def get_account(self, account_id, *, include_password=False):
                return dict(original) if account_id == 9 and include_password else None

            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, account_data=dict(account_data))
                return {"id": 9, "email": email}

        store = StubStore()
        context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
        args = SimpleNamespace(
            no_clear_tokens=True, relogin_timeout=120, codex_oauth=False,
            no_codex_oauth=False, code_source="manual", proxy="",
            proxy_insecure=False, ac_check=False,
        )
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register.flow, "step_login_existing_account", AsyncMock(side_effect=RuntimeError("login denied"))):
            try:
                await register._run_one_relogin(args, context, 9, out_dir=Path("."), label="test")
            except Exception as exc:  # Compare the observed failure without masking a wrong exception type.
                self.assertIsInstance(exc, RuntimeError)
                self.assertEqual(str(exc), "login denied")
            else:
                self.fail("the simulated login failure must propagate")

        self.assertEqual(saved["account_data"]["session"], original_session)
        self.assertTrue(saved["account_data"]["reloginRequired"])
        self.assertEqual(saved["account_data"]["reloginLastError"], "login denied")

    async def test_registration_persists_snapshot_to_mysql_without_account_json(self):
        store_type = getattr(register, "AccountStore", None)
        self.assertTrue(callable(store_type), "register must use the canonical AccountStore")
        if not callable(store_type):
            return

        saved = {}

        class StubStore:
            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, account_data=account_data)
                return {"id": 42, "email": email}

        page = SimpleNamespace(close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        birthday = SimpleNamespace(iso=Mock(return_value="1990-01-01"))
        args = SimpleNamespace(
            no_clear_tokens=True,
            email_source="manual",
            email="person@example.com",
            auth_mode="otp",
            password="",
            headless=False,
            code_source="manual",
            codex_oauth=True,
            no_codex_oauth=False,
            ac_check=False,
        )
        session_result = {
            "ok": True,
            "status": 200,
            "text": '{"accessToken":"local"}',
            "parsed": {"accessToken": "local", "expires": "2030-01-01T00:00:00Z"},
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(register, "AccountStore", StubStore), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register.flow, "step1_open", AsyncMock()), \
             patch.object(register.flow, "step2_signup_email", AsyncMock()), \
             patch.object(register.flow, "step3_password", AsyncMock(return_value="private-password")), \
             patch.object(register.flow, "step4_code", AsyncMock(return_value="123456")), \
             patch.object(register.flow, "random_profile", Mock(return_value=("First", "Last", birthday))), \
             patch.object(register.flow, "step5_profile", AsyncMock()), \
             patch.object(register.flow, "step6_wait_success", AsyncMock()), \
             patch.object(register.session, "fetch_session", AsyncMock(return_value=session_result)), \
             patch.object(register, "_build_5sim_phone_verifier", Mock(return_value=None)) as build, \
             patch.object(register.codex_oauth_module, "run_codex_oauth", AsyncMock(return_value={"access_token": "fresh"})), \
             redirect_stdout(output):
            result = await register._run_one_account(
                args, context, out_dir=Path(tmp), label="test", nav_timeout_ms=1000
            )
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

        self.assertNotIn("private-password", output.getvalue())
        self.assertEqual(saved["email"], "person@example.com")
        self.assertEqual(saved["password"], "private-password")
        self.assertNotIn("password", saved["account_data"])
        self.assertEqual(saved["account_data"]["session"], session_result["parsed"])
        self.assertEqual(result.account_id, 42)
        self.assertEqual(build.call_args.kwargs["account_id"], 42)
        self.assertIsInstance(build.call_args.kwargs["account_store"], StubStore)

    async def test_a_deactivated_account_is_marked_banned_instead_of_queued_for_relogin(self):
        original = {
            "id": 9, "email": "banned@example.com", "password": "private-password",
            "authMode": "otp", "session": {"accessToken": "old-local"},
            "codexAuth": {"access_token": "old-codex", "refresh_token": "old-refresh"},
            "reloginRequired": True,
        }
        saved = {}

        class StubStore:
            def get_account(self, account_id, *, include_password=False):
                return dict(original) if int(account_id) == 9 and include_password else None

            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, data=dict(account_data))
                return {"id": 9, "email": email}

        store = StubStore()
        context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
        args = SimpleNamespace(
            no_clear_tokens=True, relogin_timeout=120, codex_oauth=False,
            no_codex_oauth=False, code_source="manual", proxy="",
            proxy_insecure=False, ac_check=False,
        )
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register.flow, "step_login_existing_account",
                          AsyncMock(side_effect=register.flow.AccountBannedError("账号已被 OpenAI 停用"))):
            with self.assertRaises(register.flow.AccountBannedError):
                await register._run_one_relogin(args, context, 9, out_dir=Path("."), label="test")

        self.assertTrue(saved["data"]["banned"])
        self.assertEqual(saved["data"]["banSource"], "login")
        self.assertIn("停用", saved["data"]["banReason"])
        self.assertFalse(saved["data"]["reloginRequired"])
        # credentials survive the ban mark
        self.assertEqual(saved["data"]["session"], original["session"])
        self.assertEqual(saved["data"]["codexAuth"]["refresh_token"], "old-refresh")


if __name__ == "__main__":
    unittest.main()
