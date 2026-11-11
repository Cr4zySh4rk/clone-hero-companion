"""Mandatory update check and self-update.

On launch and every 6 hours the app asks GitHub for the latest release. When it is newer than this build, the UI shows
a full-window update screen and the HTTP API refuses state-changing calls with 426 (see app.Handler.do_POST) until the
user has updated. Offline users are never blocked. `CHC_SKIP_UPDATE_CHECK=1` turns the whole thing off.

The update itself is per OS: Windows runs the release's .msi, macOS swaps the .app from the .dmg, Linux replaces the
AppImage (or the files of a tar.gz install). Anything that can't be replaced falls back to opening the download."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, net, process
from .i18n import _, msg_of

log = logging.getLogger(__name__)

REPO = "Cr4zySh4rk/clone-hero-companion"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_URL = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
CHECK_EVERY_S = 6 * 3600
CHECK_TIMEOUT_S = 15.0
RETRY_AFTER_S = (20, 60, 180, 600, 1800)   # after a failed check: retry soon, then back off (then every 30 min)
LAST_ERROR = ""                             # why the last check failed, for the banner tooltip and doctor
SKIP_ENV = "CHC_SKIP_UPDATE_CHECK"
UA = f"CloneHeroCompanion/{__version__} (+https://github.com/{REPO})"

# Asset names as the release workflow publishes them. Only used when the API is unavailable and the tag came from the
# redirect fallback, so the real asset list is unknown.
GUESSED = {"msi": "CloneHeroCompanion-windows-x64.msi", "dmg": "CloneHeroCompanion-macOS-universal.dmg",
           "targz": "CloneHeroCompanion-linux-x64.tar.gz", "appimage": "CloneHeroCompanion-linux-x64.AppImage"}


def disabled() -> bool:
    return os.environ.get(SKIP_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------------------------- versions
_SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(tag) -> tuple | None:
    """Semantic-version sort key for 'v2.5.0' / '2.5.0-rc.1+build', or None when it isn't a semver. A release sorts
    above any pre-release of the same x.y.z, and numeric pre-release parts sort numerically (rc.2 < rc.10)."""
    m = _SEMVER.match(str(tag or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return int(major), int(minor), int(patch), 0 if pre else 1, ids


def is_prerelease_tag(tag) -> bool:
    k = parse_version(tag)
    return bool(k and k[3] == 0)


def is_newer(latest, current=None) -> bool:
    a, b = parse_version(latest), parse_version(current or __version__)
    return bool(a and b and a > b)


def plain_version(tag) -> str:
    return re.sub(r"^v", "", str(tag or "").strip())


# ---------------------------------------------------------------------------------------------- fetching
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _redirect_location(url: str, timeout: float) -> str | None:
    opener = urllib.request.build_opener(_NoRedirect, *net.handlers())
    try:
        opener.open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=timeout).close()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
        raise
    return None


def _api_release(timeout: float) -> dict | None:
    req = urllib.request.Request(API_URL, headers={"User-Agent": UA, "Accept": "application/vnd.github+json",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    with net.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(4_000_000).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("unexpected API response")
    return data


def _normalize(data: dict) -> dict | None:
    """A usable release from the API payload; None for drafts, pre-releases and unparsable tags."""
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or not parse_version(tag) or is_prerelease_tag(tag):
        return None
    assets = []
    for a in data.get("assets") or []:
        name, url = str(a.get("name") or ""), str(a.get("browser_download_url") or "")
        if name and url.startswith(DOWNLOAD_PREFIX):
            assets.append({"name": Path(name).name, "url": url, "size": int(a.get("size") or 0),
                           "digest": str(a.get("digest") or "")})
    return {"tag": tag, "version": plain_version(tag), "notes": str(data.get("body") or ""),
            "url": str(data.get("html_url") or PAGE_URL), "assets": assets, "source": "api"}


def _short(e: BaseException) -> str:
    return (str(getattr(e, "reason", "") or e) or type(e).__name__)[:160]


def fetch_latest(timeout: float = CHECK_TIMEOUT_S) -> tuple[str, dict | None]:
    """('ok', release|None) or ('offline', None). Tries the API, then the github.com redirect. A None release means
    the repo has no usable (non-draft, non-prerelease) release."""
    try:
        return "ok", _normalize(_api_release(timeout) or {})
    except Exception as e:                                     # rate limit (403/429), 5xx, DNS, TLS, bad JSON...
        log.info("Update check via the API failed (%s); trying the release page", e)
        api_err = e
    try:
        loc = _redirect_location(PAGE_URL, timeout)
    except Exception as e:
        log.info("Update check via the release page failed (%s)", e)
        global LAST_ERROR
        LAST_ERROR = f"{_short(api_err)} / {_short(e)}"
        return "offline", None
    m = re.search(r"/releases/tag/([^/?#]+)", loc or "")
    if not m:
        return "ok", None
    tag = urllib.parse.unquote(m.group(1))
    if not parse_version(tag) or is_prerelease_tag(tag):
        return "ok", None
    return "ok", {"tag": tag, "version": plain_version(tag), "notes": "", "url": f"https://github.com/{REPO}/releases/tag/{tag}",
                  "assets": [], "source": "redirect"}


def check_once(timeout: float = CHECK_TIMEOUT_S) -> dict:
    """One-shot check for the CLI: {'state': 'skipped'|'current'|'available'|'offline', 'latest': version|None}."""
    if disabled():
        return {"state": "skipped", "latest": None}
    status, rel = fetch_latest(timeout)
    if status != "ok":
        return {"state": "offline", "latest": None}
    if rel and is_newer(rel["tag"]):
        return {"state": "available", "latest": rel["version"]}
    return {"state": "current", "latest": rel["version"] if rel else None}


# ---------------------------------------------------------------------------------------------- assets
def asset_kind(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".msi"):
        return "msi"
    if n.endswith(".dmg"):
        return "dmg"
    if n.endswith(".appimage"):
        return "appimage"
    if n.endswith((".tar.gz", ".tgz")):
        return "targz"
    return None


def _wrong_arch(name: str, machine: str) -> bool:
    n, arm = name.lower(), machine.lower() in ("aarch64", "arm64")
    has_arm = any(t in n for t in ("arm64", "aarch64"))
    has_x86 = any(t in n for t in ("x64", "x86_64", "amd64"))
    return (arm and has_x86 and not has_arm) or (not arm and has_arm)


def pick_assets(assets: list[dict], plat: str, appimage: bool = False, machine: str | None = None) -> list[dict]:
    """Candidate installers for this OS, best first. On Linux an AppImage install prefers the AppImage and anything
    else prefers the tar.gz; the other format is kept as a last resort (it can only be downloaded, not installed)."""
    want = ["msi"] if plat == "win32" else ["dmg"] if plat == "darwin" else (["appimage", "targz"] if appimage else ["targz", "appimage"])
    machine = machine or platform.machine()
    out = []
    for kind in want:
        out += sorted((a for a in assets if asset_kind(a["name"]) == kind), key=lambda a: _wrong_arch(a["name"], machine))
    return out


def guess_assets(tag: str, plat: str, appimage: bool = False) -> list[dict]:
    kinds = ["msi"] if plat == "win32" else ["dmg"] if plat == "darwin" else (["appimage", "targz"] if appimage else ["targz", "appimage"])
    return [{"name": GUESSED[k], "url": f"{DOWNLOAD_PREFIX}{tag}/{GUESSED[k]}", "size": 0, "digest": "", "guessed": True}
            for k in kinds]


# ---------------------------------------------------------------------------------------------- install commands
DETACHED_FLAGS = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


def windows_command(msi: Path) -> list[str]:
    return ["msiexec", "/i", str(msi)]


def mac_attach_command(dmg: Path, mountpoint: Path) -> list[str]:
    return ["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-noautoopen", "-mountpoint", str(mountpoint), str(dmg)]


def mac_ditto_command(src: Path, dst: Path) -> list[str]:
    return ["ditto", str(src), str(dst)]


def mac_relaunch_command(bundle: Path) -> list[str]:
    return ["/bin/sh", "-c", 'sleep 2; open -n "$0"', str(bundle)]


def linux_relaunch_command(exe: Path) -> list[str]:
    return ["/bin/sh", "-c", 'sleep 2; exec "$0"', str(exe)]


class UpdateError(Exception):
    """Carries a translatable message."""

    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg

    def as_msg(self):
        return self.msg


class _Manual(Exception):
    """The update can't be applied in place; the download is opened for the user instead."""


