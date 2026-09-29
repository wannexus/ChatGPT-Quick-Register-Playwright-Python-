"""指纹浏览器（AntBrowser）接入 + 指纹层 + 5sim 提速/供应商限定的回归测试。"""

from __future__ import annotations

import io
import json
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import register
from core import ant_browser, num5sim, stealth
from core import fingerprint as fp


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class FakeAntServer:
    """最小可用的 AntBrowser Launch API 假服务。"""

    def __init__(self, *, fail_launch=False, no_cdp=False, drop_profile=False):
        self.calls: list[tuple[str, str, dict]] = []
        self.profiles: dict[str, dict] = {}
        self.fail_launch = fail_launch
        self.no_cdp = no_cdp
        self.drop_profile = drop_profile
        self._next = 0

    def opener(self, request, timeout):
        method = request.get_method()
        path = urllib.parse.urlsplit(request.full_url).path
        body = json.loads(request.data.decode()) if request.data else {}
        self.calls.append((method, path, body))

        if path == "/api/health":
            return _Response(b'{"ok":true}')
        if path == "/api/profiles" and method == "GET":
            return _Response(json.dumps({"count": len(self.profiles), "items": list(self.profiles.values())}).encode())
        if path == "/api/profiles" and method == "POST":
            self._next += 1
            pid = f"fake-profile-{self._next}"
            profile = {"profileId": pid, "profileName": body["profile"]["profileName"],
                       "coreId": "core-fixture", "fingerprintArgs": body["profile"].get("fingerprintArgs", [])}
            self.profiles[pid] = profile
            return _Response(json.dumps({"ok": True, "created": True, "profile": profile}).encode())
        if path.startswith("/api/profiles/") and method == "GET":
            pid = path.rsplit("/", 1)[-1]
            if self.drop_profile or pid not in self.profiles:
                return _Response(b'{"ok":false,"error":"not found"}')
            return _Response(json.dumps({"ok": True, "profile": self.profiles[pid]}).encode())
        if path.startswith("/api/profiles/") and path.endswith("/stop"):
            return _Response(b'{"ok":true,"active":false}')
        if path.startswith("/api/profiles/") and method == "DELETE":
            return _Response(b'{"ok":true,"deleted":true}')
        if path == "/api/launch":
            if self.fail_launch:
                return _Response(b'{"ok":false,"error":"launch failed"}')
            selector = body.get("selector") or {}
            pid = selector.get("profileId") or ""
            payload = {"ok": True, "profileId": pid, "profileName": self.profiles.get(pid, {}).get("profileName", ""),
                       "debugReady": not self.no_cdp, "debugPort": 0 if self.no_cdp else 51234,
                       "pid": 4321, "launchCode": "ABC123"}
            if not self.no_cdp:
                payload["directDebugUrl"] = "http://127.0.0.1:51234"
            return _Response(json.dumps(payload).encode())
        if path == "/api/runtime/session":
            pid = (body.get("selector") or {}).get("profileId", "")
            payload = {"ok": True, "profileId": pid, "profileName": self.profiles.get(pid, {}).get("profileName", ""),
                       "debugReady": not self.no_cdp,
                       "directDebugUrl": "" if self.no_cdp else "http://127.0.0.1:51234",
                       "cdpUrl": "" if self.no_cdp else "http://127.0.0.1:19876", "pid": 4321}
            return _Response(json.dumps(payload).encode())
        return _Response(b'{"ok":false,"error":"unexpected"}')

    def client(self, **kwargs):
        return ant_browser.AntBrowserClient(opener=self.opener, **kwargs)


class AntProxyConfigTests(unittest.TestCase):
    def test_credentials_are_encoded_once(self):
        self.assertEqual(
            ant_browser.proxy_config_for_ant("http://user:p+w/d@host:9091"),
            "http://user:p%2Bw%2Fd@host:9091",
        )
        self.assertEqual(
            ant_browser.proxy_config_for_ant("http://user:p%2Bw@host:9091"),
            "http://user:p%2Bw@host:9091",
            "已编码的密码不能被二次编码",
        )

    def test_plain_and_empty_values_pass_through(self):
        self.assertEqual(ant_browser.proxy_config_for_ant("http://host:9091"), "http://host:9091")
        self.assertEqual(ant_browser.proxy_config_for_ant(""), "")
        self.assertEqual(ant_browser.proxy_config_for_ant("not-a-url"), "not-a-url")


