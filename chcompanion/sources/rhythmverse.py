"""RhythmVerse (rhythmverse.co) client — unofficial API used by its website.

  POST https://rhythmverse.co/api/<system>/songfiles/search/live  (text, data_type=full, records, page)
  POST https://rhythmverse.co/api/<system>/songfiles/list         (browse)
system: ch = Clone Hero (default), ps = Phase Shift, rb3 = Rock Band 3 (needs conversion)
(API usage as documented by Clone Hero Chart Manager.)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from ..net import urlopen

BASE = "https://rhythmverse.co"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CONVERT_FORMATS = ("rb3con", "rb3", "rb2", "rb3xbox", "rb3wii", "rb3ps3", "ps3", "con")
SORTS = {"name": "title", "artist": "artist", "newest": "update_date", "downloads": "downloads",
         "length": "song_length", "year": "year"}


def _abs(url):
    if not url or str(url).lower() == "none":
        return None
    url = str(url)
    return url if url.startswith("http") else BASE + (url if url.startswith("/") else "/" + url)


def _num(v):
    try:
        n = float(v)
        return n if n == n else None
    except (TypeError, ValueError):
        return None


LEVEL_CODES = {"e": "easy", "m": "medium", "h": "hard", "x": "expert"}
LEVELS = ("easy", "medium", "hard", "expert")
INST_NAMES = {"guitar": "guitar", "bass": "bass", "drums": "drums", "vocals": "vocals", "keys": "keys",
              "rhythm": "rhythm", "guitar_coop": "guitar_coop"}


def _levels(f: dict) -> dict:
    """{"guitar": ["easy", "expert"]} from RhythmVerse's per-file "difficulties"; {} when unknown."""
    v = f.get("difficulties")
    out = {}
    if isinstance(v, dict):
        for inst, lv in v.items():
            if isinstance(lv, dict) and lv:
                have = [LEVEL_CODES[k] for k in ("e", "m", "h", "x") if lv.get(k)]
                if have:
                    out[INST_NAMES.get(inst, inst)] = have
    return out


def _matches(s: dict, instrument: str | None, difficulty: str | None) -> bool:
    """Strict check against the chart's real levels (RhythmVerse's own difficulty filter
    doesn't narrow results down). Charts that don't say which levels they have are left out."""
    lv = s.get("levels") or {}
    if difficulty:
        if instrument:
            return difficulty in lv.get(instrument, [])
        return any(difficulty in v for v in lv.values())
    if instrument and lv:
        return instrument in lv
    return True


def normalize(song: dict) -> dict:
    d, f = song.get("data") or {}, song.get("file") or {}
    diffs = {}
    for inst in ("guitar", "bass", "drums", "vocals", "keys", "rhythm"):
        v = _num(f.get(f"diff_{inst}") or d.get(f"diff_{inst}"))
        if v and v > 0:
            diffs[inst] = int(min(v, 6))
    fmt = (f.get("gameformat") or "").lower()
    dl = _abs(f.get("download_url"))
    official = not dl or "marketplace.xbox" in (dl or "")
    charter = f.get("charter")
    if not charter or charter == "None":
        a = f.get("author")
        charter = a.get("name") if isinstance(a, dict) else None
    return {
        "source": "rhythmverse", "id": f"rv:{f.get('file_id') or d.get('song_id')}",
        "name": str(d.get("title") or f.get("file_title") or ""), "artist": str(d.get("artist") or ""),
        "album": str(d.get("album") or ""), "genre": str(d.get("genre") or ""), "year": str(d.get("year") or ""),
        "charter": charter or "", "length": _num(d.get("song_length")), "difficulties": diffs, "levels": _levels(f),
        "artUrl": _abs(d.get("album_art") or f.get("album_art")), "downloadUrl": None if official else dl,
        "pageUrl": f.get("download_page_url_full") or _abs(f.get("download_page_url")),
        "format": fmt, "needsConversion": fmt in CONVERT_FORMATS, "size": _num(f.get("size")),
        "downloads": _num(f.get("downloads")), "fileName": f.get("file_name"),
    }


def _fetch(text: str, page: int, records: int, system: str, instrument: str | None,
           sort: str | None, direction: str) -> tuple[list, int]:
    endpoint = "search/live" if (text or "").strip() else "list"
    body = [("data_type", "full"), ("records", str(records)), ("page", str(max(1, page)))]
    if (text or "").strip():
        body.append(("text", text))
    if instrument:
        body.append(("instrument[]", instrument))
    if sort in SORTS:
        body += [("sort[0][sort_by]", SORTS[sort]), ("sort[0][sort_order]", "ASC" if direction == "asc" else "DESC")]
    req = urllib.request.Request(f"{BASE}/api/{system}/songfiles/{endpoint}",
                                 data=urllib.parse.urlencode(body).encode(), headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "User-Agent": UA})
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("status") != "success":
        raise RuntimeError("RhythmVerse returned an error")
    raw = (data.get("data") or {}).get("songs") or []
    songs = [s for s in (normalize(x) for x in raw) if "ps3" not in s["format"]]
    total = ((data.get("data") or {}).get("records") or {}).get("total_filtered", len(songs))
    return songs, int(total or 0), len(raw)


_cache: dict = {}
_cache_lock = threading.Lock()
BATCH = 250          # records per RhythmVerse request when we filter ourselves
MAX_BATCHES = 8      # look through at most 2000 charts for one filtered search


def search(text: str, page: int = 1, per_page: int = 50, system: str = "ch",
           instrument: str | None = None, difficulty: str | None = None,
           sort: str | None = None, direction: str = "asc") -> dict:
    page = max(1, page)
    if not difficulty:
        songs, total, _n = _fetch(text, page, per_page, system, instrument, sort, direction)
        songs = [s for s in songs if _matches(s, instrument, None)]
        return {"songs": songs, "total": total, "page": page}
    # Difficulty filter: RhythmVerse ignores it, so collect matching charts batch by batch
    key = (system, (text or "").strip().lower(), instrument, difficulty, sort, direction)
    with _cache_lock:
        st = _cache.get(key)
        if not st or st["t"] < time.time() - 600:
            st = _cache[key] = {"t": time.time(), "items": [], "next": 1, "done": False, "seen": set()}
    need = page * per_page
    while len(st["items"]) < need and not st["done"]:
        songs, total, n = _fetch(text, st["next"], BATCH, system, instrument, sort, direction)
        for s in songs:
            if s["id"] not in st["seen"] and _matches(s, instrument, difficulty):
                st["seen"].add(s["id"])
                st["items"].append(s)
        st["next"] += 1
        if n < BATCH or (st["next"] - 1) * BATCH >= total or st["next"] > MAX_BATCHES:
            st["done"] = True
    items = st["items"][(page - 1) * per_page: page * per_page]
    total = len(st["items"]) if st["done"] else len(st["items"]) + per_page   # more pages may follow
    return {"songs": items, "total": total, "page": page}
