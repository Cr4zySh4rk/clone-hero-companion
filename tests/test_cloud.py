import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from chcompanion import cloud, leaderboard, playstats
from chcompanion.cloud import Cloud, CloudError, CloudSync


class FakeHttp:
    """Records calls; answers from a list of (status, json) or a function."""
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __call__(self, method, url, headers, body, timeout=20.0):
        b = json.loads(body) if body else None
        self.calls.append((method, url, headers, b))
        status, data = self.handler(method, url, headers, b)
        return status, json.dumps(data).encode()


SESSION = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
           "user": {"id": "u1", "email": "me@example.com", "user_metadata": {"full_name": "Me", "avatar_url": "http://a/p.png"}}}


def make_cloud(tmp, handler):
    http = FakeHttp(handler)
    opened = []
    c = Cloud(Path(tmp) / "cloud_session.json", http=http, open_browser=opened.append)
    c.opened = opened
    return c, http


class AuthTests(unittest.TestCase):
    def test_pkce_login_and_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, http = make_cloud(tmp, lambda *a: (200, SESSION))
            url = c.start_login(47823)
            self.assertIn("provider=google", url)
            self.assertIn("code_challenge_method=s256", url)
            self.assertIn("127.0.0.1%3A47823%2Fauth%2Fcallback", url)
            self.assertEqual(c.opened, [url])
            c.complete_login("CODE")
            m, u, h, body = http.calls[-1]
            self.assertIn("grant_type=pkce", u)
            self.assertEqual(body["auth_code"], "CODE")
            self.assertEqual(h["apikey"], cloud.PUBLISHABLE_KEY)
            self.assertEqual(c.user()["name"], "Me")
            f = Path(tmp) / "cloud_session.json"
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertTrue(Cloud(f, http=http).signed_in)        # survives a restart
            with self.assertRaises(CloudError):                    # the verifier is single use
                c.complete_login("CODE")

    def test_pkce_challenge_is_s256_of_verifier(self):
        import base64, hashlib
        v, ch = cloud.make_pkce()
        self.assertEqual(ch, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())

    def test_refresh_before_expiry_and_on_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"n": 0}

            def h(method, url, headers, body):
                if "grant_type=refresh_token" in url:
                    return 200, {**SESSION, "access_token": "AT2"}
                if headers["Authorization"] == "Bearer AT2":
                    return 200, [{"ok": 1}]
                return 401, {}
            c, http = make_cloud(tmp, h)
            c.start_login(1)
            c._store_session(SESSION)
            self.assertEqual(c.rest("GET", "favorites"), [{"ok": 1}])      # 401 -> refresh once -> retry
            c.session["expires_at"] = time.time() + 10                      # inside the 60 s margin
            before = len(http.calls)
            c.access_token()
            self.assertIn("refresh_token", http.calls[before][1])

    def test_logout_deletes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, http = make_cloud(tmp, lambda *a: (200, {}))
            c._store_session(SESSION)
            c.logout()
            self.assertFalse(c.signed_in)
            self.assertFalse((Path(tmp) / "cloud_session.json").exists())
            self.assertIn("/auth/v1/logout", http.calls[-1][1])

    def test_email_code_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            def h(method, url, headers, body):
                if url.endswith("/auth/v1/otp"):
                    return (429, {}) if body["email"] == "slow@x.com" else (200, {})
                if url.endswith("/auth/v1/verify"):
                    return (200, {**SESSION, "user": {"id": "u2", "email": "dj@x.com", "user_metadata": {}}}) \
                        if body["token"] == "123456" else (403, {"msg": "expired"})
                return 200, {}
            c, http = make_cloud(tmp, h)
            c.send_email_code("dj@x.com")
            self.assertEqual(http.calls[-1][3], {"email": "dj@x.com", "create_user": True})
            with self.assertRaises(CloudError) as e:
                c.send_email_code("slow@x.com")
            self.assertEqual(e.exception.status, 429)
            with self.assertRaises(CloudError):
                c.send_email_code("nope")
            with self.assertRaises(CloudError) as e:
                c.verify_email_code("dj@x.com", "000000")
            self.assertIn("invalid or has expired", str(e.exception))
            c.verify_email_code("dj@x.com", "123 456")
            self.assertEqual(http.calls[-1][3], {"type": "email", "email": "dj@x.com", "token": "123456"})
            self.assertEqual(c.user()["name"], "dj")                      # name = part before @
            c.set_display_name("DJ Fret")
            self.assertEqual(http.calls[-1][0], "PATCH")
            self.assertEqual(http.calls[-1][3], {"display_name": "DJ Fret"})
            self.assertEqual(c.user()["name"], "DJ Fret")


