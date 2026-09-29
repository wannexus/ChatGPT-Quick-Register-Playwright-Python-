"""add-phone 页「验证码接收方式」回归测试。

真实事故：OpenAI 的 add-phone 页在号码下方有一个 React Aria segmented-control，
可选「短信 / Text message」和「WhatsApp」。页面（西语/英文区）会默认勾 WhatsApp，
而 5sim 的号码只会收到短信 —— 旧实现完全不碰这个控件，于是白等 poll_timeout 秒后超时，
号码被浪费。真实快照（output/debug 里的 add-phone HTML，页面语言是西语）长这样：

    <label class="_option_10e2f_185" data-state="on">
      <input class="_input_10e2f_195" type="radio" value="sms" checked name="segmented-control-_r_g_">
      <span class="_content_10e2f_206">…<span>Mensaje de texto</span></span>
    </label>
    <label class="_option_10e2f_185" data-state="off">
      <input class="_input_10e2f_195" type="radio" value="whatsapp" name="segmented-control-_r_g_">
      <span class="_content_10e2f_206">…<span>WhatsApp</span></span>
    </label>

radio 的 value 与页面语言无关，所以定位以 value 为主、文案为辅。
"""

from __future__ import annotations

import pathlib
import unittest

from core import codex_oauth


CHANNEL_PAGE = """<!doctype html>
<html lang="{lang}"><body>
<form action="/add-phone" method="post">
  <label>Tel<input type="tel" name="phone"></label>
  <div role="radiogroup">
    <label class="_option_10e2f_185" data-state="{sms_state}">
      <input class="_input_10e2f_195" type="radio" value="sms" {sms_checked} name="segmented-control-_r_g_">
      <span class="_content_10e2f_206"><span>{sms_text}</span></span>
    </label>
    {whatsapp_option}
  </div>
  <button type="submit">Continuar</button>
</form>
</body></html>
"""

WHATSAPP_OPTION = """<label class="_option_10e2f_185" data-state="{state}">
      <input class="_input_10e2f_195" type="radio" value="whatsapp" {checked} name="segmented-control-_r_g_">
      <span class="_content_10e2f_206"><span>WhatsApp</span></span>
    </label>"""

NO_CHANNEL_PAGE = """<!doctype html><html><body>
<form action="/add-phone"><input type="tel" name="phone"><button type="submit">Continuar</button></form>
</body></html>"""


def channel_page(*, sms_checked: bool, text: str = "Mensaje de texto", lang: str = "es") -> str:
    return CHANNEL_PAGE.format(
        lang=lang,
        sms_state="on" if sms_checked else "off",
        sms_checked="checked" if sms_checked else "",
        sms_text=text,
        whatsapp_option=WHATSAPP_OPTION.format(
            state="off" if sms_checked else "on", checked="" if sms_checked else "checked"),
    )


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001 — 没装浏览器时跳过，而不是让整个套件失败
        return False


PLAYWRIGHT = _playwright_available()


@unittest.skipUnless(PLAYWRIGHT, "本机没有可用的 Playwright chromium")
class SmsChannelSelectionTests(unittest.IsolatedAsyncioTestCase):
    """在真实 DOM 上验证：无论页面默认勾哪个，最终都必须落在短信上。"""

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self._tmp = pathlib.Path(__file__).resolve().parent / "_tmp_channel"
        self._tmp.mkdir(exist_ok=True)
        self._counter = 0

    async def asyncTearDown(self):
        try:
            await self.browser.close()
        finally:
            await self._pw.stop()
        for leftover in self._tmp.glob("page-*.html"):
            leftover.unlink(missing_ok=True)

    async def _open(self, html: str):
        self._counter += 1
        path = self._tmp / f"page-{self._counter}.html"
        path.write_text(html, encoding="utf-8")
        await self.page.goto(f"file://{path}")

    async def _checked(self) -> list:
        return await self.page.evaluate(
            "() => [...document.querySelectorAll(\"input[type='radio']\")]"
            ".filter(el => el.checked).map(el => el.value)"
        )

    async def test_sms_already_selected_stays_on_sms(self):
        await self._open(channel_page(sms_checked=True))
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "sms")
        self.assertEqual(await self._checked(), ["sms"])

    async def test_whatsapp_default_is_switched_to_sms(self):
        """用户报的就是这个：页面默认勾 WhatsApp，必须切回短信。"""
        await self._open(channel_page(sms_checked=False))
        self.assertEqual(await self._checked(), ["whatsapp"])
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "sms")
        self.assertEqual(await self._checked(), ["sms"])

    async def test_english_label_with_whatsapp_default_is_switched(self):
        await self._open(channel_page(sms_checked=False, text="Text message", lang="en"))
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "sms")
        self.assertEqual(await self._checked(), ["sms"])

    async def test_chinese_label_with_whatsapp_default_is_switched(self):
        await self._open(channel_page(sms_checked=False, text="短信", lang="zh"))
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "sms")
        self.assertEqual(await self._checked(), ["sms"])

    async def test_labelless_radios_are_still_matched_by_value(self):
        """只有裸 radio、没有 label 文案时，按 value 也要能选中短信。"""
        await self._open("""<html><body><form action="/add-phone">
        <input type="tel" name="phone">
        <input type="radio" value="whatsapp" name="c" checked>
        <input type="radio" value="sms" name="c">
        </form></body></html>""")
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "sms")
        self.assertEqual(await self._checked(), ["sms"])

    async def test_no_channel_control_is_reported_as_absent(self):
        await self._open(NO_CHANNEL_PAGE)
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "absent")

    async def test_whatsapp_only_page_fails_instead_of_wasting_the_number(self):
        await self._open(NO_CHANNEL_PAGE)
        await self.page.evaluate("""() => {
            document.querySelector('form').innerHTML =
              '<input type="tel" name="phone"><input type="radio" value="whatsapp" name="c" checked>';
        }""")
        self.assertEqual(await codex_oauth._select_sms_channel(self.page, wait_ms=300), "failed")


