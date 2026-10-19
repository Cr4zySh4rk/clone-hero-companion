"""Synced lyrics: extracted from the chart (notes.mid / notes.chart) or, as a
fallback, fetched from LRCLIB (https://lrclib.net)."""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

from . import __version__
from .midi import TempoMap, parse_midi

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]*>")
VOCAL_TRACKS = ("PART VOCALS", "HARM1", "PART HARM1")


def _clean_syllable(raw: str) -> tuple[str, bool] | None:
    """Apply Rock Band / Clone Hero lyric conventions.
    Returns (text, joins_next) or None if the event is not a displayable syllable."""
    s = TAG_RE.sub("", raw).strip()
    if not s or s.startswith("["):
        return None
    if s == "+":
        return ("+", False)  # pitch slide continuation
    join = False
    for ch in "#^*%$/":
        s = s.replace(ch, "")
    s = s.replace("§", " ").replace("_", " ")
    if s.endswith("-"):
        s, join = s[:-1], True
    elif s.endswith("="):
        s, join = s[:-1] + "-", True
    s = s.replace("=", "-")
    if not s:
        return ("", join)
    return (s, join)


def _build_lines(events: list[tuple[float, float | None, str]],
                 phrases: list[tuple[float, float]]) -> list[dict]:
    """events: (start_s, end_s_or_None, raw_text) sorted by time."""
    syls: list[dict] = []
    for i, (t, e, raw) in enumerate(events):
        c = _clean_syllable(raw)
        if c is None:
            continue
        text, join = c
        if text == "+":
            if syls and e:
                syls[-1]["e"] = max(syls[-1]["e"] or syls[-1]["t"], e)
            continue
        syls.append({"t": round(t, 3), "e": e, "x": text, "j": join})
    # fill missing / overlapping ends
    for i, s in enumerate(syls):
        nxt = syls[i + 1]["t"] if i + 1 < len(syls) else None
        if s["e"] is None or s["e"] <= s["t"]:
            s["e"] = min(nxt, s["t"] + 0.8) if nxt is not None else s["t"] + 0.8
        if nxt is not None and s["e"] > nxt:
            s["e"] = nxt
        s["e"] = round(s["e"], 3)

    groups: list[list[dict]] = []
    if phrases:
        phrases = sorted(phrases)
        pi = 0
        cur: list[dict] = []
        cur_idx = -1
        for s in syls:
            # find phrase containing s
            while pi < len(phrases) and phrases[pi][1] <= s["t"] - 1e-6:
                pi += 1
            idx = pi if pi < len(phrases) and phrases[pi][0] <= s["t"] + 1e-6 else -1
            if idx != cur_idx or idx == -1 and cur and s["t"] - cur[-1]["e"] > 1.2:
                if cur:
                    groups.append(cur)
                cur = []
                cur_idx = idx
            cur.append(s)
        if cur:
            groups.append(cur)
    else:
        cur = []
        for s in syls:
            if cur and (s["t"] - cur[-1]["e"] > 1.2 or
                        (len(cur) >= 12 and not cur[-1]["j"])):
                groups.append(cur)
                cur = []
            cur.append(s)
        if cur:
            groups.append(cur)

    lines = []
    for g in groups:
        # very long groups -> split at word boundaries
        while g:
            chunk, rest = g, []
            if len(g) > 18:
                cut = 14
                while cut < len(g) - 1 and g[cut - 1]["j"]:
                    cut += 1
                chunk, rest = g[:cut], g[cut:]
            text = "".join(s["x"] + ("" if s["j"] else " ") for s in chunk).strip()
            if text:
                lines.append({"start": chunk[0]["t"], "end": chunk[-1]["e"],
                              "syls": chunk, "text": text})
            g = rest
    return lines


def from_midi(data: bytes, delay_s: float = 0.0) -> list[dict]:
    keep = {n.upper() for n in VOCAL_TRACKS}
    mf = parse_midi(data, keep_tracks=keep)
    track = mf.track(*VOCAL_TRACKS)
    if track is None:
        return []
    tm: TempoMap = mf.tempo_map()
    sung: dict[int, int] = {}
    for on, off, pitch in track.notes:
        if 36 <= pitch <= 84 and on not in sung:
            sung[on] = off
    events = []
    seen_ticks = set()
    for tick, mtype, text in sorted(track.texts, key=lambda x: (x[0], -x[1])):
        if text.strip().startswith("["):
            continue
        if tick in seen_ticks:  # both lyric (0x05) and text (0x01) at same tick
            continue
        seen_ticks.add(tick)
        end = sung.get(tick)
        events.append((tm.seconds(tick) + delay_s,
                       tm.seconds(end) + delay_s if end is not None else None, text))
    phrases = [(tm.seconds(on) + delay_s, tm.seconds(off) + delay_s)
               for on, off, p in track.notes if p in (105, 106)]
    return _build_lines(events, phrases)


