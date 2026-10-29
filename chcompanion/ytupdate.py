"""Keep yt-dlp current without a new app release (YouTube breaks it every few weeks).

The newest yt-dlp wheel from PyPI (pure Python) is verified against its SHA-256,
unpacked into <config>/yt-dlp/<version>/ and put first on sys.path at the next start
(approach from BackstageHero), and hot-swapped in when no YouTube lookup is in flight, otherwise used
from the next start. Silent: network problems are only logged."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

from .net import urlopen
from .paths import config_dir

log = logging.getLogger(__name__)
PYPI = "https://pypi.org/pypi/yt-dlp/json"


def _root() -> Path:
    d = config_dir() / "yt-dlp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_stable(v: str) -> bool:
    """Release versions only (2026.10.22), never .dev / rc / post builds."""
    import re
    return bool(re.fullmatch(r"\d+(\.\d+)*", v or ""))


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in v.replace("-", ".").split(".") if x.isdigit())


def is_newer(latest: str, have: str) -> bool:
    return is_stable(latest) and _ver(latest) > _ver(have)


def bundled_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("yt-dlp")
    except Exception:
        return "0"


def activate_cached():
    """Put the newest downloaded yt-dlp first on sys.path (call before importing yt_dlp)."""
    if "yt_dlp" in sys.modules:
        return
    best = None
    for d in _root().iterdir():
        # a download without its matching yt-dlp-ejs (older app versions) would run the
        # new yt-dlp against the bundled solver scripts, which yt-dlp rejects on a version mismatch
        if d.is_dir() and (d / "yt_dlp" / "__init__.py").exists() and (d / "yt_dlp_ejs").is_dir():
            if best is None or _ver(d.name) > _ver(best.name):
                best = d
    if best and _ver(best.name) > _ver(bundled_version()):
        sys.path.insert(0, str(best))
        log.info("Using updated yt-dlp %s", best.name)


STATUS: dict = {"checking": False, "message": "", "latest": None}


def _get_json(url: str, timeout: int = 20) -> dict:
    with urlopen(urllib.request.Request(url, headers={"User-Agent": "CloneHeroCompanion"}), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str, timeout: int = 120) -> bytes:
    with urlopen(urllib.request.Request(url, headers={"User-Agent": "CloneHeroCompanion"}), timeout=timeout) as r:
        return r.read()


def latest_stable(data: dict) -> str | None:
    """Newest stable yt-dlp in a PyPI JSON answer (info.version first, then the release list)."""
    v = (data.get("info") or {}).get("version")
    if v and is_stable(v):
        return v
    stable = [k for k, files in (data.get("releases") or {}).items() if is_stable(k) and files]
    return max(stable, key=_ver) if stable else None


def active_info() -> dict:
    """The yt-dlp in use: its version and where it comes from ('bundled' or 'updated')."""
    mod = sys.modules.get("yt_dlp")
    try:
        if mod is not None:
            from yt_dlp.version import __version__ as ver
            where = str(Path(mod.__file__).resolve())
        else:
            ver, where = bundled_version(), ""
    except Exception:
        ver, where = bundled_version(), ""
    root = str(_root().resolve())
    return {"version": ver, "source": "updated" if where.startswith(root) else "bundled",
            "latest": STATUS.get("latest"), "message": STATUS.get("message", "")}


def cached_versions() -> list[str]:
    return sorted((d.name for d in _root().iterdir() if d.is_dir() and not d.name.startswith(".")
                   and (d / "yt_dlp_ejs").is_dir()), key=_ver)


def prune(keep: int = 2, protect: str | None = None):
    """Keep the `keep` newest cached versions (and the one in use)."""
    names = cached_versions()
    for n in names[:-keep] if keep else names:
        if n != protect:
            shutil.rmtree(_root() / n, ignore_errors=True)


def _verified(url: str, sha256: str) -> bytes:
    blob = _get_bytes(url)
    if hashlib.sha256(blob).hexdigest() != sha256:
        raise RuntimeError("download failed its SHA-256 check")
    return blob


def download(version: str, data: dict) -> Path:
    """Fetch yt-dlp `version` and its pinned yt-dlp-ejs, verified against PyPI's digests, into the cache."""
    wheel = next(u for u in data["urls"] if u["filename"].endswith("py3-none-any.whl"))
    blob = _verified(wheel["url"], wheel["digests"]["sha256"])
    tmp = _root() / f".{version}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        whl = tmp / "w.whl"
        whl.write_bytes(blob)
        with zipfile.ZipFile(whl) as z:
            z.extractall(tmp)
        whl.unlink()
        _fetch_ejs(data["info"].get("requires_dist") or [], tmp)
        dst = _root() / version
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
        return dst
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _purge_modules() -> dict:
    gone = {k: v for k, v in sys.modules.items() if k == "yt_dlp" or k.startswith(("yt_dlp.", "yt_dlp_ejs", "yt_dlp_plugins"))}
    for k in gone:
        del sys.modules[k]
    return gone


