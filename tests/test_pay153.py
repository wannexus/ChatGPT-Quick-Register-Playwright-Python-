from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from core import pay153


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class Pay153ClientTests(unittest.TestCase):
    def test_normalize_base_url_strips_known_endpoint(self):
        self.assertEqual(
            pay153.normalize_base_url("https://pay.153.ink/api/checkout"),
            "https://pay.153.ink",
        )

    def test_normalize_base_url_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            pay153.normalize_base_url("file:///tmp/service")

    def test_build_checkout_payload_normalizes_and_clamps(self):
        payload = pay153.build_checkout_payload(
            " token-value ",
            {
                "plan": "PLUS",
                "link_type": "pix",
                "country": "br",
                "currency": "brl",
                "entry_proxies": [" proxy-a ", ""],
                "retry_count": 99,
                "pix_auto_kind": "mixed",
            },
        )
        self.assertEqual(payload["token"], "token-value")
        self.assertEqual(payload["plan"], "plus")
        self.assertEqual(payload["country"], "BR")
        self.assertEqual(payload["currency"], "BRL")
        self.assertEqual(payload["entry_proxies"], ["proxy-a"])
        self.assertEqual(payload["retry_count"], 50)
        self.assertEqual(payload["pix_auto_kind"], "mixed")

    def test_create_checkout_matches_public_frontend_contract(self):
        captured = {}

        def fake_open(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            return _Response(json.dumps({"job_id": "job-123", "queue_position": 2}).encode())

        with patch("core.pay153.open_url", side_effect=fake_open):
            result = pay153.create_checkout(
                "secret-token",
                {"plan": "plus", "link_type": "hosted", "entry_proxies": ["proxy-a"]},
                proxy="http://127.0.0.1:7890",
            )

        request = captured["request"]
        body = json.loads(request.data.decode())
        self.assertEqual(request.full_url, "https://pay.153.ink/api/checkout")
        self.assertEqual(request.method, "POST")
        self.assertEqual(body["token"], "secret-token")
        self.assertEqual(body["link_type"], "hosted")
        self.assertEqual(result["job_id"], "job-123")
        self.assertEqual(captured["kwargs"]["proxy"], "http://127.0.0.1:7890")

    def test_progress_url_encodes_job_id(self):
        captured = {}

        def fake_open(request, **kwargs):
            captured["url"] = request.full_url
            return _Response(b'{"status":"running","percent":20}')

        with patch("core.pay153.open_url", side_effect=fake_open):
            result = pay153.get_progress("job id/1")

        self.assertIn("job_id=job+id%2F1", captured["url"])
        self.assertEqual(result["status"], "running")


if __name__ == "__main__":
    unittest.main()
