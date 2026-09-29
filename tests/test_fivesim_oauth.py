from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core import codex_oauth, num5sim


PHONE = "+15550000001"
SMS_FAILURE = ("We couldn't send a text message to this phone number, so we switched to WhatsApp. "
               "Continue to send a verification code on WhatsApp.")
SMS_FAILURE_SV = ("Vi kunde inte skicka ett sms till det här telefonnumret, så vi bytte till WhatsApp. "
                  "Fortsätt för att skicka en verifieringskod på WhatsApp.")


class BindingStore:
    def __init__(self, full=(), existing=""):
        self.full = set(full)
        self.bindings = {}
        self.reserved = set()
        self.existing = existing

    def get_account(self, account_id):
        return {"id": account_id, "codexPhoneNumber": self.bindings.get(account_id, self.existing)}

    @contextmanager
    def reserve_codex_phone(self, account_id, phone, **kwargs):
        if phone in self.full or list(self.bindings.values()).count(phone) >= kwargs.get("max_accounts", 3):
            yield None
            return
        self.reserved.add(phone)
        try:
            def bind():
                self.bindings[account_id] = phone
                return {"ok": True, "account_count": list(self.bindings.values()).count(phone)}
            yield bind
        finally:
            self.reserved.remove(phone)


def order(phone=PHONE, order_id=1, *, price=0.1, sms=None, status="RECEIVED", expires=""):
    return num5sim.ActivationOrder(order_id, phone, "any", "openai", price,
                                  status, expires, sms or [], "usa")


class FiveSimOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def run_verifier(self, store, *, purchases=None, accepted=True, pool=None, proxy=None,
                           otp_error=None, max_price=None, reuse_order=None, checks=None,
                           account_id=7, expected_phone=PHONE, country="any", providers=None,
                           save_error=None, persist_pool=False, page_text="", ban_error=None):
        page = SimpleNamespace(locator=Mock(), url="https://auth.openai.com/consent",
                               wait_for_load_state=AsyncMock(), goto=AsyncMock())
        self.page = page
        field = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                click=AsyncMock(), fill=AsyncMock(), evaluate=AsyncMock())
        page.locator.return_value.first = field
        pool = pool if pool is not None else num5sim.ActivationPool()
        buy = Mock(side_effect=purchases or [order()])
        real_sleep = asyncio.sleep

        async def no_wait(_seconds):
            await real_sleep(0)

        async def submit_otp(*_args):
            self.assertIn(expected_phone, store.reserved)
            if otp_error:
                raise otp_error
            return True

        if isinstance(reuse_order, BaseException):
            reuse = Mock(side_effect=reuse_order)
        else:
            reuse = Mock(return_value=reuse_order or order())
        self.reuse = reuse
        self.otp = AsyncMock(side_effect=submit_otp)
        if checks is not None:
            self.check = Mock(side_effect=checks)
        else:
            self.check = Mock(return_value=order(sms=[{"code": "654321"}]))
        save = Mock(side_effect=pool.save if persist_pool else save_error)
        self.ban = Mock(side_effect=ban_error)
        text = AsyncMock(side_effect=page_text) if callable(page_text) else AsyncMock(return_value=page_text)
        self.calls = Mock()
        self.calls.attach_mock(buy, "buy")
        self.calls.attach_mock(self.ban, "ban")
        self.calls.attach_mock(self.check, "check")
        self.output = io.StringIO()
        with redirect_stdout(self.output), \
             patch.object(num5sim, "buy_activation", buy), \
             patch.object(num5sim, "reuse_number", reuse), \
             patch.object(num5sim, "check_order", self.check), \
             patch.object(num5sim, "finish_order", Mock()) as finish, \
             patch.object(num5sim, "cancel_order", Mock()) as cancel, \
             patch.object(num5sim, "ban_order", self.ban), \
             patch.object(pool, "save", save), \
             patch.object(codex_oauth, "_auth_page_text", text), \
             patch.object(codex_oauth, "_select_sms_channel", AsyncMock(return_value="absent")), \
             patch.object(codex_oauth, "_select_phone_country", AsyncMock(return_value=True)), \
             patch.object(codex_oauth, "_click_with_force_fallback", AsyncMock(return_value=True)), \
             patch.object(codex_oauth, "_fill_otp_code", self.otp), \
             patch.object(codex_oauth, "_is_add_phone_page", AsyncMock(return_value=not accepted)), \
             patch.object(codex_oauth, "_has_visible_otp_input", AsyncMock(return_value=not accepted)), \
             patch.object(codex_oauth.asyncio, "sleep", no_wait):
            verify = codex_oauth.create_5sim_phone_verifier(
                "fake-key", account_id=account_id, account_store=store, reuse_pool=pool, proxy=proxy,
                max_price=max_price, country=country, providers=providers,
            )
            error = None
            try:
                await verify(page)
            except BaseException as exc:
                error = exc
        self.finish = finish
        return buy, cancel, error

    async def test_sms_failure_bans_the_order_before_buying_a_replacement(self):
        good = "+15550000002"
        store = BindingStore()
        pool = num5sim.ActivationPool()
        buy, cancel, error = await self.run_verifier(
            store, pool=pool, expected_phone=good,
            purchases=[order(), order(good, 2)],
            checks=[order(good, 2, sms=[{"code": "654321"}])],
            page_text=lambda _page: SMS_FAILURE if not self.page.goto.await_count else "",
        )
        self.assertIsNone(error)
        self.assertEqual([call[0] for call in self.calls.mock_calls], ["buy", "ban", "buy", "check"])
        self.assertEqual(self.ban.call_args.kwargs["order_id"], 1)
        self.assertEqual(buy.call_count, 2)
        self.assertFalse(buy.call_args.kwargs["enable_reuse"])
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(store.bindings, {7: good})
        self.assertEqual([entry.phone for entry in pool.entries], [good])
        self.assertEqual(store.reserved, set())
        self.page.goto.assert_awaited_once_with("https://auth.openai.com/add-phone", wait_until="domcontentloaded")

    async def test_delayed_sms_failure_stops_polling_and_buys_another_number(self):
        good = "+15550000002"
        buy, cancel, error = await self.run_verifier(
            BindingStore(), expected_phone=good, purchases=[order(), order(good, 2)],
            checks=[order(), order(good, 2, sms=[{"code": "654321"}])],
            page_text=lambda _page: SMS_FAILURE if self.check.call_count and not self.page.goto.await_count else "",
        )
        self.assertIsNone(error)
        self.assertEqual([call[0] for call in self.calls.mock_calls], ["buy", "check", "ban", "buy", "check"])
        self.assertEqual(buy.call_count, 2)
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(self.otp.await_count, 1)

    async def test_swedish_sms_failure_bans_the_number_without_polling_it(self):
        good = "+15550000002"
        store = BindingStore()
        buy, cancel, error = await self.run_verifier(
            store, expected_phone=good, purchases=[order(), order(good, 2)],
            checks=[order(good, 2, sms=[{"code": "654321"}])],
            page_text=lambda _page: SMS_FAILURE_SV if not self.page.goto.await_count else "",
        )
        self.assertIsNone(error)
        self.assertEqual([call[0] for call in self.calls.mock_calls], ["buy", "ban", "buy", "check"])
        self.assertEqual(self.ban.call_args.kwargs["order_id"], 1)
        self.assertEqual(buy.call_count, 2)
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(store.bindings, {7: good})

    async def test_rejected_pool_number_is_removed_without_erasing_existing_bindings(self):
        good = "+15550000002"
        store = BindingStore()
        store.bindings[1] = PHONE
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", last_order_id=1)])
        buy, cancel, error = await self.run_verifier(
            store, pool=pool, expected_phone=good, purchases=[order(good, 2)],
            checks=[order(), order(good, 2, sms=[{"code": "654321"}])],
            page_text=lambda _page: SMS_FAILURE if not self.page.goto.await_count else "",
            ban_error=num5sim.FiveSimError("order has sms"),
        )
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 1)
        self.assertEqual(self.ban.call_count, 1)
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(store.bindings, {1: PHONE, 7: good})
        self.assertEqual([entry.phone for entry in pool.entries], [good])
        self.assertIn("BAN 未确认", self.output.getvalue())

    async def test_repeated_sms_failures_are_bounded_and_never_wait_for_sms(self):
        store = BindingStore()
        purchases = [order(f"+1555000000{i}", i) for i in range(1, 5)]
        buy, cancel, error = await self.run_verifier(store, purchases=purchases, page_text=SMS_FAILURE)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("换号上限", str(error))
        self.assertEqual(buy.call_count, 3)
        self.assertEqual(self.ban.call_count, 3)
        self.assertEqual(self.check.call_count, 0)
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(self.otp.await_count, 0)
        self.assertEqual(store.bindings, {})
        self.assertEqual(store.reserved, set())

    async def test_purchase_budget_is_shared_across_sms_replacements_and_full_numbers(self):
        full = "+15550000002"
        buy, cancel, error = await self.run_verifier(
            BindingStore(full=[full]), purchases=[order(), order(full, 2), order(full, 3), order(full, 4)],
            page_text=SMS_FAILURE,
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("安全上限", str(error))
        self.assertEqual(buy.call_count, 3)
        self.assertEqual(self.ban.call_count, 1)
        self.assertEqual(cancel.call_count, 2)

    async def test_a_banned_number_returned_by_purchase_is_not_submitted_again(self):
        good = "+15550000002"
        store = BindingStore()
        buy, cancel, error = await self.run_verifier(
            store, expected_phone=good, purchases=[order(), order(order_id=2), order(good, 3)],
            checks=[order(good, 3, sms=[{"code": "654321"}])],
            page_text=lambda _page: SMS_FAILURE if not self.page.goto.await_count else "",
        )
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 3)
        self.assertEqual(cancel.call_args.kwargs["order_id"], 2)
        self.assertEqual(self.ban.call_count, 1)
        self.assertEqual(store.bindings, {7: good})

    async def test_normal_whatsapp_picker_does_not_ban_a_number(self):
        buy, _cancel, error = await self.run_verifier(
            BindingStore(), page_text="Text message WhatsApp Continue",
        )
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 1)
        self.assertEqual(self.ban.call_count, 0)

    async def test_otp_acceptance_binds_and_releases_reservation(self):
        store = BindingStore()
        buy, _cancel, error = await self.run_verifier(store)
        self.assertIsNone(error)
        self.assertEqual(store.bindings, {7: PHONE})
        self.assertEqual(store.reserved, set())
        self.assertEqual(buy.call_count, 1)
        self.assertNotIn("654321", self.output.getvalue())
        self.assertNotIn(PHONE, self.output.getvalue())

    async def test_unaccepted_otp_never_binds(self):
        store = BindingStore()
        _buy, _cancel, error = await self.run_verifier(store, accepted=False)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(store.bindings, {})
        self.assertEqual(store.reserved, set())

    async def test_saturated_purchased_number_is_cancelled_and_replaced(self):
        full = "+15550000002"
        store = BindingStore(full=[full])
        buy, cancel, error = await self.run_verifier(store, purchases=[order(full), order()])
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 2)
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(store.bindings, {7: PHONE})

    async def test_saturated_numbers_cannot_exceed_three_purchase_attempts(self):
        store = BindingStore(full=[PHONE])
        buy, _cancel, error = await self.run_verifier(store, purchases=[order()] * 4)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(buy.call_count, 3)
        self.assertEqual(store.bindings, {})

    async def test_ambiguous_purchase_error_is_not_replayed_directly(self):
        store = BindingStore()
        buy, _cancel, error = await self.run_verifier(
            store, purchases=[num5sim.FiveSimError("response lost")], proxy="http://fake:8080",
        )
        self.assertIsInstance(error, num5sim.FiveSimError)
        self.assertEqual(buy.call_count, 1)

    async def test_purchase_gateway_error_is_actionable_and_never_replayed(self):
        buy, _cancel, error = await self.run_verifier(
            BindingStore(), purchases=[num5sim.FiveSimGatewayError("502 Bad Gateway")], proxy="http://fixture:8080",
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("请求已发出", str(error))
        self.assertIn("核对 5sim 订单", str(error))
        self.assertEqual(buy.call_count, 1)

    async def test_pool_capacity_uses_mysql_and_ignores_legacy_use_count(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", successful_uses=99)])
        store = BindingStore()
        buy, _cancel, error = await self.run_verifier(store, pool=pool)
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 0)
        self.assertEqual(self.reuse.call_count, 1)
        self.assertEqual(len(pool.entries), 1, "legacy counts must not delete inventory")

    async def test_saturated_pool_entry_is_never_reused(self):
        full = "+15550000002"
        pool = num5sim.ActivationPool([num5sim.PoolEntry(full, "usa", "any", "openai")])
        store = BindingStore(full=[full])
        buy, _cancel, error = await self.run_verifier(store, pool=pool)
        self.assertIsNone(error)
        self.assertEqual(self.reuse.call_count, 0)
        self.assertEqual(buy.call_count, 1)

    async def test_price_ceiling_does_not_prevent_continuing_a_paid_active_order(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", last_order_id=1)])
        buy, _cancel, error = await self.run_verifier(
            BindingStore(), pool=pool, max_price=0.05,
            checks=[order(), order(sms=[{"code": "654321"}])],
        )
        self.assertIsNone(error)
        self.assertEqual(self.reuse.call_count, 0)
        self.assertEqual(buy.call_count, 0)
        self.assertEqual(self.finish.call_count, 0)

    async def test_one_order_serves_three_accounts_then_the_fourth_buys_another(self):
        store = BindingStore()
        sms = []
        finishes = 0
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(num5sim, "_project_output_dir", return_value=Path(folder)):
            for account_id in range(1, 5):
                pool = num5sim.ActivationPool.load()
                phone = PHONE if account_id <= 3 else "+15550000002"
                order_id = 1 if account_id <= 3 else 2
                if account_id == 4:
                    sms = []
                fresh_sms = sms + [{"code": str(100000 + account_id),
                                   "date": f"2026-09-29T10:00:0{account_id}Z"}]
                checks = []
                if account_id in (2, 3):
                    checks = [order(sms=sms), order(sms=sms)]
                checks.append(order(phone, order_id, sms=fresh_sms))
                buy, cancel, error = await self.run_verifier(
                    store, pool=pool, account_id=account_id, expected_phone=phone,
                    purchases=[order(phone, order_id)], checks=checks, max_price=0.2,
                    persist_pool=True,
                )
                self.assertIsNone(error)
                self.assertEqual(self.otp.call_args.args[1], str(100000 + account_id))
                self.assertTrue(all(call.kwargs["order_id"] == order_id for call in self.check.call_args_list))
                self.assertEqual(buy.call_count, int(account_id in (1, 4)))
                self.assertEqual(self.reuse.call_count, 0)
                self.assertEqual(cancel.call_count, 0)
                self.assertEqual(self.finish.call_count, int(account_id == 3))
                finishes += self.finish.call_count
                sms = fresh_sms
            pool = num5sim.ActivationPool.load()
        self.assertEqual(finishes, 1)
        self.assertEqual(list(store.bindings.values()).count(PHONE), 3)
        self.assertEqual(pool.entries[0].last_order_id, 1)
        self.assertEqual(pool.entries[1].last_order_id, 2)

    async def test_selected_providers_override_stale_country_filter_for_active_orders(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", last_order_id=1)])
        buy, _cancel, error = await self.run_verifier(
            BindingStore(), pool=pool, country="greece", providers=[("usa", "any")],
            checks=[order(), order(sms=[{"code": "654321"}])],
        )
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 0)
        self.assertEqual(self.reuse.call_count, 0)

    async def test_expired_order_and_explicit_reuse_refusal_fall_back_to_purchase(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", last_order_id=1)])
        buy, _cancel, error = await self.run_verifier(
            BindingStore(), pool=pool,
            checks=[order(expires="2000-01-01T00:00:00Z"), order(order_id=2, sms=[{"code": "654321"}])],
            reuse_order=num5sim.FiveSimReuseUnavailableError("reuse expired"),
            purchases=[order(order_id=2)],
        )
        self.assertIsNone(error)
        self.assertEqual(buy.call_count, 1)
        self.assertEqual(self.reuse.call_count, 1)
        self.assertEqual(pool.entries[0].last_order_id, 2)

    async def test_ambiguous_active_order_lookup_does_not_purchase_another(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai", last_order_id=1)])
        store = BindingStore()
        buy, _cancel, error = await self.run_verifier(
            store, pool=pool, checks=[num5sim.FiveSimGatewayError("response lost")],
        )
        self.assertIsInstance(error, num5sim.FiveSimGatewayError)
        self.assertEqual(buy.call_count, 0)
        self.assertEqual(self.reuse.call_count, 0)
        self.assertEqual(store.reserved, set())

    async def test_finished_order_during_sms_poll_does_not_submit_old_code(self):
        store = BindingStore()
        _buy, cancel, error = await self.run_verifier(
            store, checks=[order(status="FINISHED", sms=[{"code": "654321"}])],
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(self.otp.call_count, 0)
        self.assertEqual(store.bindings, {})
        self.assertEqual(cancel.call_count, 1)

    async def test_pool_save_failure_after_binding_does_not_cancel_shared_order(self):
        store = BindingStore()
        _buy, cancel, error = await self.run_verifier(store, save_error=OSError("disk full"))
        self.assertIsInstance(error, OSError)
        self.assertEqual(store.bindings, {7: PHONE})
        self.assertEqual(cancel.call_count, 0)

    async def test_already_bound_account_does_not_buy_or_reuse(self):
        store = BindingStore(existing=PHONE)
        buy, _cancel, error = await self.run_verifier(store)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(buy.call_count, 0)
        self.assertEqual(self.reuse.call_count, 0)

    async def test_phone_verification_url_is_not_mistaken_for_otp_acceptance(self):
        page = SimpleNamespace(url="https://auth.openai.com/phone-verification")
        self.assertTrue(await codex_oauth._is_add_phone_page(page))

    async def test_cancellation_releases_reservation_without_binding(self):
        store = BindingStore()
        _buy, cancel, error = await self.run_verifier(store, otp_error=asyncio.CancelledError())
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(store.bindings, {})
        self.assertEqual(store.reserved, set())
        self.assertEqual(cancel.call_count, 1)

    async def test_cancellation_during_commit_waits_for_durable_binding(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        class SlowBindingStore(BindingStore):
            @contextmanager
            def reserve_codex_phone(self, *args, **kwargs):
                with super().reserve_codex_phone(*args, **kwargs) as bind:
                    def commit():
                        loop.call_soon_threadsafe(started.set)
                        if not release.wait(timeout=2):
                            raise TimeoutError("fixture commit timeout")
                        return bind()
                    yield commit

        store = SlowBindingStore()
        pool = num5sim.ActivationPool()
        task = asyncio.create_task(self.run_verifier(store, pool=pool))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        release.set()
        _buy, cancel, error = await task
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(store.bindings, {7: PHONE})
        self.assertEqual(store.reserved, set())
        self.assertEqual(cancel.call_count, 0)
        self.assertEqual(self.finish.call_count, 0)
        self.assertEqual(pool.entries[0].last_order_id, 1)


if __name__ == "__main__":
    unittest.main()
