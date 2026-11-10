"""JSON API behind the desktop app's library manager UI."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from .process import popen as popen_process
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import hwvideo, ytupdate
from . import __version__, youtube
from .jobs import Job, PRIO_BATCH, PRIO_USER
from .libmgr import find_game, launch_game, reveal, song_id
from .library import norm_key
from . import i18n
from .i18n import _, join, msg_of
from .lyrics import clean_title

log = logging.getLogger(__name__)


def _json(h, obj, code=200):
    h._json(obj, code)


def handle(h, method: str, path: str, query: dict, body: dict) -> bool:
    """Returns True if the route was handled."""
    c = h.companion
    lm = c.libmgr
    q = {k: v[-1] for k, v in query.items()}

    # ---------------------------------------------------------------- library
    if path == "/api/library" and method == "GET":
        return _json(h, {"songs": lm.entries(), "version": lm.version, "enriching": lm.enriching,
                         "enrich": lm.enrich_progress, "roots": [str(r) for r in c.library.roots]}) or True
    if path == "/api/library/version":
        return _json(h, {"version": lm.version, "enriching": lm.enriching, "enrich": lm.enrich_progress}) or True
    if path == "/api/library/rescan" and method == "POST":
        threading.Thread(target=lambda: (lm.rescan(), c.build_preview_index()), daemon=True).start()
        return _json(h, {"ok": True}) or True
    if path.startswith("/api/song/") and method == "GET":
        sid = path.split("/")[3]
        s = lm.get(sid)
        if not s:
            return _json(h, {"error": _("api.not_found")}, 404) or True
        e = lm.entry(s)
        per = c.store.song(s.key)
        e.update({"meta": {k: v for k, v in s.meta.items() if len(str(v)) < 300},
                  "files": s.list_files(), "videoId": per.get("video_id"), "candidates": per.get("candidates", []),
                  "videoOffset": per.get("video_offset", 0.0), "lyricsOffset": per.get("lyrics_offset", 0.0),
                  "videoRes": per.get("video_res"), "videoStart": s.video_start_s,
                  "videoOffsetWritten": per.get("video_offset_written", 0.0) or 0.0,
                  "skip": bool(per.get("game_skip")),
                  "status": c.song_job_status(s)})
        return _json(h, e) or True
    if path == "/api/songs/action" and method == "POST":
        ids, act = body.get("ids") or [], body.get("action")
        n = 0
        if act == "favorite":
            lm.set_fav(lm.songs(ids), bool(body.get("value", True)))
        elif act == "fav_sync":
            n = lm.sync_fav_setlist()
        elif act == "rate":
            lm.set_flag(ids, rating=int(body.get("value", 0)) or None)
        elif act == "disable":
            n = lm.disable(ids)
        elif act == "enable":
            n = lm.disable(ids, enable=True)
        elif act == "delete":
            # big deletes take a while (one Trash call per chart): run in the background, the UI polls
            return _json(h, {"ok": True, "op": lm.start_delete(ids, after=c.build_preview_index)}) or True
        elif act == "rename":
            n = lm.rename(ids, body.get("template") or "{artist} - {name}")
        elif act == "restore":
            from . import ingame
            for s in lm.songs(ids):
                ingame.restore(s, c.store)
                c.store.update_song(s.key, video_failed=None, lyrics_none=None)
                lm.refresh_song(s)
                n += 1
        elif act == "pack":
            out = lm.export_pack(ids, body.get("name") or "Chart Pack")
            reveal(out)
            return _json(h, {"ok": True, "path": str(out)}) or True
        elif act == "reveal":
            for s in lm.songs(ids[:3]):
                reveal(s.path)
        elif act == "skip":
            lm.set_flag(ids, game_skip=bool(body.get("value", True)) or None)
        else:
            return _json(h, {"error": _("api.unknown_action")}, 400) or True
        if act in ("disable", "enable", "rename"):
            c.build_preview_index()
        return _json(h, {"ok": True, "count": n}) or True
    if path == "/api/songs/op" and method == "POST":
        st = lm.op_status(body.get("id") or "")
        return _json(h, st if st else {"error": _("api.unknown_op")}, 200 if st else 404) or True
    if path == "/api/duplicates":
        return _json(h, {"groups": lm.duplicates()}) or True

    # ---------------------------------------------------------------- jobs
    if path == "/api/jobs" and method == "GET":
        snap = c.jobs.snapshot()
        snap["youtube"] = youtube.GATE.status()
        snap["ytdlp"] = ytupdate.active_info()
        snap["now"] = {"status": c.clock.status, "song": c.clock.song}
        snap["convert"] = c.convert_status()
        return _json(h, snap) or True
    if path == "/api/videos/convert" and method == "POST":
        return _json(h, {"queued": c.convert_videos()}) or True
    if path == "/api/videos/convert/later" and method == "POST":
        c.update_settings({"video_convert_asked": __version__})
        return _json(h, c.convert_status()) or True
    if path == "/api/videos/convert/cancel" and method == "POST":
        c.jobs.cancel(kind="convert")
        return _json(h, {"ok": True}) or True
    if path == "/api/jobs" and method == "POST":
        kind = body.get("kind", "full")
        prio = PRIO_USER if len(body.get("ids") or []) <= 3 else PRIO_BATCH
        opts = body.get("options") or {}
        n = 0
        extra = {}
        if body.get("installMissing"):
            # Run in Favourites: install the missing favourites first. Their download jobs add video, lyrics
            # and metadata themselves (process=True), so only the charts already here are queued below.
            if _missing_favs(c):
                extra = {"installing": _install_cloud_favs(c, {"process": True})}
        for s in lm.songs(body.get("ids") or []):
            c.queue_song(s, kind, prio, **opts)
            n += 1
        return _json(h, {"queued": n, **extra}) or True
    if path == "/api/jobs/cancel" and method == "POST":
        if "id" in body and not body["id"]:     # a per-job cancel without an id must never mean "all"
            return _json(h, {"ok": False}) or True
        c.jobs.cancel(body.get("id"), body.get("kind"))
        return _json(h, {"ok": True}) or True
    if path == "/api/jobs/retry" and method == "POST":
        c.jobs.retry_failed()
        return _json(h, {"ok": True}) or True
    if path == "/api/jobs/clear" and method == "POST":
        c.jobs.clear_finished()
        return _json(h, {"ok": True}) or True

    # ---------------------------------------------------------------- per-song video choices
    if path.startswith("/api/song/") and path.endswith("/candidates") and method == "POST":
        s = lm.get(path.split("/")[3])
        if not s:
            return _json(h, {"error": _("api.not_found")}, 404) or True
        cands = c.game_yt.search(s)
        with ThreadPoolExecutor(4) as ex:   # check which candidates exist in HD
            res = list(ex.map(lambda cd: _safe_res(c, cd["id"]), cands[:6]))
        for cd, r in zip(cands, res):
            cd["res"] = r
        c.store.update_song(s.key, candidates=cands[:8])
        return _json(h, {"candidates": cands[:8]}) or True
    if path.startswith("/api/song/") and path.endswith("/use-video") and method == "POST":
        s = lm.get(path.split("/")[3])
        from .app import YT_ID
        m = YT_ID.search(str(body.get("video", "")).strip())
        if not s or not m:
            return _json(h, {"error": _("api.not_youtube")}, 400) or True
        r = _safe_res(c, m.group(1))
        if r < youtube.MIN_P:
            return _json(h, {"error": _("api.video_too_low", p=youtube.MIN_P)}, 400) or True
        c.store.update_song(s.key, video_id=m.group(1), video_manual=True, video_offset_auto=True)
        c.store.update_song(s.key, video_offset=None)
        c.queue_song(s, "media", PRIO_USER, force=True)
        return _json(h, {"ok": True, "res": r}) or True
    if path.startswith("/api/song/") and path.endswith("/offsets") and method == "POST":
        s = lm.get(path.split("/")[3])
        if not s:
            return _json(h, {"error": _("api.not_found")}, 404) or True
        upd = {}
        if "video" in body:
            upd.update(video_offset=round(float(body["video"]), 3), video_offset_auto=None)
        if "lyrics" in body:
            upd.update(lyrics_offset=round(float(body["lyrics"]), 3), lyrics_offset_auto=None)
        c.store.update_song(s.key, **upd)
        if body.get("apply"):
            c.queue_song(s, "media", PRIO_USER, resync=True)
        return _json(h, {"ok": True}) or True

    # ---------------------------------------------------------------- media for the UI
    if path.startswith("/media/art/"):
        s = lm.get(path.split("/")[3])
        art = s.art_file() if s else None
        if not art:
            h._send(404, b"", "text/plain")
            return True
        import mimetypes
        h._send(200, art[1], mimetypes.guess_type(art[0])[0] or "image/jpeg",
                {"Cache-Control": "max-age=3600"})
        return True
    if path.startswith("/media/video/"):
        s = lm.get(path.split("/")[3])
        v = s.local_video() if s else None
        if not v:
            h._send(404, b"", "text/plain")
            return True
        h._serve_file(v)
        return True
    if path.startswith("/media/audio/"):
        s = lm.get(path.split("/")[3])
        if not s:
            h._send(404, b"", "text/plain")
            return True
        _stream_audio(h, lm, s, float(q.get("t", 0) or 0))
        return True

    if path == "/media/chart-preview":
        _chart_preview(h, q.get("url", ""), float(q.get("t", 0) or 0))
        return True

    # ---------------------------------------------------------------- online sources
    if path == "/api/search":
        from .sources import encore, rhythmverse
        db = q.get("db", "encore")
        args = dict(page=int(q.get("page", 1)), per_page=int(q.get("per", 50)),
                    instrument=q.get("instrument") or None, difficulty=q.get("difficulty") or None,
                    sort=q.get("sort") or None, direction=q.get("dir", "asc"))
        try:
            res = (encore.search(q.get("q", ""), **args) if db == "encore"
                   else rhythmverse.search(q.get("q", ""), system=q.get("system", "ch"), **args))
        except Exception as e:
            return _json(h, {"error": _("api.search_failed", detail=msg_of(e))}, 502) or True
        from .library import strip_tags
        owned: dict[str, set] = {}
        for s in c.library.songs:
            k = norm_key(f"{clean_title(s.artist)} {clean_title(s.name)}")
            owned.setdefault(k, set()).add(strip_tags(s.meta.get("charter", "") or s.meta.get("frets", "")).strip())
        for r in res["songs"]:
            mine = owned.get(norm_key(f"{clean_title(r['artist'])} {clean_title(r['name'])}"))
            r["owned"] = mine is not None
            if mine is not None:
                ch = norm_key(r.get("charter") or "")
                r["ownedSameChart"] = bool(ch) and any(norm_key(x) == ch for x in mine)
                r["ownedCharters"] = sorted(x for x in mine if x)[:3]
        return _json(h, res) or True
    if path == "/api/download" and method == "POST":
        n = 0
        for it in body.get("items") or []:
            title = f"{it.get('artist', '')} - {it.get('name', '')}"
            c.jobs.submit(Job("download", "Download: " + title, item=it,
                              options={"process": body.get("process", c.settings()["auto_process_downloads"])},
                              priority=PRIO_USER))
            n += 1
        return _json(h, {"queued": n}) or True
    if path == "/api/spotify" and method == "POST":
        from .sources import encore, spotify
        try:
            pl = spotify.fetch_playlist(body.get("url", ""))
        except Exception as e:
            return _json(h, {"error": msg_of(e)}, 400) or True
        owned = {norm_key(f"{clean_title(s.artist)} {clean_title(s.name)}"): song_id(s) for s in c.library.songs}

        def base_title(title: str) -> str:
            t = re.sub(r"\s*[-–]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|live|mono|stereo|single|radio edit|edit|"
                       r"version|mix|acoustic|demo|bonus)\b.*$", "", title, flags=re.I)
            t = re.sub(r"\s*[\(\[][^)\]]*(?:remaster|version|edit|mono|stereo|feat\.?|with )[^)\]]*[\)\]]", "", t,
                       flags=re.I)
            return t.strip() or title

        def match(t):
            t = {**t, "title": base_title(t["title"])}
            key = norm_key(f"{t['artist']} {t['title']}")
            if key in owned:
                return {**t, "owned": True, "songId": owned[key], "match": None}
            try:
                res = encore.search(f"{t['artist']} {t['title']}", per_page=10)["songs"]
            except Exception:
                res = []
            best = None
            for r in res:
                if norm_key(clean_title(r["name"])) == norm_key(t["title"]) and \
                        norm_key(t["artist"]) in norm_key(r["artist"]):
                    best = r
                    break
            return {**t, "owned": False, "match": best}
        with ThreadPoolExecutor(6) as ex:
            tracks = list(ex.map(match, pl["tracks"]))
        return _json(h, {"name": pl["name"], "tracks": tracks, "truncated": pl["truncated"]}) or True

    # ---------------------------------------------------------------- setlists
    if path == "/api/setlists" and method == "GET":
        return _json(h, {"setlists": lm.setlists(), "signedIn": c.cloud.signed_in}) or True
    if path == "/api/setlists" and method == "POST":
        from . import setlists as sl
        act, name = body.get("action"), body.get("name", "")
        try:
            if act == "add":
                n = lm.setlist_add(name, body.get("ids") or [])
                return _json(h, {"ok": True, "added": n}) or True
            if act == "remove":
                lm.setlist_remove(name, body.get("ids") or [])
            elif act == "delete":
                sl.delete(c.data_dir, name)
            elif act == "create":
                sl.save(c.data_dir, name, [])
        except ValueError as e:
            return _json(h, {"error": msg_of(e)}, 400) or True
        c.cloudsync.schedule()                  # signed in: the change reaches the cloud copy of the list (~3 s debounce)
        return _json(h, {"ok": True}) or True

    # ---------------------------------------------------------------- friends and sharing
    if path.startswith("/api/friends") and _friends(h, c, method, path, q, body):
        return True

    # ---------------------------------------------------------------- cloud account
    if path == "/auth/callback" and method == "GET":
        return _auth_callback(h, c, q) or True
    if path == "/api/cloud/status" and method == "GET":
        return _json(h, c.cloudsync.status()) or True
    if path == "/api/cloud/login" and method == "POST":
        url = c.cloud.start_login(h.server.server_address[1])
        return _json(h, {"ok": True, "url": url}) or True
    if path in ("/api/cloud/email/send", "/api/cloud/email/verify", "/api/cloud/name") and method == "POST":
        from .cloud import CloudError
        try:
            if path.endswith("/send"):
                c.cloud.send_email_code(body.get("email", ""))
            elif path.endswith("/verify"):
                c.cloud.verify_email_code(body.get("email", ""), body.get("code", ""))
                threading.Thread(target=c.after_login, daemon=True, name="cloud-sync").start()
            else:
                c.cloud.set_display_name(body.get("name", ""))
        except CloudError as e:
            code = e.status if e.status in (429, 401) else 400
            return _json(h, {"error": msg_of(e)}, 503 if e.offline else code) or True
        return _json(h, c.cloudsync.status()) or True
    if path == "/api/cloud/logout" and method == "POST":
        c.cloud.logout()
        c.cloudsync.reset_account_state()
        c.friends.reset()
        return _json(h, c.cloudsync.status()) or True
    if path == "/api/cloud/sync" and method == "POST":
        if c.cloud.signed_in:
            def run():
                c.refresh_ch_profile()      # Sync now: fetch the CH Leaderboards profile live first, so the snapshot is current
                c.cloudsync.sync_all()
            threading.Thread(target=run, daemon=True, name="cloud-sync").start()
        return _json(h, c.cloudsync.status()) or True
    if path == "/api/cloud/favorites" and method == "GET":
        rows = _missing_favs(c)
        return _json(h, {"items": [_cloud_fav(r, c) for r in rows], "counts": fav_counts(c, rows)}) or True
    if path == "/api/cloud/favorites/remove" and method == "POST":
        keys = [k for k in (body.get("keys") or []) if isinstance(k, str)]
        n = lm.remove_favs(keys)
        c.cloudsync.forget_keys(keys)
        return _json(h, {"removed": n}) or True
    if path == "/api/cloud/favorites/install" and method == "POST":
        return _json(h, _install_cloud_favs(c, body)) or True

    # ---------------------------------------------------------------- play statistics
    if path == "/api/stats" and method == "GET":
        from . import playstats
        _refresh_lb_plays(c)
        plays = c.playlog.all()
        res = playstats.compute(plays, (q.get("instrument") or "").lower(), (q.get("difficulty") or "").lower(),
                                scoredata=c._scoredata_rows(plays))
        _link_songs(lm, res)
        aid = int(c.settings().get("ch_account_id") or 0)
        if aid:
            from .leaderboard import LeaderboardError
            try:
                prof = c.lb.profile(aid, max_age=PROFILE_MAX_AGE_S)      # live: never the 10-minute response cache
            except LeaderboardError:
                prof = None
            res["account"] = playstats.account_section(prof, c.settings().get("ch_username") or "")
            _push_career_if_fresh(c, prof)
        n_lb = len(playstats.dedupe_leaderboard(c.playlog.leaderboard(), c.playlog.all(False)))
        res["sources"].update({"local": len(c.playlog.local()), "cloud": len(c.playlog.cloud()), "leaderboard": n_lb,
                               "leaderboardRefreshing": c.playlog.lb_refreshing})
        res["cloud"] = {"signedIn": c.cloud.signed_in}
        res["ch"] = {"username": c.settings().get("ch_username") or "", "accountId": c.settings().get("ch_account_id") or 0}
        return _json(h, res) or True

    # ---------------------------------------------------------------- CH Leaderboards (read-only)
    if path.startswith("/api/lb/"):
        from .leaderboard import LeaderboardError
        try:
            return _lb(h, c, lm, path, method, q, body) or True
        except LeaderboardError as e:
            return _json(h, {"error": msg_of(e)}, 502) or True

    # ---------------------------------------------------------------- app updates (open even while an update is required)
    if path == "/api/update" and method == "GET":
        return _json(h, c.updater.public()) or True
    if path == "/api/update/check" and method == "POST":
        return _json(h, c.updater.check()) or True
    if path == "/api/update/install" and method == "POST":
        started = c.updater.start_install()
        return _json(h, dict(c.updater.public(), started=started)) or True

    # ---------------------------------------------------------------- app / settings
    if path == "/api/settings" and method == "GET":
        from .align import ffmpeg_path
        game = find_game(c.data_dir, c.settings().get("game_path"))
        return _json(h, {"settings": c.settings(), "dataDir": str(c.data_dir),
                         "songDirs": [str(r) for r in c.library.roots], "game": str(game) if game else None,
                         "version": __version__, "ytdlp": _ytdlp_version(),
                         "jsRuntime": _js_label(), "jsHelp": youtube.JS_HELP,
                         "ejs": youtube.ejs_status(), "ffmpeg": bool(ffmpeg_path()),
                         "videoPath": hwvideo.describe() if ffmpeg_path() else "",
                         "minP": youtube.MIN_P, "youtube": youtube.GATE.status(),
                         "ytdlpInfo": ytupdate.active_info()}) or True
    if path == "/api/settings" and method == "POST":
        s = body.get("settings") or {}
        if "pref_p" in s:
            s["pref_p"] = max(youtube.MIN_P, int(s["pref_p"]))
        return _json(h, {"settings": c.update_settings(s), "youtube": youtube.GATE.status()}) or True
    if path == "/api/ytdlp/check" and method == "POST":
        ytupdate.check_now_async()
        return _json(h, ytupdate.active_info()) or True
    if path == "/api/setup" and method == "GET":
        from . import paths as P
        s = c.settings()
        det = P.detect(s.get("game_path") or None)
        game = find_game(c.data_dir, s.get("game_path"))
        cur = {"game": str(game) if game else "", "dataDir": str(c.data_dir),
               "songDirs": [str(r) for r in c.library.roots]}
        return _json(h, {"detected": det, "current": cur, "setupDone": bool(s.get("setup_done")),
                         "dataOk": P.looks_like_data_dir(c.data_dir),
                         "check": {"game": P.describe_dir(cur["game"], "game") if cur["game"] else None,
                                   "data": P.describe_dir(c.data_dir, "data"),
                                   "songs": [P.describe_dir(r, "songs") for r in cur["songDirs"]]}}) or True
    if path == "/api/setup/check" and method == "POST":
        from . import paths as P
        kind = body.get("kind", "songs")
        p = (body.get("path") or "").strip()
        if not p:
            return _json(h, {"ok": False}) or True
        d = P.describe_dir(p, kind, check_write=True)
        if kind == "data" and d.get("ok"):
            d["songDirs"] = [str(x) for x in P.song_dirs(Path(p).expanduser())]
        if kind == "game" and d.get("ok"):
            gd = P.game_dir(Path(p).expanduser())
            d["dataDir"] = str(gd) if P.looks_like_data_dir(gd) else None
            d["songsDir"] = str(gd / "Songs") if (gd / "Songs").is_dir() else None
        return _json(h, d) or True
    if path == "/api/setup" and method == "POST":
        from . import paths as P
        data = Path((body.get("dataDir") or "").strip() or str(c.data_dir)).expanduser()
        songs = [str(Path(x.strip()).expanduser()) for x in (body.get("songDirs") or []) if x and x.strip()]
        game = (body.get("game") or "").strip()
        problems = []
        if game and not P.is_game(Path(game).expanduser()):
            problems.append(_("api.game_missing", path=game))
        if body.get("createData") and not data.exists():
            try:
                P.ensure_writable_dir(data)
            except RuntimeError as e:
                return _json(h, {"error": msg_of(e)}, 400) or True
        if not data.is_dir():
            problems.append(_("api.data_missing", path=data))
        missing = [x for x in songs if not Path(x).is_dir()]
        if missing:
            problems.append(_("api.songs_missing", list=", ".join(missing)))
        if problems:
            return _json(h, {"error": join(problems)}, 400) or True
        roots = P.song_dirs(data, only=songs or None)
        # Verify the selected destinations before declaring setup complete.
        # A readable Documents folder can still reject writes through Defender.
        try:
            for folder in dict.fromkeys([data, *(Path(x) for x in songs),
                                         *(roots or [data / "Songs"])]):
                P.ensure_writable_dir(folder)
        except RuntimeError as e:
            return _json(h, {"error": msg_of(e)}, 400) or True
        c.update_settings({"data_dir": str(data), "song_dirs": songs, "game_path": game, "setup_done": True})
        roots = P.song_dirs(data, only=songs or None)
        c.reconfigure(data, roots)
        return _json(h, {"ok": True, "dataDir": str(data), "songDirs": [str(r) for r in roots]}) or True
    if path == "/api/macperm" and method == "GET":
        from . import macperm
        return _json(h, {"applicable": macperm.applicable(),
                         "fda": macperm.has_full_disk_access(fresh=True),
                         "bundle": str(macperm.app_bundle() or "")}) or True
    if path == "/api/macperm/open" and method == "POST":
        from . import macperm
        return _json(h, {"ok": macperm.open_settings()}) or True
    if path == "/api/desktop-entry":   # Linux: optional app-menu entry (see desktop_entry.py)
        from . import desktop_entry as de
        if method == "POST":
            try:
                return _json(h, {"ok": bool(de.install()), "installed": True}) or True
            except RuntimeError as e:
                return _json(h, {"ok": False, "error": str(e)}) or True
        return _json(h, {"applicable": de.applicable() and de.launcher_path() is not None,
                         "installed": de.is_installed()}) or True
    if path == "/api/restart" and method == "POST":
        from . import macperm
        ok = macperm.relaunch(c.quit_requested)
        return _json(h, {"ok": ok}) or True
    if path == "/api/browse" and method == "POST":
        from .gui import pick_path
        res = pick_path(body.get("kind", "folder"), body.get("start") or "")
        return _json(h, {"path": res, "native": res is not None or None}) or True
    if path == "/api/window" and method == "POST":
        from .gui import open_extra_window
        port = h.server.server_address[1]
        page = body.get("page", "now")
        url = f"http://127.0.0.1:{port}/{'now' if page == 'now' else ''}"
        kind = open_extra_window(url, str(_("window.now_title")), bool(body.get("fullscreen")))
        return _json(h, {"ok": True, "kind": kind}) or True
    if path == "/api/game/launch" and method == "POST":
        game = find_game(c.data_dir, c.settings().get("game_path"))
        if not game:
            return _json(h, {"error": _("api.game_not_found")}, 404) or True
        launch_game(game)
        return _json(h, {"ok": True}) or True
    if path == "/api/share/save" and method == "POST":
        try:
            out = _save_share_image(c, body)
        except ValueError as e:
            return _json(h, {"error": msg_of(e)}, 400) or True
        if body.get("reveal", True):
            reveal(out)
        return _json(h, {"path": str(out)}) or True
    if path == "/api/open" and method == "POST":
        url = str(body.get("url") or "")
        if url:        # an external link (the Share buttons) in the system browser; only the known share hosts
            import urllib.parse
            import webbrowser
            u = urllib.parse.urlparse(url)
            if u.scheme != "https" or u.hostname not in SHARE_HOSTS:
                return _json(h, {"error": _("api.not_found")}, 400) or True
            webbrowser.open(url)
            return _json(h, {"ok": True}) or True
        target = Path(body.get("path") or c.data_dir)
        if target.exists():
            reveal(target)
        return _json(h, {"ok": True}) or True
    return False


def _link_songs(lm, res: dict):
    """Set songId on every song row of a stats result that is in my library (checksum, else artist + title)."""
    idx, keyidx = lm.hash_to_id(), lm.key_to_id()
    for lst in ("mostPlayed", "best", "needsPractice", "recent", "personalBests"):
        for it in res[lst]:
            it["songId"] = idx.get((it.get("checksum") or "").upper()) or keyidx.get(
                norm_key(f"{it.get('artist', '')} - {it.get('name', '')}"))


SHARE_MAX_BYTES = 10 * 1024 * 1024
SHARE_HOSTS = {"wa.me", "www.reddit.com", "www.facebook.com", "www.instagram.com", "www.snapchat.com"}


def _save_share_image(c, body: dict) -> Path:
    """Write the stats card PNG (base64 or a data URL) to ~/Downloads (else the data folder)."""
    import base64
    import binascii
    from datetime import datetime
    from .paths import config_dir
    raw = str(body.get("png_base64") or "")
    if raw.startswith("data:"):
        raw = raw.partition(",")[2]
    if len(raw) > SHARE_MAX_BYTES * 4 // 3 + 8:
        raise ValueError(_("share.err_too_big"))
    try:
        png = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(_("share.err_bad_image"))
    if len(png) > SHARE_MAX_BYTES:
        raise ValueError(_("share.err_too_big"))
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(_("share.err_bad_image"))
    name = f"CloneHeroCompanion-stats-{datetime.now():%Y%m%d-%H%M%S}.png"
    downloads = Path.home() / "Downloads"
    for folder in ([downloads] if downloads.is_dir() else []) + [config_dir()]:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / name
            out.write_bytes(png)
            return out
        except OSError as e:
            log.warning("share image: could not write to %s: %s", folder, e)
    raise ValueError(_("share.err_write"))


def _safe_res(c, vid: str) -> int:
    try:
        return c.game_yt.best_res(vid)
    except Exception:
        return 0


def _js_label() -> str | None:
    rt = youtube.js_runtime()
    if not rt:
        return None
    dn = youtube.deno_info() if rt[0] == "deno" else None
    if dn:
        return _("about.runtime_deno", version=dn["version"] or "?", origin=_("about.bundled" if dn["bundled"] else "about.system"))
    return rt[0]


def _ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v
        return v
    except Exception:
        return "missing"


_preview_lock = threading.Lock()


def _chart_preview(h, url: str, start: float):
    """Stream 30 s of a Chorus Encore chart's audio before it is downloaded."""
    import hashlib
    import tempfile
    import urllib.request
    from .align import chart_audio_files, ffmpeg_path
    from .net import urlopen
    from .paths import cache_dir, config_dir
    from .sng import open_sng
    if not url.startswith("https://files.enchor.us/") or not url.endswith(".sng"):
        h._send(400, b"", "text/plain")
        return
    cache = config_dir() / "previews"
    cache.mkdir(exist_ok=True)
    f = cache / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".sng")
    with _preview_lock:
        if not f.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CloneHeroCompanion"})
            with urlopen(req, timeout=60) as r:
                tmp = f.with_suffix(".part")
                tmp.write_bytes(r.read())
                tmp.replace(f)
            olds = sorted(cache.glob("*.sng"), key=lambda p: p.stat().st_mtime)
            for o in olds[:-20]:   # keep the cache small
                o.unlink(missing_ok=True)
    sng = open_sng(str(f))
    names = chart_audio_files(list(sng.files))
    ff = ffmpeg_path()
    if not names or not ff:
        h._send(404, b"", "text/plain")
        return
    tmpd = Path(tempfile.mkdtemp(prefix="chc-pv-", dir=cache_dir()))
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    for n in names:
        p = tmpd / n
        p.write_bytes(sng.read(n) or b"")
        cmd += ["-ss", f"{start:.1f}", "-t", "30", "-i", str(p)]
    if len(names) > 1:
        cmd += ["-filter_complex", f"amix=inputs={len(names)}:normalize=0,alimiter=limit=0.95"]
    cmd += ["-ac", "2", "-c:a", "libmp3lame", "-b:a", "160k", "-f", "mp3", "-"]
    proc = popen_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.end_headers()
        while True:
            chunk = proc.stdout.read(32768)
            if not chunk:
                break
            h.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(tmpd, ignore_errors=True)