class AntBrowserClientTests(unittest.TestCase):
    def test_health_and_profile_creation_carry_fingerprint_args(self):
        server = FakeAntServer()
        client = server.client()
        self.assertTrue(client.health())
        profile = client.create_profile("qr-test", fingerprint_args=["--fingerprint=1"], core_id="core-fixture")
        self.assertEqual(profile["profileId"], "fake-profile-1")
        created = [c for c in server.calls if c[0] == "POST" and c[1] == "/api/profiles"][0]
        self.assertEqual(created[2]["profile"]["fingerprintArgs"], ["--fingerprint=1"])
        self.assertEqual(created[2]["profile"]["coreId"], "core-fixture")

    def test_start_session_creates_launches_and_prefers_direct_debug_url(self):
        server = FakeAntServer()
        client = server.client()
        session = client.start_session(name="qr-test", identity=fp.random_identity(region="US"))
        self.assertTrue(session.created_profile)
        self.assertEqual(session.cdp_url, "http://127.0.0.1:51234")
        self.assertEqual(session.pid, 4321)
        self.assertEqual(session.debug_port, 51234)

    def test_start_session_reuses_an_existing_profile(self):
        server = FakeAntServer()
        client = server.client()
        profile = client.create_profile("qr-keep", fingerprint_args=["--fingerprint=2"])
        self.assertFalse(server.profiles[profile["profileId"]].get("running", False))
        session = client.start_session(name="qr-keep", identity=fp.random_identity(),
                                       reuse_profile_id=profile["profileId"])
        self.assertFalse(session.created_profile)
        self.assertEqual(session.profile_id, profile["profileId"])
        self.assertEqual(len([c for c in server.calls if c[0] == "POST" and c[1] == "/api/profiles"]), 1)

    def test_start_session_recreates_when_the_saved_profile_is_gone(self):
        server = FakeAntServer(drop_profile=True)
        client = server.client()
        session = client.start_session(name="qr-test", identity=fp.random_identity(),
                                       reuse_profile_id="missing-profile")
        self.assertTrue(session.created_profile)
        self.assertNotEqual(session.profile_id, "missing-profile")

    def test_launch_failure_falls_back_to_runtime_session(self):
        server = FakeAntServer(fail_launch=True)
        client = server.client()
        session = client.start_session(name="qr-test", identity=fp.random_identity())
        self.assertEqual(session.cdp_url, "http://127.0.0.1:51234")

    def test_missing_cdp_url_is_an_error(self):
        server = FakeAntServer(no_cdp=True)
        client = server.client()
        with self.assertRaises(ant_browser.AntBrowserApiError):
            client.start_session(name="qr-test", identity=fp.random_identity(), ready_timeout=1.0)

    def test_unreachable_api_reports_actionable_error(self):
        def opener(_request, _timeout):
            raise OSError("connection refused")

        client = ant_browser.AntBrowserClient(opener=opener)
        self.assertFalse(client.health())
        with self.assertRaises(ant_browser.AntBrowserUnavailable) as ctx:
            client.list_profiles()
        self.assertIn("打开 AntBrowser", str(ctx.exception))


