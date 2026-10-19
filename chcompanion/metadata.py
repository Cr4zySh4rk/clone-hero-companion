"""One-click song metadata: album, year, genre, length and album art.

Sources, in order: Chorus Encore's chart database (same songs, fast, generous),
then the iTunes Search API (rate limited to ~20 requests/minute, so it is paced).
Only missing fields are filled unless `overwrite` is set. Works for song folders
(song.ini + album.jpg) and .sng packages (metadata + album.jpg inside).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .library import Song, norm_key, strip_tags
from .lyrics import clean_title
from .net import urlopen
from .sng import open_sng, write_sng

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ART_NAMES = ("album.png", "album.jpg", "album.jpeg")
_itunes_lock = threading.Lock()
_itunes_last = [0.0]


def _get_json(url: str, data: dict | None = None, timeout: float = 15.0):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Accept": "application/json",
        **({"Content-Type": "application/json"} if body else {})})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_bytes(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _same(a: str, b: str) -> bool:
    a, b = norm_key(clean_title(a)), norm_key(clean_title(b))
    return bool(a) and (a == b or a.replace(" ", "") == b.replace(" ", ""))


def _loose(a: str, b: str) -> bool:
    a, b = norm_key(clean_title(a)), norm_key(clean_title(b))
    return bool(a and b) and (a in b or b in a)


# ------------------------------------------------------------------ sources
def from_encore(song: Song) -> dict | None:
    from .sources import encore
    try:
        res = encore.search(f"{clean_title(song.artist)} {clean_title(song.name)}", per_page=20)
    except Exception as e:
        log.debug("encore metadata: %s", e)
        return None
    best, best_score = None, -1
    for c in res.get("songs", []):
        if not (_same(c["name"], song.name) or _loose(c["name"], song.name)):
            continue
        if not (_same(c["artist"], song.artist) or _loose(c["artist"], song.artist)):
            continue
        score = sum(bool(c.get(k)) for k in ("album", "year", "genre", "artUrl"))
        score += 2 if _same(c["name"], song.name) else 0
        if score > best_score:
            best, best_score = c, score
    if not best:
        return None
    return {"album": best.get("album") or None, "year": str(best.get("year") or "") or None,
            "genre": best.get("genre") or None, "art_url": best.get("artUrl"), "source": "Chorus Encore"}


def from_itunes(song: Song) -> dict | None:
    term = f"{clean_title(song.artist)} {re.sub(r'[(][^)]*[)]', '', clean_title(song.name))}"
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "song", "limit": 15})
    with _itunes_lock:   # ~20 requests/minute allowed
        wait = _itunes_last[0] + 3.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        _itunes_last[0] = time.time()
    try:
        data = _get_json(url)
    except Exception as e:
        log.debug("itunes metadata: %s", e)
        return None
    best, best_key = None, None
    for r in data.get("results", []):
        if not _loose(r.get("artistName", ""), song.artist):
            continue
        if not (_same(r.get("trackName", ""), song.name) or _loose(r.get("trackName", ""), song.name)):
            continue
        dur = (r.get("trackTimeMillis") or 0) / 1000
        key = (_same(r.get("trackName", ""), song.name),
               "compilation" not in (r.get("collectionName") or "").lower()
               and "greatest" not in (r.get("collectionName") or "").lower(),
               -abs(dur - (song.length_s or dur)))
        if best_key is None or key > best_key:
            best, best_key = r, key
    if not best:
        return None
    art = best.get("artworkUrl100")
    if art:
        art = art.replace("100x100bb", "1000x1000bb")
    return {"album": best.get("collectionName"), "year": (best.get("releaseDate") or "")[:4] or None,
            "genre": best.get("primaryGenreName"), "art_url": art, "source": "iTunes"}


# ------------------------------------------------------------------ helpers
def audio_length_ms(song: Song) -> int | None:
    from .align import chart_audio_files, ffmpeg_path
    ff = ffmpeg_path()
    names = chart_audio_files(song.list_files())
    if not ff or not names:
        return None
    best = 0.0
    tmpdir = None
    try:
        for n in names[:3]:
            if song.is_sng:
                import tempfile
                tmpdir = tmpdir or Path(tempfile.mkdtemp(prefix="chc-meta-"))
                p = tmpdir / n
                p.write_bytes(song.read_file(n) or b"")
            else:
                p = song.path / n
            err = subprocess.run([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True,
                                 timeout=30).stderr
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
            if m:
                best = max(best, int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return int(best * 1000) if best else None


def _art_size(data: bytes) -> int:
    """Width of a JPEG/PNG image (0 if unknown)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                return int.from_bytes(data[i + 7:i + 9], "big")
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return 0


