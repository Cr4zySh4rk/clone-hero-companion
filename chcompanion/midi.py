"""Minimal Standard MIDI File reader — just enough for Clone Hero / Rock Band charts.

We only need: the tempo map (track 0), and from the vocals track the lyric/text
meta events, phrase markers (notes 105/106) and sung notes (36-84).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class MidiTrack:
    name: str = ""
    # (tick, meta_type, text)
    texts: list[tuple[int, int, str]] = field(default_factory=list)
    # (tick_on, tick_off, pitch)
    notes: list[tuple[int, int, int]] = field(default_factory=list)
    # (tick, microseconds per quarter)
    tempos: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class MidiFile:
    resolution: int
    tracks: list[MidiTrack]

    def tempo_map(self) -> "TempoMap":
        tempos: list[tuple[int, int]] = []
        for t in self.tracks:
            tempos.extend(t.tempos)
        return TempoMap(self.resolution, tempos)

    def track(self, *names: str) -> MidiTrack | None:
        wanted = [n.upper() for n in names]
        for n in wanted:
            for t in self.tracks:
                if t.name.strip().upper() == n:
                    return t
        return None


class TempoMap:
    """Converts ticks to seconds given a list of (tick, us_per_quarter)."""

    def __init__(self, resolution: int, tempos: list[tuple[int, int]]):
        self.res = resolution or 480
        tempos = sorted(tempos)
        if not tempos or tempos[0][0] != 0:
            tempos.insert(0, (0, 500000))
        # precompute seconds at each tempo change
        self.points: list[tuple[int, float, int]] = []  # tick, seconds, uspq
        secs = 0.0
        last_tick, last_us = tempos[0][0], tempos[0][1]
        self.points.append((last_tick, 0.0, last_us))
        for tick, us in tempos[1:]:
            secs += (tick - last_tick) * last_us / 1e6 / self.res
            last_tick, last_us = tick, us
            self.points.append((tick, secs, us))

    def ticks(self, seconds: float) -> int:
        """Inverse of seconds()."""
        if seconds <= 0:
            return 0
        lo, hi = 0, len(self.points) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.points[mid][1] <= seconds:
                lo = mid
            else:
                hi = mid - 1
        ptick, psec, us = self.points[lo]
        return int(round(ptick + (seconds - psec) * 1e6 * self.res / us))

    def seconds(self, tick: int) -> float:
        # binary search for last point <= tick
        lo, hi = 0, len(self.points) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.points[mid][0] <= tick:
                lo = mid
            else:
                hi = mid - 1
        ptick, psec, us = self.points[lo]
        return psec + (tick - ptick) * us / 1e6 / self.res


def split_chunks(data: bytes) -> tuple[bytes, list[bytes]]:
    """Return (header chunk, [raw track chunks incl. 'MTrk'+len])."""
    hlen = struct.unpack(">I", data[4:8])[0]
    header = data[:8 + hlen]
    pos, chunks = 8 + hlen, []
    while pos + 8 <= len(data):
        clen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        chunks.append(data[pos:pos + 8 + clen])
        pos += 8 + clen
    return header, chunks


def chunk_track_name(chunk: bytes) -> str:
    if chunk[:4] != b"MTrk":
        return ""
    t = _parse_track(chunk, 8, len(chunk), keep=None)
    return t.name


def encode_varlen(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def build_track(name: str, events: list[tuple[int, int, bytes]]) -> bytes:
    """events: (tick, order, raw_event_without_delta). Returns an MTrk chunk."""
    body = bytearray()
    nm = name.encode("utf-8")
    body += b"\x00\xff\x03" + encode_varlen(len(nm)) + nm
    last = 0
    for tick, _order, raw in sorted(events, key=lambda e: (e[0], e[1])):
        body += encode_varlen(tick - last) + raw
        last = tick
    body += b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def assemble(header: bytes, chunks: list[bytes]) -> bytes:
    fmt, _n, div = struct.unpack(">HHH", header[8:14])
    new_header = header[:8] + struct.pack(">HHH", 1 if fmt == 0 and len(chunks) > 1 else fmt,
                                          len(chunks), div) + header[14:]
    return new_header + b"".join(chunks)


def _varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", "replace")


def parse_midi(data: bytes, keep_tracks: set[str] | None = None) -> MidiFile:
    """Parse a MIDI file. If keep_tracks is given, only note/text data for those
    track names (upper-case) is retained (tempo is always kept)."""
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    hlen = struct.unpack(">I", data[4:8])[0]
    _fmt, ntrks, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise ValueError("SMPTE time division not supported")
    pos = 8 + hlen
    tracks: list[MidiTrack] = []
    for _ in range(ntrks):
        if pos + 8 > len(data):
            break
        if data[pos:pos + 4] != b"MTrk":
            # skip unknown chunk
            clen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            pos += 8 + clen
            continue
        tlen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        tstart, tend = pos + 8, min(pos + 8 + tlen, len(data))
        pos = tend
        tracks.append(_parse_track(data, tstart, tend, keep_tracks))
    return MidiFile(division, tracks)


def _parse_track(data: bytes, p: int, end: int, keep: set[str] | None) -> MidiTrack:
    tr = MidiTrack()
    tick = 0
    status = 0
    open_notes: dict[int, int] = {}
    keep_detail = keep is None
    first_name = True
    try:
        while p < end:
            delta, p = _varlen(data, p)
            tick += delta
            b = data[p]
            if b & 0x80:
                status = b
                p += 1
            if status == 0xFF:
                mtype = data[p]
                p += 1
                mlen, p = _varlen(data, p)
                payload = data[p:p + mlen]
                p += mlen
                if mtype == 0x03 and first_name:
                    tr.name = _decode(payload)
                    first_name = False
                    if keep is not None:
                        keep_detail = tr.name.strip().upper() in keep
                elif mtype == 0x51 and mlen == 3:
                    tr.tempos.append((tick, int.from_bytes(payload, "big")))
                elif mtype in (0x01, 0x05) and keep_detail:
                    tr.texts.append((tick, mtype, _decode(payload)))
                elif mtype == 0x2F:
                    break
            elif status in (0xF0, 0xF7):
                slen, p = _varlen(data, p)
                p += slen
            else:
                hi = status & 0xF0
                if hi in (0xC0, 0xD0):
                    p += 1
                else:
                    d1, d2 = data[p], data[p + 1]
                    p += 2
                    if keep_detail:
                        if hi == 0x90 and d2 > 0:
                            if d1 in open_notes:  # retrigger: close previous
                                tr.notes.append((open_notes.pop(d1), tick, d1))
                            open_notes[d1] = tick
                        elif hi == 0x80 or (hi == 0x90 and d2 == 0):
                            on = open_notes.pop(d1, None)
                            if on is not None:
                                tr.notes.append((on, tick, d1))
    except IndexError:
        pass  # truncated track; keep what we have
    for pitch, on in open_notes.items():
        tr.notes.append((on, tick, pitch))
    tr.notes.sort()
    return tr
