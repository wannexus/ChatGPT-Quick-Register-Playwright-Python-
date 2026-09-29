"""跑一次 codex CLI 风格的 OAuth 授权拿完整凭据。

流程（前提：page 当前是已登录 chatgpt.com 的 Playwright Page）：
  1. 生成 PKCE verifier + challenge
  2. 构造 https://auth.openai.com/oauth/authorize?... 链接
  3. page.route 注册拦截 http://localhost:1455/** 的请求
  4. page.goto(authorize_url)
  5. OpenAI 看到当前 cookie 已登录 → 自动同意 → 302 到 localhost:1455/auth/callback?code=...
  6. 拦截到 callback，从 query 拿 code
  7. POST https://auth.openai.com/oauth/token 换出 {access_token, refresh_token, id_token}
  8. 解析 access_token / id_token JWT 拿 chatgpt_account_id / chatgpt_user_id / organization_id / plan_type
  9. 拼装 SUB2API 期望的 credentials 字典
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from core.http_utils import open_url

# ChatGPT/Codex 公开的 OAuth client_id（从用户提供的 access_token JWT 解出）
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid profile email offline_access"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

# 同意页/继续页可能出现的按钮文本（点了能继续往 callback 跳）
CONSENT_TEXTS = (
    "continue", "allow", "authorize", "approve", "yes",
    "继续", "同意", "授权", "允许", "确认",
)

# SUB2API export 里 credentials.model_mapping 默认值（来自用户样例）
DEFAULT_MODEL_MAPPING = {
    "gpt-5.2": "gpt-5.2",
    "gpt-5.2-mini": "gpt-5.2-mini",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-2026-03-05": "gpt-5.4-2026-03-05",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.5": "gpt-5.5",
    "gpt-image-1": "gpt-image-1",
    "gpt-image-1.5": "gpt-image-1.5",
    "gpt-image-2": "gpt-image-2",
}


# ---------------------------------------------------------------------------
# PKCE / 授权 URL
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_pkce() -> "tuple[str, str]":
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(state: str, code_challenge: str) -> str:
    """注：不带 `audience`——某些 OpenAI client 配置会因为多余 audience 报错。
    OpenAI 服务端会根据 client_id 自动附 audience 到 access_token 里。"""
    params = {
        "response_type": "code",
        "client_id": CODEX_CLIENT_ID,
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# JWT 解析（不验签，只读 payload）
# ---------------------------------------------------------------------------

def jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1]
        seg += "=" * ((4 - len(seg) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8", errors="replace"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 换 token
# ---------------------------------------------------------------------------

def exchange_code(
    code: str,
    code_verifier: str,
    *,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "redirect_uri": CODEX_REDIRECT_URI,
        "code": code,
        "code_verifier": code_verifier,
    }).encode("utf-8")

    req = urllib.request.Request(TOKEN_URL, method="POST", data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    })

    try:
        with open_url(req, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OAuth /token HTTP {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if proxy and not proxy_insecure and (
            "CERTIFICATE_VERIFY_FAILED" in reason or "self signed certificate in certificate chain" in reason
        ):
            raise RuntimeError(
                f"OAuth /token 网络错误: {reason}。当前代理看起来在注入 HTTPS 证书，请重试并加上 --proxy-insecure"
            ) from e
        raise RuntimeError(f"OAuth /token 网络错误: {reason}") from e

    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OAuth /token 返回非 JSON: {raw[:300]!r}") from e


# ---------------------------------------------------------------------------
# 拼装 credentials
# ---------------------------------------------------------------------------

def _format_expires_at(token_resp: Dict[str, Any], access_payload: Dict[str, Any]) -> str:
    expires_in = token_resp.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        ts = time.time() + float(expires_in)
    else:
        exp = access_payload.get("exp")
        if not isinstance(exp, (int, float)):
            return ""
        ts = float(exp)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


def build_codex_credentials(token_resp: Dict[str, Any], *, fallback_email: str = "") -> Dict[str, Any]:
    access_token = token_resp.get("access_token") or ""
    if not access_token:
        raise RuntimeError("OAuth /token 没返回 access_token")

    access_payload = jwt_payload(access_token)
    auth_claims = access_payload.get("https://api.openai.com/auth", {}) or {}
    profile_claims = access_payload.get("https://api.openai.com/profile", {}) or {}

    id_token = token_resp.get("id_token") or ""
    id_payload = jwt_payload(id_token)
    id_auth = id_payload.get("https://api.openai.com/auth", {}) or {}

    organizations = id_auth.get("organizations") or []
    org_id = ""
    for org in organizations:
        if isinstance(org, dict) and org.get("is_default"):
            org_id = str(org.get("id") or "")
            break
    if not org_id and organizations and isinstance(organizations[0], dict):
        org_id = str(organizations[0].get("id") or "")

    return {
        "access_token": access_token,
        "chatgpt_account_id": str(
            auth_claims.get("chatgpt_account_id")
            or id_auth.get("chatgpt_account_id")
            or ""
        ),
        "chatgpt_user_id": str(
            auth_claims.get("chatgpt_user_id")
            or id_auth.get("chatgpt_user_id")
            or ""
        ),
        "client_id": CODEX_CLIENT_ID,
        "email": str(
            profile_claims.get("email")
            or id_payload.get("email")
            or fallback_email
            or ""
        ),
        "expires_at": _format_expires_at(token_resp, access_payload),
        "id_token": id_token,
        "model_mapping": dict(DEFAULT_MODEL_MAPPING),
        "organization_id": org_id,
        "plan_type": str(
            auth_claims.get("chatgpt_plan_type")
            or id_auth.get("chatgpt_plan_type")
            or "free"
        ),
        "refresh_token": token_resp.get("refresh_token") or "",
    }


# ---------------------------------------------------------------------------
# 主入口：drive OAuth in Playwright
# ---------------------------------------------------------------------------

async def _click_with_force_fallback(loc, timeout_ms: int = 4000) -> bool:
    """先尝试普通 click，被 overlay 拦下就 force click，再不行就 dispatchEvent。"""
    try:
        await loc.click(timeout=timeout_ms)
        return True
    except Exception as e1:
        msg = str(e1)
        if "intercepts pointer" not in msg and "subtree intercepts" not in msg:
            try:
                await loc.click(timeout=2000, force=True)
                return True
            except Exception:
                pass
        else:
            try:
                await loc.click(timeout=2000, force=True)
                return True
            except Exception:
                pass
    try:
        await loc.evaluate("(el) => el.click()")
        return True
    except Exception:
        return False


async def _try_click_consent(page, account_email: str = "") -> bool:
    """OAuth consent / 选择账号页，自动点「Continue / Authorize / 继续」类按钮。"""
    import re as _re
    pattern = _re.compile("|".join(_re.escape(t) for t in CONSENT_TEXTS), _re.IGNORECASE)
    cur_url = page.url or ""

    # 特殊：choose-an-account 页。账号项是默认 submit 的 button，但通常没有 type=submit。
    if "choose-an-account" in cur_url:
        account_selectors = [
            "form[action*='choose-an-account'] button[name='session_id']",
            "button[name='session_id']",
            "button[data-dd-action-name='Select existing session']",
        ]
        if account_email:
            for selector in account_selectors:
                try:
                    matches = page.locator(selector).filter(has_text=account_email)
                    n = await matches.count()
                    for i in range(n):
                        btn = matches.nth(i)
                        if not await btn.is_visible():
                            continue
                        txt = ""
                        try:
                            txt = (await btn.inner_text()).strip()
                        except Exception:
                            pass
                        print(f"[codex-oauth] choose-an-account: 点击目标账号 text={txt!r}")
                        if await _click_with_force_fallback(btn):
                            return True
                except Exception:
                    continue

        for selector in account_selectors:
            try:
                buttons = page.locator(selector)
                n = await buttons.count()
                for i in range(n):
                    btn = buttons.nth(i)
                    try:
                        if not await btn.is_visible():
                            continue
                        txt = ""
                        try:
                            txt = (await btn.inner_text()).strip()
                        except Exception:
                            pass
                        if any(bad in txt.lower() for bad in ("remove", "移除", "登录至另一个", "创建帐户")):
                            continue
                        print(f"[codex-oauth] choose-an-account: 点击首个账号 text={txt!r}")
                        if await _click_with_force_fallback(btn):
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

    # 特殊：consent 页—首位可见 submit 通常就是「Continue / Allow」
    if "consent" in cur_url:
        try:
            submits = page.locator("button[type=submit], [role=button][type=submit]")
            n = await submits.count()
            for i in range(n):
                btn = submits.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    txt = ""
                    try:
                        txt = (await btn.inner_text()).strip().lower()
                    except Exception:
                        pass
                    # 排除明显的拒绝按钮
                    if any(bad in txt for bad in ("cancel", "deny", "reject", "取消", "拒绝")):
                        continue
                    print(f"[codex-oauth] {cur_url.rsplit('/', 1)[-1]}: 点击首个 submit 按钮 text={txt!r}")
                    if await _click_with_force_fallback(btn):
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    # 通用：按文本匹配
    try:
        candidates = page.locator(
            "button[type=submit], button, [role=button], input[type=submit], a"
        ).filter(has_text=pattern)
        n = await candidates.count()
    except Exception:
        return False
    for i in range(n):
        cand = candidates.nth(i)
        try:
            if not await cand.is_visible():
                continue
            txt = (await cand.inner_text()).strip().lower()
            if any(bad in txt for bad in ("cancel", "deny", "reject", "取消", "拒绝", "返回", "use a different")):
                continue
            print(f"[codex-oauth] 点击 consent 按钮 text={txt!r}")
            if await _click_with_force_fallback(cand):
                return True
        except Exception:
            continue
    return False


async def _save_debug(page, label: str):
    """超时时落截图 + HTML，便于排查。"""
    try:
        from datetime import datetime
        out_dir = (Path(__file__).resolve().parent.parent / "output" / "debug")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        png = out_dir / f"{label}-{stamp}.png"
        html = out_dir / f"{label}-{stamp}.html"
        try:
            await page.screenshot(path=str(png), full_page=True)
        except Exception:
            pass
        try:
            html.write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        print(f"[codex-oauth] debug 已存：{png.name} / {html.name}  url={page.url}")
    except Exception as e:
        print(f"[codex-oauth] 落 debug 失败：{e}")


EMAIL_INPUT_SELECTOR = (
    "input[type=email], input[name=email], "
    "input[autocomplete=email], input[autocomplete=username], "
    "input[placeholder*=mail i]"
)


async def _auth_page_text(page) -> str:
    try:
        return await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        return ""


async def _has_auth_soft_error(page) -> bool:
    text = (await _auth_page_text(page)).lower()
    return any(
        marker in text
        for marker in (
            "operation timed out",
            "操作超时",
            "something went wrong",
            "出了点问题",
            "糟糕",
            "route error",
        )
    )


async def _is_add_phone_page(page) -> bool:
    cur = page.url or ""
    if "/add-phone" in cur or "/phone-verification" in cur:
        return True
    try:
        has_form = await page.evaluate(
            """() => Boolean(
                document.querySelector('form[action*="/add-phone" i], form[action*="/phone-verification" i], input[name*="phone" i]:not([type="hidden"])')
            )"""
        )
    except Exception:
        has_form = False
    if not has_form:
        return False
    text = (await _auth_page_text(page)).lower()
    return any(
        marker in text
        for marker in (
            "add a phone",
            "provide a phone",
            "verify your phone",
            "添加手机",
            "添加手机号",
            "添加电话号码",
            "提供手机",
            "提供手机号",
            "验证手机",
            "验证手机号",
        )
    )


async def _try_recover_auth_soft_error(page, *, label: str = "auth") -> bool:
    """Click the OpenAI auth retry button when the page lands on a transient timeout."""
    if not await _has_auth_soft_error(page):
        return False

    selectors = [
        "button[data-dd-action-name='Try again']",
        "button:has-text('重试')",
        "button:has-text('Try again')",
        "[role=button]:has-text('重试')",
        "[role=button]:has-text('Try again')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                print(f"[codex-oauth] {label}: 检测到超时/错误页，点击重试恢复")
                if await _click_with_force_fallback(loc, timeout_ms=3000):
                    await asyncio.sleep(3.0)
                    return not await _has_auth_soft_error(page)
        except Exception:
            continue

    try:
        clicked = await page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                };
                const candidates = [...document.querySelectorAll('button, [role=button]')];
                const btn = candidates.find((el) => (
                    visible(el)
                    && !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && /重试|try\\s+again/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                ));
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        if clicked:
            print(f"[codex-oauth] {label}: 用 JS 点击重试恢复")
            await asyncio.sleep(3.0)
            return not await _has_auth_soft_error(page)
    except Exception:
        pass

    return False


async def _wait_for_oauth_progress(page, callback_future, old_url: str, timeout_seconds: float = 12.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if callback_future.done():
            return True
        cur = page.url or ""
        if cur != old_url or "auth.openai.com/log-in" not in cur:
            return True
        if await _has_visible_otp_input(page):
            return True
        if await _is_add_phone_page(page):
            return True
        if await _has_visible_account_picker(page):
            return True
        if not await _has_visible_login_email_input(page):
            return True
        if await _has_auth_soft_error(page):
            return False
        await asyncio.sleep(0.25)
    return False


async def _js_submit_login_email(page, account_email: str) -> bool:
    try:
        return bool(await page.evaluate(
            """({ email, selector }) => {
                const input = document.querySelector(selector);
                if (!input) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
                if (setter) setter.call(input, email);
                else input.value = email;
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
                const form = input.form || input.closest("form");
                const button = form?.querySelector(
                    "button[type=submit][name='intent'][value='email'], button[type=submit]"
                );
                if (button) {
                    button.click();
                    return true;
                }
                if (form?.requestSubmit) {
                    form.requestSubmit();
                    return true;
                }
                if (form?.submit) {
                    form.submit();
                    return true;
                }
                return false;
            }""",
            {"email": account_email, "selector": EMAIL_INPUT_SELECTOR},
        ))
    except Exception:
        return False


async def _fill_email_on_login(page, account_email: str) -> bool:
    try:
        loc = page.locator(EMAIL_INPUT_SELECTOR).first
        if await loc.count() == 0 or not await loc.is_visible():
            return False
        try:
            await loc.click(timeout=2000)
        except Exception:
            pass
        cur = ""
        try:
            cur = await loc.input_value(timeout=1500)
        except Exception:
            pass
        if cur.strip().lower() != account_email.lower():
            try:
                await loc.fill("", timeout=1500)
            except Exception:
                pass
            await loc.fill(account_email, timeout=2500)
            try:
                await loc.evaluate(
                    """(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
            except Exception:
                pass
            print(f"[codex-oauth] log-in: 已填邮箱 {account_email}")
            await asyncio.sleep(0.3)
        try:
            submit = page.locator(
                "form[action*='log-in'] button[type=submit][name='intent'][value='email'], "
                "form[action*='log-in'] button[type=submit], "
                "button[type=submit][name='intent'][value='email']"
            ).first
            if await submit.count() and await submit.is_visible():
                await submit.click(timeout=4000)
                print("[codex-oauth] log-in: 已点击继续")
            else:
                await loc.press("Enter", timeout=1500)
                print("[codex-oauth] log-in: 已按 Enter 提交")
        except Exception:
            try:
                await loc.press("Enter", timeout=1500)
                print("[codex-oauth] log-in: 已按 Enter 提交")
            except Exception:
                if await _js_submit_login_email(page, account_email):
                    print("[codex-oauth] log-in: 已用 JS requestSubmit 提交")
                else:
                    return False
        return True
    except Exception as e:
        print(f"[codex-oauth] log-in: 填邮箱失败 {e}")
        return False


async def _has_visible_login_email_input(page) -> bool:
    try:
        loc = page.locator(EMAIL_INPUT_SELECTOR).first
        return bool(await loc.count()) and await loc.is_visible()
    except Exception:
        return False


async def _fill_otp_code(page, code: str) -> bool:
    try:
        boxes = page.locator("input[maxlength='1']")
        n = await boxes.count()
        if n >= 6:
            for i in range(min(6, n)):
                try:
                    await boxes.nth(i).fill(code[i] if i < len(code) else "")
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            print("[codex-oauth] email-verification: 已填入 OTP")
            return True
        single = page.locator(
            "input[name*=code i], input[placeholder*=code i], input[inputmode=numeric], input[type=text]"
        ).first
        if await single.count() and await single.is_visible():
            await single.fill(code)
            try:
                await single.press("Enter")
            except Exception:
                pass
            print("[codex-oauth] email-verification: 已填入 OTP（单字段）")
            return True
    except Exception as e:
        print(f"[codex-oauth] OTP 填入失败 {type(e).__name__}")
    return False


async def _has_visible_otp_input(page) -> bool:
    try:
        boxes = page.locator("input[maxlength='1']")
        if await boxes.count() and await boxes.first.is_visible():
            return True
    except Exception:
        pass
    try:
        single = page.locator(
            "input[name*=code i], input[placeholder*=code i], "
            "input[autocomplete='one-time-code'], input[inputmode='numeric']"
        ).first
        return bool(await single.count()) and await single.is_visible()
    except Exception:
        return False


async def _has_visible_account_picker(page) -> bool:
    selectors = (
        "button[name='session_id']",
        "button[data-dd-action-name='Select existing session']",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# 5sim phone verifier — 用于 add-phone 页自动接码（支持号码复用，最大复用3次）
# ---------------------------------------------------------------------------

# OpenAI add-phone 页元素选择器（参考 codex-oauth-automation-extension-Ultra9.7）
PHONE_FORM_SELECTOR = "form[action*='/add-phone' i]"
PHONE_COUNTRY_SELECT = "form[action*='/add-phone' i] select, select[name*='country' i]"
PHONE_INPUT_TEL = "form[action*='/add-phone' i] input[type='tel' i]"
PHONE_INPUT_HIDDEN = "form[action*='/add-phone' i] input[name='phoneNumber']"
PHONE_SUBMIT_BUTTON = "form[action*='/add-phone' i] button[type='submit']"

# phone-verification 页 OTP 选择器
PHONE_CODE_INPUT = (
    "form[action*='/phone-verification' i] input[name='code'], "
    "input[autocomplete='one-time-code'], "
    "input[inputmode='numeric']"
)


# ---------------------------------------------------------------------------
# 国家 / E.164 国际区号映射
# ---------------------------------------------------------------------------
# 5sim 返回的 country 是英文 slug（poland / greece / england ...），而 OpenAI add-phone
# 页的国家控件是 React Aria Select：
#   * 可见值形如「美国 (+1)」= 本地化国名 + 区号（随页面语言变化）
#   * 隐藏的原生 <select> option value 是 ISO-3166 alpha-2（AL / PL / GR ...），
#     option 文本是本地化国名（中文页面下是「波兰」，既无英文名也无区号数字）
# 所以只能按「号码真实区号 → ISO → option value」定位国家。旧实现按英文国名/文本匹配，
# 中文页面必然全部失配，然后落到「兜底 index=1」= 阿尔巴尼亚 (+355)，
# 于是页面区号与 5sim 号码区号不一致（买到的号码和页面提交的号码不是同一个）。

_ISO_DIAL: Dict[str, str] = {
    # 北美 NANP（+1）
    "US": "1", "CA": "1", "PR": "1", "DO": "1", "JM": "1", "TT": "1", "BB": "1",
    "BS": "1", "AG": "1", "GD": "1", "LC": "1", "VC": "1", "KN": "1", "DM": "1",
    "BM": "1", "TC": "1", "AS": "1", "GU": "1", "MP": "1",
    # 欧洲
    "RU": "7", "KZ": "7", "GR": "30", "NL": "31", "BE": "32", "FR": "33",
    "ES": "34", "HU": "36", "IT": "39", "VA": "39", "RO": "40", "CH": "41",
    "AT": "43", "GB": "44", "GG": "44", "IM": "44", "JE": "44", "DK": "45",
    "SE": "46", "NO": "47", "SJ": "47", "PL": "48", "DE": "49", "GI": "350",
    "PT": "351", "LU": "352", "IE": "353", "IS": "354", "AL": "355", "MT": "356",
    "CY": "357", "FI": "358", "AX": "358", "BG": "359", "LT": "370", "LV": "371",
    "EE": "372", "MD": "373", "AM": "374", "BY": "375", "AD": "376", "MC": "377",
    "SM": "378", "UA": "380", "RS": "381", "ME": "382", "XK": "383", "HR": "385",
    "SI": "386", "BA": "387", "MK": "389", "CZ": "420", "SK": "421", "LI": "423",
    "FO": "298", "GL": "299", "TR": "90", "GE": "995", "AZ": "994",
    # 亚洲 / 中东 / 中亚
    "CN": "86", "HK": "852", "MO": "853", "TW": "886", "JP": "81", "KR": "82",
    "KP": "850", "MN": "976", "IN": "91", "PK": "92", "AF": "93", "LK": "94",
    "MM": "95", "NP": "977", "BD": "880", "BT": "975", "MV": "960", "TH": "66",
    "LA": "856", "KH": "855", "VN": "84", "MY": "60", "SG": "65", "BN": "673",
    "ID": "62", "PH": "63", "TL": "670", "IR": "98", "IQ": "964", "SA": "966",
    "YE": "967", "OM": "968", "PS": "970", "AE": "971", "IL": "972", "BH": "973",
    "QA": "974", "KW": "965", "JO": "962", "LB": "961", "SY": "963", "UZ": "998",
    "TM": "993", "TJ": "992", "KG": "996",
    # 非洲
    "EG": "20", "LY": "218", "TN": "216", "DZ": "213", "MA": "212", "EH": "212",
    "SD": "249", "SS": "211", "ET": "251", "ER": "291", "DJ": "253", "SO": "252",
    "KE": "254", "TZ": "255", "UG": "256", "RW": "250", "BI": "257", "MZ": "258",
    "ZM": "260", "MG": "261", "RE": "262", "YT": "262", "ZW": "263", "NA": "264",
    "MW": "265", "LS": "266", "BW": "267", "SZ": "268", "KM": "269", "ZA": "27",
    "SH": "290", "CV": "238", "ST": "239", "CM": "237", "CF": "236", "TD": "235",
    "NE": "227", "TG": "228", "BJ": "229", "MR": "222", "ML": "223", "GN": "224",
    "CI": "225", "BF": "226", "GH": "233", "NG": "234", "GM": "220", "SN": "221",
    "SL": "232", "LR": "231", "GW": "245", "GA": "241", "CG": "242", "CD": "243",
    "AO": "244", "GQ": "240", "SC": "248", "MU": "230",
    # 美洲
    "MX": "52", "CU": "53", "AR": "54", "BR": "55", "CL": "56", "CO": "57",
    "VE": "58", "PE": "51", "PA": "507", "CR": "506", "NI": "505", "HN": "504",
    "SV": "503", "GT": "502", "BZ": "501", "BO": "591", "GY": "592", "EC": "593",
    "GF": "594", "PY": "595", "MQ": "596", "SR": "597", "UY": "598", "CW": "599",
    "AW": "297", "HT": "509", "GP": "590", "BL": "590", "MF": "590", "PM": "508",
    "FK": "500",
    # 大洋洲
    "AU": "61", "CX": "61", "CC": "61", "NZ": "64", "PN": "64", "FJ": "679",
    "PG": "675", "SB": "677", "VU": "678", "NC": "687", "WF": "681", "WS": "685",
    "KI": "686", "TV": "688", "TO": "676", "PF": "689", "CK": "682", "NU": "683",
    "MH": "692", "FM": "691", "PW": "680", "NR": "674", "NF": "672",
}

# 5sim 国家 slug（英文名，去空格/连字符）→ ISO alpha-2。
# 主要在同一区号对应多国时用于锁定正确国家（+1 / +7 / +44 / +61 / +64 / +212 / +262 / +590 / +599 ...）。
_FIVESIM_COUNTRY_ISO: Dict[str, str] = {
    "usa": "US", "unitedstates": "US", "canada": "CA", "puertorico": "PR",
    "dominicanrepublic": "DO", "jamaica": "JM", "trinidadandtobago": "TT",
    "bahamas": "BS", "barbados": "BB", "bermuda": "BM", "turksandcaicos": "TC",
    "england": "GB", "uk": "GB", "unitedkingdom": "GB", "greatbritain": "GB",
    "scotland": "GB", "wales": "GB", "northernireland": "GB", "gibraltar": "GI",
    "russia": "RU", "kazakhstan": "KZ", "greece": "GR", "netherlands": "NL",
    "holland": "NL", "belgium": "BE", "france": "FR", "spain": "ES", "hungary": "HU",
    "italy": "IT", "romania": "RO", "switzerland": "CH", "austria": "AT",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "poland": "PL", "germany": "DE",
    "portugal": "PT", "luxembourg": "LU", "ireland": "IE", "iceland": "IS",
    "albania": "AL", "malta": "MT", "cyprus": "CY", "finland": "FI", "bulgaria": "BG",
    "lithuania": "LT", "latvia": "LV", "estonia": "EE", "moldova": "MD",
    "armenia": "AM", "belarus": "BY", "andorra": "AD", "monaco": "MC",
    "sanmarino": "SM", "ukraine": "UA", "serbia": "RS", "montenegro": "ME",
    "croatia": "HR", "slovenia": "SI", "bosnia": "BA",
    "bosniaandherzegovina": "BA", "macedonia": "MK", "northmacedonia": "MK",
    "czech": "CZ", "czechia": "CZ", "slovakia": "SK", "liechtenstein": "LI",
    "georgia": "GE", "azerbaijan": "AZ", "turkey": "TR", "kosovo": "XK",
    "faroeislands": "FO", "greenland": "GL",
    "china": "CN", "hongkong": "HK", "macau": "MO", "macao": "MO", "taiwan": "TW",
    "japan": "JP", "southkorea": "KR", "korea": "KR", "northkorea": "KP",
    "mongolia": "MN", "india": "IN", "pakistan": "PK", "afghanistan": "AF",
    "srilanka": "LK", "nepal": "NP", "bangladesh": "BD", "bhutan": "BT",
    "maldives": "MV", "myanmar": "MM", "thailand": "TH", "laos": "LA",
    "cambodia": "KH", "vietnam": "VN", "malaysia": "MY", "singapore": "SG",
    "brunei": "BN", "indonesia": "ID", "philippines": "PH", "timorleste": "TL",
    "iran": "IR", "iraq": "IQ", "saudiarabia": "SA", "yemen": "YE", "oman": "OM",
    "palestine": "PS", "uae": "AE", "unitedarabemirates": "AE", "israel": "IL",
    "bahrain": "BH", "qatar": "QA", "kuwait": "KW", "jordan": "JO", "lebanon": "LB",
    "syria": "SY", "uzbekistan": "UZ", "turkmenistan": "TM", "tajikistan": "TJ",
    "kyrgyzstan": "KG",
    "egypt": "EG", "libya": "LY", "tunisia": "TN", "algeria": "DZ", "morocco": "MA",
    "westernsahara": "EH", "sudan": "SD", "southsudan": "SS", "ethiopia": "ET",
    "eritrea": "ER", "djibouti": "DJ", "somalia": "SO", "kenya": "KE",
    "tanzania": "TZ", "uganda": "UG", "rwanda": "RW", "burundi": "BI",
    "mozambique": "MZ", "zambia": "ZM", "madagascar": "MG", "reunion": "RE",
    "mayotte": "YT", "zimbabwe": "ZW", "namibia": "NA", "malawi": "MW",
    "lesotho": "LS", "botswana": "BW", "swaziland": "SZ", "eswatini": "SZ",
    "comoros": "KM", "southafrica": "ZA", "sthelena": "SH", "capeverde": "CV",
    "caboverde": "CV", "saotomeandprincipe": "ST", "cameroon": "CM",
    "centralafricanrepublic": "CF", "chad": "TD", "niger": "NE", "togo": "TG",
    "benin": "BJ", "mauritania": "MR", "mali": "ML", "guinea": "GN",
    "ivorycoast": "CI", "cotedivoire": "CI", "burkinafaso": "BF", "ghana": "GH",
    "nigeria": "NG", "gambia": "GM", "senegal": "SN", "sierraleone": "SL",
    "liberia": "LR", "guineabissau": "GW", "gabon": "GA", "congo": "CG",
    "republicofthecongo": "CG", "democraticrepublicofthecongo": "CD",
    "congokinshasa": "CD", "angola": "AO", "equatorialguinea": "GQ",
    "seychelles": "SC", "mauritius": "MU",
    "mexico": "MX", "cuba": "CU", "argentina": "AR", "brazil": "BR", "chile": "CL",
    "colombia": "CO", "venezuela": "VE", "peru": "PE", "panama": "PA",
    "costarica": "CR", "nicaragua": "NI", "honduras": "HN", "elsalvador": "SV",
    "guatemala": "GT", "belize": "BZ", "bolivia": "BO", "guyana": "GY",
    "ecuador": "EC", "frenchguiana": "GF", "paraguay": "PY", "martinique": "MQ",
    "suriname": "SR", "uruguay": "UY", "curacao": "CW", "aruba": "AW", "haiti": "HT",
    "guadeloupe": "GP", "saintbarthelemy": "BL", "saintmartin": "MF",
    "saintpierreandmiquelon": "PM", "falklandislands": "FK",
    "australia": "AU", "newzealand": "NZ", "fiji": "FJ", "papuanewguinea": "PG",
    "solomonislands": "SB", "vanuatu": "VU", "newcaledonia": "NC",
    "wallisandfutuna": "WF", "samoa": "WS", "kiribati": "KI", "tuvalu": "TV",
    "tonga": "TO", "frenchpolynesia": "PF", "cookislands": "CK", "niue": "NU",
    "marshallislands": "MH", "micronesia": "FM", "palau": "PW", "nauru": "NR",
    "guam": "GU", "northernmarianaislands": "MP", "americansamoa": "AS",
    "norfolkisland": "NF",
}

# 一个区号对应多国时的默认国家（纯按字母序兜底会取到意外国家）
_DIAL_PREFERRED_ISO: Dict[str, str] = {
    "1": "US", "7": "RU", "39": "IT", "44": "GB", "47": "NO", "61": "AU",
    "64": "NZ", "212": "MA", "262": "RE", "358": "FI", "590": "GP", "599": "CW",
    "850": "KP",
}

_DIAL_CODES = frozenset(_ISO_DIAL.values())

_DIAL_TO_ISO: Dict[str, str] = {}
for _iso, _dial in _ISO_DIAL.items():
    _DIAL_TO_ISO.setdefault(_dial, _iso)
_DIAL_TO_ISO.update(_DIAL_PREFERRED_ISO)
del _iso, _dial

_SELECT_VALUE_DIAL_RE = re.compile(r"\(\s*\+\s*(\d{1,3})\s*\)")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", value if isinstance(value, str) else "")


def _norm_country_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower() if isinstance(value, str) else "")


def _extract_dial_code(phone: str) -> str:
    """从 5sim 号码（如 +447350690992）提取 E.164 国际区号。

    用完整区号表做最长前缀匹配。旧实现基于一个不完整的表，未命中就「取前 2 位」，
    会把 +351/+420/+998/+372 这类号码截成错区号（如 +351 → 35），接着国内号码也截错，
    填进页面的号码就和 5sim 买到的号码不一致。
    """
    digits = _digits(phone)
    if not digits:
        return ""
    for length in (3, 2, 1):
        if len(digits) >= length and digits[:length] in _DIAL_CODES:
            return digits[:length]
    return ""


def _iso_for_country(country: str) -> str:
    """5sim country slug / ISO 代码 → ISO alpha-2（未知返回空串）。"""
    key = _norm_country_key(country)
    if not key:
        return ""
    iso = _FIVESIM_COUNTRY_ISO.get(key)
    if iso:
        return iso
    if len(key) == 2 and key.upper() in _ISO_DIAL:
        return key.upper()
    return ""


def _iso_for_phone(phone: str, country_hint: str = "") -> str:
    """号码实际区号对应的 ISO；country_hint 与区号一致时优先采用该 hint。"""
    dial = _extract_dial_code(phone)
    if not dial:
        return ""
    hinted = _iso_for_country(country_hint)
    if hinted and _ISO_DIAL.get(hinted) == dial:
        return hinted
    return _DIAL_TO_ISO.get(dial, "")


def _match_country_option(options: list, iso: str, dial_code: str, country: str) -> Optional[dict]:
    """在 option 列表里找与号码区号一致的那一项。

    真实页面 option value 就是 ISO alpha-2，文本是本地化国名，所以第一优先按 value==ISO；
    其余分支兼容「文本带区号」「文本带英文国名」的实现。匹配不到返回 None，
    绝不返回任意兜底项——选错国家就等于把号码填到别的区号下提交。
    """
    if not isinstance(options, list) or not options:
        return None

    def value_of(opt: dict) -> str:
        return (opt.get("value") or "").strip()

    if iso:
        for opt in options:
            if isinstance(opt, dict) and value_of(opt).upper() == iso:
                return opt
    if dial_code:
        for opt in options:
            if isinstance(opt, dict) and _digits(value_of(opt)) == dial_code:
                return opt
    slug = _norm_country_key(country)
    if len(slug) >= 4:
        for opt in options:
            if not isinstance(opt, dict):
                continue
            hay = _norm_country_key(opt.get("text")) + _norm_country_key(opt.get("label"))
            if slug and slug in hay:
                return opt
    if dial_code:
        for opt in options:
            if not isinstance(opt, dict):
                continue
            for field in ("text", "label"):
                if f"+{dial_code}" in re.sub(r"\s+", "", opt.get(field) or ""):
                    return opt
        for opt in options:
            if not isinstance(opt, dict):
                continue
            for field in ("text", "label"):
                if _digits(opt.get(field)) == dial_code:
                    return opt
    return None


async def _read_locator_value(locator) -> str:
    """读输入框 value；非字符串（含测试 fixture 的 Mock）一律当空。"""
    try:
        value = await locator.evaluate("el => el.value")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


async def _read_selected_country_dial(page) -> str:
    """读页面当前所选国家的区号（按可信度排序的三个来源）。"""
    # 1) React Aria Select 的可见值：「美国 (+1)」——React 状态的真实反映
    try:
        loc = page.locator("[class*='react-aria-SelectValue']").first
        if await loc.count():
            text = await loc.text_content()
            if isinstance(text, str):
                found = _SELECT_VALUE_DIAL_RE.search(text)
                if found:
                    return found.group(1)
    except Exception:
        pass
    # 2) 电话号码输入框左侧的区号装饰
    try:
        loc = page.locator("[class*='inputDecorationCountryCode']").first
        if await loc.count():
            text = await loc.text_content()
            if isinstance(text, str):
                digits = _digits(text)
                if digits:
                    return digits
    except Exception:
        pass
    # 3) 退路：原生 select 当前值（ISO）反查
    try:
        sel = page.locator(PHONE_COUNTRY_SELECT).first
        if await sel.count():
            value = await _read_locator_value(sel)
            if value.strip():
                return _ISO_DIAL.get(value.strip().upper(), "")
    except Exception:
        pass
    return ""


async def _click_country_option(page, dial_code: str) -> bool:
    """弹层点选：原生 select 的 change 没能驱动 React 状态时，按「(+区号)」点选项。"""
    try:
        trigger = page.locator("[class*='react-aria-Select'] button[aria-haspopup='listbox']").first
        if await trigger.count() == 0 or not await trigger.is_visible():
            return False
        if not await _click_with_force_fallback(trigger, timeout_ms=3000):
            return False
        option = page.locator(f"[role='option']:has-text('(+{dial_code})')").first
        if await option.count() and await option.is_visible():
            if await _click_with_force_fallback(option, timeout_ms=3000):
                await asyncio.sleep(0.2)
                return True
    except Exception as e:
        print(f"[codex-oauth] add-phone: 弹层点选国家失败 {type(e).__name__}")
    return False


async def _verify_selected_country(page, dial_code: str) -> bool:
    """确认页面显示的区号就是号码区号；不一致绝不继续提交。"""
    actual = await _read_selected_country_dial(page)
    if actual == dial_code:
        print(f"[codex-oauth] add-phone: 页面国家区号 +{actual} 与号码一致")
        return True
    print(f"[codex-oauth] add-phone: 页面国家区号 +{actual or '?'} 与号码区号 +{dial_code} 不一致")
    return False


async def _select_phone_country(page, country: str, phone: str) -> bool:
    """在 add-phone 页选中与号码实际区号一致的国家。

    返回 True 仅当页面显示的区号确实等于号码区号；没有「随便选第一个」兜底，
    匹配不到就返回 False，由调用方取消订单，避免把号码填到错误区号下提交。
    """
    dial_code = _extract_dial_code(phone)
    if not dial_code:
        print(f"[codex-oauth] add-phone: 无法解析号码 ****{phone[-4:]} 的国际区号，不选国家")
        return False
    iso = _iso_for_phone(phone, country)
    try:
        sel = page.locator(PHONE_COUNTRY_SELECT).first
        if await sel.count() == 0 or not await sel.is_visible():
            print("[codex-oauth] add-phone: 未找到国家选择控件")
            return False
        tag = await sel.evaluate("el => el.tagName.toLowerCase()")
        if tag != "select":
            print("[codex-oauth] add-phone: 国家控件不是原生 select，改为按区号弹层点选")
            if await _click_country_option(page, dial_code):
                return await _verify_selected_country(page, dial_code)
            return False

        options = await sel.evaluate("""(sel) => {
            return [...sel.options].map((o, i) => ({
                index: i,
                value: o.value,
                text: (o.textContent || '').trim(),
                label: (o.getAttribute('aria-label') || '')
            }));
        }""")
        target = _match_country_option(options, iso, dial_code, country)
        if target is None:
            print(f"[codex-oauth] add-phone: 国家列表里没有区号 +{dial_code}"
                  f"（5sim country={country or 'unknown'}），不提交错号码")
            if await _click_country_option(page, dial_code):
                return await _verify_selected_country(page, dial_code)
            return False

        await sel.select_option(index=int(target["index"]), timeout=3000)
        print(f"[codex-oauth] add-phone: 已按区号 +{dial_code} 选择国家 "
              f"(value={target.get('value') or target.get('text')!r})")
        await asyncio.sleep(0.2)
        if await _verify_selected_country(page, dial_code):
            return True
        # 原生 select 的 change 未驱动 React 状态时，改用弹层点选
        print("[codex-oauth] add-phone: 原生 select 未生效，改用弹层点选")
        if await _click_country_option(page, dial_code):
            return await _verify_selected_country(page, dial_code)
        return False
    except Exception as e:
        print(f"[codex-oauth] add-phone: 选择国家失败 {type(e).__name__}")
        return False


def create_5sim_phone_verifier(
    api_key: str,
    country: str = "any",
    operator: str = "any",
    product: str = "openai",
    *,
    max_price: float | None = None,
    candidate_limit: int = 8,
    acquire_priority: str = "rate",
    reuse_pool: "num5sim.ActivationPool | None" = None,
    account_id: int | None = None,
    account_store=None,
    max_accounts_per_phone: int = 3,
    max_buy_attempts: int = 3,
    poll_interval: float = 5.0,
    poll_timeout: float = 120.0,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
):
    """创建一个 phone_verifier callable，用 5sim 自动完成 OpenAI add-phone 流程。

    流程：
    1. 优先从复用池中查找可复用号码，调用 5sim reuse API
    2. 无可用号码时，新购一个（带 reuse=1 参数优先服务器端复用）
    3. 填写 OpenAI add-phone 表单（国家选择 + 号码）
    4. 轮询 SMS → 填入验证码
    5. add-phone 页面接受 OTP 后把手机号原子绑定到当前 MySQL 账号；复用池仅存订单元数据

    用法：
        pool = num5sim.ActivationPool.load()
        verifier = create_5sim_phone_verifier(api_key="...", country="vietnam", reuse_pool=pool)
        creds = await run_codex_oauth(page, ..., phone_verifier=verifier)
        pool.save()
    """
    from core import num5sim

    buy_limit = max(1, int(max_buy_attempts))

    async def run_5sim(call_factory, *, action: str, retry_direct: bool = True):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: call_factory(proxy, proxy_insecure))
        except Exception as error:
            if isinstance(error, num5sim.FiveSimGatewayError) and not retry_direct:
                raise RuntimeError(
                    "5sim 买号/复用请求已发出，但网关返回 502/504，未取得号码；"
                    "请先核对 5sim 订单记录再重试，可在短信设置中切换代理/直连"
                ) from error
            if proxy and retry_direct and not isinstance(error, num5sim.FiveSimError):
                print(f"[codex-oauth] add-phone: {action} 经代理失败，改直连重试 ({type(error).__name__})")
                return await loop.run_in_executor(None, lambda: call_factory(None, False))
            raise

    @asynccontextmanager
    async def selected_order():
        if account_id is None or account_store is None:
            raise RuntimeError("自动 5sim 接码需要 MySQL 账号绑定上下文")
        current = await asyncio.get_running_loop().run_in_executor(None, account_store.get_account, int(account_id))
        if not current:
            raise RuntimeError("MySQL 账号不存在；不购买临时号码")
        if current.get("codexPhoneNumber"):
            raise RuntimeError("该账号已有手机号绑定；请核对账号，不重复分配临时号码")
        loop = asyncio.get_running_loop()
        excluded_phones: set[str] = set()
        buy_attempts = 0
        print(f"[codex-oauth] add-phone: 开始 5sim 自动接码  proxy={'set' if proxy else 'NOT SET'}  proxy_insecure={proxy_insecure}")

        async def reserve(phone):
            slot = account_store.reserve_codex_phone(int(account_id), phone,
                                                     max_accounts=max_accounts_per_phone)
            entering = loop.run_in_executor(None, slot.__enter__)
            try:
                bind = await asyncio.shield(entering)
            except asyncio.CancelledError:
                try:
                    await entering
                finally:
                    await loop.run_in_executor(None, slot.__exit__, None, None, None)
                raise
            if bind is None:
                await loop.run_in_executor(None, slot.__exit__, None, None, None)
                return None, None
            return slot, bind

        # ── 1) 优先复用MySQL尚未达到账号上限的号码 ──
        async def buy_fresh_number() -> Any:
            nonlocal buy_attempts

            async def attempt_buy(country_value: str, operator_value: str):
                nonlocal buy_attempts
                if buy_attempts >= buy_limit:
                    raise RuntimeError(f"已达到每次手机号验证最多购买 {buy_limit} 个 5sim 订单的安全上限")
                buy_attempts += 1
                print(f"[codex-oauth] add-phone: 购买号码 country={country_value} operator={operator_value} product={product} attempt={buy_attempts}/{buy_limit}")
                return await run_5sim(
                    lambda call_proxy, call_proxy_insecure: num5sim.buy_activation(
                        api_key=api_key, country=country_value, operator=operator_value,
                        product=product, max_price=max_price, enable_reuse=not excluded_phones,
                        proxy=call_proxy, proxy_insecure=call_proxy_insecure,
                    ),
                    action=f"5sim 购买号码 {country_value}/{operator_value}",
                    retry_direct=False,
                )

            try:
                return await attempt_buy(country, operator)
            except num5sim.FiveSimNoFreePhonesError as buy_error:
                candidates = await run_5sim(
                    lambda call_proxy, call_proxy_insecure: num5sim.find_buy_candidates(
                        product=product, country=country, operator=operator,
                        max_price=max_price, proxy=call_proxy,
                        proxy_insecure=call_proxy_insecure, limit=max(1, candidate_limit),
                        priority=acquire_priority,
                    ),
                    action="5sim 查询候选库存",
                )
                for candidate in candidates:
                    try:
                        return await attempt_buy(candidate.country, candidate.operator)
                    except num5sim.FiveSimNoFreePhonesError:
                        continue
                raise RuntimeError("5sim 无可用号码或已达到候选购买上限") from buy_error

        # ── 1b) 新购号码时也以同一有界函数获取号码 ──
        order = None
        slot = None
        bind = None
        # The reuse endpoint has no price ceiling; use capped purchases when configured.
        if reuse_pool is not None and max_price is None:
            while True:
                reusable = reuse_pool.find_usable(
                    country=country if country != "any" else "",
                    operator=operator if operator != "any" else "",
                    product=product,
                    exclude_phones=excluded_phones,
                )
                if not reusable:
                    break
                slot, bind = await reserve(reusable.phone)
                if slot is None:
                    excluded_phones.add(reusable.phone)
                    continue
                print(f"[codex-oauth] add-phone: 尝试复用号码 ****{reusable.phone[-4:]}")
                try:
                    order = await run_5sim(
                        lambda call_proxy, call_proxy_insecure: num5sim.reuse_number(
                            api_key=api_key, phone=reusable.phone, product=product,
                            proxy=call_proxy, proxy_insecure=call_proxy_insecure,
                        ),
                        action="5sim 复用号码",
                        retry_direct=False,
                    )
                    if order.phone != reusable.phone:
                        raise RuntimeError("5sim 复用返回了不同号码；当前账号跳过")
                    break
                except BaseException as error:
                    await loop.run_in_executor(None, slot.__exit__, None, None, None)
                    slot = None
                    excluded_phones.add(reusable.phone)
                    order = None
                    if not isinstance(error, num5sim.FiveSimNoFreePhonesError):
                        raise

        # ── 2) 无可复用号码则新购 ──
        while order is None:
            order = await buy_fresh_number()
            slot, bind = await reserve(order.phone)
            if slot is None:
                excluded_phones.add(order.phone)
                print(f"[codex-oauth] add-phone: 号码 ****{order.phone[-4:]} 已满额，取消订单并换号")
                await run_5sim(
                    lambda call_proxy, call_proxy_insecure: num5sim.cancel_order(
                        api_key=api_key, order_id=order.id, proxy=call_proxy,
                        proxy_insecure=call_proxy_insecure,
                    ), action="取消满额号码订单",
                )
                order = None

        try:
            yield order, bind
        finally:
            await loop.run_in_executor(None, slot.__exit__, None, None, None)

    async def submit_order(page, order, bind_verified_phone):
        loop = asyncio.get_running_loop()

        phone = order.phone
        order_id = order.id
        print(f"[codex-oauth] add-phone: 已取得号码 ****{phone[-4:]} 订单={order_id}")

        # ── 3) 填写 add-phone 表单 ──
        # 3a) 选择国家：必须与号码真实区号一致，否则本号码不发码（宁可不发也不发错号码）
        actual_country = order.country or country
        dial_code = _extract_dial_code(phone)
        if not dial_code:
            await _save_debug(page, "codex-oauth-add-phone-unknown-dial")
            raise RuntimeError(
                f"无法从 5sim 号码 ****{phone[-4:]} 解析国际区号"
                f"（5sim country={actual_country or 'unknown'}）；放弃提交并取消订单"
            )
        country_ok = await _select_phone_country(page, actual_country, phone)
        if not country_ok:
            await _save_debug(page, "codex-oauth-add-phone-country-mismatch")
            raise RuntimeError(
                f"add-phone 页未选中与号码 ****{phone[-4:]} 一致的国家"
                f"（区号 +{dial_code}，5sim country={actual_country or 'unknown'}）；"
                "放弃提交并取消订单，避免把号码填到错误区号下"
            )

        # 3b) 填写号码到可见的 tel 输入框（去掉国际区号，只填国内部分）
        phone_national = _digits(phone)
        if phone_national.startswith(dial_code):
            phone_national = phone_national[len(dial_code):]
        if len(phone_national) < 6:
            await _save_debug(page, "codex-oauth-add-phone-bad-national")
            raise RuntimeError(
                f"号码区号拆分异常（区号 +{dial_code}，国内部分 {len(phone_national)} 位）；"
                "放弃提交并取消订单"
            )

        page_dial = await _read_selected_country_dial(page)
        if page_dial and page_dial != dial_code:
            await _save_debug(page, "codex-oauth-add-phone-dial-mismatch")
            raise RuntimeError(
                f"add-phone 页当前国家区号 +{page_dial} 与号码区号 +{dial_code} 不一致；"
                "放弃提交并取消订单"
            )

        try:
            tel_input = page.locator(PHONE_INPUT_TEL).first
            if await tel_input.count() == 0 or not await tel_input.is_visible():
                tel_input = page.locator(
                    "input[type='tel' i], input:not([type='hidden']):not([type='submit']):not([type='checkbox'])"
                ).first
            await tel_input.click(timeout=3000)
            await tel_input.fill("", timeout=2000)
            await tel_input.fill(phone_national, timeout=5000)
            await asyncio.sleep(0.3)
            print("[codex-oauth] add-phone: 已填写临时号码")
        except Exception as e:
            await _save_debug(page, "codex-oauth-add-phone-fill-fail")
            raise RuntimeError(f"填写号码失败：{e}") from e

        # 3c) 规范化字段 phoneNumber：页面自己会拼 E.164，若与 5sim 号码不一致就覆盖成真实号码，
        #     否则提交的会是「页面区号 + 国内号码」拼出的错号码（区号不一致的最终表现）。
        try:
            hidden = page.locator(PHONE_INPUT_HIDDEN).first
            if await hidden.count():
                current = await _read_locator_value(hidden)
                if _digits(current) == _digits(phone):
                    print("[codex-oauth] add-phone: phoneNumber 字段与 5sim 号码一致")
                else:
                    await hidden.evaluate(f"el => {{ el.value = '{phone}'; "
                                          "el.dispatchEvent(new Event('input', {bubbles:true})); "
                                          "el.dispatchEvent(new Event('change', {bubbles:true})); }")
                    print("[codex-oauth] add-phone: 已用 5sim 真实号码覆盖 phoneNumber 字段")
        except Exception:
            pass

        # 3d) 点击提交按钮
        clicked = False
        for sel in [PHONE_SUBMIT_BUTTON,
                     "button[type='submit']",
                     "button:has-text('Send code' i)",
                     "button:has-text('发送验证码' i)",
                     "button:has-text('Continue' i)",
                     "button:has-text('继续' i)"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    if await _click_with_force_fallback(btn, timeout_ms=4000):
                        clicked = True
                        print(f"[codex-oauth] add-phone: 已点击 {sel}")
                        break
            except Exception:
                continue
        if not clicked:
            await _save_debug(page, "codex-oauth-add-phone-no-submit")
            raise RuntimeError("找不到「发送验证码」按钮")

        # ── 4) 轮询等待 SMS ──
        print(f"[codex-oauth] add-phone: 等待 5sim SMS（最多 {poll_timeout}s）...")
        deadline = loop.time() + poll_timeout
        sms_code = None
        while loop.time() < deadline:
            try:
                order = await run_5sim(
                    lambda call_proxy, call_proxy_insecure: num5sim.check_order(
                        api_key=api_key,
                        order_id=order_id,
                        proxy=call_proxy,
                        proxy_insecure=call_proxy_insecure,
                    ),
                    action="5sim 查询订单",
                )
            except Exception as e:
                print(f"[codex-oauth] add-phone: 5sim check 异常 {type(e).__name__}")
                await asyncio.sleep(poll_interval)
                continue

            sms_code = order.code
            if sms_code:
                print("[codex-oauth] add-phone: 收到短信验证码")
                break
            print(f"[codex-oauth] add-phone: 状态={order.status} 尚无 SMS，{poll_interval}s 后重试...")
            await asyncio.sleep(poll_interval)

        if not sms_code:
            try:
                await run_5sim(
                    lambda call_proxy, call_proxy_insecure: num5sim.cancel_order(
                        api_key=api_key,
                        order_id=order_id,
                        proxy=call_proxy,
                        proxy_insecure=call_proxy_insecure,
                    ),
                    action="5sim 取消订单",
                )
            except Exception:
                pass
            await _save_debug(page, "codex-oauth-5sim-sms-timeout")
            raise RuntimeError(f"5sim 等待 SMS 超时（{poll_timeout}s），订单 {order_id} 已取消")

        # ── 5) 填写 OTP ──
        if not await _fill_otp_code(page, sms_code):
            raise RuntimeError("填写手机验证码 OTP 失败")

        print("[codex-oauth] add-phone: OTP 已填入，等待页面继续")
        accepted = False
        for _ in range(24):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2000)
                if (not await _is_add_phone_page(page)
                        and not await _has_visible_otp_input(page)
                        and not await _has_auth_soft_error(page)
                        and "/error" not in (page.url or "") and "error=" not in (page.url or "")):
                    accepted = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not accepted:
            raise RuntimeError("手机验证码提交后页面未离开验证步骤；号码未绑定")
        try:
            binding = loop.run_in_executor(None, bind_verified_phone)
            try:
                claim = await asyncio.shield(binding)
            except asyncio.CancelledError:
                # Finish the durable write before the reservation connection closes.
                await binding
                raise
        except Exception as bind_error:
            # OTP 可能已生效； never assign this number elsewhere until MySQL reconciliation.
            if reuse_pool is not None:
                reuse_pool.remove(phone, product)
                reuse_pool.save()
            raise RuntimeError("OTP 已被页面接受，但 MySQL 手机号绑定确认失败；流程停止，需先核对账号记录") from bind_error
        if not claim.get("ok"):
            raise RuntimeError("OTP 已接受，但 MySQL 未确认手机号绑定；请核对账号记录")
        print(f"[codex-oauth] add-phone: MySQL 已绑定账号 ID={account_id}，此号码已占用 {claim.get('account_count')}/{max_accounts_per_phone} 个账号名额")

        # ── 6) 完成订单 & 更新复用池 ──
        try:
            await run_5sim(
                lambda call_proxy, call_proxy_insecure: num5sim.finish_order(
                    api_key=api_key,
                    order_id=order_id,
                    proxy=call_proxy,
                    proxy_insecure=call_proxy_insecure,
                ),
                action="5sim 完成订单",
            )
            print(f"[codex-oauth] add-phone: 5sim 订单 {order_id} 已标记完成")
        except Exception as e:
            print(f"[codex-oauth] add-phone: 5sim finish 失败（不影响绑定）: {type(e).__name__}")

        # 写入复用池（每号最多复用 DEFAULT_MAX_USES=3 次）
        if reuse_pool is not None:
            entry = reuse_pool.add_or_update(
                phone=phone, country=actual_country,
                operator=order.operator or operator,
                product=product, order_id=order_id,
            )
            print(f"[codex-oauth] add-phone: 复用池订单元数据已更新 ****{phone[-4:]}")
            reuse_pool.prune_expired()
            reuse_pool.save()

    async def verify(page) -> None:
        async with selected_order() as (order, bind):
            try:
                await submit_order(page, order, bind)
            except BaseException:
                try:
                    await run_5sim(
                        lambda call_proxy, call_proxy_insecure: num5sim.cancel_order(
                            api_key=api_key, order_id=order.id, proxy=call_proxy,
                            proxy_insecure=call_proxy_insecure,
                        ), action="取消未完成订单",
                    )
                except Exception:
                    print(f"[codex-oauth] add-phone: 订单 {order.id} 取消失败，请核对订单状态")
                raise

    return verify


