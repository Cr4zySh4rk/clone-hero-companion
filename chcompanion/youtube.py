"""Find, inspect and download a song's music video from YouTube via yt-dlp.

Quality rule: a video is only accepted when its resolution class is at least
MIN_P (640p). The class is what YouTube calls "720p" etc.: max(height, width*9/16),
so a widescreen 1280x526 video counts as 720p while 854x480 (480p) or 640x480 (4:3)
are rejected. Among acceptable formats the one closest to the preferred class
(720p by default) wins, H.264 first because Clone Hero plays it natively.

All YouTube traffic goes through one RateGate shared by every worker thread, so a
big multithreaded batch stays under YouTube's throttling, and backs off globally
when YouTube starts asking "are you a bot".
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from .library import Song, norm_key
from .lyrics import clean_title

log = logging.getLogger(__name__)

MIN_SCORE = 4.0
MIN_P = 640                      # hard floor: nothing below this is downloaded or accepted
BAD_WORDS = ["lyric", "lyrics", "cover", "reaction", "karaoke", "tutorial", "lesson",
             "guitar", "drum", "drums", "bass", "instrumental", "remix", "slowed", "reverb",
             "8d", "sped up", "nightcore", "clone hero", "rock band", "guitar hero", "fortnite",
             "letra", "traducida", "sub espanol", "legendado", "1 hour", "10 hours", "extended",
             "playthrough", "isolated", "backing track", "piano", "fan made", "fanmade", "amv",
             "teaser", "trailer", "behind the scenes", "making of", "interview", "full album",
             "expert", "hard", "medium", "easy", "fc", "full combo", "gameplay", "yarg",
             "rocksmith", "pivot", "sickfigures", "chart", "custom song", "100", "fret", "rb3", "gh3"]
# What counts as YouTube limiting us. Only these are throttling/bot signals; they start the shared
# cool-down. Matched against yt-dlp's real messages (tests/fixtures/youtube/errors.json): note that
# "Sign in to confirm your age" is NOT one of them, it is an age-restricted video, and the bot message
# is "Sign in to confirm you're not a bot".
LIMIT_SIGNS = ("not a bot", "http error 429", "too many requests", "rate-limited by youtube",
               "rate limited by youtube", "your ip is likely being blocked")
AGE_SIGNS = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
UNAVAILABLE_SIGNS = ("video unavailable", "this video is not available", "private video", "members-only",
                     "has been removed", "is no longer available", "blocked it in your country",
                     "not made this video available in your country", "copyright")
# Player clients. yt-dlp 2026.8-2026.10 (observed live, tests/fixtures/youtube/clients.json): its own
# default clients (visionos, tv_samsung, web; visionos alone without a JS runtime) give the full list, and
# web_embedded joins them for age-gated videos. tv_embedded no longer exists (yt-dlp silently ignores it),
# and web_safari, mweb, android and android_vr need a GVS PO token, so they only ever return the reduced
# list (storyboards + one muxed 360p stream). Asking them again cost ~10 requests each and never helped.
CLIENT_FALLBACKS = [None]


class BotDetected(Exception):
    """YouTube really is limiting us (HTTP 429, "not a bot", an explicit rate-limit message)."""


class VideoProblem(Exception):
    """This video can't give us what we need; not a statement about YouTube limiting us. The
    next candidate may work and no cool-down starts. `label` is a short reason for job messages."""
    label = "problem"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.detail = message


class AgeRestricted(VideoProblem):
    label = "age-restricted"


class VideoUnavailable(VideoProblem):
    label = "unavailable"


class ReducedFormats(VideoProblem):
    """The answer held no separate video streams (the PO-token-gated clients only give storyboards and
    one muxed 360p stream), or no streams at all."""
    label = "no video streams offered"


class NoHDVideo(Exception):
    pass


def list_state(formats: list) -> str:
    """'full' (has separate video streams), 'reduced' (streams, but none video-only: what the clients
    that lack a PO token get), or 'empty' (nothing but storyboards, or nothing at all)."""
    real = [f for f in formats or [] if f.get("protocol") != "mhtml"
            and (f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none"))]
    if not real:
        return "empty"
    vo = [f for f in real if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
    return "full" if vo else "reduced"


def is_bot_error(e: Exception) -> bool:
    """Genuine throttling or bot detection."""
    m = str(e).lower()
    return any(s in m for s in LIMIT_SIGNS)


def is_age_error(e: Exception) -> bool:
    m = str(e).lower()
    return not is_bot_error(e) and any(s in m for s in AGE_SIGNS)


def is_unavailable_error(e: Exception) -> bool:
    m = str(e).lower()
    return not is_bot_error(e) and any(s in m for s in UNAVAILABLE_SIGNS)


def _short(e: Exception) -> str:
    """yt-dlp's own message without the "ERROR: [youtube] id:" prefix and the cookies advice."""
    m = re.sub(r"^\s*ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?", "", str(e).strip())
    m = re.sub(r"\s*(Use --cookies-from-browser|See\s+https?://|Also see).*$", "", m, flags=re.S)
    return m.strip()[:200] or type(e).__name__


