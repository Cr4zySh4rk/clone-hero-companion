"""Friends and sharing (Supabase): username + friend code, mutual friend requests, and friends' shared lists.

All server-side access control is row-level security (supabase/migrations/0002_friends_sharing.sql); this
module only asks for the right rows, treats 401/403/empty answers as "nothing to show", and keeps a small
cache (<config>/friends_cache.json) so friends and their last-seen lists stay viewable offline, read-only.
Email addresses are never requested or shown.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from . import favmatch
from .cloud import CloudError
from .i18n import _

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
CODE_RE = re.compile(r"^[A-Z0-9]{8}$")
POLL_S = 120.0


def clean_username(name: str) -> str:
    return (name or "").strip().lower().lstrip("@")


def _row(r: dict) -> dict:
    """One song row of a shared list, reduced to what installing and matching need."""
    return {"song_key": r.get("song_key") or "", "checksum": r.get("checksum"), "artist": r.get("artist") or "",
            "name": r.get("name") or "", "charter": r.get("charter") or "", "album": r.get("album") or "",
            "source": r.get("source") if isinstance(r.get("source"), dict) else {}}


class Friends:
    def __init__(self, cloud, cloudsync, libmgr, state_path: Path | None = None):
        self.cloud, self.cloudsync, self.lm = cloud, cloudsync, libmgr
        self.path = state_path or cloud.session_path.with_name("friends_cache.json")
        self.lock = threading.RLock()
        self.cache = {"uid": None, "me": {}, "friends": [], "incoming": [], "outgoing": [], "lists": {},
                      "sharing": {"lists": False, "stats": True}, "at": None}
        self.plays: dict[str, tuple[float, list]] = {}      # friend id -> (fetched at, plays); memory only
        self.offline = False
        try:
            s = json.loads(self.path.read_text("utf-8"))
            if isinstance(s, dict) and s.get("uid") == self._uid():
                self.cache.update(s)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ plumbing
    def _uid(self):
        return self.cloud.user().get("id") if self.cloud.signed_in else None

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache), "utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.debug("Could not save the friends cache: %s", e)

    def _need_login(self):
        if not self.cloud.signed_in:
            raise CloudError(_("cloud.not_signed_in"), 401)

    def reset(self):
        """After sign-out: forget the previous account's friends."""
        with self.lock:
            self.cache.update(uid=None, me={}, friends=[], incoming=[], outgoing=[], lists={},
                              sharing={"lists": False, "stats": True}, at=None)
            self.plays.clear()
            self.offline = False
            try:
                self.path.unlink()
            except OSError:
                pass

    def _adopt_account(self):
        uid = self._uid()
        if self.cache.get("uid") != uid:
            self.reset()
            self.cache["uid"] = uid

    @staticmethod
    def _detail(e: CloudError) -> str:
        return (getattr(e, "body", "") or "").lower()

    @classmethod
    def _message(cls, e: CloudError) -> str:
        """The server's error message (our RPCs raise 'not_found', 'self' or 'exists')."""
        try:
            return str(json.loads(getattr(e, "body", "") or "{}").get("message") or "").lower()
        except (ValueError, AttributeError):
            return cls._detail(e)

    # ------------------------------------------------------------ me
    def refresh(self) -> dict:
        """Pull my profile, friendships and sharing switches. Offline: keep the cache and say so."""
        self._need_login()
        with self.lock:
            self._adopt_account()
        c = self.cloud
        try:
            prof = c.get_profile() or {}
            rows = c.rest("POST", "rpc/my_friendships", None, {}) or []
            shared = c.rest("GET", "share_settings", {"select": "lists_shared,stats_shared", "user_id": f"eq.{self._uid()}"}) or []
        except CloudError as e:
            if e.offline:
                self.offline = True
                return self.view()
            raise
        with self.lock:
            self.offline = False
            self.cache["me"] = {"username": prof.get("username"), "friend_code": prof.get("friend_code"),
                                "display_name": prof.get("display_name")}
            fr, inc, out = [], [], []
            for r in rows:
                p = {"id": r.get("other_id"), "username": r.get("username"), "name": r.get("display_name"),
                     "avatar": r.get("avatar_url"), "since": r.get("created_at")}
                if r.get("status") == "accepted":
                    fr.append(p)
                elif r.get("direction") == "incoming":
                    inc.append(p)
                else:
                    out.append(p)
            self.cache.update(friends=fr, incoming=inc, outgoing=out, at=int(time.time()))
            self.cache["sharing"] = {"lists": bool(shared and shared[0].get("lists_shared")),
                                     "stats": not shared or shared[0].get("stats_shared") is not False}   # no row = on
            self.cloudsync.set_lists_shared(self.cache["sharing"]["lists"])
            live = {f["id"] for f in fr}
            self.cache["lists"] = {k: v for k, v in self.cache["lists"].items() if k in live}   # unfriended: forget
            self.plays = {k: v for k, v in self.plays.items() if k in live}
            self._save()
        return self.view()

    def poll(self) -> int:
        """Background check for new requests. Returns how many are waiting; never raises."""
        if not self.cloud.signed_in:
            return 0
        try:
            self.refresh()
        except Exception as e:
            log.debug("friends poll failed: %s", e)
        return len(self.cache.get("incoming") or [])

    def view(self) -> dict:
        with self.lock:
            c = self.cache
            return {"signedIn": self.cloud.signed_in, "me": c.get("me") or {}, "friends": c.get("friends") or [],
                    "incoming": c.get("incoming") or [], "outgoing": c.get("outgoing") or [],
                    "sharing": c.get("sharing") or {}, "offline": self.offline, "at": c.get("at"),
                    "needsUsername": self.cloud.signed_in and not (c.get("me") or {}).get("username")}

    def status(self) -> dict:
        """Cheap, no network: what the sidebar badge and the Profile prompt need."""
        v = self.view()
        return {"signedIn": v["signedIn"], "username": v["me"].get("username"), "friendCode": v["me"].get("friend_code"),
                "needsUsername": bool(v["signedIn"] and v["at"] and not v["me"].get("username")),
                "incoming": len(v["incoming"]), "outgoing": len(v["outgoing"]), "friends": len(v["friends"]),
                "offline": v["offline"], "sharing": v["sharing"]}

    # ------------------------------------------------------------ username
    def check_username(self, name: str) -> dict:
        self._need_login()
        n = clean_username(name)
        if not USERNAME_RE.match(n):
            return {"valid": False, "available": False, "username": n}
        ok = self.cloud.rest("POST", "rpc/username_available", None, {"name": n})
        return {"valid": True, "available": bool(ok) or n == (self.cache.get("me") or {}).get("username"), "username": n}

    def set_username(self, name: str) -> dict:
        self._need_login()
        n = clean_username(name)
        if not USERNAME_RE.match(n):
            raise CloudError(_("friends.username_invalid"))
        try:
            self.cloud.rest("PATCH", "profiles", {"id": f"eq.{self._uid()}"}, {"username": n}, "return=minimal")
        except CloudError as e:
            if e.status == 409 or "23505" in self._detail(e) or "duplicate" in self._detail(e):
                raise CloudError(_("friends.username_taken"), 409)
            raise
        return self.refresh()

    # ------------------------------------------------------------ requests
    def lookup(self, q: str) -> dict | None:
        self._need_login()
        q = (q or "").strip().lstrip("@")
        if not q:
            raise CloudError(_("friends.enter_query"))
        rows = self.cloud.rest("POST", "rpc/lookup_profile", None, {"q": q}) or []
        return ({"username": rows[0].get("username"), "name": rows[0].get("display_name"),
                 "avatar": rows[0].get("avatar_url")} if rows else None)

    def request(self, q: str) -> dict:
        self._need_login()
        q = (q or "").strip().lstrip("@")
        if not q:
            raise CloudError(_("friends.enter_query"))
        try:
            status = self.cloud.rest("POST", "rpc/request_friend", None, {"q": q})
        except CloudError as e:
            d = self._message(e)
            if d == "not_found":
                raise CloudError(_("friends.not_found"), 404)
            if d == "self":
                raise CloudError(_("friends.is_self"), 400)
            if d == "exists" or e.status == 409:
                raise CloudError(_("friends.already"), 409)
            raise
        self.refresh()
        return {"status": status if isinstance(status, str) else "pending"}

    def respond(self, other_id: str, accept: bool) -> dict:
        self._need_login()
        other_id = str(other_id or "")
        if accept:
            self.cloud.rest("PATCH", "friendships", {"requester": f"eq.{other_id}", "addressee": f"eq.{self._uid()}"},
                            {"status": "accepted"}, "return=minimal")
        else:
            self.remove(other_id)
        return self.refresh()

    def remove(self, other_id: str) -> dict:
        """Unfriend, cancel my request or decline theirs: all are deleting the row."""
        self._need_login()
        other_id = str(other_id or "")
        if not other_id:
            raise CloudError(_("friends.unknown_friend"), 400)
        self.cloud.rest("DELETE", "friendships", {"or": f"(requester.eq.{other_id},addressee.eq.{other_id})"},
                        prefer="return=minimal")
        with self.lock:
            self.cache["lists"].pop(other_id, None)
            self.plays.pop(other_id, None)
        return self.refresh()

    # ------------------------------------------------------------ a friend's shared lists
    def friend(self, fid: str) -> dict | None:
        return next((f for f in self.cache.get("friends") or [] if f["id"] == fid), None)

    def friend_lists(self, fid: str) -> dict:
        """The favourites and setlists a friend shares with me: {friend, favorites, setlists, offline, at}.
        Only accepted friends are asked for. Server denials (401/403) and empty answers mean 'nothing shared'."""
        self._need_login()
        f = self.friend(fid)
        if not f:
            raise CloudError(_("friends.unknown_friend"), 404)
        c = self.cloud
        try:
            if self._visible(fid) is False:     # the friend has the switch off: ask for nothing else
                with self.lock:
                    self.cache["lists"][fid] = {"shared": False, "favorites": [], "setlists": [], "at": int(time.time())}
                    self._save()
                self.offline = False
                return {"friend": f, "shared": False, "favorites": [], "setlists": [], "offline": False, "at": int(time.time())}
            favs = c.rest("GET", "favorites", {"select": "song_key,checksum,artist,name,charter,album,source,added_at",
                                               "user_id": f"eq.{fid}", "order": "added_at.asc"}) or []
            sls = c.rest("GET", "setlists", {"select": "id,name,updated_at,setlist_items(*)", "owner": f"eq.{fid}",
                                             "order": "name.asc"}) or []
        except CloudError as e:
            if e.offline:
                self.offline = True
                return self._cached_list(f)
            if e.status == 403:
                favs, sls = [], []
            else:
                raise
        else:
            self.offline = False
        out = {"shared": True, "favorites": [_row(r) for r in favs],
               "setlists": [{"name": s.get("name"), "updatedAt": s.get("updated_at"),
                             "items": [_row(i) for i in sorted(s.get("setlist_items") or [], key=lambda i: i.get("position", 0))]}
                            for s in sls]}
        with self.lock:
            self.cache["lists"][fid] = {**out, "at": int(time.time())}
            self._save()
        return {"friend": f, **out, "offline": False, "at": int(time.time())}

    def _visible(self, fid: str):
        """True/False from the server's lists_visible(); None if it cannot say (older database)."""
        try:
            v = self.cloud.rest("POST", "rpc/lists_visible", None, {"owner": fid})
        except CloudError as e:
            if e.offline or e.status in (401,):
                raise
            return None
        return bool(v) if isinstance(v, bool) else None

    def _cached_list(self, f: dict) -> dict:
        got = self.cache["lists"].get(f["id"]) or {}
        return {"friend": f, "shared": got.get("shared", True), "favorites": got.get("favorites") or [], "setlists": got.get("setlists") or [],
                "offline": True, "at": got.get("at")}

    # ------------------------------------------------------------ my sharing switch
    def set_lists_shared(self, value: bool) -> dict:
        """The one switch: all friends see my favourites and ALL my setlists (also later ones), or nothing."""
        self._need_login()
        value = bool(value)
        self.cloud.rest("POST", "share_settings", {"on_conflict": "user_id"},
                        {"user_id": self._uid(), "lists_shared": value}, "resolution=merge-duplicates,return=minimal")
        self.cloudsync.set_lists_shared(value)
        with self.lock:
            self.cache["sharing"] = {**self.sharing(), "lists": value}
            self._save()
        if value:
            try:
                self.cloudsync.sync_setlists()      # every local setlist is in the cloud before a friend looks
            except CloudError as e:
                log.info("Setlists will sync later: %s", e)
            self.cloudsync.schedule(0.5)            # favourites too
        return self.sharing()

    def set_shared(self, kind: str = "", name: str | None = None, value: bool = False) -> dict:
        """Deprecated 2.3 payload {kind, name, shared}: any per-list switch now means the single switch."""
        if kind not in ("favorites", "setlist"):
            raise CloudError(_("friends.bad_kind"), 400)
        return self.set_lists_shared(value)

    def set_stats_shared(self, value: bool) -> dict:
        """Friends may (default) or may not see my profile statistics (RLS: stats_visible, migration 0004)."""
        self._need_login()
        value = bool(value)
        self.cloud.rest("POST", "share_settings", {"on_conflict": "user_id"},
                        {"user_id": self._uid(), "stats_shared": value}, "resolution=merge-duplicates,return=minimal")
        with self.lock:
            self.cache["sharing"] = {**self.sharing(), "stats": value}
            self._save()
        return self.sharing()

    def sharing(self) -> dict:
        s = self.cache.get("sharing") or {}
        return {"lists": bool(s.get("lists")), "stats": s.get("stats") is not False}

    # ------------------------------------------------------------ a friend's play statistics
    PLAY_COLS = ("client_id,played_at,checksum,artist,name,charter,instrument,difficulty,speed,score,stars,accuracy,"
                 "notes_hit,total_notes,max_streak,fc,completed,duration_s")
    PLAYS_TTL_S = 600.0

    def friend_plays(self, fid: str) -> list[dict]:
        """A friend's plays (explicit columns only, never `raw`), 1000 per page, cached in memory for 10 minutes.
        Only accepted friends are asked for; RLS returns [] when they switched stats sharing off."""
        self._need_login()
        if not self.friend(fid):
            raise CloudError(_("friends.unknown_friend"), 404)
        now = time.monotonic()
        with self.lock:
            hit = self.plays.get(fid)
        if hit and now - hit[0] < self.PLAYS_TTL_S:
            return hit[1]
        rows, offset = [], 0
        while True:
            page = self.cloud.rest("GET", "plays", {"select": self.PLAY_COLS, "user_id": f"eq.{fid}",
                                                    "order": "played_at.asc,id.asc", "limit": 1000, "offset": offset}) or []
            rows += page
            if len(page) < 1000:
                break
            offset += 1000
        with self.lock:
            self.plays[fid] = (now, rows)
        return rows

    # ------------------------------------------------------------ what is already in my library
    def matcher(self):
        """find(row) -> Song | None: the chart in my library for a shared row, by checksum, else by favmatch (same
        artist, title and charter, as the favourites install uses). Indexes the library once, so a list of
        hundreds of rows stays fast."""
        lm = self.lm
        with lm.lock:
            songs = list(lm.lib.songs)
        by_md5, by_artist = {}, {}
        for s in songs:
            h = lm.chart_md5(s)
            if h:
                by_md5.setdefault(h, s)
            by_artist.setdefault(favmatch.artist_key(s.artist), []).append(s)

        def find(row: dict):
            src = row.get("source") or {}
            for m in (row.get("checksum"), row.get("md5"), src.get("encore_md5")):
                if m and m.upper() in by_md5:
                    return by_md5[m.upper()]
            a = favmatch.artist_key(row.get("artist") or "")
            if not a:
                return None
            bare = {"artist": row.get("artist"), "name": row.get("name"), "charter": row.get("charter")}
            return lm._match_in(bare, by_artist.get(a, []))
        return find

    def annotate(self, rows: list[dict]) -> list[dict]:
        from .libmgr import song_id
        find = self.matcher()
        out = []
        for r in rows:
            s = find(r)
            out.append({**r, "present": s is not None, "id": song_id(s) if s is not None else None})
        return out
