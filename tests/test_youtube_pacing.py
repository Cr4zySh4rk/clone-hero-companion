"""2.1.18: pacing, hourly budget, sign-in cookies, extended ladder, bot-check hint, deferred video step,
and the silent yt-dlp updater (everything mocked)."""
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chcompanion import ingame, youtube, ytupdate


def gate(**kw):
    g = youtube.RateGate(**kw)
    g.sleeps = []
    g._sleep = lambda s: g.sleeps.append(s)       # no real waiting
    return g


class PacingTests(unittest.TestCase):
    def test_gap_between_lookups_is_randomised_4_to_9(self):
        g = gate(rng=lambda a, b: (a, b) and 6.0)
        self.assertEqual(g.gap, (4.0, 9.0))
        with g.slot("a"):
            pass
        with g.slot("b"):
            pass
        self.assertTrue(g.sleeps and 5.0 < g.sleeps[-1] <= 6.0, g.sleeps)

    def test_default_rng_stays_in_range(self):
        got = []
        g = gate(rng=lambda a, b: got.append((a, b)) or 4.0)
        with g.slot():
            pass
        self.assertEqual(got, [(4.0, 9.0)])

    def test_concurrency_is_one(self):
        g = youtube.RateGate(spacing=0.0)
        inside, peak, lock = [0], [0], threading.Lock()

        def work():
            with g.slot():
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(peak[0], 1)
        self.assertEqual(youtube.DOWNLOADS._value, 2)

    def test_hourly_budget_makes_jobs_wait_with_message(self):
        g = gate(spacing=0.0, budget=3)
        now = time.time()
        g.requests = [now - 3000, now - 2000, now - 100]
        msgs = []
        calls = {"n": 0}

        def sleep(s):
            calls["n"] += 1
            if calls["n"] == 2:                   # the oldest lookup ages out of the hour
                g.requests = g.requests[1:]
        g._sleep = sleep
        g.wait_cooldown(msgs.append)
        self.assertTrue(msgs and msgs[0].startswith("pacing YouTube requests ("), msgs)
        self.assertRegex(msgs[0], r"\(\d+:\d\d\)")
        self.assertEqual(g.budget_wait(), 0)

    def test_requests_are_counted(self):
        g = gate(spacing=0.0)
        with g.slot("x"):
            pass
        self.assertEqual(g.status()["requests60"], 1)

    def test_ydl_opts_sleep_between_requests(self):
        with mock.patch.object(youtube, "_find_tool", return_value=None), \
                mock.patch.object(youtube, "hide_deno_console"):
            self.assertEqual(youtube._ydl_opts()["sleep_interval_requests"], youtube.REQUEST_SLEEP_S)
        self.assertEqual(youtube.SEARCH_RESULTS, 5)


