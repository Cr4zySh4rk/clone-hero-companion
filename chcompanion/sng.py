"""Reader for the .sng song container used by Clone Hero
(spec: https://github.com/mdsitton/SngFileFormat)."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class SngEntry:
    name: str
    size: int
    offset: int


@dataclass
class SngFile:
    path: str
    xor_mask: bytes
    metadata: dict[str, str] = field(default_factory=dict)
    files: dict[str, SngEntry] = field(default_factory=dict)

    def read(self, name: str) -> bytes | None:
        entry = None
        for k, v in self.files.items():
            if k.lower() == name.lower():
                entry = v
                break
        if entry is None:
            return None
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            raw = bytearray(f.read(entry.size))
        mask = self.xor_mask
        for i in range(len(raw)):
            raw[i] ^= mask[i & 15] ^ (i & 0xFF)
        return bytes(raw)


def open_sng(path: str) -> SngFile:
    with open(path, "rb") as f:
        head = f.read(26)
        if head[:6] != b"SNGPKG":
            raise ValueError("not an SNG file")
        mask = head[10:26]
        sng = SngFile(path, mask)
        meta_len, meta_count = struct.unpack("<QQ", f.read(16))
        for _ in range(meta_count):
            klen = struct.unpack("<i", f.read(4))[0]
            key = f.read(klen).decode("utf-8", "replace")
            vlen = struct.unpack("<i", f.read(4))[0]
            val = f.read(vlen).decode("utf-8", "replace")
            sng.metadata[key.lower()] = val
        _idx_len, file_count = struct.unpack("<QQ", f.read(16))
        for _ in range(file_count):
            nlen = f.read(1)[0]
            name = f.read(nlen).decode("utf-8", "replace")
            size, offset = struct.unpack("<QQ", f.read(16))
            sng.files[name] = SngEntry(name, size, offset)
    return sng
