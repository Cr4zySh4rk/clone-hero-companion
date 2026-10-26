"""What counts as YouTube limiting us, built from format lists and messages captured live from yt-dlp
2026.10.21 (tests/fixtures/youtube). Only real throttling starts the shared cool-down."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import ingame, youtube

from test_youtube_limits import FULL, FakeYDL, Store, song

FIX = Path(__file__).parent / "fixtures" / "youtube"
LISTS = json.loads((FIX / "formats.json").read_text("utf-8"))["lists"]
ERRORS = json.loads((FIX / "errors.json").read_text("utf-8"))["errors"]
CLIENTS = json.loads((FIX / "clients.json").read_text("utf-8"))["clients"]


def err(name):
    return Exception(ERRORS[name]["message"])


class FormatListTests(unittest.TestCase):
    def test_clients_without_a_po_token_give_full_lists_the_rest_are_reduced(self):
        for client in ("visionos", "tv_samsung", "web_embedded"):
            self.assertEqual(youtube.list_state(LISTS[client]), "full", client)
            self.assertIsNotNone(youtube.pick_format(LISTS[client]), client)
        for client in ("web", "mweb", "android", "android_vr", "tv_simply"):
            self.assertEqual(youtube.list_state(LISTS[client]), "reduced", client)
            self.assertIsNone(youtube.pick_format(LISTS[client]), client)

    def test_reduced_list_with_muxed_hd_is_still_usable(self):
        self.assertEqual(youtube.list_state(LISTS["web_safari"]), "reduced")
        self.assertIsNotNone(youtube.pick_format(LISTS["web_safari"]))

    def test_storyboards_alone_or_nothing_is_empty(self):
        story = [f for f in LISTS["web"] if f.get("protocol") == "mhtml"]
        self.assertTrue(story)
        self.assertEqual(youtube.list_state(story), "empty")
        self.assertEqual(youtube.list_state([]), "empty")

    def test_fallback_clients_exist_and_need_no_po_token(self):
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        for clients in youtube.CLIENT_FALLBACKS:
            for c in clients or []:
                self.assertIn(c, INNERTUBE_CLIENTS, f"{c} is gone from yt-dlp (it would be silently ignored)")
                pol = INNERTUBE_CLIENTS[c].get("GVS_PO_TOKEN_POLICY", {})
                self.assertFalse(any(p.required for p in pol.values()), f"{c} needs a PO token: reduced lists only")

    def test_captured_client_matrix_matches_the_fixtures(self):
        for client in ("visionos", "tv_samsung", "web_embedded"):
            self.assertGreater(CLIENTS[f"{client}|js"]["video_only"], 0)
        for client in ("web", "mweb", "android", "android_vr", "tv_simply"):
            self.assertEqual(CLIENTS[f"{client}|js"]["video_only"], 0)
        self.assertGreater(CLIENTS["visionos|nojs"]["video_only"], 0)       # works without a JS runtime
        self.assertIn("error", CLIENTS["web_safari|nojs"])


class MessageTests(unittest.TestCase):
    def test_only_real_throttling_is_limiting(self):
        for name in ("bot_check", "http_429", "rate_limited_by_youtube"):
            self.assertTrue(youtube.is_bot_error(err(name)), name)
            self.assertIsInstance(youtube.classify_error(err(name)), youtube.BotDetected, name)

    def test_age_gate_is_not_the_bot_check(self):
        for name in ("age_gate_embedding_disabled", "age_gate_no_js_runtime"):
            e = err(name)
            self.assertIn("Sign in to confirm your age", str(e))
            self.assertFalse(youtube.is_bot_error(e), name)
            c = youtube.classify_error(e)
            self.assertIsInstance(c, youtube.AgeRestricted, name)
            self.assertTrue(str(c).startswith("Sign in to confirm your age"), str(c))
            self.assertNotIn("cookies", str(c))

    def test_other_errors_keep_their_own_message(self):
        c = youtube.classify_error(err("unavailable"))
        self.assertIsInstance(c, youtube.VideoUnavailable)
        self.assertEqual(str(c), "Video unavailable. This video is not available")
        for name in ("googlevideo_403", "requested_format_not_available_no_formats"):
            self.assertFalse(youtube.is_bot_error(err(name)), name)
            e = err(name)
            self.assertIs(youtube.classify_error(e), e)

    def test_requested_format_error_after_a_reduced_list_says_so(self):
        c = youtube.classify_error(err("requested_format_not_available_no_formats"), "reduced", 4)
        self.assertIsInstance(c, youtube.ReducedFormats)
        self.assertIn("4 formats", str(c))
        c = youtube.classify_error(err("requested_format_not_available_no_formats"), "sd", 17)
        self.assertIsInstance(c, youtube.NoHDVideo)


class Fy(FakeYDL):
    """FakeYDL with a per-video answer: a format list, or an exception to raise."""
    answers: dict = {}

    def extract_info(self, url, download=True):
        vid = url.rsplit("=", 1)[-1]
        a = Fy.answers.get(vid)
        if isinstance(a, Exception):
            FakeYDL.calls.append(url)
            raise a
        if isinstance(a, list):
            FakeYDL.formats = a
        else:
            FakeYDL.formats = FULL
        return FakeYDL.extract_info(self, url, download)


class FlowTests(unittest.TestCase):
    def setUp(self):
        FakeYDL.calls, FakeYDL.formats, Fy.answers = [], FULL, {}
        for f in ("/tmp/x.f136.mp4", "/tmp/x.f140.m4a", "/tmp/x.f137.mp4"):
            Path(f).write_bytes(b"\0" * 32)
            self.addCleanup(Path(f).unlink, True)
        for p in (mock.patch("yt_dlp.YoutubeDL", Fy), mock.patch.object(youtube, "GATE", youtube.RateGate(2, 0.0)),
                  mock.patch.object(youtube, "_ydl_opts", lambda clients=None, **kw: dict(kw))):
            p.start()
            self.addCleanup(p.stop)

    def run_video(self, store=None):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(ingame, "cache_dir", return_value=Path(tmp)), \
                mock.patch.object(ingame, "cached_video", return_value=None), \
                mock.patch.object(ingame, "per_manual_offset", return_value=True), \
                mock.patch.object(ingame, "looks_static", return_value=False), \
                mock.patch.object(ingame, "PREFER_WEBM", False), \
                mock.patch.object(ingame, "_to_h264"), mock.patch.object(ingame, "probe_codec", return_value="h264"), \
                mock.patch.dict("os.environ", {"CHC_TARGET_OS": "darwin"}), \
                mock.patch.object(ingame, "mp4_needs_remux", return_value=False):
            return ingame.download_video(song(), youtube.YouTube(), store or Store())

    def watches(self):
        return [c for c in FakeYDL.calls if "watch?v=" in c]

    def test_age_restricted_candidate_is_skipped_without_a_cooldown(self):
        Fy.answers = {"vid0": err("age_gate_embedding_disabled")}
        out = self.run_video()
        self.assertTrue(str(out).endswith(".mp4"))
        self.assertEqual([c.rsplit("=", 1)[-1] for c in self.watches()], ["vid0", "vid1"])
        self.assertEqual((youtube.GATE.remaining(), youtube.GATE.strikes), (0, 0))

    def test_every_candidate_age_restricted_reports_that_and_remembers_nothing(self):
        Fy.answers = {f"vid{i}": err("age_gate_no_js_runtime") for i in range(3)}
        store = Store()
        with self.assertRaises(ingame.InGameError) as cm:
            self.run_video(store)
        self.assertIn("age-restricted: Sign in to confirm your age", str(cm.exception))
        self.assertNotIn("limiting", str(cm.exception))
        self.assertEqual(len(self.watches()), 3)
        self.assertEqual((youtube.GATE.remaining(), youtube.GATE.strikes), (0, 0))
        self.assertFalse(any(v.get("video_failed") or v.get("failed") for v in store.d.values()))

    def test_unavailable_candidate_shows_youtubes_message(self):
        Fy.answers = {f"vid{i}": err("unavailable") for i in range(3)}
        with self.assertRaises(ingame.InGameError) as cm:
            self.run_video()
        self.assertIn("unavailable: Video unavailable. This video is not available", str(cm.exception))
        self.assertEqual(youtube.GATE.remaining(), 0)

    def test_reduced_real_list_is_one_lookup_per_candidate_and_no_pause(self):
        Fy.answers = {f"vid{i}": LISTS["web"] for i in range(3)}
        with self.assertRaises(ingame.InGameError) as cm:
            self.run_video()
        self.assertEqual(len(self.watches()), 3)
        self.assertEqual((youtube.GATE.remaining(), youtube.GATE.strikes), (0, 0))
        self.assertIn("no separate video streams (5 formats listed)", str(cm.exception))

    def test_real_full_list_downloads_with_one_lookup(self):
        Fy.answers = {"vid0": LISTS["visionos"]}
        self.run_video()
        self.assertEqual(len(self.watches()), 1)

    def test_bot_check_still_pauses_everything_and_stops_the_chart(self):
        Fy.answers = {"vid0": err("bot_check")}
        with self.assertRaises(youtube.BotDetected):
            self.run_video()
        self.assertEqual(len(self.watches()), 1)
        self.assertGreater(youtube.GATE.remaining(), 0)
        self.assertEqual(youtube.GATE.strikes, 1)

    def test_http_429_during_the_download_pauses(self):
        Fy.answers = {"vid0": err("http_429")}
        with self.assertRaises(youtube.BotDetected):
            self.run_video()
        self.assertGreater(youtube.GATE.remaining(), 0)

    def test_403_from_googlevideo_is_a_failed_download_not_limiting(self):
        Fy.answers = {f"vid{i}": err("googlevideo_403") for i in range(3)}
        with self.assertRaises(ingame.InGameError) as cm:
            self.run_video()
        self.assertIn("download failed: ERROR: unable to download video data: HTTP Error 403", str(cm.exception))
        self.assertEqual(youtube.GATE.remaining(), 0)

    def test_gate_slot_age_error_is_not_a_strike(self):
        with self.assertRaises(Exception) as cm:
            with youtube.GATE.slot():
                raise err("age_gate_embedding_disabled")
        self.assertNotIsInstance(cm.exception, youtube.BotDetected)
        self.assertEqual((youtube.GATE.remaining(), youtube.GATE.strikes), (0, 0))


if __name__ == "__main__":
    unittest.main()
