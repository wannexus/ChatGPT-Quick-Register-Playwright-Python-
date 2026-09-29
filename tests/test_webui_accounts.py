from __future__ import annotations

import json as json_module
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from webui import server
from core import sub2api


def _record(**overrides):
    account = {
        "id": 7,
        "email": "person@example.com",
        "session": {"accessToken": "local-token", "expires": "2030-01-01T00:00:00Z"},
        "reloginRequired": False,
        "localValidity": {
            "status": "not_expired",
            "checkedAt": "2026-01-01T00:00:00+00:00",
            "expiresAt": "2030-01-01T00:00:00+00:00",
            "message": "仅依据本地 session 过期时间判断，不代表远端验证结果。",
        },
        "acCheck": {"eligible": True},
    }
    account.update(overrides)
    return account


class Sub2ApiGroupOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_settings_load_groups_without_exposing_the_key(self):
        cfg = {
            "s2BaseUrl": "https://sub2api.example.com",
            "s2AdminApiKey": "saved-secret",
            "s2AdminEmail": "",
            "s2AdminPassword": "",
        }
        with (
            patch.object(server, "effective_config", return_value=cfg),
            patch.object(sub2api, "auth_headers", return_value={"x-api-key": "saved-secret"}) as auth,
            patch.object(sub2api, "list_groups", return_value=[{"id": 4, "name": "codex", "secret": "x"}]) as groups,
        ):
            response = await server.sub2api_groups(server.Sub2ApiGroupsPayload())

        self.assertEqual(response, {"ok": True, "groups": [{"id": 4, "name": "codex"}]})
        auth.assert_called_once()
        groups.assert_called_once()
        self.assertEqual(auth.call_args.args[0].admin_api_key, "saved-secret")
        self.assertNotIn("saved-secret", json_module.dumps(response))

    async def test_typed_settings_override_saved_values(self):
        cfg = {
            "s2BaseUrl": "https://old.example.com",
            "s2AdminApiKey": "saved-secret",
            "s2AdminEmail": "old@example.com",
            "s2AdminPassword": "old-password",
        }
        payload = server.Sub2ApiGroupsPayload(
            baseUrl="https://new.example.com", adminApiKey="typed-key",
            adminEmail="new@example.com", adminPassword="new-password",
        )
        with (
            patch.object(server, "effective_config", return_value=cfg),
            patch.object(sub2api, "auth_headers", return_value={"x-api-key": "typed-key"}) as auth,
            patch.object(sub2api, "list_groups", return_value=[{"id": 9, "name": "plus"}]),
        ):
            response = await server.sub2api_groups(payload)

        self.assertTrue(response["ok"])
        target = auth.call_args.args[0]
        self.assertEqual(target.base_url, "https://new.example.com")
        self.assertEqual(target.admin_api_key, "typed-key")
        self.assertEqual(target.email, "new@example.com")
        self.assertEqual(target.password, "new-password")

    async def test_transient_credentials_are_redacted_from_upstream_errors(self):
        payload = server.Sub2ApiGroupsPayload(
            baseUrl="https://sub2api.example.com", adminApiKey="temporary-key",
        )
        with (
            patch.object(server, "effective_config", return_value={}),
            patch.object(sub2api, "auth_headers", side_effect=RuntimeError("rejected temporary-key")),
        ):
            response = await server.sub2api_groups(payload)

        self.assertFalse(response["ok"])
        self.assertNotIn("temporary-key", json_module.dumps(response))
        self.assertIn("***", response["error"])

    async def test_errors_are_actionable_and_never_echo_credentials(self):
        cfg = {"s2BaseUrl": "https://sub2api.example.com", "s2AdminApiKey": "saved-secret"}
        with (
            patch.object(server, "effective_config", return_value=cfg),
            patch.object(sub2api, "auth_headers", side_effect=RuntimeError("INVALID_ADMIN_KEY")),
        ):
            response = await server.sub2api_groups(server.Sub2ApiGroupsPayload())

        self.assertFalse(response["ok"])
        self.assertIn("INVALID_ADMIN_KEY", response["error"])
        self.assertNotIn("saved-secret", json_module.dumps(response))

    async def test_missing_credentials_fails_before_network_calls(self):
        cfg = {"s2BaseUrl": "https://sub2api.example.com"}
        with (
            patch.object(server, "effective_config", return_value=cfg),
            patch.object(sub2api, "auth_headers") as auth,
            patch.object(sub2api, "list_groups") as groups,
        ):
            response = await server.sub2api_groups(server.Sub2ApiGroupsPayload())

        self.assertFalse(response["ok"])
        self.assertIn("管理员", response["error"])
        auth.assert_not_called()
        groups.assert_not_called()

    async def test_missing_target_fails_before_network_calls(self):
        with (
            patch.object(server, "effective_config", return_value={}),
            patch.object(sub2api, "auth_headers") as auth,
            patch.object(sub2api, "list_groups") as groups,
        ):
            response = await server.sub2api_groups(server.Sub2ApiGroupsPayload())

        self.assertFalse(response["ok"])
        self.assertIn("服务地址", response["error"])
        auth.assert_not_called()
        groups.assert_not_called()


class AccountApiTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, accounts):
        store = Mock()
        store.list_accounts.return_value = list(accounts)
        store.get_account.side_effect = lambda account_id, **kwargs: next(
            (dict(a) for a in accounts if int(a["id"]) == int(account_id)), None
        )
        store.delete_account.return_value = True
        store.record_validity.return_value = _record()
        return store

    async def test_account_list_reads_mysql_records_with_local_validity(self):
        self.assertFalse(
            hasattr(server, "OUTPUT_DIR"),
            "the file-based account directory must be retired from the API owner",
        )
        store = self._store([_record()])
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(Path, "glob", side_effect=AssertionError("account list must not scan output/*.json")):
            rows = await server.list_accounts()

        self.assertEqual([row["id"] for row in rows], [7])
        self.assertEqual(rows[0]["localValidity"]["status"], "not_expired")
        self.assertNotIn("filename", rows[0])
        store.list_accounts.assert_called_once_with()

    async def test_account_detail_accepts_an_id_and_never_returns_password(self):
        record = _record(password="must-not-leak")
        store = self._store([record])
        with patch.object(server, "AccountStore", return_value=store):
            detail = await server.get_account_detail(7)
            missing = await server.get_account_detail(404)

        self.assertEqual(detail["id"], 7)
        self.assertNotIn("password", detail)
        self.assertIn(7, [call.args[0] for call in store.get_account.call_args_list])
        self.assertEqual(missing.status_code, 404)

    async def test_account_delete_and_cleanup_use_repository_rows(self):
        tokenless = _record(id=8, email="empty@example.com", session={}, localValidity={"status": "missing_session"})
        healthy = _record(id=7)
        store = self._store([tokenless, healthy])
        with patch.object(server, "AccountStore", return_value=store):
            deleted = await server.delete_account(8)
            cleaned = await server.cleanup_empty()

        self.assertTrue(deleted["ok"])
        store.delete_account.assert_any_call(8)
        self.assertEqual(cleaned["removed"], [8])
        self.assertNotIn(call(7), store.delete_account.call_args_list)

    async def test_validity_check_is_local_only_and_persists_the_result(self):
        store = self._store([_record()])
        payload = server.AccountValidityPayload(accountIds=[7])
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.ac_checker, "check_token") as remote:
            result = await server.check_account_validity(payload)

        remote.assert_not_called()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["id"], 7)
        account_id, validity = store.record_validity.call_args[0]
        self.assertEqual(account_id, 7)
        self.assertEqual(validity["status"], "not_expired")
        self.assertTrue(result["localOnly"])

    async def test_validity_check_defaults_to_every_account(self):
        store = self._store([_record(id=7), _record(id=8, email="second@example.com")])
        with patch.object(server, "AccountStore", return_value=store):
            result = await server.check_account_validity(server.AccountValidityPayload())

        self.assertEqual(result["count"], 2)
        self.assertEqual({call.args[0] for call in store.record_validity.call_args_list}, {7, 8})

    async def test_selected_relogin_passes_the_mysql_account_id(self):
        store = self._store([_record()])
        start = AsyncMock(return_value=(True, "started"))
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.manager, "start", start):
            response = await server.relogin_account(7, server.ReloginPayload(codeSource="qq", reloginLimit=1))

        self.assertEqual(response["ok"], True)
        args = start.call_args[0][0]
        self.assertIn("--relogin-id", args)
        self.assertEqual(args[args.index("--relogin-id") + 1], "7")
        self.assertNotIn("--relogin-marked", args)

    async def test_ac_check_by_account_id_persists_on_the_same_record(self):
        record = _record(password="private-password")
        store = Mock()
        store.get_account.return_value = record
        store.save_account.return_value = _record()
        options = {"base_url": "https://ac.example", "promo_id": "promo", "proxy": "", "proxy_insecure": False}
        payload = server.AcCheckPayload(accountId=7)
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server, "_ac_effective_options", return_value=options), \
             patch.object(server.ac_checker, "extract_account_token", return_value={"token": "t", "tokenSource": "session", "accountEmail": "person@example.com"}), \
             patch.object(server.ac_checker, "check_token", return_value={"eligible": True}), \
             patch.object(server.ac_checker, "normalize_base_url", return_value="https://ac.example"), \
             patch.object(server.ac_checker, "resolve_promo_id", return_value="promo"):
            result = server._run_ac_single(payload)

        self.assertEqual(result["accountId"], 7)
        email, password, saved = store.save_account.call_args[0]
        self.assertEqual(email, "person@example.com")
        self.assertEqual(password, "private-password")
        self.assertTrue(saved["acCheck"]["eligible"])
        self.assertNotIn("password", saved)

    async def test_ac_batch_all_accounts_reads_the_repository(self):
        store = Mock()
        store.list_accounts.return_value = [_record()]
        store.get_account.return_value = _record(password="private-password")
        store.save_account.return_value = _record()
        options = {"base_url": "https://ac.example", "promo_id": "promo", "proxy": "", "proxy_insecure": False}
        payload = server.AcBatchCheckPayload(allAccounts=True)
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server, "_ac_effective_options", return_value=options), \
             patch.object(Path, "glob", side_effect=AssertionError("AC batch must not scan output/*.json")), \
             patch.object(server.ac_checker, "extract_account_token", return_value={"token": "t", "tokenSource": "session", "accountEmail": "person@example.com"}), \
             patch.object(server.ac_checker, "check_tokens", return_value={"results": [{"eligible": True}], "promo_id": "promo"}), \
             patch.object(server.ac_checker, "normalize_base_url", return_value="https://ac.example"), \
             patch.object(server.ac_checker, "resolve_promo_id", return_value="promo"):
            result = server._run_ac_batch(payload)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["accountId"], 7)
        store.list_accounts.assert_called_once_with()
        store.save_account.assert_called_once()

    async def test_pay153_result_is_written_to_the_mysql_record(self):
        record = _record(password="private-password")
        store = Mock()
        store.save_account.return_value = _record()
        context = {
            "store": store,
            "account": record,
            "base_url": "https://pay.153.ink",
            "job_id": "job-1",
            "plan": "plus",
            "link_type": "hosted",
            "account_email": "person@example.com",
        }
        server._save_pay153_result(context, {"status": "completed", "result": {"ok": True}})

        email, _password, saved = store.save_account.call_args[0]
        self.assertEqual(email, "person@example.com")
        self.assertEqual(saved["payurlCheck"]["status"], "completed")
        self.assertEqual(saved["payurlCheck"]["jobId"], "job-1")
        self.assertNotIn("password", saved)

    async def test_pay153_checkout_requires_third_party_confirmation_and_a_real_account(self):
        unconfirmed = await server.pay153_checkout(server.Pay153CheckoutPayload(accountId=7))
        self.assertEqual(unconfirmed.status_code, 400)

        store = Mock()
        store.get_account.return_value = None
        payload = server.Pay153CheckoutPayload(accountId=404, acceptedThirdParty=True)
        with patch.object(server, "AccountStore", return_value=store):
            response = await server.pay153_checkout(payload)
        self.assertEqual(response.status_code, 404)

    async def test_summary_exposes_the_ban_mark_without_credentials(self):
        record = _record(
            id=9, email="banned@example.com", password="account-pw",
            banned=True, banReason="Your account has been deactivated",
            banSource="email", banDetectedAt="2026-01-05T00:00:00+00:00",
        )
        summary = server._account_summary(record)

        self.assertTrue(summary["banned"])
        self.assertEqual(summary["banReason"], "Your account has been deactivated")
        self.assertEqual(summary["banSource"], "email")
        self.assertNotIn("account-pw", str(summary))

    async def test_account_without_a_ban_mark_is_not_banned(self):
        summary = server._account_summary(_record(id=7))

        self.assertFalse(summary["banned"])

    async def test_ban_scan_starts_the_scan_mode_for_the_selected_accounts(self):
        store = self._store([_record(id=7), _record(id=8, email="second@example.com")])
        start = AsyncMock(return_value=(True, "started"))
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.manager, "start", start):
            response = await server.ban_scan(server.BanScanPayload(accountIds=[8], sinceDays=14))

        self.assertEqual(response["ok"], True)
        self.assertEqual(response["count"], 1)
        args = start.call_args[0][0]
        self.assertEqual(args[args.index("--mode") + 1], "ban-scan")
        self.assertEqual(args[args.index("--ban-since-days") + 1], "14")
        ids = [args[index + 1] for index, value in enumerate(args) if value == "--ban-id"]
        self.assertEqual(ids, ["8"])

    async def test_ban_scan_without_a_selection_covers_every_account(self):
        store = self._store([_record(id=7), _record(id=8, email="second@example.com")])
        start = AsyncMock(return_value=(True, "started"))
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.manager, "start", start):
            response = await server.ban_scan(server.BanScanPayload())

        self.assertEqual(response["count"], 2)
        self.assertNotIn("--ban-id", start.call_args[0][0])

    async def test_ban_scan_refuses_to_start_while_another_task_runs(self):
        store = self._store([_record(id=7)])
        busy = AsyncMock(return_value=(False, "已有任务在运行"))
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.manager, "start", busy):
            response = await server.ban_scan(server.BanScanPayload())

        self.assertEqual(response.status_code, 409)

    async def test_ban_scan_status_reports_marked_accounts(self):
        lines = [
            "[ban-scan] 共读取 3 封邮件",
            '[ban-scan-result] ' + json_module.dumps(
                {"id": 9, "email": "banned@example.com", "banned": True,
                 "reason": "Your account has been deactivated", "subject": "Account deactivated"}),
        ]
        with patch.object(server.manager, "recent", lines):
            status = await server.ban_scan_status()

        self.assertEqual(status["marked"], 1)
        self.assertEqual(status["results"][0]["id"], 9)
        self.assertTrue(status["results"][0]["banned"])

    async def test_retired_account_features_are_gone_from_the_api_owner(self):
        for name in ("relogin_marked", "delete_relogin_required", "_count_relogin_required",
                     "sub2api_push", "sub2api_preview", "sub2api_export", "_build_export",
                     "Sub2ApiCommon", "Sub2ApiPushPayload"):
            self.assertFalse(hasattr(server, name), f"{name} must be retired")
        retired = ("/api/accounts/relogin", "/api/accounts/delete-relogin-required",
                   "/api/sub2api/preview", "/api/sub2api/export", "/api/sub2api/push")
        live = {getattr(route, "path", "") for route in server.app.routes}
        for path in retired:
            self.assertNotIn(path, live, f"{path} must not be routed any more")
        self.assertIn("/api/accounts/codex-push", live)
        self.assertIn("/api/accounts/codex-push/status", live)
        self.assertIn("/api/accounts/{account_id}/relogin", live)

    async def _codex_push(self, payload, *, store=None, start=None, saved_cfg=None, recent=None):
        store = store or self._store([_record(id=7), _record(id=8, email="second@example.com")])
        start = start or AsyncMock(return_value=(True, "started"))
        cfg = {
            "s2BaseUrl": "https://sub2api.example.com",
            "s2AdminApiKey": "stored-admin-key",
            "s2AdminEmail": "",
            "s2AdminPassword": "",
            "s2GroupName": "codex",
            "s2Concurrency": "5",
            "s2Priority": "1",
            "s2RateMultiplier": "1",
            "s2PrivacyMode": "training_off",
        }
        cfg.update(saved_cfg or {})
        manager = server.manager
        recent_patch = patch.object(server.manager, "recent", recent or [])
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server, "effective_config", return_value=cfg), \
             patch.object(server, "save_config") as save_config, \
             patch.object(server.manager, "start", start), \
             recent_patch:
            response = await server.codex_push(payload)
        return response, start, save_config

    async def test_codex_push_requires_an_explicit_selection(self):
        response, start, _ = await self._codex_push(server.CodexPushPayload(accountIds=[], codeSource="qq"))
        self.assertEqual(response.status_code, 400)
        start.assert_not_called()

    async def test_codex_push_rejects_a_manual_code_source(self):
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[7], codeSource="manual")
        )
        self.assertEqual(response.status_code, 400)
        start.assert_not_called()

    async def test_codex_push_refuses_to_start_while_another_task_runs(self):
        busy = AsyncMock(return_value=(False, "已有任务在运行"))
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[7], codeSource="qq"), start=busy
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json_module.loads(response.body)["error"], "已有任务在运行")
        self.assertIn("--mode", start.call_args[0][0])

    async def test_codex_push_rejects_an_unknown_account_id(self):
        store = self._store([_record(id=7)])
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[404], codeSource="qq"), store=store
        )

        self.assertEqual(response.status_code, 404)
        start.assert_not_called()

    async def test_codex_push_passes_only_the_selected_ids_and_never_echoes_the_key(self):
        payload = server.CodexPushPayload(accountIds=[8, 8], codeSource="qq", s2AdminApiKey="typed-key")
        response, start, _ = await self._codex_push(payload)

        self.assertEqual(response["ok"], True)
        self.assertEqual(response["count"], 1)
        args = start.call_args[0][0]
        self.assertEqual(args[args.index("--mode") + 1], "codex-push")
        ids = [args[index + 1] for index, value in enumerate(args) if value == "--push-id"]
        self.assertEqual(ids, ["8"], "only the ticked account may be pushed")
        self.assertIn("--s2-admin-api-key", args)
        self.assertIn("typed-key", args)  # the child process legitimately needs it
        self.assertNotIn("typed-key", str(response))
        self.assertNotIn("stored-admin-key", str(response))

    async def test_codex_push_redacts_the_admin_key_from_the_echoed_command(self):
        payload = server.CodexPushPayload(accountIds=[7], codeSource="qq", s2AdminApiKey="typed-key")
        response, _, _ = await self._codex_push(payload)

        self.assertIn("--s2-admin-api-key", response["args"])
        self.assertIn("***", response["args"])
        self.assertNotIn("typed-key", " ".join(response["args"]))

    async def test_codex_push_requires_a_configured_target(self):
        empty = {"s2BaseUrl": "", "s2AdminApiKey": "", "s2AdminEmail": "", "s2AdminPassword": ""}
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[7], codeSource="qq"), saved_cfg=empty
        )
        self.assertEqual(response.status_code, 400)
        start.assert_not_called()

    async def test_codex_push_requires_a_credential_even_with_a_base_url(self):
        no_key = {"s2AdminApiKey": "", "s2AdminEmail": "", "s2AdminPassword": ""}
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[7], codeSource="qq"), saved_cfg=no_key
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("API Key", json_module.loads(response.body)["error"])
        start.assert_not_called()

    async def test_codex_push_accepts_the_jwt_fallback_when_no_key_exists(self):
        no_key = {"s2AdminApiKey": "", "s2AdminEmail": "admin@example.com", "s2AdminPassword": "s2-secret"}
        response, start, _ = await self._codex_push(
            server.CodexPushPayload(accountIds=[7], codeSource="qq"), saved_cfg=no_key
        )

        self.assertEqual(response["ok"], True)
        args = start.call_args[0][0]
        self.assertNotIn("--s2-admin-api-key", args)

    async def test_codex_push_saves_the_admin_key_only_when_asked(self):
        payload = server.CodexPushPayload(
            accountIds=[7], codeSource="qq", s2BaseUrl="https://new.example.com",
            s2AdminApiKey="new-key", saveTarget=True,
        )
        _, start, save_config = await self._codex_push(payload)
        self.assertEqual(save_config.call_args[0][0]["s2BaseUrl"], "https://new.example.com")
        self.assertEqual(save_config.call_args[0][0]["s2AdminApiKey"], "new-key")
        # the persisted secret is read from `.env` by the child, so it stays out of argv
        args = start.call_args[0][0]
        self.assertNotIn("--s2-admin-api-key", args)
        self.assertNotIn("new-key", " ".join(args))

    async def test_codex_push_status_reports_truthful_per_account_results(self):
        import json as json_module
        lines = [
            "[codex-push-result] " + json_module.dumps({"id": 7, "email": "a@example.com", "login": True,
                                                        "oauth": True, "saved": True, "pushed": True, "error": ""}),
            "[codex-push-result] " + json_module.dumps({"id": 8, "email": "b@example.com", "login": True,
                                                        "oauth": False, "saved": False, "pushed": False,
                                                        "error": "oauth failed"}),
            "noise that must be ignored",
        ]
        with patch.object(server.manager, "recent", lines):
            status = await server.codex_push_status()

        self.assertEqual(status["pushed"], 1)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(status["done"], 2)
        by_id = {row["id"]: row for row in status["results"]}
        self.assertFalse(by_id[8]["saved"], "a failed OAuth must never look saved")

    async def test_detail_never_returns_the_mailbox_password(self):
        record = _record(password="account-pw", emailPassword="mailbox-pw")
        store = self._store([record])
        with patch.object(server, "AccountStore", return_value=store):
            detail = await server.get_account_detail(7)

        self.assertNotIn("password", detail)
        self.assertNotIn("emailPassword", detail)
        self.assertNotIn("mailbox-pw", str(detail))

    async def test_account_summary_exposes_only_masked_codex_phone(self):
        summary = server._account_summary(_record(codexPhoneNumber="+15551234567"))
        self.assertEqual(summary["codexPhoneNumberMasked"], "••••4567")
        self.assertNotIn("codexPhoneNumber", summary)
        self.assertNotIn("+15551234567", str(summary))

    async def test_account_detail_masks_bound_phone(self):
        store = self._store([_record(codexPhoneNumber="+15551234567")])
        with patch.object(server, "AccountStore", return_value=store):
            detail = await server.get_account_detail(7)
        self.assertNotIn("codexPhoneNumber", detail)
        self.assertEqual(detail["codexPhoneNumberMasked"], "••••4567")

    async def test_summary_omits_all_stored_credentials(self):
        record = _record(password="account-pw", emailPassword="mailbox-pw")
        summary = server._account_summary(record)
        self.assertNotIn("account-pw", str(summary))
        self.assertNotIn("mailbox-pw", str(summary))

    async def test_relogin_accepts_the_mhjc_code_source(self):
        store = self._store([_record()])
        start = AsyncMock(return_value=(True, "started"))
        with patch.object(server, "AccountStore", return_value=store), \
             patch.object(server.manager, "start", start):
            response = await server.relogin_account(
                7, server.ReloginPayload(codeSource="mhjc", reloginLimit=1)
            )

        self.assertEqual(response["ok"], True)
        args = start.call_args[0][0]
        self.assertEqual(args[args.index("--code-source") + 1], "mhjc")

    async def test_relogin_rejects_a_manual_code_source(self):
        store = self._store([_record()])
        with patch.object(server, "AccountStore", return_value=store):
            response = await server.relogin_account(
                7, server.ReloginPayload(codeSource="manual", reloginLimit=1)
            )

        self.assertEqual(response.status_code, 400)

    async def test_batch_args_redact_every_credential_flag(self):
        args = [
            "--email-source", "mhjc",
            "--mhjc-api-key", "mhjc-secret",
            "--proxy", "http://alice:s3cret@proxy.example.com:8080",
            "--proxy-pass", "proxy-secret",
            "--qq-pass", "qq-secret",
            "--password", "account-secret",
            "--s2-admin-api-key", "sub2api-secret",
            "--s2-admin-password", "sub2api-password",
        ]
        redacted = server._redact_args(args)
        joined = " ".join(redacted)
        for secret in ("mhjc-secret", "s3cret", "proxy-secret", "qq-secret", "account-secret",
                       "sub2api-secret", "sub2api-password"):
            self.assertNotIn(secret, joined)
        self.assertEqual(redacted.count("***"), 7)
        # the flags themselves stay visible so the UI still explains the command
        for flag in ("--mhjc-api-key", "--proxy", "--proxy-pass", "--qq-pass", "--password",
                     "--s2-admin-api-key", "--s2-admin-password"):
            self.assertIn(flag, redacted)

    async def test_defaults_expose_proxy_parts_without_the_password(self):
        cfg = {
            "proxyScheme": "http", "proxyHost": "proxy.example.com", "proxyPort": "8080",
            "proxyUser": "alice", "proxyPassword": "s3cret", "proxy": "http://alice:s3cret@proxy.example.com:8080",
            "proxyInsecure": "1",
        }
        with patch.object(server, "effective_config", return_value=cfg):
            payload = await server.defaults()

        self.assertEqual(payload["proxyHost"], "proxy.example.com")
        self.assertEqual(payload["proxyPort"], "8080")
        self.assertEqual(payload["proxyUser"], "alice")
        # the editable value must never carry credentials nor a redaction marker,
        # otherwise saving the form would persist the literal "***" as the password
        self.assertEqual(payload["proxy"], "")
        self.assertEqual(payload["proxyDisplay"], "http://alice:***@proxy.example.com:8080")
        self.assertTrue(payload["proxyPasswordSet"])
        self.assertNotIn("s3cret", str(payload))
        self.assertNotIn("***", str(payload["proxy"]))

    async def test_defaults_expose_5sim_options_but_not_the_api_key(self):
        cfg = {
            "fiveSimApiKey": "fivesim-secret",
            "fiveSimCountry": "vietnam",
            "fiveSimOperator": "any",
            "fiveSimProduct": "openai",
            "fiveSimMaxPrice": "0.5",
            "fiveSimAcquirePriority": "price",
            "fiveSimCandidateLimit": "4",
        }
        with patch.object(server, "effective_config", return_value=cfg):
            payload = await server.defaults()

        self.assertTrue(payload["fiveSimApiKeyPresent"])
        self.assertNotIn("fiveSimApiKey", payload)
        self.assertNotIn("fivesim-secret", str(payload))
        self.assertEqual(payload["fiveSimCountry"], "vietnam")
        self.assertEqual(payload["fiveSimAcquirePriority"], "price")

    async def test_saving_5sim_defaults_keeps_blank_api_key_and_saves_options(self):
        saved = {"fiveSimApiKey": "stored-secret", "fiveSimCountry": "vietnam"}
        def save(values):
            self.assertNotIn("fiveSimApiKey", values)
            self.assertEqual(values["fiveSimCountry"], "vietnam")
            return saved

        payload = server.LocalConfigPayload(fiveSimApiKey="", fiveSimCountry="vietnam")
        with patch.object(server, "save_config", side_effect=save):
            result = await server.update_defaults(payload)

        self.assertTrue(result["fiveSimApiKeyPresent"])
        self.assertNotIn("stored-secret", str(result))

    async def test_fivesim_profile_uses_saved_key_when_request_key_is_blank(self):
        with (
            patch.object(server, "effective_config", return_value={"fiveSimApiKey": "stored-fivesim-secret"}),
            patch.object(server.num5sim, "get_profile", return_value={"balance": 3}) as get_profile,
        ):
            result = await server.five_sim_profile(server.FiveSimApiKeyPayload(apiKey=""))

        self.assertTrue(result["ok"])
        self.assertEqual(get_profile.call_args.kwargs["api_key"], "stored-fivesim-secret")
        self.assertNotIn("stored-fivesim-secret", str(result))

    async def test_defaults_expose_sub2api_targets_without_the_secrets(self):
        cfg = {
            "s2BaseUrl": "https://sub2api.example.com",
            "s2AdminApiKey": "admin-key-secret",
            "s2AdminEmail": "admin@example.com",
            "s2AdminPassword": "s2-secret",
            "s2GroupName": "codex",
        }
        with patch.object(server, "effective_config", return_value=cfg):
            payload = await server.defaults()

        self.assertEqual(payload["s2BaseUrl"], "https://sub2api.example.com")
        self.assertEqual(payload["s2AdminEmail"], "admin@example.com")
        self.assertTrue(payload["s2AdminApiKeySet"])
        self.assertTrue(payload["s2AdminPasswordSet"])
        self.assertNotIn("s2AdminApiKey", payload)
        self.assertNotIn("s2AdminPassword", payload)
        self.assertNotIn("admin-key-secret", str(payload))
        self.assertNotIn("s2-secret", str(payload))

    async def test_saving_defaults_reports_the_key_without_returning_it(self):
        cfg = {"s2AdminApiKey": "admin-key-secret", "s2AdminPassword": "s2-secret"}
        with patch.object(server, "save_config", return_value=cfg):
            payload = await server.update_defaults(server.LocalConfigPayload())

        self.assertTrue(payload["s2AdminApiKeySet"])
        self.assertNotIn("admin-key-secret", str(payload))


if __name__ == "__main__":
    unittest.main()
