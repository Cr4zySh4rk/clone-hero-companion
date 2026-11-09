"""Launch background tools without allocating Windows console windows."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

# Library enrichment and download workers can all probe/convert at once.
# Bound short-lived helpers too, not just the expensive video encoders.
_SLOTS = threading.BoundedSemaphore(4)
_FAILED_STARTS: dict[str, str] = {}
_FAILURE_LOCK = threading.Lock()


class ToolStartError(RuntimeError):
    pass


def host_env() -> dict | None:
    """Linux AppImage/PyInstaller: the bundle's libraries are first on LD_LIBRARY_PATH. Programs of the host
    (browser, notify-send, a system ffmpeg) must not load them, so give them the original environment.
    None means "inherit as is" (every other platform, or running from source)."""
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return None
    if "LD_LIBRARY_PATH_ORIG" not in os.environ:
        return None
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG")
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _options(options: dict) -> dict:
    options = dict(options)
    if "env" not in options:
        env = host_env()
        if env is not None:
            options["env"] = env
    if sys.platform == "win32":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        if "input" not in options:
            options.setdefault("stdin", subprocess.DEVNULL)
    return options


def run(args, **options):
    exe = str(args[0])
    with _SLOTS:
        with _FAILURE_LOCK:
            failure = _FAILED_STARTS.get(exe)
        if failure:
            raise ToolStartError(failure)
        result = subprocess.run(args, **_options(options))
    # Windows may return the status either signed or unsigned. Keep normal
    # ffmpeg probe failures (exit 1) for callers that parse stderr.
    status = result.returncode & 0xFFFFFFFF
    if status in (0xC0000142, 0xC0000135, 0xC000007B):
        from .i18n import _
        message = _("proc.win_start", tool=Path(exe).name, status=f"{status:08X}", exe=exe)
        with _FAILURE_LOCK:
            _FAILED_STARTS[exe] = message
        raise ToolStartError(message)
    return result


def popen(args, **options):
    """Streaming callers own the process lifetime and must wait after terminating it."""
    return subprocess.Popen(args, **_options(options))
