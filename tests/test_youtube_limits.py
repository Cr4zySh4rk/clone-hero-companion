"""YouTube lookups per chart, the global cool-down and what does (and does not) count as YouTube limiting us
(yt-dlp is mocked; the real format lists and messages are in tests/fixtures/youtube)."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chcompanion import ingame, youtube
from chcompanion.jobs import Job, JobManager

FULL = [  # a normal answer: HD video-only + audio-only streams
    {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080,
     "url": "https://v/1", "protocol": "https", "ext": "mp4"},
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "url": "https://a/1", "protocol": "https",
     "ext": "m4a", "abr": 128},
]
CUT_DOWN = [{"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "width": 640, "height": 360,
             "url": "https://m/1", "protocol": "https", "ext": "mp4"}]


class FakeYDL:
    calls: list = []
    formats = FULL

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        FakeYDL.calls.append(url)
        if url.startswith("ytsearch"):
            return {"entries": [{"id": f"vid{i}", "title": f"Artist - Song official music video {i}",
                                 "duration": 200, "channel": "ArtistVEVO"} for i in range(3)]}
        fm = FakeYDL.formats
        sel = self.opts.get("format")
        if not callable(sel):
            return {"formats": fm}
        picked = list(sel({"formats": fm}))
        if not picked:
            raise Exception("Requested format is not available")
        return {"requested_downloads": [dict(f, filepath=f"/tmp/x.f{f['format_id']}.{f['ext']}") for f in picked]}


def song():
    return SimpleNamespace(key="artist - song", artist="Artist", name="Song", display="Artist - Song",
                           length_s=200)


class Store:
    def __init__(self):
        self.d = {}

    def song(self, key):
        return dict(self.d.get(key, {}))

    def update_song(self, key, **kw):
        self.d.setdefault(key, {}).update(kw)


class Base(unittest.TestCase):
    def setUp(self):
        FakeYDL.calls, FakeYDL.formats = [], FULL
        patches = [mock.patch("yt_dlp.YoutubeDL", FakeYDL),
                   mock.patch.object(youtube, "GATE", youtube.RateGate(2, 0.0)),
                   mock.patch.object(youtube, "_ydl_opts", lambda clients=None, **kw: dict(kw)),
                   mock.patch.object(youtube, "js_runtime", return_value=("deno", "/bin/deno"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class LookupCountTests(Base):
    def test_search_is_remembered_per_song(self):
        yt = youtube.YouTube()
        yt.search(song())
        yt.search(song())
        self.assertEqual(len(FakeYDL.calls), 1)

    def test_fetch_is_one_lookup_with_video_and_audio(self):
        v, fmt, a = youtube.YouTube().fetch("vid0", "/tmp/video", with_audio=True)
        self.assertEqual(len(FakeYDL.calls), 1)
        self.assertTrue(v.endswith(".mp4") and a.endswith(".m4a"))

    def test_merged_download_is_split_not_looked_up_again(self):
        merged = {"format_id": "137+140", "vcodec": "avc1", "acodec": "mp4a", "width": 1920, "height": 1080,
                  "ext": "mp4", "filepath": "/tmp/m.mp4"}
        with mock.patch.object(FakeYDL, "extract_info", lambda self, url, download=True: (
                FakeYDL.calls.append(url), list(self.opts["format"]({"formats": FULL})),
                {"requested_downloads": [merged]})[-1]), \
                mock.patch.object(youtube, "_split_merged", return_value=("/tmp/m.v.mp4", "/tmp/m.audio.mka")):
            v, fmt, a = youtube.YouTube().fetch("vid0", "/tmp/video", with_audio=True)
        self.assertEqual((v, a, len(FakeYDL.calls)), ("/tmp/m.v.mp4", "/tmp/m.audio.mka", 1))

    def _download_video(self, yt, store, sg=None):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(ingame, "cache_dir", return_value=Path(tmp)), \
                mock.patch.object(ingame, "cached_video", return_value=None):
            return ingame.download_video(sg or song(), yt, store)

    def test_reduced_answer_is_not_limiting_and_costs_one_lookup_per_candidate(self):
        FakeYDL.formats = CUT_DOWN
        yt = youtube.YouTube()
        with self.assertRaises(ingame.InGameError) as cm:
            self._download_video(yt, Store())
        watches = [c for c in FakeYDL.calls if "watch?v=" in c]
        self.assertEqual(len(watches), 3)            # three candidates, each looked up exactly once
        self.assertEqual(youtube.GATE.remaining(), 0)    # no cool-down: YouTube did not limit anything
        self.assertEqual(youtube.GATE.strikes, 0)
        self.assertIn("no video streams offered", str(cm.exception))
        self.assertNotIn("limiting", str(cm.exception))

    def test_install_all_of_ten_charts_looks_up_each_chart_once(self):
        store = Store()
        fake = mock.Mock()
        fake.search.side_effect = lambda s: [{"id": f"id-{s.key}", "score": 9.0}]
        fake.fetch.side_effect = lambda vid, dest, with_audio=False, progress=None: (_ for _ in ()).throw(
            youtube.NoHDVideo("x"))
        for i in range(10):
            s = song()
            s.key = f"a - s{i}"
            s.name = f"S{i}"
            with self.assertRaises(ingame.InGameError):
                self._download_video(fake, store, s)
        self.assertEqual(fake.search.call_count, 10)
        self.assertEqual(fake.fetch.call_count, 10)


class BackoffTests(Base):
    def _manager(self, runner):
        m = JobManager(runner, workers=1)
        self.addCleanup(lambda: setattr(m, "target_workers", 0))
        return m

    def _wait(self, job, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end and not pred(job):
            time.sleep(0.05)
        self.assertTrue(pred(job), (job.status, job.step, job.error))

    def test_job_waits_for_cooldown_then_succeeds(self):
        youtube.GATE.BACKOFF = [1, 1, 1]
        attempts = []

        def runner(job, progress):
            progress("searching YouTube")
            attempts.append(1)
            if len(attempts) == 1:
                raise youtube.BotDetected("HTTP Error 429: Too Many Requests")
            return "done it"

        m = self._manager(runner)
        m.start()
        job = m.submit(Job("full", "Artist - Song"))
        self._wait(job, lambda j: j.status == "waiting" and "waiting for YouTube cool-down 0:" in j.step)
        self._wait(job, lambda j: j.status == "done")
        self.assertEqual((len(attempts), job.result), (2, "done it"))

    def test_fails_only_after_cooldowns_are_used_up(self):
        youtube.GATE.BACKOFF = [1, 1, 1]

        def runner(job, progress):
            progress("downloading the video")
            raise youtube.BotDetected("HTTP Error 429: Too Many Requests")

        m = self._manager(runner)
        m.start()
        job = m.submit(Job("full", "Artist - Song"))
        self._wait(job, lambda j: j.status == "failed")
        self.assertTrue(job.error.startswith("downloading the video: "), job.error)
        self.assertIn("YouTube kept limiting requests after 3 cool-down(s)", job.error)

    def test_media_and_full_for_one_chart_are_one_job(self):
        m = JobManager(lambda j, p: "x", workers=1)
        a = m.submit(Job("media", "A", song_id="s1"))
        b = m.submit(Job("full", "A", song_id="s1"))
        self.assertIs(a, b)
        self.assertEqual(a.kind, "full")

    def test_cooldowns_escalate_once_per_pause(self):
        g = youtube.RateGate(2, 0.0)
        g.throttled()
        g.throttled()               # in-flight requests failing together are one strike
        self.assertEqual((g.strikes, g.BACKOFF), (1, [120, 300, 900, 1800]))
        g.paused_until = 0
        g.throttled()
        self.assertGreater(g.remaining(), 299)


class JsRuntimeTests(unittest.TestCase):
    def test_message_and_status_without_runtime(self):
        with mock.patch.object(youtube, "_find_tool", return_value=None):
            self.assertIsNone(youtube.js_runtime())
            self.assertEqual(youtube.GATE.status()["jsRuntime"], None)
        self.assertIn("install Deno from deno.land", youtube.JS_HELP)

    def test_misses_are_not_cached(self):
        youtube._TOOL_CACHE.pop("deno", None)
        with mock.patch("chcompanion.youtube.shutil.which", return_value=None):
            youtube._find_tool("deno")
        self.assertNotIn("deno", youtube._TOOL_CACHE)
        youtube._TOOL_CACHE.pop("deno", None)

    def test_doctor_prints_js_runtime(self):
        from chcompanion import cli
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(youtube, "_find_tool", return_value=None), \
                mock.patch("builtins.print") as pr:
            cli.cmd_doctor(SimpleNamespace(data_dir=tmp, songs_dir=None, report=None))
        text = pr.call_args[0][0]
        self.assertIn("JS runtime  : none - " + youtube.JS_HELP, text)


if __name__ == "__main__":
    unittest.main()