class FingerprintVerificationTests(unittest.TestCase):
    def test_matching_runtime_has_no_issues(self):
        identity = fp.random_identity(region="US", platform="windows")
        persona = identity.persona
        runtime = {
            "userAgent": stealth.user_agent_for(identity),
            "platform": "Win32",
            "webdriver": False,
            "lang": persona.lang,
            "timezone": persona.timezone,
            "hardwareConcurrency": persona.cores,
        }
        self.assertEqual(ant_browser.fingerprint_mismatches(identity, runtime), [])

    def test_stock_chrome_core_is_detected(self):
        identity = fp.random_identity(region="DE", platform="windows")
        runtime = {
            "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
            "platform": "MacIntel",
            "webdriver": True,
            "lang": "de-DE",
            "timezone": "Asia/Shanghai",
            "hardwareConcurrency": 10,
        }
        issues = "；".join(ant_browser.fingerprint_mismatches(identity, runtime))
        self.assertIn("时区", issues)
        self.assertIn("navigator.platform", issues)
        self.assertIn("webdriver", issues)


class StealthLayerTests(unittest.TestCase):
    def test_context_options_match_the_persona(self):
        identity = fp.random_identity(region="JP", platform="macos")
        options = stealth.context_options_for(identity)
        self.assertEqual(options["locale"], identity.persona.lang)
        self.assertEqual(options["timezone_id"], identity.persona.timezone)
        self.assertEqual(options["screen"]["width"], identity.persona.screen[0])
        self.assertIn("Macintosh", options["user_agent"])

    def test_init_script_embeds_the_identity_and_leaves_no_placeholder(self):
        identity = fp.random_identity(region="GB", platform="windows")
        script = stealth.init_script_for(identity)
        self.assertNotIn("__QR_PAYLOAD__", script)
        self.assertIn(identity.persona.timezone, script)
        self.assertIn("Win32", script)
        self.assertIn("NVIDIA", script)
        payload = json.loads(script.split("const P = ", 1)[1].split(";", 1)[0])
        self.assertEqual(payload["cores"], identity.persona.cores)
        self.assertEqual(payload["seed"], int(identity.seed))

    def test_identity_is_stable_per_seed_but_different_across_runs(self):
        seeds = {fp.random_identity().seed for _ in range(50)}
        self.assertGreater(len(seeds), 45)
        for identity in (fp.random_identity(), fp.random_identity()):
            self.assertTrue(any(arg.startswith("--fingerprint=") for arg in identity.args))
            self.assertEqual(identity.args, fp.build_fingerprint_args(identity))

    def test_country_hint_selects_a_matching_persona_region(self):
        self.assertEqual(fp.random_identity(country="japan").persona.region, "JP")
        self.assertEqual(fp.random_identity(country="poland").persona.region, "PL")
        self.assertEqual(fp.random_identity(country="unknown-place").persona.region in
                         {p.region for p in fp.PERSONAS}, True)


class FiveSimSpeedAndProviderTests(unittest.TestCase):
    def setUp(self):
        num5sim.clear_price_cache()

    def _entries(self):
        return [
            num5sim.PriceEntry("poland", "virtual66", "openai", 0.15, 40, 90),
            num5sim.PriceEntry("greece", "virtual34", "openai", 0.12, 30, 80),
            num5sim.PriceEntry("usa", "any", "openai", 0.5, 10, 70),
        ]

    def test_strict_provider_only_returns_the_selected_provider(self):
        with patch.object(num5sim, "query_prices", return_value=self._entries()):
            picked = num5sim.find_buy_candidates(product="openai", country="poland", operator="any",
                                                strict_provider=True)
            self.assertEqual([(e.country, e.operator) for e in picked], [("poland", "virtual66")])
            none = num5sim.find_buy_candidates(product="openai", country="portugal", operator="any",
                                               strict_provider=True)
            self.assertEqual(none, [], "选定的供应商没货时不能跑到别家去买")

    def test_loose_mode_still_can_wander(self):
        with patch.object(num5sim, "query_prices", return_value=self._entries()):
            picked = num5sim.find_buy_candidates(product="openai", country="portugal",
                                                strict_provider=False)
            self.assertTrue(picked)

    def test_price_snapshot_is_cached(self):
        calls = {"n": 0}

        def fake_get(path, **_kwargs):
            calls["n"] += 1
            return {"poland": {"virtual66": {"openai": {"cost": 0.15, "count": 40, "rate": 90}}}}

        with patch.object(num5sim, "_get", side_effect=fake_get):
            first = num5sim.query_prices(product="openai")
            second = num5sim.query_prices(product="openai")
            third = num5sim.query_prices(product="openai", use_cache=False)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(calls["n"], 2, "命中快照的那次不该再发请求")
        self.assertEqual(third, first)

    def test_metadata_calls_use_the_short_timeout(self):
        self.assertLessEqual(num5sim.METADATA_TIMEOUT, 12)


