from __future__ import annotations

import asyncio
import io
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core import codex_oauth, num5sim


PHONE = "+15550000001"


class BindingStore:
    def __init__(self, full=(), existing=""):
        self.full = set(full)
        self.bindings = {}
        self.reserved = set()
        self.existing = existing

    def get_account(self, account_id):
        return {"id": account_id, "codexPhoneNumber": self.existing}

    @contextmanager
    def reserve_codex_phone(self, account_id, phone, **kwargs):
        if phone in self.full:
            yield None
            return
        self.reserved.add(phone)
        try:
            def bind():
                self.bindings[account_id] = phone
                return {"ok": True, "account_count": 1}
            yield bind
        finally:
            self.reserved.remove(phone)


def order(phone=PHONE, order_id=1):
    return num5sim.ActivationOrder(order_id, phone, "any", "openai", 0.1,
                                  "RECEIVED", "", [{"code": "654321"}], "usa")


class FiveSimOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def run_verifier(self, store, *, purchases=None, accepted=True, pool=None, proxy=None, otp_error=None, max_price=None):
        page = SimpleNamespace(locator=Mock(), url="https://auth.openai.com/consent",
                               wait_for_load_state=AsyncMock())
        field = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                click=AsyncMock(), fill=AsyncMock(), evaluate=AsyncMock())
        page.locator.return_value.first = field
        pool = pool if pool is not None else num5sim.ActivationPool()
        buy = Mock(side_effect=purchases or [order()])
        real_sleep = asyncio.sleep

        async def no_wait(_seconds):
            await real_sleep(0)

        async def submit_otp(*_args):
            self.assertIn(PHONE, store.reserved)
            if otp_error:
                raise otp_error
            return True

        reuse = Mock(return_value=order())
        self.reuse = reuse
        self.output = io.StringIO()
        with redirect_stdout(self.output), \
             patch.object(num5sim, "buy_activation", buy), \
             patch.object(num5sim, "reuse_number", reuse), \
             patch.object(num5sim, "check_order", Mock(return_value=order())), \
             patch.object(num5sim, "finish_order", Mock()), \
             patch.object(num5sim, "cancel_order", Mock()) as cancel, \
             patch.object(pool, "save", Mock()), \
             patch.object(codex_oauth, "_select_phone_country", AsyncMock(return_value=True)), \
             patch.object(codex_oauth, "_click_with_force_fallback", AsyncMock(return_value=True)), \
             patch.object(codex_oauth, "_fill_otp_code", AsyncMock(side_effect=submit_otp)), \
             patch.object(codex_oauth, "_is_add_phone_page", AsyncMock(return_value=not accepted)), \
             patch.object(codex_oauth, "_has_visible_otp_input", AsyncMock(return_value=not accepted)), \
             patch.object(codex_oauth.asyncio, "sleep", no_wait):
            verify = codex_oauth.create_5sim_phone_verifier(
                "fake-key", account_id=7, account_store=store, reuse_pool=pool, proxy=proxy,
                max_price=max_price,
            )
            error = None
            try:
                await verify(page)
            except BaseException as exc:
                error = exc
        return buy, cancel, error

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

    async def test_price_ceiling_uses_capped_purchase_instead_of_unbounded_reuse(self):
        pool = num5sim.ActivationPool([num5sim.PoolEntry(PHONE, "usa", "any", "openai")])
        buy, _cancel, error = await self.run_verifier(BindingStore(), pool=pool, max_price=0.5)
        self.assertIsNone(error)
        self.assertEqual(self.reuse.call_count, 0)
        self.assertEqual(buy.call_args.kwargs["max_price"], 0.5)

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
        task = asyncio.create_task(self.run_verifier(store))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        release.set()
        _buy, _cancel, error = await task
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(store.bindings, {7: PHONE})
        self.assertEqual(store.reserved, set())


if __name__ == "__main__":
    unittest.main()
