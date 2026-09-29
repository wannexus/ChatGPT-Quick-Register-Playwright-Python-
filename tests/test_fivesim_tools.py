from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from core import num5sim
from webui import server


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


class FiveSimPriceTests(unittest.TestCase):
    def test_price_priority_changes_the_purchase_candidate_order(self):
        prices = [num5sim.PriceEntry("usa", "any", "openai", 0.5, 1, 99),
                  num5sim.PriceEntry("vietnam", "any", "openai", 0.1, 1, 80)]
        with patch.object(num5sim, "query_prices", return_value=prices):
            candidates = num5sim.find_buy_candidates(priority="price")
        self.assertEqual(candidates[0].country, "vietnam")


if __name__ == "__main__":
    unittest.main()
