"""2.6.0: accomplishments, friends' play statistics (mocked cloud, 403 path, cache), the stats-sharing switch,
and /api/share/save + /api/open for the Share buttons."""
import base64
import contextlib
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from chcompanion import api, playstats
from test_friends import BOB, EVE, Harness

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def play(i, day=0, score=1000, fc=False, stars=3, name="Song", inst="guitar", diff="expert", **kw):
    t = T0 + timedelta(days=day, minutes=i)
    return {"client_id": f"c{i}", "played_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "artist": "Band", "name": name,
            "instrument": inst, "difficulty": diff, "score": score, "fc": fc, "stars": stars, "duration_s": 100.0,
            "notes_hit": 90, "total_notes": 100, **kw}


def acc(plays, **kw):
    return playstats.compute(plays, now=T0 + timedelta(days=400), **kw)["accomplishments"]


class AccomplishmentTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(acc([]), [])

    def test_fc_five_star_and_pb_kinds_newest_first(self):
        a = acc([play(1, score=100), play(2, score=200, day=0), play(3, score=300, fc=True, stars=5, day=0)])
        kinds = [x["kind"] for x in a]
        self.assertEqual(kinds, ["fc", "pb"])          # one highlight per play: fc wins over five_star; the first play is no "new" best
        fc = a[0]
        self.assertEqual((fc["title"], fc["detail"]), ("profile.acc.fc", "profile.acc.fc_detail"))
        self.assertEqual((fc["vars"]["name"], fc["vars"]["diff"], fc["vars"]["inst"], fc["vars"]["score"]), ("Song", "expert", "guitar", 300))
        self.assertTrue(fc["ts"].startswith("2026-03-01"))

    def test_five_star_without_fc_and_repeat_fc_is_not_new(self):
        a = acc([play(1, stars=5), play(2, fc=True, score=500), play(3, fc=True, score=400)])
        self.assertEqual([x["kind"] for x in a], ["fc", "five_star"])      # the second FC of the same chart and the lower score add nothing

    def test_streak_and_play_milestones(self):
        plays = [play(i, day=i) for i in range(10)]        # 10 plays on 10 consecutive days
        a = acc(plays)
        by = {(x["kind"], x["vars"]["count"]) for x in a if x["kind"] in ("streak", "milestone")}
        self.assertIn(("milestone", 10), by)
        self.assertIn(("streak", 7), by)
        self.assertTrue(all(x["title"].startswith("profile.acc.") for x in a))
        self.assertEqual(sorted((x["ts"] for x in a), reverse=True), [x["ts"] for x in a])

    def test_limit_and_filters(self):
        plays = [play(i, day=i % 20, score=i * 10, name=f"S{i}", fc=True) for i in range(60)]
        self.assertEqual(len(acc(plays)), 6)
        mixed = [play(1, fc=True, inst="drums"), play(2, inst="guitar")]
        self.assertEqual(acc(mixed, instrument="guitar"), [])
        self.assertEqual(acc(mixed, instrument="drums")[0]["kind"], "fc")

    def test_every_title_key_is_in_the_catalog(self):
        from chcompanion import i18n
        plays = [play(i, day=i, fc=True, name=f"S{i}") for i in range(12)]
        for x in acc(plays):
            self.assertTrue(i18n.has(x["title"]) or i18n.has(x["title"] + ".other"), x["title"])
            self.assertTrue(i18n.has(x["detail"]), x["detail"])


def rows(n, start=0):
    return [play(start + i, day=i % 30, score=100 + i, name=f"S{i % 7}") for i in range(n)]


