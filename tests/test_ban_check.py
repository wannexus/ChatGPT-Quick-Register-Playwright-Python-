from __future__ import annotations

import unittest
from email.message import EmailMessage

from core import ban_check


def _raw(subject: str, to: str, body: str, *, date: str = "Mon, 05 Jan 2026 10:00:00 +0800") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "OpenAI <noreply@openai.com>"
    msg["To"] = to
    msg["Date"] = date
    msg.set_content(body)
    return msg.as_bytes()


class ClassifyBanNoticeTests(unittest.TestCase):
    def test_english_deactivation_notices_are_detected(self):
        for subject, body in (
            ("Your account has been deactivated",
             "Your OpenAI account has been deactivated. You no longer have access to ChatGPT."),
            ("We've deactivated your account",
             "We have deactivated your account because of a violation of our usage policies."),
            ("Account suspended", "Your account has been suspended pending review."),
            ("Your account was terminated", "This account was terminated for violating our Terms of Use."),
            ("Account banned", "Your account has been banned."),
        ):
            with self.subTest(subject=subject):
                self.assertIsNotNone(ban_check.classify_ban_notice(subject, body))

    def test_chinese_deactivation_notices_are_detected(self):
        for subject, body in (
            ("您的账号已被停用", "您的账号已被停用，原因是违反使用政策。"),
            ("账号封禁通知", "该账号被封禁。"),
            ("帐号禁用", "我们发现您的帐号被禁用。"),
            ("账号已被删除", "您的账号已被删除，原因是违反条款。"),
            ("身份验证错误", "你没有账户，因为该账户已被删除或停用。如果你认为这是错误，请通过我们的帮助中心 help.openai.com 联系我们。错误代码：account_deactivated"),
        ):
            with self.subTest(subject=subject):
                self.assertIsNotNone(ban_check.classify_ban_notice(subject, body))

    def test_ordinary_mail_is_not_a_ban_notice(self):
        for subject, body in (
            ("Your ChatGPT code is 123456", "Enter this code to continue signing in."),
            ("New login to your account", "Your account was accessed from a new device."),
            ("OpenAI newsletter", "Product updates and research highlights."),
            ("Unusual activity detected", "We noticed unusual activity, please verify your identity."),
            ("Your receipt from OpenAI", "Thanks for your payment."),
        ):
            with self.subTest(subject=subject):
                self.assertIsNone(ban_check.classify_ban_notice(subject, body))

    def test_reason_reports_the_matched_phrase(self):
        reason = ban_check.classify_ban_notice("Account deactivated", "your account has been deactivated")

        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip())
        self.assertIn("deactivat", reason.lower())


class MessageRecordTests(unittest.TestCase):
    def test_raw_message_becomes_a_plain_record(self):
        raw = _raw("Your account has been deactivated", "Person <person@example.com>",
                   "Your account has been deactivated.")

        record = ban_check.message_to_record(raw)

        self.assertEqual(record["subject"], "Your account has been deactivated")
        self.assertIn("person@example.com", record["to"])
        self.assertIn("deactivated", record["body"])
        self.assertGreater(record["date_ts"], 0)

    def test_encoded_headers_and_multipart_bodies_are_decoded(self):
        msg = EmailMessage()
        msg["Subject"] = "=?utf-8?b?6LSm5Y+35bey6KKr5YGc55So?="  # 账号已被停用
        msg["To"] = "=?utf-8?b?cGVyc29uQGV4YW1wbGUuY29t?="
        msg["Date"] = "Mon, 05 Jan 2026 10:00:00 +0800"
        msg.set_content("plain fallback")
        msg.add_alternative("<p>您的账号已被停用</p>", subtype="html")

        record = ban_check.message_to_record(msg.as_bytes())

        self.assertIn("账号已被停用", record["subject"])
        self.assertIn("person@example.com", record["to"])
        self.assertIn("账号已被停用", record["body"])

    def test_broken_input_does_not_raise(self):
        record = ban_check.message_to_record(b"not a real message at all")

        self.assertEqual(record["subject"], "")
        self.assertIsNone(ban_check.classify_ban_notice(record["subject"], record["body"]))

    def test_empty_input_is_safe(self):
        for raw in (b"", None):
            with self.subTest(raw=raw):
                record = ban_check.message_to_record(raw)  # type: ignore[arg-type]
                self.assertEqual(record["subject"], "")


class FindBannedAccountsTests(unittest.TestCase):
    def _accounts(self):
        return [
            {"id": 5, "email": "first@example.com"},
            {"id": 7, "email": "banned@example.com"},
            {"id": 9, "email": "other@example.com"},
        ]

    def test_only_the_notified_account_is_marked(self):
        records = [
            ban_check.message_to_record(_raw(
                "Your account has been deactivated",
                "Person <banned@example.com>",
                "We have deactivated your account after a violation of our usage policies.",
            )),
            ban_check.message_to_record(_raw(
                "Your ChatGPT code is 123456", "Person <first@example.com>", "123456",
            )),
        ]

        found = ban_check.find_banned_accounts(records, self._accounts())

        self.assertEqual(list(found), [7])
        self.assertIn("deactivat", found[7]["reason"].lower())
        self.assertEqual(found[7]["source"], "email")

    def test_matching_is_case_insensitive_and_ignores_display_names(self):
        records = [ban_check.message_to_record(_raw(
            "Account suspended", "Banned Person <BANNED@Example.COM>", "Your account has been suspended.",
        ))]

        found = ban_check.find_banned_accounts(records, self._accounts())

        self.assertEqual(list(found), [7])

    def test_unknown_recipients_are_ignored(self):
        records = [ban_check.message_to_record(_raw(
            "Account deactivated", "stranger@elsewhere.com", "Your account has been deactivated.",
        ))]

        found = ban_check.find_banned_accounts(records, self._accounts())

        self.assertEqual(found, {})

    def test_the_newest_notice_wins(self):
        records = [
            ban_check.message_to_record(_raw(
                "Account suspended", "banned@example.com", "Your account has been suspended.",
                date="Mon, 05 Jan 2026 10:00:00 +0800",
            )),
            ban_check.message_to_record(_raw(
                "Your account has been deactivated", "banned@example.com",
                "Your account has been deactivated.",
                date="Wed, 07 Jan 2026 10:00:00 +0800",
            )),
        ]

        found = ban_check.find_banned_accounts(records, self._accounts())

        self.assertIn("deactivat", found[7]["reason"].lower())
        self.assertEqual(found[7]["subject"], "Your account has been deactivated")

    def test_accounts_without_a_ban_notice_are_absent(self):
        found = ban_check.find_banned_accounts([], self._accounts())

        self.assertEqual(found, {})

    def test_evidence_never_carries_the_whole_mailbox_body(self):
        long_body = "Your account has been deactivated. " + ("x" * 5000)
        records = [ban_check.message_to_record(_raw("Account deactivated", "banned@example.com", long_body))]

        found = ban_check.find_banned_accounts(records, self._accounts())

        self.assertLessEqual(len(found[7]["evidence"]), 500)


if __name__ == "__main__":
    unittest.main()
