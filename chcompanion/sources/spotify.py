"""Spotify playlist import without an API key: the public embed page
(open.spotify.com/embed/playlist/<id>) carries the track list (first 100 tracks)
in its __NEXT_DATA__ JSON (approach from Clone Hero Chart Manager)."""
from __future__ import annotations

import json
import re
import urllib.request

from ..i18n import _
from ..net import urlopen

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def playlist_id(url: str) -> str | None:
    m = re.search(r"(?:playlist[/:])([A-Za-z0-9]{22})", url or "")
    return m.group(1) if m else (url.strip() if re.fullmatch(r"[A-Za-z0-9]{22}", (url or "").strip()) else None)


def _find_entity(node):
    if isinstance(node, dict):
        if isinstance(node.get("trackList"), list):
            return node
        for v in node.values():
            r = _find_entity(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_entity(v)
            if r is not None:
                return r
    return None


def fetch_playlist(url: str) -> dict:
    pid = playlist_id(url)
    if not pid:
        raise ValueError(_("spotify.bad_link"))
    req = urllib.request.Request(f"https://open.spotify.com/embed/playlist/{pid}",
                                 headers={"User-Agent": UA, "Accept-Language": "en"})
    with urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(_("spotify.format_changed"))
    entity = _find_entity(json.loads(m.group(1))) or {}
    tracks = []
    for t in entity.get("trackList") or []:
        title, artist = (t.get("title") or "").strip(), (t.get("subtitle") or "").strip()
        if title and artist:
            tracks.append({"title": title, "artist": artist.split(",")[0].strip(),
                           "artists": artist, "durationMs": t.get("duration")})
    if not tracks:
        raise RuntimeError(_("spotify.empty"))
    return {"name": (entity.get("name") or _("spotify.default_name")).strip(), "tracks": tracks,
            "truncated": len(tracks) >= 100}