class FakeLib:
    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.by_id, self.cache, self.version = {}, {}, 0

    def _reindex(self): pass
    def _hash_index(self): return {}
    def fav_setlist_enabled(self): return False
    def _fav_sets(self): return set(), set()
    def missing_favs(self): return []


class FakeStore:
    def __init__(self): self.d = {}
    def song(self, k): return dict(self.d.get(k, {}))
    def update_song(self, k, **v): self.d.setdefault(k, {}).update(v)


class SyncTests(unittest.TestCase):
    def test_favorites_two_way_and_tombstones(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = [{"song_key": "queen bohemian rhapsody|harmonix", "artist": "Queen", "name": "Bohemian Rhapsody",
                       "charter": "Harmonix", "checksum": None, "source": {}}]
            sent = []

            def h(method, url, headers, body):
                if method == "GET" and "/favorites" in url:
                    return 200, remote
                sent.append((method, url, body))
                return 200, None
            c, http = make_cloud(tmp, h)
            c._store_session(SESSION)
            lm, store = FakeLib(), FakeStore()
            sync = CloudSync(c, store, lm, playstats.PlayLog(Path(tmp) / "p.jsonl", Path(tmp) / "pc.jsonl"),
                             Path(tmp) / "state.json")
            sync.state["tombstones"]["old song|x"] = 1
            sync.sync_favorites()
            self.assertEqual(sent[0][0], "DELETE")
            self.assertIn("old+song%7Cx", sent[0][1])
            self.assertEqual(sync.state["tombstones"], {})
            self.assertEqual([r["song_key"] for r in sync.cloud_only], ["queen bohemian rhapsody|harmonix"])

    def test_resolve_on_encore(self):
        row = {"artist": "Queen", "name": "Bohemian Rhapsody", "charter": "Harmonix", "source": {}}
        res = [{"name": "Bohemian Rhapsody", "artist": "Queen", "charter": "Someone", "downloadUrl": "u1", "md5": "a"},
               {"name": "Bohemian Rhapsody", "artist": "Queen", "charter": "Harmonix", "downloadUrl": "u2", "md5": "b"}]
        self.assertEqual(cloud.resolve_on_encore(row, lambda q, per_page=50: {"songs": res})["downloadUrl"], "u2")
        self.assertIsNone(cloud.resolve_on_encore({**row, "charter": "Nobody"}, lambda q, per_page=50: {"songs": res}))

    def test_plays_upload_once_and_pull_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            pl = playstats.PlayLog(Path(tmp) / "p.jsonl", Path(tmp) / "pc.jsonl")
            pl.append(playstats.parse_scorestats(SCORE, 1_700_000_000))
            posts = []

            def h(method, url, headers, body):
                if method == "POST":
                    posts.append(body)
                    return 200, None
                return 200, [{"id": 1, "user_id": "u1", "client_id": "other|1|0", "played_at": "2026-01-01T00:00:00Z",
                              "artist": "A", "name": "B", "instrument": "guitar", "difficulty": "expert", "score": 5}]
            c, _ = make_cloud(tmp, h)
            c._store_session(SESSION)
            sync = CloudSync(c, FakeStore(), FakeLib(), pl, Path(tmp) / "state.json")
            self.assertEqual(sync.push_plays(), 2)
            self.assertEqual(sync.push_plays(), 0)
            self.assertEqual(len(posts), 1)
            self.assertEqual(sync.pull_plays(), 1)
            self.assertEqual(len(pl.all()), 3)
            self.assertEqual(sync.pending(), 0)


SCORE = {"checksum": "abc123", "song_name": "Song <b>X</b>", "artist_name": "Band", "charter_name": "<color=#f00>Me</color>",
         "playback_speed": 100, "score_timestamp": 1_700_000_000,
         "players": [{"instrument": "Lead Guitar", "difficulty": 3, "score": 1000, "stars": 5, "notes_hit": 90,
                      "total_notes": 100, "max_streak": 50, "profile_name": "p1"},
                     {"instrument": "bass", "difficulty": "hard", "score": 500, "stars": 3, "notes_hit": 50,
                      "total_notes": 50, "max_streak": 50}]}


class PlayTests(unittest.TestCase):
    def test_parse(self):
        r = playstats.parse_scorestats(SCORE, 0)
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0]["charter"], "Me")
        self.assertEqual(r[0]["instrument"], "guitar")
        self.assertEqual(r[0]["difficulty"], "expert")
        self.assertAlmostEqual(r[0]["accuracy"], 0.9)
        self.assertFalse(r[0]["fc"])
        self.assertTrue(r[1]["fc"])
        self.assertNotEqual(r[0]["client_id"], r[1]["client_id"])
        self.assertEqual(r[0]["checksum"], "ABC123")

    def test_watcher_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "scorestats.json"
            pl = playstats.PlayLog(Path(tmp) / "p.jsonl", Path(tmp) / "pc.jsonl")
            got = []
            w = playstats.PlayWatcher(pl, lambda: [f], on_new=got.append)
            self.assertEqual(w.check(), [])
            f.write_text(json.dumps(SCORE))
            self.assertEqual(len(w.check()), 2)
            f.write_text(json.dumps(SCORE))
            import os
            os.utime(f, (time.time() + 5, time.time() + 5))
            self.assertEqual(w.check(), [])                 # same play again
            self.assertEqual(len(pl.local()), 2)

    def test_compute(self):
        now = datetime.now().astimezone()
        plays = []
        for i in range(3):
            t = (now - timedelta(days=i)).astimezone().isoformat()
            plays.append({"client_id": f"c{i}", "played_at": t, "checksum": "A", "artist": "Band", "name": "S",
                          "instrument": "guitar", "difficulty": "expert", "score": 100 * (i + 1), "stars": 5,
                          "notes_hit": 90 + i, "total_notes": 100, "fc": False, "duration_s": 180})
        plays.append({"client_id": "d", "played_at": now.isoformat(), "checksum": "B", "artist": "B", "name": "T",
                      "instrument": "drums", "difficulty": "easy", "score": 1, "stars": 1, "notes_hit": 10,
                      "total_notes": 10, "fc": True})
        s = playstats.compute(plays, now=now)
        self.assertEqual(s["totals"]["plays"], 4)
        self.assertEqual(s["totals"]["songs"], 2)
        self.assertEqual(s["totals"]["fcs"], 1)
        self.assertEqual(s["totals"]["streak"], 3)
        self.assertEqual(s["mostPlayed"][0]["plays"], 3)
        self.assertEqual(s["personalBests"][0]["score"], 300)
        g = playstats.compute(plays, "guitar", "expert", now=now)
        self.assertEqual(g["totals"]["plays"], 3)
        self.assertEqual(len(g["needsPractice"]), 1)
        self.assertEqual(sum(d["n"] for d in g["heatmap"]["days"]), 3)


