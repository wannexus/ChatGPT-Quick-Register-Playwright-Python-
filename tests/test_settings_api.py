from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import local_config
from webui import server


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    """The settings page persists everything into .env and nothing into JSON."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("# settings\nQR_MYSQL_HOST=127.0.0.1\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            patch.object(local_config, "ENV_PATH", self.env_path),
            patch.object(local_config, "LEGACY_JSON_PATH", self.root / "config.local.json"),
        ]
        for item in self._patches:
            item.start()
        for item in reversed(self._patches):
            self.addCleanup(item.stop)

    async def test_post_defaults_writes_every_section_into_env(self):
        payload = server.LocalConfigPayload(
            duckToken="duck-token",
            mhjcApiKey="mhjc-key",
            mhjcApiBase="https://api.mhjc.edu.kg/api",
            qqUser="me@qq.com",
            qqPass="qq-code",
            qqMaxAttempts=45,
            qqInterval=2.5,
            icloudLoginTimeout=420,
            acCheckerBaseUrl="http://ac.example:8787",
            acCheckerPromoId="promo-1",
            pay153BaseUrl="https://pay.example",
            proxyScheme="socks5",
            proxyHost="proxy.example.com",
            proxyPort="1080",
            proxyUser="alice",
            proxyPassword="s3cret",
        )
        with patch.dict(os.environ, {}, clear=True):
            result = await server.update_defaults(payload)
            text = self.env_path.read_text(encoding="utf-8")

        self.assertEqual(result["envPath"], str(self.env_path))
        for env_key in (
            "QR_DUCK_TOKEN", "QR_MHJC_API_KEY", "QR_MHJC_API_BASE", "QR_QQ_USER", "QR_QQ_PASS",
            "QR_QQ_MAX_ATTEMPTS", "QR_QQ_INTERVAL", "QR_ICLOUD_LOGIN_TIMEOUT",
            "QR_AC_CHECKER_BASE_URL", "QR_AC_CHECKER_PROMO_ID", "QR_PAY153_BASE_URL",
            "QR_PROXY_SCHEME", "QR_PROXY_HOST", "QR_PROXY_PORT", "QR_PROXY_USER", "QR_PROXY_PASSWORD",
        ):
            self.assertIn(env_key, text, f"{env_key} must be persisted to .env")

        self.assertIn("QR_MYSQL_HOST=127.0.0.1", text, "unrelated settings must survive")
        self.assertFalse((self.root / "config.local.json").exists())

    async def test_sms_page_payload_passes_http_validation_and_persists(self):
        payload = {
            "fiveSimCountry": "vietnam", "fiveSimOperator": "any", "fiveSimProduct": "openai",
            "fiveSimMaxPrice": "0.5", "fiveSimAcquirePriority": "price", "fiveSimCandidateLimit": 4,
            "fiveSimUseProxy": False,
        }
        messages = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/api/defaults", "raw_path": b"/api/defaults", "query_string": b"",
            "headers": [(b"content-type", b"application/json")], "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 12345), "root_path": "",
        }
        self.env_path.write_text("QR_FIVESIM_API_KEY=fixture-secret\nQR_QQ_USER=keep@example.com\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch.object(local_config, "PROCESS_ENV_KEYS", frozenset()):
            await server.app(scope, receive, send)
            settings = local_config.effective_config()
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        for key, value in payload.items():
            self.assertEqual(settings[key], ("1" if value else "") if isinstance(value, bool) else str(value))
        self.assertEqual(settings["fiveSimApiKey"], "fixture-secret")
        self.assertEqual(settings["qqUser"], "keep@example.com")
        self.assertNotIn(b"fixture-secret", body)

    async def test_round_trip_recomposes_the_proxy_and_keeps_other_sections(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(
                qqUser="me@qq.com", proxyHost="proxy.example.com", proxyPort="1080",
                proxyUser="alice", proxyPassword="s3cret",
            ))
            payload = await server.defaults()

        self.assertEqual(payload["proxyDisplay"], "http://alice:***@proxy.example.com:1080")
        self.assertEqual(payload["proxyHost"], "proxy.example.com")
        self.assertTrue(payload["proxyPasswordSet"])
        self.assertEqual(payload["qqUser"], "me@qq.com")
        self.assertNotIn("s3cret", str(payload))

    async def test_partial_save_does_not_clear_other_sections(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(duckToken="keep-me"))
            await server.update_defaults(server.LocalConfigPayload(qqUser="me@qq.com"))
            payload = await server.defaults()

        self.assertEqual(payload["duckToken"], "keep-me")
        self.assertEqual(payload["qqUser"], "me@qq.com")

    async def test_defaults_never_return_the_api_key(self):
        with patch.object(local_config, "PROCESS_ENV_KEYS", frozenset({"QR_MHJC_API_KEY"})), \
             patch.dict(os.environ, {"QR_MHJC_API_KEY": "top-secret"}, clear=True):
            payload = await server.defaults()

        self.assertTrue(payload["mhjcApiKeyPresent"])
        self.assertNotIn("top-secret", str(payload))

    async def test_the_register_form_defaults_are_saved_settings_too(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(
                emailSource="mhjc", codeSource="mhjc", authMode="otp",
                count=7, cooldown=45, headless=True, noClearTokens=True,
            ))
            payload = await server.defaults()
            text = self.env_path.read_text(encoding="utf-8")

        for env_key in ("QR_EMAIL_SOURCE", "QR_CODE_SOURCE", "QR_AUTH_MODE",
                        "QR_COUNT", "QR_COOLDOWN", "QR_HEADLESS", "QR_NO_CLEAR_TOKENS"):
            self.assertIn(env_key, text)
        self.assertEqual(payload["emailSource"], "mhjc")
        self.assertEqual(payload["codeSource"], "mhjc")
        self.assertEqual(payload["count"], "7")
        self.assertEqual(payload["cooldown"], "45")
        self.assertTrue(payload["headless"])
        self.assertTrue(payload["noClearTokens"])
        self.assertFalse(payload["noPersistent"])

    async def test_saved_form_defaults_change_the_cli_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(
                emailSource="mhjc", codeSource="mhjc", authMode="password",
                count=3, cooldown=12, stopOnError=True,
            ))
            import importlib
            import register
            importlib.reload(register)
            args = register.parse_args(["--mode", "register"])

        self.assertEqual(args.email_source, "mhjc")
        self.assertEqual(args.code_source, "mhjc")
        self.assertEqual(args.auth_mode, "password")
        self.assertEqual(args.count, 3)
        self.assertEqual(args.cooldown, 12)
        self.assertTrue(args.stop_on_error)
        self.assertFalse(args.headless)

    async def test_numeric_settings_reach_the_cli(self):
        args = server._build_args(server.BatchPayload(qqMaxAttempts=45, qqInterval=2.5, icloudLoginTimeout=420))
        self.assertEqual(args[args.index("--qq-max-attempts") + 1], "45")
        self.assertEqual(args[args.index("--qq-interval") + 1], "2.5")
        self.assertEqual(args[args.index("--icloud-login-timeout") + 1], "420")

import json
import re


class SettingsContractTests(unittest.TestCase):
    """The settings page must not drift from the API model (extra="forbid")."""

    def test_every_field_the_page_saves_is_accepted_by_the_api(self):
        html = Path("webui/static/index.html").read_text(encoding="utf-8")
        js = html[html.index("<script>") + 8: html.rindex("</script>")]

        body = js[js.index("async function saveSecrets()"):]
        body = body[:body.index("/api/defaults")]
        sent = set(re.findall(r"^\s{4}([a-zA-Z][A-Za-z0-9]*):", body, flags=re.MULTILINE))
        # conditionally attached in the same function
        sent |= set(re.findall(r"payload\.([a-zA-Z][A-Za-z0-9]*) =", body))

        model_fields = set(server.LocalConfigPayload.model_fields)
        unknown = sorted(sent - model_fields)
        self.assertEqual(unknown, [], f"settings page sends fields the API would reject: {unknown}")
        self.assertNotIn("configPath", json.dumps(sorted(model_fields)))

    def test_api_model_forbids_unknown_fields(self):
        with self.assertRaises(Exception):
            server.LocalConfigPayload(definitelyNotASetting="x")

class NavigationContractTests(unittest.TestCase):
    """Every nav tab must resolve; activateView() silently falls back otherwise."""

    def setUp(self):
        self.html = Path("webui/static/index.html").read_text(encoding="utf-8")
        self.js = self.html[self.html.index("<script>") + 8: self.html.rindex("</script>")]

    def _view_meta_keys(self):
        block = self.js[self.js.index("const viewMeta = {"):]
        block = block[:block.index("\n};")]
        return set(re.findall(r"^\s{2}([a-zA-Z][A-Za-z0-9]*):", block, flags=re.MULTILINE))

    def test_every_nav_tab_has_a_view_meta_entry(self):
        tabs = re.findall(r'data-view="([a-z]+)"', self.html)
        self.assertTrue(tabs)
        missing = sorted(set(tabs) - self._view_meta_keys())
        self.assertEqual(
            missing, [],
            f"these tabs would silently bounce back to the register view: {missing}",
        )

    def test_every_nav_tab_has_a_matching_section(self):
        tabs = set(re.findall(r'data-view="([a-z]+)"', self.html))
        sections = set(re.findall(r'<section class="view[^"]*" id="view-([a-z]+)"', self.html))
        self.assertEqual(tabs - sections, set(), "a tab without a view section is unclickable")

    def test_every_view_meta_entry_has_a_section(self):
        sections = set(re.findall(r'<section class="view[^"]*" id="view-([a-z]+)"', self.html))
        self.assertEqual(self._view_meta_keys() - sections, set())

    def test_fivesim_panel_is_owned_by_the_sms_view_only(self):
        sms = self.html[self.html.index('id="view-sms"'):]
        sms = sms[:sms.index("</section>")]
        tools = self.html[self.html.index('id="view-tools"'):]
        tools = tools[:tools.index("</section>")]
        self.assertIn('id="fivesimPanel"', sms)
        self.assertNotIn('id="fivesimPanel"', tools)
        self.assertEqual(len(re.findall(r'id="fsApiKey"', self.html)), 1)
        self.assertNotIn('id="fiveSimApiKey"', self.html)

    def test_settings_page_contains_all_four_groups(self):
        section = self.html[self.html.index('id="view-settings"'):]
        section = section[:section.index("</section>")]
        for heading in ("邮箱设置", "凭证", "高级设置", "代理设置"):
            self.assertIn(heading, section, f"the settings page is missing the {heading} group")

class SettingsRoundTripContractTests(unittest.TestCase):
    """Anything the page saves must also be restored, and vice versa.

    A setting that can be written but never read back looks saved and silently
    reverts on reload: the same class of bug as a missing `viewMeta` entry.
    """

    # Deliberately write-only: they are reported as "...Set" flags instead.
    WRITE_ONLY = {"mhjcApiKey", "proxyPassword"}

    def setUp(self):
        html = Path("webui/static/index.html").read_text(encoding="utf-8")
        self.js = html[html.index("<script>") + 8: html.rindex("</script>")]
        body = self.js[self.js.index("async function saveSecrets()"):]
        self.saved = set(re.findall(
            r"^\s{4}([a-zA-Z][A-Za-z0-9]*):",
            body[:body.index("/api/defaults")],
            flags=re.MULTILINE,
        ))
        self.saved |= set(re.findall(r"payload\.([a-zA-Z][A-Za-z0-9]*) =",
                                    body[:body.index("/api/defaults")]))
        # Checkboxes are restored through a loop over a literal key list.
        self.dynamic = set()
        for literal in re.findall(r"\[([^\]]*)\]\.forEach\(key =>", self.js):
            self.dynamic |= set(re.findall(r'"([A-Za-z][A-Za-z0-9]*)"', literal))

    def _restored(self, field: str) -> bool:
        return bool(re.search(rf"(?<![\w$.])d\.{field}\b", self.js)) or field in self.dynamic

    def test_every_saved_setting_is_restored_on_load(self):
        missing = sorted(
            field for field in self.saved - self.WRITE_ONLY
            if not self._restored(field)
        )
        self.assertEqual(missing, [], f"saved but never loaded back: {missing}")

    def test_write_only_settings_are_reported_as_flags(self):
        for field, flag in (("mhjcApiKey", "mhjcApiKeyPresent"), ("proxyPassword", "proxyPasswordSet")):
            self.assertFalse(
                re.search(rf"(?<![\w$.])d\.{field}\b", self.js),
                f"{field} must not be echoed back; use the {flag} flag",
            )
            self.assertIn(flag, self.js)

    def test_every_saved_setting_exists_in_the_env_map(self):
        from core.local_config import DEFAULTS
        unknown = sorted(self.saved - set(DEFAULTS) - {"proxy"})
        self.assertEqual(unknown, [], f"settings the page saves are not persisted: {unknown}")

class SavedSettingsCoverageTests(unittest.IsolatedAsyncioTestCase):
    """A setting that is stored but never consulted is dead configuration."""

    def setUp(self):
        self.defaults = set(local_config.DEFAULTS)
        self.env_map = local_config.ENV_MAP

    def test_every_setting_has_an_env_key(self):
        self.assertEqual(sorted(self.defaults - set(self.env_map)), [])

    async def test_every_setting_is_offered_to_the_ui_or_is_write_only(self):
        payload = await server.defaults()
        write_only = {"mhjcApiKey", "proxyPassword", "fiveSimApiKey", "s2AdminApiKey", "s2AdminPassword"}
        missing = sorted(self.defaults - set(payload) - write_only)
        self.assertEqual(missing, [], f"saved settings the UI can never read back: {missing}")

    def test_every_setting_is_consumed_by_the_cli_or_the_api(self):
        sources = "".join(
            Path(name).read_text(encoding="utf-8")
            for name in ("register.py", "webui/server.py")
        )
        unused = sorted(key for key in self.defaults if key not in sources)
        self.assertEqual(unused, [], f"saved settings nothing ever reads: {unused}")

    def test_every_flag_default_reads_its_saved_setting(self):
        """Flags must take their default from the saved setting, not a literal.

        Defaults are evaluated at import time in a fresh CLI process, so this is
        checked at the source level (see the other tests for the runtime effect).
        """
        source = Path("register.py").read_text(encoding="utf-8")
        expected = {
            "mhjcUsername": 'LOCAL_CONFIG.get("mhjcUsername", "")',
            "emailSource": 'LOCAL_CONFIG.get("emailSource") or "manual"',
            "codeSource": 'LOCAL_CONFIG.get("codeSource") or "manual"',
            "authMode": 'LOCAL_CONFIG.get("authMode") or "otp"',
        }
        for field, snippet in expected.items():
            self.assertIn(snippet, source, f"--{field} must use its saved setting")

class ProxyTestApiTests(unittest.IsolatedAsyncioTestCase):
    """The proxy test must exercise the same proxy a real run would use."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            patch.object(local_config, "ENV_PATH", self.env_path),
            patch.object(local_config, "LEGACY_JSON_PATH", self.root / "none.json"),
        ]
        for item in self._patches:
            item.start()
        for item in reversed(self._patches):
            self.addCleanup(item.stop)

    async def test_composes_the_proxy_from_the_form_fields(self):
        captured = {}

        def fake_test(proxy_url, **kwargs):
            captured["url"] = proxy_url
            captured["kwargs"] = kwargs
            return {"ok": True, "exitIp": "1.2.3.4", "verdict": {"kind": "residential"}}

        with patch.object(server.proxy_test_module, "test_proxy", side_effect=fake_test):
            response = await server.proxy_test_route(server.ProxyTestPayload(
                proxyScheme="socks5", proxyHost="proxy.example.com", proxyPort="1080",
                proxyUser="alice", proxyPass="s3cret", proxyInsecure=True,
            ))

        self.assertTrue(response["ok"])
        self.assertEqual(captured["url"], "socks5://alice:s3cret@proxy.example.com:1080")
        self.assertTrue(captured["kwargs"]["proxy_insecure"])

    async def test_a_blank_password_falls_back_to_the_saved_one(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(
                proxyUser="alice", proxyPassword="saved-secret"))
            captured = {}

            def fake_test(proxy_url, **_kwargs):
                captured["url"] = proxy_url
                return {"ok": True, "exitIp": "1.2.3.4"}

            with patch.object(server.proxy_test_module, "test_proxy", side_effect=fake_test):
                # password box left blank, as the UI does for an unchanged secret
                await server.proxy_test_route(server.ProxyTestPayload(
                    proxyHost="proxy.example.com", proxyPort="8080"))

        self.assertEqual(captured["url"], "http://alice:saved-secret@proxy.example.com:8080")

    async def test_accepts_a_full_proxy_url_when_no_host_is_given(self):
        captured = {}

        def fake_test(proxy_url, **_kwargs):
            captured["url"] = proxy_url
            return {"ok": True, "exitIp": "1.2.3.4"}

        with patch.object(server.proxy_test_module, "test_proxy", side_effect=fake_test):
            await server.proxy_test_route(server.ProxyTestPayload(proxy="http://127.0.0.1:7890"))

        self.assertEqual(captured["url"], "http://127.0.0.1:7890")

    async def test_rejects_an_empty_configuration(self):
        response = await server.proxy_test_route(server.ProxyTestPayload())
        self.assertEqual(response.status_code, 400)

    async def test_response_never_contains_the_proxy_password(self):
        def fake_test(proxy_url, **_kwargs):
            return {"ok": True, "proxy": "http://alice:***@proxy.example.com:8080",
                    "exitIp": "1.2.3.4", "notes": []}

        with patch.object(server.proxy_test_module, "test_proxy", side_effect=fake_test):
            response = await server.proxy_test_route(server.ProxyTestPayload(
                proxyHost="proxy.example.com", proxyPort="8080",
                proxyUser="alice", proxyPass="top-secret"))

        self.assertNotIn("top-secret", str(response))

