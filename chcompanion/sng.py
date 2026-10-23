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
            raw = f.read(entry.size)
        return _xor(raw, self.xor_mask)

    def head(self, name: str, n: int = 16) -> bytes | None:
        """First n bytes of a file inside the package (cheap: no full read)."""
        entry = next((v for k, v in self.files.items() if k.lower() == name.lower()), None)
        if entry is None:
            return None
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(min(n, entry.size))
        return _xor(raw, self.xor_mask)


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


def write_sng(path: str, metadata: dict[str, str], files: dict[str, bytes | str],
              src: SngFile | None = None) -> None:
    """Write an .sng package. `files` values are bytes, or the name of a file inside
    `src` to copy (streamed, so large videos aren't held twice in memory)."""
    import os

    names = list(files.keys())
    sizes = []
    for n in names:
        v = files[n]
        sizes.append(len(v) if isinstance(v, (bytes, bytearray)) else src.files[v].size)
    meta = b""
    for k, v in metadata.items():
        kb, vb = k.encode("utf-8"), str(v).encode("utf-8")
        meta += struct.pack("<i", len(kb)) + kb + struct.pack("<i", len(vb)) + vb
    meta_sec = struct.pack("<QQ", 8 + len(meta), len(metadata)) + meta
    index_len = 8 + sum(1 + len(n.encode("utf-8")) + 16 for n in names)
    data_start = 26 + len(meta_sec) + 8 + index_len + 8
    index = b""
    off = data_start
    for n, sz in zip(names, sizes):
        nb = n.encode("utf-8")
        index += bytes([len(nb)]) + nb + struct.pack("<QQ", sz, off)
        off += sz
    mask = os.urandom(16)
    tmp = path + ".chc-tmp"
    try:
        _write_sng_tmp(tmp, src, mask, meta_sec, names, sizes, files, index_len, index)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_sng_tmp(tmp, src, mask, meta_sec, names, sizes, files, index_len, index):
    with open(tmp, "wb") as out:
        out.write(b"SNGPKG" + struct.pack("<I", 1) + mask)
        out.write(meta_sec)
        out.write(struct.pack("<QQ", index_len, len(names)) + index)
        out.write(struct.pack("<Q", sum(sizes)))
        for n in names:
            v = files[n]
            if isinstance(v, (bytes, bytearray)):
                out.write(_xor(bytes(v), mask))
            else:
                e = src.files[v]
                with open(src.path, "rb") as f:
                    f.seek(e.offset)
                    raw = f.read(e.size)
                # un-mask with the source key, re-mask with ours
                plain = _xor(raw, src.xor_mask)
                out.write(_xor(plain, mask))


def _xor(data: bytes, mask: bytes) -> bytes:
    # (i & 0xFF) and mask[i & 15] repeat every 256 bytes -> xor with a 256-byte key stream
    key = bytes(mask[i & 15] ^ (i & 0xFF) for i in range(256))
    full = key * (len(data) // 256 + 1)
    return (int.from_bytes(data, "little") ^ int.from_bytes(full[:len(data)], "little")).to_bytes(len(data), "little") if data else b""