def classify_error(e: Exception, state: str | None = None, nformats: int = 0) -> Exception:
    """The exception to raise for what yt-dlp raised: BotDetected only for real throttling, a VideoProblem
    with the real message for a video that can't be had, `e` itself for everything else.
    state: what the selector saw ('sd' = full list without HD, 'reduced', 'empty')."""
    if isinstance(e, (BotDetected, VideoProblem, NoHDVideo)):
        return e
    if is_bot_error(e):
        return BotDetected(str(e))
    if is_age_error(e):
        return AgeRestricted(_short(e))
    if is_unavailable_error(e):
        return VideoUnavailable(_short(e))
    if state == "sd":
        return NoHDVideo(f"no format at {MIN_P}p or better")
    if state in ("reduced", "empty"):
        return ReducedFormats(f"YouTube offered {'no usable' if state == 'empty' else 'no separate video'} "
                              f"streams ({nformats} formats listed)")
    return e


# ------------------------------------------------------------------ rate gate
class RateGate:
    """Limits concurrent YouTube API calls, spaces them out, and pauses everyone
    after a bot/rate-limit response (60 s, 2 min, 5 min). Jobs wait for the cool-down and
    retry; they only fail once every cool-down has been used up."""
    BACKOFF = [60, 120, 300]

    def __init__(self, concurrent: int = 2, spacing: float = 1.0):
        self.sem = threading.BoundedSemaphore(concurrent)
        self.spacing = spacing
        self.lock = threading.Lock()
        self.last = 0.0
        self.paused_until = 0.0
        self.strikes = 0
        self.cancel = threading.Event()

    @contextmanager
    def slot(self):
        while True:
            wait = self.paused_until - time.time()
            if wait <= 0:
                break
            if self.cancel.wait(min(wait, 1.0)):
                raise RuntimeError("cancelled")
        with self.sem:
            with self.lock:
                gap = self.last + self.spacing - time.time()
                if gap > 0:
                    time.sleep(gap)
                self.last = time.time()
            try:
                yield
                self.strikes = max(0, self.strikes - 1) if self.strikes else 0
            except Exception as e:
                if is_bot_error(e):
                    self.throttled()
                    raise BotDetected(str(e)) from e
                raise

    def acquire(self):
        """Manual form of slot() for calls that release it part-way (lookup, then download)."""
        while True:
            wait = self.paused_until - time.time()
            if wait <= 0:
                break
            if self.cancel.wait(min(wait, 1.0)):
                raise RuntimeError("cancelled")
        self.sem.acquire()
        with self.lock:
            gap = self.last + self.spacing - time.time()
            if gap > 0:
                time.sleep(gap)
            self.last = time.time()

    def release(self, ok: bool = True):
        if ok and self.strikes:
            self.strikes = max(0, self.strikes - 1)
        self.sem.release()

    def throttled(self):
        with self.lock:
            if self.paused_until > time.time():
                return          # requests already in flight when we paused: one strike per cool-down
            wait = self.BACKOFF[min(self.strikes, len(self.BACKOFF) - 1)]
            self.strikes += 1
            self.paused_until = max(self.paused_until, time.time() + wait)
        log.warning("YouTube is rate-limiting; pausing YouTube requests for %ds", wait)

    def remaining(self) -> int:
        return max(0, int(self.paused_until - time.time() + 0.999))

    def max_waits(self) -> int:
        """How many cool-downs a job sits through before it fails."""
        return len(self.BACKOFF)

    def wait_cooldown(self, progress=None):
        """Block while YouTube requests are paused, telling the job why."""
        while True:
            left = self.remaining()
            if left <= 0:
                return
            if progress:
                progress(f"waiting for YouTube cool-down {left // 60}:{left % 60:02d}")
            if self.cancel.wait(min(left, 1.0)):
                raise RuntimeError("cancelled")

    def status(self) -> dict:
        return {"pausedFor": self.remaining(), "strikes": self.strikes,
                "jsRuntime": (js_runtime() or ("", ""))[0] or None}


