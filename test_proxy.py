"""Exercise real proxy transports locally; no Telegram/network dependency."""
import contextlib
import json
import os
import socketserver
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from bot import Telegram, TelegramError


@contextlib.contextmanager
def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HTTPProxy(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, json.loads(body)))
        response = json.dumps(self.server.payload).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Location", "http://unreachable.invalid/redirect")
        self.end_headers()
        self.wfile.write(response)

    def do_CONNECT(self):
        self.server.requests.append(self.path)
        self.send_error(502)  # Prove the HTTPS tunnel is routed here, without TLS/external access.


class SOCKSProxy(socketserver.StreamRequestHandler):
    def handle(self):
        version, count = self.rfile.read(2)
        methods = self.rfile.read(count)
        if version != 5 or 0 not in methods:
            return
        self.wfile.write(b"\x05\x00")
        version, command, reserved, address_type = self.rfile.read(4)
        if (version, command, reserved, address_type) != (5, 1, 0, 3):
            return
        host = self.rfile.read(self.rfile.read(1)[0]).decode()
        port = struct.unpack("!H", self.rfile.read(2))[0]
        self.server.targets.append((host, port))
        self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        line = self.rfile.readline().decode().strip()
        headers = {}
        while (header := self.rfile.readline()) != b"\r\n":
            key, _, value = header.decode().partition(":")
            headers[key.lower()] = value.strip()
        body = self.rfile.read(int(headers["content-length"]))
        self.server.requests.append((line, json.loads(body)))
        data = b'{"ok":true,"result":{"id":123}}'
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(data)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + data)


class ProxyTests(unittest.TestCase):
    TOKEN = "123:TEST_NOT_A_REAL_TOKEN"

    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def http_proxy(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPProxy)
        server.requests, server.status = [], 200
        server.payload = {"ok": True, "result": {"id": 123}}
        context = serve(server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return server

    def test_explicit_http_proxy_overrides_environment(self):
        server = self.http_proxy()
        os.environ["HTTP_PROXY"] = "http://unreachable.invalid:9"
        proxy = f"http://127.0.0.1:{server.server_port}"
        with patch.object(Telegram, "API_URL", "http://telegram.invalid"):
            self.assertEqual(Telegram(self.TOKEN, proxy).call("getMe"), {"id": 123})
        self.assertEqual(server.requests, [
            (f"http://telegram.invalid/bot{self.TOKEN}/getMe", {})])

    def test_https_proxy_environment_receives_connect(self):
        server = self.http_proxy()
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(TelegramError) as raised:
            Telegram(self.TOKEN).call("getMe")
        self.assertEqual(server.requests, ["api.telegram.org:443"])
        self.assertNotIn(self.TOKEN, str(raised.exception))

    def test_socks5h_all_proxy_resolves_domain_on_proxy(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SOCKSProxy)
        server.daemon_threads = True
        server.targets, server.requests = [], []
        with serve(server), patch.object(Telegram, "API_URL", "http://telegram.invalid"):
            os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{server.server_address[1]}"
            self.assertEqual(Telegram(self.TOKEN).call("getMe"), {"id": 123})
        self.assertEqual(server.targets, [("telegram.invalid", 80)])
        self.assertEqual(server.requests[0][0], f"POST /bot{self.TOKEN}/getMe HTTP/1.1")

    def test_business_errors_retry_after_and_redirects_are_sanitized(self):
        server = self.http_proxy()
        proxy = f"http://127.0.0.1:{server.server_port}"
        server.status = 429
        server.payload = {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}
        with patch.object(Telegram, "API_URL", "http://telegram.invalid"):
            with self.assertRaises(TelegramError) as raised:
                Telegram(self.TOKEN, proxy).call("getUpdates", timeout=25)
            self.assertEqual((raised.exception.code, raised.exception.retry_after), (429, 12))
            server.status = 302
            with self.assertRaises(TelegramError) as raised:
                Telegram(self.TOKEN, proxy).call("getMe")
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(len(server.requests), 2)

    def test_invalid_proxy_does_not_expose_credentials(self):
        secret = "secret-proxy-password"
        with self.assertRaises(ValueError) as raised:
            Telegram(self.TOKEN, f"ftp://user:{secret}@proxy:21")
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