async def run_codex_oauth(
    page,
    *,
    account_email: str = "",
    fetch_code=None,
    phone_verifier: Optional[Callable[[Any], Awaitable[None]]] = None,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 180.0,
) -> Dict[str, Any]:
    """跑一次 codex OAuth，自动应对 log-in / email-verification / choose-an-account / consent 中间页。

    `fetch_code`: async () -> str，给 OAuth 阶段拉新 OTP（OAuth 走的是另一封 OTP 邮件）。
    `phone_verifier`: async (page) -> None，可选；用于在非 headless 模式下等待用户手动完成手机号验证。
    """
    if fetch_code is None:
        raise ValueError("run_codex_oauth 需要 fetch_code（与 step4 的 OTP 拿码 callable 一样）")

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(state, challenge)

    loop = asyncio.get_event_loop()
    callback_future: "asyncio.Future[str]" = loop.create_future()

    async def handle_route(route, request):
        try:
            if request.url.startswith("http://localhost:1455"):
                if request.url.startswith(CODEX_REDIRECT_URI):
                    if not callback_future.done():
                        callback_future.set_result(request.url)
                try:
                    await route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body="<!doctype html><html><body><h2>OAuth 授权完成，可关闭本页。</h2></body></html>",
                    )
                except Exception:
                    try:
                        await route.abort()
                    except Exception:
                        pass
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    callback_pattern = "http://localhost:1455/**"
    await page.route(callback_pattern, handle_route)

    callback_url = None
    try:
        print(f"[codex-oauth] navigating to authorize URL")
        try:
            await page.goto(url, wait_until="commit", timeout=20000)
        except Exception:
            pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        deadline = loop.time() + timeout
        last_url = ""
        last_action_at = 0.0
        action_cooldown = 4.0
        otp_filled = False
        login_attempts = 0
        last_login_url = ""
        consent_clicks = 0
        authorize_restarts = 0

        while loop.time() < deadline:
            try:
                callback_url = await asyncio.wait_for(asyncio.shield(callback_future), timeout=1.5)
                break
            except asyncio.TimeoutError:
                pass

            cur = page.url or ""
            if cur != last_url:
                print(f"[codex-oauth] 当前 url={cur}")
                last_url = cur
                last_action_at = 0.0

            if "error=" in cur and "/oauth/" in cur:
                await _save_debug(page, "codex-oauth-error-page")
                raise RuntimeError(f"OpenAI OAuth 错误页：{cur}")

            if await _is_add_phone_page(page):
                await _save_debug(page, "codex-oauth-add-phone")
                if phone_verifier is None:
                    raise RuntimeError(f"OpenAI 要求手机号验证，无法仅凭邮箱 OTP 完成 Codex OAuth。当前 URL: {page.url}")
                print("[codex-oauth] add-phone: 开始手机号验证")
                await phone_verifier(page)
                deadline = max(deadline, loop.time() + 120.0)
                otp_filled = False
                last_action_at = 0.0
                continue

            now = loop.time()
            if now - last_action_at < action_cooldown:
                continue

            # OTP 页：拿新 OTP 填进去。新版登录流可能停留在 /log-in 但 DOM 已切到 OTP。
            if not otp_filled and ("email-verification" in cur or await _has_visible_otp_input(page)):
                print("[codex-oauth] email-verification 页，拉一个新 OTP...")
                try:
                    code = await fetch_code()
                except Exception as e:
                    await _save_debug(page, "codex-oauth-otp-fetch-fail")
                    raise RuntimeError(f"OAuth 阶段拿 OTP 失败：{e}") from e
                if not code:
                    raise RuntimeError("OAuth 阶段没拿到 OTP")
                if await _fill_otp_code(page, code):
                    otp_filled = True
                    last_action_at = now
                    continue

            # choose-an-account / consent：点同意。部分情况下 URL 不变，但账号选择已可见。
            if "choose-an-account" in cur or "consent" in cur or await _has_visible_account_picker(page):
                if consent_clicks < 5 and await _try_click_consent(page, account_email):
                    consent_clicks += 1
                    last_action_at = now
                    continue

            # /log-in 页：填邮箱回车
            if "auth.openai.com/log-in" in cur:
                if account_email:
                    if await _has_auth_soft_error(page):
                        if await _try_recover_auth_soft_error(page, label="log-in"):
                            login_attempts = 0
                            last_login_url = ""
                            last_action_at = 0.0
                            continue
                        last_action_at = now - action_cooldown
                        print("[codex-oauth] log-in: 超时/错误页暂未恢复，稍后重试")
                        continue

                    if await _has_auth_soft_error(page) and authorize_restarts < 2:
                        authorize_restarts += 1
                        login_attempts = 0
                        last_login_url = ""
                        consent_clicks = 0
                        print(f"[codex-oauth] log-in: 检测到超时/错误页，重开 authorize URL ({authorize_restarts}/2)")
                        try:
                            await page.goto(url, wait_until="commit", timeout=12000)
                            last_action_at = now
                            await asyncio.sleep(1.0)
                            continue
                        except Exception as e:
                            print(f"[codex-oauth] log-in: 重开 authorize 失败 {e}")

                    if not await _has_visible_login_email_input(page):
                        last_action_at = now
                        continue

                    if cur != last_login_url:
                        login_attempts = 0
                        last_login_url = cur

                    if login_attempts < 3:
                        login_attempts += 1
                        print(f"[codex-oauth] log-in: 第 {login_attempts}/3 次提交邮箱")
                        if await _fill_email_on_login(page, account_email):
                            progressed = await _wait_for_oauth_progress(page, callback_future, cur, timeout_seconds=12.0)
                            if progressed:
                                last_action_at = 0.0
                            else:
                                last_action_at = now - action_cooldown
                                print("[codex-oauth] log-in: 提交后仍停留在登录页，准备重试")
                            continue

                    if authorize_restarts < 2:
                        authorize_restarts += 1
                        login_attempts = 0
                        last_login_url = ""
                        consent_clicks = 0
                        print(f"[codex-oauth] log-in: 仍未前进，重开 authorize URL ({authorize_restarts}/2)")
                        try:
                            await page.goto(url, wait_until="commit", timeout=12000)
                            last_action_at = now
                            await asyncio.sleep(1.0)
                            continue
                        except Exception as e:
                            print(f"[codex-oauth] log-in: 重开 authorize 失败 {e}")

                last_action_at = now
                continue

            # 兜底：还在 auth 域但状态不明，尝试通用同意按钮
            if ("auth.openai.com" in cur or "auth0.openai.com" in cur) and "log-in" not in cur and consent_clicks < 5:
                if await _try_click_consent(page, account_email):
                    consent_clicks += 1
                    last_action_at = now
                    continue

        if callback_url is None:
            await _save_debug(page, "codex-oauth-callback-timeout")
            raise RuntimeError(
                f"等 OAuth 回调超时（{timeout}s）。已落 debug 截图。"
                f"当前 URL: {page.url}。"
                f"  login_attempts={login_attempts}  authorize_restarts={authorize_restarts}"
                f"  otp_filled={otp_filled}  consent_clicks={consent_clicks}"
            )
    finally:
        try:
            await page.unroute(callback_pattern, handle_route)
        except Exception:
            pass

    parsed = urllib.parse.urlparse(callback_url)
    params = dict(urllib.parse.parse_qsl(parsed.query))

    if "error" in params:
        raise RuntimeError(f"OAuth callback 报错：{params}")
    if params.get("state") != state:
        raise RuntimeError(f"OAuth state 不匹配，可能被劫持")
    if "code" not in params:
        raise RuntimeError(f"OAuth callback 缺 code: {params}")

    print("[codex-oauth] callback received, exchanging code for tokens...")
    token_resp = await loop.run_in_executor(
        None, lambda: exchange_code(
            params["code"],
            verifier,
            proxy=proxy,
            proxy_insecure=proxy_insecure,
        )
    )

    creds = build_codex_credentials(token_resp, fallback_email=account_email)
    print(f"[codex-oauth] ✓ got credentials  plan={creds['plan_type']}  "
          f"acct={creds['chatgpt_account_id'][:8]}…  refresh_token={'yes' if creds['refresh_token'] else 'NO'}")
    return creds