GATE = RateGate()
DOWNLOADS = threading.BoundedSemaphore(4)   # parallel media downloads


@contextmanager
def download_slot():
    with DOWNLOADS:
        try:
            yield
        except Exception as e:
            if is_bot_error(e):
                GATE.throttled()
                raise BotDetected(str(e)) from e
            raise


_TOOL_CACHE: dict = {}
JS_RUNTIMES = ("deno", "node", "bun", "qjs")
JS_HELP = "YouTube needs Deno or Node for some videos; install Deno from deno.land (the official apps include one)"


def bundled_dirs() -> list[Path]:
    """Folders that can hold the tools shipped inside the app: PyInstaller's extraction folder
    (_MEIPASS: _internal in the Windows onedir, Contents/Frameworks in the .app, a temp folder for
    the Linux onefile) and, as a fallback, _internal next to a frozen executable."""
    dirs: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(Path(base) / "bin")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "_internal" / "bin")
    return dirs


def _bundled_tool(name: str) -> str | None:
    import os
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in bundled_dirs():
        f = d / exe
        if f.is_file() and (sys.platform == "win32" or os.access(f, os.X_OK)):
            return str(f)
    return None


def is_bundled(path: str | None) -> bool:
    return bool(path) and any(Path(path).parent == d for d in bundled_dirs())


def _find_tool(name: str) -> str | None:
    """The copy shipped inside the app first, then shutil.which plus the usual install folders
    (a macOS app started from Finder or the Dock only gets /usr/bin:/bin:/usr/sbin:/sbin as PATH;
    a Windows app keeps the PATH it was started with, so a runtime installed later is missing from
    it). Only hits are cached: a runtime installed while the app runs is picked up."""
    if _TOOL_CACHE.get(name):
        return _TOOL_CACHE[name]
    import os
    exe = name + (".exe" if sys.platform == "win32" else "")
    p = _bundled_tool(name) or shutil.which(name)
    if not p:
        home = os.path.expanduser("~")
        dirs = ["/opt/homebrew/bin", "/usr/local/bin", f"{home}/.deno/bin", f"{home}/.bun/bin",
                f"{home}/.volta/bin", f"{home}/.local/bin", "/snap/bin"]
        if sys.platform == "win32":
            la = os.environ.get("LOCALAPPDATA", "")
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pd = os.environ.get("ProgramData", r"C:\ProgramData")
            dirs += [f"{home}\\.deno\\bin", f"{home}\\.bun\\bin", f"{pf}\\nodejs", f"{pf}\\deno",
                     f"{la}\\Programs\\nodejs", f"{la}\\Microsoft\\WinGet\\Links", f"{la}\\Volta\\bin",
                     f"{home}\\scoop\\shims", f"{pd}\\chocolatey\\bin", os.environ.get("NVM_SYMLINK", "")]
        nvm = Path(home) / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            dirs += [str(d / "bin") for d in sorted(nvm.iterdir(), reverse=True)]
        p = next((str(Path(d) / exe) for d in dirs if d and (Path(d) / exe).is_file()), None)
    if p:
        _TOOL_CACHE[name] = p
    return p