class FiveSimVerifierProviderTests(unittest.IsolatedAsyncioTestCase):
    def _store(self):
        from contextlib import contextmanager

        class _Store:
            def get_account(self, _account_id):
                return {"id": 1, "codexPhoneNumber": ""}

            @contextmanager
            def reserve_codex_phone(self, *_args, **_kwargs):
                yield lambda: {"ok": True, "account_count": 1}

        return _Store()

    async def test_selected_provider_has_no_stock_and_does_not_buy_elsewhere(self):
        bought: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            bought.append((kwargs["country"], kwargs["operator"]))
            raise num5sim.FiveSimNoFreePhonesError("no free phones")

        with patch.object(num5sim, "buy_activation", side_effect=fake_buy), \
             patch.object(num5sim, "find_buy_candidates", return_value=[]) as candidates:
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", country="poland", operator="virtual66", account_id=1,
                account_store=self._store(), proxy=None,
            )
            with self.assertRaises(RuntimeError) as ctx:
                await verifier(SimpleNamespace())
        self.assertIn("供应商", str(ctx.exception))
        self.assertEqual(bought, [("poland", "virtual66")])
        self.assertEqual(candidates.call_args.kwargs["strict_provider"], True)
        self.assertEqual(candidates.call_args.kwargs["country"], "poland")

    async def test_other_providers_flag_relaxes_the_restriction(self):
        with patch.object(num5sim, "find_buy_candidates", return_value=[]) as candidates, \
             patch.object(num5sim, "buy_activation",
                          side_effect=num5sim.FiveSimNoFreePhonesError("no free phones")):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", country="poland", operator="any", account_id=1,
                account_store=self._store(), proxy=None, allow_other_providers=True,
            )
            with self.assertRaises(RuntimeError):
                await verifier(SimpleNamespace())
        self.assertEqual(candidates.call_args.kwargs["strict_provider"], False)

    async def test_pool_provider_is_tried_before_the_generic_attempt(self):
        pool = num5sim.ActivationPool([
            num5sim.PoolEntry("+485518000111", "poland", "virtual66", "openai", last_used_at=1000.0),
        ])
        attempted: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            attempted.append((kwargs["country"], kwargs["operator"]))
            raise num5sim.FiveSimNoFreePhonesError("no free phones")

        with patch.object(num5sim, "buy_activation", side_effect=fake_buy), \
             patch.object(num5sim, "find_buy_candidates", return_value=[]), \
             patch.object(num5sim, "reuse_number", side_effect=num5sim.FiveSimNoFreePhonesError("no")), \
             patch.object(pool, "save", Mock()):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", country="any", operator="any", account_id=1,
                account_store=self._store(), reuse_pool=pool, proxy=None,
            )
            with self.assertRaises(RuntimeError):
                await verifier(SimpleNamespace())
        self.assertEqual(attempted[0], ("poland", "virtual66"), "先用上次成功的供应商，少一次空跑")


