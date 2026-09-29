from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import num5sim
from webui import server
import register


class FiveSimToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_buy_uses_saved_key_when_request_omits_it(self):
        order = num5sim.ActivationOrder(10, "+15550000001", "any", "openai", 0.1, "PENDING", "")
        with patch.object(server, "effective_config", return_value={"fiveSimApiKey": "fixture"}), \
             patch.object(num5sim, "buy_activation", Mock(return_value=order)) as buy:
            result = await server.five_sim_buy(server.FiveSimBuyPayload(country="usa"))
        self.assertTrue(result["ok"])
        self.assertEqual(buy.call_args.kwargs["api_key"], "fixture")

    async def test_manual_reuse_targets_the_clicked_inventory_phone(self):
        first = num5sim.PoolEntry("+15550000001", "usa", "any", "openai")
        second = num5sim.PoolEntry("+15550000002", "usa", "any", "openai", successful_uses=99)
        pool = num5sim.ActivationPool([first, second])
        order = num5sim.ActivationOrder(11, second.phone, "any", "openai", 0.1, "PENDING", "")
        with patch.object(server, "effective_config", return_value={"fiveSimApiKey": "fixture"}), \
             patch.object(num5sim.ActivationPool, "load", return_value=pool), \
             patch.object(pool, "save", Mock()), \
             patch.object(num5sim, "reuse_number", Mock(return_value=order)) as reuse:
            result = await server.five_sim_reuse(server.FiveSimReusePayload(phone=second.phone))
        self.assertTrue(result["ok"])
        self.assertEqual(reuse.call_args.kwargs["phone"], second.phone)
        self.assertEqual(reuse.call_args.kwargs["api_key"], "fixture")
        self.assertEqual(len(pool.entries), 2)

    async def test_sms_direct_connection_does_not_change_the_browser_proxy(self):
        cfg = {"fiveSimApiKey": "fixture", "fiveSimUseProxy": "0",
               "proxy": "http://fixture:8080", "proxyInsecure": "1"}
        with patch.object(server, "effective_config", return_value=cfg), \
             patch.object(num5sim, "get_profile", return_value={"balance": 1}) as profile:
            await server.five_sim_profile(server.FiveSimApiKeyPayload())
        self.assertIsNone(profile.call_args.kwargs["proxy"])
        self.assertFalse(profile.call_args.kwargs["proxy_insecure"])
        self.assertEqual(cfg["proxy"], "http://fixture:8080")


class FiveSimPriceTests(unittest.TestCase):
    def test_price_priority_changes_the_purchase_candidate_order(self):
        prices = [num5sim.PriceEntry("usa", "any", "openai", 0.5, 1, 99),
                  num5sim.PriceEntry("vietnam", "any", "openai", 0.1, 1, 80)]
        with patch.object(num5sim, "query_prices", return_value=prices):
            candidates = num5sim.find_buy_candidates(priority="price")
        self.assertEqual(candidates[0].country, "vietnam")

    def test_cli_sms_proxy_choice_is_independent_of_browser_proxy(self):
        args = SimpleNamespace(proxy="http://fixture:8080", proxy_insecure=True,
                               **{"5sim_api_key": "fixture", "5sim_use_proxy": False})
        with patch.object(num5sim.ActivationPool, "load", return_value=num5sim.ActivationPool()), \
             patch.object(register.codex_oauth_module, "create_5sim_phone_verifier") as create:
            register._build_5sim_phone_verifier(args, account_id=7, account_store=object())
            self.assertIsNone(create.call_args.kwargs["proxy"])
            self.assertFalse(create.call_args.kwargs["proxy_insecure"])
            setattr(args, "5sim_use_proxy", True)
            register._build_5sim_phone_verifier(args, account_id=7, account_store=object())
            self.assertEqual(create.call_args.kwargs["proxy"], args.proxy)
        self.assertEqual(args.proxy, "http://fixture:8080")

    def test_text_gateway_response_is_a_distinct_ambiguous_result(self):
        with self.assertRaises(num5sim.FiveSimGatewayError):
            num5sim._raise_text_error("/v1/user/buy/activation/any/any/openai", "502 Bad Gateway")


if __name__ == "__main__":
    unittest.main()
