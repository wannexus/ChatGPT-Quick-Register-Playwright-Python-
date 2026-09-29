from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import register
from core import sub2api


def _args(**overrides):
    args = SimpleNamespace(
        no_clear_tokens=True,
        relogin_timeout=120,
        code_source="qq",
        codex_oauth=True,
        no_codex_oauth=False,
        proxy="",
        proxy_insecure=False,
        ac_check=False,
        qq_user="user@qq.com",
        qq_pass="imap-secret",
        push_id=[7],
        s2_base_url="https://sub2api.example.com",
        s2_admin_api_key="admin-key-123",
        s2_admin_email="",
        s2_admin_password="",
        s2_group_name="codex",
        s2_concurrency=5,
        s2_priority=1,
        s2_rate_multiplier=1,
        s2_privacy_mode="training_off",
        s2_dry_run=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _account(**overrides):
    account = {
        "id": 7,
        "email": "person@example.com",
        "password": "private-password",
        "authMode": "password",
        "emailSource": "duck-api",
        "session": {"accessToken": "old-local", "expires": "2025-01-01T00:00:00Z"},
        "codexAuth": {"access_token": "old-codex", "refresh_token": "old-refresh"},
        "reloginRequired": False,
    }
    account.update(overrides)
    return account


class StubStore:
    def __init__(self, account):
        self.account = account
        self.saves: list[dict] = []
        self.requested: list[tuple] = []

    def get_account(self, account_id, *, include_password=False):
        self.requested.append((account_id, include_password))
        return dict(self.account) if int(account_id) == int(self.account["id"]) else None

    def save_account(self, email, password, account_data):
        self.saves.append({"email": email, "password": password, "data": dict(account_data)})
        return {"id": self.account["id"], "email": email}


def _context():
    page = SimpleNamespace(close=AsyncMock())
    return SimpleNamespace(new_page=AsyncMock(return_value=page))


NEW_SESSION = {
    "ok": True,
    "status": 200,
    "parsed": {"accessToken": "new-local", "expires": "2031-01-01T00:00:00Z"},
}
FRESH_CODEX = {"access_token": "fresh-codex", "refresh_token": "fresh-refresh", "email": "person@example.com"}


class CodexPushSelectionTests(unittest.TestCase):
    def test_selection_requires_explicit_ids(self):
        store = Mock()
        store.list_accounts.return_value = [_account()]
        with patch.object(register, "AccountStore", return_value=store):
            with self.assertRaises(SystemExit):
                register._load_codex_push_targets(_args(push_id=[]))

    def test_selection_is_deduplicated_and_ordered(self):
        store = Mock()
        store.list_accounts.return_value = [_account(id=7), _account(id=9, email="second@example.com")]
        with patch.object(register, "AccountStore", return_value=store):
            targets = register._load_codex_push_targets(_args(push_id=[9, 7, 9]))

        self.assertEqual([target["id"] for target in targets], [9, 7])

    def test_admin_api_key_alone_is_a_valid_target(self):
        target = register._build_push_target(_args())

        self.assertEqual(target.admin_api_key, "admin-key-123")
        self.assertEqual(sub2api.auth_headers(target), {"x-api-key": "admin-key-123"})

    def test_target_without_a_base_url_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            register._build_push_target(_args(s2_base_url=""))
        self.assertIn("base URL", str(ctx.exception))

    def test_target_without_any_credential_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            register._build_push_target(_args(s2_admin_api_key=""))
        self.assertIn("API Key", str(ctx.exception))

    def test_email_and_password_remain_an_optional_fallback(self):
        target = register._build_push_target(_args(
            s2_admin_api_key="", s2_admin_email="admin@example.com", s2_admin_password="s2-secret",
        ))

        self.assertEqual(target.admin_api_key, "")
        self.assertEqual(target.email, "admin@example.com")

    def test_half_a_fallback_pair_is_not_enough(self):
        for overrides in ({"s2_admin_email": "admin@example.com"}, {"s2_admin_password": "s2-secret"}):
            with self.assertRaises(SystemExit):
                register._build_push_target(_args(s2_admin_api_key="", **overrides))


class CodexPushFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, store, oauth=AsyncMock(return_value=FRESH_CODEX), push=None, args=None):
        args = args or _args()
        push = push or Mock(return_value=sub2api.PushResult(pushed=[{"email": "person@example.com"}], failed=[], skipped=[]))
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register.flow, "clear_openai_state", AsyncMock()), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register, "flow", register.flow), \
             patch.object(register.flow, "step_login_existing_account", AsyncMock(return_value="123456")), \
             patch.object(register.session, "fetch_session", AsyncMock(return_value=NEW_SESSION)), \
             patch.object(register.codex_oauth_module, "run_codex_oauth", oauth), \
             patch.object(register.sub2api, "push_accounts", push):
            result = await register._run_one_codex_push(args, _context(), 7, label="test")
        return result, push

    async def test_fresh_credentials_are_saved_before_the_push(self):
        store = StubStore(_account())
        result, push = await self._run(store=store)

        self.assertTrue(result["saved"] and result["pushed"])
        self.assertEqual(len(store.saves), 1)
        saved = store.saves[0]["data"]
        self.assertEqual(saved["codexAuth"]["refresh_token"], "fresh-refresh")
        self.assertEqual(saved["session"]["accessToken"], "new-local")
        self.assertFalse(saved["reloginRequired"])
        self.assertNotIn("password", saved)

        pushed_accounts = push.call_args[0][0]
        self.assertEqual(len(pushed_accounts), 1, "exactly the selected account may be pushed")
        self.assertEqual(pushed_accounts[0]["codexAuth"]["access_token"], "fresh-codex")
        self.assertTrue(push.call_args.kwargs["require_codex"])

    async def test_5sim_verifier_receives_selected_mysql_account_store_context(self):
        store = StubStore(_account())
        with patch.object(register, "_build_5sim_phone_verifier", return_value=object()) as build:
            await self._run(store=store)
        self.assertEqual(build.call_args.kwargs["account_id"], 7)
        self.assertIs(build.call_args.kwargs["account_store"], store)

    async def test_failed_oauth_never_saves_new_state_and_never_pushes(self):
        store = StubStore(_account())
        oauth = AsyncMock(side_effect=RuntimeError("oauth denied"))
        result, push = await self._run(store=store, oauth=oauth)

        self.assertFalse(result["oauth"])
        self.assertFalse(result["pushed"])
        self.assertEqual(result["error"], "oauth denied")
        push.assert_not_called()
        # The stored codexAuth stays the old one, and the login itself still
        # worked, so the account must not be re-marked as needing a re-login.
        self.assertEqual(len(store.saves), 1)
        marked = store.saves[0]["data"]
        self.assertFalse(marked.get("reloginRequired", False))
        self.assertEqual(marked["codexAuth"]["access_token"], "old-codex")
        self.assertEqual(marked["codexAuth"]["refresh_token"], "old-refresh")
        self.assertEqual(marked["session"], _account()["session"])
        self.assertEqual(marked["codexPushLastError"], "oauth denied")

    async def test_oauth_without_an_access_token_is_treated_as_a_failure(self):
        store = StubStore(_account())
        oauth = AsyncMock(return_value={"refresh_token": "only-refresh"})
        result, push = await self._run(store=store, oauth=oauth)

        self.assertFalse(result["oauth"])
        self.assertFalse(result["saved"])
        push.assert_not_called()
        stored = store.saves[0]["data"]
        self.assertEqual(stored["codexAuth"]["access_token"], "old-codex",
                         "the incomplete OAuth result must never replace stored credentials")
        self.assertNotEqual(stored["codexAuth"]["refresh_token"], "only-refresh")

    async def test_push_failure_keeps_the_freshly_saved_credentials(self):
        store = StubStore(_account())
        push = Mock(return_value=sub2api.PushResult(
            pushed=[], failed=[{"email": "person@example.com", "error": "SUB2API 500"}], skipped=[],
        ))
        result, _ = await self._run(store=store, push=push)

        self.assertTrue(result["saved"])
        self.assertFalse(result["pushed"])
        self.assertEqual(result["error"], "SUB2API 500")
        self.assertEqual(store.saves[-1]["data"]["codexAuth"]["refresh_token"], "fresh-refresh")
        self.assertFalse(store.saves[-1]["data"].get("reloginRequired", False),
                         "a push failure is not a login failure")

    async def test_a_banned_account_is_never_logged_in_or_pushed(self):
        store = StubStore(_account(banned=True, banReason="Your account has been deactivated"))
        result, push = await self._run(store=store)

        self.assertFalse(result["login"])
        self.assertFalse(result["saved"])
        self.assertFalse(result["pushed"])
        self.assertIn("封禁", result["error"])
        push.assert_not_called()
        self.assertEqual(store.saves, [], "a banned account must not be rewritten by the push flow")

    async def test_skipped_push_is_reported_as_a_failure(self):
        store = StubStore(_account())
        push = Mock(return_value=sub2api.PushResult(
            pushed=[], failed=[], skipped=[{"email": "person@example.com", "reason": "缺 refresh_token"}],
        ))
        result, _ = await self._run(store=store, push=push)

        self.assertFalse(result["pushed"])
        self.assertEqual(result["error"], "缺 refresh_token")

    async def test_login_failure_keeps_the_old_record_intact(self):
        store = StubStore(_account())
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register.flow, "clear_openai_state", AsyncMock()), \
             patch.object(register, "make_code_fetcher", Mock(return_value=object())), \
             patch.object(register.flow, "step_login_existing_account", AsyncMock(side_effect=RuntimeError("denied"))), \
             patch.object(register.sub2api, "push_accounts") as push:
            result = await register._run_one_codex_push(_args(), _context(), 7, label="test")

        self.assertFalse(result["login"])
        self.assertFalse(result["pushed"])
        push.assert_not_called()
        self.assertTrue(store.saves[0]["data"]["reloginRequired"])
        self.assertEqual(store.saves[0]["data"]["session"], _account()["session"])

    async def test_mhjc_accounts_reuse_their_stored_mailbox_password(self):
        store = StubStore(_account(id=7, emailSource="mhjc", emailPassword="mailbox-secret"))
        args = _args(code_source="mhjc")
        result, _ = await self._run(store=store, args=args)

        self.assertTrue(result["pushed"])
        self.assertEqual(args.mhjc_email, "person@example.com")
        self.assertEqual(args.mhjc_password, "mailbox-secret")

    async def test_result_line_is_machine_readable_and_carries_no_secrets(self):
        store = StubStore(_account())
        result, _ = await self._run(store=store)
        line = register._codex_push_result_line(result)

        self.assertTrue(line.startswith("[codex-push-result] "))
        payload = json.loads(line[len("[codex-push-result] "):])
        self.assertEqual(payload["id"], 7)
        self.assertTrue(payload["pushed"])
        for secret in ("private-password", "fresh-codex", "fresh-refresh", "s2-secret", "imap-secret"):
            self.assertNotIn(secret, line)

    async def test_failed_account_is_not_retried_and_the_batch_continues(self):
        targets = [_account(id=7), _account(id=9, email="next@example.com")]
        run_one = AsyncMock(side_effect=[
            {"id": 7, "email": targets[0]["email"], "pushed": False, "error": "SMS failed"},
            {"id": 9, "email": targets[1]["email"], "pushed": True, "error": ""},
        ])
        with patch.object(register, "_load_codex_push_targets", return_value=targets), \
             patch.object(register, "_open_browser_context", AsyncMock(return_value=(None, None, None, None))), \
             patch.object(register, "_close_browser_context", AsyncMock()), \
             patch.object(register, "_proxy_banner", return_value="fixture proxy"), \
             patch.object(register, "_run_one_codex_push", run_one):
            await register.run_codex_push(_args(nav_timeout=60, cooldown=0))
        self.assertEqual([call.args[2] for call in run_one.await_args_list], [7, 9])


if __name__ == "__main__":
    unittest.main()
