"""Optional cloud account (Supabase): Google sign-in, favourites and play history sync.

Cloud is an add-on. Everything works signed out and offline; failures are logged quietly and
retried on the next sync, never shown as job errors. Uses only the standard library through
net.urlopen. The session is kept in <config>/cloud_session.json (chmod 600), never in the repo.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

from . import favmatch
from .library import norm_key, strip_tags
from .net import urlopen
from .paths import config_dir

log = logging.getLogger(__name__)

URL = "https://muhboxuefazapytpqzjc.supabase.co"
# Public "publishable" key: safe to ship, row-level security protects the data.
PUBLISHABLE_KEY = "sb_publishable_MrDFLihLJ8El-VQ3WvfhEQ_BArBKEWx"
REFRESH_MARGIN_S = 60
PLAY_BATCH = 200
DEBOUNCE_S = 3.0


class CloudError(Exception):
    def __init__(self, msg: str, status: int | None = None, offline: bool = False):
        super().__init__(msg)
        self.status, self.offline = status, offline


def _http(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception as e:           # DNS, timeouts, no network...
        raise CloudError(f"offline: {e}", offline=True)


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def song_key(artist: str, name: str, charter: str) -> str:
    return norm_key(f"{artist} - {name}") + "|" + norm_key(charter)


class Cloud:
    def __init__(self, session_path: Path | None = None, http: Callable = _http,
                 url: str = URL, key: str = PUBLISHABLE_KEY, open_browser: Callable[[str], object] = webbrowser.open):
        self.session_path = session_path or (config_dir() / "cloud_session.json")
        self.http, self.url, self.key, self.open_browser = http, url.rstrip("/"), key, open_browser
        self.lock = threading.RLock()
        self._pending: dict | None = None      # {"verifier", "at"} while a login is in progress
        self.session: dict | None = None
        try:
            s = json.loads(self.session_path.read_text("utf-8"))
            if isinstance(s, dict) and s.get("refresh_token"):
                self.session = s
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ session file
    def _save(self):
        tmp = self.session_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.session, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.session_path)

    def _clear(self):
        self.session = None
        try:
            self.session_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------ low level
    def _auth_call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        headers = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        status, raw = self.http(method, self.url + path, headers, json.dumps(body).encode() if body is not None else None)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {}
        return status, data

    @property
    def signed_in(self) -> bool:
        return bool(self.session)

    def user(self) -> dict:
        return (self.session or {}).get("user") or {}

    # ------------------------------------------------------------ sign in (PKCE)
    def start_login(self, port: int) -> str:
        verifier, challenge = make_pkce()
        with self.lock:
            self._pending = {"verifier": verifier, "at": time.time()}
        redirect = f"http://127.0.0.1:{port}/auth/callback"
        q = urllib.parse.urlencode({"provider": "google", "redirect_to": redirect,
                                    "code_challenge": challenge, "code_challenge_method": "s256"})
        url = f"{self.url}/auth/v1/authorize?{q}"
        try:
            self.open_browser(url)
        except Exception as e:
            log.warning("Could not open the browser for sign-in: %s", e)
        return url

    def complete_login(self, code: str) -> dict:
        with self.lock:
            pend, self._pending = self._pending, None
        if not pend or time.time() - pend["at"] > 900:
            raise CloudError("No sign-in is in progress. Start again from the Profile page.")
        status, data = self._auth_call("POST", "/auth/v1/token?grant_type=pkce",
                                       {"auth_code": code, "code_verifier": pend["verifier"]})
        if status != 200 or not data.get("access_token"):
            raise CloudError(data.get("error_description") or data.get("msg") or f"Sign-in failed (HTTP {status})", status)
        self._store_session(data)
        return self.user()

    # ------------------------------------------------------------ sign in (email code)
    def send_email_code(self, email: str):
        email = (email or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise CloudError("Enter a valid email address.")
        status, data = self._auth_call("POST", "/auth/v1/otp", {"email": email, "create_user": True})
        if status == 429:
            raise CloudError("Too many codes requested. Wait a minute and try again.", 429)
        if status >= 400:
            raise CloudError(data.get("msg") or data.get("error_description") or f"Could not send the code (HTTP {status})", status)

    def verify_email_code(self, email: str, code: str) -> dict:
        code = re.sub(r"\s", "", code or "")
        if not re.fullmatch(r"\d{6}", code):
            raise CloudError("The code is 6 digits.")
        status, data = self._auth_call("POST", "/auth/v1/verify",
                                       {"type": "email", "email": (email or "").strip(), "token": code})
        if status == 429:
            raise CloudError("Too many attempts. Wait a minute and try again.", 429)
        if status != 200 or not data.get("access_token"):
            raise CloudError("That code is invalid or has expired. Check the newest email, or send a new code.", status)
        self._store_session(data)
        return self.user()

    def set_display_name(self, name: str):
        name = (name or "").strip()[:60]
        if not name:
            raise CloudError("Enter a name.")
        uid = self.user().get("id")
        if not uid:
            raise CloudError("Not signed in", 401)
        self.rest("PATCH", "profiles", {"id": f"eq.{uid}"}, {"display_name": name}, "return=minimal")
        with self.lock:
            self.session["user"]["name"] = name
            self._save()

    def _store_session(self, data: dict):
        with self.lock:
            u = data.get("user") or (self.session or {}).get("user") or {}
            meta = u.get("user_metadata") or {}
            self.session = {
                "access_token": data["access_token"], "refresh_token": data.get("refresh_token", ""),
                "expires_at": int(data.get("expires_at") or time.time() + int(data.get("expires_in") or 3600)),
                "user": {"id": u.get("id"), "email": u.get("email") or meta.get("email"),
                         "name": meta.get("full_name") or meta.get("name") or ((u.get("email") or "").split("@")[0] or None),
                         "avatar": meta.get("avatar_url") or meta.get("picture")},
            }
            self._save()

    # ------------------------------------------------------------ tokens
    def refresh(self) -> bool:
        with self.lock:
            if not self.session:
                return False
            status, data = self._auth_call("POST", "/auth/v1/token?grant_type=refresh_token",
                                           {"refresh_token": self.session.get("refresh_token", "")})
            if status == 200 and data.get("access_token"):
                self._store_session(data)
                return True
            if status in (400, 401, 403):          # the refresh token is no longer valid
                log.info("Cloud session expired; signed out")
                self._clear()
                return False
            raise CloudError(f"Could not refresh the session (HTTP {status})", status)

    def access_token(self) -> str:
        with self.lock:
            if not self.session:
                raise CloudError("Not signed in", 401)
            if self.session["expires_at"] - REFRESH_MARGIN_S <= time.time():
                if not self.refresh():
                    raise CloudError("Not signed in", 401)
            return self.session["access_token"]

    def logout(self):
        with self.lock:
            tok = (self.session or {}).get("access_token")
            if tok:
                try:
                    self._auth_call("POST", "/auth/v1/logout", {}, token=tok)
                except CloudError:
                    pass
            self._clear()

    # ------------------------------------------------------------ REST
    def rest(self, method: str, path: str, params: dict | None = None, body=None, prefer: str | None = None):
        """Call {URL}/rest/v1/<path>; refreshes the token and retries once on a 401."""
        q = ("?" + urllib.parse.urlencode(params, safe=",.()*:")) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        for attempt in (0, 1):
            headers = {"apikey": self.key, "Authorization": "Bearer " + self.access_token(),
                       "Content-Type": "application/json", "Accept": "application/json"}
            if prefer:
                headers["Prefer"] = prefer
            status, raw = self.http(method, f"{self.url}/rest/v1/{path.lstrip('/')}{q}", headers, data)
            if status == 401 and attempt == 0:
                with self.lock:
                    if not self.refresh():
                        raise CloudError("Not signed in", 401)
                continue
            if status >= 400:
                msg = raw.decode("utf-8", "replace")[:300]
                raise CloudError(f"{method} {path} failed (HTTP {status}): {msg}", status)
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------ profile
    def set_ch_username(self, name: str | None):
        uid = self.user().get("id")
        if uid:
            self.rest("PATCH", "profiles", {"id": f"eq.{uid}"}, {"ch_username": name or None}, "return=minimal")

    def get_profile(self) -> dict:
        uid = self.user().get("id")
        rows = self.rest("GET", "profiles", {"id": f"eq.{uid}", "select": "*"}) if uid else None
        return (rows or [{}])[0]


# ====================================================================== sync
class CloudSync:
    """Favourites and plays, two-way. All network failures are swallowed and retried later."""

    def __init__(self, cloud: Cloud, store, libmgr, playlog, state_path: Path | None = None):
        self.cloud, self.store, self.lm, self.playlog = cloud, store, libmgr, playlog
        self.state_path = state_path or (config_dir() / "cloud_state.json")
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.cloud_only: list[dict] = []     # cloud favourites not in this library
        self.not_found: set[str] = set()     # cloud favourites Chorus Encore doesn't have
        self.syncing = False
        self.state = {"tombstones": {}, "synced": [], "plays": {}, "lastSync": None, "lastError": None}
        try:
            s = json.loads(self.state_path.read_text("utf-8"))
            if isinstance(s, dict):
                self.state.update(s)
        except (OSError, ValueError):
            pass

    def _save_state(self):
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), "utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            log.debug("Could not save the cloud state: %s", e)

    # ------------------------------------------------------------ favourites: local view
    def _songs(self):
        with self.lm.lock:
            if not self.lm.by_id:
                self.lm._reindex()
            return list(self.lm.by_id.values())

    def fav_key_of(self, s) -> str:
        """Cloud song_key of one chart: its checksum when known, else artist-name|charter|relative path."""
        return self.lm.chart_key(s)

    @staticmethod
    def is_legacy_key(k: str) -> bool:
        """Pre-2.1.1 cloud keys were song-level: norm_key(artist - name)|norm_key(charter)."""
        return k.count("|") == 1

    def _record(self, s) -> dict:
        m = s.meta
        src = self.lm.source_of(s)
        return {"song_key": self.fav_key_of(s), "checksum": self.lm.chart_md5(s),
                "artist": s.artist, "name": s.name, "charter": strip_tags(m.get("charter", "") or m.get("frets", "")),
                "album": strip_tags(m.get("album", "")), "source": src}

    def _tomb_keys(self, s) -> set[str]:
        """Every cloud key that may name this chart (checksum, path, old song-level, install aliases)."""
        ks = {self.lm.chart_pid(s), self.lm.legacy_key(s), self.fav_key_of(s)}
        md5 = self.lm.chart_md5(s)
        if md5:
            ks.add(md5)
        mine = {k for k in ks}
        for rk, lk in (self.state.get("aliases") or {}).items():
            if lk in mine:
                ks.add(rk)
        return ks

    def local_favourites(self) -> dict[str, dict]:
        try:
            self.lm._hash_index()
        except Exception as e:
            log.debug("hash index failed: %s", e)
        out = {}
        sets = self.lm._fav_sets()
        for s in self._songs():
            if self.lm.is_fav(s, sets):
                out.setdefault(self.fav_key_of(s), self._record(s))
        return out

    def missing_local_favourites(self) -> dict[str, dict]:
        """Favourites in the store whose chart is not on this machine (trashed, moved away), as cloud rows."""
        out = {}
        for r in self.lm.missing_favs():
            out.setdefault(r["song_key"], r)
        return out

    # ------------------------------------------------------------ favourites: change hook
    def fav_changed(self, songs: list, value: bool):
        """Called by the library manager after charts are starred or unstarred. Removals are recorded even
        when signed out, so they are sent (and not undone by a pull) at the next sync."""
        with self.lock:
            for s in songs:
                for k in self._tomb_keys(s):
                    if value:
                        self.state["tombstones"].pop(k, None)
                    else:
                        self.state["tombstones"][k] = int(time.time())
            self._save_state()
        self.schedule()

    def forget_keys(self, keys):
        """Explicit removal of favourites by cloud key (charters not on this machine): delete them in the cloud too."""
        with self.lock:
            for k in keys:
                self.state["tombstones"][k] = int(time.time())
            self._save_state()
        self.schedule()

    def schedule(self, delay: float = DEBOUNCE_S):
        if not self.cloud.signed_in:
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(delay, self.sync_all)
            self.timer.daemon = True
            self.timer.start()

    # ------------------------------------------------------------ favourites: sync
    @staticmethod
    def _added_ts(row: dict) -> float:
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(row.get("added_at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    def sync_favorites(self):
        c, lm = self.cloud, self.lm
        remote = {r["song_key"]: r for r in (c.rest("GET", "favorites", {"select": "*", "order": "added_at.asc"}) or [])}
        # 1. removals made here: delete the cloud rows (unless re-added elsewhere after the removal)
        for k, ts in list(self.state["tombstones"].items()):
            row = remote.get(k)
            if row and self._added_ts(row) > ts:
                with self.lock:
                    self.state["tombstones"].pop(k, None)
                continue
            c.rest("DELETE", "favorites", {"song_key": f"eq.{k}"}, prefer="return=minimal")
            remote.pop(k, None)
            with self.lock:
                self.state["tombstones"].pop(k, None)
                self._save_state()
        try:
            lm._hash_index()
        except Exception as e:
            log.debug("hash index failed: %s", e)
        songs = self._songs()
        sets = lm._fav_sets()
        keyed = {}
        for s in songs:
            keyed.setdefault(self.fav_key_of(s), s)
        # 2. removals made on another machine: a chart that was in the cloud at the last sync and is gone now
        synced = set(self.state.get("synced") or [])
        gone = [s for k, s in keyed.items() if lm.is_fav(s, sets) and k in synced and k not in remote]
        if gone:
            lm.set_fav(gone, False, notify=False)
            sets = lm._fav_sets()
        # 3. pull: cloud favourites that match a chart here become favourites of that chart only
        by_legacy: dict[str, list] = {}
        by_sum = {}
        for s in songs:
            by_legacy.setdefault(lm.legacy_key(s), []).append(s)
            h = lm.chart_md5(s)
            if h:
                by_sum[h] = s
        aliases = self.state.get("aliases", {})
        tomb = dict(self.state["tombstones"])
        cloud_only, stale, starred = [], [], []
        for k, r in remote.items():
            if k in tomb:
                continue
            chk = (r.get("checksum") or "").upper()
            exact = keyed.get(k)
            targets = [exact] if exact else []
            if not targets and aliases.get(k) in keyed:
                targets = [keyed[aliases[k]]]
            if not targets and chk and chk in by_sum:
                targets = [by_sum[chk]]
            if not targets:
                lk = k if self.is_legacy_key(k) else song_key(r.get("artist") or "", r.get("name") or "", r.get("charter") or "")
                cands = by_legacy.get(lk, [])
                if self.is_legacy_key(k):
                    targets = cands
                elif len(cands) == 1:
                    targets = cands
            if not targets:
                cloud_only.append(r)
                continue
            if self.is_legacy_key(k):
                # an old song-level row: only star when no copy of the song is a favourite yet
                if not any(lm.is_fav(s, sets) for s in targets):
                    starred += targets
            else:
                starred += [s for s in targets if not lm.is_fav(s, sets)]
            if exact is None or self.fav_key_of(targets[0]) != k:
                stale.append(k)                 # keyed differently from the chart here: re-key below
        if starred:
            lm.set_fav(starred, True, notify=False)
            sets = lm._fav_sets()
        # 4. upload what's new here (or lacks a checksum in the cloud) and replace re-keyed rows
        for k in stale:
            c.rest("DELETE", "favorites", {"song_key": f"eq.{k}"}, prefer="return=minimal")
            remote.pop(k, None)
            self.state.get("aliases", {}).pop(k, None)
        local = self.local_favourites()
        synced_before = set(self.state.get("synced") or [])
        for k, r in self.missing_local_favourites().items():       # a trashed chart stays a favourite in the cloud
            if k not in synced_before and not self.is_legacy_key(k):
                local.setdefault(k, r)

        def newer_source(k, r):
            have = (remote.get(k) or {}).get("source") or {}
            return any(v and have.get(sk) != v for sk, v in (r.get("source") or {}).items())
        up = [r for k, r in local.items()
              if k not in remote or (r["checksum"] and not remote[k].get("checksum")) or newer_source(k, r)]
        if up:
            c.rest("POST", "favorites", {"on_conflict": "user_id,song_key"}, up,
                   "resolution=merge-duplicates,return=minimal")
            for r in up:
                remote.setdefault(r["song_key"], r)
        self.state["synced"] = sorted(remote)
        self._save_state()
        if starred or gone:
            lm.version += 1
            if lm.fav_setlist_enabled():
                threading.Thread(target=lm.sync_fav_setlist, daemon=True).start()
        self.cloud_only = cloud_only

    # ------------------------------------------------------------ plays
    def _uid(self):
        return self.cloud.user().get("id")

    def push_plays(self) -> int:
        """Upload local plays not yet uploaded for this account (tracked by an offset into plays.jsonl)."""
        uid = self._uid()
        st = self.state["plays"]
        if st.get("uid") != uid:
            st.clear()
            st.update(uid=uid, done=[])
        done = set(st.get("done") or [])
        todo = [r for r in self.playlog.local() if r["client_id"] not in done]
        sent = 0
        for i in range(0, len(todo), PLAY_BATCH):
            batch = todo[i:i + PLAY_BATCH]
            rows = [{k: v for k, v in r.items()} for r in batch]
            self.cloud.rest("POST", "plays", {"on_conflict": "user_id,client_id"}, rows,
                            "resolution=merge-duplicates,return=minimal")
            done.update(r["client_id"] for r in batch)
            st["done"] = sorted(done)
            self._save_state()
            sent += len(batch)
        return sent

    def pull_plays(self) -> int:
        """Merge cloud plays into the local cache (all of them on the first pull, then only newer ones)."""
        uid = self._uid()
        st = self.state.setdefault("pull", {})
        if st.get("uid") != uid:
            self.playlog.clear_cloud()
            st.clear()
            st["uid"] = uid
        added, offset = 0, 0
        params = {"select": "*", "order": "played_at.asc,id.asc", "limit": 1000}
        if st.get("since"):
            params["played_at"] = f"gte.{st['since']}"
        while True:
            rows = self.cloud.rest("GET", "plays", {**params, "offset": offset}) or []
            for r in rows:
                r.pop("id", None)
                r.pop("user_id", None)
            added += self.playlog.add_cloud(rows)
            if rows:
                st["since"] = rows[-1]["played_at"]
            if len(rows) < 1000:
                break
            offset += 1000
        self._save_state()
        return added

    def on_new_plays(self, _records):
        self.schedule()

    # ------------------------------------------------------------ everything
    def sync_all(self) -> dict:
        res = {"ok": False}
        if not self.cloud.signed_in:
            return res
        with self.lock:
            if self.syncing:
                return res
            self.syncing = True
        try:
            for name, fn in (("favorites", self.sync_favorites), ("pushed", self.push_plays), ("pulled", self.pull_plays)):
                res[name] = fn()
            self.state["lastSync"] = int(time.time())
            self.state["lastError"] = None
            res["ok"] = True
        except CloudError as e:
            self.state["lastError"] = str(e)
            log.info("Cloud sync postponed: %s", e)
        except Exception:
            self.state["lastError"] = "unexpected error"
            log.exception("Cloud sync failed")
        finally:
            self.syncing = False
            self._save_state()
        return res

    def pending(self) -> int:
        n = len(self.state["tombstones"])
        st = self.state.get("plays") or {}
        if st.get("uid") == self._uid():
            done = set(st.get("done") or [])
            n += sum(1 for r in self.playlog.local() if r["client_id"] not in done)
        else:
            n += len(self.playlog.local())
        return n

    def status(self) -> dict:
        u = self.cloud.user()
        return {"signedIn": self.cloud.signed_in, "email": u.get("email"), "name": u.get("name"),
                "avatar": u.get("avatar"), "lastSync": self.state.get("lastSync"),
                "pending": self.pending() if self.cloud.signed_in else 0,
                "syncing": self.syncing, "error": self.state.get("lastError"),
                "cloudOnly": len(self.cloud_only)}

    def reset_account_state(self):
        """After sign-out: forget per-account progress so the next account starts clean."""
        self.state.update(tombstones={}, synced=[], plays={}, pull={}, aliases={}, lastSync=None, lastError=None)
        self.cloud_only = []
        self.playlog.clear_cloud()
        self._save_state()


# ====================================================================== reinstalling favourites
def _enc_hashes(r: dict) -> set:
    return {(r.get("md5") or "").lower(), (r.get("chartMd5") or "").lower()} - {""}


def resolve_on_encore(row: dict, search=None) -> dict | None:
    """Find the Chorus Encore chart for a cloud favourite (see favmatch): the recorded md5 first, then the
    best title match by the same artist (bracketed suffixes ignored) and charter. An empty charter accepts
    any, returned as a copy with guess=True."""
    if search is None:
        from .sources import encore
        search = encore.search
    return favmatch.find(row, search, favmatch.queries_for(row), 48, _enc_hashes)


def _rv_hashes(r: dict) -> set:
    return {h.lower() for h in (r.get("hashes") or []) if h}


def resolve_on_rhythmverse(row: dict, search=None) -> dict | None:
    """Find the RhythmVerse chart for a cloud favourite: the recorded RhythmVerse file id if it is among the
    results, else the best match as for Encore (colour tags on the charter are ignored). RhythmVerse search
    is word-order sensitive, so the title and the artist are searched separately before both together."""
    if search is None:
        from .sources import rhythmverse
        search = rhythmverse.search
    want_id = (row.get("source") or {}).get("rv_id") or ""
    return favmatch.find({**row, "charter": strip_tags(row.get("charter") or "")}, search,
                         favmatch.queries_for(row, rv=True), 48,
                         lambda r: _rv_hashes(r), (lambda r: r.get("id") == want_id) if want_id else None)


def source_order(row: dict) -> list[str]:
    """Where to look first: the place the chart was recorded as coming from, then the other one."""
    src = row.get("source") or {}
    from_rv = bool(src.get("rv_id") or "rhythmverse" in (src.get("rv_url") or "").lower()
                   or "rhythmverse" in (src.get("url") or "").lower())
    return ["rhythmverse", "encore"] if from_rv and not src.get("encore_md5") else \
        ["encore", "rhythmverse"]


def resolve_favourite(row: dict, encore_search=None, rv_search=None) -> dict | None:
    """Resolve a favourite to an installable chart: recorded source first, then Chorus Encore, then
    RhythmVerse. None when no source has it by the same charter. Raises only when nothing matched and a
    lookup failed (so 'not found' is never claimed after an outage)."""
    errors = []
    for name in source_order(row):
        try:
            item = resolve_on_encore(row, encore_search) if name == "encore" else resolve_on_rhythmverse(row, rv_search)
        except Exception as e:
            log.info("%s lookup failed for %s: %s", name, row.get("name"), e)
            errors.append(e)
            continue
        if item:
            return item
    if errors:
        raise errors[0]
    return None
