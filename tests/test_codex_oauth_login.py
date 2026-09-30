from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from core import codex_oauth


LOGIN_URL = "https://auth.openai.com/log-in"


class OAuthLoginFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def show_form(self, html: str):
        await self.page.route(LOGIN_URL, lambda route: route.fulfill(content_type="text/html", body=html))
        await self.page.goto(LOGIN_URL)

    async def test_visible_code_behind_hidden_old_fields_counts_as_progress(self):
        await self.show_form("""
            <input type="email" style="display:none">
            <input name="verification_code" style="display:none">
            <input type="email" value="person@example.com">
            <input name="verification_code" aria-label="Verification code">
        """)
        callback = asyncio.get_running_loop().create_future()

        self.assertTrue(await codex_oauth._has_visible_otp_input(self.page))
        self.assertTrue(await codex_oauth._wait_for_oauth_progress(
            self.page, callback, LOGIN_URL, timeout_seconds=0.5,
        ))
        self.assertTrue(await codex_oauth._fill_otp_code(self.page, "123456"))
        self.assertEqual(await self.page.locator("input[name=verification_code]").last.input_value(), "123456")
        self.assertEqual(await self.page.locator("input[name=verification_code]").first.input_value(), "")

    async def test_six_visible_boxes_are_used_after_hidden_old_boxes(self):
        hidden = "".join('<input maxlength="1" style="display:none">' for _ in range(6))
        visible = "".join('<input maxlength="1">' for _ in range(6))
        await self.show_form(hidden + visible)

        self.assertTrue(await codex_oauth._has_visible_otp_input(self.page))
        self.assertTrue(await codex_oauth._fill_otp_code(self.page, "654321"))
        self.assertEqual(await self.page.locator("input[maxlength='1']").evaluate_all(
            "els => els.map(el => el.value)"
        ), ["", "", "", "", "", "", "6", "5", "4", "3", "2", "1"])

    async def test_login_loop_uses_code_form_even_while_email_remains_visible(self):
        html = '<input type="email"><input aria-label="Verification code">'
        await self.show_form(html)
        with patch.object(codex_oauth, "build_authorize_url", return_value=LOGIN_URL), \
             patch.object(codex_oauth, "_save_debug", AsyncMock()), \
             patch.object(codex_oauth, "_fill_email_on_login", AsyncMock()) as fill_email:
            with self.assertRaisesRegex(RuntimeError, "拿 OTP 失败"):
                await codex_oauth.run_codex_oauth(
                    self.page, account_email="person@example.com",
                    fetch_code=AsyncMock(side_effect=RuntimeError("fixture")), timeout=5,
                )
        fill_email.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
