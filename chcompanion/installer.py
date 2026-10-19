"""Put a song's music video into its folder so Clone Hero plays it natively
behind the highway (Settings -> Custom Content -> Song Videos must be on)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .library import Song, VIDEO_NAMES
from .store import Store
from .youtube import YouTube

log = logging.getLogger(__name__)


class InstallError(Exception):
    pass


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _probe_codec(path: Path) -> str:
    ff = _ffmpeg()
    if not ff:
        return ""
    try:
        out = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True,
                             text=True, timeout=30).stderr
        m = re.search(r"Video:\s*(\w+)", out)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def _transcode_vp8(src: Path, dst: Path, progress=None) -> None:
    ff = _ffmpeg()
    if not ff:
        raise InstallError("ffmpeg is needed to convert the video for Clone Hero on Linux "
                           "(Unity only plays VP8 .webm there). Install ffmpeg and retry.")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-an",
           "-c:v", "libvpx", "-b:v", "2500k", "-crf", "10", "-deadline", "realtime",
           "-cpu-used", "8", "-vf", "scale=-2:'min(720,ih)'", str(dst)]
    if progress:
        progress("converting")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise InstallError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")


def set_video_start_time(song: Song, offset_s: float) -> None:
    """Write video_start_time (ms) into song.ini, preserving everything else."""
    ini = song.path / "song.ini"
    if not ini.exists():
        return
    raw = ini.read_bytes()
    enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        enc, text = "latin-1", raw.decode("latin-1")
    ms = int(round(offset_s * 1000))
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    found = False
    for i, l in enumerate(lines):
        if l.split("=", 1)[0].strip().lower() == "video_start_time":
            lines[i] = f"video_start_time = {ms}"
            found = True
    if not found:
        idx = next((i for i, l in enumerate(lines) if l.strip().lower() == "[song]"), None)
        if idx is None:
            lines.insert(0, "[song]")
            idx = 0
        lines.insert(idx + 1, f"video_start_time = {ms}")
    ini.write_text(nl.join(lines), encoding=enc)
    song.meta["video_start_time"] = str(ms)


def install_video(song: Song, yt: YouTube, store: Store, force: bool = False,
                  target: str = sys.platform, progress=None) -> Path:
    if song.is_sng:
        raise InstallError(".sng packages can't hold an extra video file; "
                           "the companion window still shows the video for this song.")
    existing = song.local_video()
    if existing and not force:
        return existing
    info = store.song(song.key)
    vid = info.get("video_id")
    if not vid:
        cands = yt.search(song)
        if not cands:
            raise InstallError("no video found on YouTube")
        vid = cands[0]["id"]
        store.update_song(song.key, video_id=vid)
    if force:
        for n in VIDEO_NAMES:
            p = song.path / n
            if p.exists():
                p.unlink()
    tmp_base = str(song.path / "video.__download")

    def prog(done, total):
        if progress:
            progress(f"downloading {done * 100 // total if total else 0}%")
    path = Path(yt.download(vid, tmp_base, progress=prog))
    ext = path.suffix.lower()
    codec = _probe_codec(path)
    linux = target.startswith("linux")
    if linux and not (ext == ".webm" and codec in ("vp8", "")):
        out = song.path / "video.webm"
        try:
            _transcode_vp8(path, out, progress)
        finally:
            path.unlink(missing_ok=True)
    elif not linux and ext == ".webm" and codec and codec != "vp8":
        # VP9/AV1 webm won't play in Unity on Windows/macOS -> re-encode if possible
        out = song.path / "video.webm"
        try:
            _transcode_vp8(path, out, progress)
        finally:
            path.unlink(missing_ok=True)
    else:
        out = song.path / f"video{ext}"
        path.replace(out)
    off = float(info.get("video_offset", 0.0) or 0.0)
    if abs(off) >= 0.05:
        set_video_start_time(song, off)
    log.info("Installed %s for %s", out.name, song.display)
    return out
