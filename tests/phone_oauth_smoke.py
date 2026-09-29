"""Exercise the real phone DOM adapter with fully intercepted browser fixtures."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from core import codex_oauth, num5sim
from test_fivesim_oauth import BindingStore, PHONE, SMS_FAILURE, order


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        accepted = True
        reject_phone = False

        async def fixture(route):
            path = route.request.url.split("auth.openai.com", 1)[-1]
            if path == "/add-phone":
                html = """<h1>Add a phone</h1><form action='/add-phone'>
                  <select name='country'><option value='US'>United States +1</option></select>
                  <input type='tel'><input type='hidden' name='phoneNumber'>
                  <button type='submit'>Send code</button><p role='alert'></p></form>
                  <script>document.querySelector('form').onsubmit=e=>{
                    e.preventDefault();
                    if (REJECT_BAD_NUMBER && document.querySelector('input[type=tel]').value==='5550000001') {
                      document.querySelector('[role=alert]').textContent=SMS_FAILURE_MESSAGE;return;
                    }
                    location.href='/phone-verification';};</script>"""
                html = html.replace("REJECT_BAD_NUMBER", "true" if reject_phone else "false")
                html = html.replace("SMS_FAILURE_MESSAGE", json.dumps(SMS_FAILURE))
            elif path == "/phone-verification":
                next_page = "location.href='/consent';" if accepted else "document.querySelector('p').textContent='Invalid code';"
                html = """<h1>Verify your phone</h1><form action='/phone-verification'>
                  <input name='code' autocomplete='one-time-code'><button>Verify</button></form><p></p>
                  <script>document.querySelector('form').onsubmit=e=>{e.preventDefault();""" + next_page + "};</script>"
            else:
                html = "<h1>Authorize Codex</h1><button>Allow</button>"
            await route.fulfill(content_type="text/html", body=html)

        await page.route("**/*", fixture)
        pool = num5sim.ActivationPool()
        output = io.StringIO()
        try:
            with redirect_stdout(output), \
                 patch.object(num5sim, "buy_activation", Mock(return_value=order())), \
                 patch.object(num5sim, "check_order", Mock(return_value=order(sms=[{"code": "654321"}]))), \
                 patch.object(num5sim, "finish_order", Mock()), \
                 patch.object(num5sim, "cancel_order", Mock()), \
                 patch.object(pool, "save", Mock()):
                store = BindingStore()
                verifier = codex_oauth.create_5sim_phone_verifier(
                    "fixture", account_id=7, account_store=store, reuse_pool=pool,
                )
                await page.goto("https://auth.openai.com/add-phone")
                await verifier(page)
                assert store.bindings == {7: PHONE}
                assert page.url.endswith("/consent")
                accepted = False
                failed_store = BindingStore()
                verifier = codex_oauth.create_5sim_phone_verifier(
                    "fixture", account_id=8, account_store=failed_store,
                )
                await page.goto("https://auth.openai.com/add-phone")
                try:
                    await verifier(page)
                except RuntimeError as error:
                    assert "号码未绑定" in str(error)
                else:
                    raise AssertionError("Rejected OTP was accepted")
                assert failed_store.bindings == {}
                assert failed_store.reserved == set()
                accepted = True
                reject_phone = True
                good_phone = "+15550000002"
                replacement_pool = num5sim.ActivationPool()
                replacement_store = BindingStore()
                with patch.object(num5sim, "buy_activation", side_effect=[order(order_id=10), order(good_phone, 11)]) as buy, \
                     patch.object(num5sim, "check_order", return_value=order(good_phone, 11, sms=[{"code": "654321"}])) as check, \
                     patch.object(num5sim, "ban_order", Mock()) as ban, \
                     patch.object(replacement_pool, "save", Mock()):
                    verifier = codex_oauth.create_5sim_phone_verifier(
                        "fixture", account_id=9, account_store=replacement_store, reuse_pool=replacement_pool,
                    )
                    await page.goto("https://auth.openai.com/add-phone")
                    await verifier(page)
                    assert page.url.endswith("/consent")
                    assert replacement_store.bindings == {9: good_phone}
                    assert buy.call_count == 2
                    assert ban.call_count == 1 and ban.call_args.kwargs["order_id"] == 10
                    assert check.call_count == 1 and check.call_args.kwargs["order_id"] == 11
                    assert [entry.phone for entry in replacement_pool.entries] == [good_phone]
            assert PHONE not in output.getvalue()
            assert "654321" not in output.getvalue()
            print("Phone browser fixtures OK: OTP acceptance, rejection, WhatsApp fallback bans and replaces, logs masked")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
