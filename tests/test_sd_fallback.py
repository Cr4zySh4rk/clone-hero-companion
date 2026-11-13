"""SD fallback when no HD video exists, and the checks done on the stream list before anything is downloaded
(yt-dlp and the candidate search are mocked; the stream lists are in tests/fixtures/youtube/precheck.json)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chcompanion import ingame, youtube
from chcompanion.i18n import _

CASES = json.loads((Path(__file__).parent / "fixtures" / "youtube" / "precheck.json").read_text("utf-8"))["cases"]


def song():
    return SimpleNamespace(key="artist - song", artist="Artist", name="Song", display="Artist - Song", length_s=200)


class Store:
    def __init__(self):
        self.d = {}

    def song(self, key):
        return dict(self.d.get(key, {}))

    def update_song(self, key, **kw):
        e = self.d.setdefault(key, {})
        for k, v in kw.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v


class HeuristicTests(unittest.TestCase):
    def test_no_stream_of_a_real_music_video_looks_like_a_still(self):
        for f in CASES["music_video_hd"]["formats"]:
            self.assertIsNone(youtube.still_reason(f), f["format_id"])

    def test_recorded_and_modelled_stills_are_caught(self):
        for name, c in CASES.items():
            if c["expect_still"]:
                v = youtube.pick_format(c["formats"], 720) or youtube.pick_sd(c["formats"], 480)
                self.assertTrue(youtube.still_reason(v), name)

    def test_sd_only_music_video_is_not_a_still_and_has_480p(self):
        c = CASES["sd_only_640x480"]
        self.assertIsNone(youtube.pick_format(c["formats"], 720))
        sd = youtube.pick_sd(c["formats"], 480)
        self.assertEqual((sd["res_class"], youtube.still_reason(sd)), (480, None))
        self.assertIsNone(youtube.pick_sd([f for f in c["formats"] if f["height"] != 480], 480))   # 360p is below the floor
        self.assertEqual(youtube.pick_sd([f for f in c["formats"] if f["height"] != 480], 360)["res_class"], 360)

    def test_real_music_video_bits_per_pixel_stay_far_above_the_threshold(self):
        worst = min(f["tbr"] * 1000 / (f["width"] * f["height"] * 25) for f in CASES["music_video_hd"]["formats"])
        self.assertGreater(worst, youtube.STILL_MIN_BPP * 4)

    def test_strong_official(self):
        s = SimpleNamespace(artist="Three Days Grace", name="Animal I Have Become", length_s=231)
        official = {"title": "Three Days Grace - Animal I Have Become (Official Video)", "channel": "Three Days Grace",
                    "duration": 231}
        lyric = {"title": "Three Days Grace - Animal I Have Become (Lyrics)", "channel": "7clouds", "duration": 232}
        reup = {"title": "Animal I Have Become (Official Music Video) [FULL HD]", "channel": "Somebody", "duration": 231}
        vevo = {"title": "Three Days Grace - Animal I Have Become (Official Music Video)", "channel": "ThreeDaysGraceVEVO", "duration": 231}
        self.assertTrue(youtube.is_strong_official(official, s))
        self.assertTrue(youtube.is_strong_official(vevo, s))
        self.assertFalse(youtube.is_strong_official(lyric, s))
        self.assertFalse(youtube.is_strong_official(reup, s))          # artist is not in the title or channel: score too low
        self.assertFalse(youtube.is_strong_official(dict(official, duration=400), s))   # wrong length


class FakeYDL:
    """yt-dlp: the selector sees the formats; anything it yields counts as a download."""
    formats: list = []
    calls: list = []
    downloaded: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        FakeYDL.calls.append(url)
        picked = list(self.opts["format"]({"formats": FakeYDL.formats}))
        if not picked:
            raise Exception("Requested format is not available")
        FakeYDL.downloaded += [f["format_id"] for f in picked]
        return {"requested_downloads": [dict(f, filepath=f"/tmp/x.f{f['format_id']}.mp4") for f in picked]}


class PreCheckTests(unittest.TestCase):
    def setUp(self):
        FakeYDL.calls, FakeYDL.downloaded = [], []
        for p in (mock.patch("yt_dlp.YoutubeDL", FakeYDL),
                  mock.patch.object(youtube, "GATE", youtube.RateGate(2, 0.0)),
                  mock.patch.object(youtube, "_ydl_opts", lambda clients=None, **kw: dict(kw))):
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, case, sd_min_p=480, take_sd=False):
        FakeYDL.formats = CASES[case]["formats"]
        return youtube.YouTube(sd_min_p=sd_min_p).fetch("vid", "/tmp/video", take_sd=take_sd)

    def test_still_image_is_skipped_without_downloading(self):
        for case in ("still_low_bitrate_1080p", "still_one_fps", "square_album_art"):
            with self.assertRaises(youtube.StillImage):
                self.fetch(case)
        self.assertEqual(FakeYDL.downloaded, [])
        self.assertEqual(len(FakeYDL.calls), 3)                    # one lookup each, nothing else

    def test_no_hd_reports_the_sd_it_has_and_downloads_nothing(self):
        with self.assertRaises(youtube.NoHDVideo) as cm:
            self.fetch("sd_only_640x480")
        self.assertEqual((cm.exception.sd_p, cm.exception.max_p), (480, 480))
        self.assertEqual(FakeYDL.downloaded, [])

    def test_sd_not_wanted_reports_no_sd(self):
        with self.assertRaises(youtube.NoHDVideo) as cm:
            self.fetch("sd_only_640x480", sd_min_p=0)
        self.assertEqual(cm.exception.sd_p, 0)

    def test_below_the_chosen_floor_is_no_sd(self):
        formats = [f for f in CASES["sd_only_640x480"]["formats"] if f["height"] <= 360]
        FakeYDL.formats = formats
        with self.assertRaises(youtube.NoHDVideo) as cm:
            youtube.YouTube(sd_min_p=480).fetch("vid", "/tmp/video", take_sd=True)
        self.assertEqual(cm.exception.sd_p, 0)
        self.assertEqual(FakeYDL.downloaded, [])

    def test_take_sd_downloads_the_best_sd_stream(self):
        FakeYDL.formats = CASES["sd_only_640x480"]["formats"]
        with mock.patch("chcompanion.youtube.Path.unlink"):
            v, fmt, a = youtube.YouTube(sd_min_p=480).fetch("vid", "/tmp/video", take_sd=True)
        self.assertEqual(FakeYDL.downloaded, ["135"])
        self.assertTrue(fmt["sd"])

    def test_a_still_sd_stream_is_not_taken_either(self):
        with self.assertRaises(youtube.StillImage):
            self.fetch("still_480p_sd_only", take_sd=True)
        self.assertEqual(FakeYDL.downloaded, [])

    def test_hd_music_video_downloads_as_before(self):
        v, fmt, a = self.fetch("music_video_hd")
        self.assertEqual(FakeYDL.downloaded, ["136"])
        self.assertFalse(fmt["sd"])


def cand(i, score, title=None, channel="Someone"):
    return {"id": i, "score": score, "title": title or f"Song {i}", "channel": channel, "duration": 200}


class FlowTests(unittest.TestCase):
    """ingame.download_video with the YouTube object mocked: which candidates are looked up, what is downloaded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(ingame, "cache_dir", return_value=Path(self.tmp)),
                  mock.patch.object(ingame, "cached_video", return_value=None),
                  mock.patch.object(ingame, "looks_static", return_value=False),
                  mock.patch.object(ingame, "auto_offset", return_value=(0.0, 0.0)),
                  mock.patch.object(ingame.GATE if hasattr(ingame, "GATE") else youtube.GATE, "wait_cooldown", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)

    def yt(self, cands, answers, sd_min_p=480):
        """answers: id -> 'hd' | ('sd', p) | 'nohd' | 'still'; a take_sd fetch of an SD video downloads it."""
        yt = mock.Mock()
        yt.sd_min_p = sd_min_p
        yt.search.return_value = cands
        yt.downloads = []

        def fetch(vid, dest, with_audio=False, progress=None, take_sd=False):
            a = answers[vid]
            if a == "still":
                raise youtube.StillImage("x")
            if a == "hd" or (isinstance(a, tuple) and take_sd):
                res = 720 if a == "hd" else a[1]
                yt.downloads.append(vid)
                p = Path(dest + ".f1.webm")
                p.write_bytes(b"x")
                return str(p), {"vcodec": "vp9", "res_class": res, "sd": res < 640, "format_id": "1"}, None
            raise youtube.NoHDVideo("no hd", sd_p=a[1] if isinstance(a, tuple) and sd_min_p else 0)
        yt.fetch.side_effect = fetch
        return yt

    def run_dl(self, yt, store=None, sg=None):
        store = store or Store()
        path = ingame.download_video(sg or song(), yt, store, convert=False)
        return path, store

    def test_strong_official_takes_sd_at_once_without_further_lookups(self):
        s = SimpleNamespace(key="k", artist="Artist", name="Song", display="Artist - Song", length_s=200)
        official = {"id": "a", "title": "Artist - Song (Official Video)", "channel": "Artist", "duration": 200}
        official["score"] = youtube.score_candidate(official, s)
        self.assertGreaterEqual(official["score"], youtube.STRONG_SCORE)
        yt = self.yt([official, cand("b", 8), cand("c", 7)], {"a": ("sd", 480), "b": "hd", "c": "hd"})
        path, store = self.run_dl(yt, sg=s)
        self.assertEqual([c.args[0] for c in yt.fetch.call_args_list], ["a"])      # no other lookup
        self.assertTrue(yt.fetch.call_args.kwargs["take_sd"])
        self.assertEqual(yt.downloads, ["a"])
        self.assertEqual((store.d["k"]["video_res"], store.d["k"]["video_sd"]), (480, True))

    def test_weak_candidates_keep_looking_for_hd_and_hd_wins(self):
        yt = self.yt([cand("a", 9), cand("b", 8)], {"a": ("sd", 480), "b": "hd"})
        path, store = self.run_dl(yt)
        self.assertEqual(yt.downloads, ["b"])
        self.assertNotIn("video_sd", store.d["artist - song"])
        self.assertEqual(store.d["artist - song"]["video_res"], 720)

    def test_best_sd_is_remembered_and_the_only_download(self):
        yt = self.yt([cand("a", 9), cand("b", 8), cand("c", 7), cand("d", 6)],
                     {"a": ("sd", 360), "b": ("sd", 480), "c": "nohd", "d": ("sd", 480)}, sd_min_p=360)
        path, store = self.run_dl(yt)
        calls = [(c.args[0], c.kwargs.get("take_sd", False)) for c in yt.fetch.call_args_list]
        self.assertEqual(calls[:4], [("a", False), ("b", False), ("c", False), ("d", False)])   # nothing downloaded here
        self.assertEqual(calls[4:], [("a", True)])         # highest score wins over resolution
        self.assertEqual(yt.downloads, ["a"])
        self.assertEqual(store.d["artist - song"]["video_sd"], True)
        self.assertEqual(store.d["artist - song"]["video_id"], "a")

    def test_same_score_prefers_the_higher_resolution(self):
        yt = self.yt([cand("a", 8), cand("b", 8)], {"a": ("sd", 360), "b": ("sd", 480)})
        self.run_dl(yt)
        self.assertEqual(yt.downloads, ["b"])

    def test_setting_off_is_todays_behaviour(self):
        yt = self.yt([cand("a", 9), cand("b", 8), cand("c", 7)], {"a": ("sd", 480), "b": ("sd", 480), "c": "nohd"}, sd_min_p=0)
        with self.assertRaises(ingame.InGameError):
            self.run_dl(yt)
        self.assertEqual(yt.fetch.call_count, 3)
        self.assertTrue(all("take_sd" not in c.kwargs for c in yt.fetch.call_args_list))
        self.assertEqual(yt.downloads, [])

    def test_a_still_candidate_is_skipped_and_the_search_goes_on(self):
        yt = self.yt([cand("a", 9), cand("b", 8)], {"a": "still", "b": "hd"})
        self.run_dl(yt)
        self.assertEqual(yt.downloads, ["b"])

    def test_nothing_usable_at_all_still_fails_with_the_reasons(self):
        yt = self.yt([cand("a", 9), cand("b", 8)], {"a": "still", "b": "nohd"})
        with self.assertRaises(ingame.InGameError):
            self.run_dl(yt)
        self.assertEqual(yt.downloads, [])

    def test_sd_video_is_not_replaced_again_and_is_marked_in_the_job_text(self):
        self.assertTrue(ingame.sd_is_final({"video_sd": True, "video_sd_at": __import__("time").time()}))
        self.assertFalse(ingame.sd_is_final({}))
        self.assertFalse(ingame.sd_is_final({"video_sd": True, "video_sd_at": 1.0}))     # long ago: look for HD again
        self.assertIn("SD 480p (no HD version exists)", _("job.video_saved_sd", res=480, file="video.webm"))
        self.assertIn("SD 480p (no HD version exists)", _("job.video_sng_sd", res=480))


