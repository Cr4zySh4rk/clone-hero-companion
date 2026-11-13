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

import json
import logging
import random
import re
import os
import shutil
import sys
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from . import cancel as _cancel
from .i18n import _, join
from .library import Song, norm_key
from .lyrics import clean_title

log = logging.getLogger(__name__)

MIN_SCORE = 4.0
REQUEST_SLEEP_S = 1.5            # yt-dlp's own pause between requests inside one extraction (careful pace)
REQUEST_SLEEP_FAST_S = 0.5       # ... and at the normal, fast pace (2.10.3: the pauses were ~16 s of an 87 s song)
SLOW_FOR_S = 6 * 3600            # after any sign of YouTube limiting, stay at the careful pace this long
SEARCH_RESULTS = 5
MIN_P = 640                      # HD floor: below this a video only counts as an SD fallback (see SD_CHOICES)
SD_CHOICES = (480, 360)          # the lowest resolution the "use the best SD version" setting can accept
STRONG_SCORE = 12.0              # score_candidate() of a strong official match (max 15; see is_strong_official)
STILL_MAX_FPS = 1                # a "video" of 1 frame per second or less is a slideshow
STILL_MIN_KBPS = 90              # the same absolute floor looks_static() applies to a downloaded file
STILL_MIN_BPP = 0.005            # bits per pixel per frame; real music videos are >= 0.02 (see tests/fixtures/youtube/precheck.json)
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


SIGNAL_LABELS = {"http-429": _("yt.signal.http429"),
                 "bot-check": _("yt.signal.botcheck"),
                 "rate-limit-text": _("yt.signal.ratelimit")}
BOT_HINT = _("yt.bot_hint")


def limit_signal(e: Exception | str) -> str | None:
    """Which of the three genuine limit signals a yt-dlp error is: http-429, bot-check, rate-limit-text."""
    m = str(e).lower()
    if "http error 429" in m or "too many requests" in m:
        return "http-429"
    if "not a bot" in m:
        return "bot-check"
    if any(x in m for x in ("rate-limited by youtube", "rate limited by youtube", "your ip is likely being blocked")):
        return "rate-limit-text"
    return None


class BotDetected(Exception):
    """YouTube really is limiting us (HTTP 429, "not a bot", an explicit rate-limit message)."""

    def __init__(self, message: str = "", video_id: str | None = None):
        super().__init__(message)
        self.signal = limit_signal(message) or "unknown"
        self.video_id = video_id


class VideoProblem(Exception):
    """This video can't give us what we need; not a statement about YouTube limiting us. The
    next candidate may work and no cool-down starts. `label` is a short reason for job messages."""
    label = _("yt.label.problem")

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.detail = message


class AgeRestricted(VideoProblem):
    label = _("yt.label.age")


class VideoUnavailable(VideoProblem):
    label = _("yt.label.unavailable")


class ReducedFormats(VideoProblem):
    """The answer held no separate video streams (the PO-token-gated clients only give storyboards and
    one muxed 360p stream), or no streams at all."""
    label = _("yt.label.reduced")


class NoHDVideo(Exception):
    """No stream at MIN_P or better. sd_p: the best SD resolution the video does offer (0: none or SD not
    asked for), max_p: the best resolution of any kind, so the caller can still use it as an SD fallback."""
    def __init__(self, *a, sd_p: int = 0, max_p: int = 0):
        super().__init__(*a)
        self.sd_p, self.max_p = sd_p, max_p


class StillImage(Exception):
    """The stream list says the upload is a picture with audio, not a music video (nothing was downloaded)."""


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
    return limit_signal(e) is not None


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
    if isinstance(e, (BotDetected, VideoProblem, NoHDVideo, StillImage)):
        return e
    if is_bot_error(e):
        return BotDetected(str(e))
    if is_age_error(e):
        return AgeRestricted(_short(e))
    if is_unavailable_error(e):
        return VideoUnavailable(_short(e))
    if state == "sd":
        return NoHDVideo(f"no format at {MIN_P}p or better")
    if state == "still":
        return StillImage("looks like a still image")
    if state in ("reduced", "empty"):
        return ReducedFormats(_("yt.reduced_empty" if state == "empty" else "yt.reduced_nosep", count=nformats))
    return e


