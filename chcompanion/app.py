"""Glue: watches the game, prepares lyrics/video for the current song and serves
the companion page over HTTP (with Server-Sent Events for live updates)."""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, net, youtube
from . import ingame, metadata
from .jobs import Job, JobManager, PRIO_BATCH, PRIO_HOVER, PRIO_LIBRARY, PRIO_PLAYED, PRIO_USER
from .libmgr import LibraryManager, song_id
from .library import Library, Song
from .logwatch import ClockState, LogWatcher
from .lyrics import fetch_lrclib
from .paths import resource_path
from .store import Store

log = logging.getLogger(__name__)
YT_ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/|^)([A-Za-z0-9_-]{11})(?:[&?#/]|$)")


class Companion:
    def __init__(self, data_dir: Path, library: Library, store: Store, *,
                 use_youtube: bool = True, use_lrclib: bool = True,
                 in_game: bool = True, prefetch: bool = True, max_height: int = 1080,
                 game_max_height: int = 720, game_lyrics: bool = True):
        self.data_dir = data_dir
        self.library = library
        self.store = store
        self.use_youtube = use_youtube and youtube.available()
        self.use_lrclib = use_lrclib
        self.in_game = in_game and self.use_youtube
        self.prefetch = prefetch
        self.game_lyrics = game_lyrics
        self.yt = youtube.YouTube(max_height=max_height)
        self.game_yt = youtube.YouTube(max_height=game_max_height)
        self.libmgr = LibraryManager(data_dir, library, store)
        self.jobs = JobManager(self._run_job, workers=int(self.settings().get("workers") or 0) or None,
                               busy=self._job_busy, on_change=self._job_changed)
        self.game_yt.pref_p = int(self.settings().get("pref_p", 720))
        self.clients: list[queue.Queue] = []
        self.clients_lock = threading.Lock()
        self.song: Song | None = None
        self.song_info: dict | None = None
        self.lyrics: dict = {"source": None, "lines": []}
        self.video: dict = {"kind": None}
        self.notice: str | None = None
        self.version = 0
        self.prepare_token = 0
        self.install_status: dict[str, str] = {}
        self.clock = ClockState()
        self.quit_requested = threading.Event()
        self.watcher = LogWatcher(data_dir, self._on_clock,
                                  start_lead=lambda: float(self.store.get("start_lead", 2.0)),
                                  learn_lead=self._learn_lead)
        self.watcher.on_preview = self._on_preview
        self.preview_index: dict[int, list[Song]] = {}
        self.build_preview_index()

    def build_preview_index(self):
        idx: dict[int, list[Song]] = {}
        for s in self.library.songs:
            ms = s.preview_ms
            if ms is not None:
                idx.setdefault(ms, []).append(s)
        self.preview_index = idx

    def _on_preview(self, x: float):
        """A song preview started in the menu: get its video/lyrics ready before it is picked."""
        if not (self.in_game and self.prefetch):
            return
        ms = int(round(x * 1000))
        cands = []
        for k in (ms, ms - 1, ms + 1):
            cands += self.preview_index.get(k, [])
        if 0 < len(cands) <= 4:
            for s in cands:
                if self.needs_prep(s):
                    log.info("Highlighted in the menu: %s - preparing in-game video/lyrics", s.display)
                    self.queue_song(s, "media", PRIO_HOVER)

    # ------------------------------------------------------------ state
    def _learn_lead(self, v: float):
        self.store.learn_lead(v)
        log.info("Calibrated song start delay: observed %.2fs -> using %.2fs",
                 v, self.store.get("start_lead"))

    def snapshot(self, full: bool = True) -> dict:
        key = self.song.key if self.song else None
        per = self.store.song(key) if key else {}
        d = {
            "version": self.version, "app": __version__, "serverNow": time.time(),
            "clock": self.clock.public(),
            "song": self.song_info,
            "video": self.video,
            "offsets": {"lyrics": float(per.get("lyrics_offset", 0.0))
                        - (float(per.get("lyrics_offset_written", 0.0) or 0.0) if per.get("game_lyrics") else 0.0),
                        "video": float(per.get("video_offset", 0.0)),
                        "globalLyrics": float(self.store.get("lyrics_offset", 0.0))},
            "notice": self.notice,
            "install": self.song_job_status(self.song) if self.song else None,
            "inGame": self.in_game,
            "queue": self.jobs.snapshot(limit=0)["counts"].get("queued", 0),
            "lyricsSource": self.lyrics.get("source"),
        }
        if full:
            d["lyrics"] = self.lyrics
        return d

    def broadcast(self, full: bool = True):
        self.version += 1
        payload = json.dumps(self.snapshot(full))
        with self.clients_lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def _on_clock(self, state: ClockState, why: str):
        self.clock = state
        if why == "load":
            name = state.song or ""
            song = self.library.find(name)
            if song is None:  # maybe a newly added song: rescan once
                self.library.scan()
                song = self.library.find(name)
            self.set_song(song, name)
            if song is not None and self.in_game and self.needs_prep(song):
                self.queue_song(song, "media", PRIO_PLAYED)  # done after this play, ready for the next
        elif why in ("quit", "cancel", "game-restarted"):
            self.notice = None
            self.broadcast(full=False)
        else:
            self.broadcast(full=False)

    def set_song(self, song: Song | None, logged_name: str = ""):
        self.prepare_token += 1
        token = self.prepare_token
        self.song = song
        self.lyrics = {"source": None, "lines": [], "loading": True}
        self.video = {"kind": None, "loading": True}
        if song is None:
            artist, _, name = logged_name.partition(" - ")
            self.song_info = {"key": None, "artist": artist, "name": name or logged_name}
            self.notice = "Song not found in your library folders"
            self.lyrics["loading"] = False
            self.video["loading"] = False
            self.broadcast()
            return
        self.song_info = song.to_public()
        self.song_info["hasArt"] = song.art_file() is not None
        self.notice = None
        self.broadcast()
        threading.Thread(target=self._prepare_lyrics, args=(song, token), daemon=True).start()
        threading.Thread(target=self._prepare_video, args=(song, token), daemon=True).start()

    def _prepare_lyrics(self, song: Song, token: int):
        lines, source = song.chart_lyrics(), "chart"
        if not lines and self.use_lrclib:
            source = "lrclib"
            try:
                lines = fetch_lrclib(song.artist, song.name, song.length_s)
            except Exception as e:
                log.warning("LRCLIB lookup failed: %s", e)
        if token != self.prepare_token:
            return
        self.lyrics = {"source": source if lines else None, "lines": lines, "loading": False}
        log.info("Lyrics for %s: %s (%d lines)", song.display, source if lines else "none", len(lines))
        self.broadcast()

    def _prepare_video(self, song: Song, token: int, choose_index: int | None = None):
        video: dict = {"kind": None, "loading": False}
        local = song.local_video()
        per = self.store.song(song.key)
        try:
            if local and choose_index is None and not per.get("prefer_youtube"):
                # song.ini's video_start_time already contains the offset we wrote; the page adds
                # the stored per-song offset, so only add the part not yet written to the ini.
                video = {"kind": "local", "src": f"/media/local?t={token}", "name": local.name,
                         "startOffset": song.video_start_s - float(per.get("video_offset_written", 0.0) or 0.0)}
            elif self.use_youtube:
                cands = per.get("candidates")
                vid = per.get("video_id")
                if not cands or choose_index is not None or not vid:
                    if not cands:
                        cands = self.yt.search(song)
                    if choose_index is not None and cands:
                        vid = cands[choose_index % len(cands)]["id"]
                        self.store.update_song(song.key, video_manual=True)
                    elif not vid and cands and cands[0]["score"] >= youtube.MIN_SCORE:
                        vid = cands[0]["id"]
                    self.store.update_song(song.key, candidates=cands[:8], video_id=vid)
                if vid:
                    title = next((c.get("title") for c in cands or [] if c["id"] == vid), None)
                    video = {"kind": "youtube", "id": vid, "title": title,
                             "src": f"/media/yt/{vid}", "candidates": cands or []}
                    try:
                        r = self.yt.resolve(vid)
                        video["title"] = r.get("title") or title
                        video["stream"] = True
                    except Exception as e:
                        log.warning("Could not resolve stream for %s (%s); using embed player", vid, e)
                        video["stream"] = False
        except Exception as e:
            log.warning("Video lookup failed for %s: %s", song.display, e)
        if token != self.prepare_token:
            return
        self.video = video
        self.broadcast()

    # ------------------------------------------------------------ actions
    def adjust_offset(self, kind: str, delta: float | None = None, value: float | None = None):
        if kind == "global":
            cur = float(self.store.get("lyrics_offset", 0.0))
            self.store.set("lyrics_offset", round(value if value is not None else cur + (delta or 0), 3))
        elif self.song:
            field = "lyrics_offset" if kind == "lyrics" else "video_offset"
            cur = float(self.store.song(self.song.key).get(field, 0.0))
            new = round(value if value is not None else cur + (delta or 0), 3)
            self.store.update_song(self.song.key, **{field: new})
        self.broadcast(full=False)

    def next_video(self, step: int = 1):
        if not self.song:
            return
        per = self.store.song(self.song.key)
        cands = per.get("candidates") or []
        idx = next((i for i, c in enumerate(cands) if c["id"] == per.get("video_id")), -1)
        self.store.update_song(self.song.key, prefer_youtube=True)
        self.video = {"kind": None, "loading": True}
        self.broadcast(full=False)
        threading.Thread(target=self._prepare_video,
                         args=(self.song, self.prepare_token, idx + step), daemon=True).start()

    def set_video(self, url_or_id: str):
        if not self.song:
            return False
        m = YT_ID.search(url_or_id.strip())
        if not m:
            return False
        vid = m.group(1)
        per = self.store.song(self.song.key)
        cands = per.get("candidates") or []
        if not any(c["id"] == vid for c in cands):
            cands = [{"id": vid, "title": "(chosen manually)", "score": 99}] + cands
        self.store.update_song(self.song.key, video_id=vid, candidates=cands, prefer_youtube=True,
                               video_manual=True)
        threading.Thread(target=self._prepare_video,
                         args=(self.song, self.prepare_token, None), daemon=True).start()
        return True

    def install_current(self) -> str:
        """Re-do the in-game video/lyrics for the current song (e.g. after choosing a
        different video or changing offsets). Applied once the song is not playing."""
        song = self.song
        if not song:
            return "no song"
        self.queue_song(song, "media", PRIO_USER, force=True)
        return "queued"

    def prepare_library(self) -> int:
        n = 0
        for s in self.library.songs:
            if self.needs_prep(s):
                self.queue_song(s, "media", PRIO_LIBRARY)
                n += 1
        log.info("Queued %d songs for in-game video/lyrics", n)
        return n

    # ------------------------------------------------------------ settings / jobs
    DEFAULTS = {"workers": 0, "pref_p": 720, "in_game": True, "prefetch": True, "game_lyrics": True,
                "auto_process_downloads": True, "download_subdir": "", "metadata_overwrite": False,
                "replace_low_res": True, "game_path": "", "data_dir": ""}

    def settings(self) -> dict:
        s = dict(self.DEFAULTS)
        s.update(self.store.get("settings", {}) or {})
        return s

    def update_settings(self, values: dict) -> dict:
        cur = self.store.get("settings", {}) or {}
        for k, v in values.items():
            if k in self.DEFAULTS:
                cur[k] = v
        self.store.set("settings", cur)
        s = self.settings()
        self.in_game = bool(s["in_game"]) and self.use_youtube
        self.prefetch = bool(s["prefetch"])
        self.game_lyrics = bool(s["game_lyrics"])
        self.game_yt.pref_p = int(s["pref_p"])
        self.jobs.set_workers(int(s["workers"]) or min(4, __import__("os").cpu_count() or 2))
        return s

    def needs_prep(self, song: Song) -> bool:
        per = self.store.song(song.key)
        if per.get("game_skip"):
            return False
        if not ingame.has_video(song) and not per.get("video_failed"):
            return True
        if (self.game_lyrics and not per.get("game_lyrics") and not per.get("lyrics_none")
                and not song.chart_lyrics()):
            return True
        return False

    def queue_song(self, song: Song, kind: str, priority: int = PRIO_BATCH, **options) -> Job:
        title = song.display
        return self.jobs.submit(Job(kind, title, song_id=song_id(song), options=options, priority=priority))

    def song_job_status(self, song: Song) -> str | None:
        sid = song_id(song)
        for j in self.jobs.snapshot()["jobs"]:
            if j["songId"] == sid:
                if j["status"] == "done":
                    return j["result"]
                if j["status"] == "failed":
                    return "failed: " + j["error"]
                return j["step"] or j["status"]
        return None

    def _job_busy(self, job: Job) -> bool:
        if not job.song_id or not self.song:
            return False
        return (song_id(self.song) == job.song_id and self.clock.status in ("playing", "paused"))

    def _job_changed(self, job: Job):
        if self.song and job.song_id == song_id(self.song):
            self.broadcast(full=False)

    def _run_job(self, job: Job, progress) -> str:
        if job.kind == "download":
            return self._run_download(job, progress)
        song = self.libmgr.get(job.song_id)
        if song is None:
            raise RuntimeError("song no longer in the library")
        o = job.options
        parts = []
        try:
            if job.kind in ("metadata", "full"):
                ch = metadata.apply(song, overwrite=bool(o.get("overwrite", self.settings()["metadata_overwrite"])),
                                    progress=progress)
                parts.append(("metadata: " + ", ".join(ch)) if ch else "metadata up to date")
            if job.kind == "media" and o.get("resync"):
                r = ingame.resync(song, self.game_yt, self.store, progress=progress)
                bits = [x for x in ((r.get("video") and "video sync updated"), (r.get("lyrics") and "lyrics moved")) if x]
                return ", ".join(bits) or "nothing to update"
            if job.kind in ("media", "full"):
                if o.get("force"):
                    self.store.update_song(song.key, video_failed=None, lyrics_none=None)
                r = ingame.prepare(song, self.game_yt, self.store,
                                   video=o.get("video", True), lyrics=o.get("lyrics", self.game_lyrics),
                                   force_video=bool(o.get("force")),
                                   replace_low_res=o.get("replace_low_res", self.settings()["replace_low_res"]),
                                   progress=progress)
                if r.get("lyrics") == "none found":
                    self.store.update_song(song.key, lyrics_none=True)
                res = self.store.song(song.key).get("video_res")
                if r.get("video"):
                    parts.append(f"video {res}p" if res else "video")
                if r.get("lyrics") == "added":
                    parts.append("lyrics")
                if not r.get("video") and not r.get("lyrics") == "added":
                    parts.append("already set up")
        finally:
            self.libmgr.refresh_song(song)
        return "; ".join(parts) or "done"

    def _run_download(self, job: Job, progress) -> str:
        from .sources import install as inst
        s = self.settings()
        root = self.library.roots[0] if self.library.roots else self.data_dir / "Songs"
        dest = root / s["download_subdir"] if s["download_subdir"] else root
        paths = inst.install(job.item or {}, dest, progress=progress, cancel=job.cancel)
        self.library.scan()
        self.libmgr._reindex()
        self.build_preview_index()
        new = [x for x in self.library.songs if any(str(x.path) == str(p) for p in paths)]
        for x in new:
            self.libmgr.refresh_song(x)
            if job.options.get("process", s["auto_process_downloads"]):
                self.queue_song(x, "full", PRIO_BATCH)
        return f"installed {len(paths)} chart{'s' if len(paths) != 1 else ''}"

    def simulate(self, name: str):
        """Pretend the game loaded a song (for testing without the game)."""
        self.watcher.handle_line(f"[Log] Loading Song: {name}")

    def start(self):
        self.watcher.start()
        self.jobs.start()
        threading.Thread(target=self.libmgr.rescan, daemon=True).start()
        if self.in_game and self.store.get("prepare_library"):
            self.prepare_library()


# ======================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CloneHeroCompanion/" + __version__
    companion: Companion = None  # set by serve()

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    # -------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _serve_file(self, path: Path, ctype: str | None = None):
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _proxy_youtube(self, vid: str):
        c = self.companion
        for attempt in range(2):
            try:
                r = c.yt.resolve(vid)
            except Exception as e:
                return self._json({"error": str(e)}, 502)
            headers = {k: v for k, v in (r.get("headers") or {}).items()
                       if k.lower() in ("user-agent", "accept-language")}
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            req = urllib.request.Request(r["url"], headers=headers)
            try:
                up = net.urlopen(req, timeout=20)
            except urllib.error.HTTPError as e:
                if e.code in (403, 410) and attempt == 0:
                    c.yt._cache.pop(vid, None)
                    continue
                if e.code == 416:
                    return self._send(416, b"", "text/plain")
                return self._json({"error": f"upstream {e.code}"}, 502)
            except Exception as e:
                return self._json({"error": str(e)}, 502)
            try:
                self.send_response(up.status)
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if up.headers.get(h):
                        self.send_header(h, up.headers[h])
                if not up.headers.get("Accept-Ranges"):
                    self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                while True:
                    chunk = up.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # browser seeked / closed
            finally:
                up.close()
            return

    # -------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        c = self.companion
        url = urllib.parse.urlparse(self.path)
        p = url.path
        try:
            if p in ("/", "/app", "/index.html"):
                html = resource_path("web", "app.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if p in ("/now", "/karaoke"):
                html = resource_path("web", "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if p.startswith("/static/"):
                name = p.rsplit("/", 1)[-1]
                f = resource_path("web", name)
                if not f.is_file() or "/" in name or "\\" in name:
                    return self._json({"error": "not found"}, 404)
                ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
                         "png": "image/png"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8" if ctype.startswith("text") else ctype)
            from . import api
            try:
                if api.handle(self, "GET", p, urllib.parse.parse_qs(url.query), {}):
                    return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                raise
            except Exception as e:
                log.exception("API error on %s", p)
                return self._json({"error": str(e)}, 500)
            if p == "/api/state":
                return self._json(c.snapshot())
            if p == "/api/debug":
                return self._json({"events": c.watcher.debug_events, "raw": c.watcher.raw_lines[-120:], "log": c.watcher.current_file,
                                   "startLead": c.store.get("start_lead"),
                                   "leadSamples": c.store.get("lead_samples"),
                                   "songs": len(c.library.songs)})
            if p == "/events":
                return self._sse()
            if p == "/media/local":
                v = c.song.local_video() if c.song else None
                if not v:
                    return self._json({"error": "no local video"}, 404)
                return self._serve_file(v)
            if p == "/media/art":
                art = c.song.art_file() if c.song else None
                if not art:
                    return self._json({"error": "no art"}, 404)
                name, data = art
                return self._send(200, data, mimetypes.guess_type(name)[0] or "image/png")
            if p.startswith("/media/yt/"):
                return self._proxy_youtube(p.rsplit("/", 1)[-1][:11])
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        c = self.companion
        url = urllib.parse.urlparse(self.path)
        p = url.path
        b = self._body()
        from . import api
        try:
            if api.handle(self, "POST", p, urllib.parse.parse_qs(url.query), b):
                return
        except Exception as e:
            log.exception("API error on %s", p)
            return self._json({"error": str(e)}, 500)
        if p == "/api/offset":
            c.adjust_offset(b.get("kind", "lyrics"), b.get("delta"), b.get("value"))
            return self._json({"ok": True})
        if p == "/api/video/next":
            c.next_video(int(b.get("step", 1)))
            return self._json({"ok": True})
        if p == "/api/video/set":
            return self._json({"ok": c.set_video(str(b.get("url", "")))})
        if p == "/api/video/local":
            if c.song:
                c.store.update_song(c.song.key, prefer_youtube=None)
                threading.Thread(target=c._prepare_video, args=(c.song, c.prepare_token), daemon=True).start()
            return self._json({"ok": True})
        if p == "/api/install":
            return self._json({"status": c.install_current()})
        if p == "/api/prepare-library":
            c.store.set("prepare_library", True)
            return self._json({"queued": c.prepare_library()})
        if p == "/api/quit":
            self._json({"ok": True})
            c.quit_requested.set()
            return
        if p == "/api/simulate":
            c.simulate(str(b.get("song", "")))
            return self._json({"ok": True})
        if p == "/api/clock":  # manual control (simulate pause/resume)
            line = {"pause": "[Log] Seeking to song time:%s" % round(c.clock.now(), 3)}.get(b.get("action"))
            if b.get("action") == "resume":
                line = "[Log] Seeking to song time:%s" % round(max(0, c.clock.now() - 1.5), 3)
            if line:
                c.watcher.handle_line(line)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _sse(self):
        c = self.companion
        q: queue.Queue = queue.Queue(maxsize=50)
        with c.clients_lock:
            c.clients.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(c.snapshot())}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=10)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    # heartbeat keeps the connection and the page's clock in sync
                    self.wfile.write(f"event: ping\ndata: {time.time()}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with c.clients_lock:
                if q in c.clients:
                    c.clients.remove(q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(companion: Companion, host: str, port: int) -> Server:
    Handler.companion = companion
    last_err = None
    for p in [port] + list(range(port + 1, port + 10)):
        try:
            srv = Server((host, p), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
            return srv
        except OSError as e:
            last_err = e
    raise last_err  # type: ignore[misc]
