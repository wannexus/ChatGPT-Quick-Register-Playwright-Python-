from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import local_config


class EnvConfigStoreTests(unittest.TestCase):
    """All settings live in .env; config.local.json is legacy import-only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text(
            "# local settings\nQR_MYSQL_HOST=127.0.0.1\nQR_MYSQL_DATABASE=quick_register\n",
            encoding="utf-8",
        )
        self.json_path = self.root / "config.local.json"
        self._patches = [
            patch.object(local_config, "ENV_PATH", self.env_path),
            patch.object(local_config, "LEGACY_JSON_PATH", self.json_path),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(self._tmp.cleanup)
        for item in reversed(self._patches):
            self.addCleanup(item.stop)

    def _env_text(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def test_save_config_writes_qr_keys_into_env(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"duckToken": "abc123", "qqUser": "me@qq.com"})
            cfg = local_config.effective_config()

        self.assertEqual(cfg["duckToken"], "abc123")
        self.assertEqual(cfg["qqUser"], "me@qq.com")
        self.assertIn("QR_DUCK_TOKEN", self._env_text())
        self.assertIn("QR_QQ_USER", self._env_text())

    def test_save_preserves_unrelated_env_lines_and_comments(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"qqPass": "authorization-code"})

        text = self._env_text()
        self.assertIn("# local settings", text)
        self.assertIn("QR_MYSQL_HOST=127.0.0.1", text)
        self.assertIn("QR_MYSQL_DATABASE=quick_register", text)
        self.assertIn("QR_QQ_PASS", text)

    def test_save_updates_the_running_process_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"pay153BaseUrl": "https://pay.example"})
            # a second read must see the new value without re-importing the module
            self.assertEqual(os.environ.get("QR_PAY153_BASE_URL"), "https://pay.example")
            self.assertEqual(local_config.effective_config()["pay153BaseUrl"], "https://pay.example")

    def test_save_is_partial_and_ignores_unknown_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"qqUser": "keep@qq.com", "notAThing": "nope"})
            local_config.save_config({"qqPass": "pw"})
            cfg = local_config.effective_config()

        self.assertEqual(cfg["qqUser"], "keep@qq.com")
        self.assertEqual(cfg["qqPass"], "pw")
        self.assertNotIn("notAThing", cfg)

    def test_values_with_spaces_and_hashes_survive_a_round_trip(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"proxyPassword": "p@ss w#rd:1/2"})
            self.assertEqual(local_config.effective_config()["proxyPassword"], "p@ss w#rd:1/2")

    def test_booleans_are_stored_as_one_or_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"headless": True, "stopOnError": True})
            local_config.save_config({"stopOnError": False})
            cfg = local_config.effective_config()
            text = self._env_text()

        self.assertIn("QR_HEADLESS='1'", text)
        self.assertNotIn("False", text)
        self.assertNotIn("True", text)
        self.assertEqual(cfg["headless"], "1")
        self.assertEqual(cfg["stopOnError"], "", "an unchecked box clears the flag")

    def test_unchanged_values_are_not_rewritten(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"qqUser": "me@qq.com"})
            first = self._env_text()
            local_config.save_config({"qqUser": "me@qq.com", "duckBaseUrl": "", "fiveSimApiKey": ""})
            second = self._env_text()

        self.assertEqual(first, second, "a full-form save must not add empty keys")
        self.assertNotIn("QR_DUCK_BASE_URL", second)

    def test_fivesim_options_round_trip_through_env(self):
        values = {
            "fiveSimApiKey": "secret-token",
            "fiveSimCountry": "vietnam",
            "fiveSimOperator": "virtual21",
            "fiveSimProduct": "openai",
            "fiveSimMaxPrice": "0.75",
            "fiveSimAcquirePriority": "price",
            "fiveSimCandidateLimit": 4,
        }
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config(values)
            cfg = local_config.effective_config()

        for key, value in values.items():
            self.assertEqual(cfg[key], str(value))
        self.assertIn("QR_FIVESIM_COUNTRY", self._env_text())
        self.assertIn("QR_FIVESIM_CANDIDATE_LIMIT", self._env_text())

    def test_save_never_creates_a_json_config(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"duckToken": "abc"})
        self.assertFalse(self.json_path.exists(), "settings must not be written to a JSON file")

    def test_mhjc_name_style_round_trips_through_env(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"mhjcNameStyle": "provider", "mhjcUsername": "wanted"})
            self.assertEqual(local_config.effective_config()["mhjcNameStyle"], "provider")

            local_config.save_config({"mhjcNameStyle": "name", "mhjcUsername": "wanted"})
            cfg = local_config.effective_config()

        self.assertEqual(cfg["mhjcNameStyle"], "name")
        self.assertEqual(cfg["mhjcUsername"], "wanted")
        self.assertIn("QR_MHJC_NAME_STYLE", self._env_text())

    def test_default_name_style_is_person_names(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = local_config.effective_config()
        self.assertEqual(cfg["mhjcNameStyle"], "name")

    def test_env_file_is_private(self):
        with patch.dict(os.environ, {}, clear=True):
            local_config.save_config({"duckToken": "abc"})
        mode = stat.S_IMODE(self.env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_legacy_json_is_imported_once_when_the_key_is_absent(self):
        self.json_path.write_text(
            json.dumps({"duckToken": "legacy-token", "acCheckerPromoId": "legacy-promo"}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            imported = local_config.migrate_legacy_json_config()
            cfg = local_config.effective_config()

        self.assertEqual(sorted(imported), ["acCheckerPromoId", "duckToken"])
        self.assertEqual(cfg["duckToken"], "legacy-token")
        self.assertEqual(cfg["acCheckerPromoId"], "legacy-promo")
        # the legacy file itself is left untouched for the operator to remove
        self.assertTrue(self.json_path.exists())

    def test_legacy_json_never_overwrites_a_value_already_in_env(self):
        self.env_path.write_text("QR_DUCK_TOKEN='from-env'\n", encoding="utf-8")
        self.json_path.write_text(json.dumps({"duckToken": "stale-json"}), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            imported = local_config.migrate_legacy_json_config()
            cfg = local_config.effective_config()

        self.assertEqual(cfg["duckToken"], "from-env")
        self.assertEqual(imported, [])
        self.assertNotIn("stale-json", self._env_text())

    def test_migration_is_a_no_op_without_a_legacy_file(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_config.migrate_legacy_json_config(), [])

    def test_an_explicit_save_takes_effect_even_if_the_process_already_had_the_key(self):
        """A UI save must not be silently ignored by an earlier export in this process.

        On the next start `load_dotenv(override=False)` still lets a real shell
        export win, which keeps the documented "环境变量优先" behaviour.
        """
        with patch.dict(os.environ, {"QR_DUCK_TOKEN": "shell-value"}, clear=True):
            local_config.save_config({"duckToken": "saved-value"})
            self.assertEqual(local_config.effective_config()["duckToken"], "saved-value")
        self.assertIn("QR_DUCK_TOKEN", self._env_text())


if __name__ == "__main__":
    unittest.main()