_CHART_LINE = re.compile(r'^\s*(\d+)\s*=\s*(\w+)\s+(.*)$')


def from_chart(text: str, delay_s: float = 0.0) -> list[dict]:
    sections: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            sections[cur] = []
        elif cur and line not in ("{", "}"):
            sections[cur].append(line)
    res, offset = 192, 0.0
    for l in sections.get("Song", []):
        k, _, v = l.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        try:
            if k == "Resolution":
                res = int(float(v))
            elif k == "Offset":
                offset = float(v)
        except ValueError:
            pass
    tempos = []
    for l in sections.get("SyncTrack", []):
        m = _CHART_LINE.match(l)
        if m and m.group(2) == "B":
            bpm = int(m.group(3).split()[0]) / 1000.0
            if bpm > 0:
                tempos.append((int(m.group(1)), int(60e6 / bpm)))
    tm = TempoMap(res, tempos)
    events, phrases, pstart = [], [], None
    for l in sections.get("Events", []):
        m = _CHART_LINE.match(l)
        if not m or m.group(2) != "E":
            continue
        tick = int(m.group(1))
        val = m.group(3).strip().strip('"')
        t = tm.seconds(tick) + delay_s + offset
        if val.startswith("lyric "):
            events.append((t, None, val[6:]))
        elif val == "phrase_start":
            if pstart is not None:
                phrases.append((pstart, t))
            pstart = t
        elif val == "phrase_end" and pstart is not None:
            phrases.append((pstart, t))
            pstart = None
    if pstart is not None:
        phrases.append((pstart, 1e9))
    events.sort(key=lambda e: e[0])
    return _build_lines(events, phrases)


# ---------------------------------------------------------------- LRCLIB

_LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(lrc: str) -> list[dict]:
    stamped: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        stamps = _LRC_TS.findall(raw)
        text = _LRC_TS.sub("", raw).strip()
        for mm, ss in stamps:
            stamped.append((int(mm) * 60 + float(ss), text))
    stamped.sort(key=lambda x: x[0])
    lines = []
    for i, (t, text) in enumerate(stamped):
        if not text:
            continue
        nxt = stamped[i + 1][0] if i + 1 < len(stamped) else t + 5
        words = text.split(" ")
        # sing the line over ~85% of the gap (max ~0.6 s per word)
        span = min(nxt - t, max(0.6, 0.6 * len(words))) * 0.9
        total = sum(len(w) + 1 for w in words)
        syls, cursor = [], t
        for w in words:
            d = span * (len(w) + 1) / total
            syls.append({"t": round(cursor, 3), "e": round(cursor + d, 3), "x": w, "j": False})
            cursor += d
        lines.append({"start": round(t, 3), "end": round(t + span, 3), "syls": syls,
                      "text": text, "approx": True})
    return lines


def _http_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"CloneHeroCompanion/{__version__} (https://github.com/Cr4zySh4rk/clone-hero-companion)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def clean_title(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = re.sub(r"\((?:WaveGroup|[^)]*(?:Rock Band|Guitar Hero|Rip|Cover|Chart)[^)]*)\)", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def fetch_lrclib(artist: str, title: str, duration_s: float | None) -> list[dict]:
    artist, title = clean_title(artist), clean_title(title)
    base = "https://lrclib.net/api"
    attempts = []
    q = {"artist_name": artist, "track_name": title}
    if duration_s:
        attempts.append(base + "/get?" + urllib.parse.urlencode({**q, "duration": int(round(duration_s))}))
    attempts.append(base + "/get?" + urllib.parse.urlencode(q))
    plain_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    for url in attempts:
        try:
            data = _http_json(url)
            if data and data.get("syncedLyrics"):
                return parse_lrc(data["syncedLyrics"])
        except Exception as e:  # 404 etc.
            log.debug("lrclib %s: %s", url, e)
    try:
        results = _http_json(base + "/search?" + urllib.parse.urlencode(
            {"artist_name": artist, "track_name": plain_title or title}))
        best, best_score = None, 1e9
        for r in results or []:
            if not r.get("syncedLyrics"):
                continue
            score = abs((r.get("duration") or 0) - (duration_s or 0)) if duration_s else 0
            if score < best_score:
                best, best_score = r, score
        if best and (not duration_s or best_score < 20):
            return parse_lrc(best["syncedLyrics"])
    except Exception as e:
        log.debug("lrclib search: %s", e)
    return []
