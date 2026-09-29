"""6-step ChatGPT registration flow driven by Playwright."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time

from core.ban_check import BAN_PATTERNS as BAN_TEXT_PATTERNS
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, TimeoutError as PWTimeout


def _target_closed(error: BaseException) -> bool:
    """Playwright 的 TargetClosedError 只从私有模块导出，按类名+文案判定。"""
    return (
        type(error).__name__ == "TargetClosedError"
        or "has been closed" in str(error).lower()
        or "target page, context or browser" in str(error).lower()
    )

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


class HeadlessBlockedError(RuntimeError):
    """Raised when a browser challenge prevents headless registration."""


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
    "免费注册", "注册", "创建账户", "创建账号", "建立帳戶", "免費註冊",
    "create account", "register", "get started",
    # 首页的 Sign up / Create account 按钮没有任何 data-testid，文案完全跟随
    # 浏览器语言；而指纹画像会在 en/de/fr/nl/es/it/pl/sv/pt/id/vi/th/ja/ko/zh 之间
    # 轮换（见 core/fingerprint.PERSONAS），所以文案表必须覆盖这些语言，
    # 否则每次注册都要白等 25s 再走 auth URL 回退。
    # 现网实测（波兰语画像）：首页按钮是 "Zarejestruj się za darmo"、"Utwórz konto"。
    "kostenlos registrieren", "konto erstellen", "registrieren",
    "inscription gratuite", "créer un compte", "s'inscrire", "s’inscrire",
    "regístrate gratis", "crear cuenta", "regístrate",
    "registrati gratis", "crea un account", "registrati",
    "gratis aanmelden", "account aanmaken", "registreren",
    "zarejestruj się", "utwórz konto",
    "registrera dig", "skapa konto", "registrera",
    "inscreva-se", "criar conta",
    "daftar gratis", "buat akun",
    "đăng ký", "tạo tài khoản",
    "สมัครฟรี", "สร้างบัญชี", "สมัคร",
    "無料で登録", "アカウントを作成", "新規登録",
    "무료로 가입", "계정 만들기", "가입하기",
]
LOGIN_BUTTON_TEXTS = [
    "log in", "login", "sign in", "登录", "登入",
]
SIGNUP_BUTTON_CSS_SELECTORS = [
    '[data-testid="signup-button"]',
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
# Ban text rules come from core.ban_check so login and mailbox classification stay aligned.
CLOUDFLARE_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com/turnstile",
    "__cf_chl_tk",
    "__cf_chl_rt_tk",
    "__cf_chl_f_tk",
    "cf-turnstile-response",
    "__cf_chl_",
    "challenge-error-text",
    # 2026-09-29 真实抓包（/tmp 之外的 output/debug 快照同款）：托管挑战页的
    # <title> 会随浏览器语言本地化（泰语 "รอสักครู่..."、西语等），但下面这些
    # 令牌始终在 HTML 里，所以判定不能只靠英文 title 文案。
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
)
# 挑战容器/iframe 的语言无关兜底（DOM 层，不看任何文案）。
CLOUDFLARE_DOM_PROBE_JS = """
() => {
  try {
    if (document.querySelector(
      '#challenge-form, #challenge-running, #challenge-stage, #cf-challenge, '
      + '#challenge-body-text, div.cf-turnstile, '
      + 'iframe[src*="challenges.cloudflare.com"]'
    )) return true;
    return !!document.querySelector('script[src*="/cdn-cgi/challenge-platform/"]');
  } catch (e) {
    return false;
  }
}
"""
CLOUDFLARE_TEXT_PATTERNS = (
    re.compile(r"just a moment", re.IGNORECASE),
    re.compile(r"enable javascript and cookies to continue", re.IGNORECASE),
    re.compile(r"verification successful\.?\s*waiting", re.IGNORECASE),
    re.compile(r"turnstile", re.IGNORECASE),
)
COMPLETE_TEXTS = [
    "agree", "同意", "完成", "continue", "继续", "create account", "完成帐户创建", "创建账号",
]

REG_SUCCESS_WAIT_SECONDS = 20
# 托管挑战（"Just a moment..."）在这台机器+代理上实测需要 ~29s 才自动放行，
# 所以「等挑战过去」的预算必须远大于点击 Sign up 的 25s 预算。
CLOUDFLARE_WAIT_SECONDS = 300

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


async def _detect_cloudflare_challenge(page: Page) -> bool:
    try:
        url = page.url.lower()
        if "__cf_chl" in url or "/cdn-cgi/challenge-platform/" in url:
            return True
    except Exception:
        pass
    try:
        title = (await page.title()).strip().lower()
        if title in {"just a moment...", "just a moment", "attention required! | cloudflare"}:
            return True
    except Exception:
        pass
    try:
        html = (await page.content()).lower()
    except Exception:
        html = ""
    if any(marker.lower() in html for marker in CLOUDFLARE_CHALLENGE_MARKERS):
        return True
    try:
        if await page.evaluate(CLOUDFLARE_DOM_PROBE_JS):
            return True
    except Exception:
        pass
    try:
        body = await _page_text(page)
    except Exception:
        body = ""
    return any(pattern.search(body) for pattern in CLOUDFLARE_TEXT_PATTERNS)


async def _wait_for_cloudflare_clear(
    page: Page,
    *,
    timeout_seconds: float = CLOUDFLARE_WAIT_SECONDS,
    allow_manual_cloudflare: bool = False,
    label: str = "cloudflare",
    poll: float = 1.0,
) -> bool:
    """Wait until the Cloudflare interstitial is gone.

    Returns True once the challenge is no longer detected, False on timeout.
    Raises :class:`HeadlessBlockedError` in headless mode (nothing can solve the
    challenge there) or when the page/browser goes away while waiting.
    """
    loop = asyncio.get_event_loop()
    if not await _detect_cloudflare_challenge(page):
        return True
    if not allow_manual_cloudflare:
        await _raise_if_cloudflare_challenge(page, label=f"{label}-challenge")
    deadline = loop.time() + timeout_seconds
    last_notice = 0.0
    await save_debug_artifacts(page, f"{label}-challenge")
    print(
        "[cloudflare] 检测到 Cloudflare 托管挑战（页面标题可能被本地化，例如 'รอสักครู่...'）："
        f"浏览器窗口会先停在验证页，通常 30s 左右自动放行；如超过 60s 可在窗口里手动点一下验证，"
        f"最多等待 {int(timeout_seconds)}s（请勿关闭该窗口）"
    )
    while loop.time() < deadline:
        if not await _detect_cloudflare_challenge(page):
            print("[cloudflare] 验证已通过，继续注册流程")
            return True
        now = loop.time()
        if now - last_notice >= 15:
            print(f"[cloudflare] 等待验证通过... 剩余 {max(0, int(deadline - now))}s（不要关闭浏览器窗口）")
            last_notice = now
        await asyncio.sleep(poll)
    await save_debug_artifacts(page, f"{label}-challenge-timeout")
    print(f"[cloudflare] 等待 {int(timeout_seconds)}s 仍未通过验证，放弃")
    return False


def _looks_like_chatgpt_home(url: str) -> bool:
    u = (url or "").lower()
    return (
        u == "https://chatgpt.com/"
        or u.startswith("https://chatgpt.com/?")
        or u.startswith("https://chatgpt.com/zh-cn")
        or u.startswith("https://chatgpt.com/zh-cn/")
    )


async def _raise_if_cloudflare_challenge(page: Page, *, label: str) -> None:
    if not await _detect_cloudflare_challenge(page):
        return
    await save_debug_artifacts(page, label)
    raise HeadlessBlockedError(
        "OpenAI 注册页被 Cloudflare/Turnstile 拦截，页面没有邮箱输入框。"
        "duck-api 已可在无头模式生成邮箱，但无头浏览器当前无法继续注册。"
        "即使用有头模式跑通过一次，如果 Cloudflare 没下发可复用的 cf_clearance，"
        "headless 仍会被重新挑战；请取消 headless，用有头模式完成验证/注册。"
    )


class AccountBannedError(RuntimeError):
    """Raised when OpenAI's login page says the account was deactivated."""