# ------------------------------------------------------------------ rate gate
class RateGate:
    """Paces YouTube metadata lookups like one human client: one at a time, a random gap between them, a soft
    hourly budget, and a global pause after a genuine limit (2, 5, 15, 30 min). Jobs wait for the pause and
    retry (or defer just their video step); see ingame.prepare.

    Adaptive (2.10.3): a 2-5 s gap normally; the careful 4-9 s gap (and slower requests inside yt-dlp) for
    SLOW_FOR_S after any limit, or while strikes remain."""
    BACKOFF = [120, 300, 900, 1800]
    HOUR = 3600.0

    def __init__(self, concurrent: int = 1, spacing=(4.0, 9.0), budget: int = 60, rng=None, fast_spacing=None):
        self.concurrent = concurrent
        self.sem = threading.BoundedSemaphore(concurrent)
        self.gap = (float(spacing), float(spacing)) if isinstance(spacing, (int, float)) else tuple(spacing)
        if fast_spacing is None:          # explicit spacing (tests) means: no separate fast pace
            fast_spacing = (2.0, 5.0) if self.gap == (4.0, 9.0) else self.gap
        self.fast_gap = (float(fast_spacing), float(fast_spacing)) if isinstance(fast_spacing, (int, float)) else tuple(fast_spacing)
        self.budget = budget
        self.rng = rng or random.uniform
        self.lock = threading.Lock()
        self.last = 0.0
        self.paused_until = 0.0
        self.strikes = 0
        self.cancel = threading.Event()       # shutting the app down; a job's own cancel arrives via cancel.current()
        self.requests: list[float] = []       # start times of lookups in the last hour
        self.last_limit: dict | None = None   # the latest genuine limit: signal, message, video id, counts
        self.path: Path | None = None

    # ------------------------------------------------------------ activity
    def _sleep(self, secs: float):
        _cancel.sleep(secs, self.cancel)

    def _count(self, now: float, secs: float) -> int:
        return sum(1 for t in self.requests if now - t < secs)

    def record_request(self, video_id: str | None = None):
        now = time.time()
        with self.lock:
            self.requests = [t for t in self.requests if now - t < self.HOUR] + [now]
        self._save()

    def attach(self, path):
        """Keep the activity (requests, last limit) in a file so Settings, doctor and restarts see it."""
        self.path = Path(path)
        try:
            d = json.loads(self.path.read_text("utf-8"))
            now = time.time()
            self.requests = sorted(t for t in d.get("requests", []) if now - t < self.HOUR)
            self.last_limit = d.get("lastLimit") or None
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"requests": self.requests[-300:], "lastLimit": self.last_limit}), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def budget_wait(self) -> int:
        """Seconds until the hourly budget has room again (0 = go ahead)."""
        if not self.budget:
            return 0
        now = time.time()
        with self.lock:
            live = [t for t in self.requests if now - t < self.HOUR]
            if len(live) < self.budget:
                return 0
            return max(1, int(live[len(live) - self.budget] + self.HOUR - now + 0.999))

    def wait_budget(self, progress=None):
        while True:
            left = self.budget_wait()
            if left <= 0:
                return
            if progress:
                progress(_("yt.pacing", time=f"{left // 60}:{left % 60:02d}"))
            self._sleep(min(left, 1.0))

    def careful(self) -> bool:
        """True while YouTube recently showed signs of limiting us: use the slower pace."""
        lim = self.last_limit or {}
        return bool(self.strikes) or (bool(lim.get("at")) and time.time() - float(lim["at"]) < SLOW_FOR_S)

    def current_gap(self) -> tuple:
        return self.gap if self.careful() else self.fast_gap

    def request_sleep(self) -> float:
        return REQUEST_SLEEP_S if self.careful() else REQUEST_SLEEP_FAST_S

    def _begin(self, video_id=None):
        """Called once the concurrency slot is held: budget, then a random gap since the last lookup."""
        self.wait_budget()
        with self.lock:
            gap = self.last + self.rng(*self.current_gap()) - time.time()
        if gap > 0:
            self._sleep(gap)
        with self.lock:
            self.last = time.time()
        self.record_request(video_id)

    def _wait_unpaused(self):
        while True:
            wait = self.paused_until - time.time()
            if wait <= 0:
                return
            self._sleep(min(wait, 1.0))

    @contextmanager
    def slot(self, video_id: str | None = None):
        self._wait_unpaused()
        _cancel.acquire(self.sem)
        try:
            self._begin(video_id)
            try:
                yield
                self.strikes = max(0, self.strikes - 1) if self.strikes else 0
            except Exception as e:
                if is_bot_error(e):
                    self.throttled(e, video_id)
                    raise BotDetected(str(e), video_id) from e
                raise
            finally:
                self.last = time.time()
        finally:
            self.sem.release()

    def acquire(self, video_id: str | None = None):
        """Manual form of slot() for calls that release it part-way (lookup, then download)."""
        self._wait_unpaused()
        _cancel.acquire(self.sem)
        try:
            self._begin(video_id)
        except BaseException:
            self.sem.release()
            raise

    def release(self, ok: bool = True):
        if ok and self.strikes:
            self.strikes = max(0, self.strikes - 1)
        self.last = time.time()
        self.sem.release()

    def throttled(self, exc: Exception | None = None, video_id: str | None = None):
        now = time.time()
        with self.lock:
            if exc is not None:
                self.last_limit = {
                    "at": now, "signal": limit_signal(exc) or getattr(exc, "signal", "unknown"),
                    "message": _short(exc)[:300], "videoId": video_id or getattr(exc, "video_id", None),
                    "requests10": self._count(now, 600), "requests60": self._count(now, 3600)}
            if self.paused_until > now:
                self._save()
                return          # requests already in flight when we paused: one strike per cool-down
            wait = self.BACKOFF[min(self.strikes, len(self.BACKOFF) - 1)]
            self.strikes += 1
            self.paused_until = max(self.paused_until, now + wait)
            ll = self.last_limit or {}
        self._save()
        log.warning("YouTube is rate-limiting; pausing YouTube requests for %ds (signal %s, video %s, %s requests "
                    "in 10 min, %s in 60 min): %s", wait, ll.get("signal"), ll.get("videoId"),
                    ll.get("requests10"), ll.get("requests60"), ll.get("message"))

    def remaining(self) -> int:
        return max(0, int(self.paused_until - time.time() + 0.999))

    def max_waits(self) -> int:
        """How many cool-downs a job sits through before it fails."""
        return len(self.BACKOFF)

    def wait_cooldown(self, progress=None):
        """Block while YouTube requests are paused or the hourly budget is used up, telling the job why."""
        while True:
            left = self.remaining()
            if left <= 0:
                break
            if progress:
                progress(_("job.wait_cooldown", time=f"{left // 60}:{left % 60:02d}"))
            self._sleep(min(left, 1.0))
        self.wait_budget(progress)

    def status(self) -> dict:
        now = time.time()
        with self.lock:
            r10, r60 = self._count(now, 600), self._count(now, 3600)
            ll = dict(self.last_limit) if self.last_limit else None
        if ll:
            ll["signalLabel"] = SIGNAL_LABELS.get(ll.get("signal"), ll.get("signal"))
        cs = cookie_status()
        paused = self.remaining()
        hint = BOT_HINT if (paused and ll and ll.get("signal") == "bot-check" and not cs["active"]) else None
        return {"pausedFor": paused, "strikes": self.strikes,
                "jsRuntime": (js_runtime() or ("", ""))[0] or None,
                "requests10": r10, "requests60": r60, "budget": self.budget,
                "lastLimit": ll, "cookies": cs, "hint": hint}


