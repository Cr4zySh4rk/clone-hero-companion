import http.server
import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock

from chcompanion import net

BLACKHOLE = "10.255.255.1"   # non-routable: connect() never completes (like a dead CDN address)


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class HappyEyeballsTests(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _addrs(self, *ips):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, self.port)) for ip in ips]

    def test_dead_first_address_does_not_stall_the_request(self):
        # 2.6.2 -> 2.6.3 sat at 0% for 30 s: the first CDN address never answered and Python waited it out
        with mock.patch.object(net.socket, "getaddrinfo", return_value=self._addrs(BLACKHOLE, "127.0.0.1")):
            t = time.monotonic()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), *net.handlers())
            with opener.open(urllib.request.Request(f"http://example.test:{self.port}/"), timeout=30) as r:
                self.assertEqual(r.read(), b"ok")
            self.assertLess(time.monotonic() - t, 2.0)

    def test_read_timeout_is_the_callers_not_the_connect_cap(self):
        with mock.patch.object(net.socket, "getaddrinfo", return_value=self._addrs("127.0.0.1")):
            s = net.fast_create_connection(("example.test", self.port), timeout=30)
            self.assertEqual(s.gettimeout(), 30)
            s.close()

    def test_all_addresses_dead_fails_within_the_connect_cap(self):
        with mock.patch.object(net.socket, "getaddrinfo", return_value=self._addrs(BLACKHOLE, BLACKHOLE)), \
                mock.patch.object(net, "CONNECT_TIMEOUT_S", 1.0):
            t = time.monotonic()
            with self.assertRaises(OSError):
                net.fast_create_connection(("example.test", self.port), timeout=30)
            self.assertLess(time.monotonic() - t, 3.0)

    def test_refused_address_falls_through_to_the_next(self):
        with socket.socket() as dead:
            dead.bind(("127.0.0.2", 0))
            refused = dead.getsockname()[1]
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", refused)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", self.port))]
        with mock.patch.object(net.socket, "getaddrinfo", return_value=addrs):
            s = net.fast_create_connection(("example.test", self.port), timeout=5)
            self.assertEqual(s.getpeername()[0], "127.0.0.1")
            s.close()

    def test_interleaves_families(self):
        v6 = (socket.AF_INET6, 1, 6, "", ("::1", 1, 0, 0))
        v4 = (socket.AF_INET, 1, 6, "", ("127.0.0.1", 1))
        self.assertEqual(net._interleave([v6, v6, v4, v4]), [v6, v4, v6, v4])


if __name__ == "__main__":
    unittest.main()