def _stream_audio(h, lm, s, start: float):
    try:
        cmd, tmp = lm.audio_stream_cmd(s, start)
    except Exception:
        h._send(404, b"", "text/plain")
        return
    proc = popen_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        while True:
            chunk = proc.stdout.read(32768)
            if not chunk:
                break
            h.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    finally:
        proc.kill()
        proc.wait()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


_CB_PAGE = """<!doctype html><html lang="@LANG@"><head><meta charset="utf-8"><title>Clone Hero Companion</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#160d2b;color:#f6f1e7;font:16px/1.5 "Segoe UI",system-ui,sans-serif}
main{background:#1e1338;border:1px solid rgba(246,241,231,.12);border-radius:14px;padding:34px 40px;max-width:430px;text-align:center}
.frets{display:flex;gap:6px;justify-content:center;margin-bottom:14px}.frets i{width:12px;height:12px;border-radius:50%}
h1{font-size:22px;margin:0 0 8px}p{margin:0;color:rgba(246,241,231,.7)}
</style></head><body><main><div class="frets"><i style="background:#3cc85a"></i><i style="background:#e8413a"></i><i style="background:#f2d23a"></i><i style="background:#3a8ee8"></i><i style="background:#f08a2a"></i></div>
<h1>@TITLE@</h1><p>@MSG@</p></main></body></html>"""