class FriendStatsTests(Harness):
    def call(self, path, q=None, body=None, method="GET"):
        sent = []
        h = types.SimpleNamespace(_json=lambda obj, code=200: sent.append((obj, code)))
        lm = mock.Mock()
        lm.hash_to_id.return_value, lm.key_to_id.return_value = {}, {}
        c = types.SimpleNamespace(friends=self.fr, cloud=self.cloud, libmgr=lm)
        api._friends(h, c, method, path, q or {}, body or {})
        return sent[0]

    def test_pages_by_1000_with_explicit_columns_and_cache(self):
        self.befriend(BOB)
        pages = []
        data = rows(1500)

        def route(q, b):
            pages.append(q)
            off = int(q["offset"])
            return 200, data[off:off + 1000]
        self.sb.routes[("GET", "plays")] = route
        out = self.fr.friend_plays(BOB)
        self.assertEqual(len(out), 1500)
        self.assertEqual([p["offset"] for p in pages], ["0", "1000"])
        self.assertEqual(pages[0]["user_id"], f"eq.{BOB}")
        self.assertEqual(pages[0]["limit"], "1000")
        self.assertNotIn("raw", pages[0]["select"].split(","))
        self.assertNotEqual(pages[0]["select"], "*")
        self.fr.friend_plays(BOB)
        self.assertEqual(len(pages), 2)                  # served from the 10-minute cache
        self.fr.plays[BOB] = (self.fr.plays[BOB][0] - 601, self.fr.plays[BOB][1])
        self.fr.friend_plays(BOB)
        self.assertEqual(len(pages), 4)                  # expired: fetched again

    def test_stats_route_returns_compute_plus_friend(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "plays")] = lambda q, b: (200, rows(12))
        d, code = self.call("/api/friends/stats", {"id": BOB, "instrument": "guitar", "difficulty": "expert"})
        self.assertEqual(code, 200)
        self.assertEqual(d["totals"]["plays"], 12)
        self.assertEqual(d["friend"]["username"], "bob")
        self.assertIn("accomplishments", d)
        self.assertIn("songId", d["mostPlayed"][0])
        d2, _ = self.call("/api/friends/stats", {"id": BOB, "instrument": "drums"})
        self.assertEqual(d2["totals"]["plays"], 0)

    def test_403_not_shared_when_rls_returns_nothing(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "plays")] = lambda q, b: (200, [])
        self.assertEqual(self.call("/api/friends/stats", {"id": BOB}), ({"error": "not_shared"}, 403))

    def test_403_not_shared_for_a_non_friend_without_asking_the_server(self):
        self.befriend(BOB)
        self.assertEqual(self.call("/api/friends/stats", {"id": EVE}), ({"error": "not_shared"}, 403))
        self.assertEqual(self.sb.calls_to("plays"), [])

    def test_unfriending_drops_the_cache(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "plays")] = lambda q, b: (200, rows(3))
        self.sb.routes[("DELETE", "friendships")] = lambda q, b: (204, None)
        self.fr.friend_plays(BOB)
        self.friend_rows[:] = []
        self.fr.remove(BOB)
        self.assertNotIn(BOB, self.fr.plays)

    def test_own_plays_pull_only_asks_for_my_rows(self):
        self.sb.routes[("GET", "plays")] = lambda q, b: (200, [])
        self.sync.pull_plays()
        self.assertEqual(self.sb.calls_to("plays", "GET")[0][2]["user_id"], "eq.u1")