def hot_swap(newdir: Path, version: str, gate=None, downloads=None, wait: float = 10.0) -> bool:
    """Start using the downloaded yt-dlp now: with every YouTube lookup and download paused, drop the old
    yt_dlp modules, put `newdir` first on sys.path and import again, then check the version and that a
    YoutubeDL and the extractors work. Any failure restores the old modules and returns False (a swap that
    could not start because YouTube work is in flight also returns False: it applies at the next launch).
    Raises RuntimeError when the new package is broken (the caller deletes it)."""
    held: list = []
    try:
        for sem, n in ((gate.sem, gate.concurrent) if gate else (None, 0), (downloads, 2) if downloads else (None, 0)):
            for _ in range(n):
                if not sem.acquire(timeout=wait):
                    return False
                held.append(sem)
        old_path, old_mods = list(sys.path), _purge_modules()
        sys.path.insert(0, str(newdir))
        try:
            import importlib
            importlib.invalidate_caches()
            import yt_dlp
            from yt_dlp.version import __version__ as got
            if got != version:
                raise RuntimeError(f"expected yt-dlp {version}, imported {got}")
            with yt_dlp.YoutubeDL({"quiet": True}):
                pass
            from yt_dlp.extractor import gen_extractor_classes
            if not any(True for _ in gen_extractor_classes()):
                raise RuntimeError("no extractors")
        except Exception as e:
            _purge_modules()
            sys.path[:] = old_path
            sys.modules.update(old_mods)
            raise RuntimeError(f"updated yt-dlp does not load ({e}); kept the previous version") from e
        return True
    finally:
        for sem in held:
            sem.release()


def check_for_update(force: bool = False, gate=None, downloads=None) -> str:
    """Look for a newer stable yt-dlp and install it quietly. Returns a short status line; never raises."""
    STATUS["checking"] = True
    try:
        data = _get_json(PYPI)
        latest = latest_stable(data)
        STATUS["latest"] = latest
        have = active_info()["version"]
        cached = cached_versions()
        if cached:
            have = max([have, cached[-1]], key=_ver)
        if not latest or not is_newer(latest, have):
            STATUS["message"] = f"yt-dlp {have} is up to date"
            return STATUS["message"]
        dst = download(latest, data)
        try:
            swapped = hot_swap(dst, latest, gate, downloads)
        except RuntimeError as e:
            shutil.rmtree(dst, ignore_errors=True)
            log.warning("%s", e)
            STATUS["message"] = str(e)
            return STATUS["message"]
        prune(2, protect=latest)
        STATUS["message"] = (f"updated to yt-dlp {latest}" if swapped
                             else f"yt-dlp {latest} downloaded; used from the next start")
        log.info("%s", STATUS["message"])
        return STATUS["message"]
    except Exception as e:
        log.debug("yt-dlp update check failed: %s", e)
        STATUS["message"] = "couldn't check for a yt-dlp update"
        return STATUS["message"]
    finally:
        STATUS["checking"] = False


def _fetch_ejs(requires: list[str], into: Path):
    """The challenge-solver scripts yt-dlp pins (yt-dlp-ejs==X). Raises if they cannot be fetched, so a
    yt-dlp without its matching scripts is never activated."""
    import re
    pin = next((m.group(1) for r in requires if "extra ==" not in r or "'default'" in r
                for m in [re.match(r"yt-dlp-ejs\s*==\s*([\w.]+)", r)] if m), None)
    if not pin:
        raise RuntimeError("new yt-dlp does not pin yt-dlp-ejs")
    with urlopen(urllib.request.Request(f"https://pypi.org/pypi/yt-dlp-ejs/{pin}/json",
                                        headers={"User-Agent": "CloneHeroCompanion"}), timeout=20) as r:
        meta = json.loads(r.read().decode())
    wheel = next(u for u in meta["urls"] if u["filename"].endswith(".whl"))
    with urlopen(urllib.request.Request(wheel["url"], headers={"User-Agent": "CloneHeroCompanion"}), timeout=120) as r:
        blob = r.read()
    if hashlib.sha256(blob).hexdigest() != wheel["digests"]["sha256"]:
        raise RuntimeError("yt-dlp-ejs failed its checksum")
    whl = into / "e.whl"
    whl.write_bytes(blob)
    with zipfile.ZipFile(whl) as z:
        z.extractall(into)
    whl.unlink()


def _run_check(force: bool = False) -> str:
    from . import youtube
    return check_for_update(force, youtube.GATE, youtube.DOWNLOADS)


def start_background_check():
    """Every launch of a packaged app (not in tests): look, download and apply quietly."""
    import os
    if "pytest" in sys.modules or os.environ.get("CHC_NO_UPDATE_CHECK") or not getattr(sys, "frozen", False):
        return
    threading.Thread(target=_run_check, daemon=True, name="ytdlp-update").start()


def check_now_async():
    if not STATUS["checking"]:
        threading.Thread(target=_run_check, args=(True,), daemon=True, name="ytdlp-update").start()