def _auth_callback(h, c, q: dict):
    """Google sign-in comes back here with ?code=...; exchange it and show a small result page."""
    import html
    code, err = q.get("code"), q.get("error_description") or q.get("error")
    ok, msg = False, ""
    if err or not code:
        msg = err or str(_("auth.no_code"))
    else:
        try:
            c.cloud.complete_login(code)
            ok = True
            threading.Thread(target=c.after_login, daemon=True, name="cloud-sync").start()
        except Exception as e:
            msg = str(msg_of(e))
    if ok:
        page = _CB_PAGE.replace("@TITLE@", str(_("auth.title_ok"))).replace("@MSG@", str(_("auth.close_tab")))
    else:
        page = _CB_PAGE.replace("@TITLE@", str(_("auth.title_fail"))).replace(
            "@MSG@", html.escape(msg) + " " + html.escape(str(_("auth.close_retry"))))
    page = page.replace("@LANG@", i18n.LANG)
    h._send(200 if ok else 400, page.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})


def _cloud_err(h, e) -> bool:
    """A cloud failure as a JSON error: 503 offline, 401 signed out, else the server's own status when sensible."""
    code = 503 if e.offline else e.status if e.status in (401, 403, 404, 409, 429) else 400
    h._json({"error": msg_of(e)}, code)
    return True


