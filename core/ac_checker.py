"""AC Checker API client."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from core.http_utils import open_url

DEFAULT_BASE_URL = "http://161.33.29.94:8787"
DEFAULT_PROMO_ID = "plus-1-month-free"
MAX_BATCH_SIZE = 50

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_RESULT_FIELDS = (
    "token_ok",
    "eligible",
    "reason",
    "coupon_state",
    "promo_id",
    "status",
    "email",
    "account_id",
    "plan_type",
    "jwt_expired",
    "jwt_exp_ms",
    "jwt_exp_in_sec",
)


def normalize_base_url(base_url: str = "") -> str:
    raw = (base_url or DEFAULT_BASE_URL).strip()
    if not raw:
        raise ValueError("AC Checker base URL 为空")
    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"AC Checker base URL 无效：{raw!r}")

    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1/check", "/api/check", "/api/v1/docs", "/api"):
        if path == suffix or path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
    return normalized.rstrip("/") or f"{parsed.scheme}://{parsed.netloc}"


def resolve_promo_id(promo_id: str = "") -> str:
    return (promo_id or DEFAULT_PROMO_ID).strip() or DEFAULT_PROMO_ID


def mask_token(token: str) -> str:
    raw = (token or "").strip()
    if len(raw) <= 20:
        return raw
    return f"{raw[:10]}...{raw[-6:]}"


def extract_account_token(account: dict[str, Any]) -> dict[str, str]:
    session = account.get("session") if isinstance(account.get("session"), dict) else {}
    session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
    token = str(session.get("accessToken") or "").strip()
    if token:
        return {
            "token": token,
            "tokenSource": "session.accessToken",
            "accountEmail": str(account.get("email") or session_user.get("email") or "").strip(),
        }

    codex = account.get("codexAuth") if isinstance(account.get("codexAuth"), dict) else {}
    token = str(codex.get("access_token") or "").strip()
    if token:
        return {
            "token": token,
            "tokenSource": "codexAuth.access_token",
            "accountEmail": str(account.get("email") or codex.get("email") or "").strip(),
        }

    raise ValueError("账号文件里没有可用 token（session.accessToken / codexAuth.access_token）")


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 20.0,
) -> Any:
    url = f"{normalize_base_url(base_url)}{path}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": _BROWSER_UA,
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, method=method.upper(), data=data, headers=headers)
    try:
        with open_url(req, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"AC Checker HTTP {e.code} {e.reason} {path}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"AC Checker 网络错误 {path}: {e.reason}") from e

    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise RuntimeError(f"AC Checker 返回空响应 {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AC Checker 返回非 JSON {path}: {text[:300]}") from e


def normalize_result(
    data: Any,
    *,
    promo_id: str = "",
    token_source: str = "",
    token: str = "",
) -> dict[str, Any]:
    row = data if isinstance(data, dict) else {}
    result = {field: row.get(field) for field in _RESULT_FIELDS}
    result["promo_id"] = result.get("promo_id") or row.get("promoId") or resolve_promo_id(promo_id)
    if token_source:
        result["tokenSource"] = token_source
    if token:
        result["tokenPreview"] = mask_token(token)
    return result


def normalize_batch_result(
    data: Any,
    *,
    promo_id: str = "",
    tokens: list[str] | None = None,
) -> dict[str, Any]:
    body = data if isinstance(data, dict) else {}
    results_raw = body.get("results") if isinstance(body.get("results"), list) else []
    normalized_results = [
        normalize_result(
            row,
            promo_id=promo_id,
            token=tokens[idx] if tokens and idx < len(tokens) else "",
        )
        for idx, row in enumerate(results_raw)
    ]
    return {
        "count": int(body.get("count") or len(normalized_results)),
        "promo_id": body.get("promo_id") or body.get("promoId") or resolve_promo_id(promo_id),
        "results": normalized_results,
    }


def healthz(
    *,
    base_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    data = _request_json(
        base_url,
        "/healthz",
        method="GET",
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        raise RuntimeError("AC Checker /healthz 返回格式异常")
    return data


def check_token(
    token: str,
    *,
    base_url: str = "",
    promo_id: str = "",
    proxy_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 20.0,
) -> dict[str, Any]:
    raw_token = (token or "").strip()
    if not raw_token:
        raise ValueError("AC token 为空")
    payload: dict[str, Any] = {
        "token": raw_token,
        "promoId": resolve_promo_id(promo_id),
    }
    if proxy_url.strip():
        payload["proxyUrl"] = proxy_url.strip()
    data = _request_json(
        base_url,
        "/api/v1/check",
        method="POST",
        body=payload,
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )
    return normalize_result(data, promo_id=payload["promoId"], token=raw_token)


def check_tokens(
    tokens: list[str],
    *,
    base_url: str = "",
    promo_id: str = "",
    proxy_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    cleaned = [str(token or "").strip() for token in tokens if str(token or "").strip()]
    if not cleaned:
        raise ValueError("没有可检查的 token")

    resolved_promo_id = resolve_promo_id(promo_id)
    merged_results: list[dict[str, Any]] = []
    for start in range(0, len(cleaned), MAX_BATCH_SIZE):
        chunk = cleaned[start:start + MAX_BATCH_SIZE]
        payload: dict[str, Any] = {
            "tokens": chunk,
            "promoId": resolved_promo_id,
        }
        if proxy_url.strip():
            payload["proxyUrl"] = proxy_url.strip()
        data = _request_json(
            base_url,
            "/api/v1/check",
            method="POST",
            body=payload,
            proxy=proxy,
            proxy_insecure=proxy_insecure,
            timeout=timeout,
        )
        batch = normalize_batch_result(data, promo_id=resolved_promo_id, tokens=chunk)
        merged_results.extend(batch["results"])

    return {
        "count": len(merged_results),
        "promo_id": resolved_promo_id,
        "results": merged_results,
    }
