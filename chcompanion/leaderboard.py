"""Read-only client for the public CH Leaderboards API (https://leaderboards.clonehero.net).

  GET {API}/search/quick?q=            songs [{songHash, name, artist, charter, icon, playCount, playerCount}]
  GET {API}/search?q=                  songs and users
  GET {API}/scores?songHash&instrument&difficulty&topScores&sort&accountId
  GET {API}/scores/latest-scores
  GET {API}/stats/site-totals
  GET {API}/stats/player-leaderboard?metric=
  GET {API}/stats/song-leaderboard?tab=&controllerGroup=
  GET {API}/account/{id}/profile

Scores can't be submitted from here: the game submits replays itself in leaderboards mode.
Responses are cached for 10 minutes and requests are spaced out politely.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .library import norm_key, strip_tags
from .net import urlopen

log = logging.getLogger(__name__)

API = "https://api.clonehero.net/v1"
SITE = "https://leaderboards.clonehero.net"
UA = "CloneHeroCompanion (read-only client)"
CACHE_S = 600
MIN_GAP_S = 0.25
INSTRUMENTS = ("guitar", "guitarcoop", "rhythm", "bass", "drums", "prodrums", "keys",
               "guitarghl", "guitarcoopghl", "rhythmghl", "bassghl", "keysghl")
DIFFICULTIES = ("expert", "hard", "medium", "easy")
METRICS = ("totalScore", "submissions", "totalFcs", "totalPfcs", "hoursPlayed", "starsEarned", "notesHit")
SONG_TABS = ("trending", "topAllTime", "topYear", "topMonth", "topWeek")
CONTROLLER_GROUPS = ("fiveFret", "sixFret", "drums")


class LeaderboardError(Exception):
    pass


def song_url(song_hash: str) -> str:
    return f"{SITE}/scores/{urllib.parse.quote(song_hash, safe='')}"


def user_url(account_id) -> str:
    return f"{SITE}/users/{int(account_id)}"


class Client:
    def __init__(self, fetch=None, now=time.time, sleep=time.sleep):
        self._fetch = fetch or self._real_fetch
        self.now, self.sleep = now, sleep
        self.cache: dict[str, tuple[float, object]] = {}
        self.lock = threading.Lock()
        self._last = 0.0

    def _real_fetch(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read().decode("utf-8")).get("message", "")[:160]
            except Exception:
                pass
            raise LeaderboardError(f"CH Leaderboards answered HTTP {e.code}. {msg}".strip())
        except Exception as e:
            raise LeaderboardError(f"CH Leaderboards is not reachable: {e}")

    def get(self, path: str, params: dict | None = None):
        q = urllib.parse.urlencode([(k, v) for k, v in (params or {}).items() if v not in (None, "")])
        url = f"{API}/{path}" + (f"?{q}" if q else "")
        with self.lock:
            hit = self.cache.get(url)
            if hit and self.now() - hit[0] < CACHE_S:
                return hit[1]
            gap = MIN_GAP_S - (self.now() - self._last)
            if gap > 0:
                self.sleep(gap)
            try:
                data = self._fetch(url)
            finally:
                self._last = self.now()
            self.cache[url] = (self.now(), data)
            if len(self.cache) > 300:
                for k, _ in sorted(self.cache.items(), key=lambda kv: kv[1][0])[:100]:
                    self.cache.pop(k, None)
            return data

    # ------------------------------------------------------------ site
    def site_totals(self):
        return self.get("stats/site-totals")

    def players(self, metric: str = "totalScore"):
        if metric not in METRICS:
            raise LeaderboardError("Unknown metric")
        rows = self.get("stats/player-leaderboard", {"metric": metric}) or []
        return [{**r, "url": user_url(r["accountId"])} for r in rows if "accountId" in r]

    def songs_board(self, tab: str = "trending", group: str = "fiveFret"):
        if tab not in SONG_TABS or group not in CONTROLLER_GROUPS:
            raise LeaderboardError("Unknown leaderboard")
        rows = self.get("stats/song-leaderboard", {"tab": tab, "controllerGroup": group}) or []
        return [{**r, "url": song_url(r["songHash"])} for r in rows if r.get("songHash")]

    def latest(self):
        rows = self.get("scores/latest-scores") or []
        return [self.slim(r) for r in rows][:30]

    # ------------------------------------------------------------ songs and scores
    def search_users(self, q: str):
        d = self.get("search", {"q": q}) or {}
        return d.get("users") or []

    def find_song(self, artist: str, name: str, charter: str = "") -> dict | None:
        """Map a local song to a leaderboards song: matching name and artist, and charter when given."""
        best = None
        want_a, want_n, want_c = norm_key(artist), norm_key(name), norm_key(strip_tags(charter))
        d = self.get("search/quick", {"q": f"{artist} {name}".strip()}) or {}
        for s in d.get("songs") or []:
            if norm_key(s.get("name", "")) != want_n:
                continue
            a = norm_key(s.get("artist", ""))
            if not (a == want_a or a.startswith(want_a + " ") or want_a.startswith(a + " ")):
                continue
            score = 1 + (2 if a == want_a else 0)
            if want_c and norm_key(s.get("charter", "")) == want_c:
                score += 4
            elif want_c and want_c in norm_key(s.get("charter", "")):
                score += 2
            score += min(1.0, (s.get("playCount") or 0) / 1e5)
            if best is None or score > best[0]:
                best = (score, s)
        return best[1] if best else None

    @staticmethod
    def slim(r: dict) -> dict:
        return {"placement": r.get("placement"), "accountId": r.get("accountId"), "username": r.get("username"),
                "score": r.get("totalScore"), "stars": r.get("stars"), "accuracy": _f(r.get("accuracy")),
                "fc": bool(r.get("isFc")), "pfc": bool(r.get("isPfc")), "speed": r.get("speed"),
                "instrument": r.get("instrument"), "difficulty": r.get("difficulty"),
                "name": r.get("name"), "artist": r.get("artist"), "songHash": r.get("songHash"),
                "at": r.get("createdAt"), "url": song_url(r["songHash"]) if r.get("songHash") else None}

    def top_scores(self, song_hash: str, instrument: str = "guitar", difficulty: str = "expert",
                   account_id: int | None = None) -> dict:
        if instrument not in INSTRUMENTS or difficulty not in DIFFICULTIES:
            raise LeaderboardError("Unknown instrument or difficulty")
        d = self.get("scores", {"songHash": song_hash, "instrument": instrument, "difficulty": difficulty,
                                "topScores": 10, "sort": "score", "accountId": account_id}) or {}
        top = [self.slim(r) for r in ((d.get("top") or {}).get("scoreData") or [])]
        mine = None
        if account_id:
            for r in top + [self.slim(x) for x in ((d.get("near") or {}).get("scoreData") or [])]:
                if r["accountId"] == account_id:
                    mine = r
                    break
        s = (d.get("summary") or {})
        return {"scores": top, "you": mine, "songHash": song_hash, "instrument": instrument,
                "difficulty": difficulty, "submissions": s.get("submissionCount"),
                "players": s.get("totalPlayerCount"), "url": song_url(song_hash)}

    # ------------------------------------------------------------ accounts
    def profile(self, account_id: int) -> dict:
        d = self.get(f"account/{int(account_id)}/profile") or {}
        return {"accountId": d.get("accountId"), "username": d.get("username"),
                "avatar": d.get("profilePictureUrl"), "stats": d.get("stats") or {}, "ranks": d.get("ranks") or {},
                "topArtists": d.get("topArtists") or [], "topInstruments": d.get("topInstruments") or [],
                "recent": [self.slim(r) for r in (d.get("latestSubmissions") or [])][:10],
                "url": user_url(d["accountId"]) if d.get("accountId") is not None else None}

    def find_user(self, username: str) -> dict | None:
        want = username.strip().casefold()
        users = self.search_users(username.strip())
        for u in users:
            if (u.get("username") or "").casefold() == want:
                return u
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