GATE = RateGate()
DOWNLOADS = threading.BoundedSemaphore(2)   # parallel media downloads (streams already resolved)


@contextmanager
def download_slot(video_id: str | None = None):
    _cancel.acquire(DOWNLOADS)
    try:
        try:
            yield
        except Exception as e:
            if is_bot_error(e):
                GATE.throttled(e, video_id)
                raise BotDetected(str(e), video_id) from e
            raise
    finally:
        DOWNLOADS.release()


# ------------------------------------------------------------------ sign-in cookies
COOKIE_BROWSERS = ("chrome", "edge", "firefox", "safari", "brave")
CHROMIUM_BROWSERS = ("chrome", "edge", "brave")
_COOKIES: dict = {"mode": "none", "file": "", "error": None, "failed": False}


def set_cookies(mode: str = "none", file: str = "") -> None:
    """Where yt-dlp reads the user's YouTube sign-in from: a browser name, 'file' (cookies.txt) or 'none'."""
    mode = (mode or "none").lower()
    if mode not in COOKIE_BROWSERS and mode != "file":
        mode = "none"
    _COOKIES.update(mode=mode, file=(file or "").strip(), error=None, failed=False)


def cookie_opts() -> dict:
    """yt-dlp options for the configured sign-in; {} when none, or after a failure (no cookies then)."""
    mode = _COOKIES["mode"]
    if mode == "none" or _COOKIES["failed"]:
        return {}
    if mode == "file":
        f = _COOKIES["file"]
        if not f or not os.path.isfile(f):
            _COOKIES.update(failed=True, error=f"cookies.txt not found ({f or 'no file chosen'}). Continuing without sign-in.")
            return {}
        return {"cookiefile": f}
    if sys.platform == "win32" and mode in CHROMIUM_BROWSERS:
        # Chrome/Edge/Brave on Windows use app-bound encryption: no other program can decrypt their cookies
        _COOKIES.update(failed=True, error=cookie_problem(RuntimeError("app-bound encryption"), mode))
        log.warning("%s", _COOKIES["error"])
        return {}
    return {"cookiesfrombrowser": (mode,)}


