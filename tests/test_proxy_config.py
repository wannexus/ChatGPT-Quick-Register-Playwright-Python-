from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from core import http_utils, local_config
import register


class BuildProxyUrlTests(unittest.TestCase):
    def test_builds_a_url_from_host_port_and_credentials(self):
        self.assertEqual(
            http_utils.build_proxy_url(host="proxy.example.com", port="8080", user="alice", password="s3cret"),
            "http://alice:s3cret@proxy.example.com:8080",
        )

    def test_returns_empty_without_a_host(self):
        self.assertEqual(http_utils.build_proxy_url(host="", port="8080", user="alice"), "")
        self.assertEqual(http_utils.build_proxy_url(host="   "), "")

    def test_percent_encodes_credentials_that_contain_url_delimiters(self):
        url = http_utils.build_proxy_url(
            host="proxy.example.com", port="1080", user="user@corp", password="p@ss:w/rd#1"
        )
        self.assertEqual(url, "http://user%40corp:p%40ss%3Aw%2Frd%231@proxy.example.com:1080")

    def test_omits_credentials_when_only_one_is_given(self):
        self.assertEqual(
            http_utils.build_proxy_url(host="h", port="1", user="alice"), "http://alice@h:1"
        )
        self.assertEqual(
            http_utils.build_proxy_url(host="h", port="1", password="pw"), "http://:pw@h:1"
        )

    def test_honours_the_scheme_and_tolerates_a_stray_host_scheme(self):
        self.assertEqual(
            http_utils.build_proxy_url(host="socks5://h", port="1080", scheme="socks5"),
            "socks5://h:1080",
        )
        self.assertEqual(http_utils.build_proxy_url(host="h", port="1", scheme="https"), "https://h:1")


class ProxyUrlRedactionTests(unittest.TestCase):
    def test_masks_the_password_and_keeps_the_rest_readable(self):
        self.assertEqual(
            http_utils.redact_proxy_url("http://alice:s3cret@proxy.example.com:8080"),
            "http://alice:***@proxy.example.com:8080",
        )

    def test_leaves_a_credential_free_url_alone(self):
        self.assertEqual(
            http_utils.redact_proxy_url("http://127.0.0.1:7890"), "http://127.0.0.1:7890"
        )

    def test_handles_empty_and_garbage_input(self):
        self.assertEqual(http_utils.redact_proxy_url(""), "")
        self.assertEqual(http_utils.redact_proxy_url("not a url"), "***")

    def test_parses_a_url_back_into_parts_for_the_ui(self):
        parts = http_utils.proxy_parts("http://alice:s3cret@proxy.example.com:8080")
        self.assertEqual(parts["scheme"], "http")
        self.assertEqual(parts["host"], "proxy.example.com")
        self.assertEqual(parts["port"], "8080")
        self.assertEqual(parts["user"], "alice")
        self.assertEqual(parts["password"], "s3cret")

    def test_proxy_parts_of_an_empty_value_are_blank(self):
        self.assertEqual(
            http_utils.proxy_parts(""),
            {"scheme": "", "host": "", "port": "", "user": "", "password": ""},
        )


