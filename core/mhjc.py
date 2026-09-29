"""Temporary mailboxes from the MHJC mail API + verification codes over IMAP.

REST (create/delete mailbox) uses the documented `X-API-Key` header:
    POST   {api_base}/mailbox
    DELETE {api_base}/mailbox/{username}

Verification codes are read over IMAP (SSL) as requested, using the mailbox
address and the password returned by the create call:
    host `mail.mhjc.edu.kg`, port `993`

The API key is never logged, echoed in errors, or persisted into account data.
"""

from __future__ import annotations

import email
import imaplib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from core.http_utils import open_url
from data.names import random_mailbox_username

DEFAULT_API_BASE = "https://api.mhjc.edu.kg/api"
DEFAULT_IMAP_HOST = "mail.mhjc.edu.kg"
DEFAULT_IMAP_PORT = 993
DEFAULT_DOMAIN = "mhjc.edu.kg"
DEFAULT_TTL_SECONDS = 86400  # provider maximum
API_KEY_HEADER = "X-API-Key"
MAX_RECENT_UIDS = 40

# Ordered most-specific first: a labelled code wins over a bare digit run.
CODE_PATTERNS = (
    re.compile(r"(?:代码为|验证码)[^0-9]{0,10}?(\d{6})"),
    re.compile(r"(?:chatgpt\s+log-?in\s+code|enter\s+this\s+code)[^0-9]{0,24}(\d{6})", re.IGNORECASE),
    re.compile(r"verification\s+code[^0-9]{0,12}(\d{6})", re.IGNORECASE),
    re.compile(r"code[:\s]+is[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"code[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
)


def api_error_message(body: str) -> str:
    """Pull the human-readable part out of an MHJC error body."""
    text = str(body or "").strip()
    if not text:
        return "未知错误"
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text[:200]
    if isinstance(parsed, dict):
        for key in ("message", "error"):
            value = str(parsed.get(key) or "").strip()
            if value:
                return value
    return text[:200]


def looks_like_name_conflict(message: str) -> bool:
    """True when the provider rejected the address because it is already taken."""
    return "exists" in str(message or "").lower()


class MhjcApiError(RuntimeError):
    """Raised when the MHJC mail API reports a failure."""


class MhjcAuthError(RuntimeError):
    """Raised when MHJC IMAP rejects the mailbox address or password."""


@dataclass
class MhjcImapConfig:
    email: str
    password: str
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    mailbox: str = "INBOX"


def normalize_api_base(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_API_BASE
    if not raw.startswith(("http://", "https://")):
        raise ValueError("MHJC API base 必须以 http:// 或 https:// 开头")
    return raw.rstrip("/")


def username_from_email(value: str) -> str:
    return str(value or "").strip().split("@", 1)[0].strip().lower()


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
    proxy: str | None,
    proxy_insecure: bool,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            API_KEY_HEADER: api_key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (ChatGPT Quick Register)",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        data=body,
    )
    try:
        with open_url(request, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise MhjcApiError(f"MHJC API HTTP {exc.code}：{api_error_message(detail)}") from exc
    except urllib.error.URLError as exc:
        raise MhjcApiError(f"MHJC API 连接失败：{exc.reason}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MhjcApiError("MHJC API 返回的不是 JSON") from exc
    if not isinstance(parsed, dict):
        raise MhjcApiError("MHJC API 返回结构异常")
    return parsed


def create_mailbox(
    api_key: str,
    *,
    base_url: str = "",
    username: str = "",
    password: str = "",
    ttl: int = DEFAULT_TTL_SECONDS,
    timeout: float = 20.0,
    proxy: str | None = None,
    proxy_insecure: bool = False,
) -> dict[str, Any]:
    """Create a temporary mailbox and return its address plus IMAP credentials."""
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("MHJC API key 为空（--mhjc-api-key 或 QR_MHJC_API_KEY）")

    payload: dict[str, Any] = {"ttl": max(60, min(int(ttl or DEFAULT_TTL_SECONDS), DEFAULT_TTL_SECONDS))}
    if str(username or "").strip():
        payload["username"] = str(username).strip()
    if str(password or "").strip():
        payload["password"] = str(password).strip()

    url = f"{normalize_api_base(base_url)}/mailbox"
    parsed = _request_json(
        "POST", url,
        api_key=key, payload=payload, timeout=timeout,
        proxy=proxy, proxy_insecure=proxy_insecure,
    )
    if not parsed.get("success"):
        raise MhjcApiError(f"创建 MHJC 邮箱失败：{parsed.get('error') or '未知错误'}")

    data = parsed.get("data")
    if not isinstance(data, dict):
        raise MhjcApiError("创建 MHJC 邮箱失败：响应缺少 data")

    address = str(data.get("email") or "").strip()
    mailbox_password = str(data.get("password") or "")
    if not address or not mailbox_password:
        raise MhjcApiError("创建 MHJC 邮箱失败：响应缺少 email/password")

    return {
        "email": address,
        "username": str(data.get("username") or username_from_email(address)),
        "password": mailbox_password,
        "expiresAt": data.get("expires_at"),
    }


def create_unique_mailbox(
    api_key: str,
    *,
    base_url: str = "",
    username: str = "",
    password: str = "",
    ttl: int = DEFAULT_TTL_SECONDS,
    timeout: float = 20.0,
    proxy: str | None = None,
    proxy_insecure: bool = False,
    name_style: str = "name",
    name_first: str = "",
    name_last: str = "",
    name_attempts: int = 4,
    name_generator: "Callable[[], str] | None" = None,
) -> dict[str, Any]:
    """Create a mailbox that is guaranteed to have a free address.

    Address preference order:
      1. an explicit ``username`` (a saved custom name can only ever be used once —
         the provider rejects a repeat with HTTP 400 — so a conflict is retried with
         a short unique suffix);
      2. ``name_style="name"`` (default): locally generated person-name addresses such
         as ``emma.wilson`` / ``emmadavis92``. The provider's own "random" default is
         ``temp_<hex>``, which reads as a throwaway mailbox and is easy to flag;
      3. the provider default (``username=""``) as the last resort.

    Real errors (bad key, network) are raised immediately instead of being masked by
    retries. The result carries ``nameFallback`` (and ``usernameFallback`` when an
    explicit name was requested) so the caller can tell the operator which candidate
    actually got through.
    """
    wanted = str(username or "").strip()
    style = str(name_style or "name").strip().lower()

    attempts: list[dict[str, str]] = []
    if wanted:
        attempts.append({"username": wanted, "kind": "explicit"})
        attempts.append({"username": f"{wanted}-{secrets.token_hex(2)}", "kind": "explicit-suffix"})

    seen = {item["username"] for item in attempts}
    if style != "provider":
        generate = name_generator or (lambda: random_mailbox_username(name_first, name_last))
        for _ in range(max(1, int(name_attempts))):
            try:
                generated = str(generate() or "").strip()
            except Exception:  # a broken generator must not break registration
                continue
            if not generated or generated in seen:
                continue
            seen.add(generated)
            attempts.append({"username": generated, "kind": "name"})

    attempts.append({"username": "", "kind": "provider"})

    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            created = create_mailbox(
                api_key,
                base_url=base_url,
                username=attempt["username"],
                password=password,
                ttl=ttl,
                timeout=timeout,
                proxy=proxy,
                proxy_insecure=proxy_insecure,
            )
        except MhjcApiError as exc:
            last_error = exc
            if not looks_like_name_conflict(str(exc)):
                raise  # wrong key / network / invalid request: do not retry
            continue
        if index:
            if wanted:
                created["usernameFallback"] = wanted
            created["nameFallback"] = attempt["kind"]
        return created

    raise last_error or MhjcApiError("创建 MHJC 邮箱失败")


def delete_mailbox(
    api_key: str,
    username: str,
    *,
    base_url: str = "",
    timeout: float = 20.0,
    proxy: str | None = None,
    proxy_insecure: bool = False,
) -> bool:
    """Delete a temporary mailbox and its messages. Best-effort cleanup helper."""
    key = str(api_key or "").strip()
    name = str(username or "").strip()
    if not key:
        raise ValueError("MHJC API key 为空（--mhjc-api-key 或 QR_MHJC_API_KEY）")
    if not name:
        raise ValueError("MHJC 邮箱 username 为空")

    url = f"{normalize_api_base(base_url)}/mailbox/{urllib.parse.quote(name, safe='')}"
    parsed = _request_json(
        "DELETE", url,
        api_key=key, timeout=timeout,
        proxy=proxy, proxy_insecure=proxy_insecure,
    )
    return bool(parsed.get("success"))


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _body_text(msg: Message) -> str:
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            if part.get_content_type() not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_code(text: str) -> str | None:
    """Return the first plausible 6-digit verification code, or None."""
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    for pattern in CODE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1)
    return None


def code_from_raw_message(raw: bytes) -> str | None:
    """Parse one RFC822 message (headers + body) and extract the code."""
    msg = email.message_from_bytes(raw)
    subject = _decode_header_value(msg.get("Subject"))
    code = extract_code(subject)
    if code:
        return code
    return extract_code(f"{subject}\n{_body_text(msg)}")


def _message_timestamp(date_header: str | None) -> float:
    if not date_header:
        return 0.0
    try:
        return parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _safe_logout(conn: imaplib.IMAP4_SSL | None) -> None:
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


_sessions: dict[tuple[str, int, str], imaplib.IMAP4_SSL] = {}


def close_sessions() -> None:
    for conn in list(_sessions.values()):
        _safe_logout(conn)
    _sessions.clear()


def _login(conn: imaplib.IMAP4_SSL, config: MhjcImapConfig) -> None:
    try:
        conn.login(config.email, config.password)
    except imaplib.IMAP4.error as exc:
        detail = exc.args[0].decode("utf-8", errors="replace") if exc.args and isinstance(exc.args[0], bytes) else str(exc)
        raise MhjcAuthError(
            "MHJC IMAP 登录被拒绝。用户名必须是完整邮箱地址，密码是创建邮箱时返回的密码；"
            "邮箱可能已过期（默认 24 小时）。"
            f" 服务器返回：{detail}"
        ) from exc


def _session(config: MhjcImapConfig) -> imaplib.IMAP4_SSL:
    key = (config.host, config.port, config.email)
    conn = _sessions.get(key)
    if conn is None:
        try:
            conn = imaplib.IMAP4_SSL(config.host, config.port, timeout=15)
        except OSError as exc:
            raise RuntimeError(f"MHJC IMAP 连接失败：{exc}") from exc
        _login(conn, config)
        _sessions[key] = conn
    return conn


def check_mhjc_imap_login(config: MhjcImapConfig, *, timeout: float = 12.0) -> None:
    """Open one IMAP session to validate mailbox credentials before a run."""
    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = imaplib.IMAP4_SSL(config.host, config.port, timeout=timeout)
        _login(conn, config)
    except MhjcAuthError:
        raise
    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
        raise RuntimeError(f"MHJC IMAP 连接失败：{exc}") from exc
    finally:
        _safe_logout(conn)


def _uids(data: Any) -> list[bytes]:
    out: list[bytes] = []
    for chunk in data or []:
        if chunk:
            out.extend(chunk.split())
    return out


def _poll_once(
    config: MhjcImapConfig,
    *,
    since_ts: float,
    seen: set[bytes],
) -> str | None:
    """One IMAP pass over the inbox; returns a fresh code or None."""
    conn = _session(config)
    try:
        typ, _ = conn.select(config.mailbox, readonly=True)
        if typ != "OK":
            return None
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            return None

        for uid in reversed(_uids(data)[-MAX_RECENT_UIDS:]):
            if uid in seen:
                continue
            typ, raw = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not raw:
                continue
            payload = next(
                (item[1] for item in raw if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)),
                b"",
            )
            if not payload:
                continue
            seen.add(uid)

            msg = email.message_from_bytes(payload)
            msg_ts = _message_timestamp(msg.get("Date"))
            if msg_ts and msg_ts < since_ts - 30:
                continue
            code = code_from_raw_message(payload)
            if code:
                print(f"[mhjc-imap] got code from {config.mailbox}")
                return code
    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
        _sessions.pop((config.host, config.port, config.email), None)
        raise
    return None


def fetch_mhjc_code(
    config: MhjcImapConfig,
    *,
    max_attempts: int = 60,
    interval_seconds: float = 3.0,
    since_ts: float | None = None,
) -> str:
    """Poll the MHJC mailbox over IMAP until a verification code arrives."""
    if since_ts is None:
        since_ts = time.time()
    floor = max(0.0, since_ts)
    seen: set[bytes] = set()
    last_error: Exception | None = None

    try:
        for attempt in range(1, max_attempts + 1):
            print(f"[mhjc-imap] poll {attempt}/{max_attempts}  interval={interval_seconds:g}s")
            try:
                code = _poll_once(config, since_ts=floor, seen=seen)
                if code:
                    return code
            except MhjcAuthError:
                raise
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                last_error = exc
                print(f"[mhjc-imap] reconnect after IMAP error: {exc}")
            if attempt < max_attempts:
                time.sleep(interval_seconds)
    finally:
        close_sessions()

    message = f"MHJC IMAP 在 {int(max_attempts * interval_seconds)} 秒内未找到验证码（邮箱 {config.email}）"
    if last_error:
        message += f"；最后一次错误：{last_error}"
    raise TimeoutError(message)


def scan_mhjc_messages(
    config: MhjcImapConfig,
    *,
    since_ts: float | None = None,
    limit: int = MAX_RECENT_UIDS,
) -> list[dict]:
    """Read recent messages as plain records for ban/notice detection.

    Unlike :func:`fetch_mhjc_code` this does not look for a verification code;
    it returns whatever the mailbox holds so the caller can classify it.
    """
    from core.ban_check import message_to_record

    conn = _session(config)
    typ, _ = conn.select(config.mailbox, readonly=True)
    if typ != "OK":
        return []
    typ, data = conn.uid("SEARCH", None, "ALL")
    if typ != "OK":
        return []

    records: list[dict] = []
    for uid in reversed(_uids(data)[-max(1, limit):]):
        typ, raw = conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not raw:
            continue
        payload = next(
            (item[1] for item in raw if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)),
            b"",
        )
        if not payload:
            continue
        record = message_to_record(payload)
        if since_ts is not None and record.get("date_ts") and record["date_ts"] < since_ts - 30:
            continue
        record["uid"] = uid.decode("ascii", errors="replace")
        records.append(record)
    return records


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_DOMAIN",
    "DEFAULT_IMAP_HOST",
    "DEFAULT_IMAP_PORT",
    "DEFAULT_TTL_SECONDS",
    "MhjcApiError",
    "MhjcAuthError",
    "MhjcImapConfig",
    "check_mhjc_imap_login",
    "close_sessions",
    "code_from_raw_message",
    "api_error_message",
    "create_mailbox",
    "create_unique_mailbox",
    "delete_mailbox",
    "extract_code",
    "fetch_mhjc_code",
    "scan_mhjc_messages",
    "normalize_api_base",
    "username_from_email",
]
