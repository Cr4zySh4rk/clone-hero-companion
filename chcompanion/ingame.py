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
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from . import lyrics as lyr
from .library import Song, VIDEO_NAMES
from .midi import TempoMap, build_track, chunk_track_name, encode_varlen, parse_midi, split_chunks, assemble
from .paths import config_dir
from .sng import open_sng, write_sng
from .store import Store
from .youtube import MIN_SCORE, YouTube

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
        p_on = to_tick(syls[0]["t"] + offset_s)
        p_off = max(p_on + 1, to_tick(syls[-1]["e"] + offset_s))
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
            raise InGameError("chart already has lyrics")
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
        raise InGameError("chart already has lyrics")
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


def _to_vp8(src: Path, dst: Path) -> None:
    ff = _ffmpeg()
    if not ff:
        raise InGameError("ffmpeg is needed to convert the video (Clone Hero on Linux only plays VP8 .webm)")
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-an",
                        "-c:v", "libvpx", "-b:v", "2500k", "-crf", "10", "-deadline", "realtime",
                        "-cpu-used", "8", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        raise InGameError("ffmpeg failed: " + r.stderr.strip()[-300:])


def download_video(song: Song, yt: YouTube, store: Store, progress=None) -> Path:
    info = store.song(song.key)
    vid = info.get("video_id")
    if not vid:
        cands = yt.search(song)
        if not cands or cands[0]["score"] < MIN_SCORE:
            store.update_song(song.key, candidates=cands[:8], video_failed=True)
            raise InGameError("no matching music video found on YouTube")
        vid = cands[0]["id"]
        store.update_song(song.key, video_id=vid, candidates=cands[:8])
    tmpdir = Path(tempfile.mkdtemp(prefix="chc-", dir=config_dir()))

    def prog(done, total):
        if progress and total:
            progress(f"downloading video {done * 100 // total}%")
    path = Path(yt.download(vid, str(tmpdir / "video"), progress=prog))
    if not per_manual_offset(store, song):
        try:
            if progress:
                progress("lining the video up with the song")
            off, conf = auto_offset(song, yt, vid, tmpdir)
            if conf >= ALIGN_MIN_Z:
                store.update_song(song.key, video_offset=round(off, 3), video_offset_auto=True)
                log.info("Video for %s lined up: start %+0.2fs into the video (confidence %.0f)",
                         song.display, off, conf)
            else:
                log.info("Video for %s: no reliable audio match (confidence %.1f); starting at 0",
                         song.display, conf)
        except Exception as e:
            log.warning("Could not line up the video for %s: %s", song.display, e)
    target = os.environ.get("CHC_TARGET_OS", sys.platform)
    if target.startswith("linux") or (path.suffix.lower() == ".webm" and "vp8" not in _codec(path)):
        if progress:
            progress("converting video")
        out = tmpdir / "video_vp8.webm"
        _to_vp8(path, out)
        path.unlink(missing_ok=True)
        out = out.rename(tmpdir / "video.webm")
        return out
    return path


ALIGN_MIN_Z = 6.0


def per_manual_offset(store: Store, song: Song) -> bool:
    per = store.song(song.key)
    return "video_offset" in per and not per.get("video_offset_auto")


def auto_offset(song: Song, yt: YouTube, vid: str, tmpdir: Path) -> tuple[float, float]:
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
    tmp = Path(tempfile.mkdtemp(prefix="chc-", dir=config_dir()))
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
                    off, z = align.find_offset(chart, audio, max_lag=60.0)
                    if z >= ALIGN_MIN_Z:
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
        err = subprocess.run([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
        m = re.search(r"Video:\s*(\w+)", err)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


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


def prepare(song: Song, yt: YouTube, store: Store, *, video: bool = True, lyrics: bool = True,
            force_video: bool = False, progress=None) -> dict:
    """Put video and lyrics into the song for Clone Hero. Returns a summary dict."""
    with _lock_for(song.key):
        result = {"video": None, "lyrics": None}
        per = store.song(song.key)
        lines: list[dict] = []
        want_lyrics = lyrics and not per.get("game_lyrics") and not song.chart_lyrics()
        if want_lyrics:
            if progress:
                progress("finding lyrics")
            entry = lyr.fetch_lrclib_entry(song.artist, song.name, song.length_s)
            lines = entry["lines"] if entry else []
            if not lines:
                result["lyrics"] = "none found"
            elif "lyrics_offset" not in per or per.get("lyrics_offset_auto"):
                if progress:
                    progress("lining the lyrics up with the song")
                shift = auto_lyrics_offset(song, entry, yt)
                if shift is not None:
                    store.update_song(song.key, lyrics_offset=round(shift, 3), lyrics_offset_auto=True)
                    per = store.song(song.key)
        want_video = video and (force_video or not has_video(song))
        vpath = None
        if want_video:
            vpath = download_video(song, yt, store, progress)
            per = store.song(song.key)
        off_v = float(per.get("video_offset", 0.0) or 0.0)
        off_l = float(per.get("lyrics_offset", 0.0) or 0.0) + float(store.get("lyrics_offset", 0.0) or 0.0)
        try:
            if song.is_sng:
                _apply_sng(song, vpath, lines, off_l, off_v, result)
            else:
                _apply_folder(song, vpath, lines, off_l, off_v, result)
        finally:
            if vpath:
                shutil.rmtree(vpath.parent, ignore_errors=True)
        changes = {}
        if result["video"]:
            changes["game_video"] = result["video"]
            changes["video_offset_written"] = off_v if abs(off_v) >= 0.05 else 0.0
        if result["lyrics"] == "added":
            changes["game_lyrics"] = True
            changes["lyrics_offset_written"] = off_l
        if changes:
            store.update_song(song.key, **changes)
        return result


def _apply_folder(song: Song, vpath: Path | None, lines, off_l, off_v, result):
    if vpath:
        for n in VIDEO_NAMES:
            old = song.path / n
            if old.exists():
                old.unlink()
        dst = song.path / ("video" + vpath.suffix.lower())
        tmp = song.path / ".chc-video.tmp"
        shutil.copyfile(vpath, tmp)
        os.replace(tmp, dst)
        result["video"] = dst.name
        if abs(off_v) >= 0.05:
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
    tmp.write_bytes(data)
    os.replace(tmp, p)


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
        if abs(off_v) >= 0.05:
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
