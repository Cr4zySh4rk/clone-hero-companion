import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import telemetry
from chcompanion.telemetry import Telemetry, clean_props


class Sink:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, headers, body, timeout):
        if self.fail:
            raise OSError("offline")
        self.calls.append((url, headers, json.loads(body)))
        return 200


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CHC_NO_TELEMETRY": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, post):
        t = Telemetry(self.dir, url="https://x.invalid", key="k", post=post, version="2.8.0", locale=lambda: "en")
        self.addCleanup(t._stop.set)
        return t

    def test_env_disables(self):
        with mock.patch.dict(os.environ, {"CHC_NO_TELEMETRY": "1"}):
            sink = Sink(); t = self.make(sink)
            t.apply(True, True); t.event("view", name="library")
            self.assertFalse(t.active); self.assertEqual(t.queue, [])

    def test_not_active_before_notice_or_when_off(self):
        t = self.make(Sink()); t.apply(True, False); t.event("view", name="library")
        self.assertEqual(t.queue, [])
        t.apply(False, True); t.event("view", name="library")
        self.assertEqual(t.queue, [])

    def test_allow_list_drops_unknown_events_and_props(self):
        self.assertIsNone(clean_props("song_title", {}))
        self.assertEqual(clean_props("view", {"name": "library", "title": "Song", "x": 1}), {"name": "library"})
        self.assertEqual(clean_props("view", {"name": "/home/me/Songs"}), {})        # not in the enum
        self.assertEqual(clean_props("favourite", {"on": "yes"}), {})                # must be a bool
        self.assertEqual(clean_props("app_quit", {"session_s": True}), {})           # bool is not a count
        self.assertEqual(clean_props("download_chart", {"source": "encore", "artist": "A"}), {"source": "encore"})

    def test_queue_flush_and_payload_is_anonymous(self):
        sink = Sink(); t = self.make(sink)
        t.apply(True, True)
        t.event("view", name="settings", path="/secret")
        self.assertTrue(t.flush())
        rows = sink.calls[0][2]["events"]
        self.assertEqual([r["name"] for r in rows], ["app_start", "view"])
        self.assertEqual(rows[1]["props"], {"name": "settings"})
        self.assertEqual(set(rows[1]), {"source", "install_id", "session_id", "name", "props", "app_version", "os", "locale"})
        self.assertTrue(sink.calls[0][0].endswith("/rest/v1/rpc/track"))
        self.assertEqual(t.queue, [])

    def test_install_id_is_random_and_persisted(self):
        a = self.make(Sink()).install_id()
        b = self.make(Sink()).install_id()
        self.assertEqual(a, b)
        self.assertEqual((self.dir / "telemetry_id").read_text().strip(), a)

    def test_offline_events_persist_and_resend(self):
        t = self.make(Sink(fail=True)); t.apply(True, True)
        t.event("launch_game")
        self.assertFalse(t.flush())
        saved = json.loads((self.dir / "telemetry_queue.json").read_text())
        self.assertEqual([r["name"] for r in saved], ["app_start", "launch_game"])
        sink = Sink(); t2 = self.make(sink); t2.apply(True, True)
        self.assertTrue(t2.flush())
        names = [r["name"] for r in sink.calls[0][2]["events"]]
        self.assertEqual(names[:2], ["app_start", "launch_game"])
        self.assertFalse((self.dir / "telemetry_queue.json").exists())

    def test_queue_is_capped(self):
        t = self.make(Sink()); t.apply(True, True)
        for _ in range(telemetry.MAX_QUEUE + 50):
            t.event("job_cancel")
        self.assertEqual(len(t.queue), telemetry.MAX_QUEUE)

    def test_opt_out_clears_queue_and_sends_nothing(self):
        sink = Sink(fail=True); t = self.make(sink); t.apply(True, True)
        t.event("launch_game"); t.flush()
        self.assertTrue((self.dir / "telemetry_queue.json").exists())
        t.apply(False, True)
        self.assertEqual(t.queue, []); self.assertFalse(t.active)
        self.assertFalse((self.dir / "telemetry_queue.json").exists())
        t.event("launch_game"); t.shutdown()
        self.assertEqual(sink.calls, []); self.assertEqual(t.queue, [])

    def test_event_never_raises(self):
        t = self.make(Sink()); t.apply(True, True)
        t.locale = lambda: 1 / 0
        t.event("view", name="library")


if __name__ == "__main__":
    unittest.main()