class SmsChannelContractTests(unittest.TestCase):
    """流程上必须在提交前选短信；选不成必须停，不能傻等短信超时。"""

    def setUp(self):
        source = pathlib.Path("core/codex_oauth.py").read_text(encoding="utf-8")
        start = source.index("验证码接收方式必须是「短信")
        self.submit_block = source[start:source.index("# ── 4) 轮询等待 SMS ──", start)]
        self.source = source

    def test_sms_is_selected_before_the_submit_click(self):
        self.assertIn("_select_sms_channel(page)", self.submit_block)
        self.assertLess(
            self.submit_block.index("_select_sms_channel(page)"),
            self.submit_block.index("已点击 {sel}"),
            "必须先切到短信再点提交，否则码会发到 WhatsApp",
        )

    def test_a_failed_switch_aborts_instead_of_waiting_for_sms(self):
        self.assertIn('channel == "failed"', self.submit_block)
        self.assertIn("WhatsApp 收不到 5sim 的验证码", self.submit_block)

    def test_the_phone_number_is_refilled_if_switching_reset_it(self):
        self.assertIn("已重新填写", self.submit_block)

    def test_a_submitted_whatsapp_delivery_is_called_out(self):
        """提交后如果页面说「发到 WhatsApp 了」，日志必须点明，不能让人以为 5sim 没发码。"""
        self.assertIn("验证码发到了 WhatsApp", self.submit_block)
        self.assertIn("_WHATSAPP_DELIVERY_RE", self.submit_block)

    def test_the_delivery_hint_does_not_fire_on_the_channel_picker(self):
        """add-phone 页本来就把 WhatsApp 列成选项：只看关键词会误报，必须匹配交付语义。"""
        picker = "Mensaje de texto WhatsApp Continuar"
        self.assertIsNone(codex_oauth._WHATSAPP_DELIVERY_RE.search(picker))

    def test_the_delivery_hint_matches_every_language(self):
        for text in (
            "Hemos enviado un código de verificación a tu WhatsApp.",
            "We sent a verification code to your WhatsApp.",
            "验证码已发送到 WhatsApp，请输入验证码。",
        ):
            self.assertIsNotNone(codex_oauth._WHATSAPP_DELIVERY_RE.search(text), text)

    def test_explicit_sms_failure_is_recognized_across_line_breaks(self):
        for text in (
            "We couldn't send a text message to this phone number, so we switched to WhatsApp.",
            "We could not send a text message to this phone number.\nWe switched to WhatsApp.",
        ):
            self.assertIsNotNone(codex_oauth._WHATSAPP_SMS_FAILURE_RE.search(text))
        self.assertIsNone(codex_oauth._WHATSAPP_SMS_FAILURE_RE.search("Text message WhatsApp Continue"))

    def test_every_page_language_has_keywords(self):
        lowered = self.source.lower()
        for text in ("text message", "sms", "mensaje de texto", "短信", "文本消息"):
            self.assertIn(text, lowered, f"缺少 {text} 的文案匹配（页面语言会变）")


if __name__ == "__main__":
    unittest.main()