def js_runtime() -> tuple[str, str] | None:
    """(name, path) of the JavaScript runtime yt-dlp will use to solve YouTube's challenges."""
    for rt in JS_RUNTIMES:
        p = _find_tool(rt)
        if p:
            return rt, p
    return None


_DENO_VERSIONS: dict[str, str | None] = {}


def deno_info() -> dict | None:
    """{"version", "path", "bundled"} for the Deno yt-dlp will use, or None. Runs `deno --version`
    (through process.run, so no console window) and caches the answer per path."""
    p = _find_tool("deno")
    if not p:
        return None
    if p not in _DENO_VERSIONS:
        ver = None
        try:
            from .process import run, ToolStartError
            r = run([p, "--version"], capture_output=True, text=True, timeout=20)
            m = re.match(r"deno (\S+)", r.stdout or "") if r.returncode == 0 else None
            ver = m.group(1) if m else None
        except Exception:   # OSError, timeout, ToolStartError
            ver = None
        _DENO_VERSIONS[p] = ver
    return {"version": _DENO_VERSIONS[p], "path": p, "bundled": is_bundled(p)}


def setup_deno_env() -> None:
    """Deno writes its cache next to the user's data, never into Program Files or the .app
    bundle. yt-dlp starts Deno with a copy of os.environ, so setting it once at startup is enough.
    A DENO_DIR the user already set is respected."""
    import os
    if os.environ.get("DENO_DIR"):
        return
    try:
        from .paths import config_dir
        d = config_dir() / "deno"
        d.mkdir(parents=True, exist_ok=True)
        os.environ["DENO_DIR"] = str(d)
    except OSError:
        log.debug("could not prepare DENO_DIR", exc_info=True)


_POPEN_PATCHED = False


def hide_deno_console() -> None:
    """yt-dlp starts Deno with its own Popen, which only sets STARTF_USESHOWWINDOW. In the
    windowed Windows app (no parent console) deno.exe, a console program, could still be given a
    console, so add CREATE_NO_WINDOW. Scoped to the Deno provider module; other yt-dlp
    subprocesses are untouched."""
    global _POPEN_PATCHED
    if _POPEN_PATCHED or sys.platform != "win32":
        return
    try:
        import subprocess
        import yt_dlp.extractor.youtube.jsc._builtin.deno as mod

        class _QuietPopen(mod.Popen):
            def __init__(self, *a, **kw):
                kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
                super().__init__(*a, **kw)
        mod.Popen = _QuietPopen
        _POPEN_PATCHED = True
    except Exception:   # older/newer yt-dlp layouts: its own startupinfo still hides the window
        log.debug("could not patch yt-dlp's Deno launcher", exc_info=True)


def ejs_status() -> str:
    """Whether yt-dlp's challenge-solver scripts (yt-dlp-ejs) are present."""
    try:
        import yt_dlp_ejs.yt.solver as solver
        import yt_dlp_ejs
        solver.core()
        return f"yt-dlp-ejs {getattr(yt_dlp_ejs, 'version', '?')}"
    except Exception as e:
        return f"missing ({type(e).__name__}: {e})"


def _ydl_opts(clients=None, **extra) -> dict:
    runtimes = {}
    hide_deno_console()
    for rt in JS_RUNTIMES:
        p = _find_tool(rt)
        if p:
            runtimes["quickjs" if rt == "qjs" else rt] = {"path": p}
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
            "retries": 5, "fragment_retries": 5, "noprogress": True}
    # Frozen apps carry ffmpeg outside PATH (imageio also uses a versioned filename).
    # yt-dlp must use the same executable as alignment and video conversion. Note that with
    # ffmpeg found yt-dlp merges the two streams fetch() asks for; fetch() splits them again.
    from .align import ffmpeg_path
    ff = ffmpeg_path()
    if ff:
        opts["ffmpeg_location"] = ff
    if runtimes:
        opts["js_runtimes"] = runtimes
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    opts.update(extra)
    return opts


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ quality
def res_class(width: int | None, height: int | None) -> int:
    """YouTube-style resolution class ("720p") of a landscape video, 0 for portrait/unknown."""
    w, h = width or 0, height or 0
    if not w or not h or h > w:
        return 0
    return int(round(max(h, w * 9 / 16)))


