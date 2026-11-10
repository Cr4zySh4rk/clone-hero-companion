"""Clone Hero's scoredata.bin: the game's own table of per-chart bests (stars, full combo, score).

scorestats.json only holds the last finished song, and CH Leaderboards' public API cannot list an account's
scores, so this file is the most complete record of what a player has achieved. It is unused unless it parses
and sanity-checks (see ScoreDataCache.read).

Format (little endian), v1.1.0.6142 era, version tag 20211009. There is no official specification; the layout
below is what independent open-source readers agree on:

    u32  version tag = 20211009
    u32  number of charts
    per chart:
        16B  MD5 of the chart file (notes.chart / notes.mid), the same checksum scorestats.json carries
        u8   number of score rows
        u24  play count of the chart
        per row, 16 bytes:
            +0  u16  instrument id (Clone Hero's enum, see INSTRUMENTS)
            +2  u8   difficulty 0 Easy .. 3 Expert
            +3  u8   best percent, floored
            +4  u8   full combo flag
            +5  u16  playback speed in percent
            +7  u8   stars (0..6; 7 is not seen but tolerated)
            +8  u32  modifier flags
            +12 u32  best score (the game's reported score minus the clean-play bonus)

Sources read to pin this down (none is official):
  * heiner-palmen/chkit, chkit/chfiles/scoredata.py: the full field layout above, version tag, instrument
    enum, decoded against a v1.1.0.6142 install and CH Leaderboards data.
  * unicxrn/encore, src/main/play/scoredata.ts: the same header, chart head and 16-byte row, verified on 101
    charts / 111 rows (it could not confirm the instrument enum: it only saw ids 0 and 8, and it could not tell
    a full combo flag from a perfect one).
  * Staycation44/clonehero-score-exporter, main.py: same header, chart head, row size, stars at +7, score at
    +12 (it reads +3..+6 as two u16 numerator/denominator, which gives the same percent when the FC flag is 0).
  * matthiasduyck/CloneHeroSaveGameEditor (2021): a much older layout (32-char hex ids), not this one.
  * Data locations: wiki.clonehero.net "data locations".

Uncertain: the instrument enum beyond ids 0 and 8 rests on chkit alone; the full combo flag cannot be told apart
from a perfect-full-combo flag; each row is the best run per chart and instrument, so its difficulty is that of
the best-score run.
"""
from __future__ import annotations

import logging
import os
import struct
import sys
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 20211009
FILENAME = "scoredata.bin"

# Clone Hero's instrument enum as chkit decodes it, mapped to the app's instrument ids (playstats.norm_instrument).
INSTRUMENTS = {0: "guitar", 1: "bass", 2: "rhythm", 3: "guitar", 4: "guitarghl", 5: "bassghl", 6: "drums",
               7: "keys", 8: "band", 9: "drums", 10: "rhythmghl", 11: "guitarghl", 12: "crowd", 13: "vocals",
               14: "keysghl", 0xFFFF: "none"}
DIFFICULTIES = ("easy", "medium", "hard", "expert")
IGNORED_INSTRUMENTS = ("band", "crowd", "none")     # not a single player's instrument: never counted

_HEADER = struct.Struct("<II")
_ROW = struct.Struct("<HBBBHBII")                   # 16 bytes
_CHART_HEAD = 20


def parse(data: bytes) -> list[dict]:
    """Every score row as {checksum, instrument, difficulty, stars, score, fc, percent, speed, play_count}.
    Never raises: on an unknown version or a truncated file it logs and returns the rows it could read."""
    try:
        return _parse(data)[0]
    except Exception:
        log.exception("scoredata.bin could not be parsed")
        return []


