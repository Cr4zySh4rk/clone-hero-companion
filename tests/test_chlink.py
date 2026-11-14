"""2.7.0: link a CH Leaderboards account by signing in to CH. The CH API and the Supabase functions are mocked; the
session token must never reach disk (settings, session file, any file in the config dir) or the logs."""
import json
import logging
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

from chcompanion import chlink
from chcompanion.app import Companion as App
from chcompanion.chlink import ChLink, ChLinkError
from chcompanion.cloud import Cloud
from chcompanion.store import Store

TOKEN = "SECRET-CH-SESSION-TOKEN-0123456789"
EXPIRY_ISO = "2099-01-02T03:04:05.000Z"
EXPIRY_TS = 4071006245.0
SESSION = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "user": {"id": "u1"}}


class FakeApp:
    """Just what ChLink uses of the app: cloud, set_ch_link (the real App method, bound to this fake)."""
    def __init__(self, tmp, fn_handler):
        self.tmp = Path(tmp)
        self.opened, self.fn_calls = [], []
        self.store = Store(self.tmp / "store.json")
        self.cleared = 0
        self.playlog = type("PL", (), {"clear_leaderboard": lambda s: setattr(self, "cleared", self.cleared + 1)})()

        def http(method, url, headers, body, timeout=20.0):
            if "/functions/v1/" in url:
                b = json.loads(body) if body else None
                self.fn_calls.append((url.rsplit("/", 1)[1], b))
                st, data = fn_handler(url.rsplit("/", 1)[1], b)
                return st, json.dumps(data).encode()
            return 200, json.dumps(SESSION).encode()
        self.cloud = Cloud(self.tmp / "cloud_session.json", http=http, open_browser=self.opened.append)
        self.cloud.session = dict(SESSION, expires_at=time.time() + 3600)
        self.cloud._save()

    DEFAULTS = App.DEFAULTS

    def settings(self):
        s = dict(self.DEFAULTS)
        s.update(self.store.get("settings", {}) or {})
        return s

    def update_settings(self, values):
        cur = self.store.get("settings", {}) or {}
        cur.update({k: v for k, v in values.items() if k in self.DEFAULTS})
        self.store.set("settings", cur)

    set_ch_link = App.set_ch_link
    ch_account_id = App.ch_account_id


def ch_http(script, calls):
    """A CH API mock: `script` is the list of /v2/auth/status answers, consumed in order (the last one repeats)."""
    def http(method, url, headers, body, timeout=20.0):
        calls.append((method, url, headers.get("Authorization")))
        if "/v2/auth/start" in url:
            return 200, json.dumps({"authRequestId": "REQ1", "authorizationUrl": "https://leaderboards.clonehero.net/login?authRequestId=REQ1"}).encode()
        if "/v2/auth/status" in url:
            return 200, json.dumps(script.pop(0) if len(script) > 1 else script[0]).encode()
        if "/v2/auth/complete" in url:
            return 200, json.dumps({"sessionToken": TOKEN, "purpose": "launcher", "sessionTokenExpirationDate": EXPIRY_ISO}).encode()
        return 404, b"{}"
    return http


def link_ok(name, body):
    if name == "ch-link" and body.get("sessionToken") == TOKEN:
        return 200, {"accountId": 83556, "username": "W4RL0CKM0NK3Y", "profile": {}, "alsoLinkedElsewhere": False,
                     "sessionKept": body.get("keepSession") is True}
    if name == "ch-link" and body.get("unlink"):
        return 200, {"unlinked": True}
    return 400, {"error": "bad"}


