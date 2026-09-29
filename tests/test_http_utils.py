from __future__ import annotations

import http.client
import io
import ssl
import unittest
import urllib.request
from unittest.mock import Mock, patch

from core import http_utils


class HttpsHandshakeTests(unittest.TestCase):
    def _connection(self):
        opener = http_utils.build_opener(proxy="http://user:secret@proxy.test:8080")
        handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        captured = {}

        def capture(connection_type, request, **kwargs):
            captured["connection"] = connection_type("proxy.test:8080", **kwargs)
            return Mock()

        with patch.object(handler, "do_open", side_effect=capture):
            handler.https_open(urllib.request.Request("https://mail.test/api/mailbox"))
        connection = captured["connection"]
        connection.set_tunnel("mail.test", headers={"Proxy-Authorization": "Basic test"})
        return connection

    def test_handshake_eof_reconnects_before_sending_the_request(self):
        connection = self._connection()
        failed_socket = Mock()
        connection.sock = failed_socket
        with patch.object(http.client.HTTPSConnection, "connect", side_effect=[ssl.SSLEOFError("handshake EOF"), None]) as connect, \
             patch.object(connection, "close") as close, \
             patch("time.sleep"):
            connection.connect()
        self.assertEqual(connect.call_count, 2)
        failed_socket.close.assert_called_once()
        close.assert_not_called()
        self.assertEqual(connection._tunnel_host, "mail.test")
        self.assertEqual(connection._tunnel_headers["Proxy-Authorization"], "Basic test")

    def test_handshake_eof_is_bounded_to_three_attempts(self):
        connection = self._connection()
        sockets = []

        def fail_connect():
            connection.sock = Mock()
            sockets.append(connection.sock)
            raise ssl.SSLEOFError("handshake EOF")

        with patch.object(http.client.HTTPSConnection, "connect", side_effect=fail_connect) as connect, \
             patch.object(connection, "close") as close, \
             patch("time.sleep"):
            with self.assertRaises(ssl.SSLEOFError):
                connection.connect()
        self.assertEqual(connect.call_count, 3)
        for sock in sockets:
            sock.close.assert_called_once()
        close.assert_not_called()

    def test_certificate_errors_are_not_retried(self):
        connection = self._connection()
        with patch.object(http.client.HTTPSConnection, "connect", side_effect=ssl.SSLCertVerificationError("bad certificate")) as connect:
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        self.assertEqual(connect.call_count, 1)

    def test_proxy_authentication_errors_are_not_retried(self):
        connection = self._connection()
        with patch.object(http.client.HTTPSConnection, "connect", side_effect=OSError("Tunnel connection failed: 407")) as connect:
            with self.assertRaises(OSError):
                connection.connect()
        self.assertEqual(connect.call_count, 1)

    def test_post_survives_real_handshake_retry_without_losing_request_state(self):
        first, second = Mock(), Mock()
        second.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
        )
        sockets = iter((first, second))

        def connect_tcp(connection):
            connection.sock = next(sockets)

        request = urllib.request.Request("https://mail.test/api/mailbox", data=b"{}", method="POST")
        with patch.object(http.client.HTTPConnection, "connect", new=connect_tcp), \
             patch.object(ssl.SSLContext, "wrap_socket", side_effect=[ssl.SSLEOFError("handshake EOF"), second]), \
             patch("time.sleep"):
            with http_utils.open_url(request, proxy="http://user:secret@proxy.test:8080") as response:
                self.assertEqual(response.read(), b"{}")
        first.close.assert_called_once()
        first.sendall.assert_not_called()
        sent = b"".join(call.args[0] for call in second.sendall.call_args_list)
        self.assertEqual(sent.count(b"POST /api/mailbox HTTP/1.1"), 1)
        self.assertTrue(sent.endswith(b"{}"))

    def test_tls_failure_after_post_is_not_replayed(self):
        opener = http_utils.build_opener(proxy="http://user:secret@proxy.test:8080")
        request = urllib.request.Request("https://mail.test/api/mailbox", data=b"{}", method="POST")
        with patch.object(http.client.HTTPSConnection, "request") as send, \
             patch.object(http.client.HTTPSConnection, "getresponse", side_effect=ssl.SSLEOFError("response EOF")), \
             patch.object(http.client.HTTPSConnection, "close"):
            with self.assertRaises(ssl.SSLEOFError):
                opener.open(request, timeout=1)
        self.assertEqual(send.call_count, 1)

    def test_insecure_is_scoped_to_the_opener_and_keeps_proxy(self):
        original = urllib.request._opener
        opener = http_utils.build_opener(proxy="http://proxy.test:8080", insecure=True)
        handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_NONE)
        proxy_handler = next(h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler))
        self.assertEqual(proxy_handler.proxies["https"], "http://proxy.test:8080")
        self.assertIs(urllib.request._opener, original)

    def test_default_opener_keeps_certificate_validation(self):
        handler = next(h for h in http_utils.build_opener().handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)

    def test_open_url_uses_one_scoped_opener_for_both_ssl_modes(self):
        for insecure in (False, True):
            opener = Mock()
            opener.open.return_value = io.BytesIO(b"ok")
            with patch.object(http_utils, "build_opener", return_value=opener) as build:
                request = urllib.request.Request("https://mail.test/api/mailbox", data=b"{}")
                response = http_utils.open_url(request, proxy="http://proxy.test:8080", insecure=insecure, timeout=7)
            build.assert_called_once_with(proxy="http://proxy.test:8080", insecure=insecure)
            opener.open.assert_called_once_with(request, timeout=7)
            self.assertEqual(response.read(), b"ok")
