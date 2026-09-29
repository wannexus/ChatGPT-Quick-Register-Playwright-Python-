from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import async_playwright

from core import codex_oauth, flow


class AccountStateCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_preserves_clearance_and_other_origins_without_network_requests(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            requests = []

            async def fixture(route):
                requests.append(route.request.url)
                await route.fulfill(content_type="text/html", body="<h1>Fixture</h1>")

            await context.route("**/*", fixture)
            try:
                page = await context.new_page()
                await page.goto("https://chatgpt.com/fixture")
                await page.evaluate("() => {localStorage.setItem('account', 'old'); sessionStorage.setItem('account', 'old');}")
                mail = await context.new_page()
                await mail.goto("https://mail.example.com/fixture")
                await mail.evaluate("() => {localStorage.setItem('mail', 'keep'); sessionStorage.setItem('mail', 'keep');}")
                cookies = [
                    {"name": name, "value": "fixture", "domain": ".chatgpt.com", "path": "/", "secure": True}
                    for name in (*flow.CLOUDFLARE_COOKIE_NAMES, "account_session")
                ]
                cookies += [
                    {"name": "account_session", "value": "fixture", "domain": "auth.openai.com", "path": "/", "secure": True},
                    {"name": "account_session", "value": "fixture", "domain": "mail.example.com", "path": "/", "secure": True},
                ]
                await context.add_cookies(cookies)
                requests.clear()
                await flow.clear_openai_state(context)
                self.assertEqual(requests, [])
                self.assertEqual(await page.evaluate("() => [localStorage.length, sessionStorage.length]"), [0, 0])
                self.assertEqual(await mail.evaluate("() => [localStorage.getItem('mail'), sessionStorage.getItem('mail')]"), ["keep", "keep"])
                remaining = await context.cookies()
                self.assertEqual({cookie["name"] for cookie in remaining if "chatgpt.com" in cookie["domain"]}, flow.CLOUDFLARE_COOKIE_NAMES)
                self.assertFalse(any(cookie["domain"] == "auth.openai.com" for cookie in remaining))
                self.assertTrue(any(cookie["domain"] == "mail.example.com" and cookie["name"] == "account_session" for cookie in remaining))
            finally:
                await context.close()
                await browser.close()

    async def test_storage_cleanup_failure_stops_without_visiting_fallback_pages(self):
        page = SimpleNamespace(close=AsyncMock(), goto=AsyncMock())
        context = SimpleNamespace(cookies=AsyncMock(return_value=[]), new_page=AsyncMock(return_value=page),
                                  new_cdp_session=AsyncMock(side_effect=RuntimeError("CDP unavailable")))
        with self.assertRaisesRegex(RuntimeError, "CDP unavailable"):
            await flow.clear_openai_state(context)
        page.goto.assert_not_awaited()
        page.close.assert_awaited_once()


class OAuthChallengeTests(unittest.IsolatedAsyncioTestCase):
    async def run_oauth(self, *, cleared=True, headless=False):
        page = SimpleNamespace(url="https://auth.openai.com/add-phone", route=AsyncMock(), unroute=AsyncMock(),
                               goto=AsyncMock(), wait_for_load_state=AsyncMock())
        self.page = page
        callback_handler = None
        oauth_state = ""

        async def install_route(_pattern, handler):
            nonlocal callback_handler
            callback_handler = handler

        async def navigate(url, **_kwargs):
            nonlocal oauth_state
            oauth_state = parse_qs(urlparse(url).query)["state"][0]

        async def verify_phone(_page):
            self.wait.assert_awaited_once()
            route = SimpleNamespace(fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())
            request = SimpleNamespace(url=codex_oauth.CODEX_REDIRECT_URI + "?" + urlencode({"code": "fixture", "state": oauth_state}))
            await callback_handler(route, request)

        async def fast_wait(future, **_kwargs):
            if future.done():
                return await future
            raise asyncio.TimeoutError

        page.route.side_effect = install_route
        page.goto.side_effect = navigate
        self.phone = AsyncMock(side_effect=verify_phone)
        self.wait = AsyncMock(return_value=cleared)
        if headless:
            self.wait.side_effect = flow.HeadlessBlockedError("fixture challenge requires manual verification")
        with patch.object(flow, "_detect_cloudflare_challenge", AsyncMock(side_effect=[True, False])), \
             patch.object(flow, "_wait_for_cloudflare_clear", self.wait), \
             patch.object(codex_oauth, "exchange_code", Mock(return_value={"access_token": "fixture", "refresh_token": "fixture"})), \
             patch.object(codex_oauth.asyncio, "wait_for", fast_wait), \
             patch.object(codex_oauth.asyncio, "sleep", AsyncMock()), \
             patch.object(codex_oauth, "_try_click_consent", AsyncMock()) as consent, \
             patch.object(codex_oauth, "_fill_email_on_login", AsyncMock()) as email:
            error = None
            result = None
            try:
                result = await codex_oauth.run_codex_oauth(
                    page, account_email="fixture@example.com", fetch_code=AsyncMock(), phone_verifier=self.phone,
                    timeout=5, allow_manual_cloudflare=not headless, cloudflare_timeout_seconds=42,
                )
            except Exception as exc:
                error = exc
        consent.assert_not_awaited()
        email.assert_not_awaited()
        page.goto.assert_awaited_once()
        page.unroute.assert_awaited_once()
        return result, error

    async def test_oauth_waits_for_manual_challenge_before_any_phone_purchase(self):
        result, error = await self.run_oauth()
        self.assertIsNone(error)
        self.assertEqual(result["access_token"], "fixture")
        self.phone.assert_awaited_once()
        self.assertEqual(self.wait.call_args.kwargs["timeout_seconds"], 42)
        self.assertTrue(self.wait.call_args.kwargs["allow_manual_cloudflare"])

    async def test_stuck_challenge_stops_without_purchasing_or_restarting_authorization(self):
        _result, error = await self.run_oauth(cleared=False)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("Cloudflare", str(error))
        self.phone.assert_not_awaited()

    async def test_headless_challenge_is_reported_without_purchasing_a_phone(self):
        _result, error = await self.run_oauth(headless=True)
        self.assertIsInstance(error, flow.HeadlessBlockedError)
        self.phone.assert_not_awaited()
        self.assertFalse(self.wait.call_args.kwargs["allow_manual_cloudflare"])


if __name__ == "__main__":
    unittest.main()
