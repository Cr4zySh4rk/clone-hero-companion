"""Recover artist/name/charter for favourites that only have an id (starred under v2.1.1, whose charts were
trashed). Sources, in order: the OS trash and the app's own 'Deleted Songs' (same chart parsing and
checksum as the library), the pid itself, library_cache.json, legacy song-level keys. Best effort, quiet."""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

from .library import Library, Song, _read_text, parse_ini, strip_tags
from .sng import open_sng

log = logging.getLogger(__name__)
MAX_DEPTH = 8
MAX_SECONDS = 90.0


def trash_dirs(data_dir: Path, roots=()) -> list[Path]:
    """Folders that may hold trashed charts on this OS, plus <data_dir>/Deleted Songs."""
    out: list[Path] = [Path(data_dir) / "Deleted Songs"]
    try:
        if sys.platform == "darwin":
            out.append(Path.home() / ".Trash")
        elif sys.platform == "win32":
            seen = set()
            for r in roots:
                drive = Path(r).drive
                if not drive or drive.upper() in seen:
                    continue
                seen.add(drive.upper())
                try:
                    for sid in Path(drive + "\\$Recycle.Bin").iterdir():
                        out.extend(p for p in sid.iterdir() if p.name.startswith("$R"))
                except OSError:
                    continue
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            out.append(Path(base) / "Trash" / "files")
    except Exception as e:
        log.debug("trash folder lookup failed: %s", e)
    return out


def scan_trash(dirs, deadline: float | None = None):
    """Yield every chart (song.ini folder or .sng) found under dirs, limited in depth and time.
    A folder that cannot be read (macOS without Full Disk Access) is skipped quietly."""
    deadline = deadline or time.monotonic() + MAX_SECONDS
    for top in dirs:
        try:
            top = Path(top)
            if not top.exists():
                continue
            if top.is_file() and top.suffix.lower() == ".sng":
                walk = [(str(top.parent), [], [top.name])]
            else:
                walk = os.walk(top, onerror=lambda e: None)
            base_depth = len(top.parts)
            for dirpath, dirs_, files in walk:
                if time.monotonic() > deadline:
                    return
                if len(Path(dirpath).parts) - base_depth >= MAX_DEPTH:
                    dirs_[:] = []
                if "song.ini" in files:
                    dirs_[:] = []
                    try:
                        yield Song(Path(dirpath), False, parse_ini(_read_text(Path(dirpath) / "song.ini")))
                    except Exception as e:
                        log.debug("bad ini in trash %s: %s", dirpath, e)
                    continue
                for f in files:
                    if f.lower().endswith(".sng"):
                        p = Path(dirpath) / f
                        try:
                            yield Song(p, True, open_sng(str(p)).metadata)
                        except Exception as e:
                            log.debug("bad sng in trash %s: %s", p, e)
        except OSError as e:
            log.debug("trash folder %s not readable: %s", top, e)


CHARTER_PLACEHOLDERS = {"unknown charter", "unknown", "no charter", "charter unknown", "unknown charter s", "n a"}
_TAIL_RE = re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$")


def strip_charter_suffix(title: str, charter: str = "") -> str:
    """Drop a trailing '(...)' that is the charter ('Da Funk (Justin? )', matched loosely) or a placeholder
    such as '(Unknown Charter)'. Idempotent; other bracketed qualifiers ('(Live)') are kept."""
    from .favmatch import fold
    want = fold(charter)
    while True:
        m = _TAIL_RE.search(title)
        inner = fold(m.group(1)) if m else ""
        if not m or not inner or not (inner == want or inner in CHARTER_PLACEHOLDERS):
            return title.strip()
        title = title[:m.start()]


def parse_pid(pid: str) -> dict:
    """Readable metadata from 'norm(artist - name)|norm(charter)|relpath'. The first two parts are
    normalised (no hyphen, lower case), so artist and title come from the relpath's folder name when it
    reads 'Artist - Title'; otherwise the title is the normalised text."""
    parts = (pid or "").split("|", 2)
    if len(parts) < 3:
        return {}
    ident, charter, rel = parts
    leaf = re.sub(r"\.sng$", "", rel.replace("\\", "/").rstrip("/").split("/")[-1], flags=re.I)
    leaf = strip_tags(leaf)
    leaf = strip_charter_suffix(leaf, charter)
    out = {}
    if " - " in leaf:
        artist, _, name = leaf.partition(" - ")
        if artist.strip() and name.strip():
            out = {"artist": artist.strip(), "name": name.strip()}
    if not out and ident:
        out = {"name": leaf.strip() or ident}
    if charter:
        out["charter"] = charter
    return out


def unknown_label(v: dict, key: str) -> str:
    import hashlib
    h = (v.get("md5") or "") or hashlib.md5((v.get("pid") or key).encode("utf-8", "replace")).hexdigest()
    return f"Unknown chart (checksum {h[:6].lower()}…)"