class LocalConfigProxyTests(unittest.TestCase):
    """Proxy parts compose the effective proxy URL, now stored in .env."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.addCleanup(self._tmp.cleanup)

    def _config_with(self, values, env=None):
        import os
        from unittest.mock import patch as _patch
        from core import local_config
        with _patch.object(local_config, "ENV_PATH", self.root / ".env"), \
             _patch.object(local_config, "LEGACY_JSON_PATH", self.root / "none.json"), \
             _patch.dict(os.environ, env or {}, clear=True):
            local_config.save_config(values)
            return local_config.effective_config()

    def test_structured_proxy_parts_compose_the_effective_proxy(self):
        cfg = self._config_with({
            "proxyHost": "proxy.example.com", "proxyPort": "8080",
            "proxyUser": "alice", "proxyPassword": "s3cret",
        })
        self.assertEqual(cfg["proxy"], "http://alice:s3cret@proxy.example.com:8080")
        self.assertEqual(cfg["proxyHost"], "proxy.example.com")

    def test_full_url_still_works_when_no_parts_are_set(self):
        cfg = self._config_with({"proxy": "http://127.0.0.1:7890"})
        self.assertEqual(cfg["proxy"], "http://127.0.0.1:7890")

    def test_parts_override_a_stored_full_url(self):
        cfg = self._config_with({
            "proxy": "http://127.0.0.1:7890",
            "proxyScheme": "socks5", "proxyHost": "file-host",
            "proxyPort": "1080", "proxyUser": "bob", "proxyPassword": "pw",
        })
        self.assertEqual(cfg["proxy"], "socks5://bob:pw@file-host:1080")

    def test_environment_variables_are_used_when_nothing_is_saved(self):
        import os
        from unittest.mock import patch as _patch
        from core import local_config
        env = {"QR_PROXY_HOST": "env-host", "QR_PROXY_PORT": "3128", "QR_PROXY_USER": "u", "QR_PROXY_PASSWORD": "p"}
        with _patch.object(local_config, "ENV_PATH", self.root / "missing.env"), \
             _patch.object(local_config, "LEGACY_JSON_PATH", self.root / "none.json"), \
             _patch.object(local_config, "PROCESS_ENV_KEYS", frozenset(env)), \
             _patch.dict(os.environ, env, clear=True):
            self.assertEqual(local_config.effective_config()["proxy"], "http://u:p@env-host:3128")


class RegisterProxyArgTests(unittest.IsolatedAsyncioTestCase):
    """CLI proxy composition, isolated from whatever .env the machine has."""

    # Isolated from this machine's .env, but keeps the declared defaults.
    EMPTY_CONFIG = dict(local_config.DEFAULTS)

    def _parse(self, argv):
        # parse_args() reads the saved settings; pin them empty so the test only
        # exercises the flags it passes.
        with patch.object(register, "LOCAL_CONFIG", dict(self.EMPTY_CONFIG)):
            return register.parse_args(argv)

    def test_cli_parts_compose_the_proxy_used_by_every_consumer(self):
        args = self._parse([
            "--proxy-host", "proxy.example.com",
            "--proxy-port", "8080",
            "--proxy-user", "alice",
            "--proxy-pass", "s3cret",
        ])
        self.assertEqual(args.proxy, "http://alice:s3cret@proxy.example.com:8080")

    def test_full_url_flag_still_wins_when_no_parts_are_given(self):
        args = self._parse(["--proxy", "http://127.0.0.1:7890"])
        self.assertEqual(args.proxy, "http://127.0.0.1:7890")

    def test_parts_override_a_full_url(self):
        args = self._parse(["--proxy", "http://127.0.0.1:7890", "--proxy-host", "h", "--proxy-port", "1"])
        self.assertEqual(args.proxy, "http://h:1")

    def test_saved_settings_apply_when_no_flag_is_given(self):
        saved = dict(self.EMPTY_CONFIG)
        saved.update({"proxyScheme": "socks5", "proxyHost": "saved-host",
                      "proxyPort": "1080", "proxyUser": "u", "proxyPassword": "p"})
        with patch.object(register, "LOCAL_CONFIG", saved):
            args = register.parse_args(["--mode", "register"])
        self.assertEqual(args.proxy, "socks5://u:p@saved-host:1080")

    async def test_init_log_never_prints_the_proxy_password(self):
        args = self._parse([
            "--email-source", "manual", "--email", "person@example.com",
            "--proxy-host", "proxy.example.com", "--proxy-port", "8080",
            "--proxy-user", "alice", "--proxy-pass", "s3cret",
        ])
        output = io.StringIO()
        with redirect_stdout(output), \
             patch.object(register, "_open_browser_context",
                          AsyncMock(side_effect=RuntimeError("stop before any network use"))):
            with self.assertRaises(RuntimeError):
                await register.run_register(args)

        printed = output.getvalue()
        self.assertNotIn("s3cret", printed)
        self.assertIn("alice:***@proxy.example.com:8080", printed)

class ProxyEnabledByDefaultTests(unittest.TestCase):
    """The registration program uses the configured proxy unless it is turned off."""

    CONFIGURED = {
        **local_config.DEFAULTS,
        "proxyScheme": "http",
        "proxyHost": "proxy.example.com",
        "proxyPort": "8080",
        "proxyUser": "alice",
        "proxyPassword": "pw",
    }

    def _parse(self, argv, saved=None):
        with patch.object(register, "LOCAL_CONFIG", dict(saved if saved is not None else self.CONFIGURED)):
            return register.parse_args(argv)

    def test_proxy_is_used_with_no_flag_at_all(self):
        args = self._parse(["--mode", "register"])
        self.assertEqual(args.proxy, "http://alice:pw@proxy.example.com:8080")
        self.assertTrue(args.proxy_enabled)

    def test_no_proxy_flag_turns_it_off(self):
        args = self._parse(["--mode", "register", "--no-proxy"])
        self.assertEqual(args.proxy, "")
        self.assertFalse(args.proxy_enabled)

    def test_a_saved_off_switch_is_respected(self):
        saved = dict(self.CONFIGURED, proxyEnabled="")
        args = self._parse(["--mode", "register"], saved=saved)
        self.assertEqual(args.proxy, "")
        self.assertFalse(args.proxy_enabled)

    def test_proxy_flag_overrides_a_saved_off_switch(self):
        saved = dict(self.CONFIGURED, proxyEnabled="")
        args = self._parse(["--mode", "register", "--proxy", "http://127.0.0.1:7890"], saved=saved)
        self.assertEqual(args.proxy, "http://127.0.0.1:7890")
        self.assertTrue(args.proxy_enabled)

    def test_an_explicit_host_flag_also_turns_it_on(self):
        saved = {**local_config.DEFAULTS, "proxyEnabled": ""}
        args = self._parse(["--mode", "register", "--proxy-host", "h", "--proxy-port", "1"], saved=saved)
        self.assertEqual(args.proxy, "http://h:1")
        self.assertTrue(args.proxy_enabled)

    def test_enabled_but_unconfigured_is_reported_not_crashed(self):
        saved = {**local_config.DEFAULTS, "proxyHost": "", "proxyPort": "",
                 "proxyUser": "", "proxyPassword": "", "proxyScheme": "", "proxy": ""}
        args = self._parse(["--mode", "register"], saved=saved)
        self.assertTrue(args.proxy_enabled)
        self.assertEqual(args.proxy, "")


if __name__ == "__main__":
    unittest.main()
