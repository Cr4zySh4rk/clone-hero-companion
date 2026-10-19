"""Clone Hero setlists (<data>/Setlists/<name>.setlist).

Format (Clone Hero v1.1, as documented by Clone Hero Chart Manager):
  header  EA EC 33 01
  count   uint32 little-endian
  entry   0x20, 32 ASCII uppercase-hex MD5 of the raw notes.chart (preferred) or
          notes.mid, 1 byte speed percent (100), 0x00
Clone Hero reads setlists at startup.
"""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

from .library import Song

HEADER = bytes([0xEA, 0xEC, 0x33, 0x01])


def setlists_dir(data_dir: Path) -> Path:
    d = data_dir / "Setlists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def song_hash(song: Song) -> str | None:
    files = {f.lower(): f for f in song.list_files()}
    for n in ("notes.chart", "notes.mid"):
        if n in files:
            data = song.read_file(files[n])
            if data:
                return hashlib.md5(data).hexdigest().upper()
    return None


def decode(buf: bytes) -> list[tuple[str, int]]:
    if len(buf) < 8 or buf[:4] != HEADER:
        return []
    count = struct.unpack("<I", buf[4:8])[0]
    out, off = [], 8
    for _ in range(count):
        if off + 35 > len(buf):
            break
        h = buf[off + 1: off + 33].decode("ascii", "replace")
        out.append((h, buf[off + 33]))
        off += 35
    return out


def encode(entries: list[tuple[str, int]]) -> bytes:
    b = bytearray(HEADER + struct.pack("<I", len(entries)))
    for h, speed in entries:
        b += b"\x20" + h.upper().encode("ascii")[:32] + bytes([max(1, min(int(speed), 255))]) + b"\x00"
    return bytes(b)


def safe(name: str) -> str:
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()[:100]
    if not n:
        raise ValueError("Give the setlist a name")
    return n


def list_all(data_dir: Path) -> list[dict]:
    out = []
    for p in sorted(setlists_dir(data_dir).glob("*.setlist")):
        out.append({"name": p.stem, "entries": decode(p.read_bytes())})
    return out


def save(data_dir: Path, name: str, entries: list[tuple[str, int]]) -> Path:
    p = setlists_dir(data_dir) / (safe(name) + ".setlist")
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(encode(entries))
    tmp.replace(p)
    return p


def add(data_dir: Path, name: str, hashes: list[str]) -> int:
    p = setlists_dir(data_dir) / (safe(name) + ".setlist")
    entries = decode(p.read_bytes()) if p.exists() else []
    have = {h for h, _ in entries}
    added = 0
    for h in hashes:
        if h and h not in have:
            entries.append((h, 100))
            have.add(h)
            added += 1
    save(data_dir, name, entries)
    return added


def remove(data_dir: Path, name: str, hashes: list[str]) -> None:
    p = setlists_dir(data_dir) / (safe(name) + ".setlist")
    if p.exists():
        drop = set(hashes)
        save(data_dir, name, [e for e in decode(p.read_bytes()) if e[0] not in drop])


def delete(data_dir: Path, name: str) -> None:
    p = setlists_dir(data_dir) / (safe(name) + ".setlist")
    if p.exists():
        p.unlink()