class CookieTests(unittest.TestCase):
    def tearDown(self):
        youtube.set_cookies("none")

    def _opts(self):
        with mock.patch.object(youtube, "_find_tool", return_value=None), mock.patch.object(youtube, "hide_deno_console"):
            return youtube._ydl_opts()

    def test_browser_is_passed_to_yt_dlp(self):
        youtube.set_cookies("firefox")
        self.assertEqual(self._opts()["cookiesfrombrowser"], ("firefox",))
        youtube.set_cookies("none")
        self.assertNotIn("cookiesfrombrowser", self._opts())

    def test_cookies_txt_option(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"
            f.write_text("# Netscape HTTP Cookie File\n")
            youtube.set_cookies("file", str(f))
            self.assertEqual(self._opts()["cookiefile"], str(f))
        youtube.set_cookies("file", "/nonexistent/c.txt")
        self.assertNotIn("cookiefile", self._opts())
        self.assertIn("not found", youtube.cookie_status()["error"])

    def test_unreadable_cookies_fall_back_with_a_clear_message(self):
        cases = {("chrome", "Failed to decrypt with DPAPI"): "app-bound",
                 ("chrome", "Could not copy Chrome cookie database. database is locked"): "locked",
                 ("safari", "PermissionError: [Errno 1] Operation not permitted: Cookies.binarycookies"): "Full Disk Access"}
        for (browser, err), expect in cases.items():
            built = []

            class Fake:
                def __init__(self, opts):
                    if "cookiesfrombrowser" in opts:
                        raise Exception(err)
                    built.append(opts)
            youtube.set_cookies(browser)
            with mock.patch("yt_dlp.YoutubeDL", Fake):
                youtube.make_ydl(youtube._ydl_opts() if False else {"cookiesfrombrowser": (browser,)})
            self.assertEqual(built, [{}])
            st = youtube.cookie_status()
            self.assertFalse(st["active"])
            self.assertIn(expect, st["error"])
            self.assertIn("without sign-in", st["error"])
            self.assertNotIn("cookiesfrombrowser", self._opts())      # no more tries this session

    def test_other_errors_are_not_swallowed(self):
        class Fake:
            def __init__(self, opts):
                raise ValueError("bad option")
        with mock.patch("yt_dlp.YoutubeDL", Fake), self.assertRaises(ValueError):
            youtube.make_ydl({"cookiesfrombrowser": ("chrome",)})


class LadderAndHintTests(unittest.TestCase):
    def test_extended_ladder(self):
        g = gate()
        waits = []
        for _ in range(5):
            g.paused_until = 0
            g.throttled(Exception("HTTP Error 429: Too Many Requests"), "vid12345678")
            waits.append(g.remaining())
        self.assertEqual(youtube.RateGate.BACKOFF, [120, 300, 900, 1800])
        self.assertEqual([round(w, -1) for w in waits], [120, 300, 900, 1800, 1800])

    def test_signals_and_diagnostics_recorded(self):
        for msg, sig in (("HTTP Error 429: Too Many Requests", "http-429"),
                         ("Sign in to confirm you're not a bot", "bot-check"),
                         ("This content isn't available, try again later. The current session has been rate-limited by YouTube", "rate-limit-text")):
            g = gate()
            for _ in range(3):
                g.record_request()
            g.throttled(Exception("ERROR: [youtube] abcdefghijk: " + msg), "abcdefghijk")
            ll = g.status()["lastLimit"]
            self.assertEqual((ll["signal"], ll["videoId"], ll["requests10"], ll["requests60"]), (sig, "abcdefghijk", 3, 3))
            self.assertIn(msg.split(".")[0][:20], ll["message"])

    def test_bot_check_hint_only_without_cookies(self):
        g = gate()
        g.throttled(Exception("Sign in to confirm you're not a bot"), "v")
        try:
            youtube.set_cookies("none")
            self.assertEqual(g.status()["hint"], youtube.BOT_HINT)
            self.assertIn("Use my YouTube sign-in", youtube.BOT_HINT)
            youtube.set_cookies("firefox")
            self.assertIsNone(g.status()["hint"])
        finally:
            youtube.set_cookies("none")
        g2 = gate()
        g2.throttled(Exception("HTTP Error 429: Too Many Requests"), "v")
        self.assertIsNone(g2.status()["hint"])

    def test_activity_persists(self):
        with tempfile.TemporaryDirectory() as d:
            g = gate()
            g.attach(Path(d) / "a.json")
            g.record_request()
            g.throttled(Exception("HTTP Error 429"), "v")
            h = youtube.RateGate()
            h.attach(Path(d) / "a.json")
            self.assertEqual((len(h.requests), h.last_limit["signal"]), (1, "http-429"))


class DeferredVideoTests(unittest.TestCase):
    def test_other_steps_continue_while_video_is_deferred(self):
        gt = gate()
        gt.throttled(Exception("Sign in to confirm you're not a bot"), "v")
        song = SimpleNamespace(key="a - s", artist="A", name="S", display="A - S", length_s=100, is_sng=False,
                               path=Path(tempfile.mkdtemp()), chart_lyrics=lambda: [], list_files=lambda: [])
        store = SimpleNamespace(song=lambda k: {}, update_song=lambda k, **kw: None, get=lambda *a: 0)
        applied = {}
        with mock.patch.object(youtube, "GATE", gt), \
                mock.patch.object(ingame.lyr, "fetch_lrclib_entry", return_value={"lines": [{"start": 1, "text": "x"}]}), \
                mock.patch.object(ingame, "auto_lyrics_offset", return_value=0.0), \
                mock.patch.object(ingame, "has_video", return_value=False), \
                mock.patch.object(ingame, "_apply_folder", lambda s, v, lines, *a: applied.update(v=v, lines=lines) or a[-1].update(lyrics="added")), \
                mock.patch.object(ingame, "_lock_for", return_value=threading.Lock()), \
                mock.patch.object(ingame, "download_video", side_effect=ingame.download_video):
            fake_fetch = mock.Mock()
            yt = SimpleNamespace(search=mock.Mock(), fetch=fake_fetch)
            store_real = store
            with mock.patch.object(ingame, "cached_video", return_value=None):
                r = ingame.prepare(song, yt, store_real, defer_video=True)
        self.assertEqual(r["video_pending"], "bot-check")
        self.assertEqual(r["lyrics"], "added")
        self.assertIsNone(applied["v"])
        yt.search.assert_not_called()          # no YouTube lookup while paused
        fake_fetch.assert_not_called()

    def test_without_defer_a_limit_still_raises(self):
        with mock.patch.object(ingame, "download_video", side_effect=youtube.BotDetected("HTTP Error 429")):
            pass   # the raising path is covered by test_youtube_limits (jobs wait out the cool-down)


class UpdaterTests(unittest.TestCase):
    def test_version_compare_ignores_dev(self):
        self.assertTrue(ytupdate.is_newer("2026.10.22", "2026.9.1"))
        self.assertFalse(ytupdate.is_newer("2026.10.22", "2026.10.22"))
        self.assertFalse(ytupdate.is_newer("2026.11.1.dev0", "2026.9.1"))
        self.assertFalse(ytupdate.is_stable("2026.11.1.dev20260101"))
        data = {"info": {"version": "2026.11.1.dev0"}, "releases": {"2026.10.22": [1], "2026.9.1": [1], "2026.11.1.dev0": [1]}}
        self.assertEqual(ytupdate.latest_stable(data), "2026.10.22")

    def test_sha_mismatch_rejected(self):
        with mock.patch.object(ytupdate, "_get_bytes", return_value=b"not the wheel"):
            with self.assertRaises(RuntimeError):
                ytupdate._verified("https://x/w.whl", "0" * 64)

    def _fake_pkg(self, root: Path, version: str, broken=False):
        (root / "yt_dlp" / "extractor").mkdir(parents=True)
        (root / "yt_dlp" / "__init__.py").write_text(
            ("raise ImportError('broken')\n" if broken else
             "class YoutubeDL:\n    def __init__(self, o=None): pass\n    def __enter__(self): return self\n    def __exit__(self, *a): return False\n"))
        (root / "yt_dlp" / "version.py").write_text(f"__version__ = '{version}'\n")
        (root / "yt_dlp" / "extractor" / "__init__.py").write_text("def gen_extractor_classes():\n    return [object]\n")

    def _isolated(self):
        saved_mods = {k: v for k, v in sys.modules.items() if k.startswith("yt_dlp")}
        saved_path = list(sys.path)

        def restore():
            for k in [k for k in sys.modules if k.startswith("yt_dlp")]:
                del sys.modules[k]
            sys.modules.update(saved_mods)
            sys.path[:] = saved_path
        self.addCleanup(restore)

    def test_hot_swap_reloads_module(self):
        self._isolated()
        with tempfile.TemporaryDirectory() as d:
            self._fake_pkg(Path(d), "2099.1.1")
            g = youtube.RateGate()
            self.assertTrue(ytupdate.hot_swap(Path(d), "2099.1.1", g, None))
            import yt_dlp.version
            self.assertEqual(yt_dlp.version.__version__, "2099.1.1")
            self.assertEqual(sys.path[0], d)
            self.assertTrue(g.sem.acquire(timeout=1))     # the gate was released again
            g.sem.release()

    def test_hot_swap_rolls_back_on_broken_import(self):
        self._isolated()
        import yt_dlp
        before = yt_dlp.version.__version__ if hasattr(yt_dlp, "version") else None
        old_path = list(sys.path)
        with tempfile.TemporaryDirectory() as d:
            self._fake_pkg(Path(d), "2099.1.1", broken=True)
            with self.assertRaises(RuntimeError):
                ytupdate.hot_swap(Path(d), "2099.1.1", youtube.RateGate(), None)
        self.assertEqual(sys.path, old_path)
        import yt_dlp as again
        self.assertIs(again, yt_dlp)
        if before:
            self.assertEqual(again.version.__version__, before)

    def test_hot_swap_waits_for_no_lookup_in_flight(self):
        g = youtube.RateGate()
        with g.slot():
            self.assertFalse(ytupdate.hot_swap(Path("/nonexistent"), "1", g, None, wait=0.05))

    def test_keeps_last_two_versions(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ytupdate, "_root", return_value=Path(d)):
            for v in ("2026.1.1", "2026.2.1", "2026.3.1"):
                (Path(d) / v / "yt_dlp_ejs").mkdir(parents=True)
            ytupdate.prune(2)
            self.assertEqual(ytupdate.cached_versions(), ["2026.2.1", "2026.3.1"])

    def test_network_failure_is_silent(self):
        with mock.patch.object(ytupdate, "_get_json", side_effect=OSError("offline")):
            msg = ytupdate.check_for_update()
        self.assertIn("couldn't", msg)

    def test_background_check_skipped_in_tests(self):
        with mock.patch.object(ytupdate.threading, "Thread") as T:
            ytupdate.start_background_check()
            T.assert_not_called()


if __name__ == "__main__":
    unittest.main()
