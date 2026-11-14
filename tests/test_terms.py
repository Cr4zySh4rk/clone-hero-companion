"""Terms of Use / Privacy Policy acceptance gate: 451 on state-changing POSTs until accepted, accept, version bump, quit."""
import http.client
import json
import os
import threading
import types
import unittest
from unittest import mock

from chcompanion import app, terms
from chcompanion.updater import Updater


class TermsGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start(); self.addCleanup(patcher.stop)
        os.environ.pop("CHC_ACCEPT_TERMS", None)
        os.environ.pop("CHC_SKIP_UPDATE_CHECK", None)
        self.settings = {}
        self.quit = threading.Event()
        self.u = Updater(quit_event=self.quit, current="2.8.1")
        self.comp = types.SimpleNamespace(
            updater=self.u, adjust_offset=mock.Mock(), quit_requested=self.quit, simulate=mock.Mock(), libmgr=mock.Mock(),
            settings=lambda: dict(self.settings), update_settings=lambda v: self.settings.update(v) or dict(self.settings))
        self.old = app.Handler.companion
        self.srv = app.serve(self.comp, "127.0.0.1", 0)
        self.addCleanup(lambda: setattr(app.Handler, "companion", self.old))
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, json.dumps(body or {}) if method == "POST" else None, {"Content-Type": "application/json"})
        r = conn.getresponse(); data = r.read(); conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def test_posts_are_blocked_before_acceptance(self):
        for path in ("/api/offset", "/api/library/rescan", "/api/settings", "/api/songs/action", "/api/install", "/api/telemetry/event"):
            code, body = self.call("POST", path, {})
            self.assertEqual((code, body["error"]), (451, "terms_not_accepted"), path)
        self.comp.adjust_offset.assert_not_called()
        self.assertEqual(self.call("GET", "/api/terms")[1]["required"], True)

    def test_accept_unblocks_and_records(self):
        code, body = self.call("POST", "/api/terms/accept", {})
        self.assertEqual(code, 200); self.assertFalse(body["required"])
        self.assertEqual(self.settings["terms_version"], terms.TERMS_VERSION)
        self.assertRegex(self.settings["accepted_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(self.call("POST", "/api/offset", {"kind": "lyrics", "delta": 0.1})[0], 200)

    def test_version_bump_asks_again(self):
        self.call("POST", "/api/terms/accept", {})
        self.assertEqual(self.call("POST", "/api/offset", {"kind": "lyrics", "delta": 0.1})[0], 200)
        with mock.patch.object(terms, "TERMS_VERSION", terms.TERMS_VERSION + 1):
            self.assertEqual(self.call("POST", "/api/offset", {})[0], 451)
            self.assertTrue(self.call("GET", "/api/terms")[1]["required"])

    def test_decline_quits(self):
        self.assertEqual(self.call("POST", "/api/quit", {})[0], 200)
        self.assertTrue(self.quit.is_set())

    def test_settings_cannot_fake_acceptance(self):
        os.environ["CHC_ACCEPT_TERMS"] = "1"          # let the POST through; the keys must still be dropped by the route
        self.comp.update_settings = mock.Mock(return_value={})
        self.call("POST", "/api/settings", {"settings": {"terms_version": 99, "accepted_at": "x", "workers": 1}})
        self.comp.update_settings.assert_called_once_with({"workers": 1})

    def test_update_gate_has_priority(self):
        self.u.status = "available"
        self.u.release = {"tag": "v9.0.0", "version": "9.0.0", "notes": "", "url": "https://example/r", "assets": [], "source": "api"}
        self.assertEqual(self.call("POST", "/api/offset", {})[0], 426)

    def test_env_accepts_for_tests_and_ci(self):
        os.environ["CHC_ACCEPT_TERMS"] = "1"
        self.assertEqual(self.call("POST", "/api/offset", {"kind": "lyrics", "delta": 0.1})[0], 200)

    def test_open_only_allows_the_two_documents(self):
        with mock.patch("chcompanion.process.open_url") as wb:
            self.assertEqual(self.call("POST", "/api/open", {"url": terms.URL_TERMS})[0], 200)
            self.assertEqual(self.call("POST", "/api/open", {"url": "https://cloneherocompanion.com/evil"})[0], 400)
        wb.assert_called_once_with(terms.URL_TERMS)


if __name__ == "__main__":
    unittest.main()
