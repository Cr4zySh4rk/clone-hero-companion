"""Keep yt-dlp current without a new app release (YouTube breaks it every few weeks).

The newest yt-dlp wheel from PyPI (pure Python) is verified against its SHA-256,
unpacked into <config>/yt-dlp/<version>/ and put first on sys.path at the next start
(approach from BackstageHero). Only used by the packaged app."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import threading
import time
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


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in v.replace("-", ".").split(".") if x.isdigit())


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


def check_for_update():
    stamp = _root() / "last_check"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 20 * 3600:
            return
        stamp.write_text(str(time.time()))
        with urlopen(urllib.request.Request(PYPI, headers={"User-Agent": "CloneHeroCompanion"}), timeout=20) as r:
            data = json.loads(r.read().decode())
        latest = data["info"]["version"]
        have = max([bundled_version()] + [d.name for d in _root().iterdir()
                                          if d.is_dir() and (d / "yt_dlp_ejs").is_dir()], key=_ver)
        if _ver(latest) <= _ver(have):
            return
        wheel = next(u for u in data["urls"] if u["filename"].endswith("py3-none-any.whl"))
        with urlopen(urllib.request.Request(wheel["url"], headers={"User-Agent": "CloneHeroCompanion"}), timeout=120) as r:
            blob = r.read()
        if hashlib.sha256(blob).hexdigest() != wheel["digests"]["sha256"]:
            log.warning("yt-dlp update failed its checksum; ignored")
            return
        tmp = _root() / f".{latest}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        whl = tmp / "w.whl"
        whl.write_bytes(blob)
        with zipfile.ZipFile(whl) as z:
            z.extractall(tmp)
        whl.unlink()
        _fetch_ejs(data["info"].get("requires_dist") or [], tmp)
        dst = _root() / latest
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
        for old in _root().iterdir():   # keep the two newest
            if old.is_dir() and old.name not in (latest, have) and not old.name.startswith("."):
                shutil.rmtree(old, ignore_errors=True)
        log.info("Downloaded yt-dlp %s; it will be used from the next start", latest)
    except Exception as e:
        log.debug("yt-dlp update check failed: %s", e)


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


def start_background_check():
    if getattr(sys, "frozen", False):
        threading.Thread(target=check_for_update, daemon=True, name="ytdlp-update").start()