def _friends(h, c, method: str, path: str, q: dict, body: dict) -> bool:
    from .cloud import CloudError
    f = c.friends
    sub = path[len("/api/friends"):].strip("/")
    try:
        if sub == "status" and method == "GET":
            return _json(h, f.status()) or True
        if sub == "" and method == "GET":
            return _json(h, f.view() if q.get("cached") or not c.cloud.signed_in else f.refresh()) or True
        if sub == "username/check" and method == "GET":
            return _json(h, f.check_username(q.get("name", ""))) or True
        if sub == "username" and method == "POST":
            return _json(h, f.set_username(body.get("username", ""))) or True
        if sub == "lookup" and method == "GET":
            return _json(h, {"profile": f.lookup(q.get("q", ""))}) or True
        if sub == "request" and method == "POST":
            return _json(h, f.request(body.get("query", ""))) or True
        if sub == "respond" and method == "POST":
            return _json(h, f.respond(body.get("id", ""), bool(body.get("accept")))) or True
        if sub == "remove" and method == "POST":
            return _json(h, f.remove(body.get("id", ""))) or True
        if sub == "list" and method == "GET":
            d = f.friend_lists(q.get("friend", ""))
            d["favorites"] = f.annotate(d["favorites"])
            d["setlists"] = [{**x, "items": f.annotate(x["items"])} for x in d["setlists"]]
            return _json(h, d) or True
        if sub == "stats" and method == "GET":
            from . import playstats
            fid = q.get("id", "")
            plays = f.friend_plays(fid) if f.friend(fid) else []
            if not plays:       # not a friend, or the friend switched stats sharing off (RLS returns no rows)
                return _json(h, {"error": "not_shared"}, 403) or True
            res = playstats.compute(plays, (q.get("instrument") or "").lower(), (q.get("difficulty") or "").lower())
            _link_songs(c.libmgr, res)
            p = f.friend(fid)
            res["friend"] = {"username": p.get("username"), "name": p.get("name"), "avatar": p.get("avatar")}
            return _json(h, res) or True
        if sub == "install" and method == "POST":
            return _json(h, _install_friend(c, body)) or True
        if sub == "sharing" and method == "GET":
            return _json(h, f.sharing()) or True
        if sub == "share" and method == "POST":
            if "kind" in body and "lists_shared" not in body:       # 2.3 payload {kind, name, shared}: accepted for one version
                return _json(h, f.set_shared(body.get("kind", ""), body.get("name"), bool(body.get("shared")))) or True
            if "stats_shared" in body:
                f.set_stats_shared(bool(body["stats_shared"]))
                if "lists_shared" not in body and "shared" not in body:
                    return _json(h, f.sharing()) or True
            return _json(h, f.set_lists_shared(bool(body.get("lists_shared", body.get("shared"))))) or True
    except CloudError as e:
        return _cloud_err(h, e)
    except ValueError as e:
        return _json(h, {"error": msg_of(e)}, 400) or True
    return False


