"""6-step ChatGPT registration flow driven by Playwright."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import Page, TimeoutError as PWTimeout

from data.names import (
    Birthday,
    generate_password,
    random_birthday,
    random_first_name,
    random_last_name,
)

CHATGPT_HOME = "https://chatgpt.com/"
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
CHATGPT_SIGNUP_HINT_URL = "https://chatgpt.com/auth/login?screen_hint=signup"

# 清 cookie / localStorage 时覆盖的域名（不动 DuckDuckGo / 邮箱 等其他域）
OPENAI_COOKIE_DOMAINS = (
    "chatgpt.com",
    "chat.openai.com",
    "openai.com",
    "auth.openai.com",
    "auth0.openai.com",
    "accounts.openai.com",
)
OPENAI_STORAGE_ORIGINS = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://auth.openai.com",
)

DEBUG_DIR = Path(__file__).resolve().parent.parent / "output" / "debug"


async def save_debug_artifacts(page: Page, label: str) -> None:
    """Dump screenshot + page HTML to output/debug/ for post-mortem."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    png = DEBUG_DIR / f"{label}-{stamp}.png"
    html = DEBUG_DIR / f"{label}-{stamp}.html"
    try:
        await page.screenshot(path=str(png), full_page=True)
    except Exception as e:  # noqa: BLE001
        print(f"[debug] screenshot failed: {e}")
    try:
        html.write_text(await page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[debug] html dump failed: {e}")
    print(f"[debug] saved {png.name}, {html.name} (current url: {page.url})")

SIGNUP_BUTTON_TEXTS = [
    "sign up for free", "sign up", "signup",
    "免费注册", "注册", "创建账户", "创建账号",
    "create account", "register", "get started",
]
LOGIN_BUTTON_TEXTS = [
    "log in", "login", "sign in", "登录", "登入",
]
SIGNUP_BUTTON_CSS_SELECTORS = [
    '[data-testid*="signup" i]',
    '[data-testid*="sign-up" i]',
    'a[href*="screen_hint=signup"]',
    'a[href*="create-account"]',
    'button[aria-label*="sign up" i]',
    'a[aria-label*="sign up" i]',
]
CONTINUE_TEXTS = ["continue", "继续", "下一步", "next"]
# Words that, when present in a "Continue" button, indicate it's actually
# a 3rd-party OAuth provider button (Continue with Google etc.) and must be skipped.
PROVIDER_BLACKLIST = (
    "google", "apple", "microsoft", "phone", "facebook", "github",
    "wechat", "twitter", "x ", " x", "linkedin", "okta",
)

RETRY_TEXTS = ["重试", "再试一次", "retry", "try again"]

# 「使用一次性验证码代替密码」类按钮/链接的文本片段
OTP_SWITCH_PATTERNS = [
    re.compile(r"use\s+a?\s*(one[\s-]?time|verification)\s+code", re.IGNORECASE),
    re.compile(r"send\s+(a\s+)?(verification|one[\s-]?time)\s+code", re.IGNORECASE),
    re.compile(r"sign\s+in\s+with\s+(a\s+)?code", re.IGNORECASE),
    re.compile(r"email\s+(me\s+)?a\s+code", re.IGNORECASE),
    re.compile(r"magic\s+link", re.IGNORECASE),
    re.compile(r"使用[一壹]次性(验证)?(码|代码)|改用[一壹]次性|改用验证码|使用验证码登录|发送验证码"),
    re.compile(r"通过(邮箱|邮件)验证码"),
]
SOFT_ERROR_PATTERNS = [
    re.compile(r"操作超时|operation\s+timed\s+out", re.IGNORECASE),
    re.compile(r"糟糕[，,]?\s*出错了|something\s+went\s+wrong", re.IGNORECASE),
    re.compile(r"route\s*[_-]?\s*error", re.IGNORECASE),
    re.compile(r"\b40[45]\b", re.IGNORECASE),  # 405 / 404
]
HARD_BLOCK_PATTERNS = [
    re.compile(r"max[_-]?check[_-]?attempts", re.IGNORECASE),
    re.compile(r"user[_-]?already[_-]?exists", re.IGNORECASE),
]
COMPLETE_TEXTS = [
    "agree", "同意", "完成", "continue", "继续", "create account", "完成帐户创建", "创建账号",
]

REG_SUCCESS_WAIT_SECONDS = 20

CodeFetcher = Callable[[], Awaitable[str]]


def _ci_text_locator(page: Page, texts: list[str]):
    """Locator that matches any of the given texts case-insensitively, on
    button-like elements."""
    pattern = "|".join(re.escape(t) for t in texts)
    return page.locator(
        "button, a, [role=button], input[type=submit]"
    ).filter(has_text=re.compile(pattern, re.IGNORECASE))


async def _click_first(page: Page, texts: list[str], *, timeout: int = 15000):
    locator = _ci_text_locator(page, texts).first
    await locator.wait_for(state="visible", timeout=timeout)
    await locator.click()


async def _click_strict_continue(page: Page, *, timeout: int = 8000) -> bool:
    """Click a 'Continue' button while skipping Continue with Google/Apple/etc.

    Order:
      1) button[type=submit]:visible (skip if its text mentions a provider)
      2) Buttons whose text is exactly Continue / 继续 / 下一步 / Next
         (with no provider keyword)
    """
    deadline = asyncio.get_event_loop().time() + timeout / 1000

    async def _safe_text(loc) -> str:
        try:
            return (await loc.inner_text()).strip().lower()
        except Exception:
            return ""

    while asyncio.get_event_loop().time() < deadline:
        # Strategy 1: type=submit
        submits = page.locator("button[type=submit], input[type=submit]")
        try:
            n = await submits.count()
        except Exception:
            n = 0
        for i in range(n):
            cand = submits.nth(i)
            try:
                if not await cand.is_visible():
                    continue
                txt = await _safe_text(cand)
                if any(p in txt for p in PROVIDER_BLACKLIST):
                    continue
                await cand.click(timeout=2000)
                print(f"[continue] clicked button[type=submit]  text={txt!r}")
                return True
            except Exception:
                continue

        # Strategy 2: exact text "Continue"
        exact = re.compile(
            r"^\s*(" + "|".join(re.escape(t) for t in CONTINUE_TEXTS) + r")\s*$",
            re.IGNORECASE,
        )
        candidates = page.locator("button, a, [role=button]")
        try:
            n2 = await candidates.count()
        except Exception:
            n2 = 0
        for i in range(n2):
            cand = candidates.nth(i)
            try:
                if not await cand.is_visible():
                    continue
                txt = await _safe_text(cand)
                if not txt or not exact.fullmatch(txt):
                    continue
                if any(p in txt for p in PROVIDER_BLACKLIST):
                    continue
                await cand.click(timeout=2000)
                print(f"[continue] clicked strict-text button  text={txt!r}")
                return True
            except Exception:
                continue

        await asyncio.sleep(0.5)

    return False


async def _page_text(page: Page) -> str:
    try:
        return await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        return ""


_POLICY_PAGE_RE = re.compile(
    r"openai\.com/(?:[a-z]{2}(?:-[A-Za-z]+)*/)?"
    r"(policies|terms|privacy|help|enterprise|brand|safety|trust|charter|"
    r"plugins|security|business|stories|research|api|sora|index|news|blog)",
    re.IGNORECASE,
)
_AUTH_HOST_RE = re.compile(r"^https?://(auth|auth0|accounts)\.openai\.com/", re.IGNORECASE)


def _is_policy_url(url: str) -> bool:
    return bool(_POLICY_PAGE_RE.search(url or ""))


def _is_safe_retry_href(href: str) -> bool:
    if not href:
        return False
    if href.startswith("#") or href.startswith("/") or href.startswith("?"):
        return True
    if href.startswith("javascript:"):
        return True
    return ("auth.openai.com" in href
            or "auth0.openai.com" in href
            or "accounts.openai.com" in href)


async def _safe_get_text(loc) -> str:
    try:
        return (await loc.inner_text()).strip().lower()
    except Exception:
        return ""


async def _click_retry_if_present(page: Page) -> bool:
    """Strictly click a retry control. Skips footer links, privacy/terms links,
    and any anchor that would navigate away from auth.openai.com. If the click
    accidentally lands on a policy page, go_back() to undo."""
    exact_pattern = re.compile(
        r"^\s*(" + "|".join(re.escape(t) for t in RETRY_TEXTS) + r")\s*$",
        re.IGNORECASE,
    )

    async def _attempt_click(loc, *, kind: str) -> bool:
        try:
            if not await loc.is_visible():
                return False
        except Exception:
            return False
        pre_url = page.url
        try:
            await loc.click(timeout=4000)
        except Exception as e:  # noqa: BLE001
            print(f"[recover] click ({kind}) 失败: {e}")
            return False
        await asyncio.sleep(1.0)
        post_url = page.url
        if _is_policy_url(post_url):
            print(f"[recover] WARN 点 {kind} 后跳到了 policy/help 页 ({post_url})，回退")
            try:
                await page.go_back(wait_until="commit", timeout=10000)
            except Exception:
                try:
                    await page.goto(pre_url, wait_until="commit", timeout=10000)
                except Exception as e2:
                    print(f"[recover] 回退失败: {e2}")
            return False
        print(f"[recover] 已点击 {kind}（重试）")
        return True

    # Pass 1: <button> / <input type=submit> / [role=button] with EXACT retry text
    btns = page.locator("button, input[type=submit], [role=button]")
    try:
        n_btn = await btns.count()
    except Exception:
        n_btn = 0
    for i in range(n_btn):
        cand = btns.nth(i)
        txt = await _safe_get_text(cand)
        if not txt or not exact_pattern.fullmatch(txt):
            continue
        if await _attempt_click(cand, kind="button"):
            return True

    # Pass 2: <a> with EXACT retry text AND a safe href (same-origin / auth.*)
    anchors = page.locator("a")
    try:
        n_a = await anchors.count()
    except Exception:
        n_a = 0
    for i in range(n_a):
        cand = anchors.nth(i)
        txt = await _safe_get_text(cand)
        if not txt or not exact_pattern.fullmatch(txt):
            continue
        try:
            href = await cand.get_attribute("href") or ""
        except Exception:
            href = ""
        if href and not _is_safe_retry_href(href):
            print(f"[recover] skip <a {href!r}> (looks like an external link)")
            continue
        if await _attempt_click(cand, kind="link"):
            return True

    return False


async def _detect_hard_block(page: Page) -> str | None:
    body = await _page_text(page)
    for pat in HARD_BLOCK_PATTERNS:
        m = pat.search(body)
        if m:
            return m.group(0)
    return None


async def _detect_soft_error(page: Page) -> bool:
    body = await _page_text(page)
    return any(pat.search(body) for pat in SOFT_ERROR_PATTERNS)


async def wait_for_url_with_recovery(
    page: Page,
    success_patterns: list[re.Pattern],
    *,
    total_timeout_seconds: float = 120,
    refill_password: str | None = None,
    label: str = "",
    max_retries: int = 3,
) -> None:
    """Poll until page.url matches any of `success_patterns`. Auto-click 重试
    on soft error pages (Operation timed out / Route Error / 405). On hard
    blocks (max_check_attempts / user_already_exists) raise immediately. If
    `refill_password` is provided and we land back on the password page, refill
    and resubmit it."""
    deadline = asyncio.get_event_loop().time() + total_timeout_seconds
    retry_count = 0
    same_state_streak = 0  # 连续多少轮卡在「重试后还在错误页」

    while asyncio.get_event_loop().time() < deadline:
        url = page.url
        if any(p.search(url) for p in success_patterns):
            return

        # Hard block first
        hard = await _detect_hard_block(page)
        if hard:
            raise RuntimeError(
                f"{label or 'OAuth'} 硬阻断：页面命中关键字 {hard!r}"
                f"（OpenAI 风控/IP 限速；换 VPN 节点或等 15-30 分钟再试）"
            )

        # Soft error → click retry, 但限频限次
        if await _detect_soft_error(page):
            if retry_count >= max_retries:
                raise RuntimeError(
                    f"{label or 'OAuth'} 已重试 {retry_count} 次仍出错。"
                    f"这通常是 OpenAI 后端被限速：换 IP / 关掉脚本等 30 分钟再试。"
                )
            if await _click_retry_if_present(page):
                retry_count += 1
                # 等 5 秒看是否真的离开错误页
                stuck_until = asyncio.get_event_loop().time() + 5
                while asyncio.get_event_loop().time() < stuck_until:
                    if any(p.search(page.url) for p in success_patterns):
                        return
                    if not await _detect_soft_error(page):
                        same_state_streak = 0
                        break
                    await asyncio.sleep(0.5)
                else:
                    same_state_streak += 1
                    if same_state_streak >= 2:
                        raise RuntimeError(
                            f"{label or 'OAuth'} 连续 {same_state_streak} 次「重试」后页面还停在错误页，"
                            f"判定为 OpenAI 限速；换 IP / 等 30 分钟再试。"
                        )
                continue
            # error keywords present but no button — wait a bit more
            await asyncio.sleep(2)
            continue

        # Re-fill password if we landed back on password page
        if refill_password and re.search(r"/password", url, re.IGNORECASE):
            try:
                pw_loc = page.locator("input[type=password]").first
                if await pw_loc.count() > 0 and await pw_loc.is_visible():
                    current = ""
                    try:
                        current = await pw_loc.input_value()
                    except Exception:
                        pass
                    if not current:
                        print("[recover] 密码页再次出现，重填密码")
                        await pw_loc.click()
                        await pw_loc.fill(refill_password)
                        await _submit_after_input(pw_loc, page)
                        await asyncio.sleep(2)
                        continue
            except Exception as e:
                print(f"[recover] 重填密码失败: {e}")

        await asyncio.sleep(0.8)

    await save_debug_artifacts(page, f"{label or 'wait'}-timeout")
    raise TimeoutError(
        f"{label or 'wait'} 超时（{total_timeout_seconds}s），当前 url: {page.url}"
    )


async def _submit_after_input(input_locator, page: Page, *, post_wait_seconds: float = 1.5) -> None:
    """Press Enter on the input; if no progress, fall back to a strict
    Continue-button click."""
    pre_url = page.url
    try:
        await input_locator.press("Enter")
        print("[submit] pressed Enter on input")
    except Exception as e:  # noqa: BLE001
        print(f"[submit] press Enter failed: {e}")

    # Give the page a moment to react (navigation / DOM swap).
    await asyncio.sleep(post_wait_seconds)

    # If we're still showing the same input as the active focus, the form
    # didn't submit on Enter — try a strict Continue click as a fallback.
    if page.url != pre_url:
        return
    try:
        still_visible = await input_locator.is_visible()
    except Exception:
        still_visible = False
    if still_visible:
        # Heuristic: input still there, attempt button click as fallback.
        clicked = await _click_strict_continue(page, timeout=6000)
        if not clicked:
            # Last resort: do nothing here; caller will see no progress and dump artifacts.
            print("[submit] strict Continue click also failed (will rely on URL/DOM check)")


async def _submit_email_form(page: Page, email_input, email: str) -> None:
    """Fill and submit the current auth email form without touching provider buttons."""
    await email_input.click()
    await email_input.fill(email)
    try:
        value = await email_input.input_value()
    except Exception:
        value = ""
    if value.strip().lower() != email.strip().lower():
        await email_input.fill(email)

    form_id = ""
    try:
        form_id = await email_input.get_attribute("form") or ""
    except Exception:
        pass

    clicked = False
    if form_id:
        submit = page.locator(
            f'button[type=submit][form="{form_id}"][name="intent"][value="email"], '
            f'input[type=submit][form="{form_id}"][name="intent"][value="email"], '
            f'button[type=submit][form="{form_id}"]'
        ).first
        try:
            if await submit.count() > 0 and await submit.is_visible():
                await submit.click(timeout=5000)
                print("[submit-email] clicked email form submit")
                clicked = True
        except Exception as e:
            print(f"[submit-email] form submit click failed: {e}")

    if not clicked:
        try:
            submitted = await email_input.evaluate(
                """input => {
                    const form = input.form || input.closest('form');
                    if (!form) return false;
                    if (typeof form.requestSubmit === 'function') {
                        const emailButton = form.querySelector('button[name="intent"][value="email"], input[name="intent"][value="email"]');
                        form.requestSubmit(emailButton || undefined);
                    } else {
                        form.submit();
                    }
                    return true;
                }"""
            )
            if submitted:
                print("[submit-email] requestSubmit email form")
                clicked = True
        except Exception as e:
            print(f"[submit-email] requestSubmit failed: {e}")

    if not clicked:
        await _submit_after_input(email_input, page)

    await asyncio.sleep(2.0)


async def _wait_url(page: Page, patterns: list[re.Pattern], *, timeout: int = 30000):
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        url = page.url
        if any(p.search(url) for p in patterns):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PWTimeout:
                pass
            return
        await asyncio.sleep(0.4)
    raise TimeoutError(
        f"等待 URL 超时，期望匹配：{[p.pattern for p in patterns]}（当前：{page.url}）"
    )


async def clear_openai_state(context, *, also_storage: bool = True) -> None:
    """Clear cookies (and optionally localStorage / sessionStorage) for all
    OpenAI / ChatGPT domains. Other origins (DuckDuckGo, email providers) are
    untouched."""
    # ---- cookies ----
    total_to_clear = 0
    try:
        all_cookies = await context.cookies()
        for c in all_cookies:
            d = (c.get("domain") or "").lstrip(".").lower()
            if any(d == td or d.endswith("." + td) for td in OPENAI_COOKIE_DOMAINS):
                total_to_clear += 1
    except Exception as e:  # noqa: BLE001
        print(f"[clear] 列出 cookie 失败: {e}")

    cleared = 0
    for domain in OPENAI_COOKIE_DOMAINS:
        try:
            await context.clear_cookies(domain=domain)
            # 子域 catch-all：再来一遍 .domain
            await context.clear_cookies(domain="." + domain)
            cleared += 1
        except Exception:
            continue
    print(f"[clear] cookies: 已扫描 {total_to_clear} 个目标 cookie，清理了 {cleared} 个域")

    # ---- localStorage / sessionStorage / IndexedDB ----
    if not also_storage:
        return
    tmp_page = None
    try:
        tmp_page = await context.new_page()
        for origin in OPENAI_STORAGE_ORIGINS:
            try:
                await tmp_page.goto(origin + "/blank-clear", wait_until="commit", timeout=10000)
            except Exception:
                # 大多数 OpenAI 子域 / 路径会 4xx 但仍能拿到正确 origin 上下文
                pass
            try:
                await tmp_page.evaluate(
                    "() => { try { localStorage.clear(); } catch(_){} "
                    "try { sessionStorage.clear(); } catch(_){} }"
                )
            except Exception:
                pass
        print(f"[clear] localStorage/sessionStorage 已清理: {', '.join(OPENAI_STORAGE_ORIGINS)}")
    except Exception as e:  # noqa: BLE001
        print(f"[clear] storage 清理失败（一般无所谓）: {e}")
    finally:
        if tmp_page is not None:
            try:
                await tmp_page.close()
            except Exception:
                pass


async def step1_open(page: Page) -> None:
    print("[step 1] 打开 chatgpt.com")
    await page.goto(CHATGPT_HOME, wait_until="commit")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
    except PWTimeout:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass


async def _try_click_signup(page: Page, *, total_timeout: float = 25) -> bool:
    """Best-effort: try multiple ways to click the Sign up entry on chatgpt.com."""
    deadline = asyncio.get_event_loop().time() + total_timeout
    while asyncio.get_event_loop().time() < deadline:
        # 1) CSS selectors with stable testids / hrefs
        for sel in SIGNUP_BUTTON_CSS_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[step 2] clicked via selector: {sel}")
                    return True
            except Exception:
                continue
        # 2) Visible text fallback
        try:
            await _click_first(page, SIGNUP_BUTTON_TEXTS, timeout=2000)
            print("[step 2] clicked via text matcher")
            return True
        except (PWTimeout, TimeoutError):
            pass
        await asyncio.sleep(1)
    return False


EMAIL_INPUT_SELECTOR = (
    'input[type=email], input[name=email], '
    'input[autocomplete=email], input[autocomplete=username], '
    'input[placeholder*=mail i]'
)
LOGIN_EMAIL_INPUT_SELECTOR = (
    "input[type=email], input[name*=email i], input[name*=username i], "
    "input[autocomplete=email], input[autocomplete=username], "
    "input[id*=email i], input[id*=username i], input[placeholder*=mail i]"
)


async def _wait_for_email_input_anywhere(page: Page, timeout_seconds: float = 30):
    """Wait for an email input to be visible — either inside an inline modal on
    chatgpt.com, or after redirecting to auth.openai.com."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        loc = page.locator(EMAIL_INPUT_SELECTOR).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            pass
        await asyncio.sleep(0.4)
    raise TimeoutError("等待邮箱输入框超时")


async def step2_signup_email(page: Page, email: str) -> None:
    print(f"[step 2] 点 Sign up 并填邮箱：{email}")

    clicked = await _try_click_signup(page, total_timeout=25)
    if not clicked:
        # chatgpt.com itself uses this URL when you click the homepage Sign up
        # button. Going there directly skips the inline modal entirely.
        print(f"[step 2] 未找到 Sign up 按钮，回退跳转 {CHATGPT_SIGNUP_HINT_URL}")
        try:
            await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
        except Exception as e:  # noqa: BLE001
            await save_debug_artifacts(page, "step2-signup-fallback-failed")
            raise RuntimeError(f"找不到 Sign up 按钮，且 fallback 跳转也失败：{e}") from e

    # The Sign up button on chatgpt.com may either:
    #   (a) open an inline modal on chatgpt.com with an email input, or
    #   (b) navigate to auth.openai.com.
    # Wait for an email input visible in either case.
    try:
        email_input = await _wait_for_email_input_anywhere(page, timeout_seconds=25)
    except TimeoutError:
        # Still nothing — try the explicit signup URL (only if we haven't already)
        if "screen_hint=signup" not in page.url and "auth.openai.com" not in page.url:
            print(f"[step 2] 当前页 ({page.url}) 没出现邮箱输入框，跳 {CHATGPT_SIGNUP_HINT_URL}")
            try:
                await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
            except Exception as e:  # noqa: BLE001
                await save_debug_artifacts(page, "step2-fallback-after-modal")
                raise RuntimeError(f"模态框未出现，跳 fallback URL 也失败：{e}") from e
            email_input = await _wait_for_email_input_anywhere(page, timeout_seconds=30)
        else:
            await save_debug_artifacts(page, "step2-no-email-input")
            raise

    print(f"[step 2] 找到邮箱输入框，url={page.url}")
    await email_input.click()
    await email_input.fill(email)
    await asyncio.sleep(0.3)

    # Submit by Enter first — avoids the "Continue with Google/Apple/..." trap
    # on chatgpt.com signup modal.
    await _submit_after_input(email_input, page)

    # Now wait for the OAuth redirect to land on auth.openai.com (password page
    # or email-verification page). Step 3 will pick up from here.
    try:
        await _wait_url(
            page,
            [re.compile(r"^https://(auth|auth0|accounts)\.openai\.com/")],
            timeout=45000,
        )
        print(f"[step 2] 已进入 OAuth 域，url={page.url}")
    except TimeoutError:
        await save_debug_artifacts(page, "step2-no-auth-redirect-after-continue")
        raise


async def _try_click_otp_switch(page: Page, *, total_timeout: float = 8) -> bool:
    """Look for the 「使用一次性验证码」/「Use a one-time code instead」 link
    on the password page. Click it and return True on success.

    Skips footer/policy links and any anchor that would navigate off
    auth.openai.com.
    """
    deadline = asyncio.get_event_loop().time() + total_timeout

    async def _attempt(loc, *, kind: str) -> bool:
        try:
            if not await loc.is_visible():
                return False
        except Exception:
            return False
        href = ""
        try:
            href = (await loc.get_attribute("href")) or ""
        except Exception:
            pass
        if href and not _is_safe_retry_href(href):
            return False
        pre_url = page.url
        try:
            await loc.click(timeout=4000)
        except Exception as e:  # noqa: BLE001
            print(f"[otp-switch] click ({kind}) 失败: {e}")
            return False
        await asyncio.sleep(1.0)
        if _is_policy_url(page.url):
            print(f"[otp-switch] WARN 点 {kind} 后跳到了 {page.url}，回退")
            try:
                await page.go_back(wait_until="commit", timeout=10000)
            except Exception:
                try:
                    await page.goto(pre_url, wait_until="commit", timeout=10000)
                except Exception:
                    pass
            return False
        print(f"[otp-switch] 已点击 {kind}（切到一次性验证码模式）")
        return True

    while asyncio.get_event_loop().time() < deadline:
        # Scan all clickable text nodes; match by inner text against OTP_SWITCH_PATTERNS.
        for sel in ("button, [role=button], input[type=submit]", "a"):
            els = page.locator(sel)
            try:
                n = await els.count()
            except Exception:
                n = 0
            for i in range(n):
                cand = els.nth(i)
                txt = await _safe_get_text(cand)
                if not txt:
                    continue
                if not any(p.search(txt) for p in OTP_SWITCH_PATTERNS):
                    continue
                kind = "button" if sel.startswith("button") else "link"
                if await _attempt(cand, kind=kind):
                    return True
        await asyncio.sleep(0.5)
    return False


async def step3_password(
    page: Page,
    password: str,
    *,
    auth_mode: str = "otp",
) -> str:
    """Step 3: handle the password page. Returns the password actually used
    (empty string if we switched to OTP-only signup)."""
    print(f"[step 3] 等密码页（auth_mode={auth_mode}）")
    try:
        await _wait_url(
            page,
            [re.compile(r"create-account/password|/password|email-verification|verification|verify", re.IGNORECASE)],
            timeout=30000,
        )
    except TimeoutError:
        await save_debug_artifacts(page, "step3-no-password-or-verify-page")
        raise

    # 已经在邮箱验证页：直接结束 step3
    if re.search(r"email-verification|verification|verify", page.url, re.IGNORECASE):
        print("[step 3] 直接落在邮箱验证页，跳过密码步骤")
        return ""

    # 在密码页：根据 auth_mode 决定走 OTP 切换 or 填密码
    if auth_mode == "otp":
        switched = await _try_click_otp_switch(page, total_timeout=10)
        if switched:
            await wait_for_url_with_recovery(
                page,
                success_patterns=[re.compile(r"email-verification|verification|verify", re.IGNORECASE)],
                total_timeout_seconds=60,
                label="step 3 OTP 切换后等验证码页",
                max_retries=5,
            )
            return ""
        print("[step 3] 未找到 OTP 切换链接，回退到填密码")

    # 填密码路径
    pw = page.locator("input[type=password]").first
    await pw.wait_for(state="visible", timeout=20000)
    await pw.click()
    await pw.fill(password)
    await asyncio.sleep(0.2)
    await _submit_after_input(pw, page)

    # OpenAI 偶尔在密码提交后弹「操作超时 / Route Error」+ 重试按钮，最多自动点 5 次
    await wait_for_url_with_recovery(
        page,
        success_patterns=[re.compile(r"email-verification|verification|verify", re.IGNORECASE)],
        total_timeout_seconds=120,
        refill_password=password,
        label="step 3 等验证码页",
        max_retries=5,
    )
    return password


async def _fill_verification_code(page: Page, fetch_code: CodeFetcher, *, label: str) -> str:
    print(f"[{label}] 等验证码页并填入验证码")
    code = (await fetch_code()).strip()
    if not code or not re.fullmatch(r"\d{4,8}", code):
        raise RuntimeError(f"无效验证码：{code!r}")

    boxes = page.locator("input[maxlength='1']")
    count = await boxes.count()
    submit_target = None
    if count >= 6:
        for i in range(6):
            await boxes.nth(i).fill(code[i] if i < len(code) else "")
            await asyncio.sleep(0.05)
        submit_target = boxes.nth(min(count - 1, 5))
    else:
        single = page.locator(
            "input[name*=code i], input[placeholder*=code i], input[inputmode=numeric], input[type=text]"
        ).first
        await single.wait_for(state="visible", timeout=10000)
        await single.click()
        await single.fill(code)
        submit_target = single

    await asyncio.sleep(0.3)
    try:
        if submit_target is not None:
            await submit_target.press("Enter")
            print("[submit-otp] pressed Enter on OTP input")
    except Exception as e:  # noqa: BLE001
        print(f"[submit-otp] press Enter failed (likely auto-submitted): {e}")
    return code


async def _ensure_login_email_input(page: Page):
    async def _email_input():
        return page.locator(LOGIN_EMAIL_INPUT_SELECTOR).first

    async def _visible_email_input(timeout: int = 3000):
        loc = await _email_input()
        try:
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except PWTimeout:
            return None

    async def _click_login_entry() -> bool:
        selectors = [
            '[data-testid="login-button"]',
            'a[href*="/auth/login_with"]',
            'a[href*="callback_path"]',
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=5000)
                    print(f"[relogin] clicked login entry via selector: {sel}")
                    return True
            except Exception:
                continue

        exact = re.compile(r"^\s*(登录|log\s*in|login|sign\s*in)\s*$", re.IGNORECASE)
        controls = page.locator("button, a, [role=button]")
        try:
            count = await controls.count()
        except Exception:
            count = 0
        for i in range(count):
            loc = controls.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                txt = await _safe_get_text(loc)
                if not txt or not exact.fullmatch(txt):
                    continue
                await loc.click(timeout=5000)
                print(f"[relogin] clicked login entry via text: {txt!r}")
                return True
            except Exception:
                continue
        return False

    first = await _visible_email_input(timeout=2000)
    if first is not None:
        return first

    # chatgpt.com/auth/login is now a landing modal page. Click its login
    # entry first; the actual email box is on the following auth/login_with flow.
    for url in (
        "https://chatgpt.com/auth/login",
        "https://chatgpt.com/auth/login?screen_hint=login",
        "https://chatgpt.com/auth/login_with?callback_path=/",
    ):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            continue

        loc = await _visible_email_input(timeout=4000)
        if loc is not None:
            return loc

        await _click_login_entry()
        loc = await _visible_email_input(timeout=15000)
        if loc is not None:
            return loc

    # auth.openai.com/log-in sometimes only shows a "session ended" page with a
    # link back to chatgpt.com/auth/login_with. Click it instead of waiting for
    # a nonexistent email field.
    try:
        await page.goto("https://auth.openai.com/log-in", wait_until="domcontentloaded", timeout=20000)
        loc = await _visible_email_input(timeout=3000)
        if loc is not None:
            return loc
        if await _click_login_entry():
            loc = await _visible_email_input(timeout=15000)
            if loc is not None:
                return loc
    except Exception:
        pass

    await save_debug_artifacts(page, "relogin-no-email-input")
    loc = await _email_input()
    await loc.wait_for(state="visible", timeout=1000)
    return loc


async def step_login_existing_account(
    page: Page,
    *,
    email: str,
    password: str = "",
    auth_mode: str = "otp",
    fetch_code: CodeFetcher,
    total_timeout_seconds: float = 180,
) -> str:
    print(f"[relogin] 打开登录页：{email}")
    await page.goto(CHATGPT_LOGIN_URL, wait_until="domcontentloaded")

    email_input = await _ensure_login_email_input(page)
    await _submit_email_form(page, email_input, email)

    next_patterns = [
        re.compile(r"/password", re.IGNORECASE),
        re.compile(r"email-verification|verification|verify", re.IGNORECASE),
        re.compile(r"chatgpt\.com/(?!auth)", re.IGNORECASE),
    ]
    for attempt in range(3):
        try:
            await wait_for_url_with_recovery(
                page,
                success_patterns=next_patterns,
                total_timeout_seconds=25,
                label="relogin 等登录下一步",
                max_retries=2,
            )
            break
        except TimeoutError:
            if attempt >= 2:
                raise
            retry_input = page.locator(LOGIN_EMAIL_INPUT_SELECTOR).first
            try:
                if await retry_input.count() > 0 and await retry_input.is_visible():
                    print("[relogin] 邮箱页仍未前进，重新提交邮箱")
                    await _submit_email_form(page, retry_input, email)
                    continue
            except Exception:
                pass
            raise

    used_code = ""
    if re.search(r"/password", page.url, re.IGNORECASE):
        if auth_mode == "otp" or not password:
            switched = await _try_click_otp_switch(page, total_timeout=10)
            if switched:
                await wait_for_url_with_recovery(
                    page,
                    success_patterns=[re.compile(r"email-verification|verification|verify", re.IGNORECASE)],
                    total_timeout_seconds=60,
                    label="relogin OTP 切换后等验证码页",
                    max_retries=5,
                )
            elif password:
                print("[relogin] 未找到 OTP 入口，回退到密码登录")
            else:
                raise RuntimeError("账号无密码且未找到 OTP 登录入口")

        if re.search(r"/password", page.url, re.IGNORECASE):
            pw = page.locator("input[type=password]").first
            await pw.wait_for(state="visible", timeout=20000)
            await pw.click()
            await pw.fill(password)
            await asyncio.sleep(0.2)
            await _submit_after_input(pw, page)
            await wait_for_url_with_recovery(
                page,
                success_patterns=[
                    re.compile(r"email-verification|verification|verify", re.IGNORECASE),
                    re.compile(r"chatgpt\.com/(?!auth)", re.IGNORECASE),
                ],
                total_timeout_seconds=90,
                refill_password=password,
                label="relogin 密码提交后",
                max_retries=5,
            )

    if re.search(r"email-verification|verification|verify", page.url, re.IGNORECASE):
        used_code = await _fill_verification_code(page, fetch_code, label="relogin code")

    await wait_for_url_with_recovery(
        page,
        success_patterns=[re.compile(r"chatgpt\.com/(?!auth)", re.IGNORECASE)],
        total_timeout_seconds=total_timeout_seconds,
        label="relogin 等登录完成",
        max_retries=5,
    )
    return used_code


async def step4_code(page: Page, fetch_code: CodeFetcher) -> str:
    print("[step 4] 等验证码页并填入验证码")
    try:
        await _wait_url(
            page,
            [re.compile(r"email-verification|verification|verify", re.IGNORECASE)],
            timeout=60000,
        )
    except TimeoutError as e:
        # Some pages do not change URL; try locating the input instead.
        try:
            await page.locator(
                "input[name*=code i], input[placeholder*=code i], input[inputmode=numeric]"
            ).first.wait_for(state="visible", timeout=15000)
        except PWTimeout:
            raise e

    code = await _fill_verification_code(page, fetch_code, label="step 4")

    # 验证码提交后也可能命中 OpenAI 操作超时 / Route Error 重试页
    await wait_for_url_with_recovery(
        page,
        success_patterns=[
            re.compile(r"/(welcome|onboarding|account|setup)", re.IGNORECASE),
            re.compile(r"chatgpt\.com/(?!auth)", re.IGNORECASE),
            # any auth.openai.com URL that doesn't include verification means we moved on
            re.compile(r"auth\.openai\.com/.*(?<!verification)(?<!verify)$", re.IGNORECASE),
        ],
        total_timeout_seconds=90,
        label="step 4 验证码提交后",
        max_retries=5,
    )
    return code


_PROFILE_URL_RE = re.compile(
    r"about-?you|create-account|onboarding|profile|setup|account-setup|sign-up",
    re.IGNORECASE,
)
_ANY_TEXT_INPUT_SELECTOR = (
    "input:not([type=hidden]):not([type=submit])"
    ":not([type=checkbox]):not([type=radio]):not([type=button])"
    ":not([type=image]):not([type=reset])"
)


async def _enumerate_visible_inputs(page: Page) -> list[tuple]:
    """Return [(locator, metadata_dict)] for every visible non-button input."""
    all_inputs = page.locator(_ANY_TEXT_INPUT_SELECTOR)
    try:
        n = await all_inputs.count()
    except Exception:
        return []
    out = []
    for i in range(n):
        inp = all_inputs.nth(i)
        try:
            if not await inp.is_visible():
                continue
        except Exception:
            continue
        try:
            meta = await inp.evaluate("""el => ({
                name: (el.name||'').toLowerCase(),
                id: (el.id||'').toLowerCase(),
                placeholder: (el.placeholder||'').toLowerCase(),
                type: (el.type||'').toLowerCase(),
                autocomplete: (el.autocomplete||'').toLowerCase(),
                ariaLabel: (el.getAttribute('aria-label')||'').toLowerCase(),
                inputmode: (el.getAttribute('inputmode')||'').toLowerCase(),
                maxlength: el.getAttribute('maxlength')||''
            })""")
        except Exception:
            meta = {}
        out.append((inp, meta))
    return out


def _classify_input(meta: dict) -> str:
    blob = " ".join(str(meta.get(k, "")) for k in (
        "name", "id", "placeholder", "autocomplete", "ariaLabel"
    ))
    typ = str(meta.get("type", ""))
    name_attr = str(meta.get("name", "")).lower()
    placeholder = str(meta.get("placeholder", ""))
    autocomplete = str(meta.get("autocomplete", "")).lower()

    if typ == "date":
        return "birth"

    # Age（数字、`age` 关键字）
    if name_attr == "age" or "年龄" in placeholder:
        return "age"
    # 「全名 / Full name」单字段，必须比 first/last 更早判定
    if (autocomplete == "name"
            or name_attr == "name"
            or name_attr == "fullname"
            or name_attr == "full-name"
            or "全名" in placeholder
            or "full name" in placeholder.lower()
            or "fullname" in placeholder.lower()):
        return "name"

    if any(k in blob for k in ("firstname", "first-name", "first_name", "first ", "given")):
        return "first"
    if any(k in blob for k in ("lastname", "last-name", "last_name", "last ", "family", "surname")):
        return "last"
    if any(k in blob for k in ("bday", "birth", "dob", "date-of-birth", "date_of_birth")):
        return "birth"
    if "age" in blob:
        return "age"
    if any(k in blob for k in ("姓氏", "姓 ", " 姓")):
        return "last"
    if "名字" in blob:
        return "first"
    if "生日" in blob or "出生" in blob:
        return "birth"
    if any(k in blob for k in ("day", "日")):
        return "day"
    if any(k in blob for k in ("month", "月")):
        return "month"
    if any(k in blob for k in ("year", "年")) and "yearly" not in blob:
        return "year"
    return "unknown"


async def step5_profile(page: Page, *, first_name: str, last_name: str, birthday: Birthday) -> None:
    print(f"[step 5] 填资料 {first_name} {last_name} {birthday.iso()}")

    if re.search(r"^https?://(chatgpt\.com|chat\.openai\.com)/(?!auth)", page.url, re.IGNORECASE):
        print(f"[step 5] 已在 {page.url}，OpenAI 跳过了资料页，本步直接结束")
        return

    deadline = asyncio.get_event_loop().time() + 30
    inputs: list[tuple] = []
    while asyncio.get_event_loop().time() < deadline:
        inputs = await _enumerate_visible_inputs(page)
        if inputs:
            break
        await asyncio.sleep(0.5)

    if not inputs:
        body = await _page_text(page)
        if re.search(r"how can I help|有什么可以帮你|发个消息|message chatgpt", body, re.IGNORECASE):
            print(f"[step 5] 已是 ChatGPT 主界面，跳过本步 (url={page.url})")
            return
        await save_debug_artifacts(page, "step5-no-inputs")
        print(f"[step 5] 30s 内没找到任何可见输入框 (url={page.url})，已落截图，跳过本步")
        return

    print(f"[step 5] 资料页就绪 url={page.url}  visible_inputs={len(inputs)}")
    classified: dict[str, list] = {}
    for idx, (loc, meta) in enumerate(inputs):
        kind = _classify_input(meta)
        print(f"  input[{idx}] kind={kind}  meta={meta}")
        classified.setdefault(kind, []).append((loc, meta))

    async def _safe_fill(loc, value: str, *, label: str) -> bool:
        """fill() 自动 focus，不需要先 click——避免 React Aria 浮标拦截 click。"""
        try:
            await loc.fill(value, timeout=5000)
            print(f"[step 5] filled {label} = {value!r}")
            return True
        except Exception as e1:  # noqa: BLE001
            print(f"[step 5] fill {label} 失败: {e1}；试 force focus + 直接赋值")
            try:
                await loc.focus(timeout=2000)
                await loc.evaluate(
                    "(el, v) => { "
                    "  const setter = Object.getOwnPropertyDescriptor("
                    "    el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,"
                    "    'value').set;"
                    "  setter.call(el, v);"
                    "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                    "  el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "}",
                    value,
                )
                print(f"[step 5] filled {label} = {value!r} (via JS)")
                return True
            except Exception as e2:  # noqa: BLE001
                print(f"[step 5] fill {label} 也失败 (JS): {e2}")
                return False

    async def _fill_kind(kind: str, value: str) -> bool:
        if kind not in classified or not classified[kind]:
            return False
        loc, _ = classified[kind][0]
        return await _safe_fill(loc, value, label=kind)

    filled_first = await _fill_kind("first", first_name)
    filled_last = await _fill_kind("last", last_name)
    if not (filled_first and filled_last):
        # 单个 Full name 字段
        await _fill_kind("name", f"{first_name} {last_name}")
    if not filled_first and not filled_last and "unknown" in classified and len(classified["unknown"]) >= 2:
        await _safe_fill(classified["unknown"][0][0], first_name, label="unknown[0]→first")
        await _safe_fill(classified["unknown"][1][0], last_name, label="unknown[1]→last")

    if "birth" in classified:
        loc, meta = classified["birth"][0]
        value = birthday.iso() if meta.get("type") == "date" else f"{birthday.month:02d}/{birthday.day:02d}/{birthday.year}"
        await _safe_fill(loc, value, label="birth")
    elif "age" in classified:
        await _fill_kind("age", str(birthday.age()))
    elif all(k in classified for k in ("day", "month", "year")):
        await _fill_kind("day", f"{birthday.day:02d}")
        await _fill_kind("month", f"{birthday.month:02d}")
        await _fill_kind("year", str(birthday.year))

    consents = page.locator("input[type=checkbox]")
    try:
        cn = await consents.count()
    except Exception:
        cn = 0
    for i in range(cn):
        cb = consents.nth(i)
        try:
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check(timeout=2000)
                print(f"[step 5] 已勾选 checkbox[{i}]")
        except Exception:
            continue

    async def _click_profile_complete(*, required: bool = True, reason: str = "") -> bool:
        suffix = f"（{reason}）" if reason else ""
        try:
            await _click_first(page, COMPLETE_TEXTS, timeout=8000 if required else 3000)
            print(f"[step 5] 已点击 完成账户创建/继续 按钮{suffix}")
            return True
        except (PWTimeout, TimeoutError):
            try:
                submit = page.locator("button[type=submit]:visible").first
                if await submit.count() > 0:
                    await submit.click(timeout=5000 if required else 2500)
                    print(f"[step 5] 已点击 button[type=submit]（兜底）{suffix}")
                    return True
                raise PWTimeout("no submit button")
            except Exception as e:  # noqa: BLE001
                if required:
                    await save_debug_artifacts(page, "step5-no-complete-button")
                    raise RuntimeError(f"未找到完成按钮：{e}") from e
                print(f"[step 5] 重新点击完成按钮失败{suffix}: {e}")
                return False

    async def _wait_profile_submit_result() -> None:
        deadline2 = asyncio.get_event_loop().time() + 60
        retry_count = 0
        resubmit_count = 0
        last_submit_at = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() < deadline2:
            url = page.url or ""
            if re.search(r"^https?://(chatgpt\.com|chat\.openai\.com)/(?!auth)", url, re.IGNORECASE):
                print(f"[step 5] 资料提交后已进入 {url}")
                return

            if "auth.openai.com/about-you" not in url:
                print(f"[step 5] 资料提交后离开资料页 url={url}")
                return

            hard = await _detect_hard_block(page)
            if hard:
                raise RuntimeError(
                    f"step 5 资料提交后硬阻断：页面命中关键字 {hard!r}"
                    f"（OpenAI 风控/IP 限速；换 VPN 节点或等 15-30 分钟再试）"
                )

            soft_error = await _detect_soft_error(page)
            retry_clicked = await _click_retry_if_present(page)
            if soft_error or retry_clicked:
                if retry_count >= 3:
                    await save_debug_artifacts(page, "step5-retry-exhausted")
                    raise RuntimeError("step 5 资料提交后连续出现重试/超时，已达到 3 次上限")
                retry_count += 1
                print(f"[step 5] 资料提交后检测到重试/超时，已处理第 {retry_count}/3 次")
                last_submit_at = asyncio.get_event_loop().time()
                await asyncio.sleep(2)
                continue

            # about-you 停留太久时，说明提交没有真正发出或后端没响应，补点一次完成。
            if asyncio.get_event_loop().time() - last_submit_at >= 8 and resubmit_count < 2:
                resubmit_count += 1
                if await _click_profile_complete(required=False, reason=f"仍停留资料页，重提 {resubmit_count}/2"):
                    last_submit_at = asyncio.get_event_loop().time()
                    await asyncio.sleep(2)
                    continue

            await asyncio.sleep(1)

        await save_debug_artifacts(page, "step5-submit-timeout")
        raise TimeoutError(f"step 5 资料提交后仍停留在 {page.url}，不进入 step 6")

    await asyncio.sleep(0.4)
    await _click_profile_complete(required=True)
    await _wait_profile_submit_result()


async def step6_wait_success(page: Page) -> None:
    print(f"[step 6] 等最多 {REG_SUCCESS_WAIT_SECONDS}s，看注册是否会自动跳到 chatgpt.com")
    deadline = asyncio.get_event_loop().time() + REG_SUCCESS_WAIT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if re.search(r"^https?://(chatgpt\.com|chat\.openai\.com)/(?!auth)", page.url, re.IGNORECASE):
            print(f"[step 6] 已自动跳到 {page.url}")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            return
        await asyncio.sleep(1)

    if "chatgpt.com" not in page.url and "chat.openai.com" not in page.url:
        print(f"[step 6] 仍在 {page.url}，主动跳 chatgpt.com")
        try:
            await page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
    else:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass


def random_profile():
    return random_first_name(), random_last_name(), random_birthday()


def ensure_password(password: str) -> str:
    return password.strip() or generate_password()
