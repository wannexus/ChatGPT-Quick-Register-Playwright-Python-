"""Exercise the real phone DOM adapter with fully intercepted browser fixtures."""

from __future__ import annotations

import asyncio
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from core import codex_oauth, num5sim
from test_fivesim_oauth import BindingStore, PHONE, order


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        accepted = True

        async def fixture(route):
            path = route.request.url.split("auth.openai.com", 1)[-1]
            if path == "/add-phone":
                html = """<h1>Add a phone</h1><form action='/add-phone'>
                  <select name='country'><option value='1'>United States +1</option></select>
                  <input type='tel'><input type='hidden' name='phoneNumber'>
                  <button type='submit'>Send code</button></form>
                  <script>document.querySelector('form').onsubmit=e=>{
                    e.preventDefault();location.href='/phone-verification';};</script>"""
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
                 patch.object(num5sim, "check_order", Mock(return_value=order())), \
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
            assert PHONE not in output.getvalue()
            assert "654321" not in output.getvalue()
            print("Phone browser fixtures OK: accepted OTP binds, rejected OTP does not, logs masked")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