def _unique_setlist(c, name: str, friend: str = "") -> str:
    """A free local setlist name. On a collision (a file, or the derived Favourites list) append ' (from <friend>)',
    then a number if even that is taken."""
    from . import setlists as sl
    base = sl.safe(name)
    d = sl.setlists_dir(c.data_dir)

    def taken(n):
        return (d / (n + ".setlist")).exists() or n.lower() == "favourites"
    if not taken(base):
        return base
    base = sl.safe(f"{base} ({_('friends.from', name=friend)})") if friend else base
    out, n = base, 1
    while taken(out):
        n += 1
        out = f"{base} ({n})"
    return out


def _install_friend(c, body: dict) -> dict:
    """Install the missing charts of a friend's shared list through the normal pipeline (resolve_favourite:
    recorded source, then Chorus Encore, then RhythmVerse; the charter must match; download_first_working falls
    back to the next candidate) and, if asked, star them and write a Clone Hero .setlist from the local copies.
    A wanted setlist name that already exists becomes "<name> (from <friend>)". addToSetlist appends to one of mine.
    body: friend, list {kind: favorites|setlist, name}, keys? (a subset), install (default true), process
    (video, lyrics, metadata; default true), favourite (also add to my favourites), setlist (new local name)."""
    from .cloud import CloudError, resolve_favourite
    data = c.friends.friend_lists(body.get("friend", ""))
    spec = body.get("list") or {}
    if spec.get("kind") == "setlist":
        lst = next((x for x in data["setlists"] if x["name"] == spec.get("name")), None)
        if lst is None:
            raise CloudError(_("friends.list_gone"), 404)
        rows = lst["items"]
    else:
        rows = data["favorites"]
    keys = set(body.get("keys") or [])
    if keys:
        rows = [r for r in rows if r["song_key"] in keys]
    fdata = data.get("friend") or {}
    fname = fdata.get("name") or fdata.get("username") or ""
    append = str(body.get("addToSetlist") or "")              # per-song: add to one of MY existing setlists
    name = append or (_unique_setlist(c, body["setlist"], fname) if body.get("setlist") else None)
    process = bool(body.get("process", True))
    want_fav = bool(body.get("favourite"))
    find = c.friends.matcher()
    have, todo, seen = [], [], set()
    for r in rows:
        if r["song_key"] in seen:
            continue
        seen.add(r["song_key"])
        s = find(r)
        if s is not None:
            have.append(s)
        else:
            todo.append(r)
    if want_fav and have:
        c.libmgr.set_fav(have, True)
    if body.get("install", True) is False:
        todo = []

    def one(r):
        try:
            return r, resolve_favourite(r)
        except Exception as e:
            log.info("Chart lookup failed for %s: %s", r.get("name"), e)
            return r, False

    jobs, missing, failed = [], [], 0
    with ThreadPoolExecutor(4) as ex:
        for r, item in ex.map(one, todo):
            if item is False:
                failed += 1
            elif item is None:
                missing.append(r["song_key"])
            else:
                jobs.append(Job("download", f"Download: {item['artist']} - {item['name']}", item=item,
                                options={"process": process, "mark_fav": want_fav, "fav_charter": r.get("charter") or "",
                                         "fav_checksum": r.get("checksum") or "",
                                         "fav_row": {k: r.get(k) for k in ("artist", "name", "charter", "checksum", "source")}},
                                priority=PRIO_USER))
    if name:
        c.queue_friend_setlist(name, list(rows), jobs, append=bool(append))
    else:
        for j in jobs:
            c.jobs.submit(j)
    return {"queued": len(jobs), "alreadyHave": len(have), "notFound": missing, "failed": failed, "setlist": name}