def cookie_status() -> dict:
    mode = _COOKIES["mode"]
    return {"mode": mode, "file": _COOKIES["file"], "error": _COOKIES["error"],
            "active": mode != "none" and not _COOKIES["failed"]}


def is_cookie_error(e: Exception) -> bool:
    m = str(e).lower()
    return not is_bot_error(e) and any(s in m for s in (
        "cookie", "dpapi", "keyring", "keychain", "full disk access", "app-bound", "app bound"))


def cookie_problem(e: Exception, browser: str):
    """A plain-language reason for a failed cookie read, ending in what happens next."""
    m = str(e).lower()
    name = browser.capitalize()
    if "dpapi" in m or "app-bound" in m or "app bound" in m or "decrypt" in m:
        why = _("yt.cookie.dpapi", name=name)
    elif browser == "safari" and ("permission" in m or "operation not permitted" in m or "full disk" in m):
        why = _("yt.cookie.safari")
    elif "locked" in m or "could not copy" in m or "being used by another" in m or "permission" in m:
        why = _("yt.cookie.locked", name=name)
    elif "could not find" in m or "no such file" in m or "not found" in m:
        why = _("yt.cookie.notfound", name=name)
    else:
        why = (str(e).strip().splitlines() or [type(e).__name__])[0][:160]
    return _("yt.cookie.fail", name=name, why=why)


def _note_cookie_failure(e: Exception) -> None:
    opts = cookie_opts_mode()
    msg = cookie_problem(e, opts) if opts != "file" else _("yt.cookie.file_fail", detail=str(e)[:120])
    _COOKIES.update(failed=True, error=msg)
    log.warning("%s (%s)", msg, e)


def cookie_opts_mode() -> str:
    return _COOKIES["mode"]


def _cancel_hook(d) -> None:
    """yt-dlp progress hook: a cancelled job stops the download at the next chunk."""
    _cancel.check()


def make_ydl(opts: dict):
    """YoutubeDL(opts); if the sign-in cookies can't be read, say so once and carry on without them.

    yt-dlp reads browser cookies lazily (on the first request), so a DPAPI / app-bound-encryption failure used to
    surface mid-download as "ERROR: Failed to decrypt with DPAPI" and fail the job (2.8.2, Windows). Touching
    `cookiejar` here makes it happen now, where it falls back to no cookies."""
    from yt_dlp import YoutubeDL
    try:
        ydl = YoutubeDL(opts)
        if "cookiesfrombrowser" in opts or "cookiefile" in opts:
            try:
                ydl.cookiejar                     # load the cookies now
            except Exception:
                ydl.close()
                raise
        return ydl
    except Exception as e:
        if ("cookiesfrombrowser" in opts or "cookiefile" in opts) and is_cookie_error(e):
            _note_cookie_failure(e)
            return YoutubeDL({k: v for k, v in opts.items() if k not in ("cookiesfrombrowser", "cookiefile")})
        raise


def check_cookies() -> str | None:
    """Try reading the configured sign-in now (called when the user picks it). Returns the problem, if any."""
    mode = _COOKIES["mode"]
    _COOKIES["failed"], _COOKIES["error"] = False, None
    if mode == "none":
        return None
    try:
        o = cookie_opts()
        if "cookiesfrombrowser" in o:
            from yt_dlp.cookies import extract_cookies_from_browser
            extract_cookies_from_browser(*o["cookiesfrombrowser"])
    except Exception as e:
        _note_cookie_failure(e)
    return _COOKIES["error"]


