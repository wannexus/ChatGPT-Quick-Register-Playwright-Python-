"""Client for the public PAY.153 checkout task API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from core.http_utils import open_url


DEFAULT_BASE_URL = "https://pay.153.ink"
ALLOWED_PLANS = frozenset({"plus", "pro", "team", "codex_low"})
ALLOWED_LINK_TYPES = frozenset(
    {"hosted", "ph_short", "paypal", "ideal", "upi", "pix", "momo", "gcash", "kakao"}
)
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})
MAX_PROXY_POOL_SIZE = 500

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def normalize_base_url(base_url: str = "") -> str:
    raw = (base_url or DEFAULT_BASE_URL).strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"PAY.153 base URL 无效：{raw!r}")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/checkout-progress", "/api/checkout-cancel", "/api/checkout"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def _clean_proxies(values: list[str] | None, *, field: str) -> list[str]:
    cleaned = [str(value or "").strip() for value in (values or []) if str(value or "").strip()]
    if len(cleaned) > MAX_PROXY_POOL_SIZE:
        raise ValueError(f"{field} 最多 {MAX_PROXY_POOL_SIZE} 条")
    return cleaned


def build_checkout_payload(token: str, options: dict[str, Any]) -> dict[str, Any]:
    raw_token = str(token or "").strip()
    if not raw_token:
        raise ValueError("账号 token 为空")

    plan = str(options.get("plan") or "plus").strip().lower()
    link_type = str(options.get("link_type") or "hosted").strip().lower()
    if plan not in ALLOWED_PLANS:
        raise ValueError(f"不支持的订阅类型：{plan}")
    if link_type not in ALLOWED_LINK_TYPES:
        raise ValueError(f"不支持的支付路径：{link_type}")

    retry_count = max(1, min(50, int(options.get("retry_count") or 10)))
    seat_quantity = max(2, min(999, int(options.get("seat_quantity") or 5)))
    credit_quantity = max(1, min(100000, int(options.get("credit_quantity") or 13)))
    use_promo = bool(options.get("use_promo")) and plan == "plus"

    return {
        "token": raw_token,
        "plan": plan,
        "link_type": link_type,
        "country": str(options.get("country") or "US").strip().upper(),
        "currency": str(options.get("currency") or "USD").strip().upper(),
        "entry_proxies": _clean_proxies(options.get("entry_proxies"), field="代理池 1"),
        "exit_proxies": _clean_proxies(options.get("exit_proxies"), field="代理池 2"),
        "retry_count": retry_count,
        "use_sen": bool(options.get("use_sen", True)),
        "use_so": bool(options.get("use_so", True)),
        "use_promo": use_promo,
        "promo_campaign": str(options.get("promo_campaign") or "").strip() if use_promo else "",
        "promo_code": str(options.get("promo_code") or "").strip() if plan == "team" else "",
        "workspace_name": str(options.get("workspace_name") or "Codex Workspace").strip()[:80],
        "workspace_id": str(options.get("workspace_id") or "").strip()[:120],
        "seat_quantity": seat_quantity,
        "price_interval": "year" if str(options.get("price_interval")) == "year" else "month",
        "credit_quantity": credit_quantity,
        "ideal_bank": "",
        "pix_tax_id": str(options.get("pix_tax_id") or "").strip() if link_type == "pix" else "",
        "pix_auto_kind": (
            str(options.get("pix_auto_kind") or "cpf")
            if str(options.get("pix_auto_kind") or "cpf") in {"cpf", "mixed", "cnpj"}
            else "cpf"
        ),
    }


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    query: dict[str, str] | None = None,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = f"{normalize_base_url(base_url)}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": _BROWSER_UA}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, method=method.upper(), data=data, headers=headers)
    try:
        with open_url(request, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        try:
            parsed = json.loads(detail)
            detail = str(parsed.get("error") or parsed.get("detail") or detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RuntimeError(f"PAY.153 HTTP {exc.code} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PAY.153 网络错误 {path}: {exc.reason}") from exc

    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise RuntimeError(f"PAY.153 返回空响应 {path}")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PAY.153 返回非 JSON {path}: {text[:300]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"PAY.153 返回格式异常 {path}")
    return result


def create_checkout(
    token: str,
    options: dict[str, Any],
    *,
    base_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 45.0,
) -> dict[str, Any]:
    result = _request_json(
        base_url,
        "/api/checkout",
        method="POST",
        body=build_checkout_payload(token, options),
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )
    if not str(result.get("job_id") or "").strip():
        raise RuntimeError("PAY.153 创建任务成功但未返回 job_id")
    return result


def get_progress(
    job_id: str,
    *,
    base_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 20.0,
) -> dict[str, Any]:
    raw_job_id = str(job_id or "").strip()
    if not raw_job_id or len(raw_job_id) > 200:
        raise ValueError("PAY.153 job_id 无效")
    return _request_json(
        base_url,
        "/api/checkout-progress",
        query={"job_id": raw_job_id},
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )


def cancel_checkout(
    job_id: str,
    *,
    base_url: str = "",
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 20.0,
) -> dict[str, Any]:
    raw_job_id = str(job_id or "").strip()
    if not raw_job_id or len(raw_job_id) > 200:
        raise ValueError("PAY.153 job_id 无效")
    return _request_json(
        base_url,
        "/api/checkout-cancel",
        method="POST",
        body={"job_id": raw_job_id},
        proxy=proxy,
        proxy_insecure=proxy_insecure,
        timeout=timeout,
    )