class ProxySwitchApiTests(unittest.IsolatedAsyncioTestCase):
    """The run switch must turn the proxy off explicitly for the child process."""

    async def test_checked_switch_sends_no_disable_flag(self):
        args = server._build_args(server.BatchPayload(proxyHost="h", proxyPort="1", proxyEnabled=True))
        self.assertNotIn("--no-proxy", args)

    async def test_unchecked_switch_disables_the_proxy_for_the_run(self):
        args = server._build_args(server.BatchPayload(proxyHost="h", proxyPort="1", proxyEnabled=False))
        self.assertIn("--no-proxy", args)

        args = server._build_args(server.BatchPayload(proxyEnabled=False))
        self.assertIn("--no-proxy", args)

    async def test_relogin_respects_the_switch_too(self):
        args = server._build_relogin_args(
            server.ReloginPayload(codeSource="qq", proxyEnabled=False), account_id=7)
        self.assertIn("--no-proxy", args)
        args = server._build_relogin_args(
            server.ReloginPayload(codeSource="qq", proxyEnabled=True), account_id=7)
        self.assertNotIn("--no-proxy", args)

    async def test_defaults_report_the_switch_as_on_when_unset(self):
        import os
        from unittest.mock import patch as _patch
        from core import local_config as lc
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as tmp:
            with _patch.object(lc, "ENV_PATH", _Path(tmp) / ".env"), \
                 _patch.object(lc, "LEGACY_JSON_PATH", _Path(tmp) / "none.json"), \
                 _patch.dict(os.environ, {}, clear=True):
                payload = await server.defaults()
        self.assertTrue(payload["proxyEnabled"], "proxy must default to on")

    async def test_saved_off_switch_survives_and_is_reported(self):
        import os
        from unittest.mock import patch as _patch
        from core import local_config as lc
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as tmp:
            env = _Path(tmp) / ".env"
            with _patch.object(lc, "ENV_PATH", env), \
                 _patch.object(lc, "LEGACY_JSON_PATH", _Path(tmp) / "none.json"), \
                 _patch.dict(os.environ, {}, clear=True):
                await server.update_defaults(server.LocalConfigPayload(proxyEnabled=False))
                payload = await server.defaults()
                text = env.read_text(encoding="utf-8")
        self.assertFalse(payload["proxyEnabled"])
        self.assertIn("QR_PROXY_ENABLED=''", text)