class ProviderListTests(unittest.TestCase):
    """已选供应商列表：顺序=优先级。"""

    def test_json_text_and_object_forms_parse_the_same(self):
        expected = [("poland", "virtual66"), ("greece", "virtual34")]
        self.assertEqual(num5sim.parse_providers('[{"country":"Poland","operator":"virtual66"},'
                                                 '{"country":"greece","operator":"virtual34"}]'), expected)
        self.assertEqual(num5sim.parse_providers("poland/virtual66, greece|virtual34"), expected)
        self.assertEqual(num5sim.parse_providers(["poland/virtual66", "greece/virtual34"]), expected)
        self.assertEqual(num5sim.parse_providers([("poland", "virtual66"), ("greece", "virtual34")]), expected)

    def test_priority_order_is_preserved_and_duplicates_dropped(self):
        listed = num5sim.parse_providers("usa/virtual21, poland/virtual66, usa/virtual21")
        self.assertEqual(listed, [("usa", "virtual21"), ("poland", "virtual66")])

    def test_broken_config_never_becomes_a_purchase_target(self):
        self.assertEqual(num5sim.parse_providers(""), [])
        self.assertEqual(num5sim.parse_providers(None), [])
        self.assertEqual(num5sim.parse_providers("[not json"), [])
        self.assertEqual(num5sim.format_providers(""), "[]")

    def test_round_trip_through_env_text(self):
        text = num5sim.format_providers([("usa", "virtual21"), ("poland", "any")])
        self.assertEqual(num5sim.parse_providers(text), [("usa", "virtual21"), ("poland", "any")])


class FiveSimPriorityPurchaseTests(unittest.IsolatedAsyncioTestCase):
    """买号必须按优先级顺序，且只在选定的供应商里选。"""

    def _store(self):
        from contextlib import contextmanager

        class _Store:
            def get_account(self, _account_id):
                return {"id": 1, "codexPhoneNumber": ""}

            @contextmanager
            def reserve_codex_phone(self, *_args, **_kwargs):
                yield lambda: {"ok": True, "account_count": 1}

        return _Store()

    def _order(self, country: str, operator: str):
        return num5sim.ActivationOrder(
            id=7, phone="+485518000111", operator=operator, product="openai", price=0.15,
            status="PENDING", expires="", country=country,
        )

    async def test_falls_through_to_the_next_priority(self):
        attempted: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            attempted.append((kwargs["country"], kwargs["operator"]))
            if len(attempted) == 1:
                raise num5sim.FiveSimNoFreePhonesError("no free phones")
            return self._order(kwargs["country"], kwargs["operator"])

        with patch.object(num5sim, "buy_activation", side_effect=fake_buy), \
             patch.object(num5sim, "stock_counts", return_value={}), \
             patch.object(num5sim, "query_prices", return_value=[]):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", account_id=1, account_store=self._store(), proxy=None,
                providers=[("greece", "virtual34"), ("poland", "virtual66")],
            )
            # 买到号之后才是页面交互（这里没有真页面），所以只断言买号的顺序
            with self.assertRaises(Exception):
                await verifier(SimpleNamespace())

        self.assertEqual(attempted, [("greece", "virtual34"), ("poland", "virtual66")],
                         "希腊无号必须自动切到第二优先级波兰")

    async def test_only_selected_providers_are_ever_bought(self):
        attempted: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            attempted.append((kwargs["country"], kwargs["operator"]))
            raise num5sim.FiveSimNoFreePhonesError("no free phones")

        with patch.object(num5sim, "buy_activation", side_effect=fake_buy), \
             patch.object(num5sim, "stock_counts", return_value={}), \
             patch.object(num5sim, "query_prices", return_value=[]):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", account_id=1, account_store=self._store(), proxy=None,
                providers="poland/virtual66, greece/virtual34",
            )
            with self.assertRaises(RuntimeError) as ctx:
                await verifier(SimpleNamespace())

        self.assertEqual(attempted, [("poland", "virtual66"), ("greece", "virtual34")])
        self.assertIn("优先级", str(ctx.exception))

    async def test_empty_snapshot_providers_are_skipped_but_not_ordered_away(self):
        attempted: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            attempted.append((kwargs["country"], kwargs["operator"]))
            return self._order(kwargs["country"], kwargs["operator"])

        with patch.object(num5sim, "buy_activation", side_effect=fake_buy), \
             patch.object(num5sim, "stock_counts", return_value={("greece", "virtual34"): 0,
                                                                 ("poland", "virtual66"): 9}), \
             patch.object(num5sim, "query_prices", return_value=[]):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", account_id=1, account_store=self._store(), proxy=None,
                providers=[("greece", "virtual34"), ("poland", "virtual66")],
            )
            with self.assertRaises(Exception):
                await verifier(SimpleNamespace())

        self.assertEqual(attempted, [("poland", "virtual66")], "快照显示无货的先跳过，省一次买号请求")

    async def test_other_providers_are_only_used_when_the_flag_is_on(self):
        with patch.object(num5sim, "buy_activation",
                          side_effect=num5sim.FiveSimNoFreePhonesError("no free phones")), \
             patch.object(num5sim, "stock_counts", return_value={}), \
             patch.object(num5sim, "find_buy_candidates", return_value=[]) as candidates, \
             patch.object(num5sim, "query_prices", return_value=[]):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", account_id=1, account_store=self._store(), proxy=None,
                providers=[("poland", "virtual66")], allow_other_providers=True,
            )
            with self.assertRaises(RuntimeError):
                await verifier(SimpleNamespace())

        self.assertEqual(candidates.call_count, 1, "勾选了才去找其它供应商")

    async def test_reuse_pool_numbers_outside_the_list_are_skipped(self):
        pool = num5sim.ActivationPool([
            num5sim.PoolEntry("+12025550000", "usa", "virtual21", "openai", last_used_at=2000.0),
            num5sim.PoolEntry("+485518000111", "poland", "virtual66", "openai", last_used_at=1000.0),
        ])
        reused: list[str] = []

        def fake_reuse(**kwargs):
            reused.append(kwargs["phone"])
            return self._order("poland", "virtual66")

        with patch.object(num5sim, "reuse_number", side_effect=fake_reuse), \
             patch.object(pool, "save", Mock()):
            verifier = register.codex_oauth_module.create_5sim_phone_verifier(
                "key", account_id=1, account_store=self._store(), reuse_pool=pool, proxy=None,
                providers=[("poland", "virtual66")],
            )
            with self.assertRaises(Exception):
                await verifier(SimpleNamespace())

        self.assertEqual(reused, ["+485518000111"], "池里不在选定列表内的号码不能被复用")


