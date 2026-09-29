"""add-phone 国家/区号一致性回归测试。

背景（真实事故）：5sim 买到波兰号 +4855...，OpenAI add-phone 页是中文 React Aria Select，
隐藏原生 <select> 的 option value 是 ISO alpha-2、文本是中文国名（无英文名、无区号数字）。
旧实现按英文国名/「(+区号)」文本匹配 → 全部失配 → 落到「兜底 select_option(index=1)」
= 阿尔巴尼亚 (+355)，页面区号与 5sim 号码区号不一致，提交的号码和买到的号码不是同一个。
"""

from __future__ import annotations

import re
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core import codex_oauth, num5sim

# 真实页面 option 文本是本地化国名（取自 output/debug 抓到的 add-phone HTML）
ZH_NAMES = {
    "US": "美国", "PL": "波兰", "GR": "希腊", "AL": "阿尔巴尼亚",
    "GB": "英国", "KZ": "哈萨克斯坦", "PT": "葡萄牙", "DZ": "阿尔及利亚",
}

SELECT_MARKERS = ("inputDecorationCountryCode", "react-aria-SelectValue", "aria-haspopup", "role='option'")


def options_for(isos):
    """复刻真实 evaluate 结果：index 0 是空 option，value=ISO，text=本地化国名。"""
    rows = [{"index": 0, "value": "", "text": "", "label": ""}]
    for position, iso in enumerate(isos, start=1):
        rows.append({"index": position, "value": iso, "text": ZH_NAMES.get(iso, iso), "label": ""})
    return rows


def order(phone="+15550000001", country="usa", order_id=1, sms=None, status="PENDING"):
    return num5sim.ActivationOrder(order_id, phone, "any", "openai", 0.1, status, "", sms, country)


class _FirstSelf:
    """Playwright 的 page.locator(...) 返回 Locator，代码里普遍取 .first。"""

    @property
    def first(self):
        return self


class _Missing(_FirstSelf):
    async def count(self):
        return 0


class _Field(_FirstSelf):
    """可见输入框 / 提交按钮的最小替身。"""

    def __init__(self, count=1, visible=True):
        self._count, self._visible = count, visible
        self.fills = []

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def click(self, **kwargs):
        return None

    async def fill(self, value, **kwargs):
        self.fills.append(value)

    async def evaluate(self, script):
        return None


class _HiddenNumberField(_Field):
    def __init__(self, value=""):
        super().__init__()
        self.value = value
        self.writes = []

    async def evaluate(self, script):
        if "el.value" not in script:
            return None
        if "el.value =" not in script:
            return self.value
        found = re.search(r"el\.value = '([^']*)'", script)
        if found:
            self.value = found.group(1)
            self.writes.append(self.value)
        return None


class FakeAddPhonePage:
    """复刻 React Aria Select 行为：原生 select 的 change 未必驱动 React 状态。"""

    def __init__(self, isos, *, selected="US", react_updates=True, popup_available=True):
        self.isos = list(isos)
        self.selected_iso = selected
        self.native_value = selected
        self.react_updates = react_updates
        self.popup_available = popup_available
        self.popup_open = False
        self.select_calls = []
        self.tel = _Field()
        self.hidden = _HiddenNumberField()
        self.submit = _Field(count=0)

    # --- 页面状态 ---
    def dial_of(self, iso):
        return codex_oauth._ISO_DIAL.get(iso, "")

    def iso_for_dial(self, dial):
        for iso in self.isos:
            if self.dial_of(iso) == dial:
                return iso
        return ""

    def locator(self, selector):
        if "role='option'" in selector:
            dial = selector.split("(+")[1].split(")")[0]
            iso = self.iso_for_dial(dial)
            if self.popup_open and self.popup_available and iso:
                return _PopupOption(self, iso)
            return _Missing()
        if "inputDecorationCountryCode" in selector:
            return _TextLocator(lambda: self.dial_of(self.selected_iso))
        if "react-aria-SelectValue" in selector:
            def text():
                iso = self.selected_iso
                return f"{ZH_NAMES.get(iso, iso)} (+{self.dial_of(iso)})"
            return _TextLocator(text)
        if "aria-haspopup" in selector:
            return _Trigger(self)
        if "phoneNumber" in selector:
            return self.hidden
        if "input[type='tel'" in selector:
            return self.tel
        if "button" in selector:
            return self.submit
        return _CountrySelect(self)


