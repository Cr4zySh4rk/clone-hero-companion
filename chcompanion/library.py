"""Index of the user's song library (song folders and .sng packages)."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import lyrics as lyr
from .sng import open_sng

log = logging.getLogger(__name__)

VIDEO_NAMES = ("video.mp4", "video.webm", "video.m4v", "video.mov", "video.avi",
               "video.mpeg", "video.mpg", "video.ogv", "video.vp8")
ART_NAMES = ("album.png", "album.jpg", "album.jpeg", "background.png", "background.jpg")
TAG_RE = re.compile(r"<[^>]*>")


def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "").strip()


def norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", strip_tags(s)).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", " ", s).strip()


def parse_ini(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith(("[", ";", "#")) or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lower()
        if k and k not in out:
            out[k] = v.strip()
    return out


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


@dataclass
class Song:
    path: Path                 # folder, or the .sng file
    is_sng: bool
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def artist(self) -> str:
        return strip_tags(self.meta.get("artist", "")) or "Unknown Artist"

    @property
    def name(self) -> str:
        return strip_tags(self.meta.get("name", "")) or self.path.stem

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.name}"

    @property
    def key(self) -> str:
        return norm_key(self.display)

    @property
    def length_s(self) -> float | None:
        try:
            v = float(self.meta.get("song_length", "0"))
            return v / 1000.0 if v > 0 else None
        except ValueError:
            return None

    @property
    def delay_s(self) -> float:
        try:
            return float(self.meta.get("delay", "0") or 0) / 1000.0
        except ValueError:
            return 0.0

    @property
    def video_start_s(self) -> float:
        try:
            return float(self.meta.get("video_start_time", "0") or 0) / 1000.0
        except ValueError:
            return 0.0

    def to_public(self) -> dict:
        return {
            "key": self.key, "artist": self.artist, "name": self.name,
            "album": strip_tags(self.meta.get("album", "")),
            "year": strip_tags(self.meta.get("year", "")).lstrip(", "),
            "charter": strip_tags(self.meta.get("charter", "") or self.meta.get("frets", "")),
            "genre": strip_tags(self.meta.get("genre", "")),
            "length": self.length_s, "sng": self.is_sng, "path": str(self.path),
        }

    # ---- file access (folder or .sng) ----
    def list_files(self) -> list[str]:
        if self.is_sng:
            try:
                return list(open_sng(str(self.path)).files.keys())
            except Exception:
                return []
        try:
            return [p.name for p in self.path.iterdir() if p.is_file()]
        except OSError:
            return []

    def read_file(self, name: str) -> bytes | None:
        if self.is_sng:
            try:
                return open_sng(str(self.path)).read(name)
            except Exception:
                return None
        for p in self.path.iterdir():
            if p.name.lower() == name.lower() and p.is_file():
                return p.read_bytes()
        return None

    @property
    def preview_ms(self) -> int | None:
        try:
            v = int(float(self.meta.get("preview_start_time", "-1") or -1))
            return v if v >= 0 else None
        except ValueError:
            return None

    def local_video(self) -> Path | None:
        if self.is_sng:
            return self._sng_video()
        names = {p.name.lower(): p for p in self.path.iterdir() if p.is_file()}
        for n in VIDEO_NAMES:
            if n in names:
                return names[n]
        return None

    def _sng_video(self) -> Path | None:
        """Extract a video packed in the .sng to a cache file (for the companion page)."""
        try:
            sng = open_sng(str(self.path))
        except Exception:
            return None
        name = next((n for n in sng.files if n.lower() in VIDEO_NAMES), None)
        if not name:
            return None
        import hashlib
        from .paths import config_dir
        st = self.path.stat()
        h = hashlib.sha1(f"{self.path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
        out = config_dir() / "cache" / f"{h}{Path(name).suffix.lower()}"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            data = sng.read(name)
            if not data:
                return None
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(out)
        return out

    def art_file(self) -> tuple[str, bytes] | None:
        files = {f.lower(): f for f in self.list_files()}
        for n in ART_NAMES:
            if n in files:
                data = self.read_file(files[n])
                if data:
                    return n, data
        return None

    def chart_lyrics(self) -> list[dict]:
        files = {f.lower(): f for f in self.list_files()}
        try:
            if "notes.mid" in files:
                data = self.read_file(files["notes.mid"])
                if data:
                    lines = lyr.from_midi(data, self.delay_s)
                    if lines:
                        return lines
            if "notes.chart" in files:
                data = self.read_file(files["notes.chart"])
                if data:
                    return lyr.from_chart(data.decode("utf-8-sig", "replace"), self.delay_s)
        except Exception as e:
            log.warning("Could not read lyrics from chart of %s: %s", self.display, e)
        return []


class Library:
    def __init__(self, roots: list[Path]):
        self.roots = roots
        self.songs: list[Song] = []
        self.by_key: dict[str, list[Song]] = {}
        self.lock = threading.Lock()
        self.scanned_at = 0.0

    def scan(self) -> int:
        t0 = time.time()
        songs: list[Song] = []
        for root in self.roots:
            for ini in root.rglob("song.ini"):
                try:
                    songs.append(Song(ini.parent, False, parse_ini(_read_text(ini))))
                except Exception as e:
                    log.debug("bad ini %s: %s", ini, e)
            for sng in root.rglob("*.sng"):
                try:
                    songs.append(Song(sng, True, open_sng(str(sng)).metadata))
                except Exception as e:
                    log.debug("bad sng %s: %s", sng, e)
        by_key: dict[str, list[Song]] = {}
        for s in songs:
            by_key.setdefault(s.key, []).append(s)
        with self.lock:
            self.songs, self.by_key, self.scanned_at = songs, by_key, time.time()
        log.info("Indexed %d songs in %.1fs", len(songs), time.time() - t0)
        return len(songs)

    def find(self, display: str) -> Song | None:
        """Find a song by the 'Artist - Name' string Clone Hero logs."""
        key = norm_key(display)
        with self.lock:
            hits = self.by_key.get(key)
            if not hits:
                # tolerate differences (tags, punctuation): substring match on both parts
                for k, v in self.by_key.items():
                    if k.replace(" ", "") == key.replace(" ", ""):
                        hits = v
                        break
        if not hits:
            return None
        # prefer the copy with vocals / most recently modified
        def rank(s: Song):
            files = {f.lower() for f in s.list_files()}
            return ("vocals.ogg" in files or "vocals.opus" in files or "vocals.mp3" in files,
                    s.meta.get("diff_vocals", "-1") not in ("-1", ""))
        return max(hits, key=rank)