class FiveSimProviderSettingsTests(unittest.IsolatedAsyncioTestCase):
    """已选供应商列表（顺序=优先级）必须能存进 .env、再原样读回来。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            patch.object(local_config, "ENV_PATH", self.env_path),
            patch.object(local_config, "LEGACY_JSON_PATH", self.root / "none.json"),
        ]
        for item in self._patches:
            item.start()
        for item in reversed(self._patches):
            self.addCleanup(item.stop)

    async def test_provider_list_round_trips_and_keeps_priority_order(self):
        picked = [
            server.FiveSimProvider(country="usa", operator="virtual21"),
            server.FiveSimProvider(country="poland", operator="virtual66"),
        ]
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(fiveSimProviders=picked))
            payload = await server.defaults()
            text = self.env_path.read_text(encoding="utf-8")

        self.assertIn("QR_FIVESIM_PROVIDERS=", text)
        self.assertEqual(payload["fiveSimProviders"],
                         [{"country": "usa", "operator": "virtual21"},
                          {"country": "poland", "operator": "virtual66"}])

    async def test_an_empty_list_clears_the_selection(self):
        with patch.dict(os.environ, {}, clear=True):
            await server.update_defaults(server.LocalConfigPayload(
                fiveSimProviders=[server.FiveSimProvider(country="usa", operator="virtual21")]))
            await server.update_defaults(server.LocalConfigPayload(fiveSimProviders=[]))
            payload = await server.defaults()
        self.assertEqual(payload["fiveSimProviders"], [])


class FiveSimPriorityBuyRouteTests(unittest.IsolatedAsyncioTestCase):
    """「按优先级买号」接口：失败要按顺序切到下一个供应商。"""

    def _order(self, country, operator):
        from core import num5sim
        return num5sim.ActivationOrder(id=5, phone="+485518000111", operator=operator,
                                       product="openai", price=0.2, status="PENDING",
                                       expires="", country=country)

    async def test_walks_the_list_and_reports_every_attempt(self):
        from core import num5sim
        calls: list[tuple[str, str]] = []

        def fake_buy(**kwargs):
            calls.append((kwargs["country"], kwargs["operator"]))
            if len(calls) == 1:
                raise num5sim.FiveSimNoFreePhonesError("no free phones")
            return self._order(kwargs["country"], kwargs["operator"])

        payload = server.FiveSimPriorityBuyPayload(
            apiKey="key", product="openai",
            providers=[server.FiveSimProvider(country="greece", operator="virtual34"),
                       server.FiveSimProvider(country="poland", operator="virtual66")],
        )
        with patch.object(num5sim, "buy_activation", side_effect=fake_buy):
            result = await server.five_sim_buy_priority(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("greece", "virtual34"), ("poland", "virtual66")])
        self.assertEqual(result["order"]["country"], "poland")
        self.assertEqual([a["ok"] for a in result["attempts"]], [False, True])

    async def test_all_failures_return_the_priority_list(self):
        from core import num5sim
        payload = server.FiveSimPriorityBuyPayload(
            apiKey="key",
            providers=[server.FiveSimProvider(country="poland", operator="virtual66")],
        )
        with patch.object(num5sim, "buy_activation",
                          side_effect=num5sim.FiveSimNoFreePhonesError("no free phones")):
            response = await server.five_sim_buy_priority(payload)

        self.assertEqual(response.status_code, 502)
        body = json.loads(response.body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["attempts"][0]["country"], "poland")

    async def test_an_empty_selection_is_rejected_before_spending_money(self):
        from core import num5sim
        with patch.object(num5sim, "buy_activation") as buy:
            response = await server.five_sim_buy_priority(
                server.FiveSimPriorityBuyPayload(apiKey="key", providers=[]))
        self.assertEqual(response.status_code, 400)
        buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