class _TextLocator(_FirstSelf):
    def __init__(self, text_fn):
        self._text_fn = text_fn

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def text_content(self):
        return self._text_fn()


class _Trigger(_FirstSelf):
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self, **kwargs):
        self.page.popup_open = True


class _PopupOption(_FirstSelf):
    def __init__(self, page, iso):
        self.page, self.iso = page, iso

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self, **kwargs):
        self.page.selected_iso = self.iso
        self.page.popup_open = False


class _CountrySelect(_FirstSelf):
    def __init__(self, page):
        self.page = page
        self.options = options_for(page.isos)

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def evaluate(self, script):
        if "tagName" in script:
            return "select"
        if "sel.options" in script:
            return self.options
        if "el.value" in script:
            return self.page.native_value
        return None

    async def select_option(self, *, value=None, index=None, timeout=None):
        self.page.select_calls.append({"value": value, "index": index})
        if index is not None:
            self.page.native_value = self.options[index]["value"]
        else:
            self.page.native_value = value
        if self.page.react_updates and self.page.native_value in self.page.isos:
            self.page.selected_iso = self.page.native_value


class DialCodeUnitTests(unittest.TestCase):
    def test_longest_prefix_uses_complete_dial_table(self):
        cases = {
            "+485518000111": "48",
            "+306912345678": "30",
            "+447350690992": "44",
            "+15550000001": "1",
            "+79161234567": "7",
            "+8613800138000": "86",
        }
        for phone, expected in cases.items():
            self.assertEqual(codex_oauth._extract_dial_code(phone), expected, phone)

    def test_no_more_two_digit_guess_for_three_digit_dials(self):
        # 旧实现：这些区号不在表里 → 取前两位（35/42/99/37），国内号码跟着截错
        for phone, expected in {
            "+351912345678": "351",   # 葡萄牙（旧实现给 35）
            "+420601123456": "420",   # 捷克（旧实现给 42）
            "+37251234567": "372",    # 爱沙尼亚（旧实现给 37）
            "+998901234567": "998",   # 乌兹别克斯坦（旧实现给 99）
            "+380671234567": "380",   # 乌克兰（表里本来就有，保持）
        }.items():
            self.assertEqual(codex_oauth._extract_dial_code(phone), expected, phone)

    def test_iso_resolution_prefers_country_hint_inside_shared_dial(self):
        self.assertEqual(codex_oauth._iso_for_phone("+485518000111", "poland"), "PL")
        self.assertEqual(codex_oauth._iso_for_phone("+447350690992", "england"), "GB")
        self.assertEqual(codex_oauth._iso_for_phone("+79161234567", "kazakhstan"), "KZ")
        self.assertEqual(codex_oauth._iso_for_phone("+15550000001", "canada"), "CA")
        # hint 与号码区号矛盾时以号码为准
        self.assertEqual(codex_oauth._iso_for_phone("+485518000111", "greece"), "PL")
        self.assertEqual(codex_oauth._iso_for_phone("+351912345678", ""), "PT")

    def test_matches_real_page_option_format_by_iso_value(self):
        options = options_for(["US", "PL", "AL", "GR", "DZ"])
        target = codex_oauth._match_country_option(options, "PL", "48", "poland")
        self.assertIsNotNone(target)
        self.assertEqual(target["value"], "PL")
        self.assertEqual(target["index"], 2, "必须是波兰那一项，而不是 index=1 的阿尔巴尼亚")
        self.assertNotEqual(options[1]["value"], "PL", "夹具必须复刻真实列表：index=1 是别的国家")

    def test_never_returns_an_arbitrary_option(self):
        options = options_for(["US", "PL", "AL"])
        self.assertIsNone(codex_oauth._match_country_option(options, "PT", "351", "portugal"))
        self.assertIsNone(codex_oauth._match_country_option([], "PL", "48", "poland"))
        self.assertIsNone(codex_oauth._match_country_option(None, "PL", "48", "poland"))


class SelectCountryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_number_country_on_chinese_page(self):
        page = FakeAddPhonePage(["US", "PL", "AL", "GR"])
        ok = await codex_oauth._select_phone_country(page, "poland", "+485518000111")
        self.assertTrue(ok)
        self.assertEqual(page.select_calls, [{"value": None, "index": 2}])
        self.assertEqual(page.native_value, "PL")
        self.assertEqual(page.selected_iso, "PL")

    async def test_popup_fallback_when_native_change_is_ignored(self):
        page = FakeAddPhonePage(["US", "PL", "AL", "GR"], react_updates=False)
        ok = await codex_oauth._select_phone_country(page, "greece", "+306912345678")
        self.assertTrue(ok)
        self.assertEqual(page.selected_iso, "GR", "必须经弹层点选真正更新 React 状态")

    async def test_missing_country_fails_instead_of_picking_index_one(self):
        page = FakeAddPhonePage(["US", "PL", "AL"], popup_available=False)
        ok = await codex_oauth._select_phone_country(page, "portugal", "+351912345678")
        self.assertFalse(ok)
        self.assertEqual(page.select_calls, [], "不得再用 index=1 之类的兜底")
        self.assertEqual(page.selected_iso, "US")

    async def test_react_state_not_updated_and_no_popup_fails(self):
        page = FakeAddPhonePage(["US", "PL"], react_updates=False, popup_available=False)
        ok = await codex_oauth._select_phone_country(page, "poland", "+485518000111")
        self.assertFalse(ok)
        self.assertEqual(page.selected_iso, "US")

    async def test_unparseable_number_is_refused(self):
        page = FakeAddPhonePage(["US", "PL"])
        ok = await codex_oauth._select_phone_country(page, "poland", "not-a-number")
        self.assertFalse(ok)
        self.assertEqual(page.select_calls, [])


class _Store:
    def __init__(self):
        self.reserved = set()

    def get_account(self, account_id):
        return {"id": account_id, "codexPhoneNumber": ""}

    @contextmanager
    def reserve_codex_phone(self, account_id, phone, **kwargs):
        self.reserved.add(phone)
        try:
            yield lambda: {"ok": True, "account_count": 1}
        finally:
            self.reserved.discard(phone)


class SubmitGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_country_mismatch_cancels_order_before_submitting(self):
        store = _Store()
        page = SimpleNamespace(locator=Mock(), url="https://auth.openai.com/add-phone",
                               wait_for_load_state=AsyncMock())
        with patch.object(num5sim, "buy_activation",
                          Mock(return_value=order("+485518000111", "poland"))), \
             patch.object(num5sim, "cancel_order", Mock()) as cancel, \
             patch.object(codex_oauth, "_select_phone_country", AsyncMock(return_value=False)), \
             patch.object(codex_oauth, "_save_debug", AsyncMock()):
            verify = codex_oauth.create_5sim_phone_verifier("k", account_id=1, account_store=store)
            with self.assertRaises(RuntimeError) as ctx:
                await verify(page)
        self.assertIn("区号", str(ctx.exception))
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(store.reserved, set())
        page.locator.assert_not_called()

    async def test_page_composed_number_is_overwritten_when_inconsistent(self):
        store = _Store()
        page = FakeAddPhonePage(["US", "PL"], selected="US")
        page.hidden.value = "+3555518000111"   # 页面按错误区号拼出的号码
        with patch.object(num5sim, "buy_activation",
                          Mock(return_value=order("+485518000111", "poland"))), \
             patch.object(num5sim, "cancel_order", Mock()), \
             patch.object(codex_oauth, "_select_phone_country", AsyncMock(return_value=True)), \
             patch.object(codex_oauth, "_read_selected_country_dial", AsyncMock(return_value="48")), \
             patch.object(codex_oauth, "_save_debug", AsyncMock()):
            verify = codex_oauth.create_5sim_phone_verifier("k", account_id=1, account_store=store)
            with self.assertRaises(RuntimeError):   # submit 按钮夹具 count=0，走到 3d 才失败
                await verify(page)
        self.assertEqual(page.tel.fills[-1], "5518000111")
        self.assertEqual(page.hidden.value, "+485518000111")


if __name__ == "__main__":
    unittest.main()