_TOOL_CACHE: dict = {}
JS_RUNTIMES = ("deno", "node", "bun", "qjs")
JS_HELP = _("yt.js_help")


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
            r = run([p, "--version"], capture_output=True, text=True, timeout=60)
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
            "retries": 5, "fragment_retries": 5, "noprogress": True,
            "sleep_interval_requests": GATE.request_sleep()}
    opts.update(cookie_opts())
    # Frozen apps carry ffmpeg outside PATH (imageio also uses a versioned filename).
    # yt-dlp must use the same executable as alignment and video conversion. Note that with
    # ffmpeg found yt-dlp merges the two streams fetch() asks for; fetch() splits them again.
    from .align import ffmpeg_path
    ff = ffmpeg_path()
    if ff:
        opts["ffmpeg_location"] = ff
    if runtimes:
        opts["js_runtimes"] = runtimes
    ea: dict = {}
    if clients:
        ea["youtube"] = {"player_client": clients}
    try:
        from . import potserver
        ea.update(potserver.extractor_args() or {})      # anonymous PO tokens (bgutil); None: as before
    except Exception:
        log.debug("PO token server unavailable", exc_info=True)
    if ea:
        opts["extractor_args"] = ea
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


def pick_sd(formats: list[dict], min_p: int) -> dict | None:
    """Best SD stream (min_p <= class < MIN_P): highest resolution first, then the usual codec/protocol order."""
    best, best_key = None, None
    for f in formats or []:
        if (f.get("vcodec") in (None, "none")) or not f.get("url"):
            continue
        p = res_class(f.get("width"), f.get("height"))
        if p < min_p or p >= MIN_P:
            continue
        proto = (f.get("protocol") or "")
        key = (p, _codec_rank(f.get("vcodec"), True), proto.startswith("http") and "m3u8" not in proto,
               f.get("acodec") in (None, "none"), f.get("tbr") or 0)
        if best_key is None or key > best_key:
            best, best_key = f, key
    if best is not None:
        best = dict(best)
        best["res_class"] = res_class(best.get("width"), best.get("height"))
    return best


def max_res(formats: list[dict]) -> int:
    return max((res_class(f.get("width"), f.get("height")) for f in formats or []
                if f.get("vcodec") not in (None, "none") and f.get("protocol") != "mhtml"), default=0)


def video_kbps(f: dict) -> float:
    """Bitrate of the picture alone (a muxed stream also carries its audio)."""
    if f.get("vbr"):
        return float(f["vbr"])
    t = float(f.get("tbr") or 0)
    if t and f.get("acodec") not in (None, "none"):
        t -= float(f.get("abr") or 96)
    return max(t, 0.0)


def still_reason(f: dict) -> str | None:
    """Why this stream is probably an audio-plus-picture upload, from the stream list alone (no download).
    Square frame (album art), 1 fps or less, an absolute picture bitrate under 90 kbps (what looks_static()
    checks after a download), or bits per pixel per frame under 0.005: a moving video of any resolution or codec
    needs at least 0.02 (recorded 144p-2160p lists of a real music video: 0.022 for 1080p VP9 up to 0.063)."""
    w, h = f.get("width") or 0, f.get("height") or 0
    if w and h and abs(w - h) <= 2:
        return "square frame"
    fps = f.get("fps")
    if fps is not None and float(fps) <= STILL_MAX_FPS:
        return f"{fps} fps"
    kbps = video_kbps(f)
    if kbps and kbps < STILL_MIN_KBPS and h >= 360:
        return f"{kbps:.0f} kbps"
    if kbps and w and h:
        bpp = kbps * 1000 / (w * h * float(fps or 25))      # an unknown frame rate counts as 25: never flags more
        if bpp < STILL_MIN_BPP:
            return f"{bpp:.4f} bits/pixel/frame"
    return None


# ------------------------------------------------------------------ search
# A release-version suffix on a chart's song name ("Prayer In C - Robin Schulz Radio Edit", "Song - Remastered 2011",
# "Song (Single Version)") is not in the music video's title, and made official videos score below MIN_SCORE (2.10.1).
_VERSION_WORDS = (r"(?:radio\s+edit|single\s+edit|edit|remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?|"
                  r"single\s+version|album\s+version|radio\s+version|original\s+mix|extended\s+mix|club\s+mix|"
                  r"mono|stereo|explicit|clean(?:\s+version)?|remix)")
