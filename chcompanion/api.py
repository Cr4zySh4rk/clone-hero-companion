"""JSON API behind the desktop app's library manager UI."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, youtube
from .jobs import Job, PRIO_BATCH, PRIO_USER
from .libmgr import find_game, launch_game, reveal, song_id
from .library import norm_key
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
            return _json(h, {"error": "not found"}, 404) or True
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
            lm.set_flag(ids, fav=bool(body.get("value", True)) or None)
        elif act == "rate":
            lm.set_flag(ids, rating=int(body.get("value", 0)) or None)
        elif act == "disable":
            n = lm.disable(ids)
        elif act == "enable":
            n = lm.disable(ids, enable=True)
        elif act == "delete":
            n = lm.delete(ids)
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
            return _json(h, {"error": "unknown action"}, 400) or True
        if act in ("disable", "enable", "delete", "rename"):
            c.build_preview_index()
        return _json(h, {"ok": True, "count": n}) or True
    if path == "/api/duplicates":
        return _json(h, {"groups": lm.duplicates()}) or True

    # ---------------------------------------------------------------- jobs
    if path == "/api/jobs" and method == "GET":
        snap = c.jobs.snapshot()
        snap["youtube"] = youtube.GATE.status()
        snap["now"] = {"status": c.clock.status, "song": c.clock.song}
        return _json(h, snap) or True
    if path == "/api/jobs" and method == "POST":
        kind = body.get("kind", "full")
        prio = PRIO_USER if len(body.get("ids") or []) <= 3 else PRIO_BATCH
        opts = body.get("options") or {}
        n = 0
        for s in lm.songs(body.get("ids") or []):
            c.queue_song(s, kind, prio, **opts)
            n += 1
        return _json(h, {"queued": n}) or True
    if path == "/api/jobs/cancel" and method == "POST":
        c.jobs.cancel(body.get("id"))
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
            return _json(h, {"error": "not found"}, 404) or True
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
            return _json(h, {"error": "Not a YouTube link"}, 400) or True
        r = _safe_res(c, m.group(1))
        if r < youtube.MIN_P:
            return _json(h, {"error": f"That video isn't available in {youtube.MIN_P}p or better"}, 400) or True
        c.store.update_song(s.key, video_id=m.group(1), video_manual=True, video_offset_auto=True)
        c.store.update_song(s.key, video_offset=None)
        c.queue_song(s, "media", PRIO_USER, force=True)
        return _json(h, {"ok": True, "res": r}) or True
    if path.startswith("/api/song/") and path.endswith("/offsets") and method == "POST":
        s = lm.get(path.split("/")[3])
        if not s:
            return _json(h, {"error": "not found"}, 404) or True
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
            return _json(h, {"error": f"Search failed: {e}"}, 502) or True
        owned = {norm_key(f"{clean_title(s.artist)} {clean_title(s.name)}") for s in c.library.songs}
        for r in res["songs"]:
            r["owned"] = norm_key(f"{clean_title(r['artist'])} {clean_title(r['name'])}") in owned
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
            return _json(h, {"error": str(e)}, 400) or True
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
        return _json(h, {"setlists": lm.setlists()}) or True
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
            return _json(h, {"error": str(e)}, 400) or True
        return _json(h, {"ok": True}) or True

    # ---------------------------------------------------------------- app / settings
    if path == "/api/settings" and method == "GET":
        from .align import ffmpeg_path
        game = find_game(c.data_dir, c.settings().get("game_path"))
        return _json(h, {"settings": c.settings(), "dataDir": str(c.data_dir),
                         "songDirs": [str(r) for r in c.library.roots], "game": str(game) if game else None,
                         "version": __version__, "ytdlp": _ytdlp_version(), "ffmpeg": bool(ffmpeg_path()),
                         "minP": youtube.MIN_P}) or True
    if path == "/api/settings" and method == "POST":
        s = body.get("settings") or {}
        if "pref_p" in s:
            s["pref_p"] = max(youtube.MIN_P, int(s["pref_p"]))
        return _json(h, {"settings": c.update_settings(s)}) or True
    if path == "/api/game/launch" and method == "POST":
        game = find_game(c.data_dir, c.settings().get("game_path"))
        if not game:
            return _json(h, {"error": "Clone Hero not found; set its location in Settings"}, 404) or True
        launch_game(game)
        return _json(h, {"ok": True}) or True
    if path == "/api/open" and method == "POST":
        target = Path(body.get("path") or c.data_dir)
        if target.exists():
            reveal(target)
        return _json(h, {"ok": True}) or True
    return False


def _safe_res(c, vid: str) -> int:
    try:
        return c.game_yt.best_res(vid)
    except Exception:
        return 0


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
    from .paths import config_dir
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
    tmpd = Path(tempfile.mkdtemp(prefix="chc-pv-", dir=config_dir()))
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    for n in names:
        p = tmpd / n
        p.write_bytes(sng.read(n) or b"")
        cmd += ["-ss", f"{start:.1f}", "-t", "30", "-i", str(p)]
    if len(names) > 1:
        cmd += ["-filter_complex", f"amix=inputs={len(names)}:normalize=0,alimiter=limit=0.95"]
    cmd += ["-ac", "2", "-c:a", "libmp3lame", "-b:a", "160k", "-f", "mp3", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        shutil.rmtree(tmpd, ignore_errors=True)


def _stream_audio(h, lm, s, start: float):
    try:
        cmd, tmp = lm.audio_stream_cmd(s, start)
    except Exception:
        h._send(404, b"", "text/plain")
        return
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
