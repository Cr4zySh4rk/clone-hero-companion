"""Make Clone Hero itself show the music video and the lyrics.

* Video: Clone Hero plays video.mp4 / video.webm from a song folder (or inside a
  .sng package) behind the highway when "Song Videos" is enabled.
* Lyrics: Clone Hero shows lyrics from the chart's vocals (PART VOCALS in notes.mid,
  lyric events in notes.chart). For charts without vocals we write the LRCLIB
  synced lyrics into the chart.

Every file we change is backed up first (<name>.chc-backup) and `restore()` puts
everything back.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from . import cancel as _cancel
from .process import run as run_process
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import lyrics as lyr
from .library import Song, VIDEO_NAMES
from .midi import TempoMap, build_track, chunk_track_name, encode_varlen, parse_midi, split_chunks, assemble
from .paths import cache_dir, config_dir
from .sng import open_sng, write_sng
from .store import Store
from .i18n import _, join
from .youtube import MIN_P as MIN_HD, MIN_SCORE, YouTube, is_strong_official

log = logging.getLogger(__name__)
BACKUP = ".chc-backup"
MARKER = "[chc_lyrics]"
_song_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


class InGameError(Exception):
    pass


def _lock_for(key: str) -> threading.Lock:
    with _locks_lock:
        return _song_locks.setdefault(key, threading.Lock())


# --------------------------------------------------------------- lyrics → chart
def _word_events(lines: list[dict], to_tick, offset_s: float):
    """Yield (tick, order, raw MIDI event bytes) for phrases, lyrics and sung notes."""
    ev = []
    for line in lines:
        syls = line["syls"]
        if not syls:
            continue
        p_on = max(0, to_tick(syls[0]["t"] + offset_s) - 2)   # phrase must start before its first word
        p_off = max(p_on + 1, to_tick(syls[-1]["e"] + offset_s) + 1)
        ev.append((p_on, 0, bytes([0x90, 105, 100])))
        ev.append((p_off, 9, bytes([0x80, 105, 0])))
        for i, s in enumerate(syls):
            on = to_tick(s["t"] + offset_s)
            off = max(on + 1, to_tick(s["e"] + offset_s) - 1)
            text = s["x"] + ("-" if s.get("j") else "")
            tb = text.encode("utf-8")
            ev.append((on, 1, b"\xff\x05" + encode_varlen(len(tb)) + tb))
            ev.append((on, 2, bytes([0x90, 60, 100])))
            ev.append((off, 8, bytes([0x80, 60, 0])))
    return ev


def midi_with_lyrics(original: bytes, lines: list[dict], delay_s: float, offset_s: float) -> bytes:
    mf = parse_midi(original, keep_tracks=set())
    tm: TempoMap = mf.tempo_map()
    header, chunks = split_chunks(original)
    names = [chunk_track_name(c).strip().upper() for c in chunks]
    if "PART VOCALS" in names:
        i = names.index("PART VOCALS")
        existing = parse_midi(header + chunks[i], keep_tracks={"PART VOCALS"}).tracks[0]
        if any(not t.strip().startswith("[") for _tick, _ty, t in existing.texts):
            raise InGameError(_("ingame.has_lyrics"))
        del chunks[i]  # a vocals track without any lyrics: replace it
    marker = MARKER.encode()
    events = [(0, -1, b"\xff\x01" + encode_varlen(len(marker)) + marker)]
    events += _word_events(lines, lambda t: tm.ticks(t - delay_s), offset_s)
    chunks.append(build_track("PART VOCALS", events))
    return assemble(header, chunks)


def chart_with_lyrics(text: str, lines: list[dict], delay_s: float, offset_s: float) -> str:
    sections = lyr.parse_chart_sections(text)
    _res, chart_off, tm = lyr.chart_timing(sections)
    if any("lyric " in l for l in sections.get("Events", [])):
        raise InGameError(_("ingame.has_lyrics"))
    new = []
    for line in lines:
        if not line["syls"]:
            continue
        to_tick = lambda t: tm.ticks(t + offset_s - delay_s - chart_off)  # noqa: E731
        new.append((to_tick(line["syls"][0]["t"]), 0, 'E "phrase_start"'))
        for s in line["syls"]:
            word = (s["x"] + ("-" if s.get("j") else "")).replace('"', "'")
            new.append((to_tick(s["t"]), 1, f'E "lyric {word}"'))
        new.append((to_tick(line["syls"][-1]["e"]), 2, 'E "phrase_end"'))
    # merge into [Events]
    nl = "\r\n" if "\r\n" in text else "\n"
    out_lines = text.split(nl)
    try:
        start = next(i for i, l in enumerate(out_lines) if l.strip() == "[Events]")
    except StopIteration:
        out_lines += ["[Events]", "{", "}"]
        start = len(out_lines) - 3
    open_i = start + 1
    close_i = next(i for i in range(open_i + 1, len(out_lines)) if out_lines[i].strip() == "}")
    existing = []
    for l in out_lines[open_i + 1:close_i]:
        m = re.match(r"\s*(\d+)\s*=\s*(.*)$", l)
        if m:
            existing.append((int(m.group(1)), 5, m.group(2).strip()))
    merged = sorted(existing + new, key=lambda e: (e[0], e[1]))
    body = [f"  {t} = {v}" for t, _o, v in merged]
    return nl.join(out_lines[:open_i + 1] + body + out_lines[close_i:])


def lyrics_for_game(song: Song) -> list[dict]:
    """LRCLIB lyrics for songs whose chart has none ([] if the chart has lyrics)."""
    if song.chart_lyrics():
        return []
    return lyr.fetch_lrclib(song.artist, song.name, song.length_s)


# --------------------------------------------------------------- video download
def _ffmpeg() -> str | None:
    from .align import ffmpeg_path
    return ffmpeg_path()


# --------------------------------------------------------------- VP8 / WebM encoding
# Every video we write into a song is VP8 in WebM: Clone Hero plays that on Windows, macOS and Linux, so a
# library can be copied between systems. YouTube serves no VP8 at HD, so nearly every video is re-encoded;
# the settings below are the fastest ones whose quality is fine behind a highway (see the 2.3.1 notes).
from .hwvideo import MAX_HEIGHT, encode_slots  # noqa: E402
from . import hwvideo  # noqa: E402

WEB_EXTS = (".webm", ".ogv", ".vp8")


def _probe(p: Path) -> tuple[tuple[int, int], int]:
    """((width, height), overall kb/s) of a video file, zeros if unknown."""
    ff = _ffmpeg()
    if not ff:
        return (0, 0), 0
    try:
        err = run_process([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return (0, 0), 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    b = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
    return ((int(m.group(1)), int(m.group(2))) if m else (0, 0)), (int(b.group(1)) if b else 0)


def looks_static(p: Path) -> bool:
    """Album-art uploads: square frame and/or almost no video bitrate."""
    (w, h), kbps = _probe(p)
    if w and h and abs(w - h) <= 2:
        return True
    return bool(kbps) and kbps < 90


_ENCODES = threading.Semaphore(encode_slots())     # encodes are CPU heavy and multithreaded
_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time", "dup_frames",
                  "drop_frames", "speed", "progress"}


def _encode(ff: str, src: Path, dst: Path, args: list[str], progress=None, total: float = 0.0,
            pre: list[str] = ()) -> None:
    """Run ffmpeg, reporting the percentage from its -progress output (needs `total` seconds).
    pre: input options (hardware decoding) that go before -i."""
    from .process import popen, terminate, kill_on_cancel
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-nostats", *pre, "-i", str(src), *args,
           "-progress", "pipe:1", str(dst)]
    _cancel.check()
    if not _cancel.acquire(_ENCODES, blocking=False):
        if progress:
            progress(_("ingame.waiting_encoder"))
        _cancel.acquire(_ENCODES)
    proc = None
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        last, errs = -1, []
        with kill_on_cancel(proc):          # a cancel stops ffmpeg within a fraction of a second
            for line in proc.stdout:
                key, sep, val = line.strip().partition("=")
                if sep and key in _PROGRESS_KEYS:
                    if key == "out_time_us" and total and progress and val.isdigit():
                        pct = min(99, int(int(val) / 1e6 / total * 100))
                        if pct != last:
                            last = pct
                            progress(_("ingame.encoding_pct", pct=pct))
                elif line.strip():
                    errs.append(line.strip())
            rc = proc.wait()
        _cancel.check()
    except BaseException:
        if proc is not None:
            terminate(proc, 3.0)
        dst.unlink(missing_ok=True)
        raise
    finally:
        _ENCODES.release()
    if rc != 0:
        dst.unlink(missing_ok=True)
        raise InGameError(_("ingame.ffmpeg_failed", detail=" ".join(errs)[-300:]))


def media_info(p: Path) -> dict:
    """{'w', 'h', 'kbps', 'secs'} of a media file via ffmpeg -i (zeros if unknown)."""
    ff = _ffmpeg()
    out = {"w": 0, "h": 0, "kbps": 0, "secs": 0.0}
    if not ff:
        return out
    try:
        err = run_process([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return out
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        out["w"], out["h"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
    if m:
        out["kbps"] = int(m.group(1))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        out["secs"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return out


def encode_vp8(src: Path, dst: Path, pad: float = 0.0, progress=None) -> None:
    """The one place a video becomes VP8 WebM (padding with black included, so a video is never encoded twice).
    Tries the hardware paths hwvideo found (GPU decode/scale, rarely a GPU VP8 encode), each verified or
    falling back, and always ends with the plain CPU path."""
    ff = _ffmpeg()
    if not ff:
        raise InGameError(_("ingame.ffmpeg_needed"))
    info = media_info(src)
    total = (info["secs"] + pad) if info["secs"] else 0.0
    expect = 0
    for path in hwvideo.plan():
        pre, post = hwvideo.build(path, info["w"], info["h"], pad)
        try:
            _encode(ff, src, dst, post, progress, total, pre)
            if path.encode:          # a GPU-made VP8 file is only trusted when it checks out
                expect = expect or hwvideo.count_frames(ff, src)
                if not hwvideo.verify_output(ff, dst, expect):
                    raise InGameError(_("ingame.ffmpeg_failed", detail="GPU output failed verification"))
            return
        except InGameError as e:
            if path.key == "cpu":
                raise
            dst.unlink(missing_ok=True)
            hwvideo.note_failed(path, str(e))


PAD_MARK = ".padded"


def padded_seconds(vpath: Path) -> float:
    """Seconds of black download_video already put in front of this video (0 if none)."""
    try:
        return float((vpath.parent / PAD_MARK).read_text())
    except (OSError, ValueError):
        return 0.0


STD_MP4_BRANDS = {b"isom", b"mp42", b"mp41", b"avc1", b"M4V ", b"qt  ", b"iso2"}


def mp4_needs_remux(head: bytes | None) -> bool:
    """YouTube DASH downloads are fragmented MP4s ('dash' brand). AVFoundation (macOS:
    Clone Hero's video player and the app's preview) won't play them; a plain MP4 works
    everywhere."""
    return bool(head) and len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] not in STD_MP4_BRANDS


def _remux_mp4(src: Path, dst: Path) -> None:
    """Lossless rewrite into a standard, fast-start MP4 (video stream copied as-is)."""
    ff = _ffmpeg()
    if not ff:
        raise InGameError(_("ingame.no_ffmpeg"))
    r = run_process([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-an", "-c:v", "copy",
                        "-movflags", "+faststart", "-f", "mp4", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        raise InGameError(_("ingame.ffmpeg_remux_failed", detail=r.stderr.strip()[-300:]))


def repair_fragmented_videos(songs, progress=None) -> int:
    """Rewrite DASH-fragmented video.mp4 files (in folders and inside .sng) as standard MP4."""
    from .sng import open_sng, write_sng
    fixed = 0
    for song in songs:
        try:
            if song.is_sng:
                src = open_sng(str(song.path))
                name = next((n for n in src.files if n.lower() in ("video.mp4", "video.m4v")), None)
                if not name or not mp4_needs_remux(src.head(name)):
                    continue
                tmpdir = Path(tempfile.mkdtemp(prefix="chc-remux-", dir=cache_dir()))
                try:
                    a, b = tmpdir / "in.mp4", tmpdir / "out.mp4"
                    a.write_bytes(src.read(name))
                    _remux_mp4(a, b)
                    files: dict[str, bytes | str] = {n: n for n in src.files if n != name}
                    files["video.mp4"] = b.read_bytes()
                    write_sng(str(song.path), dict(src.metadata), files, src=src)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            else:
                done = False
                for n in ("video.mp4", "video.m4v"):
                    p = song.path / n
                    if not p.is_file():
                        continue
                    with open(p, "rb") as f:
                        head = f.read(16)
                    if not mp4_needs_remux(head):
                        continue
                    tmp = song.path / ".chc-remux.mp4"
                    _remux_mp4(p, tmp)
                    os.replace(tmp, p)
                    done = True
                if not done:
                    continue
            fixed += 1
            log.info("Rewrote the video of %s as a standard MP4 (plays on macOS now)", song.display)
            if progress:
                progress(fixed)
        except Exception as e:
            log.warning("Could not rewrite the video of %s: %s", getattr(song, "display", song), e)
    return fixed


# What we write: VP8 in WebM, which Clone Hero plays on Windows, macOS and Linux (an .mp4 with H.264 plays on
# Windows/macOS only; on Linux just VP8 .webm and .ogv do). Videos already in the library may be MP4.
def playable_in_game(codec: str) -> bool:
    """Plays in Clone Hero on every OS."""
    return not codec or codec in ("vp8", "theora")      # unknown: don't touch it


def video_names(song: Song) -> list[str]:
    return [n for n in song.list_files() if n.lower() in VIDEO_NAMES]


def needs_vp8(song: Song) -> bool:
    """The song's video is in a container that does not play everywhere (MP4, MOV...) and there is no WebM.
    Judged by file name only, so a whole library can be checked quickly after a scan."""
    names = [n.lower() for n in video_names(song)]
    return bool(names) and not any(n.endswith(WEB_EXTS) for n in names)


def unplayable_songs(songs) -> list[Song]:
    out = []
    for s in songs:
        try:
            if needs_vp8(s):
                out.append(s)
        except Exception:
            continue
    return out


def probe_codec(p: Path) -> str:
    """Video codec name as ffmpeg reports it (h264, hevc, vp8, vp9, av1...), '' if unknown."""
    ff = _ffmpeg()
    if not ff:
        return ""
    try:
        err = run_process([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return ""
    m = re.search(r"Video:\s*([A-Za-z0-9_]+)", err)
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------- video cache
# Videos fetched for the Now Playing screen while a song is being played. The song's
# own files can't be changed while the game plays it; the job afterwards reuses these.
def video_cache_dir() -> Path:
    d = cache_dir() / "video-cache"          # OS temp dir, wiped at every start (paths.wipe_cache)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_video(video_id: str | None) -> Path | None:
    if not video_id:
        return None
    for p in video_cache_dir().glob(f"{video_id}.*"):
        if p.suffix.lower() in (".mp4", ".webm") and p.stat().st_size > 0:
            return p
    return None


def prune_video_cache(max_bytes: int = 3_000_000_000, max_age_s: float = 14 * 86400) -> None:
    try:
        files = sorted(video_cache_dir().glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        total, now = 0, time.time()
        for p in files:
            st = p.stat()
            total += st.st_size
            if total > max_bytes or now - st.st_mtime > max_age_s or p.suffix == ".part":
                p.unlink(missing_ok=True)
    except OSError:
        pass


def fetch_preview_video(song: Song, yt: YouTube, store: Store, progress=None) -> Path:
    """Download (and line up) the song's HD video into the cache for Now Playing."""
    with _lock_for(song.key):
        per = store.song(song.key)
        hit = cached_video(per.get("video_id")) if per.get("video_id") else None
        if hit:
            return hit
        vpath = download_video(song, yt, store, progress, convert=False)
        try:
            vid = store.song(song.key).get("video_id")
            dst = video_cache_dir() / f"{vid}{vpath.suffix.lower()}"
            tmp = dst.with_suffix(".part")
            try:
                shutil.copyfile(vpath, tmp)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
            return dst
        finally:
            shutil.rmtree(vpath.parent, ignore_errors=True)


