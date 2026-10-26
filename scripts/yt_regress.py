#!/usr/bin/env python3
"""Live YouTube regression harness: runs the real in-game video path on fake library charts and
counts/classifies every YouTube request.

    PYTHONPATH=<checkout> python3 scripts/yt_regress.py [--json out.json] [--keep] [--dump-formats DIR]

The checkout on PYTHONPATH decides which version of chcompanion is measured (git worktree per tag).
Songs are built the way the library scanner builds them (Library.scan over real song.ini files).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

SONGS = [("Nickelback", "Rockstar"), ("The Killers", "Mr. Brightside"), ("Linkin Park", "Numb"),
         ("Foo Fighters", "Everlong"), ("AC/DC", "Back in Black")]

EV: list = []          # event log for the song being run
_cur = {"song": None}
_lock = threading.Lock()


def ev(kind, **kw):
    with _lock:
        EV.append({"t": round(time.time(), 2), "kind": kind, **kw})


def fmt_summary(fm):
    out = []
    for f in fm or []:
        out.append({"id": f.get("format_id"), "v": f.get("vcodec"), "a": f.get("acodec"), "w": f.get("width"),
                    "h": f.get("height"), "proto": f.get("protocol"), "ext": f.get("ext"),
                    "has_url": bool(f.get("url")), "note": f.get("format_note"), "client": f.get("__client")})
    return out


def make_library(root: Path, audio: bool = False):
    from chcompanion.library import Library
    for artist, name in SONGS:
        d = root / f"{artist} - {name}"
        d.mkdir(parents=True)
        (d / "song.ini").write_text(f"[song]\nname = {name}\nartist = {artist}\nalbum = Test\ncharter = harness\n"
                                    "song_length = 240000\ndiff_guitar = 3\n", "utf-8")
        if audio:      # a few seconds of tone so the alignment step (which downloads/uses the YouTube audio) runs too
            import subprocess
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                            "-c:a", "libvorbis", str(d / "song.ogg")], check=True)
        (d / "notes.chart").write_text("[Song]\n{\n  Resolution = 192\n}\n[SyncTrack]\n{\n  0 = TS 4\n  0 = B 120000\n}\n"
                                       "[Events]\n{\n}\n[ExpertSingle]\n{\n  0 = N 0 0\n  192 = N 1 0\n}\n", "utf-8")
    lib = Library([root])
    lib.scan()
    return lib


def install_probes(yt_mod):
    import yt_dlp
    from yt_dlp import YoutubeDL

    orig_extract = YoutubeDL.extract_info

    def extract_info(self, url, *a, **kw):
        dl = kw.get("download", a[0] if a else True)
        kind = "search" if str(url).startswith("ytsearch") else ("watch-dl" if dl else "watch-info")
        ev("extract_info", url=str(url)[:80], mode=kind, song=_cur["song"],
           clients=(self.params.get("extractor_args") or {}).get("youtube", {}).get("player_client"),
           ffmpeg_location=self.params.get("ffmpeg_location"))
        try:
            r = orig_extract(self, url, *a, **kw)
        except BaseException as e:
            ev("extract_error", cls=type(e).__name__, msg=str(e)[:300], mode=kind)
            raise
        if kind != "search" and isinstance(r, dict):
            ev("extract_ok", mode=kind, nformats=len(r.get("formats") or []),
               formats=fmt_summary(r.get("formats")), rd=[d.get("format_id") for d in r.get("requested_downloads") or []])
        return r
    YoutubeDL.extract_info = extract_info

    orig_pie = YoutubeDL.process_ie_result

    def pie(self, ie_result, download=True, extra_info=None):
        if isinstance(ie_result, dict) and ie_result.get("_type") in (None, "video") and ie_result.get("formats"):
            ev("process_ie_result", id=ie_result.get("id"), download=download)
        return orig_pie(self, ie_result, download, extra_info)
    YoutubeDL.process_ie_result = pie

    orig_dl = YoutubeDL.download

    def dl(self, url_list):
        ev("YoutubeDL.download", urls=list(url_list))
        return orig_dl(self, url_list)
    YoutubeDL.download = dl

    # every HTTP request yt-dlp itself makes (watch page, player API, googlevideo media, HLS...)
    orig_urlopen = YoutubeDL.urlopen

    def urlopen(self, req):
        u = getattr(req, "url", str(req))
        host = u.split("/")[2] if "//" in u else u
        if "youtube" in host or "googlevideo" in host or "ytimg" not in host and "google" in host:
            ev("http", host=host, path=u.split("?")[0][len(host) + 8:][:50], method=getattr(req, "method", None) or "GET")
        try:
            r = orig_urlopen(self, req)
        except BaseException as e:
            ev("http_error", host=host, cls=type(e).__name__, msg=str(e)[:160], status=getattr(getattr(e, "response", None), "status", None))
            raise
        return r
    YoutubeDL.urlopen = urlopen

    # plain urllib to youtube/googlevideo (our own code, e.g. sources.hosts)
    import urllib.request
    orig_open = urllib.request.OpenerDirector.open

    def uopen(self, fullurl, *a, **kw):
        u = fullurl if isinstance(fullurl, str) else fullurl.full_url
        if "youtube" in u or "googlevideo" in u:
            ev("urllib_open", url=u[:100])
        return orig_open(self, fullurl, *a, **kw)
    urllib.request.OpenerDirector.open = uopen

    # classification decisions in our code
    if hasattr(yt_mod, "degraded"):                       # <= 2.1.13: "reduced list" meant "YouTube is limiting"
        od = yt_mod.degraded

        def degraded(formats):
            r = od(formats)
            ev("classify", fn="degraded()", result=r, n=len(formats or []),
               video_only=sum(1 for f in formats or [] if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")),
               stack=traceback.extract_stack(limit=3)[0].name)
            return r
        yt_mod.degraded = degraded
    if hasattr(yt_mod, "list_state"):                     # >= 2.1.14: a description, never a limit decision
        ols = yt_mod.list_state

        def list_state(formats):
            r = ols(formats)
            ev("list_state", state=r, n=len(formats or []))
            return r
        yt_mod.list_state = list_state
    if hasattr(yt_mod, "classify_error"):
        oce = yt_mod.classify_error

        def classify_error(e, *a, **kw):
            r = oce(e, *a, **kw)
            ev("classify", fn="classify_error()", result=False, into=type(r).__name__, msg=str(e)[:100])
            return r
        yt_mod.classify_error = classify_error

    oib = yt_mod.is_bot_error

    def is_bot_error(e):
        r = oib(e)
        ev("classify", fn="is_bot_error()", result=r, cls=type(e).__name__, msg=str(e)[:200])
        return r
    yt_mod.is_bot_error = is_bot_error

    ot = yt_mod.RateGate.throttled

    def throttled(self):
        ev("classify", fn="GATE.throttled()", strikes_before=self.strikes,
           caller="/".join(f"{f.name}:{f.lineno}" for f in traceback.extract_stack(limit=4)[:-1]))
        return ot(self)
    yt_mod.RateGate.throttled = throttled

    for name in ("YouTubeLimited", "BotDetected", "NoHDVideo", "AgeRestricted", "VideoUnavailable", "ReducedFormats"):
        cls = getattr(yt_mod, name, None)
        if cls is None:
            continue
        oi = cls.__init__

        def init(self, *a, _oi=oi, _n=name, **kw):
            ev("raise", cls=_n, msg=" ".join(map(str, a))[:200],
               caller="/".join(f"{f.name}:{f.lineno}" for f in traceback.extract_stack(limit=4)[:-1]))
            _oi(self, *a, **kw)
        cls.__init__ = init


def run_song(lib, artist, name, tmp, ingame, yt_mod, pin=None):
    from chcompanion.store import Store
    song = lib.find(f"{artist} - {name}")
    assert song is not None, (artist, name)
    store = Store(Path(tmp) / f"store-{name}.json")
    if pin:
        store.update_song(song.key, video_id=pin, video_manual=True)
    yt = yt_mod.YouTube()
    g = yt_mod.GATE
    g.paused_until, g.strikes = 0.0, 0
    EV.clear()
    _cur["song"] = song.display
    steps = []
    t0 = time.time()
    outcome, err = "ok", None
    try:
        p = ingame.download_video(song, yt, store, progress=lambda m, *a: steps.append(m) if not steps or steps[-1] != m else None)
        sz = Path(p).stat().st_size
        outcome = f"ok ({Path(p).suffix}, {sz // 1024} KiB)"
    except BaseException as e:
        outcome, err = "FAIL " + type(e).__name__, str(e)[:300]
    evs = list(EV)
    n = lambda k, **m: sum(1 for e in evs if e["kind"] == k and all(e.get(a) == b for a, b in m.items()))
    res = {
        "song": song.display, "secs": round(time.time() - t0, 1), "outcome": outcome, "error": err,
        "search": n("extract_info", mode="search"),
        "watch_info": n("extract_info", mode="watch-info"), "watch_dl": n("extract_info", mode="watch-dl"),
        "extract_total": n("extract_info"),
        "http_youtube": sum(1 for e in evs if e["kind"] == "http" and "googlevideo" not in e["host"]),
        "http_googlevideo": sum(1 for e in evs if e["kind"] == "http" and "googlevideo" in e["host"]),
        "urllib_yt": n("urllib_open"),
        "extract_errors": [f"{e['cls']}: {e['msg']}" for e in evs if e["kind"] == "extract_error"],
        "http_errors": [f"{e['host']} {e['cls']} {e.get('status')}: {e['msg']}" for e in evs if e["kind"] == "http_error"],
        "limited_decisions": [f"{e['fn']} -> {e['result']}" + (f" (n={e['n']}, video_only={e['video_only']})" if "n" in e else "")
                              for e in evs if e["kind"] == "classify" and e.get("result") is True],
        "throttled_calls": [e["caller"] for e in evs if e["kind"] == "classify" and e["fn"] == "GATE.throttled()"],
        "raised": [f"{e['cls']}({e['msg'][:60]}) @ {e['caller']}" for e in evs if e["kind"] == "raise"],
        "steps": steps[:12], "events": evs,
    }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--dump-formats", help="write each watch format list (trimmed JSON) to this dir")
    ap.add_argument("--only", help="comma list of song indexes")
    ap.add_argument("--video-id", help="pin this YouTube id for every song (skips the search; to compare on a video known to work)")
    ap.add_argument("--audio", action="store_true", help="give each fake chart a tiny song.ogg so the alignment step runs")
    ap.add_argument("--song", action="append", help='extra song as "Artist|Title" (replaces the default five)')
    a = ap.parse_args()
    if a.song:
        SONGS[:] = [tuple(x.split("|", 1)) for x in a.song]
    os.environ.setdefault("CHC_TARGET_OS", "darwin")   # owner's platforms: H.264 stays as is, no VP8 transcode
    import chcompanion
    from chcompanion import ingame, youtube as yt_mod
    import yt_dlp
    print(f"chcompanion {chcompanion.__version__} from {Path(chcompanion.__file__).parent}; yt-dlp {yt_dlp.version.__version__}; "
          f"js_runtime={getattr(yt_mod, 'js_runtime', lambda: None)()}")
    install_probes(yt_mod)
    tmp = tempfile.mkdtemp(prefix="ytreg-")
    lib = make_library(Path(tmp) / "Songs", a.audio)
    idx = [int(i) for i in a.only.split(",")] if a.only else range(len(SONGS))
    results = []
    for i in idx:
        r = run_song(lib, *SONGS[i], tmp, ingame, yt_mod, a.video_id)
        results.append(r)
        print(f"\n== {r['song']}: {r['outcome']} in {r['secs']}s\n   search={r['search']} watch-info={r['watch_info']} watch-dl={r['watch_dl']} "
              f"yt-http={r['http_youtube']} googlevideo-http={r['http_googlevideo']} urllib-yt={r['urllib_yt']}")
        for k in ("error", "extract_errors", "http_errors", "limited_decisions", "throttled_calls", "raised"):
            if r[k]:
                print(f"   {k}: {r[k]}")
        if a.dump_formats:
            Path(a.dump_formats).mkdir(parents=True, exist_ok=True)
            for j, e in enumerate(x for x in r["events"] if x["kind"] == "extract_ok"):
                (Path(a.dump_formats) / f"{i}_{j}_{e['mode']}.json").write_text(json.dumps(e["formats"], indent=0))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))
    print("\nsummary: " + " | ".join(f"{r['song'].split(' - ')[1]}: {r['outcome']} req={r['extract_total']}" for r in results))


if __name__ == "__main__":
    main()
