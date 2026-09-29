"""邮箱用户名按真人姓名生成（替代服务端 temp_<hex>）的回归测试。"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import register
from core import mhjc
from data import names
from webui import server


class MailboxUsernameFormatTests(unittest.TestCase):
    def test_generated_names_satisfy_the_provider_rule(self):
        # 服务端规则：3–32 位，只允许小写字母、数字、点、下划线、连字符
        samples = [names.random_mailbox_username() for _ in range(500)]
        for local in samples:
            self.assertTrue(names.MAILBOX_USERNAME_PATTERN.match(local), local)
            self.assertNotIn("temp", local, local)
        self.assertGreaterEqual(min(len(s) for s in samples), 3)
        self.assertLessEqual(max(len(s) for s in samples), names.MAILBOX_USERNAME_MAX)

    def test_generated_names_are_varied(self):
        samples = {names.random_mailbox_username() for _ in range(200)}
        self.assertGreater(len(samples), 150, "姓名候选必须有足够的去重空间")

    def test_signup_profile_name_is_reused_in_the_address(self):
        for _ in range(40):
            local = names.random_mailbox_username("Emma", "Wilson")
            self.assertIn("wilson", local, local)
            self.assertTrue(local.startswith("emma") or local.startswith("e"), local)

    def test_dirty_or_uppercase_hints_are_sanitized(self):
        for _ in range(20):
            local = names.random_mailbox_username("EMMA!!", "WIL-SON 2")
            self.assertTrue(names.MAILBOX_USERNAME_PATTERN.match(local), local)
            self.assertEqual(local, local.lower())

    def test_sanitizer_matches_the_provider_contract(self):
        self.assertEqual(names.sanitize_mailbox_username("ABC.DEF@mhjc.edu.kg"), "abc.def")
        self.assertEqual(names.sanitize_mailbox_username(""), "")
        self.assertEqual(names.sanitize_mailbox_username("__..__"), "")
        self.assertEqual(len(names.sanitize_mailbox_username("x" * 40)), names.MAILBOX_USERNAME_MAX)

    def test_mailbox_helper_defaults_to_person_names(self):
        signature = inspect.signature(mhjc.create_unique_mailbox)
        self.assertEqual(signature.parameters["name_style"].default, "name")


class DefaultWiringTests(unittest.TestCase):
    def test_cli_defaults_to_person_names(self):
        args = register.parse_args(["--mode", "register", "--email-source", "mhjc"])
        self.assertEqual(args.mhjc_name_style, "name")

    def test_cli_can_restore_the_provider_default(self):
        args = register.parse_args(["--mode", "register", "--mhjc-name-style", "provider"])
        self.assertEqual(args.mhjc_name_style, "provider")

    def test_webui_passes_the_name_style_to_the_cli(self):
        payload = server.BatchPayload(emailSource="mhjc", codeSource="mhjc", mhjcNameStyle="provider")
        args = server._build_args(payload)
        self.assertIn("--mhjc-name-style", args)
        self.assertEqual(args[args.index("--mhjc-name-style") + 1], "provider")

    def test_webui_batch_payload_omits_the_flag_when_unset(self):
        payload = server.BatchPayload(emailSource="mhjc", codeSource="mhjc")
        self.assertNotIn("--mhjc-name-style", server._build_args(payload))


class DefaultsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_webui_defaults_expose_the_name_style(self):
        with patch.object(server, "effective_config", return_value={"mhjcNameStyle": "provider"}):
            payload = await server.defaults()
        self.assertEqual(payload["mhjcNameStyle"], "provider")

    async def test_webui_defaults_fall_back_to_person_names(self):
        with patch.object(server, "effective_config", return_value={}):
            payload = await server.defaults()
        self.assertEqual(payload["mhjcNameStyle"], "name")


if __name__ == "__main__":
    unittest.main()