# ---------------------------------------------------------------------------------------------- the updater
class Updater:
    def __init__(self, quit_event: threading.Event | None = None, current: str | None = None):
        self.quit_event = quit_event or threading.Event()
        self.current = current or __version__
        self.status = "unknown"           # unknown | current | available | offline | disabled
        self.release: dict | None = None
        self.checked_at = 0.0
        self.checking = False
        self.install = self._idle()
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._installing = False
        self._stop = threading.Event()
        self.quit_delay = 1.5

    @staticmethod
    def _idle() -> dict:
        return {"phase": "idle", "done": 0, "total": 0, "message": None, "error": None}

    # -------------------------------------------------------- state
    @property
    def required(self) -> bool:
        return self.status == "available" and not disabled()

    @property
    def latest_version(self) -> str | None:
        return self.release["version"] if self.release else None

    @property
    def release_url(self) -> str:
        return (self.release or {}).get("url") or PAGE_URL

    def public(self) -> dict:
        with self._lock:
            status = "disabled" if disabled() else self.status
            rel = self.release if status == "available" else None
            return {"status": status, "required": self.required, "current": self.current,
                    "latest": rel["version"] if rel else None, "notes": rel["notes"] if rel else "",
                    "url": rel["url"] if rel else PAGE_URL, "checkedAt": self.checked_at, "checking": self.checking,
                    "error": LAST_ERROR if status == "offline" else "",
                    "install": dict(self.install)}

    def gate_payload(self) -> dict:
        """The body of the 426 response."""
        return {"updateRequired": True, "latest": self.latest_version, "current": self.current, "url": self.release_url,
                "error": _("api.update_required", latest=self.latest_version or "")}

    # -------------------------------------------------------- checking
    def check(self) -> dict:
        if disabled():
            return self.public()
        if not self._check_lock.acquire(blocking=False):      # a check is already running: wait for it, reuse its result
            with self._check_lock:
                return self.public()
        try:
            self.checking = True
            state, rel = fetch_latest()
            with self._lock:
                self.checked_at = time.time()
                if state != "ok":
                    if self.status != "available":            # never unblock a user who already knows about an update
                        self.status = "offline"
                elif rel and is_newer(rel["tag"], self.current):
                    self.status, self.release = "available", rel
                else:
                    self.status, self.release = "current", rel
        except Exception:
            log.exception("update check crashed")
            with self._lock:
                if self.status != "available":
                    self.status = "offline"
        finally:
            self.checking = False
            self._check_lock.release()
        return self.public()

    def start(self) -> None:
        """Check now and every 6 hours in a background thread."""
        if disabled():
            self.status = "disabled"
            return

        def loop():
            fails = 0
            while not self._stop.is_set():
                ok = self.check().get("status") != "offline"
                fails = 0 if ok else fails + 1
                self._stop.wait(CHECK_EVERY_S if ok else RETRY_AFTER_S[min(fails, len(RETRY_AFTER_S)) - 1])
        threading.Thread(target=loop, daemon=True, name="update-check").start()

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------- installing
    def _progress(self, **kw) -> None:
        with self._lock:
            self.install.update(kw)

    def start_install(self) -> bool:
        with self._lock:
            if not self.required:
                return False
            if self._installing:
                return True
            self._installing = True
            # report progress at once: the reply to the click must already show the download starting,
            # otherwise the screen looks unchanged for seconds and people click again (2.7.0)
            self.install = dict(self._idle(), phase="downloading", message=_("update.p.downloading"))
        threading.Thread(target=self._run_install, daemon=True, name="update-install").start()
        return True

    def _run_install(self) -> None:
        try:
            {"win32": self._install_windows, "darwin": self._install_macos}.get(sys.platform, self._install_linux)()
            from . import telemetry
            telemetry.event("update_install", result="ok")
            telemetry.get().flush(2.0)
        except UpdateError as e:
            from . import telemetry
            telemetry.event("update_install", result="fail")
            self._progress(phase="error", error=e.msg, message=None)
        except Exception as e:
            from . import telemetry
            telemetry.event("update_install", result="fail")
            log.exception("update failed")
            self._progress(phase="error", error=_("update.err.install", reason=msg_of(e)), message=None)
        finally:
            self._installing = False

    def _assets(self) -> list[dict]:
        rel = self.release or {}
        assets = rel.get("assets") or []
        if not assets:                                          # found through the redirect: try the API for the asset list
            try:
                again = _normalize(_api_release(CHECK_TIMEOUT_S) or {})
                if again and again["tag"] == rel.get("tag"):
                    assets = again["assets"]
            except Exception:
                pass
        return assets

    def _candidates(self, plat: str) -> list[dict]:
        appimage = bool(os.environ.get("APPIMAGE"))
        real = self._assets()
        cands = pick_assets(real, plat, appimage) if real else guess_assets(self.release["tag"], plat, appimage)
        if not cands:
            raise UpdateError(_("update.err.no_asset"))
        return cands

    def _download(self, cands: list[dict], dest_dir: Path) -> tuple[Path, dict]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for i, a in enumerate(cands):
            try:
                return self._download_one(a, dest_dir), a
            except urllib.error.HTTPError as e:
                if e.code == 404 and a.get("guessed") and i < len(cands) - 1:
                    continue                                    # a guessed name that isn't in this release
                raise UpdateError(_("update.err.download", reason=f"HTTP {e.code}"))
            except UpdateError:
                raise
            except (OSError, ValueError) as e:                 # URLError, timeouts, disk errors
                raise UpdateError(_("update.err.download", reason=str(getattr(e, "reason", None) or e)))
        raise UpdateError(_("update.err.no_asset"))

    def _download_one(self, a: dict, dest_dir: Path) -> Path:
        if not a["url"].startswith(DOWNLOAD_PREFIX):
            raise UpdateError(_("update.err.download", reason="unexpected download address"))
        dest = dest_dir / Path(a["name"]).name
        part = dest.with_name(dest.name + ".part")
        self._progress(phase="downloading", done=0, total=int(a.get("size") or 0), message=_("update.p.downloading"), error=None)
        sha = hashlib.sha256()
        with net.urlopen(urllib.request.Request(a["url"], headers={"User-Agent": UA}), timeout=30) as r:
            header_len = int(r.headers.get("Content-Length") or 0)
            self._progress(total=header_len or int(a.get("size") or 0))
            done = 0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    self._progress(done=done)
        self._progress(phase="verifying", message=_("update.p.verifying"))
        for expected in {int(a.get("size") or 0), header_len} - {0}:
            if done != expected:
                part.unlink(missing_ok=True)
                raise UpdateError(_("update.err.size", got=done, expected=expected))
        digest = a.get("digest") or ""
        if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
            part.unlink(missing_ok=True)
            raise UpdateError(_("update.err.checksum"))
        os.replace(part, dest)
        return dest

    def _quit_soon(self) -> None:
        self._progress(phase="done")
        threading.Timer(self.quit_delay, self.quit_event.set).start()

    def _manual(self, message, opener: list[str]) -> None:
        try:
            process.popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.warning("could not open the download: %s", e)
        self._progress(phase="manual", message=message, error=None)

    # ---- Windows
    def _install_windows(self) -> None:
        path, _a = self._download(self._candidates("win32"), Path(tempfile.gettempdir()) / "CloneHeroCompanion-update")
        self._progress(phase="installing", message=_("update.p.installing_msi"))
        process.popen(windows_command(path), creationflags=DETACHED_FLAGS, close_fds=True,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._quit_soon()                                       # the installer can't replace files while we run

    # ---- macOS
    def _install_macos(self) -> None:
        from . import macperm
        dmg, _a = self._download(self._candidates("darwin"), Path(tempfile.gettempdir()) / "CloneHeroCompanion-update")
        target = macperm.app_bundle()
        if target is None:
            return self._manual(_("update.p.manual_mac"), ["open", str(dmg)])
        self._progress(phase="installing", message=_("update.p.installing"))
        mount = Path(tempfile.mkdtemp(prefix="chc-update-mnt-"))
        attached = False
        try:
            r = process.run(mac_attach_command(dmg, mount), capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise UpdateError(_("update.err.install", reason=(r.stderr or "hdiutil attach failed").strip()))
            attached = True
            apps = sorted(mount.glob("*.app"))
            if not apps:
                raise UpdateError(_("update.err.install", reason="no app in the disk image"))
            try:
                self._swap_bundle(apps[0], target)
            except OSError as e:                                # no permission to write next to the app, disk full...
                log.warning("could not replace %s: %s", target, e)
                return self._manual(_("update.p.manual_mac"), ["open", str(dmg)])
        finally:
            if attached:
                process.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True, timeout=60)
            shutil.rmtree(mount, ignore_errors=True)
        dmg.unlink(missing_ok=True)
        self._progress(phase="installing", message=_("update.p.restarting"))
        process.popen(mac_relaunch_command(target), start_new_session=True,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._quit_soon()

    @staticmethod
    def _swap_bundle(src: Path, target: Path) -> None:
        """ditto the new app next to the old one (same volume), then swap them; roll back if the swap fails."""
        staged, old = target.with_name(f".{target.name}.new"), target.with_name(f".{target.name}.old")
        for p in (staged, old):
            shutil.rmtree(p, ignore_errors=True)
        r = process.run(mac_ditto_command(src, staged), capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            shutil.rmtree(staged, ignore_errors=True)
            raise OSError((r.stderr or "ditto failed").strip())
        try:
            os.rename(target, old)
            try:
                os.rename(staged, target)
            except OSError:
                os.rename(old, target)
                raise
        except OSError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        shutil.rmtree(old, ignore_errors=True)

    # ---- Linux
    def _install_linux(self) -> None:
        downloads = Path.home() / "Downloads"
        path, asset = self._download(self._candidates(sys.platform), downloads if downloads.is_dir() else Path(tempfile.gettempdir()) / "CloneHeroCompanion-update")
        self._progress(phase="installing", message=_("update.p.installing"))
        try:
            kind = asset_kind(asset["name"])
            appimage = os.environ.get("APPIMAGE")
            if kind == "appimage" and appimage:
                exe = self._swap_appimage(path, Path(appimage))
            elif kind == "targz" and getattr(sys, "frozen", False):
                exe = self._swap_targz(path, Path(sys.executable))
            else:
                raise _Manual()
        except (_Manual, OSError, tarfile.TarError) as e:
            log.warning("could not install the update in place: %s", e or "unsupported install type")
            return self._manual(_("update.p.manual_linux", path=str(path.parent)), ["xdg-open", str(path.parent)])
        path.unlink(missing_ok=True)
        self._progress(phase="installing", message=_("update.p.restarting"))
        env = {k: v for k, v in os.environ.items() if k not in ("APPDIR", "ARGV0", "OWD")}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"             # a onefile build must not inherit this process's unpack dir
        process.popen(linux_relaunch_command(exe), start_new_session=True, env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._quit_soon()

    @staticmethod
    def _swap_appimage(src: Path, target: Path) -> Path:
        tmp = target.with_name(f".{target.name}.new")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)                                 # the running copy keeps its old inode until we exit
        return target

    @staticmethod
    def _swap_targz(archive: Path, current_exe: Path) -> Path:
        """Extract next to the install and swap the top-level entries in (a onefile build is a single file, a onedir
        build a folder next to its executable). The old entries are kept until everything is in place."""
        dest = current_exe.parent
        stage = Path(tempfile.mkdtemp(prefix=".chc-update-", dir=dest))
        try:
            with tarfile.open(archive) as tf:
                try:
                    tf.extractall(stage, filter="data")
                except TypeError:                               # Python without extraction filters
                    for m in tf.getmembers():
                        if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                            raise tarfile.TarError(f"unsafe path in archive: {m.name}")
                    tf.extractall(stage)
            root = stage if (stage / current_exe.name).is_file() else None
            if root is None:
                subs = [p for p in stage.iterdir() if p.is_dir() and (p / current_exe.name).is_file()]
                root = subs[0] if len(subs) == 1 else None
            if root is None:
                raise UpdateError(_("update.err.layout"))
            moved, placed = [], []
            try:
                for entry in sorted(root.iterdir()):
                    tgt = dest / entry.name
                    if tgt.exists() or tgt.is_symlink():
                        bak = dest / (".chc-old-" + entry.name)
                        shutil.rmtree(bak, ignore_errors=True) if bak.is_dir() else bak.unlink(missing_ok=True)
                        os.rename(tgt, bak)
                        moved.append((tgt, bak))
                    os.rename(entry, tgt)
                    placed.append(tgt)
            except Exception:
                for tgt in placed:
                    shutil.rmtree(tgt, ignore_errors=True) if tgt.is_dir() else tgt.unlink(missing_ok=True)
                for tgt, bak in moved:
                    os.rename(bak, tgt)
                raise
            for _t, bak in moved:
                shutil.rmtree(bak, ignore_errors=True) if bak.is_dir() else bak.unlink(missing_ok=True)
            return dest / current_exe.name
        finally:
            shutil.rmtree(stage, ignore_errors=True)
