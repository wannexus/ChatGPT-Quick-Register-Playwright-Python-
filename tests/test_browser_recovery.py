"""浏览器被关闭 / Cloudflare 挑战页 的回归测试。

真实事故（2026-09-29 15:00 的一轮注册）：

1. 首页在这一台机器+当前代理上会被 Cloudflare 托管挑战拦下约 30s：标题被本地化成
   泰语 `รอสักครู่...`（英文是 `Just a moment...`），页面上根本没有 Sign up 按钮。
   旧实现只给点击 Sign up 25s 预算，于是「找不到按钮」——实测挑战在放弃后 2s 就放行了。
   用户看到窗口像卡住，把窗口关掉 → Playwright 抛
   `TargetClosedError: Locator.wait_for: Target page, context or browser has been closed`，
   整轮注册直接作废（邮箱已建、号码已买）。

2. 这里覆盖两件事：
   - Cloudflare 挑战必须被识别（不依赖英文标题），并且要等它过去而不是白烧预算；
   - 「浏览器没了」必须能和普通页面报错区分开，触发自动重开浏览器重试（复用邮箱）。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import register
from core import flow


# --- 真实抓包（/tmp/cf.html，2026-09-29 15:0x 从 chatgpt.com 首页抓的托管挑战页）里
# 出现过的令牌；标题换成本地化的泰语，正文不含任何英文提示，用来证明判定不靠文案。
THAI_CHALLENGE_PAGE = """<!doctype html>
<html><head><title>รอสักครู่...</title>
<script>window._cf_chl_opt = {cvId: "3", cType: "managed", ray: "VERIFY"};</script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=VERIFY"></script>
</head><body><div id="challenge-running"><span>รอสักครู่...</span></div></body></html>
"""

# 只有 DOM 结构（没有任何 CF 令牌/文案），用于验证兜底探测。
DOM_ONLY_CHALLENGE_PAGE = """<!doctype html>
<html><head><title>รอสักครู่...</title></head>
<body><div class="cf-turnstile" data-sitekey="x"></div></body></html>
"""

# 2026-09-29 17:35 真机抓包（output/debug/step3-no-password-or-verify-page-*.html）：
# 指纹画像轮到韩语后，step3 中间页标题是 "잠시만 기다리십시오…"，正文是
# 「验证成功，等 auth.openai.com 回包」。旧 step3 只盯 URL，把它当成「找不到密码页」。
KOREAN_STEP3_CHALLENGE_PAGE = """<!doctype html>
<html dir="ltr"><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="360">
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=a429fe8d496daccf"></script>
<title>잠시만 기다리십시오…</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/b/d76008a69eab/api.js?onload=zaERg6&amp;render=explicit" async defer crossorigin="anonymous"></script>
</head>
<body>
<div class="container">
  <div class="data">
    <div>auth.openai.com 보안 확인 수행 중</div>
    <div>이 웹 사이트는 보안 서비스를 사용하여 악의적인 봇으로부터 보호합니다.</div>
    <div>이 페이지는 웹 사이트에서 사용자가 봇이 아님을 확인하는 동안에만 표시됩니다.</div>
    <div>확인에 성공했습니다.</div>
    <div>auth.openai.com 응답을 기다리는 중</div>
    <div>Enable JavaScript and cookies to continue</div>
  </div>
</div>
</body></html>
"""

NORMAL_PAGE = """<!doctype html>
<html><head><title>ChatGPT: Chat, Work, Create & Code with AI</title></head>
<body><button>Log in</button><button>Sign up</button></body></html>
"""

# 真机抓到的波兰语未登录首页（按钮没有 data-testid，文案全本地化）：
# 头部/首屏是 "Zarejestruj się za darmo"（免费注册），登录入口是 href 固定的 /auth/login。
POLISH_HOME_PAGE = """<!doctype html>
<html><head><title>ChatGPT: Chat, Work, Create &amp; Code with AI</title></head>
<body>
  <a href="https://chatgpt.com/auth/login">Zaloguj się</a>
  <button>Zaloguj się</button>
  <button>Zarejestruj się za darmo</button>
  <a href="https://chatgpt.com/auth/login_with?connection=google-oauth2">Kontynuuj z kontem Google</a>