class FingerprintModeTests(unittest.IsolatedAsyncioTestCase):
    """默认必须是「内置指纹层」，不许再自动去连 AntBrowser。"""

    def _args(self, mode: str):
        return SimpleNamespace(
            fingerprint_browser=mode, fingerprint_region="US", fingerprint_platform="windows",
            ant_api_base="http://127.0.0.1:1", ant_api_key="", ant_core_id="",
            ant_keep_profile=True, proxy="",
        )

    def test_the_cli_default_is_the_builtin_layer(self):
        self.assertEqual(register.parse_args(["--mode", "register"]).fingerprint_browser, "stealth")
        self.assertIn("stealth", register.parse_args(["--mode", "register", "--fingerprint-browser", "stealth"]).fingerprint_browser)

    def test_the_legacy_auto_value_now_means_the_builtin_layer(self):
        self.assertEqual(register._fingerprint_mode(self._args("auto")), "stealth")
        self.assertEqual(register._fingerprint_mode(self._args("stealth")), "stealth")
        self.assertEqual(register._fingerprint_mode(SimpleNamespace()), "stealth")

    async def test_default_mode_never_touches_the_antbrowser_api(self):
        fake = FakeAntServer()
        for mode in ("stealth", "off"):
            with patch.object(ant_browser.urllib.request, "build_opener",
                              side_effect=AssertionError("默认模式不应访问 AntBrowser")):
                client, session = await register._start_fingerprint_session(
                    self._args(mode), reuse_profile_id="", name="qr-test")
            self.assertIsNone(client)
            self.assertIsNone(session)
        self.assertEqual(fake.calls, [])

    async def test_explicit_ant_mode_still_needs_a_running_antbrowser(self):
        with patch.object(ant_browser.AntBrowserClient, "health", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                await register._start_fingerprint_session(
                    self._args("ant"), reuse_profile_id="", name="qr-test")
        self.assertIn("AntBrowser", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
