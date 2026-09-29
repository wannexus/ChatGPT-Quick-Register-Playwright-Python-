from __future__ import annotations

import json
import unittest
from typing import Any, Callable, Dict, List
from unittest.mock import patch

from core import sub2api

ACCOUNT = {
    "email": "person@example.com",
    "session": {"accessToken": "web-token", "user": {"email": "person@example.com"}},
    "codexAuth": {
        "access_token": "codex-access",
        "refresh_token": "codex-refresh",
        "id_token": "codex-id",
        "email": "person@example.com",
    },
}


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_open_url(responder: Callable[[str], Any]):
    """Record every outgoing request and answer with canned SUB2API JSON."""
    seen: List[Any] = []

    def fake_open(req, **_kwargs):
        seen.append(req)
        return _FakeResponse(json.dumps(responder(req.full_url)))

    return seen, fake_open


def _lower_headers(req) -> Dict[str, str]:
    return {key.lower(): value for key, value in req.headers.items()}


def _responder(routes: Dict[str, Any]):
    def respond(url: str):
        for fragment, payload in routes.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unexpected SUB2API request: {url}")
    return respond


class AdminKeyHeaderTests(unittest.TestCase):
    def test_admin_key_is_sent_as_x_api_key_without_any_login_call(self):
        seen, fake = _fake_open_url(_responder({
            "/api/v1/admin/groups/all": {"code": 0, "data": [{"id": 3, "name": "codex"}]},
            "/api/v1/admin/accounts": {"code": 0, "data": {"id": 99}},
        }))
        target = sub2api.Sub2ApiTarget(
            base_url="https://sub2api.example.com",
            admin_api_key="admin-key-123",
            group_name="codex",
        )

        with patch.object(sub2api, "open_url", fake):
            result = sub2api.push_accounts([ACCOUNT], target, skip_empty=False, require_codex=True)

        self.assertEqual(len(result.pushed), 1)
        urls = [req.full_url for req in seen]
        self.assertFalse(any("/auth/login" in url for url in urls),
                         "an admin API key must not be exchanged for a JWT first")
        for req in seen:
            headers = _lower_headers(req)
            self.assertEqual(headers.get("x-api-key"), "admin-key-123")
            self.assertNotIn("authorization", headers)

    def test_blank_key_is_not_treated_as_configured(self):
        target = sub2api.Sub2ApiTarget(base_url="https://sub2api.example.com", admin_api_key="   ")
        with self.assertRaises(RuntimeError) as ctx:
            sub2api.auth_headers(target)
        self.assertIn("API Key", str(ctx.exception))

    def test_missing_credentials_explain_both_supported_modes(self):
        target = sub2api.Sub2ApiTarget(base_url="https://sub2api.example.com")
        with self.assertRaises(RuntimeError) as ctx:
            sub2api.auth_headers(target)
        message = str(ctx.exception)
        self.assertIn("API Key", message)
        self.assertIn("x-api-key", message)

    def test_jwt_fallback_still_works_when_no_key_is_configured(self):
        seen, fake = _fake_open_url(_responder({
            "/api/v1/auth/login": {"code": 0, "data": {"access_token": "jwt-token"}},
            "/api/v1/admin/groups/all": {"code": 0, "data": [{"id": 5, "name": "codex"}]},
        }))
        target = sub2api.Sub2ApiTarget(
            base_url="https://sub2api.example.com",
            email="admin@example.com",
            password="admin-password",
            group_name="codex",
        )

        with patch.object(sub2api, "open_url", fake):
            sub2api.list_groups(target, sub2api.auth_headers(target))

        urls = [req.full_url for req in seen]
        self.assertIn("https://sub2api.example.com/api/v1/auth/login", urls)
        self.assertNotIn("x-api-key", _lower_headers(seen[-1]))
        self.assertEqual(_lower_headers(seen[-1]).get("authorization"), "Bearer jwt-token")

    def test_an_invalid_admin_key_tells_the_operator_to_regenerate_it(self):
        _, fake = _fake_open_url(_responder({
            "/api/v1/admin/groups/all": {"code": 401, "message": "INVALID_ADMIN_KEY"},
        }))
        target = sub2api.Sub2ApiTarget(base_url="https://sub2api.example.com", admin_api_key="stale-key")

        with patch.object(sub2api, "open_url", fake):
            with self.assertRaises(RuntimeError) as ctx:
                sub2api.list_groups(target, sub2api.auth_headers(target))

        message = str(ctx.exception)
        self.assertIn("INVALID_ADMIN_KEY", message)
        self.assertIn("重新生成", message)

    def test_progress_reports_the_auth_mode_without_leaking_the_key(self):
        _, fake = _fake_open_url(_responder({
            "/api/v1/admin/groups/all": {"code": 0, "data": [{"id": 3, "name": "codex"}]},
            "/api/v1/admin/accounts": {"code": 0, "data": {"id": 99}},
        }))
        target = sub2api.Sub2ApiTarget(
            base_url="https://sub2api.example.com", admin_api_key="admin-key-123", group_name="codex",
        )
        stages: List[tuple] = []

        with patch.object(sub2api, "open_url", fake):
            sub2api.push_accounts(
                [ACCOUNT], target, skip_empty=False, require_codex=True,
                on_progress=lambda stage, info: stages.append((stage, info)),
            )

        names = [stage for stage, _ in stages]
        self.assertIn("auth", names)
        self.assertIn("auth_ok", names)
        self.assertNotIn("login", names)
        self.assertNotIn("admin-key-123", json.dumps(stages))


if __name__ == "__main__":
    unittest.main()