LOGIN_PASSWORD_SELECTOR = "input[type=password]"
LOGIN_CODE_SELECTOR = (
    "input[name*=code i], input[placeholder*=code i], "
    "input[inputmode=numeric], input[autocomplete=one-time-code]"
)


def classify_login_signal(
    *,
    url: str,
    has_password_form: bool = False,
    has_code_form: bool = False,
    body_text: str = "",
    has_email_form: bool = False,
) -> str:
    """Describe what the live login page is actually showing.

    Why this exists: after the email is submitted, the current ChatGPT auth flow
    renders the password / OTP step **on the same**
    ``auth.openai.com/api/accounts/authorize?...`` URL, which matches none of the
    URL patterns the old code polled — so a perfectly healthy re-login timed out.
    Deciding from the DOM (with the URL as a fallback) fixes that.

    Precedence: explicit notice/failure text first, then real forms, then URL.
    """
    text = body_text or ""
    if any(pattern.search(text) for pattern in BAN_TEXT_PATTERNS):
        return "banned"
    if any(pattern.search(text) for pattern in CLOUDFLARE_TEXT_PATTERNS):
        return "cloudflare"
    if any(pattern.search(text) for pattern in SOFT_ERROR_PATTERNS):
        return "rate_limited"

    lowered = (url or "").lower()
    if has_password_form:
        return "password"
    if has_code_form:
        return "code"
    if _looks_like_chatgpt_home(url) or re.search(
        r"^https?://(chatgpt\.com|chat\.openai\.com)/(?!auth)", url or "", re.IGNORECASE
    ):
        return "logged_in"
    if re.search(r"/password", lowered):
        return "password"
    if re.search(r"email-verification|verification|verify", lowered):
        return "code"
    if has_email_form:
        return "email"
    return "unknown"


