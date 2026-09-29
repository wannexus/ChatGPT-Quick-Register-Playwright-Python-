from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import AsyncMock, Mock, patch

from core import mhjc
from data import names


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _raw_message(subject: str, body: str, *, sender: str = "OpenAI <noreply@openai.com>") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Mon, 8 Jun 2026 16:07:27 +0800"
    msg.set_content(body)
    return msg.as_bytes()


class MhjcApiTests(unittest.TestCase):
    def test_normalize_api_base_strips_path_noise_and_rejects_bad_scheme(self):
        self.assertEqual(
            mhjc.normalize_api_base("https://api.mhjc.edu.kg/api/"),
            "https://api.mhjc.edu.kg/api",
        )
        self.assertEqual(mhjc.normalize_api_base(""), mhjc.DEFAULT_API_BASE)
        with self.assertRaises(ValueError):
            mhjc.normalize_api_base("file:///tmp/mail")

    def test_create_mailbox_sends_api_key_header_and_parses_payload(self):
        captured = {}

        def fake_open(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            return _Response(json.dumps({
                "success": True,
                "data": {
                    "email": "abc12345@mhjc.edu.kg",
                    "username": "abc12345",
                    "password": "MailPass@2026",
                    "expires_at": "2026-06-09T04:30:00Z",
                },
            }).encode())

        with patch("core.mhjc.open_url", side_effect=fake_open):
            created = mhjc.create_mailbox("secret-key", ttl=7200, proxy="http://127.0.0.1:7890")

        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.mhjc.edu.kg/api/mailbox")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("X-api-key"), "secret-key")
        self.assertEqual(json.loads(request.data.decode())["ttl"], 7200)
        self.assertEqual(captured["kwargs"]["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(created["email"], "abc12345@mhjc.edu.kg")
        self.assertEqual(created["username"], "abc12345")
        self.assertEqual(created["password"], "MailPass@2026")

    def test_create_mailbox_requires_a_key_and_never_leaks_it_in_errors(self):
        with self.assertRaises(ValueError):
            mhjc.create_mailbox("")

        def fake_open(_request, **_kwargs):
            return _Response(json.dumps({"success": False, "error": "Unauthorized"}).encode())

        with patch("core.mhjc.open_url", side_effect=fake_open):
            try:
                mhjc.create_mailbox("super-secret-key")
            except mhjc.MhjcApiError as exc:
                message = str(exc)
            else:
                self.fail("a failed mailbox creation must raise MhjcApiError")

        self.assertIn("Unauthorized", message)
        self.assertNotIn("super-secret-key", message)

    def test_create_mailbox_rejects_an_incomplete_response(self):
        def fake_open(_request, **_kwargs):
            return _Response(json.dumps({"success": True, "data": {"email": ""}}).encode())

        with patch("core.mhjc.open_url", side_effect=fake_open):
            with self.assertRaises(mhjc.MhjcApiError):
                mhjc.create_mailbox("key")

    def test_delete_mailbox_targets_the_username_path(self):
        captured = {}

        def fake_open(request, **kwargs):
            captured["request"] = request
            return _Response(json.dumps({"success": True}).encode())

        with patch("core.mhjc.open_url", side_effect=fake_open):
            self.assertTrue(mhjc.delete_mailbox("key", "abc12345"))

        self.assertEqual(captured["request"].full_url, "https://api.mhjc.edu.kg/api/mailbox/abc12345")
        self.assertEqual(captured["request"].method, "DELETE")

    def test_username_from_email(self):
        self.assertEqual(mhjc.username_from_email("abc12345@mhjc.edu.kg"), "abc12345")
        self.assertEqual(mhjc.username_from_email(" ABC@Example.com "), "abc")


class MhjcCodeParseTests(unittest.TestCase):
    def test_extracts_labelled_six_digit_code(self):
        raw = _raw_message("Your ChatGPT code", "Enter this code to continue: 448291")
        self.assertEqual(mhjc.code_from_raw_message(raw), "448291")

    def test_prefers_a_six_digit_code_over_other_numbers(self):
        raw = _raw_message("Verify", "Order 12345678 shipped. Verification code is 902144")
        self.assertEqual(mhjc.code_from_raw_message(raw), "902144")

    def test_ignores_html_tags_when_scanning_the_body(self):
        raw = _raw_message("Verify", "<html><body><b>验证码：</b> 731905</body></html>")
        self.assertEqual(mhjc.code_from_raw_message(raw), "731905")

    def test_returns_none_when_no_code_is_present(self):
        raw = _raw_message("Welcome", "Thanks for signing up, no digits here")
        self.assertIsNone(mhjc.code_from_raw_message(raw))

    def test_decodes_non_ascii_subjects(self):
        raw = _raw_message("验证码", "您的验证码是 556677")
        self.assertEqual(mhjc.code_from_raw_message(raw), "556677")

    def test_polling_raises_timeout_when_no_code_arrives(self):
        config = mhjc.MhjcImapConfig(email="a@mhjc.edu.kg", password="pw")
        with patch.object(mhjc, "_poll_once", return_value=None) as poll, \
             patch.object(mhjc.time, "sleep"):
            with self.assertRaises(TimeoutError):
                mhjc.fetch_mhjc_code(config, max_attempts=2, interval_seconds=0)
        self.assertEqual(poll.call_count, 2)

    def test_polling_returns_the_first_code_it_sees(self):
        config = mhjc.MhjcImapConfig(email="a@mhjc.edu.kg", password="pw")
        with patch.object(mhjc, "_poll_once", side_effect=[None, "123456"]), \
             patch.object(mhjc.time, "sleep"):
            self.assertEqual(mhjc.fetch_mhjc_code(config, max_attempts=5, interval_seconds=0), "123456")


class MhjcImapConfigTests(unittest.TestCase):
    def test_defaults_point_at_the_documented_imap_endpoint(self):
        config = mhjc.MhjcImapConfig(email="a@mhjc.edu.kg", password="pw")
        self.assertEqual(config.host, "mail.mhjc.edu.kg")
        self.assertEqual(config.port, 993)
        self.assertEqual(config.mailbox, "INBOX")


class LocalConfigEnvFileTests(unittest.TestCase):
    """`.env` in the project root must feed the QR_* config used by the CLI."""

    def test_env_file_is_loaded_without_overriding_real_environment(self):
        from core import local_config
        with patch.object(local_config, "load_dotenv") as loader:
            local_config.load_env_file()
        self.assertTrue(loader.called, "local_config must load the project .env")
        _args, kwargs = loader.call_args
        self.assertFalse(kwargs.get("override", False), "real environment variables must win over .env")
        self.assertEqual(str(kwargs.get("dotenv_path")), str(local_config.PROJECT_DIR / ".env"))

    def test_mhjc_key_is_mapped_from_the_environment(self):
        import os
        import tempfile
        from pathlib import Path as _Path
        from core import local_config
        # patch.dict simulates a real launch variable, so declare it as one.
        with patch.object(local_config, "PROCESS_ENV_KEYS", frozenset({"QR_MHJC_API_KEY"})), \
             patch.object(local_config, "ENV_PATH", _Path(tempfile.mkdtemp()) / "absent.env"), \
             patch.dict(os.environ, {"QR_MHJC_API_KEY": "env-key"}, clear=True):
            self.assertEqual(local_config.effective_config()["mhjcApiKey"], "env-key")

    def test_saved_values_are_read_back_from_the_env_file(self):
        """Settings persistence itself is covered by tests/test_local_config_env.py."""
        import tempfile
        from pathlib import Path as _Path
        import os
        from core import local_config
        with tempfile.TemporaryDirectory() as tmp:
            env = _Path(tmp) / ".env"
            with patch.object(local_config, "ENV_PATH", env), \
                 patch.object(local_config, "LEGACY_JSON_PATH", _Path(tmp) / "none.json"), \
                 patch.dict(os.environ, {}, clear=True):
                local_config.save_config({"duckToken": "saved"})
                self.assertEqual(local_config.effective_config()["duckToken"], "saved")


class RegisterMhjcWiringTests(unittest.IsolatedAsyncioTestCase):
    """The CLI must expose mhjc as both an email source and an IMAP code source."""

    def _args(self, *extra):
        import register
        return register.parse_args(["--mode", "register", "--email-source", "mhjc", "--code-source", "mhjc", *extra])

    def test_cli_accepts_mhjc_sources(self):
        args = self._args()
        self.assertEqual(args.email_source, "mhjc")
        self.assertEqual(args.code_source, "mhjc")
        self.assertEqual(args.mhjc_imap_host, mhjc.DEFAULT_IMAP_HOST)
        self.assertEqual(args.mhjc_imap_port, mhjc.DEFAULT_IMAP_PORT)

    async def test_code_fetcher_reads_the_created_mailbox_over_imap(self):
        import register
        args = self._args()
        args.mhjc_email = "abc12345@mhjc.edu.kg"
        args.mhjc_password = "MailPass@2026"

        fetch = register.make_code_fetcher(args, since_ts=123.0)
        with patch.object(register.mhjc, "fetch_mhjc_code", return_value="654321") as poll:
            code = await fetch()

        self.assertEqual(code, "654321")
        config = poll.call_args[0][0]
        self.assertEqual(config.email, "abc12345@mhjc.edu.kg")
        self.assertEqual(config.password, "MailPass@2026")
        self.assertEqual(config.host, mhjc.DEFAULT_IMAP_HOST)
        self.assertEqual(poll.call_args[1]["since_ts"], 123.0)

    async def test_code_fetcher_requires_a_mailbox_created_in_this_run(self):
        import register
        args = self._args()
        with self.assertRaises(SystemExit):
            register.make_code_fetcher(args, since_ts=0.0)

    async def test_registration_persists_the_mailbox_password_without_printing_it(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path as _Path
        from types import SimpleNamespace
        import register

        saved = {}

        class StubStore:
            def save_account(self, email, password, account_data):
                saved.update(email=email, password=password, account_data=account_data)
                return {"id": 7, "email": email}

        page = SimpleNamespace(close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        birthday = SimpleNamespace(iso=Mock(return_value="1990-01-01"))
        args = register.parse_args(["--mode", "register", "--email-source", "mhjc", "--code-source", "mhjc"])
        args.ac_check = False
        args.codex_oauth = False
        args.no_codex_oauth = True
        args.no_clear_tokens = True

        session_result = {"ok": True, "status": 200, "text": '{"accessToken":"t"}',
                          "parsed": {"accessToken": "t", "expires": "2030-01-01T00:00:00Z"}}
        created = {"email": "abc12345@mhjc.edu.kg", "username": "abc12345",
                   "password": "MailPass@2026", "expiresAt": None}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(register, "AccountStore", StubStore), \
             patch.object(register.mhjc, "create_mailbox", Mock(return_value=created)) as create, \
             patch.object(register, "make_code_fetcher", Mock(return_value=AsyncMock(return_value="123456"))), \
             patch.object(register.flow, "step1_open", AsyncMock()), \
             patch.object(register.flow, "step2_signup_email", AsyncMock()), \
             patch.object(register.flow, "step3_password", AsyncMock(return_value="")), \
             patch.object(register.flow, "step4_code", AsyncMock(return_value="123456")), \
             patch.object(register.flow, "random_profile", Mock(return_value=("First", "Last", birthday))), \
             patch.object(register.flow, "step5_profile", AsyncMock()), \
             patch.object(register.flow, "step6_wait_success", AsyncMock()), \
             patch.object(register.session, "fetch_session", AsyncMock(return_value=session_result)), \
             redirect_stdout(output):
            result = await register._run_one_account(
                args, context, out_dir=_Path(tmp), label="test", nav_timeout_ms=1000
            )

        self.assertEqual(saved["email"], "abc12345@mhjc.edu.kg")
        self.assertEqual(saved["account_data"]["emailSource"], "mhjc")
        self.assertEqual(saved["account_data"]["emailPassword"], "MailPass@2026")
        self.assertNotIn("password", saved["account_data"])
        self.assertNotIn("MailPass@2026", output.getvalue())
        self.assertEqual(result.account_id, 7)
        create.assert_called_once()
        # 邮箱用户名来自注册档案姓名（flow.random_profile 被 patch 成 First/Last），不再是 temp_
        generated = create.call_args.kwargs["username"]
        self.assertTrue(names.MAILBOX_USERNAME_PATTERN.match(generated), generated)
        self.assertIn("last", generated)
        self.assertNotIn("temp_", generated)


class UniqueMailboxTests(unittest.TestCase):
    """A saved custom username can only ever be used once, so it must not be fatal."""

    def _create(self, side_effect, **kwargs):
        calls = []

        def fake_create(api_key, **inner):
            calls.append(inner.get("username", ""))
            outcome = side_effect(len(calls) - 1)
            if isinstance(outcome, Exception):
                raise outcome
            return {"email": f"{outcome}@mhjc.edu.kg", "username": outcome,
                    "password": "pw", "expiresAt": None}

        with patch.object(mhjc, "create_mailbox", side_effect=fake_create):
            result = mhjc.create_unique_mailbox("key", **kwargs)
        return result, calls

    def test_generated_person_name_is_preferred_over_the_provider_default(self):
        # 旧行为是直接把 username="" 交给服务端，服务端会给出 temp_<hex> 这种一眼临时的地址
        result, calls = self._create(lambda i: "emma.wilson", name_generator=lambda: "emma.wilson")
        self.assertEqual(calls, ["emma.wilson"])
        self.assertEqual(result["email"], "emma.wilson@mhjc.edu.kg")
        self.assertNotIn("usernameFallback", result)
        self.assertNotIn("nameFallback", result)

    def test_generated_name_uses_the_signup_profile_name_when_available(self):
        # 排版随机（emma.wilson / emmawilson92 / ewilson346 ...），但姓名必须来自注册档案
        for _ in range(30):
            result, calls = self._create(lambda i: "ok", name_first="Emma", name_last="Wilson")
            self.assertEqual(len(calls), 1)
            self.assertTrue(names.MAILBOX_USERNAME_PATTERN.match(calls[0]), calls)
            self.assertIn("wilson", calls[0])
            self.assertTrue(calls[0].startswith(("emma", "e")), calls)

    def test_a_taken_generated_name_is_retried_with_another_name(self):
        generated = iter(["emma.wilson", "emma.davis92", "emmawilson"])

        def outcome(index):
            if index == 0:
                return mhjc.MhjcApiError("HTTP 400：user 'emma.wilson' already exists")
            return "emma.davis92"

        result, calls = self._create(outcome, name_generator=lambda: next(generated))
        self.assertEqual(calls[:2], ["emma.wilson", "emma.davis92"])
        self.assertEqual(result["nameFallback"], "name")
        self.assertNotIn("usernameFallback", result)

    def test_provider_default_is_only_the_last_resort(self):
        def outcome(index):
            return mhjc.MhjcApiError("HTTP 400：user already exists")

        calls = []

        def fake_create(api_key, **inner):
            calls.append(inner.get("username", ""))
            raise outcome(len(calls) - 1)

        with patch.object(mhjc, "create_mailbox", side_effect=fake_create):
            with self.assertRaises(mhjc.MhjcApiError):
                mhjc.create_unique_mailbox("key", name_generator=lambda: "emma.wilson", name_attempts=1)
        self.assertEqual(calls, ["emma.wilson", ""], "只有姓名候选全部被占用才回落服务端默认")

    def test_provider_fallback_is_reported_to_the_caller(self):
        def outcome(index):
            if index == 0:
                return mhjc.MhjcApiError("HTTP 400：user 'emma.wilson' already exists")
            return "temp_aabbcc"

        result, calls = self._create(outcome, name_generator=lambda: "emma.wilson", name_attempts=1)
        self.assertEqual(calls, ["emma.wilson", ""])
        self.assertEqual(result["username"], "temp_aabbcc")
        self.assertEqual(result["nameFallback"], "provider")

    def test_provider_style_keeps_the_old_single_attempt_behaviour(self):
        result, calls = self._create(lambda i: "temp_aabbcc", name_style="provider")
        self.assertEqual(calls, [""])
        self.assertEqual(result["email"], "temp_aabbcc@mhjc.edu.kg")

    def test_random_name_when_nothing_is_requested(self):
        result, calls = self._create(lambda i: "random123", name_style="provider")
        self.assertEqual(calls, [""])
        self.assertEqual(result["email"], "random123@mhjc.edu.kg")
        self.assertNotIn("usernameFallback", result)

    def test_requested_name_is_used_as_is_when_free(self):
        result, calls = self._create(lambda i: "wanted-name", username="wanted-name")
        self.assertEqual(calls, ["wanted-name"])
        self.assertNotIn("usernameFallback", result)

    def test_a_taken_name_gets_a_unique_suffix(self):
        def outcome(index):
            if index == 0:
                return mhjc.MhjcApiError("MHJC API HTTP 400：user 'wanted' already exists")
            return "wanted-ab12"

        result, calls = self._create(outcome, username="wanted")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("wanted-"), calls)
        self.assertNotEqual(calls[1], "wanted")
        self.assertEqual(result["usernameFallback"], "wanted")
        self.assertEqual(result["email"], "wanted-ab12@mhjc.edu.kg")

    def test_falls_back_to_a_generated_name_when_the_suffix_is_taken(self):
        def outcome(index):
            if index < 2:
                return mhjc.MhjcApiError("HTTP 400：user already exists")
            return "enma.davis77"

        result, calls = self._create(outcome, username="wanted",
                                     name_generator=lambda: "enma.davis77")
        self.assertEqual(len(calls), 3, "自定义名 → 加后缀 → 姓名候选")
        self.assertEqual(calls[2], "enma.davis77")
        self.assertEqual(result["email"], "enma.davis77@mhjc.edu.kg")
        self.assertEqual(result["usernameFallback"], "wanted")

    def test_explicit_username_still_wins_over_generated_names(self):
        result, calls = self._create(lambda i: "wanted-name", username="wanted-name",
                                     name_generator=lambda: "emma.wilson")
        self.assertEqual(calls, ["wanted-name"])
        self.assertNotIn("nameFallback", result)

    def test_a_real_error_is_not_masked_by_retries(self):
        calls = []

        def fake_create(api_key, **inner):
            calls.append(inner.get("username", ""))
            raise mhjc.MhjcApiError("MHJC API HTTP 401：Unauthorized")

        with patch.object(mhjc, "create_mailbox", side_effect=fake_create):
            with self.assertRaises(mhjc.MhjcApiError) as ctx:
                mhjc.create_unique_mailbox("bad-key", username="wanted")

        self.assertIn("Unauthorized", str(ctx.exception))
        self.assertEqual(len(calls), 1, "an auth failure must not be retried as a name conflict")

    def test_every_attempt_failing_reports_the_last_error(self):
        with patch.object(mhjc, "create_mailbox",
                          side_effect=mhjc.MhjcApiError("HTTP 400：user already exists")):
            with self.assertRaises(mhjc.MhjcApiError):
                mhjc.create_unique_mailbox("key", username="wanted")


class MhjcErrorMessageTests(unittest.TestCase):
    def test_extracts_the_useful_message_from_an_api_error_body(self):
        body = '{"error":"Creation failed","message":"ERROR: user \'x\' already exists\n","code":6}'
        self.assertIn("already exists", mhjc.api_error_message(body))

    def test_detects_a_name_conflict(self):
        self.assertTrue(mhjc.looks_like_name_conflict("ERROR: user 'x' already exists"))
        self.assertFalse(mhjc.looks_like_name_conflict("Unauthorized"))


class RegisterUsesUniqueMailboxTests(unittest.TestCase):
    def test_the_run_creates_mailboxes_through_the_fallback_capable_helper(self):
        """Using create_mailbox directly would make a taken username fatal again."""
        source = Path("register.py").read_text(encoding="utf-8")
        self.assertIn("mhjc.create_unique_mailbox(", source)
        self.assertNotIn("mhjc.create_mailbox(", source)


if __name__ == "__main__":
    unittest.main()