def video_res(p: Path) -> int:
    """Resolution class (e.g. 720) of a video file, 0 if unknown."""
    from .youtube import res_class
    (w, h), _dur = _probe(p)
    return res_class(w, h)


def base_key(song: Song) -> str:
    """Same recording, any chart: artist and title without "(WaveGroup)", "(Co-op)" and the like."""
    from .library import norm_key
    return "~base " + norm_key(f"{lyr.clean_title(song.artist)} - {lyr.clean_title(song.name)}")


FAIL_MEMORY_S = 14 * 86400      # how long a real "no HD video" result is trusted


def _remember_failure(store: Store, song: Song, bkey: str, why: str) -> None:
    now = time.time()
    store.update_song(song.key, video_failed=True, video_failed_at=now, video_failed_reason=why)
    store.update_song(bkey, failed=why, failed_at=now)


def download_video(song: Song, yt: YouTube, store: Store, progress=None, force: bool = False,
                   defer: bool = False, convert: bool = True) -> Path:
    """See _download_video. A cancel removes the work folder (partial downloads, half-written WebM)."""
    made: list[Path] = []
    try:
        return _download_video(song, yt, store, progress, force, defer, convert, made)
    except _cancel.Cancelled:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)
        raise


def _download_video(song: Song, yt: YouTube, store: Store, progress, force: bool, defer: bool,
                    convert: bool, made: list) -> Path:
    """Download the best matching HD (>= 640p) music video for the song, lined up and as VP8 WebM (plays in
    Clone Hero on every OS). A video that is already VP8 is used as it is; a lead-in of black for a negative
    offset is folded into the one encode (see padded_seconds). Raises InGameError if there is none.
    defer: while a YouTube cool-down runs, raise BotDetected at once instead of waiting it out.
    convert=False: return the original download (for the Now Playing preview), nothing is encoded."""
    from .youtube import GATE, BotDetected, NoHDVideo, StillImage, VideoProblem

    def gate_wait():
        if defer and GATE.remaining() > 0:
            raise BotDetected((GATE.last_limit or {}).get("message") or "YouTube requests are paused")
        GATE.wait_cooldown(progress)        # a cool-down or the hourly budget: wait it out, visibly
    info = store.song(song.key)
    bkey = base_key(song)
    base = store.song(bkey)
    chosen = info.get("video_id") if info.get("video_manual") else None
    cmap: dict = {}
    sd_min = getattr(yt, "sd_min_p", 0)                 # 0: no SD fallback
    sd_min = sd_min if isinstance(sd_min, int) else 0
    best_sd = None                                      # (score, res, candidate id): best SD seen so far
    known_sd = bool(info.get("video_sd") or base.get("video_sd"))
    known = None if force else (info.get("video_id") or base.get("video_id"))
    if not chosen and not known and not force:
        failed_at = max(float(info.get("video_failed_at") or 0), float(base.get("failed_at") or 0))
        if (info.get("video_failed") or base.get("failed")) and time.time() - failed_at < FAIL_MEMORY_S:
            why = base.get("failed") or info.get("video_failed_reason") or _("ingame.reason_earlier")
            raise InGameError(_("ingame.no_hd_cached", why=why))
    if chosen:
        order = [chosen]
    else:
        gate_wait()
        if progress:
            progress(_("yt.searching"))
        cands = yt.search(song)
        store.update_song(song.key, candidates=cands[:8])
        cmap = {c["id"]: c for c in cands}
        order = [c["id"] for c in cands if c["score"] >= MIN_SCORE][:4]
        if known:                      # what this song (or another chart of it) got before
            order = [known] + [x for x in order if x != known][:3]
        if not order:
            _remember_failure(store, song, bkey, _("ingame.reason_nomatch"))
            raise InGameError(_("ingame.no_match"))
    tmpdir = Path(tempfile.mkdtemp(prefix="chc-", dir=cache_dir()))
    made.append(tmpdir)

    def prog(done, total):
        if progress and total:
            progress(_("ingame.downloading_video", pct=done * 100 // total))
    path, vid, fmt, reasons = None, None, None, []
    transient_seen = False
    from_cache = False
    fetched_audio = None
    need_align = not per_manual_offset(store, song)
    for cand in order:
        fetched_audio = None            # only ever line up a video with its own audio
        hit = cached_video(cand)
        if hit:                          # fetched earlier for Now Playing: no download needed
            p = tmpdir / ("video" + hit.suffix.lower())
            shutil.copyfile(hit, p)
            codec = probe_codec(p)
            path, vid, from_cache = p, cand, True
            fmt = {"res_class": video_res(p) or store.song(song.key).get("video_res"),
                   "vcodec": "avc1" if codec == "h264" else codec}
            break
        gate_wait()
        entry = cmap.get(cand) or {}
        # a strong official match (or the SD video this song already got from it) takes its best SD at once
        strong = bool(sd_min and (chosen == cand or (known_sd and cand == known)
                                  or (entry and is_strong_official(entry, song, entry.get("score")))))
        try:
            if hasattr(yt, "fetch"):
                # one lookup: the video stream, plus the audio stream when the video gets lined up
                kw = {"take_sd": True} if strong else {}
                p, f, fetched_audio = yt.fetch(cand, str(tmpdir / "video"), with_audio=need_align, progress=prog, **kw)
            else:
                p, f = yt.download(cand, str(tmpdir / "video"), progress=prog)
        except NoHDVideo as e:
            sd_p = int(getattr(e, "sd_p", 0) or 0)
            if sd_min and sd_p >= sd_min:      # no HD, but an SD version exists: remember the best one
                key = (float(entry.get("score", MIN_SCORE)), sd_p)
                if best_sd is None or key > best_sd[:2]:
                    best_sd = (*key, cand)
            log.info("Skipping %s for %s: not available in %dp or better (best SD: %s)", cand, song.display,
                     MIN_HD, sd_p or "none")
            reasons.append(_("ingame.reason_low", p=MIN_HD))
            continue
        except StillImage:
            log.info("Skipping %s for %s: the stream list says it's a still image (nothing downloaded)", cand, song.display)
            reasons.append(_("ingame.reason_still"))
            continue
        except BotDetected:
            # YouTube really is limiting us: every other candidate would be refused too. Stop, let the
            # job wait out the shared cool-down and retry, instead of spending more lookups on this chart.
            log.info("YouTube is limiting requests (%s)", song.display)
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        except VideoProblem as e:
            # This video can't be had (age-restricted, unavailable, no video streams offered). That is
            # not YouTube limiting us: say what it is, try the next candidate, no cool-down.
            log.info("Skipping %s for %s: %s (%s)", cand, song.display, e.label, e.detail)
            reasons.append(_("ingame.reason_label", label=e.label, detail=e.detail) if e.detail else e.label)
            transient_seen = True
            continue
        except Exception as e:
            log.info("Skipping %s for %s: download failed (%s)", cand, song.display, str(e)[:120])
            reasons.append(_("ingame.reason_failed", detail=str(e).strip()[:120] or type(e).__name__))
            transient_seen = True
            continue
        p = Path(p)
        if not chosen and looks_static(p):
            log.info("Skipping %s for %s: it's a still image, not a music video", cand, song.display)
            reasons.append(_("ingame.reason_still"))
            p.unlink(missing_ok=True)
            continue
        path, vid, fmt = p, cand, f
        break
    if path is None and best_sd:
        # no HD anywhere: download the one best SD version (a second lookup of that video, nothing else)
        cand = best_sd[2]
        log.info("No HD video for %s: using the SD version of %s (%sp)", song.display, cand, best_sd[1])
        fetched_audio = None
        gate_wait()
        try:
            p, f, fetched_audio = yt.fetch(cand, str(tmpdir / "video"), with_audio=need_align, progress=prog, take_sd=True)
            p = Path(p)
            if looks_static(p):
                reasons.append(_("ingame.reason_still"))
                p.unlink(missing_ok=True)
            else:
                path, vid, fmt = p, cand, f
        except BotDetected:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        except (NoHDVideo, StillImage):
            pass
        except Exception as e:
            reasons.append(_("ingame.reason_failed", detail=str(e).strip()[:120] or type(e).__name__))
            transient_seen = True
    if path is None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        why = join(dict.fromkeys(reasons)) if reasons else _("ingame.reason_none")
        if reasons and not transient_seen:
            # every candidate was really checked and rejected for its resolution: remember it for all
            # charts of the song (errors like "age-restricted" or "download failed" are not remembered)
            _remember_failure(store, song, bkey, why)
        raise InGameError(_("ingame.no_hd", why=why))
    res_now = int(fmt.get("res_class") or 0)
    sd = bool(res_now and res_now < MIN_HD)        # accepted below HD because no HD version exists
    store.update_song(bkey, video_id=vid, failed=None, failed_at=None, video_sd=sd or None)
    store.update_song(song.key, video_id=vid, video_res=fmt.get("res_class"), video_failed=None,
                      video_failed_at=None, video_failed_reason=None, video_sd=sd or None,
                      video_sd_at=time.time() if sd else None)
    already_aligned = from_cache and info.get("video_id") == vid and "video_offset" in info
    if not per_manual_offset(store, song) and not already_aligned:
        try:
            if progress:
                progress(_("ingame.aligning_video"))
            off, conf = auto_offset(song, yt, vid, tmpdir,
                                    audio=Path(fetched_audio) if fetched_audio and Path(fetched_audio).exists() else None)
            if conf >= ALIGN_MIN_Z:
                store.update_song(song.key, video_offset=round(off, 3), video_offset_auto=True)
                log.info("Video for %s lined up: start %+0.2fs into the video (confidence %.0f)",
                         song.display, off, conf)
            else:
                log.info("Video for %s: no reliable audio match (confidence %.1f); starting at 0",
                         song.display, conf)
        except Exception as e:
            if "BotDetected" in type(e).__name__:
                raise
            log.warning("Could not line up the video for %s: %s", song.display, e)
    codec = (fmt.get("vcodec") or "").lower()
    if convert:
        pad = -float(store.song(song.key).get("video_offset", 0.0) or 0.0)
        pad = pad if pad >= 0.05 else 0.0
        height = (video_res(path) or int(fmt.get("res_class") or 0)) if codec.startswith("vp8") else 0
        if path.suffix.lower() == ".webm" and codec.startswith("vp8") and not pad and 0 < height <= MAX_HEIGHT + 20:
            return path          # YouTube offered VP8 (rare) or the cache had it: no re-encode
        out = tmpdir / "video_vp8.webm"
        encode_vp8(path, out, pad, progress)
        path.unlink(missing_ok=True)
        if pad:
            (tmpdir / PAD_MARK).write_text(f"{pad:.3f}")
        return out.rename(tmpdir / "video.webm")
    if path.suffix.lower() in (".mp4", ".m4v"):
        with open(path, "rb") as f:
            head = f.read(16)
        if mp4_needs_remux(head):
            out = tmpdir / "video_std.mp4"
            _remux_mp4(path, out)
            path.unlink(missing_ok=True)
            return out.rename(tmpdir / "video.mp4")
    return path


ALIGN_MIN_Z = 6.0


def per_manual_offset(store: Store, song: Song) -> bool:
    per = store.song(song.key)
    return "video_offset" in per and not per.get("video_offset_auto")


def auto_offset(song: Song, yt: YouTube, vid: str, tmpdir: Path, audio: Path | None = None) -> tuple[float, float]:
    from . import align
    names = align.chart_audio_files(song.list_files())
    if not names:
        return 0.0, 0.0
    paths = []
    for n in names:
        if song.is_sng:
            p = tmpdir / ("stem_" + n)
            p.write_bytes(song.read_file(n) or b"")
            paths.append(p)
        else:
            paths.append(song.path / n)
    if audio is None:
        audio = Path(yt.download_audio(vid, str(tmpdir / "yt_audio")))
    return align.find_offset(paths, audio)


LYRICS_MIN_Z = 5.0


def _audio_paths(song: Song, names: list[str], tmp: Path) -> list[Path]:
    paths = []
    for n in names:
        if song.is_sng:
            p = tmp / ("stem_" + n)
            p.write_bytes(song.read_file(n) or b"")
            paths.append(p)
        else:
            paths.append(song.path / n)
    return paths


def auto_lyrics_offset(song: Song, entry: dict, yt: YouTube) -> float | None:
    """LRCLIB timestamps follow the album recording. Find that recording on YouTube
    (same duration), cross-correlate it with the chart audio and return the shift to
    apply to the lyrics. Falls back to matching the chart's vocal stem."""
    from . import align
    tmp = Path(tempfile.mkdtemp(prefix="chc-", dir=cache_dir()))
    try:
        chart = _audio_paths(song, align.chart_audio_files(song.list_files()), tmp)
        if not chart:
            return None
        if entry.get("duration"):
            try:
                cand = yt.find_album_audio(entry.get("artist") or song.artist,
                                           entry.get("track") or song.name, float(entry["duration"]))
                if cand:
                    audio = Path(yt.download_audio(cand["id"], str(tmp / "album")))
                    off, z = align.find_offset(chart, audio, max_lag=60.0, tolerance=0.5)
                    if z >= LYRICS_MIN_Z:
                        log.info("Lyrics for %s lined up via the album audio: %+0.2fs (confidence %.0f)",
                                 song.display, -off, z)
                        return -off
                    log.info("Lyrics for %s: album audio didn't match the chart audio (confidence %.1f)",
                             song.display, z)
            except Exception as e:
                log.debug("album-audio alignment failed: %s", e)
        vocals = [p for p in chart if p.name.lower().replace("stem_", "").startswith("vocals")]
        if vocals:
            off, z = align.lyrics_offset(vocals, entry["lines"])
            if z >= LYRICS_MIN_Z:
                log.info("Lyrics for %s lined up with the vocal track: %+0.2fs (confidence %.0f)",
                         song.display, off, z)
                return off
        return None
    except Exception as e:
        log.warning("Could not line up lyrics for %s: %s", song.display, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _codec(p: Path) -> str:
    ff = _ffmpeg()
    if not ff:
        return ""
    try:
        err = run_process([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
        m = re.search(r"Video:\s*(\w+)", err)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def chart_gone():
    return _("job.chart_gone")


# --------------------------------------------------------------- apply to song
def _backup(p: Path):
    b = p.with_name(p.name + BACKUP)
    if not b.exists():
        shutil.copy2(p, b)
    return b


def has_video(song: Song) -> bool:
    if song.is_sng:
        try:
            return any(n.lower().startswith("video.") for n in open_sng(str(song.path)).files)
        except Exception:
            return False
    return song.local_video() is not None


def sd_is_final(per: dict) -> bool:
    """The song's video is an SD one that was taken because no HD version exists: don't look for HD again
    (every attempt costs lookups) until the "no HD" memory expires."""
    return bool(per.get("video_sd")) and time.time() - float(per.get("video_sd_at") or 0) < FAIL_MEMORY_S


def current_video_res(song: Song, store: Store) -> int:
    """Resolution class of the video the song already has (cached by size/mtime)."""
    try:
        if song.is_sng:
            sng = open_sng(str(song.path))
            name = next((n for n in sng.files if n.lower().startswith("video.")), None)
            if not name:
                return 0
            st = song.path.stat()
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            per = store.song(song.key)
            if per.get("video_probe", {}).get("sig") == sig:
                return per["video_probe"]["res"]
            p = song.local_video()   # extracted cache copy
        else:
            p = song.local_video()
            if not p:
                return 0
            st = p.stat()
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            per = store.song(song.key)
            if per.get("video_probe", {}).get("sig") == sig:
                return per["video_probe"]["res"]
        res = video_res(p) if p else 0
        store.update_song(song.key, video_probe={"sig": sig, "res": res})
        return res
    except Exception:
        return 0


def prepare(song: Song, yt: YouTube, store: Store, *, video: bool = True, lyrics: bool = True,
            force_video: bool = False, replace_low_res: bool = True, progress=None,
            defer_video: bool = False) -> dict:
    """Put video and lyrics into the song for Clone Hero. Returns a summary dict.
    defer_video: when YouTube is limiting requests, don't fail or wait: lyrics still go in and the result
    carries 'video_pending' (the YouTube limit signal) so the video step can run later."""
    with _lock_for(song.key):
        if not song.path.exists():      # waited behind another chart of the song; this one may have gone meanwhile
            raise InGameError(chart_gone())
        result = {"video": None, "lyrics": None, "video_pending": None}
        per = store.song(song.key)
        lines: list[dict] = []
        want_lyrics = lyrics and not per.get("game_lyrics") and not song.chart_lyrics()
        if want_lyrics:
            if progress:
                progress(_("ingame.finding_lyrics"))
            entry = lyr.fetch_lrclib_entry(song.artist, song.name, song.length_s)
            lines = entry["lines"] if entry else []
            if not lines:
                result["lyrics"] = "none found"
            elif "lyrics_offset" not in per or per.get("lyrics_offset_auto"):
                if progress:
                    progress(_("ingame.aligning_lyrics"))
                shift = auto_lyrics_offset(song, entry, yt)
                if shift is not None:
                    store.update_song(song.key, lyrics_offset=round(shift, 3), lyrics_offset_auto=True)
                    per = store.song(song.key)
        existing = has_video(song)
        converted = None
        if existing and video and not force_video and needs_vp8(song):
            # a video from before (MP4): convert it in place instead of downloading it again
            try:
                converted = convert_song_video(song, store, progress)
            except Exception as e:
                log.warning("Could not convert the video of %s (%s); getting a new one", song.display, e)
            if converted:
                result["video"] = converted
                store.update_song(song.key, game_video=converted)
        unplayable = existing and video and converted is None and needs_vp8(song)
        want_video = video and (force_video or not existing or unplayable
                                or (replace_low_res and 0 < current_video_res(song, store) < MIN_HD
                                    and not sd_is_final(per)))
        vpath = None
        if want_video:
            try:
                vpath = download_video(song, yt, store, progress, force=force_video or unplayable,
                                       defer=defer_video)
            except Exception as e:
                if not (defer_video and type(e).__name__ == "BotDetected"):
                    raise
                result["video_pending"] = getattr(e, "signal", "unknown")
                log.info("Video for %s deferred: YouTube is limiting requests (%s)", song.display,
                         result["video_pending"])
            per = store.song(song.key)
        off_v = float(per.get("video_offset", 0.0) or 0.0)
        write_v = off_v  # value for video_start_time
        if vpath and padded_seconds(vpath):     # the black lead-in is already in the file (one encode)
            write_v = 0.0
        off_l = float(per.get("lyrics_offset", 0.0) or 0.0) + float(store.get("lyrics_offset", 0.0) or 0.0)
        _cancel.check()
        try:
            if song.is_sng:
                _apply_sng(song, vpath, lines, off_l, write_v, result)
            else:
                _apply_folder(song, vpath, lines, off_l, write_v, result)
        finally:
            if vpath:
                shutil.rmtree(vpath.parent, ignore_errors=True)
        changes = {}
        if result["video"] and vpath:
            changes["game_video"] = result["video"]
            changes["video_offset_written"] = off_v if abs(off_v) >= 0.05 else 0.0
            changes["video_padded"] = round(off_v - write_v, 3) or None
        if result["lyrics"] == "added":
            changes["game_lyrics"] = True
            changes["lyrics_offset_written"] = off_l
        if changes:
            store.update_song(song.key, **changes)
        return result


def convert_song_video(song: Song, store: Store, progress=None) -> str | None:
    """Re-encode the song's MP4 (folder or .sng) as VP8 WebM and replace it: a folder gets video.webm and loses
    video.mp4; a .sng is repacked with video.webm (backup first, then verified, like every .sng change here).
    Start time and padding stay as they were. Returns the new name, None if there was nothing to convert."""
    with _lock_for(song.key):
        if not song.path.exists():
            raise InGameError(chart_gone())
        names = video_names(song)
        if not names or not needs_vp8(song):
            return None
        if progress:
            progress(_("ingame.converting_webm"))
        tmpdir = Path(tempfile.mkdtemp(prefix="chc-conv-", dir=cache_dir()))
        try:
            if song.is_sng:
                src = open_sng(str(song.path))
                name = next(n for n in src.files if n.lower() in VIDEO_NAMES)
                a = tmpdir / ("in" + Path(name).suffix.lower())
                a.write_bytes(src.read(name))
            else:
                a = song.path / names[0]
            out = tmpdir / "video.webm"
            encode_vp8(a, out, progress=progress)
            if not out.exists() or out.stat().st_size == 0:
                raise InGameError(_("ingame.ffmpeg_failed", detail="empty output"))
            _cancel.check()
            result: dict = {"video": None, "lyrics": None}
            if song.is_sng:
                _apply_sng(song, out, [], 0.0, song.video_start_s, result)
            else:
                _apply_folder(song, out, [], 0.0, song.video_start_s, result)
            return result["video"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _apply_folder(song: Song, vpath: Path | None, lines, off_l, off_v, result):
    if vpath:
        dst = song.path / ("video" + vpath.suffix.lower())
        tmp = song.path / ".chc-video.tmp"
        try:
            shutil.copyfile(vpath, tmp)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        want = vpath.stat().st_size
        try:
            got = dst.stat().st_size
        except OSError:
            got = -1
        if got != want:
            raise InGameError(_("ingame.video_not_saved", path=song.path, got=got, want=want))
        for n in VIDEO_NAMES:                # only now (a failed copy must not cost the old video)
            old = song.path / n
            if old.exists() and old.name != dst.name:
                old.unlink()
        result["video"] = dst.name
        if abs(off_v) >= 0.05 or "video_start_time" in song.meta:
            _set_ini(song, "video_start_time", int(round(off_v * 1000)))
    if lines:
        mid = song.path / "notes.mid"
        chart = song.path / "notes.chart"
        if mid.exists():
            b = _backup(mid)
            _atomic_write(mid, midi_with_lyrics(b.read_bytes(), lines, song.delay_s, off_l))
        elif chart.exists():
            b = _backup(chart)
            text = b.read_bytes().decode("utf-8-sig", "replace")
            _atomic_write(chart, chart_with_lyrics(text, lines, song.delay_s, off_l).encode("utf-8"))
        else:
            return
        result["lyrics"] = "added"


def _atomic_write(p: Path, data: bytes) -> None:
    tmp = p.with_name(p.name + ".chc-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def _apply_sng(song: Song, vpath: Path | None, lines, off_l, off_v, result):
    src_path = str(song.path)
    src = open_sng(src_path)
    files: dict[str, bytes | str] = {n: n for n in src.files}
    meta = dict(src.metadata)
    changed = False
    if vpath:
        for n in list(files):
            if n.lower().startswith("video."):
                del files[n]
        files["video" + vpath.suffix.lower()] = vpath.read_bytes()
        result["video"] = "video" + vpath.suffix.lower() + " (inside .sng)"
        if abs(off_v) >= 0.05 or "video_start_time" in meta:
            meta["video_start_time"] = str(int(round(off_v * 1000)))
        changed = True
    if lines:
        names = {n.lower(): n for n in src.files}
        try:
            if "notes.mid" in names:
                files[names["notes.mid"]] = midi_with_lyrics(src.read(names["notes.mid"]), lines, song.delay_s, off_l)
                changed = result.__setitem__("lyrics", "added") or True
            elif "notes.chart" in names:
                text = src.read(names["notes.chart"]).decode("utf-8-sig", "replace")
                files[names["notes.chart"]] = chart_with_lyrics(text, lines, song.delay_s, off_l).encode("utf-8")
                changed = result.__setitem__("lyrics", "added") or True
        except InGameError as e:
            result["lyrics"] = str(e)
    if changed:
        _backup(song.path)
        write_sng(src_path, meta, files, src=src)
        song.meta.update({k.lower(): v for k, v in meta.items()})
        if vpath:
            chk = open_sng(src_path)
            name = "video" + vpath.suffix.lower()
            entry = next((e for n, e in chk.files.items() if n.lower() == name), None)
            if entry is None or entry.size != vpath.stat().st_size:
                raise InGameError(_("ingame.video_not_packed", name=song.path.name))


def _set_ini(song: Song, key: str, value) -> None:
    ini = song.path / "song.ini"
    if not ini.exists():
        return
    _backup(ini)
    raw = ini.read_bytes()
    enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        enc, text = "latin-1", raw.decode("latin-1")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    for i, l in enumerate(lines):
        if l.split("=", 1)[0].strip().lower() == key:
            lines[i] = f"{key} = {value}"
            break
    else:
        idx = next((i for i, l in enumerate(lines) if l.strip().lower() == "[song]"), None)
        if idx is None:
            lines.insert(0, "[song]")
            idx = 0
        lines.insert(idx + 1, f"{key} = {value}")
    ini.write_text(nl.join(lines), encoding=enc)
    song.meta[key] = str(value)


def resync(song: Song, yt: YouTube, store: Store, progress=None) -> dict:
    """Apply changed video/lyrics offsets to a song that is already set up, without
    downloading the video again when only video_start_time has to change."""
    with _lock_for(song.key):
        per = store.song(song.key)
        result: dict = {"video": None, "lyrics": None}
        off_v = float(per.get("video_offset", 0.0) or 0.0)
        padded = float(per.get("video_padded") or 0.0)
        if has_video(song):
            if padded or off_v <= -0.05:
                if per.get("video_id"):
                    # the black lead-in is baked into the file: rebuild it
                    pass_result = None
                    store.update_song(song.key, video_offset_auto=None)
                    pass_result = _redo_video(song, yt, store, progress)
                    result["video"] = pass_result
            else:
                if progress:
                    progress(_("ingame.updating_start"))
                ms = int(round(off_v * 1000))
                if song.is_sng:
                    src = open_sng(str(song.path))
                    meta = dict(src.metadata)
                    meta["video_start_time"] = str(ms)
                    _backup(song.path)
                    write_sng(str(song.path), meta, {n: n for n in src.files}, src=src)
                    song.meta["video_start_time"] = str(ms)
                else:
                    _set_ini(song, "video_start_time", ms)
                store.update_song(song.key, video_offset_written=off_v)
                result["video"] = f"start {off_v:+.2f}s"
        if per.get("game_lyrics"):
            if progress:
                progress(_("ingame.moving_lyrics"))
            entry = lyr.fetch_lrclib_entry(song.artist, song.name, song.length_s)
            if entry and entry["lines"]:
                off_l = float(per.get("lyrics_offset", 0.0) or 0.0) + float(store.get("lyrics_offset", 0.0) or 0.0)
                if song.is_sng:
                    orig = open_sng(str(song.path) + BACKUP) if Path(str(song.path) + BACKUP).exists() else None
                    src = open_sng(str(song.path))
                    files: dict[str, bytes | str] = {n: n for n in src.files}
                    names = {n.lower(): n for n in src.files}
                    if orig:
                        onames = {n.lower(): n for n in orig.files}
                        if "notes.mid" in onames:
                            files[names["notes.mid"]] = midi_with_lyrics(orig.read(onames["notes.mid"]),
                                                                         entry["lines"], song.delay_s, off_l)
                        elif "notes.chart" in onames:
                            files[names["notes.chart"]] = chart_with_lyrics(
                                orig.read(onames["notes.chart"]).decode("utf-8-sig", "replace"),
                                entry["lines"], song.delay_s, off_l).encode("utf-8")
                        write_sng(str(song.path), dict(src.metadata), files, src=src)
                        result["lyrics"] = "moved"
                else:
                    mid, chart = song.path / ("notes.mid" + BACKUP), song.path / ("notes.chart" + BACKUP)
                    if mid.exists():
                        _atomic_write(song.path / "notes.mid",
                                      midi_with_lyrics(mid.read_bytes(), entry["lines"], song.delay_s, off_l))
                        result["lyrics"] = "moved"
                    elif chart.exists():
                        _atomic_write(song.path / "notes.chart", chart_with_lyrics(
                            chart.read_bytes().decode("utf-8-sig", "replace"), entry["lines"], song.delay_s,
                            off_l).encode("utf-8"))
                        result["lyrics"] = "moved"
                if result["lyrics"]:
                    store.update_song(song.key, lyrics_offset_written=off_l)
        return result


def _redo_video(song: Song, yt: YouTube, store: Store, progress=None) -> str | None:
    """Download the chosen video again and write it with the current offset."""
    store.update_song(song.key, video_manual=True)
    vpath = download_video(song, yt, store, progress)
    per = store.song(song.key)
    off_v = float(per.get("video_offset", 0.0) or 0.0)
    write_v = off_v
    try:
        if padded_seconds(vpath):
            write_v = 0.0
        result: dict = {"video": None, "lyrics": None}
        if song.is_sng:
            _apply_sng(song, vpath, [], 0.0, write_v, result)
        else:
            _apply_folder(song, vpath, [], 0.0, write_v, result)
    finally:
        shutil.rmtree(vpath.parent, ignore_errors=True)
    store.update_song(song.key, game_video=result["video"], video_offset_written=off_v,
                      video_padded=round(off_v - write_v, 3) or None)
    return result["video"]


def restore(song: Song, store: Store) -> list[str]:
    """Undo everything we changed in this song."""
    done = []
    per = store.song(song.key)
    if song.is_sng:
        b = Path(str(song.path) + BACKUP)
        if b.exists():
            os.replace(b, song.path)
            done.append(song.path.name)
    else:
        for name in ("notes.mid", "notes.chart", "song.ini"):
            b = song.path / (name + BACKUP)
            if b.exists():
                os.replace(b, song.path / name)
                done.append(name)
        gv = per.get("game_video")
        if gv and (song.path / gv).exists():
            (song.path / gv).unlink()
            done.append(gv)
    store.update_song(song.key, game_video=None, game_lyrics=None)
    return done
