"""Generate a private @duck.com address by calling the DuckDuckGo Email
Protection API directly.

This is the same endpoint the official DDG browser extension hits when you
click "Generate Private Duck Address". You need an `auth_token` from your DDG
account; it's stored by the extension at runtime.

Reference shape (from a typical extension config dump):

    "duckduckgo": {
        "auth_token": "...long-token...",
        "base_url": "https://quack.duckduckgo.com"
    }
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://quack.duckduckgo.com"
ADDRESSES_PATH = "/api/email/addresses"


def generate_private_address(
    token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 15.0,
    proxy: str | None = None,
) -> str:
    """Call POST /api/email/addresses and return the freshly generated full
    `<random>@duck.com` address."""

    if not token:
        raise ValueError("Duck API token 为空")

    url = f"{base_url.rstrip('/')}{ADDRESSES_PATH}"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (DuckDuckGo Email Protection)",
        },
        data=b"",  # the API needs a POST body, even if empty
    )

    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        # Honour HTTP(S)_PROXY env vars if user set them.
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Duck API HTTP {e.code} {e.reason}: {raw[:300]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Duck API 网络错误：{e.reason}") from e

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Duck API 返回非 JSON：{body[:300]!r}"
        ) from e

    address = (data.get("address") or "").strip()
    if not address:
        raise RuntimeError(f"Duck API 未返回 address，原始响应：{data!r}")

    full = f"{address}@duck.com"
    print(f"[duck-api] new address: {full}")
    return full


def token_from_args(token_arg: str | None) -> str:
    """Resolve the token from --duck-token, env, or a fallback file. Empty if
    none configured."""
    if token_arg:
        return token_arg.strip()
    env_value = os.environ.get("QR_DUCK_TOKEN", "").strip()
    if env_value:
        return env_value
    return ""
