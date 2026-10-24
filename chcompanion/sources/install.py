"""Install downloaded charts into the Songs folder: .sng files as-is, archives
(.zip, .7z, .rar when an extractor is available) and loose folders are unpacked and
every song folder found inside is moved into place."""
from __future__ import annotations

import logging
import re
import shutil
from ..process import run as run_process
import tempfile
import zipfile
from pathlib import Path

from ..paths import cache_dir, ensure_writable_dir
from . import hosts

log = logging.getLogger(__name__)
MARKERS = {"song.ini", "notes.chart", "notes.mid"}


def safe_name(s: str) -> str:
    s = re.sub(r"<[^>]*>", "", s or "")
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip().rstrip(".")
    return s[:120] or "Unknown"


class AlreadyInstalled(Exception):
    """The chart is already in the Songs folder (we don't make numbered duplicates)."""


def unique(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf = (p.stem, p.suffix) if p.is_file() or p.suffix == ".sng" else (p.name, "")
    i = 2
    while True:
        q = p.with_name(f"{stem} ({i}){suf}")
        if not q.exists():
            return q
        i += 1


def _extract(archive: Path, out: Path):
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                target = (out / m.filename).resolve()
                if not str(target).startswith(str(out.resolve())):
                    continue  # zip-slip
                z.extract(m, out)
        return
    if name.endswith(".7z"):
        try:
            import py7zr
            with py7zr.SevenZipFile(archive) as z:
                z.extractall(out)
            return
        except ImportError:
            pass
    if name.endswith((".rar", ".7z")):
        for tool in (["unar", "-q", "-o", str(out), str(archive)], ["bsdtar", "-xf", str(archive), "-C", str(out)],
                     ["7z", "x", "-y", f"-o{out}", str(archive)], ["unrar", "x", "-y", str(archive), str(out) + "/"]):
            exe = shutil.which(tool[0])
            if exe:
                # Use the resolved executable, including its Windows extension.
                try:
                    r = run_process([exe, *tool[1:]], capture_output=True)
                except FileNotFoundError:
                    continue  # stale PATH entry: try the next extractor
                if r.returncode == 0:
                    return
        raise RuntimeError(f"Can't extract {archive.suffix} archives here; install 'unar' or 7-Zip")
    raise RuntimeError(f"Unknown archive type: {archive.name}")


def _song_dirs(root: Path) -> list[Path]:
    found = []
    for p in [root, *root.rglob("*")]:
        if p.is_dir() and any((p / m).exists() for m in MARKERS):
            if not any(str(p).startswith(str(f) + "/") or str(p).startswith(str(f) + "\\") for f in found):
                found.append(p)
    return found


def install_files(files: list[Path], songs_dir: Path, meta: dict) -> list[Path]:
    installed: list[Path] = []
    skipped: list[Path] = []
    songs_dir.mkdir(parents=True, exist_ok=True)
    label = safe_name(f"{meta.get('artist', '')} - {meta.get('name', '')}"
                      + (f" ({meta['charter']})" if meta.get("charter") else ""))
    work = Path(tempfile.mkdtemp(prefix="chc-x-", dir=cache_dir()))
    try:
        loose = work / "loose"
        for f in files:
            low = f.name.lower()
            if low.endswith(".sng"):
                dst = songs_dir / (label + ".sng")
                if dst.exists():
                    # Clone Hero shows only one of a set of duplicate charts (and only knows about
                    # a video in the copy it shows), so never add a numbered second copy
                    skipped.append(dst)
                    continue
                shutil.move(str(f), dst)
                installed.append(dst)
            elif low.endswith((".zip", ".7z", ".rar")):
                # Archive names can be very long. Repeating them in the extraction
                # path pushes otherwise valid charts over Windows' path limit.
                out = work / "extract"
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir(parents=True)
                _extract(f, out)
                for sng in out.rglob("*.sng"):
                    dst = songs_dir / safe_name(sng.name)
                    if dst.exists():
                        skipped.append(dst)
                        continue
                    shutil.move(str(sng), dst)
                    installed.append(dst)
                for d in _song_dirs(out):
                    name = d.name if d != out else label
                    dst = songs_dir / safe_name(name)
                    if dst.exists():
                        skipped.append(dst)
                        continue
                    shutil.move(str(d), dst)
                    installed.append(dst)
            else:  # loose file (a Drive folder of song files)
                loose.mkdir(exist_ok=True)
                shutil.move(str(f), loose / f.name)
        if loose.exists() and any((loose / m).exists() for m in MARKERS):
            dst = songs_dir / label
            if dst.exists():
                skipped.append(dst)
            else:
                shutil.move(str(loose), dst)
                installed.append(dst)
        if not installed and skipped:
            raise AlreadyInstalled(", ".join(p.name for p in skipped))
        if not installed:
            raise RuntimeError("No chart found in the download (needs song.ini / notes.chart / notes.mid or a .sng)")
        return installed
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install(item: dict, songs_dir: Path, progress=None, cancel=None) -> list[Path]:
    """item: a normalized search result (encore / rhythmverse) or {url, name, artist}."""
    if item.get("needsConversion"):
        raise RuntimeError("Rock Band CON charts need conversion, which isn't supported; pick a Clone Hero version")
    url = item.get("downloadUrl") or item.get("url")
    if not url:
        raise hosts.ManualDownload("No direct download for this chart; open its page to download it")
    if item.get("source") == "encore":
        label = safe_name(f"{item.get('artist', '')} - {item.get('name', '')}"
                          + (f" ({item['charter']})" if item.get("charter") else ""))
        if (songs_dir / (label + ".sng")).exists():
            raise AlreadyInstalled(label + ".sng")
    if progress:
        progress("checking the Songs folder is writable")
    ensure_writable_dir(songs_dir)
    if progress:
        progress("creating the download folder")
    tmp = Path(tempfile.mkdtemp(prefix="chc-dl-", dir=cache_dir()))
    try:
        def prog(done, total):
            if progress:
                progress(f"downloading {done * 100 // total}%" if total else f"downloading {done // 1048576} MB")
        if progress:
            progress("connecting to the chart download")
        files = hosts.download(url, tmp, prog, cancel)
        if item.get("source") == "encore" and files and not files[0].name.lower().endswith(".sng"):
            if progress:
                progress("renaming the downloaded chart")
            files[0] = files[0].rename(files[0].with_suffix(".sng"))
        if progress:
            progress(f"installing into {songs_dir}")
        # Drive folders come back as nested paths: treat the folder as one song
        all_files = []
        for f in files:
            all_files.append(f)
        if len(files) > 1 and not any(f.name.lower().endswith((".zip", ".7z", ".rar", ".sng")) for f in files):
            folder = tmp / "folder_song"
            folder.mkdir(exist_ok=True)
            for f in files:
                shutil.move(str(f), folder / f.name)
            dst = songs_dir / safe_name(f"{item.get('artist', '')} - {item.get('name', '')}")
            if dst.exists():
                raise AlreadyInstalled(dst.name)
            shutil.move(str(folder), dst)
            return [dst]
        return install_files(all_files, songs_dir, item)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