def _missing_favs(c) -> list[dict]:
    """Favourites not on this machine: cloud rows with no local chart (signed in) plus the ones kept in the
    local store (trashed or moved away; works signed out)."""
    rows = list(c.cloudsync.cloud_only) if c.cloud.signed_in else []
    have_k = {r["song_key"] for r in rows}
    have_c = {(r.get("checksum") or "").upper() for r in rows} - {""}
    have_i = {norm_key(f"{r.get('artist', '')} - {r.get('name', '')}") + "|" + norm_key(r.get("charter") or "") for r in rows}
    for r in c.libmgr.missing_favs():
        ident = norm_key(f"{r['artist']} - {r['name']}") + "|" + norm_key(r.get("charter") or "")
        if r["song_key"] in have_k or (r.get("checksum") or "").upper() in have_c or (ident in have_i and not r.get("unknown")):
            continue
        rows.append(r)
    return rows


def fav_counts(c, missing: list[dict] | None = None) -> dict:
    """Distinct favourited charts in the collection: on-disk favourites plus the ones not on this machine,
    de-duplicated by chart identity (md5, else artist/title/charter). Several local copies count once, so two
    synced machines show the same total whatever they have on disk."""
    lm = c.libmgr
    rows = _missing_favs(c) if missing is None else missing
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def add(tokens):
        tokens = [t for t in tokens if t]
        for t in tokens[1:]:
            parent[find(t)] = find(tokens[0])
        return tokens[0]

    sets = lm._fav_sets()
    with lm.lock:
        songs = list(lm.lib.songs)
    disk = []
    for s in songs:
        if not lm.is_fav(s, sets):
            continue
        md5, charter = lm.chart_md5(s), lm.charter_of(s)
        ident = lm.legacy_key(s) if norm_key(charter) else None
        disk.append(add(["m:" + md5 if md5 else None, "i:" + ident if ident else None, "p:" + lm.chart_pid(s)]))
    other = []
    for r in rows:
        chk = (r.get("checksum") or "").upper()
        ident = norm_key(f"{r.get('artist', '')} - {r.get('name', '')}") + "|" + norm_key(r.get("charter") or "")
        has_i = bool(norm_key(r.get("charter") or "")) and not r.get("unknown")
        other.append(add(["m:" + chk if chk else None, "i:" + ident if has_i else None, "k:" + r["song_key"]]))
    on_disk = {find(t) for t in disk}
    everything = on_disk | {find(t) for t in other}
    return {"total": len(everything), "onDisk": len(on_disk), "notHere": len(everything - on_disk)}