_VERSION_DASH_RE = re.compile(r"\s+[-\u2013\u2014]\s+(?:[^-\u2013\u2014]*?\s)?" + _VERSION_WORDS + r"\b[^-\u2013\u2014]*$", re.I)
_VERSION_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*\b" + _VERSION_WORDS + r"\b[^\)\]]*[\)\]]", re.I)


def match_name(name: str) -> str:
    """The song name as a music video would title it: chart tags and release-version suffixes removed."""
    s = clean_title(name)
    for _ in range(2):
        s = _VERSION_PAREN_RE.sub("", _VERSION_DASH_RE.sub("", s)).strip()
    return s or clean_title(name)


def is_version_of_remix(song_name: str) -> bool:
    """The chart is itself an edit/remix ("- Robin Schulz Radio Edit"), so a "Remix" video can be the right one."""
    return bool(re.search(r"\b(?:remix|edit|mix)\b", song_name or "", re.I))


def score_candidate(entry: dict, song: Song) -> float:
    title = norm_key(entry.get("title") or "")
    chan = norm_key(entry.get("channel") or entry.get("uploader") or "")
    artist = norm_key(clean_title(song.artist))
    name = norm_key(re.sub(r"\s*[\(\[].*?[\)\]]", "", match_name(song.name))) or norm_key(song.name)
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
        if f" {w} " in f" {title} " and w not in full_name \
                and not (w in ("remix", "mix") and is_version_of_remix(song.name)):
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


def _compact_channel(name: str) -> str:
    c = norm_key(name).split()
    while c and c[0] == "the":
        c = c[1:]
    s = "".join(c)
    for suf in ("officialchannel", "officialyoutube", "official", "vevo", "music", "tv"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)]
    return s


def is_strong_official(entry: dict, song: Song, score: float | None = None) -> bool:
    """An official upload for this song, sure enough to take its best SD version at once instead of trying other
    uploads for HD. score_candidate() tops out at 15 (title 5, artist 3, "official music video" 4, "music video" 1,
    VEVO/artist channel 2, minus up to 4 for a wrong duration); >= 12 needs the title, the artist and "official ...
    video" all matching and an exact duration, or the artist/VEVO channel on top with a few seconds' slack. Beyond the score, one of: the channel is the
    artist (or its VEVO/Topic channel), or the title says "official"."""
    sc = score_candidate(entry, song) if score is None else score
    if sc < STRONG_SCORE:
        return False
    title = norm_key(entry.get("title") or "")
    chan = norm_key(entry.get("channel") or entry.get("uploader") or "")
    artist = norm_key(clean_title(song.artist))
    ac, cc = _compact_channel(artist), _compact_channel(chan)
    return bool("official" in title.split() or "vevo" in chan or chan.endswith(" topic")
                or (ac and cc and ac == cc))


