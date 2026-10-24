"""Launch background tools without allocating Windows console windows."""
from __future__ import annotations

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


def _options(options: dict) -> dict:
    options = dict(options)
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
        message = (
            f"Windows could not start {Path(exe).name} (0x{status:08X}). "
            f"Executable: {exe}. Restart Companion and repair/reinstall it using the Windows installer. "
            "Check Windows Security > Protection history if it still fails."
        )
        with _FAILURE_LOCK:
            _FAILED_STARTS[exe] = message
        raise ToolStartError(message)
    return result


def popen(args, **options):
    """Streaming callers own the process lifetime and must wait after terminating it."""
    return subprocess.Popen(args, **_options(options))
