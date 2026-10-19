"""Find and stream a song's music video from YouTube via yt-dlp."""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import urllib.parse

from .library import Song, norm_key
from .lyrics import clean_title

log = logging.getLogger(__name__)

MIN_SCORE = 4.0
BAD_WORDS = ["lyric", "lyrics", "cover", "reaction", "karaoke", "tutorial", "lesson",
             "guitar", "drum", "drums", "bass", "instrumental", "remix", "slowed", "reverb",
             "8d", "sped up", "nightcore", "clone hero", "rock band", "guitar hero", "fortnite",
             "letra", "traducida", "sub espanol", "legendado", "1 hour", "10 hours", "extended",
             "playthrough", "isolated", "backing track", "piano", "fan made", "fanmade", "amv",
             "teaser", "trailer", "behind the scenes", "making of", "interview", "full album",
             "expert", "hard", "medium", "easy", "fc", "full combo", "gameplay", "playthrough", "yarg",
             "rocksmith", "pivot", "sickfigures", "chart", "custom song", "100", "fret", "rb3", "gh3"]


def _ydl_opts(**extra) -> dict:
    runtimes = {}
    for rt in ("deno", "node", "bun", "qjs"):
        p = shutil.which(rt)
        if p:
            runtimes["quickjs" if rt == "qjs" else rt] = {"path": p}
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 15,
            "retries": 5, "fragment_retries": 5, "noprogress": True}
    if runtimes:
        opts["js_runtimes"] = runtimes
    opts.update(extra)
    return opts


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


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
    def __init__(self, max_height: int = 1080):
        self.max_height = max_height
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def search(self, song: Song, n: int = 8) -> list[dict]:
        from yt_dlp import YoutubeDL
        q = f"{clean_title(song.artist)} - {clean_title(song.name)} official music video"
        with YoutubeDL(_ydl_opts(extract_flat="in_playlist")) as ydl:
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

    def best(self, song: Song) -> dict | None:
        """Best candidate, or None if nothing looks like this song's video."""
        c = self.search(song)
        return c[0] if c and c[0]["score"] >= MIN_SCORE else None

    def format_selector(self, for_game: bool = False) -> str:
        h = self.max_height
        if for_game:
            # Clone Hero (Unity) plays H.264 mp4 on Windows/macOS; VP8/VP9 webm everywhere.
            return (f"bestvideo[height<={h}][vcodec^=avc1][ext=mp4]/"
                    f"bestvideo[height<={h}][ext=webm]/best[height<={h}]")
        return (f"bestvideo[height<={h}][vcodec^=avc1][protocol^=http]/"
                f"bestvideo[height<={h}][vcodec^=vp9][protocol^=http]/"
                f"bestvideo[height<={h}][protocol^=http]/best[protocol^=http]")

    def resolve(self, video_id: str) -> dict:
        """Direct (googlevideo) URL for the video-only stream; cached until it expires."""
        with self._lock:
            hit = self._cache.get(video_id)
            if hit and hit[0] > time.time() + 300:
                return hit[1]
        from yt_dlp import YoutubeDL
        with YoutubeDL(_ydl_opts(format=self.format_selector())) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        fmt = info
        if info.get("requested_formats"):
            fmt = info["requested_formats"][0]
        url = fmt.get("url")
        if not url:
            raise RuntimeError("no playable format")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        expire = float(qs.get("expire", [time.time() + 3600])[0])
        res = {"url": url, "ext": fmt.get("ext"), "vcodec": fmt.get("vcodec"),
               "height": fmt.get("height"), "headers": fmt.get("http_headers") or {},
               "title": info.get("title"), "duration": info.get("duration")}
        with self._lock:
            self._cache[video_id] = (expire, res)
        return res

    def download_audio(self, video_id: str, dest_noext: str) -> str:
        from yt_dlp import YoutubeDL
        last = None
        for fmt in ("bestaudio[ext=m4a]/bestaudio", "bestaudio[acodec=opus]/bestaudio", "worstaudio"):
            try:
                opts = _ydl_opts(format=fmt, outtmpl=dest_noext + ".%(ext)s", overwrites=True)
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                    return ydl.prepare_filename(info)
            except Exception as e:  # sporadic 403s: try another format
                last = e
        raise last  # type: ignore[misc]

    def download(self, video_id: str, dest_noext: str, progress=None) -> str:
        """Download the video-only stream to dest_noext.<ext>; returns the final path."""
        from yt_dlp import YoutubeDL
        hooks = []
        if progress:
            def hook(d):
                if d.get("status") == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress(d.get("downloaded_bytes", 0), tot)
            hooks.append(hook)
        opts = _ydl_opts(format=self.format_selector(for_game=True),
                         outtmpl=dest_noext + ".%(ext)s", progress_hooks=hooks,
                         overwrites=True, continuedl=False)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            path = ydl.prepare_filename(info)
        return path