def _parse(data: bytes) -> tuple[list[dict], bool]:
    """(rows, complete): complete is True only when the version is known and the file walks exactly to its end."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < _HEADER.size:
        log.info("scoredata.bin: %s bytes, too short", len(data) if hasattr(data, "__len__") else "?")
        return [], False
    data = bytes(data)
    version, count = _HEADER.unpack_from(data, 0)
    if version != VERSION:
        log.info("scoredata.bin: unknown version tag %s (this code reads %s)", version, VERSION)
        return [], False
    out: list[dict] = []
    pos, complete = _HEADER.size, True
    for _ in range(count):
        if pos + _CHART_HEAD > len(data):
            log.info("scoredata.bin ends inside a chart header at byte %d", pos)
            complete = False
            break
        checksum = data[pos:pos + 16].hex().upper()
        rows = data[pos + 16]
        plays = int.from_bytes(data[pos + 17:pos + 20], "little")
        pos += _CHART_HEAD
        for i in range(rows):
            if pos + _ROW.size > len(data):
                log.info("scoredata.bin ends inside the rows of chart %s", checksum)
                complete = False
                break
            inst, diff, pct, fc, speed, stars, _mods, score = _ROW.unpack_from(data, pos)
            pos += _ROW.size
            out.append({
                "checksum": checksum, "instrument": INSTRUMENTS.get(inst, f"unknown{inst}"),
                "difficulty": DIFFICULTIES[diff] if diff < 4 else f"unknown{diff}",
                "stars": stars, "score": score, "fc": fc == 1, "percent": pct, "speed": speed,
                "play_count": plays, "_raw": {"instrument": inst, "difficulty": diff, "fc": fc},
            })
        if not complete:
            break
    if complete and pos != len(data):
        log.info("scoredata.bin has %d unexpected bytes after the last chart", len(data) - pos)
        complete = False
    return out, complete


def sane(rows: list[dict]) -> bool:
    """Sanity check on a parsed file: values must be in the ranges the format allows, with some rows at all.
    Anything else means the layout is not what this module expects and the file must not be used."""
    if not rows:
        return False
    bad = 0
    for r in rows:
        raw = r.get("_raw") or {}
        if r["stars"] > 7 or r["percent"] > 100 or raw.get("difficulty", 0) > 3 or raw.get("fc", 0) not in (0, 1) \
                or not 0 < r["speed"] <= 1000:
            bad += 1
    return bad == 0


def candidate_files(data_dir: Path | None = None, game_dir: Path | None = None) -> list[Path]:
    """Where scoredata.bin may live (Clone Hero's Unity persistent data folder), most likely first.
    macOS ~/Library/Application Support/com.srylain.CloneHero, Windows %USERPROFILE%\\AppData\\LocalLow\\
    srylain Inc_\\Clone Hero, Linux ~/.config/unity3d/srylain Inc_/Clone Hero (wiki) or ~/.clonehero, plus the
    configured Clone Hero data folder and the game folder (portable installs)."""
    home = Path.home()
    dirs: list[Path] = []
    if sys.platform == "darwin":
        dirs.append(home / "Library" / "Application Support" / "com.srylain.CloneHero")
    elif sys.platform == "win32":
        base = os.environ.get("USERPROFILE")
        dirs.append((Path(base) if base else home) / "AppData" / "LocalLow" / "srylain Inc_" / "Clone Hero")
    else:
        dirs += [home / ".config" / "unity3d" / "srylain Inc_" / "Clone Hero", home / ".clonehero"]
    dirs += [d for d in (data_dir, game_dir) if d]
    out: list[Path] = []
    for d in dirs:
        p = Path(d) / FILENAME
        if p not in out:
            out.append(p)
    return out


class ScoreDataCache:
    """Reads scoredata.bin, re-reading only when its mtime or size changes (one stat per file per call)."""

    def __init__(self, locate):
        self.locate = locate                  # () -> list[Path]
        self._sig: dict[str, tuple] = {}
        self._res: dict[str, tuple[list[dict], bool]] = {}

    def read(self) -> list[dict]:
        """Rows of the newest scoredata.bin that parses completely and sanity-checks, else []."""
        best: tuple[float, list[dict]] | None = None
        for p in self.locate() or []:
            try:
                st = p.stat()
            except OSError:
                continue
            key, sig = str(p), (st.st_mtime_ns, st.st_size)
            if self._sig.get(key) != sig:
                try:
                    rows, complete = _parse(p.read_bytes())
                except Exception:
                    log.exception("scoredata.bin could not be read: %s", p)
                    rows, complete = [], False
                self._res[key] = (rows if complete and sane(rows) else [], complete)
                self._sig[key] = sig
            rows = self._res.get(key, ([], False))[0]
            if rows and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, rows)
        return best[1] if best else []
