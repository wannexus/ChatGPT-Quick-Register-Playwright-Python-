"""Generate a private @duck.com address from DuckDuckGo Email Protection."""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import Page, TimeoutError as PWTimeout

DUCK_AUTOFILL_URL = "https://duckduckgo.com/email/settings/autofill"

ADDRESS_INPUT_SELECTOR = "input.AutofillSettingsPanel__PrivateDuckAddressValue"
GENERATE_BUTTON_SELECTOR = "button.AutofillSettingsPanel__GeneratorButton"
GENERATE_BUTTON_TEXT = re.compile(
    r"generate\s+private\s+duck\s+address|new\s+private\s+duck\s+address|"
    r"generate\s+new|new\s+address|生成.*duck.*地址|生成.*私有.*地址|生成.*地址|新.*地址",
    re.IGNORECASE,
)


async def _read_address(page: Page) -> str:
    handle = await page.query_selector(ADDRESS_INPUT_SELECTOR)
    if not handle:
        return ""
    value = (await handle.input_value()).strip()
    return value if "@duck.com" in value else ""


async def _find_generate_button(page: Page):
    direct = await page.query_selector(GENERATE_BUTTON_SELECTOR)
    if direct:
        return direct
    candidates = await page.query_selector_all('button, [role="button"]')
    for btn in candidates:
        text = " ".join(filter(None, [
            (await btn.inner_text()).strip() if btn else "",
            await btn.get_attribute("aria-label") or "",
            await btn.get_attribute("title") or "",
        ]))
        text = re.sub(r"\s+", " ", text).strip()
        if GENERATE_BUTTON_TEXT.search(text):
            return btn
    return None


async def fetch_duck_email(
    page: Page,
    *,
    generate_new: bool = True,
    timeout: float = 30,
    nav_timeout_ms: int | None = None,
) -> str:
    """Open DDG autofill page in `page` and return a (newly generated) @duck.com address.

    Caller is responsible for being signed in to DuckDuckGo (use a persistent
    user_data_dir + an interactive first-run login). DDG is unreachable from
    Mainland China without a proxy — pass --proxy on the CLI.
    """
    nav_ms = nav_timeout_ms or 60_000
    print(f"[duck] navigating to {DUCK_AUTOFILL_URL} (timeout={nav_ms // 1000}s)")
    try:
        # Use 'commit' so we don't wait on hung sub-resources / trackers.
        await page.goto(DUCK_AUTOFILL_URL, wait_until="commit", timeout=nav_ms)
    except PWTimeout as e:
        raise RuntimeError(
            "打开 DuckDuckGo 超时。常见原因：\n"
            "  1) 国内直连 DDG 不通——加 --proxy http://127.0.0.1:7890 之类的参数\n"
            "  2) 代理本身不通——先 curl -x http://127.0.0.1:7890 https://duckduckgo.com 试一下\n"
            "  3) 真的很慢——加 --nav-timeout 120"
        ) from e

    try:
        await page.wait_for_selector(
            f"{ADDRESS_INPUT_SELECTOR}, {GENERATE_BUTTON_SELECTOR}",
            timeout=timeout * 1000,
        )
    except PWTimeout as e:
        raise RuntimeError(
            "未找到 Duck 地址输入框 / 生成按钮。请确认浏览器已登录 DuckDuckGo "
            "并开通了 Email Protection（首次跑要用持久化 profile 在浏览器里手动登录一次）。"
        ) from e

    current = await _read_address(page)
    if current and not generate_new:
        print(f"[duck] reuse existing address {current}")
        return current

    btn = await _find_generate_button(page)
    if not btn:
        if current:
            print(f"[duck] no generate button; reuse existing {current}")
            return current
        raise RuntimeError("未找到 Generate Private Duck Address 按钮（可能页面文案变了）")

    for attempt in range(1, 3):
        await asyncio.sleep(0.6)
        try:
            await btn.click()
        except Exception:
            await btn.evaluate("el => el.click()")
        print(f"[duck] clicked generate button (attempt {attempt}/2)")

        # Poll for the address value to change
        deadline = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            value = await _read_address(page)
            if value and value != current:
                print(f"[duck] new address ready: {value}")
                return value
            await asyncio.sleep(0.2)

        if attempt >= 2:
            raise RuntimeError("等待 Duck 新地址出现超时（请检查页面是否有提示框/限流）")
        print("[duck] address did not change, retrying...")

    raise RuntimeError("Duck 地址生成失败")
