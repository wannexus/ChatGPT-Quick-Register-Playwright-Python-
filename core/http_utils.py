"""Shared stdlib HTTP helpers for proxy-aware requests."""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

PROXY_PART_KEYS = ("scheme", "host", "port", "user", "password")


class _HandshakeRetryConnection(http.client.HTTPSConnection):
    def connect(self):
        # Only reconnect before HTTP bytes are sent; never replay a POST.
        for attempt in range(3):
            try:
                return super().connect()
            except ssl.SSLEOFError:
                # HTTPConnection.close() would reset an in-progress request to Idle.
                sock, self.sock = self.sock, None
                if sock is not None:
                    sock.close()
                if attempt == 2:
                    raise
                print(f"[http] TLS handshake closed; reconnecting ({attempt + 2}/3)")
                time.sleep(0.5 * (attempt + 1))


class _HandshakeRetryHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _HandshakeRetryConnection, req,
            context=self._context, check_hostname=self._check_hostname,
        )


def build_opener(*, proxy: Optional[str] = None, insecure: bool = False):
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    handlers = [_HandshakeRetryHandler(context=context)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def open_url(req, *, proxy: Optional[str] = None, insecure: bool = False, timeout: float = 20.0):
    return build_opener(proxy=proxy, insecure=insecure).open(req, timeout=timeout)


def build_proxy_url(
    *,
    host: str = "",
    port: str | int = "",
    user: str = "",
    password: str = "",
    scheme: str = "http",
) -> str:
    """Compose a proxy URL from separate host/port/username/password fields.

    Returns "" when no host is configured, so callers can keep treating an empty
    string as "no proxy". Credentials are percent-encoded, which matters for
    passwords containing `@`, `:` or `/`.
    """
    raw_host = str(host or "").strip()
    if not raw_host:
        return ""

    raw_scheme = str(scheme or "").strip() or "http"
    if "://" in raw_host:
        embedded_scheme, _, raw_host = raw_host.partition("://")
        if embedded_scheme:
            raw_scheme = embedded_scheme
    raw_host = raw_host.strip().strip("/")

    credentials = ""
    raw_user = str(user or "")
    raw_password = str(password or "")
    if raw_user or raw_password:
        encoded_user = urllib.parse.quote(raw_user, safe="")
        encoded_password = urllib.parse.quote(raw_password, safe="")
        # `user@host` reads better than `user:@host` when there is no password.
        if raw_password:
            credentials = f"{encoded_user}:{encoded_password}@"
        else:
            credentials = f"{encoded_user}@"

    raw_port = str(port or "").strip()
    authority = f"{credentials}{raw_host}"
    if raw_port:
        authority += f":{raw_port}"
    return f"{raw_scheme}://{authority}"


def proxy_parts(proxy_value: str) -> Dict[str, str]:
    """Split a stored proxy URL back into the editable fields the UI shows."""
    raw = str(proxy_value or "").strip()
    if not raw or "://" not in raw:
        return {key: "" for key in PROXY_PART_KEYS}
    parsed = urllib.parse.urlparse(raw)
    if not parsed.hostname:
        return {key: "" for key in PROXY_PART_KEYS}
    return {
        "scheme": parsed.scheme or "",
        "host": parsed.hostname or "",
        "port": str(parsed.port) if parsed.port else "",
        "user": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
    }


def redact_proxy_url(proxy_value: str) -> str:
    """Mask proxy credentials so a proxy URL is safe to log."""
    raw = str(proxy_value or "").strip()
    if not raw:
        return ""
    parts = proxy_parts(raw)
    if not parts["host"]:
        return "***"
    credentials = ""
    if parts["user"] or parts["password"]:
        credentials = f"{parts['user']}:***@"
    authority = f"{credentials}{parts['host']}"
    if parts["port"]:
        authority += f":{parts['port']}"
    return f"{parts['scheme'] or 'http'}://{authority}"
