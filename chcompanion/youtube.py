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
import threading
import time
import urllib.parse
from contextlib import contextmanager

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
BOT_SIGNS = ("sign in to confirm", "not a bot", "http error 429", "too many requests")
# yt-dlp player clients to fall back on when the default one can't get HD formats
# (BackstageHero's set: no PO token needed for DASH streams).
CLIENT_FALLBACKS = [None, ["tv_embedded", "android_vr", "android"], ["web_safari", "mweb"]]


class BotDetected(Exception):
    pass


class NoHDVideo(Exception):
    pass


def is_bot_error(e: Exception) -> bool:
    m = str(e).lower()
    return any(s in m for s in BOT_SIGNS)


# ------------------------------------------------------------------ rate gate
class RateGate:
    """Limits concurrent YouTube API calls, spaces them out, and pauses everyone
    after a bot/rate-limit response (60 s, 3 min, 7 min, ...)."""
    BACKOFF = [60, 180, 420, 900]

    def __init__(self, concurrent: int = 3, spacing: float = 0.35):
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

    def throttled(self):
        with self.lock:
            wait = self.BACKOFF[min(self.strikes, len(self.BACKOFF) - 1)]
            self.strikes += 1
            self.paused_until = max(self.paused_until, time.time() + wait)
        log.warning("YouTube is rate-limiting; pausing YouTube requests for %ds", wait)

    def status(self) -> dict:
        return {"pausedFor": max(0, int(self.paused_until - time.time())), "strikes": self.strikes}


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


def _ydl_opts(clients=None, **extra) -> dict:
    runtimes = {}
    for rt in ("deno", "node", "bun", "qjs"):
        p = shutil.which(rt)
        if p:
            runtimes["quickjs" if rt == "qjs" else rt] = {"path": p}
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
            "retries": 5, "fragment_retries": 5, "noprogress": True}
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
        self._lock = threading.Lock()

    # -------------------------------------------------------------- search
    def search(self, song: Song, n: int = 8) -> list[dict]:
        from yt_dlp import YoutubeDL
        q = f"{clean_title(song.artist)} - {clean_title(song.name)} official music video"
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
        """Full extraction (formats with URLs), cached ~1 h. Tries fallback player
        clients when the default one returns no format at >= MIN_P."""
        with self._lock:
            hit = self._infos.get(video_id)
            if hit and hit[0] > time.time():
                return hit[1]
        from yt_dlp import YoutubeDL
        last_err, info = None, None
        for clients in CLIENT_FALLBACKS:
            try:
                with GATE.slot(), YoutubeDL(_ydl_opts(clients)) as ydl:
                    cur = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                info = cur
                if pick_format(cur.get("formats") or [], self.pref_p):
                    break
            except BotDetected:
                raise
            except Exception as e:
                last_err = e
        if info is None:
            raise last_err or RuntimeError("extraction failed")
        with self._lock:
            self._infos[video_id] = (time.time() + 3000, info)
        return info

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
            raise NoHDVideo(f"no format at {MIN_P}p or better")
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
            raise NoHDVideo(f"no format at {MIN_P}p or better")
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
        with download_slot(), YoutubeDL(opts) as ydl:
            done = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            path = ydl.prepare_filename(done)
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
            except Exception as e:  # sporadic 403s: try another format
                last = e
        raise last  # type: ignore[misc]