async def _login_signal(page: Page) -> str:
    """Read the live page and classify it (see :func:`classify_login_signal`)."""
    has_password = False
    has_code = False
    has_email = False
    for selector, key in (
        (LOGIN_PASSWORD_SELECTOR, "password"),
        (LOGIN_CODE_SELECTOR, "code"),
        (LOGIN_EMAIL_INPUT_SELECTOR, "email"),
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                if key == "password":
                    has_password = True
                elif key == "code":
                    has_code = True
                else:
                    has_email = True
        except Exception:  # noqa: BLE001
            continue
    try:
        body = await _page_text(page)
    except Exception:  # noqa: BLE001
        body = ""
    return classify_login_signal(
        url=page.url,
        has_password_form=has_password,
        has_code_form=has_code,
        has_email_form=has_email,
        body_text=body,
    )


async def wait_for_login_step(
    page: Page,
    *,
    accept: tuple = ("password", "code", "logged_in"),
    total_timeout_seconds: float = 45,
    label: str = "relogin",
) -> str:
    """Wait until the login page reaches one of the accepted steps.

    Returns the matched signal; raises :class:`AccountBannedError` for a
    deactivated account (so callers can mark it instead of retrying forever) and
    ``TimeoutError`` carrying the last observed signal otherwise.
    """
    deadline = asyncio.get_event_loop().time() + max(1.0, total_timeout_seconds)
    last = "unknown"
    while asyncio.get_event_loop().time() < deadline:
        signal = await _login_signal(page)
        last = signal
        if signal in accept:
            return signal
        if signal == "banned":
            try:
                body = await _page_text(page)
            except Exception:  # noqa: BLE001
                body = ""
            hit = next((p.search(body) for p in BAN_TEXT_PATTERNS if p.search(body)), None)
            raise AccountBannedError(
                f"{label}：账号已被 OpenAI 停用"
                + (f"（页面提示：{hit.group(0)!r}）" if hit else "")
            )
        if signal == "cloudflare":
            raise RuntimeError(f"{label}：被 Cloudflare/Turnstile 拦截，请换 IP 或用有头模式重试")
        if signal == "rate_limited":
            # transient: click 重试 when the page offers it, then keep waiting
            with contextlib.suppress(Exception):
                await _click_retry_if_present(page)
        await asyncio.sleep(0.8)

    await save_debug_artifacts(page, f"{label or 'login'}-timeout")
    raise TimeoutError(
        f"{label} 超时（{int(total_timeout_seconds)}s）：页面停在 {last}，当前 url: {page.url}"
    )


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
    await email_input.fill("")
    await email_input.type(email, delay=35)
    try:
        value = await email_input.input_value()
    except Exception:
        value = ""
    if value.strip().lower() != email.strip().lower():
        await email_input.fill("")
        await email_input.type(email, delay=35)

    form_id = ""
    try:
        form_id = await email_input.get_attribute("form") or ""
    except Exception:
        pass

    await asyncio.sleep(0.6)

    clicked = False
    if form_id:
        submit = page.locator(
            f'button[type=submit][form="{form_id}"][name="intent"][value="email"], '
            f'input[type=submit][form="{form_id}"][name="intent"][value="email"], '
            f'button[type=submit][form="{form_id}"]'
        ).first
        try:
            if await submit.count() > 0 and await submit.is_visible():
                try:
                    await submit.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                await submit.click(timeout=5000)
                print("[submit-email] clicked email form submit")
                clicked = True
        except Exception as e:
            print(f"[submit-email] form submit click failed: {e}")

    if not clicked:
        submit = page.locator('button[type=submit], input[type=submit]').first
        try:
            if await submit.count() > 0 and await submit.is_visible():
                await submit.click(timeout=5000)
                print("[submit-email] clicked generic submit")
                clicked = True
        except Exception as e:
            print(f"[submit-email] generic submit click failed: {e}")

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


def _is_email_submission_progress_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    path = parsed.path.lower()
    if host == "chatgpt.com" and path == "/auth/login":
        return bool(parse_qs(parsed.query).get("email"))
    if host in {"auth.openai.com", "auth0.openai.com", "accounts.openai.com"}:
        return True
    return bool(re.search(r"email-verification|verification|verify|/password", path, re.IGNORECASE))


async def _wait_email_submission_progress(page: Page, *, timeout: int) -> None:
    """Accept both URL navigation and same-URL auth form transitions."""
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        if _is_email_submission_progress_url(page.url):
            return
        next_inputs = page.locator(
            'input[type="password"], input[autocomplete="one-time-code"], '
            'input[inputmode="numeric"][maxlength]'
        )
        try:
            count = await next_inputs.count()
        except Exception:
            count = 0
        for index in range(count):
            try:
                if await next_inputs.nth(index).is_visible():
                    return
            except Exception:
                continue
        await asyncio.sleep(0.4)
    raise TimeoutError(f"邮箱提交后未进入下一步（当前：{page.url}）")


async def _email_submit_state(page: Page) -> str:
    email = page.locator(EMAIL_INPUT_SELECTOR).first
    submit = page.locator('button[type="submit"], input[type="submit"]').first

    async def _flag(locator, method: str) -> str:
        try:
            if await locator.count() <= 0:
                return "missing"
            return "yes" if await getattr(locator, method)() else "no"
        except Exception:
            return "unknown"

    return (
        f"url={page.url} email_visible={await _flag(email, 'is_visible')} "
        f"email_disabled={await _flag(email, 'is_disabled')} "
        f"submit_disabled={await _flag(submit, 'is_disabled')}"
    )


async def clear_openai_state(context, *, also_storage: bool = True) -> None:
    """Clear cookies (and optionally localStorage / sessionStorage) for all
    OpenAI / ChatGPT domains. Other origins (DuckDuckGo, email providers) are
    untouched."""
    # ---- cookies ----
    total_target = 0
    try:
        all_cookies = await context.cookies()
        for c in all_cookies:
            d = (c.get("domain") or "").lstrip(".").lower()
            if any(d == td or d.endswith("." + td) for td in OPENAI_COOKIE_DOMAINS):
                total_target += 1
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
    print(f"[clear] cookies: 已扫描 {total_target} 个目标 cookie，清理了 {cleared} 个域")

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


async def _logged_out_home_ready(page: Page) -> bool:
    """未登录首页是否已经渲染完成（有指向 /auth/login 的入口）。

    首页的 Sign up 按钮文案随语言变化且没有 data-testid，所以「文案没匹配上」并不
    代表页面没加载完；但未登录首页在**任何语言**下都有指向 /auth/login（或其
    login_with 变体）的链接，href 与语言无关，可以当作「首页已经渲染」的稳定信号。
    """
    try:
        return bool(
            await page.evaluate(
                """() => document.readyState === 'complete' && !!document.querySelector(
                     'a[href*="/auth/login"], a[href*="/auth/login_with"]'
                   )"""
            )
        )
    except Exception:
        return False


async def _try_click_signup(
    page: Page,
    *,
    total_timeout: float = 25,
    allow_manual_cloudflare: bool = False,
    cloudflare_timeout_seconds: float = CLOUDFLARE_WAIT_SECONDS,
    homepage_give_up_passes: int = 3,
) -> bool:
    """Best-effort: try multiple ways to click the Sign up entry on chatgpt.com.

    A Cloudflare interstitial is not a failure: the challenge page has no Sign up
    button at all, and in this environment it takes ~30s to clear (its title is
    localized, e.g. Thai "รอสักครู่...", so it is detected by HTML/DOM markers).
    While a challenge is up we keep waiting instead of burning the click budget,
    otherwise the caller gives up right before the real homepage appears.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + total_timeout
    cloudflare_deadline: float | None = None
    last_challenge_notice = 0.0
    passes = 0
    while True:
        now = loop.time()
        limit = cloudflare_deadline if cloudflare_deadline is not None else deadline
        if now >= limit:
            return False
        if await _detect_cloudflare_challenge(page):
            if not allow_manual_cloudflare:
                await _raise_if_cloudflare_challenge(page, label="step2-cloudflare-challenge")
            if cloudflare_deadline is None:
                cloudflare_deadline = now + cloudflare_timeout_seconds
                await save_debug_artifacts(page, "step2-cloudflare-before-signup")
                print(
                    "[cloudflare] 点击 Sign up 之前页面仍是 Cloudflare 托管挑战（标题可能被本地化）；"
                    f"先等它自动放行，最多 {int(cloudflare_timeout_seconds)}s（不要关闭浏览器窗口）"
                )
            if now - last_challenge_notice >= 15:
                print(
                    "[cloudflare] 等待挑战通过..."
                    f" 剩余 {max(0, int(cloudflare_deadline - now))}s"
                )
                last_challenge_notice = now
            await asyncio.sleep(1.0)
            continue
        # 1) CSS selectors with stable testids / hrefs
        for sel in SIGNUP_BUTTON_CSS_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[step 2] clicked via selector: {sel}")
                    return True
            except Exception as exc:
                if _target_closed(exc):
                    raise
                continue
        # 2) Visible text fallback
        try:
            await _click_first(page, SIGNUP_BUTTON_TEXTS, timeout=2000)
            print("[step 2] clicked via text matcher")
            return True
        except (PWTimeout, TimeoutError):
            pass
        # 3) 首页已经渲染但没有可点的 Sign up（文案是没收录的语言）：
        #    与其空转到 total_timeout，不如立刻交给 auth URL 回退。
        passes += 1
        if passes >= homepage_give_up_passes and await _logged_out_home_ready(page):
            print(
                "[step 2] 首页已经渲染，但没有匹配到 Sign up 按钮（按钮文案为本地化语言）；"
                "直接改用 auth URL 回退，不再空转"
            )
            return False
        await asyncio.sleep(1)


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


async def _wait_for_email_input_anywhere(
    page: Page,
    timeout_seconds: float = 30,
    *,
    allow_manual_cloudflare: bool = False,
    cloudflare_timeout_seconds: float = 300,
):
    """Wait for an email input to be visible — either inside an inline modal on
    chatgpt.com, or after redirecting to auth.openai.com."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    cloudflare_deadline: float | None = None
    cloudflare_logged = False
    last_cloudflare_notice = 0.0

    while loop.time() < deadline:
        loc = page.locator(EMAIL_INPUT_SELECTOR).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            pass

        if await _detect_cloudflare_challenge(page):
            if not allow_manual_cloudflare:
                await _raise_if_cloudflare_challenge(page, label="step2-cloudflare-challenge")
            now = loop.time()
            if cloudflare_deadline is None:
                cloudflare_deadline = now + cloudflare_timeout_seconds
                deadline = cloudflare_deadline
                print(
                    "[cloudflare] 检测到 Cloudflare/Turnstile 验证；"
                    f"请在打开的浏览器窗口里手动完成验证，最多等待 {int(cloudflare_timeout_seconds)}s"
                )
                await save_debug_artifacts(page, "step2-cloudflare-challenge")
            if now - last_cloudflare_notice >= 15:
                print(f"[cloudflare] 等待手动验证通过... {max(0, int(cloudflare_deadline - now))}s left")
                last_cloudflare_notice = now
            await asyncio.sleep(1.0)
            continue

        if cloudflare_deadline is not None and not cloudflare_logged:
            print("[cloudflare] 验证页已消失，继续等待邮箱输入框")
            deadline = loop.time() + timeout_seconds
            cloudflare_logged = True

        await asyncio.sleep(0.4)
    if cloudflare_deadline is not None and loop.time() >= cloudflare_deadline:
        await save_debug_artifacts(page, "step2-cloudflare-timeout")
        raise TimeoutError("等待 Cloudflare/Turnstile 手动验证超时")
    raise TimeoutError("等待邮箱输入框超时")


async def step2_signup_email(
    page: Page,
    email: str,
    *,
    allow_manual_cloudflare: bool = False,
    cloudflare_timeout_seconds: float = CLOUDFLARE_WAIT_SECONDS,
) -> None:
    print(f"[step 2] 点 Sign up 并填邮箱：{email}")

    # 登录页/首页可能先是 Cloudflare 托管挑战（标题被本地化），此时页面上根本
    # 没有 Sign up 按钮。先把它等过去，避免把挑战页误判成「找不到按钮」。
    if await _detect_cloudflare_challenge(page):
        if not await _wait_for_cloudflare_clear(
            page,
            timeout_seconds=cloudflare_timeout_seconds,
            allow_manual_cloudflare=allow_manual_cloudflare,
            label="step2-cloudflare-before-signup",
        ):
            raise TimeoutError(
                "首页一直是 Cloudflare 挑战页，没等到 Sign up 按钮。"
                "如浏览器窗口仍开着可在窗口里手动完成验证后重试；"
                "频繁出现请更换代理出口 IP。"
            )

    clicked = await _try_click_signup(
        page,
        total_timeout=25,
        allow_manual_cloudflare=allow_manual_cloudflare,
        cloudflare_timeout_seconds=cloudflare_timeout_seconds,
    )
    if not clicked:
        # chatgpt.com itself uses this URL when you click the homepage Sign up
        # button. Going there directly skips the inline modal entirely.
        print(f"[step 2] 未找到 Sign up 按钮，回退跳转 {CHATGPT_SIGNUP_HINT_URL}")
        try:
            await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
        except Exception as e:  # noqa: BLE001
            await save_debug_artifacts(page, "step2-signup-fallback-failed")
            raise RuntimeError(f"找不到 Sign up 按钮，且 fallback 跳转也失败：{e}") from e
    else:
        try:
            await asyncio.sleep(1.0)
            if _looks_like_chatgpt_home(page.url):
                print(f"[step 2] 点击 Sign up 后仍停留在首页，回退跳转 {CHATGPT_SIGNUP_HINT_URL}")
                await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
        except Exception:
            pass

    # The Sign up button on chatgpt.com may either:
    #   (a) open an inline modal on chatgpt.com with an email input, or
    #   (b) navigate to auth.openai.com.
    # Wait for an email input visible in either case.
    try:
        email_input = await _wait_for_email_input_anywhere(
            page,
            timeout_seconds=25,
            allow_manual_cloudflare=allow_manual_cloudflare,
        )
    except TimeoutError:
        if not allow_manual_cloudflare:
            await _raise_if_cloudflare_challenge(page, label="step2-cloudflare-challenge")
        # Still nothing — try the explicit signup URL (only if we haven't already)
        if "screen_hint=signup" not in page.url and "auth.openai.com" not in page.url:
            print(f"[step 2] 当前页 ({page.url}) 没出现邮箱输入框，跳 {CHATGPT_SIGNUP_HINT_URL}")
            try:
                await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
            except Exception as e:  # noqa: BLE001
                await save_debug_artifacts(page, "step2-fallback-after-modal")
                raise RuntimeError(f"模态框未出现，跳 fallback URL 也失败：{e}") from e
            email_input = await _wait_for_email_input_anywhere(
                page,
                timeout_seconds=30,
                allow_manual_cloudflare=allow_manual_cloudflare,
            )
        else:
            await save_debug_artifacts(page, "step2-no-email-input")
            if not allow_manual_cloudflare:
                raise HeadlessBlockedError(
                    "headless 模式没有等到邮箱输入框。已保存 step2-no-email-input debug；"
                    "如果 debug 页面是 Cloudflare/Turnstile，headless 不能自动通过，请取消 headless 后重试。"
                ) from None
            raise

    network_state: dict[str, str | int] = {}

    def _on_auth_response(response) -> None:
        try:
            parsed = urlparse(response.url)
            method = response.request.method.upper()
            if parsed.hostname == "chatgpt.com" and parsed.path == "/auth/login" and method == "POST":
                network_state["status"] = response.status
                print(f"[step2-net] POST /auth/login -> {response.status}")
        except Exception:
            pass

    def _on_auth_request_failed(request) -> None:
        try:
            parsed = urlparse(request.url)
            method = request.method.upper()
            if parsed.hostname == "chatgpt.com" and parsed.path == "/auth/login" and method == "POST":
                failure = str(request.failure or "unknown")
                network_state["failure"] = failure
                print(f"[step2-net] POST /auth/login failed: {failure}")
        except Exception:
            pass

    page.on("response", _on_auth_response)
    page.on("requestfailed", _on_auth_request_failed)
    try:
        for attempt in range(1, 3):
            print(f"[step 2] 找到邮箱输入框，提交尝试 {attempt}/2，url={page.url}")
            await _submit_email_form(page, email_input, email)
            try:
                await _wait_email_submission_progress(page, timeout=30000 if attempt == 1 else 45000)
                print(f"[step 2] 已进入下一步，url={page.url}")
                return
            except TimeoutError:
                state = await _email_submit_state(page)
                net = (
                    f"POST status={network_state.get('status')}"
                    if network_state.get("status") is not None
                    else f"POST failure={network_state.get('failure') or 'no response'}"
                )
                print(f"[step 2] 邮箱提交卡住：{state}; {net}")
                if attempt >= 2:
                    await save_debug_artifacts(page, "step2-no-next-step-after-continue")
                    raise TimeoutError(f"邮箱提交两次均未进入下一步；{net}；{state}") from None

                await save_debug_artifacts(page, "step2-submit-stalled-retry")
                print("[step 2] 刷新注册入口并重提一次")
                await page.goto(CHATGPT_SIGNUP_HINT_URL, wait_until="commit", timeout=45000)
                email_input = await _wait_for_email_input_anywhere(
                    page,
                    timeout_seconds=30,
                    allow_manual_cloudflare=allow_manual_cloudflare,
                )
                network_state.clear()
    finally:
        page.remove_listener("response", _on_auth_response)
        page.remove_listener("requestfailed", _on_auth_request_failed)


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


async def _continue_chatgpt_email_login(page: Page, email: str) -> bool:
    """Handle the newer `chatgpt.com/auth/login?email=...` intermediate page.

    After step 2, ChatGPT sometimes stays on the same login URL with the email
    prefilled and expects one more explicit submit before moving to the OTP page.
    """
    try:
        email_input = page.locator(LOGIN_EMAIL_INPUT_SELECTOR).first
        if not await email_input.count() or not await email_input.is_visible():
            return False
    except Exception:
        return False

    try:
        current = (await email_input.input_value()).strip()
    except Exception:
        current = ""
    if current.lower() != email.strip().lower():
        try:
            await email_input.fill("", timeout=2000)
        except Exception:
            pass
        try:
            await email_input.fill(email, timeout=5000)
        except Exception:
            try:
                await email_input.click(timeout=2000)
            except Exception:
                pass
            await email_input.type(email, delay=35)
        try:
            await email_input.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
        except Exception:
            pass
        current = email
    print(f"[step 3] 检测到 chatgpt 登录中间页，email={current or '<empty>'}，补点继续")

    try:
        await _submit_email_form(page, email_input, email)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[step 3] chatgpt 登录中间页补提失败: {e}")
        return False


async def step3_password(
    page: Page,
    email: str,
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
            [
                re.compile(r"create-account/password|/password|email-verification|verification|verify", re.IGNORECASE),
                re.compile(r"^https://chatgpt\.com/auth/login\?email=", re.IGNORECASE),
            ],
            timeout=30000,
        )
    except TimeoutError:
        await save_debug_artifacts(page, "step3-no-password-or-verify-page")
        raise

    if re.search(r"^https://chatgpt\.com/auth/login\?email=", page.url, re.IGNORECASE):
        if await _continue_chatgpt_email_login(page, email):
            pass
        else:
            await save_debug_artifacts(page, "step3-chatgpt-email-stuck")
            raise TimeoutError(f"step 3 卡在 ChatGPT 登录中间页：{page.url}")
        await wait_for_url_with_recovery(
            page,
            success_patterns=[
                re.compile(r"create-account/password|/password|email-verification|verification|verify", re.IGNORECASE),
                re.compile(r"^https://(auth|auth0|accounts)\.openai\.com/", re.IGNORECASE),
            ],
            total_timeout_seconds=60,
            label="step 3 chatgpt 中间页继续后",
            max_retries=5,
        )
        if re.search(r"^https://chatgpt\.com/auth/login\?email=", page.url, re.IGNORECASE):
            await save_debug_artifacts(page, "step3-chatgpt-email-still-stuck")
            raise TimeoutError(f"step 3 继续后仍卡在 ChatGPT 登录中间页：{page.url}")

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


async def _refill_password_if_stuck(page: Page, password: str) -> bool:
    """The password form can come back empty after a submit; refill it once."""
    if not password:
        return False
    try:
        pw = page.locator(LOGIN_PASSWORD_SELECTOR).first
        if await pw.count() == 0 or not await pw.is_visible():
            return False
        current = ""
        with contextlib.suppress(Exception):
            current = await pw.input_value()
        if current:
            return False
        print("[recover] 密码页再次出现，重填密码")
        await pw.click()
        await pw.fill(password)
        await _submit_after_input(pw, page)
        return True
    except Exception:  # noqa: BLE001
        return False


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

    # The next step is decided by what the page actually renders, not by its URL:
    # the authorize endpoint shows the password / OTP form without changing URL.
    step_budget = max(30.0, min(60.0, total_timeout_seconds / 3))
    signal = ""
    for attempt in range(3):
        try:
            signal = await wait_for_login_step(
                page,
                accept=("password", "code", "logged_in"),
                total_timeout_seconds=step_budget,
                label="relogin 等登录下一步",
            )
            break
        except TimeoutError:
            if attempt >= 2:
                raise
            current = await _login_signal(page)
            if current == "email":
                retry_input = page.locator(LOGIN_EMAIL_INPUT_SELECTOR).first
                try:
                    if await retry_input.count() > 0 and await retry_input.is_visible():
                        print("[relogin] 邮箱页仍未前进，重新提交邮箱")
                        await _submit_email_form(page, retry_input, email)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            print(f"[relogin] 登录下一步尚未就绪（{current}），重试第 {attempt + 2} 次")
            continue

    used_code = ""
    if signal == "password":
        if auth_mode == "otp" or not password:
            switched = await _try_click_otp_switch(page, total_timeout=10)
            if switched:
                signal = await wait_for_login_step(
                    page,
                    accept=("code", "logged_in"),
                    total_timeout_seconds=60,
                    label="relogin OTP 切换后等验证码页",
                )
            elif password:
                print("[relogin] 未找到 OTP 入口，回退到密码登录")
            else:
                raise RuntimeError("账号无密码且未找到 OTP 登录入口")

        if signal == "password":
            pw = page.locator(LOGIN_PASSWORD_SELECTOR).first
            await pw.wait_for(state="visible", timeout=20000)
            await pw.click()
            await pw.fill(password)
            await asyncio.sleep(0.2)
            await _submit_after_input(pw, page)
            for attempt in range(3):
                try:
                    signal = await wait_for_login_step(
                        page,
                        accept=("code", "logged_in"),
                        total_timeout_seconds=40,
                        label="relogin 密码提交后",
                    )
                    break
                except TimeoutError:
                    if attempt >= 2 or not await _refill_password_if_stuck(page, password):
                        raise
                    continue

    if signal == "code":
        used_code = await _fill_verification_code(page, fetch_code, label="relogin code")

    if signal != "logged_in":
        await wait_for_login_step(
            page,
            accept=("logged_in",),
            total_timeout_seconds=total_timeout_seconds,
            label="relogin 等登录完成",
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


async def _profile_form_still_needs_input(page: Page) -> bool:
    """Whether the combined profile form still has unresolved required inputs.

    Newer OpenAI flows may keep the user on `/email-verification/register` even after
    clicking the submit button, and only surface inline validation errors instead of
    redirecting immediately. We must not treat that as a successful submit.
    """
    try:
        inputs = await _enumerate_visible_inputs(page)
    except Exception:
        return False
    for loc, meta in inputs:
        kind = _classify_input(meta)
        try:
            value = (await loc.input_value()).strip()
        except Exception:
            value = ""
        if kind in {"name", "first", "last"} and not value:
            return True
        if kind == "age":
            if not value:
                return True
            try:
                age = int(float(value))
            except Exception:
                return True
            if age < 5 or age > 130:
                return True
        if kind in {"birth", "day", "month", "year"} and not value:
            return True

    try:
        invalid_visible = await page.locator(
            "[aria-invalid='true']:visible, [data-invalid='true']:visible, .react-aria-FieldError:visible"
        ).count()
        if invalid_visible > 0:
            return True
    except Exception:
        pass
    return False


_DATE_ORDER_HINTS = (
    # (regex over the placeholder/label, strftime pattern)
    (re.compile(r"yyyy\s*[-/.年]\s*mm\s*[-/.月]\s*dd\s*日?", re.I), "%Y{sep}%m{sep}%d"),
    (re.compile(r"mm\s*[-/.]\s*dd\s*[-/.]\s*yyyy", re.I), "%m{sep}%d{sep}%Y"),
    (re.compile(r"dd\s*[-/.]\s*mm\s*[-/.]\s*yyyy", re.I), "%d{sep}%m{sep}%Y"),
    (re.compile(r"年\s*/\s*月\s*/\s*日"), "%Y/%m/%d"),
    (re.compile(r"月\s*/\s*日\s*/\s*年"), "%m/%d/%Y"),
    (re.compile(r"日\s*/\s*月\s*/\s*年"), "%d/%m/%Y"),
)

# A whole-date field, even when the placeholder only shows separators.
_DATE_FIELD_RE = re.compile(
    r"yyyy|yy\s*[-/.]\s*mm|mm\s*[-/.]\s*dd|dd\s*[-/.]\s*mm|年月日|年/月/日|月/日/年|日/月/年",
    re.I,
)
_AGE_WORDS = ("age", "年龄", "岁")
_BIRTH_WORDS = ("birth", "dob", "bday", "生日", "出生", "date of birth", "date-of-birth")


def _label_blob(meta: dict) -> str:
    return " ".join(str(meta.get(key, "")) for key in ("placeholder", "ariaLabel", "name", "id")).lower()


def date_hint(meta: dict) -> str | None:
    """Return a strftime pattern matching the field's placeholder, if it hints one."""
    blob = _label_blob(meta)
    for pattern, template in _DATE_ORDER_HINTS:
        if pattern.search(blob):
            if template == "%Y/%m/%d" and "年" in blob:
                return "%Y年%m月%d日" if "年" in blob and "/" not in blob else "%Y/%m/%d"
            separator = "-"
            if "/" in blob:
                separator = "/"
            elif "." in blob:
                separator = "."
            elif "年" in blob:
                return "%Y年%m月%d日"
            return template.replace("{sep}", separator)
    return None


def _is_date_like(meta: dict) -> bool:
    return str(meta.get("type", "")) == "date" or bool(_DATE_FIELD_RE.search(_label_blob(meta)))


def choose_birthday_strategy(kinds: set[str]) -> str:
    """Which representation the page is actually asking for."""
    if "birth" in kinds:
        return "date"
    if "age" in kinds:
        return "age"
    if {"day", "month", "year"} <= kinds:
        return "split"
    return "none"


def birthday_age_value(birthday) -> str:
    return str(birthday.age())


def _format_birthday(birthday, pattern: str) -> str:
    return pattern.replace("%Y", f"{birthday.year:04d}").replace("%m", f"{birthday.month:02d}").replace("%d", f"{birthday.day:02d}")


def birthday_text_candidates(birthday, meta: dict) -> list[str]:
    """Date strings to try, most likely first, based on the field's own hints."""
    candidates: list[str] = []
    if str(meta.get("type", "")) == "date":
        candidates.append(birthday.iso())
    else:
        hint = date_hint(meta)
        if hint:
            candidates.append(_format_birthday(birthday, hint))
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        candidates.append(_format_birthday(birthday, pattern))
    seen: set[str] = set()
    unique: list[str] = []
    for value in candidates:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def profile_needs_birthday(filled: dict[str, str], kinds: set[str]) -> bool:
    """True when the profile page still has an empty birthday/age field to fill."""
    strategy = choose_birthday_strategy(kinds)
    if strategy == "none":
        return False
    if strategy == "split":
        return not all(filled.get(part) for part in ("day", "month", "year"))
    return not filled.get("birth" if strategy == "date" else "age")


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

    blob_label = _label_blob(meta)
    # A whole-date field must be recognised before the day/month/year heuristics,
    # otherwise "yyyy-mm-dd" is misread as a "day" field.
    if _is_date_like(meta) or any(word in blob_label for word in _BIRTH_WORDS):
        return "birth"
    if name_attr == "age" or any(word in blob_label for word in _AGE_WORDS):
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


async def fill_profile_birthday(
    birthday,
    classified: dict[str, list],
    filled: dict[str, str],
    *,
    safe_fill,
) -> None:
    """Fill whichever birthday representation this page uses.

    The page may ask for a birth date (native date input, or a text field with its
    own format), a plain age, or separate day/month/year fields. `safe_fill` is
    injected so this stays independent of the surrounding step's helpers; it must
    return True when the value actually stuck.
    """

    async def _fill_kind(kind: str, value: str) -> bool:
        if kind not in classified or not classified[kind]:
            return False
        loc, _meta = classified[kind][0]
        return await safe_fill(loc, value, label=kind)

    kinds = set(classified)
    strategy = choose_birthday_strategy(kinds)
    if strategy == "date":
        loc, meta = classified["birth"][0]
        for candidate in birthday_text_candidates(birthday, meta):
            if await safe_fill(loc, candidate, label=f"birth({candidate})"):
                filled["birth"] = candidate
                return
        print("[step 5] 出生日期所有候选格式都没写进去，稍后重试")
    elif strategy == "age":
        value = birthday_age_value(birthday)
        if await _fill_kind("age", value):
            filled["age"] = value
    elif strategy == "split":
        for label, value in (("day", f"{birthday.day:02d}"),
                             ("month", f"{birthday.month:02d}"),
                             ("year", str(birthday.year))):
            if await _fill_kind(label, value):
                filled[label] = value


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
            try:
                actual = await loc.input_value(timeout=1000)
            except Exception:
                actual = value
            if actual.strip() == value.strip():
                print(f"[step 5] filled {label} = {value!r}")
                return True
            print(f"[step 5] fill {label} 后值未保留（actual={actual!r}），改用逐字输入")
            try:
                await loc.click(timeout=2000)
            except Exception:
                pass
            try:
                await loc.press("Meta+a", timeout=1000)
            except Exception:
                pass
            try:
                await loc.press("Control+a", timeout=1000)
            except Exception:
                pass
            await loc.type(value, delay=35, timeout=8000)
            actual = await loc.input_value(timeout=1000)
            if actual.strip() == value.strip():
                print(f"[step 5] filled {label} = {value!r} (via type)")
                return True
            raise RuntimeError(f"value mismatch after type: {actual!r}")
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

    filled: dict[str, str] = {}

    filled_first = await _fill_kind("first", first_name)
    filled_last = await _fill_kind("last", last_name)
    if filled_first:
        filled["first"] = first_name
    if filled_last:
        filled["last"] = last_name
    if not (filled_first and filled_last):
        # 单个 Full name 字段
        if await _fill_kind("name", f"{first_name} {last_name}"):
            filled["name"] = f"{first_name} {last_name}"
    if not filled_first and not filled_last and "unknown" in classified and len(classified["unknown"]) >= 2:
        await _safe_fill(classified["unknown"][0][0], first_name, label="unknown[0]→first")
        await _safe_fill(classified["unknown"][1][0], last_name, label="unknown[1]→last")

    tried_birth_formats: list[str] = []

    async def _try_next_birth_format() -> bool:
        """A text date field can accept a value yet still be rejected by the form.

        The placeholder usually states the format, but when it lies the only honest
        signal is the form's own validation feedback, so step through the remaining
        candidates instead of guessing once.
        """
        if "birth" not in classified or not classified["birth"]:
            return False
        loc, meta = classified["birth"][0]
        for candidate in birthday_text_candidates(birthday, meta):
            if candidate in tried_birth_formats:
                continue
            tried_birth_formats.append(candidate)
            if await _safe_fill(loc, candidate, label=f"birth reformat({candidate})"):
                filled["birth"] = candidate
                return True
        return False

    # The name field is sometimes revealed first and the birthday/age field only
    # appears afterwards, so re-scan the page instead of trusting one snapshot.
    await fill_profile_birthday(birthday, classified, filled, safe_fill=_safe_fill)
    if filled.get("birth"):
        tried_birth_formats.append(filled["birth"])
    if profile_needs_birthday(filled, set(classified)):
        for attempt in range(1, 4):
            await asyncio.sleep(0.6)
            rescan = await _enumerate_visible_inputs(page)
            rescanned: dict[str, list] = {}
            for loc, meta in rescan:
                kind = _classify_input(meta)
                if kind not in rescanned:
                    print(f"  [rescan {attempt}] new input kind={kind}  placeholder={meta.get('placeholder')!r}")
                    rescanned.setdefault(kind, []).append((loc, meta))
            merged = {kind: items for kind, items in classified.items()}
            for kind, items in rescanned.items():
                merged.setdefault(kind, items)
            classified = merged
            if not profile_needs_birthday(filled, set(classified)):
                break
            await fill_profile_birthday(birthday, classified, filled, safe_fill=_safe_fill)

    strategy = choose_birthday_strategy(set(classified))
    strategy_text = {"date": "出生日期", "age": "年龄", "split": "日/月/年 三格", "none": "未发现年龄/出生日期字段"}[strategy]
    print(f"[step 5] 生日字段类型={strategy_text}  已填={filled or '无'}")

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
            try:
                submitted = await page.evaluate(
                    "() => { if (typeof window.__submitPendingForm === 'function') { window.__submitPendingForm(); return true; } return false; }"
                )
                if submitted:
                    print(f"[step 5] 已触发 __submitPendingForm{suffix}")
            except Exception:
                pass
            return True
        except (PWTimeout, TimeoutError):
            try:
                submit = page.locator("button[type=submit]:visible").first
                if await submit.count() > 0:
                    await submit.click(timeout=5000 if required else 2500)
                    print(f"[step 5] 已点击 button[type=submit]（兜底）{suffix}")
                    try:
                        submitted = await page.evaluate(
                            "() => { if (typeof window.__submitPendingForm === 'function') { window.__submitPendingForm(); return true; } return false; }"
                        )
                        if submitted:
                            print(f"[step 5] 已触发 __submitPendingForm（兜底）{suffix}")
                    except Exception:
                        pass
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

            if "auth.openai.com/about-you" not in url and "email-verification/register" not in url:
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

            if await _profile_form_still_needs_input(page):
                if asyncio.get_event_loop().time() - last_submit_at >= 3 and resubmit_count < 2:
                    resubmit_count += 1
                    if await _try_next_birth_format():
                        print(f"[step 5] 表单校验未通过，换一种日期格式重填：{filled.get('birth')}")
                        last_submit_at = asyncio.get_event_loop().time()
                        await asyncio.sleep(1)
                        continue
                    if await _click_profile_complete(required=False, reason=f"表单仍未通过校验，重提 {resubmit_count}/2"):
                        last_submit_at = asyncio.get_event_loop().time()
                        await asyncio.sleep(2)
                        continue
                await asyncio.sleep(1)
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
    # A recognised-but-empty birthday field means the form would fail validation;
    # fill it (re-scanning once more) before submitting rather than resubmitting blind.
    if profile_needs_birthday(filled, set(classified)):
        await asyncio.sleep(0.8)
        await fill_profile_birthday(birthday, classified, filled, safe_fill=_safe_fill)
        if profile_needs_birthday(filled, set(classified)):
            print("[step 5] 警告：年龄/出生日期字段仍为空，提交可能被校验拦下")
        else:
            print(f"[step 5] 提交前补齐生日字段：{filled}")

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
        last_error = None
        for attempt in range(1, 4):
            try:
                await page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=10000)
                return
            except PWTimeout as e:
                last_error = e
            except Exception as e:  # noqa: BLE001
                last_error = e
                if "ERR_CONNECTION_CLOSED" in str(e):
                    print(f"[step 6] 跳 chatgpt.com 遇到连接关闭，重试 {attempt}/3")
                    await asyncio.sleep(min(2 * attempt, 5))
                    continue
                raise
        if last_error:
            raise last_error
    else:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass


def random_profile():
    return random_first_name(), random_last_name(), random_birthday()


def ensure_password(password: str) -> str:
    return password.strip() or generate_password()
