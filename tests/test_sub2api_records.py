from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from core import sub2api


def _records():
    return [
        {"id": 1, "email": "codex@example.com", "codexAuth": {"access_token": "codex-token", "refresh_token": "r"}},
        {"id": 2, "email": "session@example.com", "session": {"accessToken": "session-token"}},
        {"id": 3, "email": "empty@example.com", "session": {}},
    ]


class Sub2ApiRecordContractTests(unittest.TestCase):
    def test_collection_consumes_records_and_never_scans_a_directory(self):
        with patch.object(Path, "glob", side_effect=AssertionError("SUB2API must not enumerate output/*.json")):
            rows = sub2api.collect_accounts_from_dir(_records(), skip_empty=True, require_codex=False)

        self.assertEqual([label for label, _account in rows], ["codex@example.com", "session@example.com"])
        self.assertEqual([account["id"] for _label, account in rows], [1, 2])

    def test_require_codex_keeps_only_records_with_complete_codex_auth(self):
        rows = sub2api.collect_accounts_from_dir(_records(), skip_empty=False, require_codex=True)
        self.assertEqual([label for label, _account in rows], ["codex@example.com"])

    def test_export_bundle_is_built_from_records(self):
        bundle = sub2api.build_export_bundle(_records(), require_codex=True, privacy_mode="training_off")
        self.assertEqual(bundle["_meta"]["count"], 1)
        self.assertEqual(bundle["accounts"][0]["name"], "codex@example.com")
        self.assertEqual(bundle["accounts"][0]["credentials"]["access_token"], "codex-token")

    def test_export_bundle_reports_skipped_records_by_email(self):
        broken = [{"id": 4, "email": "broken@example.com", "codexAuth": {"refresh_token": "only"}}]
        bundle = sub2api.build_export_bundle(broken, skip_empty=False, require_codex=False)
        self.assertEqual(bundle["_meta"]["count"], 0)
        self.assertEqual(bundle["_meta"]["skipped"][0]["email"], "broken@example.com")


if __name__ == "__main__":
    unittest.main()
