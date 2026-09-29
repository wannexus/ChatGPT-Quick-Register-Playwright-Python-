"""Tiny local HTTP proxy bridge for browser traffic.

Chromium sometimes rejects authenticated upstream proxies with
`ERR_PROXY_AUTH_UNSUPPORTED`. This bridge exposes a local unauthenticated HTTP
proxy and forwards CONNECT requests to the upstream proxy with
Proxy-Authorization injected server-side.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ssl
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass
class UpstreamProxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def needs_auth_bridge(self) -> bool:
        return bool(self.username or self.password) and self.scheme in {"http", "https"}

    @property
    def auth_header(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


def parse_upstream_proxy(proxy_value: str) -> UpstreamProxy | None:
    raw = str(proxy_value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return UpstreamProxy(
        scheme=parsed.scheme.lower(),
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


class BrowserProxyBridge:
    def __init__(self, upstream: UpstreamProxy) -> None:
        self.upstream = upstream
        self.server: asyncio.base_events.Server | None = None
        self.listen_host = "127.0.0.1"
        self.listen_port = 0
        self._client_tasks: set[asyncio.Task] = set()

    @property
    def server_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, self.listen_host, 0)
        sock = self.server.sockets[0]
        self.listen_port = int(sock.getsockname()[1])

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.server = None

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._client_tasks.add(current)
        upstream_reader = None
        upstream_writer = None
        try:
            request_line = await client_reader.readline()
            if not request_line:
                return
            method, target, version = _parse_request_line(request_line)
            header_lines = await _read_header_lines(client_reader)
            if method.upper() != "CONNECT":
                await _write_simple_response(client_writer, 501, b"CONNECT only")
                return

            upstream_reader, upstream_writer = await _open_upstream(self.upstream)
            upstream_writer.write(request_line)
            for line in _inject_proxy_auth(header_lines, self.upstream.auth_header):
                upstream_writer.write(line)
            upstream_writer.write(b"\r\n")
            await upstream_writer.drain()

            status_line = await upstream_reader.readline()
            if not status_line:
                raise RuntimeError("upstream proxy closed before CONNECT response")
            response_headers = await _read_header_lines(upstream_reader)
            client_writer.write(status_line)
            for line in response_headers:
                client_writer.write(line)
            client_writer.write(b"\r\n")
            await client_writer.drain()

            try:
                status_code = int(status_line.split(b" ", 2)[1])
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"bad upstream CONNECT response: {status_line!r}") from exc
            if status_code != 200:
                return

            await _bidirectional_relay(client_reader, client_writer, upstream_reader, upstream_writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await _write_simple_response(client_writer, 502, str(exc).encode("utf-8", errors="replace"))
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await client_writer.wait_closed()
            if current is not None:
                self._client_tasks.discard(current)


async def _open_upstream(upstream: UpstreamProxy):
    ssl_ctx = None
    server_hostname = None
    if upstream.scheme == "https":
        ssl_ctx = ssl.create_default_context()
        server_hostname = upstream.host
    return await asyncio.open_connection(
        upstream.host,
        upstream.port,
        ssl=ssl_ctx,
        server_hostname=server_hostname,
    )


def _parse_request_line(line: bytes) -> tuple[str, str, str]:
    parts = line.decode("iso-8859-1", errors="replace").strip().split(" ")
    if len(parts) != 3:
        raise RuntimeError(f"bad request line: {line!r}")
    return parts[0], parts[1], parts[2]


async def _read_header_lines(reader: asyncio.StreamReader) -> list[bytes]:
    lines: list[bytes] = []
    while True:
        line = await reader.readline()
        if not line:
            break
        if line in {b"\r\n", b"\n"}:
            break
        lines.append(line)
        if len(lines) > 200:
            raise RuntimeError("too many headers")
    return lines


def _inject_proxy_auth(lines: list[bytes], auth_header: str) -> list[bytes]:
    out: list[bytes] = []
    saw_auth = False
    for raw in lines:
        name = raw.split(b":", 1)[0].strip().lower()
        if name == b"proxy-authorization":
            saw_auth = True
            continue
        out.append(raw)
    if not saw_auth:
        out.append(f"Proxy-Authorization: {auth_header}\r\n".encode("ascii"))
    return out


async def _write_simple_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reasons = {
        501: b"Not Implemented",
        502: b"Bad Gateway",
    }
    reason = reasons.get(status, b"Error")
    payload = body or reason
    writer.write(
        b"HTTP/1.1 "
        + str(status).encode("ascii")
        + b" "
        + reason
        + b"\r\nContent-Length: "
        + str(len(payload)).encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + payload
    )
    await writer.drain()


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        return
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def _bidirectional_relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    to_upstream = asyncio.create_task(_pump(client_reader, upstream_writer))
    to_client = asyncio.create_task(_pump(upstream_reader, client_writer))
    done, pending = await asyncio.wait(
        {to_upstream, to_client},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done | pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