def _cloud_fav(r: dict, c) -> dict:
    nf = c.cloudsync.not_found
    return {"key": r["song_key"], "artist": r.get("artist"), "name": r.get("name"), "charter": r.get("charter"),
            "album": r.get("album"), "checksum": r.get("checksum"), "notFound": r["song_key"] in nf, "addedAt": r.get("added_at"), "unknown": bool(r.get("unknown"))}


def _install_cloud_favs(c, body: dict) -> dict:
    """Resolve missing favourites (recorded source, then Chorus Encore, then RhythmVerse) and queue their downloads."""
    from .cloud import resolve_favourite
    keys = set(body.get("keys") or [])
    rows = [r for r in _missing_favs(c) if (not keys or r["song_key"] in keys) and not r.get("unknown")]
    process = body.get("process", c.settings()["auto_process_downloads"])

    def one(r):
        try:
            return r, resolve_favourite(r)
        except Exception as e:
            log.info("Chart lookup failed for %s: %s", r.get("name"), e)
            return r, False

    queued, missing, failed, have, todo = 0, [], 0, 0, []
    for r in rows:          # already here (same artist, title, charter, even under another md5): adopt, never reinstall
        s = c.libmgr.find_present_chart(r)
        if s is None:
            todo.append(r)
            continue
        if not c.libmgr.adopt_fav(r["song_key"], s):        # a cloud-only row: no stored record to merge
            c.libmgr.set_fav([s], True)
            rk = getattr(c.cloudsync, "rekey", None)
            if rk:
                rk(r["song_key"], c.libmgr.chart_key(s, compute=True))
        have += 1
    rows = todo
    with ThreadPoolExecutor(4) as ex:
        for r, item in ex.map(one, rows):
            if item is False:
                failed += 1
            elif item is None:
                missing.append(r["song_key"])
                c.cloudsync.not_found.add(r["song_key"])
            else:
                c.cloudsync.not_found.discard(r["song_key"])
                c.jobs.submit(Job("download", f"Download: {item['artist']} - {item['name']}", item=item,
                                  options={"process": process, "mark_fav": True, "fav_key": r["song_key"],
                                           "fav_charter": r.get("charter") or "", "fav_checksum": r.get("checksum") or "",
                                           "fav_row": {k: r.get(k) for k in ("artist", "name", "charter", "checksum", "source")}},
                                  priority=PRIO_USER))
                queued += 1
    return {"queued": queued, "notFound": missing, "failed": failed, "alreadyHave": have}