def wait_for(cl, states, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cl.status()["state"] in states:
            return cl.status()
        time.sleep(0.01)
    raise AssertionError(f"state {cl.status()} not in {states}")


def files_text(root):
    out = []
    for p in Path(root).rglob("*"):
        if p.is_file():
            out.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(out)


class ChLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.logs = []

        class H(logging.Handler):
            def emit(h, rec):
                self.logs.append(rec.getMessage())
        self.secret_store = {}                                       # the OS secure storage, in memory (never the real one)
        fake = types.SimpleNamespace(get=lambda n: self.secret_store.get(n),
                                     set=lambda n, v: self.secret_store.__setitem__(n, v) or True,
                                     delete=lambda n: self.secret_store.pop(n, None) and None)
        p = mock.patch.object(chlink, "secrets", fake)
        p.start()
        self.addCleanup(p.stop)
        self.handler = H(level=logging.DEBUG)
        logging.getLogger().addHandler(self.handler)
        self.old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(lambda: (logging.getLogger().removeHandler(self.handler), logging.getLogger().setLevel(self.old_level)))

    def make(self, script, fn=link_ok):
        app = FakeApp(self.tmp.name, fn)
        calls = []
        cl = ChLink(app, http=ch_http(script, calls), sleep=lambda s: time.sleep(0.005))
        return app, cl, calls

    def test_no_browser_opened_puts_the_link_in_the_status(self):
        app = FakeApp(self.tmp.name, link_ok)
        cl = ChLink(app, http=ch_http([{"status": "pending"}] * 50, []), sleep=lambda s: time.sleep(0.005),
                    open_url=lambda url: False)
        st = cl.start()
        self.assertEqual(st["state"], "waiting")
        self.assertIs(st.get("opened"), False)
        self.assertIn("authRequestId=REQ1", st["url"])
        cl.cancel()

    def test_pending_then_complete_links_and_never_stores_the_token(self):
        app, cl, calls = self.make([{"status": "pending"}, {"status": "pending"}, {"status": "complete"}])
        st = cl.start()
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(app.opened, ["https://leaderboards.clonehero.net/login?authRequestId=REQ1"])
        done = wait_for(cl, {"done"})
        self.assertEqual((done["username"], done["accountId"]), ("W4RL0CKM0NK3Y", 83556))
        s = app.settings()
        self.assertEqual((s["ch_username"], s["ch_account_id"], s["ch_verified"]), ("W4RL0CKM0NK3Y", 83556, True))
        self.assertEqual(app.ch_account_id(), 83556)
        self.assertEqual(app.cleared, 1)
        self.assertEqual([c[0] for c in app.fn_calls], ["ch-link"])           # the token went to the server function only
        # the token is nowhere on disk and not in the logs or in the status the UI polls
        self.assertNotIn(TOKEN, files_text(self.tmp.name))
        self.assertNotIn(TOKEN, "\n".join(self.logs))
        self.assertNotIn(TOKEN, json.dumps(cl.status()))
        self.assertNotIn(TOKEN, json.dumps(app.settings()))
        self.assertNotIn(TOKEN, json.dumps(vars(cl), default=str))

    def test_requires_a_companion_sign_in(self):
        app, cl, calls = self.make([{"status": "pending"}])
        app.cloud.session = None
        with self.assertRaises(ChLinkError):
            cl.start()
        self.assertEqual(calls, [])                                            # CH was not even contacted

    def test_pending_account_creation_shows_its_message_then_completes(self):
        app, cl, calls = self.make([{"status": "pendingAccountCreation"}, {"status": "pendingAccountCreation"}, {"status": "complete"}])
        cl.start()
        st = wait_for(cl, {"pendingAccountCreation", "done"})
        if st["state"] == "pendingAccountCreation":
            self.assertIn("CH account", st["message"])
        wait_for(cl, {"done"})

    def test_failed_and_expired(self):
        app, cl, _ = self.make([{"status": "failed", "errorMessage": "denied by user"}])
        cl.start()
        st = wait_for(cl, {"failed"})
        self.assertIn("denied by user", st["message"])
        self.assertEqual(app.settings()["ch_verified"], False)
        app, cl, _ = self.make([{"status": "expired"}])
        cl.start()
        self.assertEqual(wait_for(cl, {"expired"})["state"], "expired")
        self.assertEqual(app.fn_calls, [])

    def test_gives_up_after_ten_minutes(self):
        clock = [0.0]
        app = FakeApp(self.tmp.name, link_ok)
        cl = ChLink(app, http=ch_http([{"status": "pending"}], []), sleep=lambda s: clock.__setitem__(0, clock[0] + 300) or time.sleep(0.001),
                    now=lambda: clock[0])
        cl.start()
        self.assertEqual(wait_for(cl, {"expired"})["state"], "expired")

    def test_cancel_stops_polling_and_links_nothing(self):
        app, cl, calls = self.make([{"status": "pending"}])
        cl.start()
        st = cl.cancel()
        self.assertEqual(st["state"], "cancelled")
        n = len(calls)
        time.sleep(0.05)
        self.assertEqual(len(calls), n)                                        # polling really stopped
        self.assertEqual(app.fn_calls, [])
        self.assertFalse(app.settings()["ch_verified"])

    def test_server_rejects_the_session(self):
        app, cl, _ = self.make([{"status": "complete"}], fn=lambda n, b: (401, {"error": "ch_session_invalid"}))
        cl.start()
        st = wait_for(cl, {"failed"})
        self.assertIn("didn't accept", st["message"])
        self.assertFalse(app.settings()["ch_verified"])
        self.assertNotIn(TOKEN, files_text(self.tmp.name) + "\n".join(self.logs))

    def test_unlink_calls_the_function_and_forgets_the_link(self):
        app, cl, _ = self.make([{"status": "complete"}])
        cl.start()
        wait_for(cl, {"done"})
        cl.unlink()
        self.assertEqual(app.fn_calls[-1], ("ch-link", {"unlink": True, "sessionToken": TOKEN}))   # the server logs the kept session out
        self.assertEqual(self.secret_store, {})                                                    # and the local copy is deleted
        s = app.settings()
        self.assertEqual((s["ch_username"], s["ch_account_id"], s["ch_verified"]), ("", 0, False))
        self.assertEqual(app.ch_account_id(), 0)

    def test_unlink_failure_keeps_the_link(self):
        app, cl, _ = self.make([{"status": "complete"}])
        cl.start()
        wait_for(cl, {"done"})
        cl.app.cloud.http = lambda *a, **k: (500, b"{}")
        with self.assertRaises(ChLinkError):
            cl.unlink()
        self.assertTrue(app.settings()["ch_verified"])

    def test_legacy_unverified_username_has_no_account_id_for_stats(self):
        app, cl, _ = self.make([{"status": "pending"}])
        app.update_settings({"ch_username": "typed", "ch_account_id": 5})       # saved before 2.7.0, never verified
        self.assertEqual(app.ch_account_id(), 0)

    # ------------------------------------------------------------ 2.10.0: the kept session
    def link(self, fn=link_ok):
        app, cl, calls = self.make([{"status": "complete"}], fn=fn)
        cl.start()
        wait_for(cl, {"done", "failed"})
        return app, cl, calls

    def test_keepsession_is_requested_and_the_session_is_kept_only_in_secure_storage(self):
        app, cl, _ = self.link()
        self.assertEqual(app.fn_calls[0], ("ch-link", {"sessionToken": TOKEN, "keepSession": True}))
        self.assertTrue(cl.status()["launchSignedIn"])
        self.assertEqual(json.loads(self.secret_store[chlink.SECRET_NAME]), {"token": TOKEN, "expires": EXPIRY_TS})
        self.assertEqual(chlink.load_session(now=1.0), {"token": TOKEN, "expires": EXPIRY_TS})
        self.assertNotIn(TOKEN, files_text(self.tmp.name))
        self.assertNotIn(TOKEN, "\n".join(self.logs))
        self.assertNotIn(TOKEN, json.dumps(app.settings()) + json.dumps(cl.status()) + json.dumps(vars(cl), default=str))

    def test_an_older_server_that_logged_the_session_out_stores_nothing(self):
        app, cl, _ = self.link(fn=lambda n, b: (200, {"accountId": 83556, "username": "W4RL0CKM0NK3Y"}))     # no sessionKept
        self.assertEqual(cl.status()["state"], "done")
        self.assertFalse(cl.status()["launchSignedIn"])
        self.assertEqual(self.secret_store, {})

    def test_a_storage_failure_still_links_and_logs_no_token(self):
        with mock.patch.object(chlink.secrets, "set", lambda n, v: False):
            app, cl, _ = self.link()
        self.assertEqual(cl.status()["state"], "done")
        self.assertFalse(cl.status()["launchSignedIn"])
        self.assertNotIn(TOKEN, "\n".join(self.logs))

    def test_a_failed_link_keeps_nothing(self):
        self.link(fn=lambda n, b: (401, {"error": "ch_session_invalid"}))
        self.assertEqual(self.secret_store, {})

    def test_expired_session_is_deleted_when_read(self):
        chlink.save_session(TOKEN, 1000.0)
        self.assertEqual(chlink.load_session(now=999.0)["token"], TOKEN)
        self.assertEqual(chlink.load_session(now=1000.0), {})
        self.assertEqual(self.secret_store, {})

    def test_parse_expiry(self):
        self.assertEqual(chlink.parse_expiry("2099-01-02T03:04:05Z"), EXPIRY_TS)
        self.assertEqual(chlink.parse_expiry("2099-01-02T03:04:05+00:00"), EXPIRY_TS)
        self.assertEqual(chlink.parse_expiry(EXPIRY_TS), EXPIRY_TS)
        self.assertEqual(chlink.parse_expiry(int(EXPIRY_TS * 1000)), EXPIRY_TS)
        self.assertIsNone(chlink.parse_expiry(None))
        self.assertIsNone(chlink.parse_expiry("soon"))

    def test_unlink_while_signed_out_of_companion_logs_out_directly_and_deletes(self):
        app, cl, _ = self.link()
        app.cloud.session = None
        sent = []
        with mock.patch.object(chlink, "_http", lambda m, u, h, b, timeout=20.0: sent.append((m, u, h.get("Authorization"))) or (200, b"{}")):
            cl.unlink()
        self.assertEqual(sent, [("POST", chlink.API + "/v2/auth/logout", "Bearer " + TOKEN)])
        self.assertEqual(self.secret_store, {})
        self.assertEqual(app.ch_account_id(), 0)

    def test_failed_unlink_keeps_the_session(self):
        app, cl, _ = self.link()
        cl.app.cloud.http = lambda *a, **k: (500, b"{}")
        with self.assertRaises(ChLinkError):
            cl.unlink()
        self.assertIn(chlink.SECRET_NAME, self.secret_store)

    def test_companion_sign_out_ends_and_deletes_the_session_but_keeps_the_link(self):
        app, cl, _ = self.link()
        sent = []
        with mock.patch.object(chlink, "_http", lambda m, u, h, b, timeout=20.0: sent.append((u, h.get("Authorization"))) or (200, b"{}")):
            cl.discard_session()
        self.assertEqual(sent, [(chlink.API + "/v2/auth/logout", "Bearer " + TOKEN)])
        self.assertEqual(self.secret_store, {})
        self.assertTrue(app.settings()["ch_verified"])
        sent.clear()
        with mock.patch.object(chlink, "_http", lambda *a, **k: sent.append(1) or (200, b"{}")):
            cl.discard_session()                                          # nothing kept: no network call
        self.assertEqual(sent, [])
        self.assertNotIn(TOKEN, "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