</body></html>
"""

# 未收录语言的首页：有 /auth/login 入口（说明首页已渲染）但没有可匹配的注册文案。
CZECH_HOME_PAGE = """<!doctype html>
<html><head><title>ChatGPT</title></head>
<body>
  <a href="https://chatgpt.com/auth/login">Přihlásit se</a>
  <button>Vytvořit účet</button>
</body></html>
"""


class TargetClosedError(Exception):
    """和 Playwright 的 TargetClosedError 同名（判定按类名，不 import 私有模块）。"""


class BrowserGoneDetectionTests(unittest.TestCase):
    def test_playwright_target_closed_message_is_detected(self):
        # 用户那轮的原始报错文本
        error = RuntimeError(
            "Locator.wait_for: Target page, context or browser has been closed"
        )
        self.assertTrue(register._is_browser_gone(error))
        self.assertTrue(register._is_browser_gone(TargetClosedError("boom")))
        self.assertTrue(register._is_browser_gone(RuntimeError("Browser has been closed")))
        self.assertTrue(register._is_browser_gone(RuntimeError("Connection closed")))

    def test_wrapped_target_closed_is_detected_through_the_cause_chain(self):
        inner = TargetClosedError("Target page, context or browser has been closed")
        outer = RuntimeError("找不到 Sign up 按钮，且 fallback 跳转也失败")
        outer.__cause__ = inner
        self.assertTrue(register._is_browser_gone(outer))

    def test_ordinary_failures_are_not_treated_as_browser_death(self):
        for error in (
            None,
            TimeoutError("等待邮箱输入框超时"),
            RuntimeError("HTTP 502"),
            RuntimeError("页面提示验证码发到了 WhatsApp"),
        ):
            self.assertFalse(register._is_browser_gone(error), error)


class BrowserRetryLoopTests(unittest.IsolatedAsyncioTestCase):
    """窗口被关掉时应该自动重开浏览器、复用邮箱重试，而不是丢掉这一轮。"""

    def _args(self, tmp: str, *extra: str):
        return register.parse_args([
            "--out", tmp,
            "--email-source", "mhjc",
            "--code-source", "mhjc",
            "--auth-mode", "otp",
            "--mhjc-api-key", "dummy",
            "--count", "1",
            "--cooldown", "0",
            *extra,
        ])

    async def _run(self, args, error, *, succeed_on_attempt: int | None = None):
        calls: list[bool] = []

        async def fake_open(*_a, **_kw):
            return (object(), None, object(), None, None)

        async def fake_close(*_a, **_kw):
            return None

        async def fake_run(*_a, **kwargs):
            calls.append(bool(kwargs.get("reuse_email")))
            if succeed_on_attempt is not None and len(calls) == succeed_on_attempt:
                return register.RunResult(email="reuse@mhjc.edu.kg", status="ok")
            raise error

        with patch.object(register, "_open_browser_context", AsyncMock(side_effect=fake_open)), \
             patch.object(register, "_close_browser_context", AsyncMock(side_effect=fake_close)), \
             patch.object(register, "_run_one_account", AsyncMock(side_effect=fake_run)):
            code = await register.run_register(args)
        return calls, code

    async def test_browser_death_retries_once_and_reuses_the_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(tmp)
            calls, code = await self._run(
                args,
                RuntimeError("Target page, context or browser has been closed"),
                succeed_on_attempt=2,
            )
        self.assertEqual(calls, [False, True], "第一次正常，重试必须复用邮箱")
        self.assertEqual(code, 0)

    async def test_retry_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(tmp, "--browser-retry", "0")
            calls, code = await self._run(
                args, TargetClosedError("Target page, context or browser has been closed")
            )
        self.assertEqual(calls, [False])
        self.assertEqual(code, 0, "失败但没勾 stop-on-error 时批量整体仍返回 0")

    async def test_real_page_failures_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(tmp)
            calls, _code = await self._run(args, RuntimeError("等待邮箱输入框超时"))
        self.assertEqual(calls, [False], "普通页面错误不该重开浏览器")


class RetryProfileTests(unittest.TestCase):
    """重试复用邮箱时，姓名档案也要跟着复用（邮箱名是按姓名生成的）。"""

    def _args(self):
        return register.parse_args([
            "--email-source", "mhjc", "--code-source", "mhjc",
            "--auth-mode", "otp", "--mhjc-api-key", "dummy",
        ])

    def test_first_attempt_generates_and_stashes_a_profile(self):
        args = self._args()
        profile = register._profile_for_attempt(args, reuse_email=False, label="1/1")
        self.assertEqual(args._retry_profile, profile)
        self.assertEqual(len(profile), 3)

    def test_retry_reuses_the_stashed_profile(self):
        args = self._args()
        first = register._profile_for_attempt(args, reuse_email=False, label="1/1")
        again = register._profile_for_attempt(args, reuse_email=True, label="1/1")
        self.assertIs(first, again, "重试必须复用同一份姓名/生日档案")

    def test_retry_without_a_stashed_profile_still_generates_one(self):
        args = self._args()
        profile = register._profile_for_attempt(args, reuse_email=True, label="1/1")
        self.assertEqual(len(profile), 3)
        self.assertEqual(args._retry_profile, profile)


class CloudflareWaitTests(unittest.IsolatedAsyncioTestCase):
    """Cloudflare 挑战页：必须认出来，并且等它过去而不是白烧点击预算。"""

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True)

    async def asyncTearDown(self):
        try:
            await self.browser.close()
        finally:
            await self._pw.stop()

    async def _page(self, html: str):
        page = await self.browser.new_page()
        await page.set_content(html)
        return page

    async def test_localized_challenge_page_is_detected_without_english_text(self):
        page = await self._page(THAI_CHALLENGE_PAGE)
        self.assertTrue(await flow._detect_cloudflare_challenge(page))

    async def test_dom_only_challenge_page_is_detected(self):
        page = await self._page(DOM_ONLY_CHALLENGE_PAGE)
        self.assertTrue(await flow._detect_cloudflare_challenge(page))

    async def test_normal_homepage_is_not_a_challenge(self):
        page = await self._page(NORMAL_PAGE)
        self.assertFalse(await flow._detect_cloudflare_challenge(page))

    async def test_wait_returns_immediately_on_a_clean_page(self):
        page = await self._page(NORMAL_PAGE)
        with patch.object(flow, "save_debug_artifacts", AsyncMock()):
            self.assertTrue(await flow._wait_for_cloudflare_clear(page, timeout_seconds=5))

    async def test_wait_times_out_on_a_stuck_challenge(self):
        page = await self._page(THAI_CHALLENGE_PAGE)
        with patch.object(flow, "save_debug_artifacts", AsyncMock()):
            cleared = await flow._wait_for_cloudflare_clear(
                page, timeout_seconds=0.6, allow_manual_cloudflare=True, poll=0.1
            )
        self.assertFalse(cleared)

    async def test_headless_raises_instead_of_waiting_for_a_human(self):
        page = await self._page(THAI_CHALLENGE_PAGE)
        with patch.object(flow, "save_debug_artifacts", AsyncMock()):
            with self.assertRaises(flow.HeadlessBlockedError):
                await flow._wait_for_cloudflare_clear(
                    page, timeout_seconds=30, allow_manual_cloudflare=False
                )

    async def test_signup_click_gives_up_instead_of_looping_forever(self):
        page = await self._page(THAI_CHALLENGE_PAGE)
        with patch.object(flow, "save_debug_artifacts", AsyncMock()):
            clicked = await flow._try_click_signup(
                page,
                total_timeout=25,
                allow_manual_cloudflare=True,
                cloudflare_timeout_seconds=0.6,
            )
        self.assertFalse(clicked)

    async def test_signup_click_works_once_the_challenge_clears(self):
        page = await self._page(THAI_CHALLENGE_PAGE)
        probes = {"count": 0}

        async def fake_detect(*_a, **_kw):
            probes["count"] += 1
            if probes["count"] == 1:
                return True
            # 第二次探测：挑战已经放行，页面换成真实首页
            await page.set_content(NORMAL_PAGE)
            return False

        with patch.object(flow, "save_debug_artifacts", AsyncMock()), \
             patch.object(flow, "_detect_cloudflare_challenge", side_effect=fake_detect):
            clicked = await flow._try_click_signup(
                page, total_timeout=25, allow_manual_cloudflare=True,
                cloudflare_timeout_seconds=5,
            )
        self.assertTrue(clicked)
        self.assertGreaterEqual(probes["count"], 2)

    async def test_korean_step3_challenge_page_is_detected(self):
        """韩语标题 + 「验证成功等回包」也必须算挑战，不能当成业务页。"""
        page = await self._page(KOREAN_STEP3_CHALLENGE_PAGE)
        self.assertTrue(await flow._detect_cloudflare_challenge(page))

    async def test_wait_advances_turnstile_instead_of_only_sleeping(self):
        page = await self._page(KOREAN_STEP3_CHALLENGE_PAGE)
        advances = {"n": 0}
        detects = {"n": 0}

        async def fake_advance(_page):
            advances["n"] += 1
            return False

        async def fake_detect(_page):
            detects["n"] += 1
            # 前两次探测是挑战中；推进过一次之后就放行
            return detects["n"] <= 2

        with patch.object(flow, "save_debug_artifacts", AsyncMock()), \
             patch.object(flow, "_detect_cloudflare_challenge", side_effect=fake_detect), \
             patch.object(flow, "_try_advance_cloudflare", side_effect=fake_advance):
            cleared = await flow._wait_for_cloudflare_clear(
                page, timeout_seconds=2, allow_manual_cloudflare=True, poll=0.05
            )
        self.assertTrue(cleared)
        self.assertGreaterEqual(advances["n"], 1, "等待期间必须尝试推进 Turnstile，而不是干等")


class CloudflareUrlMatchTests(unittest.IsolatedAsyncioTestCase):
    """挑战页 URL 往往已经命中 success_patterns（auth.openai.com），不能当成功。"""

    async def test_url_match_is_rejected_while_the_challenge_is_showing(self):
        page = SimpleNamespace(
            url="https://auth.openai.com/api/accounts/authorize?prompt=login",
        )
        with patch.object(flow, "_detect_cloudflare_challenge", AsyncMock(return_value=True)):
            self.assertFalse(
                await flow._url_ready(page, [re.compile(r"auth\.openai\.com/", re.I)])
            )
        with patch.object(flow, "_detect_cloudflare_challenge", AsyncMock(return_value=False)):
            self.assertTrue(
                await flow._url_ready(page, [re.compile(r"auth\.openai\.com/", re.I)])
            )

    async def test_wait_url_settles_the_challenge_before_accepting_the_url(self):
        events = []

        class Page:
            url = "https://auth.openai.com/log-in/password"

            async def wait_for_load_state(self, *_a, **_kw):
                return None

        page = Page()
        detects = iter([True, True, False, False])

        async def fake_detect(_p):
            return next(detects)

        async def fake_wait(_p, **kwargs):
            events.append(kwargs.get("label"))
            return True

        with patch.object(flow, "_detect_cloudflare_challenge", side_effect=fake_detect), \
             patch.object(flow, "_wait_for_cloudflare_clear", side_effect=fake_wait):
            await flow._wait_url(
                page,
                [re.compile(r"/password", re.I)],
                timeout=2000,
                allow_manual_cloudflare=True,
                cloudflare_timeout_seconds=5,
                label="step 3 等密码/验证码页",
            )
        self.assertTrue(events, "遇到挑战必须先等它过去")
        self.assertIn("step 3 等密码/验证码页-cloudflare", events[0])


class Step3CloudflareRegressionTests(unittest.TestCase):
    """源码契约：step3 / relogin 不能在挑战页上直接超时或直接报错。"""

    def setUp(self):
        self.source = Path("core/flow.py").read_text(encoding="utf-8")

    def test_step3_settles_cloudflare_before_waiting_for_the_password_url(self):
        body = self.source[self.source.index("async def step3_password"):]
        body = body[: body.index("async def _fill_verification_code")]
        settle_at = body.index("await _settle_cloudflare")
        wait_at = body.index("await _wait_url")
        self.assertLess(settle_at, wait_at, "step3 必须先等挑战过去，再等密码页 URL")

    def test_relogin_waits_through_cloudflare_instead_of_raising_immediately(self):
        body = self.source[self.source.index("async def wait_for_login_step"):]
        body = body[: body.index("async def wait_for_url_with_recovery")]
        self.assertIn('if signal == "cloudflare"', body)
        cf_at = body.index('if signal == "cloudflare"')
        cf_block = body[cf_at: cf_at + 600]
        self.assertIn("_wait_for_cloudflare_clear", cf_block)
        self.assertNotIn("请换 IP 或用有头模式重试", cf_block)


class LocalizedHomepageTests(unittest.IsolatedAsyncioTestCase):
    """未登录首页的 Sign up 按钮没有 testid，文案跟随语言（指纹画像会轮换语言）。"""

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True)

    async def asyncTearDown(self):
        try:
            await self.browser.close()
        finally:
            await self._pw.stop()

    async def _page(self, html: str):
        page = await self.browser.new_page()
        await page.set_content(html)
        return page

    async def test_logged_out_home_is_recognised_by_href_not_by_wording(self):
        self.assertTrue(await flow._logged_out_home_ready(await self._page(POLISH_HOME_PAGE)))
        self.assertTrue(await flow._logged_out_home_ready(await self._page(CZECH_HOME_PAGE)))
        self.assertFalse(await flow._logged_out_home_ready(await self._page(NORMAL_PAGE)))

    async def test_polish_signup_button_is_clicked(self):
        # 真机抓到的文案就是 "Zarejestruj się za darmo"
        page = await self._page(POLISH_HOME_PAGE)
        self.assertTrue(await flow._try_click_signup(page, total_timeout=6))

    async def test_unknown_language_gives_up_early_instead_of_burning_25s(self):
        page = await self._page(CZECH_HOME_PAGE)
        started = asyncio.get_event_loop().time()
        clicked = await flow._try_click_signup(
            page, total_timeout=25, homepage_give_up_passes=1
        )
        elapsed = asyncio.get_event_loop().time() - started
        self.assertFalse(clicked)
        self.assertLess(elapsed, 15, "首页已渲染时应当立刻回退，而不是空转到 25s")

    async def test_page_without_a_home_marker_keeps_trying_until_timeout(self):
        """没有 /auth/login 入口的页面（例如半渲染）不能提前放弃。"""
        page = await self._page(
            '<!doctype html><html><head><title>ChatGPT</title></head>'
            "<body><button>Vytvořit účet</button></body></html>"
        )
        started = asyncio.get_event_loop().time()
        clicked = await flow._try_click_signup(
            page, total_timeout=4, homepage_give_up_passes=1
        )
        elapsed = asyncio.get_event_loop().time() - started
        self.assertFalse(clicked)
        self.assertGreaterEqual(elapsed, 3, "应当把 total_timeout 用完再放弃")


class Step2CloudflareContractTests(unittest.TestCase):
    """源码契约：step2 必须在点 Sign up 之前先等挑战过去。"""

    def setUp(self):
        self.source = Path("core/flow.py").read_text(encoding="utf-8")

    def test_step2_waits_for_the_challenge_before_clicking_signup(self):
        body = self.source[self.source.index("async def step2_signup_email"):]
        body = body[:body.index("def _looks_like_chatgpt_home") if "def _looks_like_chatgpt_home" in body else 4000]
        wait_at = body.index("_wait_for_cloudflare_clear")
        click_at = body.index("_try_click_signup")
        self.assertLess(wait_at, click_at, "先等挑战过去，才轮到点击 Sign up")
        self.assertIn("_detect_cloudflare_challenge", body[:wait_at])

    def test_register_passes_a_cloudflare_timeout_and_a_retry_budget(self):
        source = Path("register.py").read_text(encoding="utf-8")
        self.assertIn("cloudflare_timeout_seconds=", source)
        self.assertIn('"--browser-retry"', source)
        self.assertIn("_is_browser_gone(failure)", source)
        self.assertIn("reuse_email=attempt > 1", source)
        self.assertIn("reuse_email: bool = False", source)


if __name__ == "__main__":
    unittest.main()