PROFILE_MAX_AGE_S = 30          # the Profile shows the CH Leaderboards account at most this stale


def _push_career_if_fresh(c, prof):
    """Right after a fresh CH profile arrived (a new fetch, not a cached answer), upload the career snapshot for the
    website; push_career() skips the upload when nothing changed."""
    at = (prof or {}).get("fetchedAt")
    if not at or not c.cloud.signed_in or at == getattr(c, "_ch_pushed_at", None):
        return
    c._ch_pushed_at = at
    threading.Thread(target=c.cloudsync.push_career, daemon=True, name="career-push").start()


def _refresh_lb_plays(c):
    """Import the linked CH Leaderboards account's public scores as source 'leaderboard' plays, in the
    background, at most every 10 minutes. They stay local: never uploaded to the cloud plays table."""
    from . import playstats
    aid = int(c.settings().get("ch_account_id") or 0)
    pl = c.playlog
    if not aid:
        if pl.leaderboard():
            pl.clear_leaderboard()
        return
    age = pl.lb_age()
    if pl.lb_refreshing or (age is not None and age < playstats.LB_REFRESH_S):
        return
    pl.lb_refreshing = True

    def run():
        try:
            rows = c.lb.account_scores(aid)
            plays = [p for p in (playstats.lb_row_to_play(r) for r in rows) if p]
            pl.set_leaderboard(plays)
        except Exception:
            log.warning("CH Leaderboards import failed", exc_info=True)
            if age is None:
                pl.set_leaderboard([])       # stamp the attempt so a failure isn't retried on every view
            else:
                pl.lb_path.touch()
        finally:
            pl.lb_refreshing = False
    threading.Thread(target=run, daemon=True, name="lb-import").start()


def _lb_default(c, lm, sid, artist, name, charter, instrument, difficulty):
    """Instrument and difficulty to show: the chosen ones, else what was just played, else Expert guitar."""
    if instrument and difficulty:
        return instrument, difficulty
    last = None
    s = lm.get(sid) if sid else None
    if s:
        artist, name = s.artist, s.name
    want = norm_key(f"{artist} - {name}")
    for p in c.playlog.all():
        if norm_key(f"{p.get('artist', '')} - {p.get('name', '')}") == want and (
                last is None or (p.get("played_at") or "") > (last.get("played_at") or "")):
            last = p
    from .leaderboard import DIFFICULTIES, INSTRUMENTS
    li = last.get("instrument") if last and last.get("instrument") in INSTRUMENTS else None
    ld = last.get("difficulty") if last and last.get("difficulty") in DIFFICULTIES else None
    return instrument or li or "guitar", difficulty or ld or "expert"


def _lb_song(c, artist: str, name: str, charter: str):
    """Map a local song to a leaderboards song, remembering the answer (misses for a day)."""
    maps = c.store.get("lb_map", {}) or {}
    k = norm_key(f"{artist} - {name}") + "|" + norm_key(charter)
    hit = maps.get(k)
    if hit and (hit.get("songHash") or time.time() - hit.get("at", 0) < 86400):
        return hit if hit.get("songHash") else None
    s = c.lb.find_song(artist, name, charter)
    maps[k] = {"songHash": s["songHash"], "name": s["name"], "artist": s["artist"], "charter": s.get("charter"),
               "at": time.time()} if s else {"songHash": None, "at": time.time()}
    if len(maps) > 3000:
        maps = dict(sorted(maps.items(), key=lambda kv: kv[1].get("at", 0))[-2000:])
    c.store.set("lb_map", maps)
    return maps[k] if s else None


def _lb(h, c, lm, path: str, method: str, q: dict, body: dict):
    lb = c.lb
    if path == "/api/lb/song":
        from .library import strip_tags
        sid = q.get("id")
        s = lm.get(sid) if sid else None
        artist, name, charter = (s.artist, s.name, strip_tags(s.meta.get("charter", "") or s.meta.get("frets", ""))) \
            if s else (q.get("artist", ""), q.get("name", ""), q.get("charter", ""))
        inst, diff = _lb_default(c, lm, sid, artist, name, charter, q.get("instrument"), q.get("difficulty"))
        song = _lb_song(c, artist, name, charter)
        out = {"instrument": inst, "difficulty": diff, "found": bool(song), "ch": c.settings().get("ch_username") or ""}
        if song:
            acct = int(c.settings().get("ch_account_id") or 0) or None
            out.update(lb.top_scores(song["songHash"], inst, diff, acct))
            out["song"] = song
        return _json(h, out)
    if path == "/api/lb/overview":
        out = {"totals": lb.site_totals(),
               "players": lb.players(q.get("metric") or "totalScore"),
               "songs": lb.songs_board(q.get("tab") or "trending", q.get("group") or "fiveFret"),
               "latest": lb.latest(), "site": "https://leaderboards.clonehero.net/"}
        return _json(h, out)
    if path == "/api/lb/profile":
        aid = int(c.settings().get("ch_account_id") or 0)
        if not aid:
            return _json(h, {"linked": False})
        return _json(h, {"linked": True, **lb.profile(aid)})
    if path == "/api/lb/link" and method == "POST":
        user = c.link_ch_username(body.get("username", ""))
        return _json(h, {"ok": True, "username": user["username"] if user else ""})
    return _json(h, {"error": _("api.not_found")}, 404)