def current_art(song: Song) -> tuple[str, bytes] | None:
    return song.art_file()


# ------------------------------------------------------------------ apply
def fetch(song: Song) -> dict:
    """Collect metadata from all sources (without writing)."""
    out: dict = {}
    sources = []
    for fn in (from_encore, from_itunes):
        need = [k for k in ("album", "year", "genre", "art_url") if not out.get(k)]
        if not need:
            break
        # iTunes art is 1000px; prefer it over Encore's when we have to ask anyway
        r = fn(song)
        if not r:
            continue
        sources.append(r["source"])
        for k in ("album", "year", "genre", "art_url"):
            if r.get(k) and (not out.get(k) or (k == "art_url" and r["source"] == "iTunes")):
                out[k] = r[k]
    out["sources"] = sources
    return out


def apply(song: Song, overwrite: bool = False, art: bool = True, progress=None) -> dict:
    """Fill in missing metadata for the song. Returns {field: new value}."""
    if progress:
        progress("looking up metadata")
    found = fetch(song)
    changes: dict[str, str] = {}
    for k in ("album", "year", "genre"):
        v = found.get(k)
        cur = strip_tags(song.meta.get(k, ""))
        if v and (overwrite or not cur or cur.lower() in ("unknown", "unknown album", "0")):
            if str(v) != cur:
                changes[k] = str(v)
    if not song.length_s:
        ms = audio_length_ms(song)
        if ms:
            changes["song_length"] = str(ms)
    art_bytes, art_name = None, None
    if art and found.get("art_url"):
        existing = song.art_file()
        weak = existing is None or _art_size(existing[1]) < 400
        if overwrite or weak:
            try:
                art_bytes = _get_bytes(found["art_url"])
                if _art_size(art_bytes) and existing and _art_size(art_bytes) <= _art_size(existing[1]) \
                        and not overwrite:
                    art_bytes = None
                art_name = "album.png" if art_bytes and art_bytes[:4] == b"\x89PNG" else "album.jpg"
            except Exception as e:
                log.debug("art download failed: %s", e)
                art_bytes = None
    if not changes and not art_bytes:
        return {}
    if song.is_sng:
        _write_sng(song, changes, art_name, art_bytes)
    else:
        _write_folder(song, changes, art_name, art_bytes)
    song.meta.update(changes)
    result = dict(changes)
    if art_bytes:
        result["album art"] = f"{_art_size(art_bytes) or '?'}px from {found.get('sources', ['?'])[-1]}"
    return result


def _write_folder(song: Song, changes: dict, art_name: str | None, art_bytes: bytes | None):
    from .ingame import _set_ini
    for k, v in changes.items():
        _set_ini(song, k, v)
    if art_bytes and art_name:
        for n in ART_NAMES:
            p = song.path / n
            if p.exists():
                b = p.with_name(p.name + ".chc-backup")
                if not b.exists():
                    shutil.copy2(p, b)
                p.unlink()
        tmp = song.path / ".chc-art.tmp"
        tmp.write_bytes(art_bytes)
        tmp.replace(song.path / art_name)


def _write_sng(song: Song, changes: dict, art_name: str | None, art_bytes: bytes | None):
    from .ingame import _backup
    src = open_sng(str(song.path))
    files: dict[str, bytes | str] = {n: n for n in src.files}
    meta = dict(src.metadata)
    meta.update(changes)
    if art_bytes and art_name:
        for n in list(files):
            if n.lower() in ART_NAMES:
                del files[n]
        files[art_name] = art_bytes
    _backup(song.path)
    write_sng(str(song.path), meta, files, src=src)