if __name__ == "__main__":
    unittest.main()


class RealStreamListTests(unittest.TestCase):
    """Real YouTube stream lists recorded from a home connection (2026-11-13), URLs replaced."""

    @classmethod
    def setUpClass(cls):
        import json
        from pathlib import Path
        cls.lists = json.loads((Path(__file__).parent / "fixtures" / "youtube" / "real_lists.json").read_text("utf-8"))

    def best(self, vid):
        fm = self.lists[vid]["formats"]
        return youtube.pick_format(fm, 720, max_p=1080) or youtube.pick_sd(fm, 360)

    def test_official_sd_only_videos_get_their_sd_stream(self):
        self.assertIsNone(youtube.pick_format(self.lists["xqds0B_meys"]["formats"], 720, max_p=1080))
        self.assertEqual(youtube.pick_sd(self.lists["xqds0B_meys"]["formats"], 480)["height"], 480)   # Animal I Have Become
        self.assertIsNone(youtube.pick_sd(self.lists["0xyxtzD54rM"]["formats"], 480))
        self.assertEqual(youtube.pick_sd(self.lists["0xyxtzD54rM"]["formats"], 360)["height"], 360)   # Given Up (default 360p)

    def test_still_image_uploads_are_recognised_before_download(self):
        for vid in ("j7rLdd8EiJQ", "71blIL2Dnl8", "D93vYwuMeUw"):
            self.assertIsNotNone(youtube.still_reason(self.best(vid)), vid)

    def test_real_music_videos_are_not_called_still(self):
        for vid in ("xqds0B_meys", "0xyxtzD54rM", "FVovq9TGBw0", "tMDFv5m18Pw", "n9h7hU6_VI4"):
            self.assertIsNone(youtube.still_reason(self.best(vid)), vid)