class YouTube:
    def __init__(self, max_height: int = 1080, pref_p: int = 720, sd_min_p: int = 0):
        self.max_height = max_height
        self.pref_p = pref_p
        self.sd_min_p = sd_min_p      # 0: never use SD; else the lowest SD class accepted when no HD exists
        self._streams: dict[str, tuple[float, dict]] = {}
        self._infos: dict[str, tuple[float, dict]] = {}
        self._searches: dict[str, tuple[float, list]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------------- search
    def search(self, song: Song, n: int = SEARCH_RESULTS) -> list[dict]:
        from yt_dlp import YoutubeDL
        q = f"{clean_title(song.artist)} - {match_name(song.name)} official music video"
        with self._lock:                     # one search per song per session (retries, other charts)
            hit = self._searches.get(norm_key(q))
            if hit and hit[0] > time.time():
                return [dict(c) for c in hit[1]]
        with GATE.slot(), make_ydl(_ydl_opts(extract_flat="in_playlist")) as ydl:
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
        with GATE.slot(), make_ydl(_ydl_opts(extract_flat="in_playlist")) as ydl:
            info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{q}", download=False)
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
                with GATE.slot(video_id), make_ydl(_ydl_opts(clients)) as ydl:
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
        return ReducedFormats(_("yt.reduced_empty" if st == "empty" else "yt.reduced_nosep", count=len(formats or [])))

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
        hooks = [_cancel_hook]
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
            with GATE.slot(video_id), download_slot(video_id), make_ydl(opts) as ydl:
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

    def fetch(self, video_id: str, dest_noext: str, with_audio: bool = False, progress=None,
              take_sd: bool = False) -> tuple[str, dict, str | None]:
        """One YouTube lookup, then the HD video stream (and the audio stream, for lining the
        video up) downloaded in the same session: (video_path, format, audio_path).
        Raises NoHDVideo when the video has no >= 640p stream (its sd_p says what SD it does have, when
        self.sd_min_p asks for SD; take_sd=True downloads that SD stream instead of raising), StillImage when
        the stream list says it is a picture with audio (checked before anything is downloaded); BotDetected only when YouTube really
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
                _cancel.acquire(DOWNLOADS)
                state["dl"] = True
            state["n"] = len(fm)
            v = pick_format(fm, pref, max_p=maxp)
            if not v:
                st = list_state(fm)
                state["why"] = "sd" if st == "full" else st
                if st != "full":
                    return
                state["max_p"] = max_res(fm)
                sd = pick_sd(fm, self.sd_min_p) if self.sd_min_p else None
                if not sd:
                    return
                state["sd_p"] = sd["res_class"]
                if take_sd:
                    v = sd
                    state["sd_taken"] = True
                    state["why"] = None
                else:
                    return
            state["max_p"] = state.get("max_p") or v["res_class"]
            reason = still_reason(v)
            if reason:                        # decided from the stream list: nothing is downloaded
                state["why"], state["still"] = "still", reason
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

        hooks = [_cancel_hook]
        if progress:
            def hook(d):
                if d.get("status") == "downloading" and (d.get("info_dict") or {}).get("vcodec") not in (None, "none"):
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress(d.get("downloaded_bytes", 0), tot)
            hooks.append(hook)
        opts = _ydl_opts(CLIENT_FALLBACKS[0], format=selector, outtmpl=dest_noext + ".f%(format_id)s.%(ext)s",
                         progress_hooks=hooks, overwrites=True, continuedl=False,
                         concurrent_fragment_downloads=4)
        GATE.acquire(video_id)
        ok = True
        try:
            with make_ydl(opts) as ydl:
                done = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        except Exception as e:
            ok = False
            c = classify_error(e, state.get("why"), state.get("n", 0))
            if isinstance(c, BotDetected):
                c.video_id = video_id
                GATE.throttled(c, video_id)
            if isinstance(c, NoHDVideo):
                c.sd_p, c.max_p = state.get("sd_p", 0), state.get("max_p", 0)
            if isinstance(c, StillImage):
                log.info("%s: %s, skipped without downloading", video_id, state.get("still"))
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
            raise RuntimeError(_("yt.no_stream"))
        if with_audio and not apath and got.get("acodec") not in (None, "none"):
            # ffmpeg was found, so yt-dlp merged the streams into one file. Split it again with a
            # stream copy: the video must stay video-only, and the audio is needed for lining up.
            # Asking YouTube for the audio a second time is what used to trigger rate limiting.
            vpath, apath = _split_merged(vpath)
        got_res = res_class(got.get("width"), got.get("height"))
        if got_res and got_res < (self.sd_min_p if state.get("sd_taken") else MIN_P):
            for x in (vpath, apath):
                try:
                    Path(x).unlink()
                except (OSError, TypeError):
                    pass
            raise NoHDVideo(f"only {got_res}p was delivered")
        fmt = dict(state.get("fmt") or {})
        fmt.update({k: got.get(k) for k in ("format_id", "vcodec", "width", "height", "ext") if got.get(k)})
        fmt["res_class"] = got_res or fmt.get("res_class")
        fmt["sd"] = bool(fmt["res_class"] and fmt["res_class"] < MIN_P)
        return vpath, fmt, apath

    def download_audio(self, video_id: str, dest_noext: str) -> str:
        from yt_dlp import YoutubeDL
        last = None
        for fmt in ("bestaudio[ext=m4a]/bestaudio", "bestaudio[acodec=opus]/bestaudio", "worstaudio"):
            try:
                opts = _ydl_opts(format=fmt, outtmpl=dest_noext + ".%(ext)s", overwrites=True,
                                 progress_hooks=[_cancel_hook])
                with GATE.slot(video_id), download_slot(video_id), make_ydl(opts) as ydl:
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
