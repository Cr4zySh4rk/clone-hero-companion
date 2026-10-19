"""Chorus Encore (enchor.us) client.

  POST https://api.enchor.us/search  {search, page (1-based), per_page (<= 250),
                                      instrument?, difficulty?, sort?: {type, direction}}
  Chart:     https://files.enchor.us/{md5}.sng
  Album art: https://files.enchor.us/{albumArtMd5}.jpg
(API usage as documented by Clone Hero Chart Manager.)
"""
from __future__ import annotations

import json
import time
import urllib.request

from ..net import urlopen

API = "https://api.enchor.us"
FILES = "https://files.enchor.us"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
INSTRUMENTS = ("guitar", "bass", "rhythm", "keys", "drums", "guitarghl", "bassghl", "vocals")
SORTS = {"name": "name", "artist": "artist", "album": "album", "year": "year", "length": "length",
         "charter": "charter", "newest": "modifiedTime"}


def _post(body: dict, retries: int = 2) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(f"{API}/search", data=data, headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            code = getattr(e, "code", None)
            if attempt >= retries or code not in (429, 503, None):
                raise
            time.sleep(0.4 * (attempt + 1))
    return {}


def _diffs(c: dict) -> dict:
    out = {}
    nd = c.get("notesData") or {}
    have = set(nd.get("instruments") or [])
    for inst in INSTRUMENTS:
        v = c.get(f"diff_{inst}")
        if isinstance(v, int) and v >= 0 and (not have or inst in have or inst == "vocals"):
            if inst == "vocals" and nd.get("hasVocals") is False:
                continue
            out[inst] = v
    return out


def normalize(c: dict) -> dict:
    md5 = c.get("md5")
    art = c.get("albumArtMd5")
    return {
        "source": "encore", "id": f"encore:{c.get('chartId') or md5}", "md5": md5,
        "name": c.get("name") or "", "artist": c.get("artist") or "", "album": c.get("album") or "",
        "genre": c.get("genre") or "", "year": str(c.get("year") or ""), "charter": c.get("charter") or "",
        "length": (c.get("song_length") or 0) / 1000 or None, "difficulties": _diffs(c),
        "hasVideo": bool(c.get("hasVideoBackground")), "hasVocals": bool((c.get("notesData") or {}).get("hasVocals")),
        "pack": c.get("packName"), "modified": c.get("modifiedTime"),
        "previewStart": (c.get("preview_start_time") or 0) / 1000 if (c.get("preview_start_time") or 0) > 0 else None,
        "artUrl": f"{FILES}/{art}.jpg" if art else None,
        "downloadUrl": f"{FILES}/{md5}.sng" if md5 else None,
        "pageUrl": f"https://www.enchor.us/chart/{md5}" if md5 else None,
        "format": "sng",
    }


def search(text: str, page: int = 1, per_page: int = 50, instrument: str | None = None,
           difficulty: str | None = None, sort: str | None = None, direction: str = "asc") -> dict:
    body: dict = {"search": text or "", "page": max(1, page), "per_page": max(1, min(per_page, 250))}
    if not (text or "").strip():
        if instrument:
            body["instrument"] = instrument
        if difficulty:
            body["difficulty"] = difficulty
    if sort in SORTS:
        body["sort"] = {"type": SORTS[sort], "direction": direction}
    data = _post(body)
    seen, songs = set(), []
    for c in data.get("data") or []:
        s = normalize(c)
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        if instrument and text and instrument not in s["difficulties"]:
            continue
        songs.append(s)
    return {"songs": songs, "total": data.get("found", len(songs)), "page": page}
