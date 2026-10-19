"""macOS Full Disk Access helpers.

An app cannot grant itself Full Disk Access; the user has to switch it on in
System Settings → Privacy & Security → Full Disk Access. We can detect whether it
is on, open that settings pane directly, and restart ourselves afterwards (macOS
only applies the change to a fresh process)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
_cache: dict = {}


def applicable() -> bool:
    return sys.platform == "darwin"


def has_full_disk_access(fresh: bool = False) -> bool | None:
    """True/False on macOS, None elsewhere. Reading the TCC database needs Full Disk Access."""
    if not applicable():
        return None
    if not fresh and "fda" in _cache:
        return _cache["fda"]
    home = Path.home()
    probes = [home / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db",
              home / "Library" / "Safari" / "Bookmarks.plist",
              Path("/Library/Application Support/com.apple.TCC/TCC.db")]
    result = False
    for p in probes:
        try:
            with open(p, "rb") as f:
                f.read(16)
            result = True
            break
        except PermissionError:
            continue
        except OSError:
            continue
    _cache["fda"] = result
    return result


def app_bundle() -> Path | None:
    """The .app we run from (frozen builds only)."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    for p in exe.parents:
        if p.suffix == ".app":
            return p
    return None


def open_settings() -> bool:
    if not applicable():
        return False
    try:
        subprocess.Popen(["open", FDA_URL])
        b = app_bundle()
        if b:      # show the app in Finder so it can be dragged into the list if it isn't there
            subprocess.Popen(["open", "-R", str(b)])
        return True
    except Exception:
        return False


def relaunch(quit_event) -> bool:
    """Start a new copy of the app a moment after this one quits."""
    b = app_bundle()
    if not applicable() or b is None:
        return False
    subprocess.Popen(["/bin/sh", "-c", f'sleep 1.5; open -n "{b}"'], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    quit_event.set()
    return True


def protected_ok() -> bool:
    """May we look into Desktop/Documents/Downloads without causing prompts?"""
    return bool(has_full_disk_access()) or os.environ.get("CHC_SCAN_PROTECTED") == "1"
