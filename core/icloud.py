"""Generate iCloud Hide My Email aliases via the signed-in iCloud web session.

This mirrors the extension flow from codex-oauth-automation-extension-Ultra8.0:
validate the iCloud web session, resolve the `premiummailsettings` service, then
call the Hide My Email generate/reserve endpoints from an iCloud Mail page
context so browser cookies are used naturally.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout


SETUP_URLS = [
    "https://setup.icloud.com/setup/ws/1",
    "https://setup.icloud.com.cn/setup/ws/1",
]
CLIENT_BUILD_NUMBER = "2512Hotfix21"
CKJS_BUILD_VERSION = "2310ProjectDev27"
CKJS_VERSION = "2.6.4"
REQUEST_TIMEOUT_MS = 15_000
LIST_MAX_ATTEMPTS = 3
WRITE_MAX_ATTEMPTS = 2
RETRY_DELAYS_SECONDS = [1.0, 2.5, 5.0]


class IcloudRequestError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def normalize_icloud_host(raw_host: str = "") -> str:
    host = str(raw_host or "").strip().lower()
    if not host:
        return ""
    if "://" in host:
        try:
            host = urlparse(host).hostname or host
        except Exception:
            pass
    host = host.split("/")[0].split("?")[0].split("#")[0]
    host = re.sub(r":\d+$", "", host).rstrip(".")
    if host == "icloud.com" or host.endswith(".icloud.com"):
        return "icloud.com"
    if host == "icloud.com.cn" or host.endswith(".icloud.com.cn"):
        return "icloud.com.cn"
    return ""


def setup_url_for_host(host: str) -> str:
    normalized = normalize_icloud_host(host)
    if normalized == "icloud.com.cn":
        return "https://setup.icloud.com.cn/setup/ws/1"
    if normalized == "icloud.com":
        return "https://setup.icloud.com/setup/ws/1"
    return ""


def mail_url_for_host(host: str) -> str:
    normalized = normalize_icloud_host(host)
    if normalized == "icloud.com.cn":
        return "https://www.icloud.com.cn/mail/"
    return "https://www.icloud.com/mail/"


def preferred_setup_urls(host_preference: str = "auto") -> list[str]:
    forced = setup_url_for_host(host_preference)
    if forced:
        return [forced]
    return list(SETUP_URLS)


def _strip_default_port(raw_url: str = "") -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return value.rstrip("/")

    default_port = (
        (parsed.scheme == "https" and port == 443)
        or (parsed.scheme == "http" and port == 80)
    )
    if not default_port:
        return value.rstrip("/")

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunparse(parsed._replace(netloc=host)).rstrip("/")


def normalize_service_url(raw_url: str = "") -> str:
    return _strip_default_port(raw_url)


def is_maildomainws_host(host: str = "") -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    return normalized.endswith("maildomainws.icloud.com") or normalized.endswith("maildomainws.icloud.com.cn")


def append_client_query_params(
    raw_url: str,
    *,
    client_id: str = "",
    dsid: str = "",
    include_ckjs: bool = True,
) -> str:
    normalized_url = _strip_default_port(raw_url)
    parsed = urlparse(normalized_url)
    if not is_maildomainws_host(parsed.hostname or ""):
        return normalized_url

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.setdefault("clientBuildNumber", CLIENT_BUILD_NUMBER)
    params.setdefault("clientMasteringNumber", CLIENT_BUILD_NUMBER)
    if include_ckjs:
        params.setdefault("ckjsBuildVersion", CKJS_BUILD_VERSION)
        params.setdefault("ckjsVersion", CKJS_VERSION)
    params.setdefault("clientId", client_id)
    params.setdefault("dsid", dsid)
    return urlunparse(parsed._replace(query=urlencode(params)))


def _find_alias_array(node: Any, depth: int = 0) -> list[Any] | None:
    if node is None or depth > 4:
        return None
    if isinstance(node, list):
        return node if any(isinstance(item, dict) for item in node) else None
    if not isinstance(node, dict):
        return None

    for key in ("hmeEmails", "hmeEmailList", "hmeList", "hmes", "aliases", "items"):
        value = node.get(key)
        if isinstance(value, list):
            return value
    for value in node.values():
        found = _find_alias_array(value, depth + 1)
        if found is not None:
            return found
    return None


def _normalize_alias_record(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    metadata = raw.get("metaData") if isinstance(raw.get("metaData"), dict) else {}
    email = str(
        raw.get("hme")
        or raw.get("email")
        or raw.get("alias")
        or raw.get("address")
        or metadata.get("hme")
        or ""
    ).strip().lower()
    if not email or "@" not in email:
        return None

    state = str(raw.get("state") or raw.get("status") or "").strip().lower()
    return {
        "anonymousId": str(raw.get("anonymousId") or raw.get("id") or "").strip(),
        "email": email,
        "label": str(raw.get("label") or metadata.get("label") or "").strip(),
        "note": str(raw.get("note") or metadata.get("note") or "").strip(),
        "state": state,
        "active": raw.get("active") is not False
        and raw.get("isActive") is not False
        and state not in {"inactive", "deleted"},
    }


def normalize_alias_list(response: Any) -> list[dict[str, Any]]:
    aliases = _find_alias_array(response) or []
    normalized = [_normalize_alias_record(item) for item in aliases]
    return sorted(
        [item for item in normalized if item],
        key=lambda item: (not item["active"], item["email"]),
    )


def pick_reusable_alias(
    aliases: list[dict[str, Any]],
    *,
    used_emails: set[str] | None = None,
) -> dict[str, Any] | None:
    used = {str(item or "").strip().lower() for item in (used_emails or set()) if str(item or "").strip()}
    for alias in aliases:
        email = str(alias.get("email") or "").strip().lower()
        if alias.get("active") and email and email not in used:
            return alias
    return None


def find_alias_by_email(aliases: list[dict[str, Any]], email: str) -> dict[str, Any] | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    for alias in aliases:
        if str(alias.get("email") or "").strip().lower() == normalized:
            return alias
    return None


def alias_label() -> str:
    return f"ChatGPT Quick Register {datetime.now().strftime('%Y-%m-%d')}"


def _extract_generated_alias(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip().lower()
    if isinstance(raw, dict):
        return str(
            raw.get("hme")
            or raw.get("email")
            or raw.get("alias")
            or raw.get("address")
            or ""
        ).strip().lower()
    return ""


def _is_retryable_error(error: Exception) -> bool:
    message = str(error).lower()
    status = int(getattr(error, "status", 0) or 0)
    if status in {401, 403, 408, 409, 421, 429, 500, 502, 503, 504}:
        return True
    return any(
        hint in message
        for hint in (
            "status 401",
            "status 403",
            "status 408",
            "status 409",
            "status 421",
            "status 429",
            "status 500",
            "status 502",
            "status 503",
            "status 504",
            "failed to fetch",
            "networkerror",
            "network error",
            "fetch failed",
            "timed out",
            "timeout",
            "abort",
        )
    )


async def _ensure_mail_page(
    context: BrowserContext,
    *,
    host_preference: str = "auto",
    nav_timeout_ms: int = 60_000,
) -> Page:
    host = normalize_icloud_host(host_preference) or "icloud.com"
    target_url = mail_url_for_host(host)
    for page in context.pages:
        try:
            current_host = normalize_icloud_host(urlparse(page.url).hostname or "")
        except Exception:
            current_host = ""
        if current_host and current_host == host:
            if "/mail" not in urlparse(page.url).path.lower():
                await page.goto(target_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            return page

    page = await context.new_page()
    print(f"[icloud] opening {target_url}")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    return page


async def _request_in_page(
    page: Page,
    method: str,
    url: str,
    *,
    data: Any = None,
    client_id: str = "",
    dsid: str = "",
    include_ckjs: bool = True,
    timeout_ms: int = REQUEST_TIMEOUT_MS,
) -> Any:
    request_url = append_client_query_params(url, client_id=client_id, dsid=dsid, include_ckjs=include_ckjs)
    content_type = ""
    if data is not None:
        content_type = (
            "text/plain;charset=UTF-8"
            if is_maildomainws_host(urlparse(request_url).hostname or "")
            else "application/json"
        )

    result = await page.evaluate(
        """
        async ({ method, url, hasData, data, contentType, timeoutMs }) => {
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
          try {
            const headers = hasData ? { "Content-Type": contentType || "application/json" } : undefined;
            const response = await fetch(url, {
              method,
              credentials: "include",
              cache: "no-store",
              mode: "cors",
              headers,
              body: hasData ? JSON.stringify(data) : undefined,
              signal: controller.signal,
            });
            const text = await response.text();
            return { ok: response.ok, status: response.status, text, error: "" };
          } catch (error) {
            return {
              ok: false,
              status: 0,
              text: "",
              error: String((error && error.message) || error || "unknown error"),
            };
          } finally {
            clearTimeout(timeoutId);
          }
        }
        """,
        {
            "method": method,
            "url": request_url,
            "hasData": data is not None,
            "data": data,
            "contentType": content_type,
            "timeoutMs": timeout_ms,
        },
    )

    if not result.get("ok"):
        status = int(result.get("status") or 0)
        body = str(result.get("text") or "").strip()[:240]
        detail = body or str(result.get("error") or "request failed")
        raise IcloudRequestError(
            f"iCloud request failed: {method} {request_url} status {status}: {detail}",
            status=status,
            url=request_url,
        )

    text = str(result.get("text") or "")
    if not text.strip():
        return {}
    try:
        return await page.evaluate("text => JSON.parse(text)", text)
    except Exception as exc:
        raise RuntimeError(f"iCloud returned invalid JSON: {exc}") from exc


async def _request_with_retry(
    page: Page,
    method: str,
    url: str,
    *,
    data: Any = None,
    client_id: str = "",
    dsid: str = "",
    include_ckjs: bool = True,
    max_attempts: int = 1,
) -> Any:
    last_error: Exception | None = None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            return await _request_in_page(
                page,
                method,
                url,
                data=data,
                client_id=client_id,
                dsid=dsid,
                include_ckjs=include_ckjs,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= attempts or not _is_retryable_error(exc):
                raise
            delay = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            print(f"[icloud] request retry {attempt}/{attempts}: {exc}; sleeping {delay}s")
            await asyncio.sleep(delay)
    raise last_error or RuntimeError("iCloud request failed")


async def _resolve_service(
    page: Page,
    *,
    host_preference: str = "auto",
) -> tuple[str, str, dict[str, Any]]:
    errors: list[str] = []
    for setup_url in preferred_setup_urls(host_preference):
        try:
            data = await _request_with_retry(page, "POST", f"{setup_url}/validate", max_attempts=2)
            service_url = normalize_service_url(
                (((data or {}).get("webservices") or {}).get("premiummailsettings") or {}).get("url") or ""
            )
            if service_url:
                ds_info = data.get("dsInfo") if isinstance(data.get("dsInfo"), dict) else {}
                return service_url, setup_url, ds_info
            raise RuntimeError("Hide My Email service URL missing")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{urlparse(setup_url).hostname}: {exc}")
    raise RuntimeError(
        "Could not validate iCloud session. 请先在当前持久化浏览器里登录 iCloud，并确认已开通 iCloud+ / 隐藏邮件地址。"
        + (" " + " | ".join(errors) if errors else "")
    )


async def _resolve_service_with_login_wait(
    page: Page,
    *,
    host_preference: str = "auto",
    login_timeout_seconds: int = 300,
) -> tuple[str, str, dict[str, Any]]:
    try:
        return await _resolve_service(page, host_preference=host_preference)
    except Exception as first_error:  # noqa: BLE001
        timeout = max(0, int(login_timeout_seconds or 0))
        if timeout <= 0:
            raise

        print(
            "[icloud] iCloud session is not ready. "
            f"Please finish signing in on the opened iCloud page; waiting up to {timeout}s..."
        )
        deadline = asyncio.get_event_loop().time() + timeout
        last_error: Exception = first_error
        next_log_at = 0.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            try:
                return await _resolve_service(page, host_preference=host_preference)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                now = asyncio.get_event_loop().time()
                if now >= next_log_at:
                    remaining = max(0, int(deadline - now))
                    print(f"[icloud] still waiting for iCloud login/session... {remaining}s left")
                    next_log_at = now + 30

        raise RuntimeError(
            f"iCloud 登录等待超时，仍无法校验 Hide My Email 会话：{last_error}"
        ) from last_error


async def _list_aliases(page: Page, service_url: str) -> list[dict[str, Any]]:
    response = await _request_with_retry(
        page,
        "GET",
        f"{service_url}/v2/hme/list",
        max_attempts=LIST_MAX_ATTEMPTS,
    )
    return normalize_alias_list(response)


async def _get_or_create_client_id(page: Page) -> str:
    value = await page.evaluate(
        """
        () => {
          const keys = [
            "com.apple.icloud.clientId",
            "icloud.clientId",
            "clientId",
          ];
          for (const key of keys) {
            try {
              const value = localStorage.getItem(key) || sessionStorage.getItem(key);
              if (value) return String(value);
            } catch (_) {}
          }
          return "";
        }
        """
    )
    client_id = str(value or "").strip()
    if not client_id:
        client_id = str(uuid.uuid4()).upper()
        await page.evaluate(
            """
            (clientId) => {
              try { localStorage.setItem("com.apple.icloud.clientId", clientId); } catch (_) {}
            }
            """,
            client_id,
        )
    return client_id


async def _generate_hme_candidate(
    page: Page,
    service_url: str,
    *,
    host_preference: str = "auto",
    client_id: str = "",
    dsid: str = "",
) -> tuple[Any, str]:
    active_service_url = service_url
    variants = [
        {"name": "web-empty-body", "data": None, "include_ckjs": True},
        {"name": "json-langcode", "data": {"langCode": "en-us"}, "include_ckjs": True},
        {"name": "legacy-empty-body", "data": None, "include_ckjs": False},
        {"name": "legacy-json-langcode", "data": {"langCode": "en-us"}, "include_ckjs": False},
    ]
    last_error: Exception | None = None
    try:
        for index, variant in enumerate(variants, start=1):
            try:
                print(f"[icloud] generate attempt variant={variant['name']} ({index}/{len(variants)})")
                generated = await _request_with_retry(
                    page,
                    "POST",
                    f"{active_service_url}/v1/hme/generate",
                    data=variant["data"],
                    client_id=client_id,
                    dsid=dsid,
                    include_ckjs=bool(variant["include_ckjs"]),
                    max_attempts=1,
                )
                return generated, active_service_url
            except Exception as variant_err:  # noqa: BLE001
                last_error = variant_err
                if not _is_retryable_error(variant_err):
                    raise
                print(f"[icloud] generate variant failed: {variant_err}")
        raise last_error or RuntimeError("iCloud generate failed")
    except Exception as exc:  # noqa: BLE001
        if not _is_retryable_error(exc):
            raise
        print(f"[icloud] generate failed; refreshing service endpoint and retrying once: {exc}")
        active_service_url, _, ds_info = await _resolve_service(page, host_preference=host_preference)
        dsid = dsid or str(ds_info.get("dsid") or "")
        for index, variant in enumerate(variants, start=1):
            try:
                print(f"[icloud] generate retry variant={variant['name']} ({index}/{len(variants)})")
                generated = await _request_with_retry(
                    page,
                    "POST",
                    f"{active_service_url}/v1/hme/generate",
                    data=variant["data"],
                    client_id=client_id,
                    dsid=dsid,
                    include_ckjs=bool(variant["include_ckjs"]),
                    max_attempts=1,
                )
                return generated, active_service_url
            except Exception as variant_err:  # noqa: BLE001
                last_error = variant_err
                if not _is_retryable_error(variant_err):
                    raise
                print(f"[icloud] generate retry variant failed: {variant_err}")
        raise last_error or RuntimeError("iCloud generate failed after refresh")


async def fetch_icloud_hide_my_email(
    context: BrowserContext,
    *,
    generate_new: bool = False,
    host_preference: str = "auto",
    nav_timeout_ms: int = 60_000,
    login_timeout_seconds: int = 300,
    used_emails: set[str] | None = None,
) -> str:
    """Return an iCloud Hide My Email alias.

    The caller must use a persistent browser context and sign in to iCloud in
    that browser once. If `generate_new` is false, an existing active alias may
    be reused, matching the reference extension's default strategy.
    """
    if context is None:
        raise RuntimeError("iCloud 邮箱生成需要 Playwright browser context")

    page = await _ensure_mail_page(
        context,
        host_preference=host_preference,
        nav_timeout_ms=nav_timeout_ms,
    )
    try:
        service_url, setup_url, ds_info = await _resolve_service_with_login_wait(
            page,
            host_preference=host_preference,
            login_timeout_seconds=login_timeout_seconds,
        )
        print(f"[icloud] session OK via {urlparse(setup_url).hostname}; service={urlparse(service_url).hostname}")
        client_id = await _get_or_create_client_id(page)
        dsid = str(ds_info.get("dsid") or "")
        print(f"[icloud] request params: clientId={'yes' if client_id else 'NO'} dsid={'yes' if dsid else 'NO'}")

        existing_aliases = await _list_aliases(page, service_url)
        existing_email_set = {
            str(item.get("email") or "").strip().lower()
            for item in existing_aliases
            if item.get("email")
        }
        if not generate_new:
            reusable = pick_reusable_alias(existing_aliases, used_emails=used_emails)
            if reusable and reusable.get("email"):
                print(f"[icloud] reuse alias {reusable['email']}")
                return str(reusable["email"])
        else:
            print("[icloud] always-new mode enabled; skip reusable aliases")

        try:
            generated, service_url = await _generate_hme_candidate(
            page,
            service_url,
            host_preference=host_preference,
            client_id=client_id,
            dsid=dsid,
        )
        except Exception as generate_err:  # noqa: BLE001
            fallback_alias = pick_reusable_alias(existing_aliases, used_emails=used_emails)
            if fallback_alias and fallback_alias.get("email"):
                print(
                    "[icloud] generate failed; falling back to existing active alias "
                    f"{fallback_alias['email']}: {generate_err}"
                )
                return str(fallback_alias["email"])
            raise RuntimeError(
                "iCloud 创建新隐私邮箱被拒绝，且没有可复用的现有别名。"
                "请先在 iCloud 隐藏邮件地址里手动创建一个别名，或稍后再试。"
                f"原始错误：{generate_err}"
            ) from generate_err
        raw_hme = ((generated or {}).get("result") or {}).get("hme")
        generated_alias = _extract_generated_alias(raw_hme)
        if not (generated or {}).get("success") or not generated_alias:
            error = ((generated or {}).get("error") or {}).get("errorMessage") or "iCloud 隐私邮箱生成失败"
            raise RuntimeError(error)
        print(f"[icloud] generated candidate {generated_alias}; reserving")

        reserve_data: dict[str, Any] = {}
        if isinstance(raw_hme, dict):
            reserve_data.update(raw_hme)
        reserve_data.update(
            {
                "hme": generated_alias,
                "label": alias_label(),
                "note": "Generated through ChatGPT Quick Register",
            }
        )

        alias = ""
        try:
            reserved = await _request_with_retry(
                page,
                "POST",
                f"{service_url}/v1/hme/reserve",
                data=reserve_data,
                client_id=client_id,
                dsid=dsid,
                max_attempts=1,
            )
            alias = str((((reserved or {}).get("result") or {}).get("hme") or {}).get("hme") or "").strip().lower()
            if not (reserved or {}).get("success") or not alias:
                error = ((reserved or {}).get("error") or {}).get("errorMessage") or "iCloud 隐私邮箱保留失败"
                raise RuntimeError(error)
        except Exception as reserve_err:  # noqa: BLE001
            print(f"[icloud] reserve returned an error; checking alias list: {reserve_err}")
            aliases_after = await _list_aliases(page, service_url)
            recovered = find_alias_by_email(aliases_after, generated_alias)
            if not recovered:
                recovered = next(
                    (
                        item
                        for item in aliases_after
                        if str(item.get("email") or "").strip().lower() not in existing_email_set
                    ),
                    None,
                )
            if recovered and recovered.get("email"):
                alias = str(recovered["email"]).strip().lower()
            elif _is_retryable_error(reserve_err):
                reserved_retry = await _request_with_retry(
                    page,
                    "POST",
                    f"{service_url}/v1/hme/reserve",
                    data=reserve_data,
                    client_id=client_id,
                    dsid=dsid,
                    max_attempts=1,
                )
                alias = str(
                    (((reserved_retry or {}).get("result") or {}).get("hme") or {}).get("hme") or ""
                ).strip().lower()
            else:
                raise

        if not alias:
            raise RuntimeError("iCloud 隐私邮箱保留失败：未返回可用别名")
        print(f"[icloud] alias ready: {alias}")
        return alias
    except PWTimeout as exc:
        raise RuntimeError("打开 iCloud Mail 超时，请检查网络/代理并确认 iCloud 页面可访问。") from exc
