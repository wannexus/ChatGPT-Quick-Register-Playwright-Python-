from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core import flow

# The exact URL shape that made the old re-login time out: the OAuth authorize
# endpoint renders the password / OTP form in place, so URL-only polling stalls.
AUTHORIZE_URL = (
    "https://auth.openai.com/api/accounts/authorize?client_id=app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
    "&screen_hint=login_or_signup&login_hint=person%40example.com&prompt=login"
)
LOGIN_URL = "https://chatgpt.com/auth/login"
PASSWORD_URL = "https://auth.openai.com/log-in/password"


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    def nth(self, _index: int):
        return self

    async def count(self) -> int:
        return 1 if self._visible() else 0

    async def is_visible(self) -> bool:
        return self._visible()

    def _visible(self) -> bool:
        page = self._page
        sel = self._selector
        if "password" in sel:
            return page.password_visible
        if "code" in sel.lower() or "numeric" in sel.lower() or "one-time-code" in sel:
            return page.code_visible
        if "email" in sel or "username" in sel or "mail" in sel:
            return page.email_visible
        return False


class FakePage:
    """Minimal Page stand-in driven by a tiny visibility state machine."""

    def __init__(self, *, url: str, body: str = "", password: bool = False,
                 code: bool = False, email: bool = False) -> None:
        self.url = url
        self.body = body
        self.password_visible = password
        self.code_visible = code
        self.email_visible = email
        self.goto = AsyncMock()
        self.screenshots: list = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def evaluate(self, _script: str):
        return self.body

    async def title(self) -> str:
        return ""

    async def content(self) -> str:
        return self.body


class ClassifyLoginSignalTests(unittest.TestCase):
    def test_password_form_on_the_authorize_url_counts_as_progress(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=True, has_code_form=False, body_text="",
        )

        self.assertEqual(signal, "password")

    def test_code_form_on_the_authorize_url_counts_as_progress(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=True, body_text="",
        )

        self.assertEqual(signal, "code")

    def test_ban_notice_beats_a_reused_login_form(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=False,
            body_text="Your account has been deactivated. You no longer have access to ChatGPT.",
        )

        self.assertEqual(signal, "banned")

    def test_chinese_ban_notice_is_detected(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=False,
            body_text="您的账号已被停用，原因是违反使用政策。",
        )

        self.assertEqual(signal, "banned")

    def test_account_deactivated_error_page_is_detected(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=False,
            body_text=("身份验证错误。你没有账户，因为该账户已被删除或停用。"
                       "如果你认为这是错误，请通过我们的帮助中心 help.openai.com 联系我们。"
                       "错误代码：account_deactivated 请求 ID：f302ec39-6536-4062-96b2-818f3e084245"),
        )

        self.assertEqual(signal, "banned")

    def test_logged_in_page_is_recognised(self):
        signal = flow.classify_login_signal(
            url="https://chatgpt.com/", has_password_form=False, has_code_form=False, body_text="How can I help?",
        )

        self.assertEqual(signal, "logged_in")

    def test_still_on_the_email_form_is_reported(self):
        signal = flow.classify_login_signal(
            url=LOGIN_URL, has_password_form=False, has_code_form=False, body_text="", has_email_form=True,
        )

        self.assertEqual(signal, "email")

    def test_rate_limit_and_cloudflare_are_reported_separately(self):
        rate = flow.classify_login_signal(
            url=PASSWORD_URL, has_password_form=False, has_code_form=False, body_text="操作超时，请重试",
        )
        cloudflare = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=False,
            body_text="Just a moment... enable JavaScript and cookies to continue",
        )

        self.assertEqual(rate, "rate_limited")
        self.assertEqual(cloudflare, "cloudflare")

    def test_password_page_without_a_form_still_counts(self):
        signal = flow.classify_login_signal(
            url=PASSWORD_URL, has_password_form=False, has_code_form=False, body_text="",
        )

        self.assertEqual(signal, "password")

    def test_nothing_recognisable_is_unknown(self):
        signal = flow.classify_login_signal(
            url=AUTHORIZE_URL, has_password_form=False, has_code_form=False, body_text="",
        )

        self.assertEqual(signal, "unknown")


class WaitForLoginStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_authorize_page_with_a_password_form_is_progress_not_a_timeout(self):
        page = FakePage(url=AUTHORIZE_URL, password=True)

        signal = await flow.wait_for_login_step(
            page, accept=("password", "code", "logged_in"), total_timeout_seconds=5, label="test",
        )

        self.assertEqual(signal, "password")

    async def test_a_ban_notice_raises_a_typed_error_with_the_page_text(self):
        page = FakePage(url=AUTHORIZE_URL, body="Your account has been deactivated.")

        with self.assertRaises(flow.AccountBannedError) as ctx:
            await flow.wait_for_login_step(
                page, accept=("password", "code", "logged_in"), total_timeout_seconds=5, label="test",
            )

        self.assertIn("deactivated", str(ctx.exception))

    async def test_timeout_reports_the_last_signal_and_url(self):
        page = FakePage(url=AUTHORIZE_URL, body="")

        with self.assertRaises(TimeoutError) as ctx:
            await flow.wait_for_login_step(
                page, accept=("password", "code", "logged_in"), total_timeout_seconds=1, label="relogin",
            )

        message = str(ctx.exception)
        self.assertIn("relogin", message)
        self.assertIn("unknown", message)
        self.assertIn("auth.openai.com", message)


class ReloginProgressRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_relogin_fills_the_code_when_the_form_lives_on_the_authorize_url(self):
        page = FakePage(url=AUTHORIZE_URL, code=True)
        seen_urls: list[str] = []

        async def _fill(target, *_args, **_kwargs):
            # Submitting the code is what finally navigates away.
            seen_urls.append(target.url)
            target.url = "https://chatgpt.com/"
            target.code_visible = False
            target.body = "How can I help?"
            return "654321"

        with patch.object(flow, "_ensure_login_email_input", AsyncMock(return_value=object())), \
             patch.object(flow, "_submit_email_form", AsyncMock()), \
             patch.object(flow, "_fill_verification_code", AsyncMock(side_effect=_fill)):
            code = await flow.step_login_existing_account(
                page, email="person@example.com", password="", auth_mode="otp",
                fetch_code=AsyncMock(return_value="654321"), total_timeout_seconds=5,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(seen_urls, [AUTHORIZE_URL],
                         "the code form was reached while the URL was still the authorize endpoint")

    async def test_relogin_raises_the_ban_error_instead_of_timing_out(self):
        page = FakePage(url=AUTHORIZE_URL, body="We have deactivated your account.")
        with patch.object(flow, "_ensure_login_email_input", AsyncMock(return_value=object())), \
             patch.object(flow, "_submit_email_form", AsyncMock()):
            with self.assertRaises(flow.AccountBannedError):
                await flow.step_login_existing_account(
                    page, email="person@example.com", password="", auth_mode="otp",
                    fetch_code=AsyncMock(), total_timeout_seconds=5,
                )

    async def test_relogin_returns_once_the_account_lands_on_chatgpt(self):
        page = FakePage(url="https://chatgpt.com/", body="How can I help?")
        with patch.object(flow, "_ensure_login_email_input", AsyncMock(return_value=object())), \
             patch.object(flow, "_submit_email_form", AsyncMock()):
            code = await flow.step_login_existing_account(
                page, email="person@example.com", password="", auth_mode="otp",
                fetch_code=AsyncMock(), total_timeout_seconds=5,
            )

        self.assertEqual(code, "")


if __name__ == "__main__":
    unittest.main()