def _codec_rank(vcodec: str, for_game: bool) -> int:
    v = (vcodec or "").lower()
    if v.startswith("avc1") or v.startswith("h264"):
        return 3
    if v.startswith("vp9") or v.startswith("vp09"):
        return 2 if not for_game else 1
    if v.startswith("vp8"):
        return 2
    return 0  # av01 etc. (needs re-encoding for the game and Safari)


def pick_format(formats: list[dict], pref_p: int = 720, min_p: int = MIN_P, max_p: int = 1080,
                for_game: bool = True) -> dict | None:
    """Best video-only (or muxed) format at >= min_p, closest to pref_p."""
    min_p = max(MIN_P, min_p)
    max_p = max(max_p, pref_p)
    best, best_key = None, None
    for f in formats or []:
        if (f.get("vcodec") in (None, "none")) or not f.get("url"):
            continue
        p = res_class(f.get("width"), f.get("height"))
        if p < min_p or p > max_p + 20:
            continue
        proto = (f.get("protocol") or "")
        key = (_codec_rank(f.get("vcodec"), for_game),
               proto.startswith("http") and "m3u8" not in proto,
               -abs(p - pref_p), p,
               f.get("acodec") in (None, "none"),   # video-only beats muxed
               f.get("tbr") or 0)
        if best_key is None or key > best_key:
            best, best_key = f, key
    if best is not None:
        best = dict(best)
        best["res_class"] = res_class(best.get("width"), best.get("height"))
    return best