class StatsSwitchTests(Harness):
    def test_refresh_reads_stats_shared_and_defaults_to_on(self):
        self.fr.refresh()
        self.assertTrue(self.fr.sharing()["stats"])                       # no share_settings row: on
        self.sb.routes[("GET", "share_settings")] = lambda q, b: (200, [{"lists_shared": False, "stats_shared": False}])
        self.fr.refresh()
        self.assertEqual(self.fr.sharing(), {"lists": False, "stats": False})
        self.assertIn("stats_shared", self.sb.calls_to("share_settings", "GET")[-1][2]["select"])

    def test_set_stats_shared_writes_only_that_column_and_status_includes_it(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        out = self.fr.set_stats_shared(False)
        sent = self.sb.calls_to("share_settings", "POST")[-1]
        self.assertEqual(sent[3], {"user_id": "u1", "stats_shared": False})
        self.assertEqual(out, {"lists": False, "stats": False})
        self.assertEqual(self.fr.status()["sharing"]["stats"], False)

    def test_share_route_accepts_stats_shared(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        sent = []
        h = types.SimpleNamespace(_json=lambda obj, code=200: sent.append((obj, code)))
        c = types.SimpleNamespace(friends=self.fr, cloud=self.cloud)
        api._friends(h, c, "POST", "/api/friends/share", {}, {"stats_shared": False})
        self.assertEqual(sent[0], ({"lists": False, "stats": False}, 200))
        self.assertEqual([x[3] for x in self.sb.calls_to("share_settings", "POST")], [{"user_id": "u1", "stats_shared": False}])


class ShareApiTests(unittest.TestCase):
    def post(self, path, body):
        sent = []
        h = types.SimpleNamespace(_json=lambda obj, code=200: sent.append((obj, code)), companion=mock.Mock())
        self.assertTrue(api.handle(h, "POST", path, {}, body))
        return sent[0]

    @contextlib.contextmanager
    def home(self, downloads=True):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home, cfg = Path(td) / "home", Path(td) / "cfg"
            (home / "Downloads").mkdir(parents=True) if downloads else home.mkdir()
            with mock.patch("pathlib.Path.home", return_value=home), mock.patch("chcompanion.paths.config_dir", return_value=cfg), \
                    mock.patch("chcompanion.api.reveal") as rv:
                yield home, cfg, rv

    def test_save_writes_a_named_png_to_downloads_and_reveals_it(self):
        with self.home() as (home, cfg, rv):
            for payload in (base64.b64encode(PNG).decode(), "data:image/png;base64," + base64.b64encode(PNG).decode()):
                d, code = self.post("/api/share/save", {"png_base64": payload})
                self.assertEqual(code, 200)
                p = Path(d["path"])
                self.assertEqual(p.parent, home / "Downloads")
                self.assertRegex(p.name, r"^CloneHeroCompanion-stats-\d{8}-\d{6}\.png$")
                self.assertEqual(p.read_bytes(), PNG)
            self.assertEqual(rv.call_count, 2)
            self.post("/api/share/save", {"png_base64": base64.b64encode(PNG).decode(), "reveal": False})
            self.assertEqual(rv.call_count, 2)

    def test_save_falls_back_to_the_config_dir(self):
        with self.home(downloads=False) as (home, cfg, rv):
            d, code = self.post("/api/share/save", {"png_base64": base64.b64encode(PNG).decode()})
            self.assertEqual((code, Path(d["path"]).parent), (200, cfg))

    def test_save_rejects_big_and_invalid_images(self):
        with self.home() as (home, cfg, rv):
            big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (10 * 1024 * 1024)).decode()
            self.assertEqual(self.post("/api/share/save", {"png_base64": big})[1], 400)
            self.assertEqual(self.post("/api/share/save", {"png_base64": base64.b64encode(b"GIF89a....").decode()})[1], 400)
            self.assertEqual(self.post("/api/share/save", {"png_base64": "%%%not base64"})[1], 400)
            self.assertEqual(self.post("/api/share/save", {})[1], 400)
            self.assertEqual(list((home / "Downloads").iterdir()), [])
            rv.assert_not_called()

    def test_open_url_only_for_the_share_hosts(self):
        with mock.patch("chcompanion.process.open_url") as wb:
            self.assertEqual(self.post("/api/open", {"url": "https://wa.me/?text=hi"}), ({"ok": True}, 200))
            wb.assert_called_once_with("https://wa.me/?text=hi")
            for bad in ("http://wa.me/", "https://evil.example/", "file:///etc/passwd", "javascript:alert(1)"):
                self.assertEqual(self.post("/api/open", {"url": bad})[1], 400)
            self.assertEqual(wb.call_count, 1)


if __name__ == "__main__":
    unittest.main()
