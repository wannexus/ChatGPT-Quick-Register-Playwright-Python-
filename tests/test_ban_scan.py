from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import register
from core import ban_check


def _args(**overrides):
    args = SimpleNamespace(
        ban_id=[],
        ban_since_days=30,
        qq_user="owner@qq.com",
        qq_pass="imap-secret",
        qq_host="imap.qq.com",
        qq_port=993,
        proxy="",
        proxy_insecure=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _account(account_id: int, email: str, **overrides):
    account = {
        "id": account_id,
        "email": email,
        "emailSource": "qq",
        "session": {"accessToken": f"token-{account_id}"},
        "codexAuth": {"access_token": f"codex-{account_id}", "refresh_token": f"refresh-{account_id}"},
        "reloginRequired": True,
        "authMode": "otp",
    }
    account.update(overrides)
    return account


def _notice(subject: str, to: str, body: str, *, date: str = "Mon, 05 Jan 2026 10:00:00 +0800"):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = to
    msg["Date"] = date
    msg["From"] = "OpenAI <noreply@openai.com>"
    msg.set_content(body)
    return ban_check.message_to_record(msg.as_bytes())


class BanScanSelectionTests(unittest.TestCase):
    def test_all_accounts_are_scanned_by_default(self):
        store = Mock()
        store.list_accounts.return_value = [_account(1, "a@example.com"), _account(2, "b@example.com")]
        with patch.object(register, "AccountStore", return_value=store):
            targets = register._load_ban_scan_targets(_args())

        self.assertEqual([t["id"] for t in targets], [1, 2])

    def test_selected_ids_are_respected_and_deduplicated(self):
        store = Mock()
        store.list_accounts.return_value = [_account(1, "a@example.com"), _account(2, "b@example.com")]
        with patch.object(register, "AccountStore", return_value=store):
            targets = register._load_ban_scan_targets(_args(ban_id=[2, 2]))

        self.assertEqual([t["id"] for t in targets], [2])

    def test_an_unknown_id_is_rejected(self):
        store = Mock()
        store.list_accounts.return_value = [_account(1, "a@example.com")]
        with patch.object(register, "AccountStore", return_value=store):
            with self.assertRaises(SystemExit):
                register._load_ban_scan_targets(_args(ban_id=[404]))


class BanRecordCollectionTests(unittest.TestCase):
    def test_qq_and_mhjc_mailboxes_are_both_scanned(self):
        mhjc_account = _account(2, "b@example.com", emailSource="mhjc", emailPassword="box-secret")
        targets = [_account(1, "a@example.com"), mhjc_account]
        qq_scan = Mock(return_value=[{"subject": "x", "to": "a@example.com"}])
        mhjc_scan = Mock(return_value=[{"subject": "y", "to": "b@example.com"}])

        with patch.object(register.qq_imap, "scan_qq_messages", qq_scan), \
             patch.object(register.mhjc, "scan_mhjc_messages", mhjc_scan):
            records, errors = register._collect_ban_records(_args(), targets)

        self.assertEqual(len(records), 2)
        self.assertEqual(errors, [])
        qq_scan.assert_called_once()
        mhjc_scan.assert_called_once()
        self.assertEqual(mhjc_scan.call_args[0][0].email, "b@example.com")
        self.assertEqual(mhjc_scan.call_args[0][0].password, "box-secret")

    def test_a_failing_mailbox_does_not_abort_the_scan(self):
        mhjc_account = _account(2, "b@example.com", emailSource="mhjc", emailPassword="box-secret")
        targets = [_account(1, "a@example.com"), mhjc_account]
        qq_scan = Mock(side_effect=RuntimeError("IMAP 登录被拒绝"))
        mhjc_scan = Mock(return_value=[{"subject": "y", "to": "b@example.com"}])

        with patch.object(register.qq_imap, "scan_qq_messages", qq_scan), \
             patch.object(register.mhjc, "scan_mhjc_messages", mhjc_scan):
            records, errors = register._collect_ban_records(_args(), targets)

        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("IMAP 登录被拒绝", errors[0])

    def test_without_qq_credentials_the_qq_mailbox_is_skipped(self):
        targets = [_account(1, "a@example.com")]
        qq_scan = Mock()
        with patch.object(register.qq_imap, "scan_qq_messages", qq_scan), \
             patch.object(register.mhjc, "scan_mhjc_messages", Mock()):
            records, errors = register._collect_ban_records(_args(qq_user="", qq_pass=""), targets)

        self.assertEqual(records, [])
        self.assertEqual(errors, [])
        qq_scan.assert_not_called()


class BanMarkingTests(unittest.TestCase):
    def _store(self, account):
        saved: dict = {}

        class StubStore:
            def get_account(self, account_id, *, include_password=False):
                self.requested = (account_id, include_password)
                return dict(account) if int(account_id) == int(account["id"]) else None

            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, data=dict(account_data))
                return {"id": account["id"], "email": email}

        return StubStore(), saved

    def test_marking_preserves_credentials_and_clears_the_relogin_flag(self):
        account = _account(7, "banned@example.com", password="private-password")
        store, saved = self._store(account)
        evidence = {
            "reason": "Your account has been deactivated",
            "subject": "Your account has been deactivated",
            "date": "Mon, 05 Jan 2026 10:00:00 +0800",
            "evidence": "We have deactivated your account.",
            "source": "email",
        }

        register._mark_account_banned(store, 7, evidence)

        self.assertEqual(saved["email"], "banned@example.com")
        self.assertEqual(saved["password"], "private-password")
        data = saved["data"]
        self.assertTrue(data["banned"])
        self.assertEqual(data["banReason"], evidence["reason"])
        self.assertEqual(data["banSource"], "email")
        self.assertFalse(data["reloginRequired"])
        self.assertTrue(data["banDetectedAt"])
        # credentials stay on the record
        self.assertEqual(data["session"]["accessToken"], "token-7")
        self.assertEqual(data["codexAuth"]["refresh_token"], "refresh-7")
        self.assertNotIn("password", data)

    def test_marking_an_unknown_account_is_a_no_op(self):
        store, saved = self._store(_account(7, "banned@example.com"))

        register._mark_account_banned(store, 404, {"reason": "x"})

        self.assertEqual(saved, {})


class RunBanScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_accounts_with_a_notice_are_marked(self):
        accounts = [
            _account(5, "healthy@example.com"),
            _account(7, "banned@example.com"),
        ]
        store = Mock()
        store.list_accounts.return_value = accounts
        store.get_account.side_effect = lambda account_id, **kwargs: next(
            (dict(a) for a in accounts if a["id"] == account_id), None
        )
        saved: list = []
        store.save_account.side_effect = lambda email, password, data: saved.append((email, dict(data))) or {"id": 7, "email": email}

        records = [
            _notice("Your ChatGPT code is 123456", "healthy@example.com", "123456"),
            _notice("Your account has been deactivated", "banned@example.com",
                    "We have deactivated your account after a violation of our usage policies."),
        ]
        printed: list[str] = []
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "_collect_ban_records", Mock(return_value=(records, []))), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            code = await register.run_ban_scan(_args())

        self.assertEqual(code, 0)
        self.assertEqual([email for email, _ in saved], ["banned@example.com"])
        self.assertTrue(saved[0][1]["banned"])
        self.assertNotIn("healthy@example.com", str(saved))
        payloads = [line for line in printed if line.startswith("[ban-scan-result] ")]
        self.assertEqual(len(payloads), 1)
        row = json.loads(payloads[0][len("[ban-scan-result] "):])
        self.assertEqual(row["id"], 7)
        self.assertTrue(row["banned"])

    async def test_a_clean_scan_marks_nothing_and_still_reports(self):
        accounts = [_account(5, "healthy@example.com")]
        store = Mock()
        store.list_accounts.return_value = accounts
        printed: list[str] = []
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "_collect_ban_records", Mock(return_value=([], ["QQ IMAP 未配置"]))), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            code = await register.run_ban_scan(_args())

        self.assertEqual(code, 0)
        store.save_account.assert_not_called()
        self.assertTrue(any("未发现" in line for line in printed))

    async def test_mailbox_errors_do_not_fail_the_run(self):
        accounts = [_account(5, "healthy@example.com")]
        store = Mock()
        store.list_accounts.return_value = accounts
        printed: list[str] = []
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "_collect_ban_records", Mock(return_value=([], ["QQ IMAP 登录被拒绝"]))), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            code = await register.run_ban_scan(_args())

        self.assertEqual(code, 0)
        self.assertTrue(any("QQ IMAP 登录被拒绝" in line for line in printed))

    async def test_dry_run_reports_matches_without_writing_to_mysql(self):
        accounts = [_account(7, "banned@example.com")]
        store = Mock()
        store.list_accounts.return_value = accounts
        printed: list[str] = []
        records = [_notice("Your account has been deactivated", "banned@example.com",
                           "We have deactivated your account.")]
        with patch.object(register, "AccountStore", return_value=store), \
             patch.object(register, "_collect_ban_records", Mock(return_value=(records, []))), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            code = await register.run_ban_scan(_args(ban_dry_run=True))

        self.assertEqual(code, 0)
        store.save_account.assert_not_called()
        payloads = [line for line in printed if line.startswith("[ban-scan-result] ")]
        self.assertEqual(len(payloads), 1)
        row = json.loads(payloads[0][len("[ban-scan-result] "):])
        self.assertTrue(row["dryRun"])
        self.assertIn("dry-run", " ".join(printed))


if __name__ == "__main__":
    unittest.main()