class LeaderboardTests(unittest.TestCase):
    def test_find_song_and_cache(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return {"songs": [
                {"songHash": "h1", "name": "War Pigs", "artist": "Black Sabbath (WaveGroup)", "charter": "Harmonix", "playCount": 5},
                {"songHash": "h2", "name": "War Pigs", "artist": "Black Sabbath", "charter": "7777villain", "playCount": 1},
                {"songHash": "h3", "name": "War Pigs (cover)", "artist": "Cake", "charter": "x", "playCount": 99}]}
        c = leaderboard.Client(fetch=fetch, sleep=lambda s: None)
        self.assertEqual(c.find_song("Black Sabbath", "War Pigs", "<color=#fff>7777villain</color>")["songHash"], "h2")
        self.assertEqual(c.find_song("Black Sabbath", "War Pigs", "")["songHash"], "h2")   # exact artist wins
        self.assertEqual(len(calls), 1)                                                     # cached
        self.assertIsNone(c.find_song("Cake", "Nope"))

    def test_top_scores_rank(self):
        def fetch(url):
            return {"summary": {"submissionCount": 3}, "top": {"scoreData": [
                {"placement": 1, "accountId": 5, "username": "a", "totalScore": 9}]},
                "near": {"scoreData": [{"placement": 7, "accountId": 9, "username": "me", "totalScore": 3}]}}
        c = leaderboard.Client(fetch=fetch, sleep=lambda s: None)
        r = c.top_scores("h", "guitar", "expert", 9)
        self.assertEqual(r["you"]["placement"], 7)
        with self.assertRaises(leaderboard.LeaderboardError):
            c.top_scores("h", "banjo", "expert")


if __name__ == "__main__":
    unittest.main()