# ------------------------------------------------------------------ search
def score_candidate(entry: dict, song: Song) -> float:
    title = norm_key(entry.get("title") or "")
    chan = norm_key(entry.get("channel") or entry.get("uploader") or "")
    artist = norm_key(clean_title(song.artist))
    name = norm_key(re.sub(r"\s*[\(\[].*?[\)\]]", "", clean_title(song.name))) or norm_key(song.name)
    full_name = norm_key(song.name)
    score = 0.0
    name_toks = [t for t in name.split() if t]
    if name_toks and all(t in title.split() for t in name_toks):
        score += 5
    elif name and name in title:
        score += 4
    else:
        score -= 3
    art_toks = [t for t in artist.split() if len(t) > 1 and t not in ("the", "and")]
    if art_toks and sum(t in title.split() or t in chan.split() for t in art_toks) >= max(1, len(art_toks) // 2):
        score += 3
    if "official music video" in title or "official video" in title:
        score += 4
    elif "official" in title:
        score += 1
    if "music video" in title:
        score += 1
    if "vevo" in chan or (art_toks and all(t in chan for t in art_toks)):
        score += 2
    if "audio" in title.split():
        score -= 3
    if "visualizer" in title or "visualiser" in title:
        score -= 2
    for w in BAD_WORDS:
        if f" {w} " in f" {title} " and w not in full_name:
            score -= 4
    if " live" in f" {title}" and "live" not in full_name:
        score -= 2
    if chan.endswith(" topic") or "provided to youtube" in title:
        score -= 7  # auto-generated "Topic" uploads are a static album-art image
    dur, length = entry.get("duration"), song.length_s
    if dur and dur < 60:
        score -= 6  # shorts / clips
    if dur and length:
        score -= min(4.0, abs(dur - length) / 25.0)
        if dur > length * 1.6 + 60:
            score -= 5
    return score


class YouTube:
    def __init__(self, max_height: int = 1080, pref_p: int = 720):
        self.max_height = max_height
        self.pref_p = pref_p
        self._streams: dict[str, tuple[float, dict]] = {}
        self._infos: dict[str, tuple[float, dict]] = {}
        self._searches: dict[str, tuple[float, list]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------------- search
    def search(self, song: Song, n: int = 8) -> list[dict]:
        from yt_dlp import YoutubeDL
        q = f"{clean_title(song.artist)} - {clean_title(song.name)} official music video"
        with self._lock:                     # one search per song per session (retries, other charts)
            hit = self._searches.get(norm_key(q))
            if hit and hit[0] > time.time():
                return [dict(c) for c in hit[1]]
        with GATE.slot(), YoutubeDL(_ydl_opts(extract_flat="in_playlist")) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
        out = []
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            out.append({"id": e["id"], "title": e.get("title"), "duration": e.get("duration"),
                        "channel": e.get("channel") or e.get("uploader"),
                        "score": round(score_candidate(e, song), 2)})
        out.sort(key=lambda c: -c["score"])
        with self._lock:
            self._searches[norm_key(q)] = (time.time() + 3000, [dict(c) for c in out])
        return out

    def find_album_audio(self, artist: str, track: str, duration: float) -> dict | None:
        """A YouTube upload of the album recording (duration within 6 s), e.g. a Topic track."""
        from yt_dlp import YoutubeDL
        q = f"{clean_title(artist)} - {clean_title(track)} audio"
        with GATE.slot(), YoutubeDL(_ydl_opts(extract_flat="in_playlist")) as ydl:
            info = ydl.extract_info(f"ytsearch10:{q}", download=False)
        best, best_key = None, None
        name = norm_key(track)
        for e in (info or {}).get("entries") or []:
            d = e.get("duration") if e else None
            if not d or abs(d - duration) > 6.0:
                continue
            title = norm_key(e.get("title") or "")
            chan = norm_key(e.get("channel") or e.get("uploader") or "")
            if name and name.split()[0] not in title:
                continue
            if any(w in title.split() for w in ("live", "lyrics", "lyric", "cover", "remix", "karaoke", "instrumental")) \
                    and not any(w in name.split() for w in ("live", "remix")):
                continue
            key = (chan.endswith(" topic"), "audio" in title.split(), -abs(d - duration))
            if best_key is None or key > best_key:
                best, best_key = e, key
        return best

    def best(self, song: Song) -> dict | None:
        c = self.search(song)
        return c[0] if c and c[0]["score"] >= MIN_SCORE else None

    # -------------------------------------------------------------- formats
    def info(self, video_id: str) -> dict:
        """Full extraction (formats with URLs), cached ~1 h. One extraction with yt-dlp's own player
        clients (see CLIENT_FALLBACKS); real errors are raised as what they are."""
        with self._lock:
            hit = self._infos.get(video_id)
            if hit and hit[0] > time.time():
                return hit[1]
        from yt_dlp import YoutubeDL
        info = None
        for clients in CLIENT_FALLBACKS:
            try:
                with GATE.slot(), YoutubeDL(_ydl_opts(clients)) as ydl:
                    cur = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            except BotDetected:
                raise
            except Exception as e:
                c = classify_error(e)
                if c is e:
                    raise
                raise c from e
            if info is None or (bool(pick_format(cur.get("formats") or [], self.pref_p)),
                                len(cur.get("formats") or [])) > (bool(pick_format(info.get("formats") or [], self.pref_p)),
                                                                  len(info.get("formats") or [])):
                info = cur
            if pick_format(cur.get("formats") or [], self.pref_p):
                break
        with self._lock:
            self._infos[video_id] = (time.time() + 3000, info)
        return info

    def _no_hd(self, formats: list) -> Exception:
        st = list_state(formats)
        if st == "full":
            return NoHDVideo(f"no format at {MIN_P}p or better")
        return ReducedFormats(f"YouTube offered {'no usable' if st == 'empty' else 'no separate video'} "
                              f"streams ({len(formats or [])} formats listed)")

    def best_res(self, video_id: str) -> int:
        f = pick_format(self.info(video_id).get("formats") or [], self.pref_p, min_p=MIN_P)
        return f["res_class"] if f else 0

    def resolve(self, video_id: str) -> dict:
        """Direct URL of the HD video-only stream for the companion page."""
        with self._lock:
            hit = self._streams.get(video_id)
            if hit and hit[0] > time.time() + 300:
                return hit[1]
        info = self.info(video_id)
        fmt = pick_format(info.get("formats") or [], self.pref_p, max_p=self.max_height, for_game=False)
        if not fmt:
            raise self._no_hd(info.get("formats") or [])
        url = fmt["url"]
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        expire = float(qs.get("expire", [time.time() + 3600])[0])
        res = {"url": url, "ext": fmt.get("ext"), "vcodec": fmt.get("vcodec"),
               "height": fmt.get("height"), "res": fmt["res_class"],
               "headers": fmt.get("http_headers") or {},
               "title": info.get("title"), "duration": info.get("duration")}
        with self._lock:
            self._streams[video_id] = (expire, res)
        return res

    # -------------------------------------------------------------- downloads
    def download(self, video_id: str, dest_noext: str, progress=None) -> tuple[str, dict]:
        """Download the HD video-only stream; returns (path, format). Raises NoHDVideo."""
        from yt_dlp import YoutubeDL
        info = self.info(video_id)
        fmt = pick_format(info.get("formats") or [], self.pref_p, max_p=max(self.pref_p, 1080))
        if not fmt:
            if list_state(info.get("formats") or []) != "full":
                with self._lock:           # don't keep the cut-down answer cached
                    self._infos.pop(video_id, None)
            raise self._no_hd(info.get("formats") or [])
        hooks = []
        if progress:
            def hook(d):
                if d.get("status") == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress(d.get("downloaded_bytes", 0), tot)
            hooks.append(hook)
        # Download with a fresh extraction (re-using cached format URLs gets HTTP 403).
        # Fallbacks stay within the HD rule; the result is checked again below.
        selector = (f"{fmt['format_id']}/bestvideo[vcodec^=avc1][width>=1138][height<=1100]"
                    f"/bestvideo[width>=1138][height<=1100]/bestvideo[height>={MIN_P}][height<=1100]")
        opts = _ydl_opts(format=selector, outtmpl=dest_noext + ".%(ext)s",
                         progress_hooks=hooks, overwrites=True, continuedl=False,
                         concurrent_fragment_downloads=4)
        try:
            with GATE.slot(), download_slot(), YoutubeDL(opts) as ydl:
                done = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                path = ydl.prepare_filename(done)
        except Exception as e:
            c = classify_error(e)
            if c is e:
                raise
            raise c from e
        got = (done.get("requested_formats") or [done])[0]
        got_res = res_class(got.get("width"), got.get("height"))
        if got_res and got_res < MIN_P:
            import os
            try:
                os.unlink(path)
            except OSError:
                pass
            raise NoHDVideo(f"only {got_res}p was delivered")
        if got.get("format_id") != fmt.get("format_id"):
            fmt = dict(got)
            fmt["res_class"] = got_res or fmt.get("res_class")
        return path, fmt

    def fetch(self, video_id: str, dest_noext: str, with_audio: bool = False, progress=None) -> tuple[str, dict, str | None]:
        """One YouTube lookup, then the HD video stream (and the audio stream, for lining the
        video up) downloaded in the same session: (video_path, format, audio_path).
        Raises NoHDVideo when the video has no >= 640p stream; BotDetected only when YouTube really
        limits us; a VideoProblem (age-restricted, unavailable, no video streams offered) with
        yt-dlp's own message otherwise. Exactly one extraction, whatever the answer."""
        from yt_dlp import YoutubeDL
        pref, maxp = self.pref_p, max(self.pref_p, 1080)
        state: dict = {"gate": True}

        def selector(ctx):
            fm = ctx.get("formats") or []
            if state["gate"]:                 # the lookup is done: let the next one start
                GATE.release()
                state["gate"] = False
                DOWNLOADS.acquire()
                state["dl"] = True
            state["n"] = len(fm)
            v = pick_format(fm, pref, max_p=maxp)
            if not v:
                st = list_state(fm)
                state["why"] = "sd" if st == "full" else st
                return
            state["fmt"] = v
            yield next(f for f in fm if f.get("format_id") == v["format_id"])
            if with_audio:
                auds = [f for f in fm if f.get("vcodec") in (None, "none")
                        and f.get("acodec") not in (None, "none") and f.get("url")]
                a = max(auds, key=lambda f: (f.get("ext") == "m4a", "m3u8" not in (f.get("protocol") or ""),
                                             f.get("abr") or 0), default=None)
                if a:
                    yield a

        hooks = []
        if progress:
            def hook(d):
                if d.get("status") == "downloading" and (d.get("info_dict") or {}).get("vcodec") not in (None, "none"):
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress(d.get("downloaded_bytes", 0), tot)
            hooks.append(hook)
        opts = _ydl_opts(CLIENT_FALLBACKS[0], format=selector, outtmpl=dest_noext + ".f%(format_id)s.%(ext)s",
                         progress_hooks=hooks, overwrites=True, continuedl=False,
                         concurrent_fragment_downloads=4)
        GATE.acquire()
        ok = True
        try:
            with YoutubeDL(opts) as ydl:
                done = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        except Exception as e:
            ok = False
            c = classify_error(e, state.get("why"), state.get("n", 0))
            if isinstance(c, BotDetected):
                GATE.throttled()
            if c is e:
                raise
            raise c from e
        finally:
            if state["gate"]:
                GATE.release(ok)
            if state.get("dl"):
                DOWNLOADS.release()
        vpath = apath = None
        got = state.get("fmt") or {}
        for d in done.get("requested_downloads") or []:
            if d.get("vcodec") not in (None, "none"):
                vpath, got = d.get("filepath"), d
            else:
                apath = d.get("filepath")
        if not vpath:
            raise RuntimeError("the video stream wasn't downloaded")
        if with_audio and not apath and got.get("acodec") not in (None, "none"):
            # ffmpeg was found, so yt-dlp merged the streams into one file. Split it again with a
            # stream copy: the video must stay video-only, and the audio is needed for lining up.
            # Asking YouTube for the audio a second time is what used to trigger rate limiting.
            vpath, apath = _split_merged(vpath)
        got_res = res_class(got.get("width"), got.get("height"))
        if got_res and got_res < MIN_P:
            for x in (vpath, apath):
                try:
                    Path(x).unlink()
                except (OSError, TypeError):
                    pass
            raise NoHDVideo(f"only {got_res}p was delivered")
        fmt = dict(state.get("fmt") or {})
        fmt.update({k: got.get(k) for k in ("format_id", "vcodec", "width", "height", "ext") if got.get(k)})
        fmt["res_class"] = got_res or fmt.get("res_class")
        return vpath, fmt, apath

    def download_audio(self, video_id: str, dest_noext: str) -> str:
        from yt_dlp import YoutubeDL
        last = None
        for fmt in ("bestaudio[ext=m4a]/bestaudio", "bestaudio[acodec=opus]/bestaudio", "worstaudio"):
            try:
                opts = _ydl_opts(format=fmt, outtmpl=dest_noext + ".%(ext)s", overwrites=True)
                with GATE.slot(), download_slot(), YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                    return ydl.prepare_filename(info)
            except BotDetected:
                raise
            except Exception as e:  # sporadic 403s or a format that isn't there: try another format
                c = classify_error(e)
                if c is not e:      # age-restricted / unavailable: another format will not help
                    raise c from e
                last = e
        raise last  # type: ignore[misc]


def _split_merged(path: str) -> tuple[str, str | None]:
    """(video-only file, audio file) from a merged download, by stream copy; (path, None) if it fails."""
    from .align import ffmpeg_path
    from .process import run
    ff = ffmpeg_path()
    src = Path(path)
    if not ff:
        return path, None
    vout, aout = src.with_name(src.stem + ".v" + src.suffix), src.with_suffix(".audio.mka")
    try:
        for out, flag in ((vout, "-an"), (aout, "-vn")):
            r = run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), flag, "-c", "copy", str(out)],
                    capture_output=True, timeout=180)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError((r.stderr or b"").decode(errors="replace")[-200:] or f"exit {r.returncode}")
    except Exception as e:
        log.info("could not split the merged download: %s", e)
        vout.unlink(missing_ok=True)
        aout.unlink(missing_ok=True)
        return path, None
    src.unlink(missing_ok=True)
    return str(vout), str(aout)
