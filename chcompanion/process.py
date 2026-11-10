"""Launch background tools without allocating Windows console windows."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from . import cancel as _cancel

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


def terminate(proc, grace: float = 3.0) -> None:
    """Stop a process: terminate, then kill if it is still there after the grace period."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(10)
        except Exception:
            pass
    except OSError:
        pass


@contextmanager
def kill_on_cancel(proc, cancel=None):
    """While the block runs, terminate proc (then kill) as soon as the job's cancel Event is set.
    The caller sees the process end (its pipes close) and checks cancel.check() afterwards."""
    cancel = cancel or _cancel.current()
    done = threading.Event()
    if cancel is not None:
        def watch():
            while not done.is_set():
                if cancel.wait(0.1):
                    if not done.is_set():
                        terminate(proc)
                    return
        threading.Thread(target=watch, daemon=True, name="proc-cancel").start()
    try:
        yield
    finally:
        done.set()


def _run_cancellable(args, options):
    timeout = options.pop("timeout", None)
    check = options.pop("check", False)
    data = options.pop("input", None)
    if options.pop("capture_output", False):
        options["stdout"] = options["stderr"] = subprocess.PIPE
    if data is not None:
        options["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(args, **options)
    try:
        with kill_on_cancel(proc):
            try:
                out, err = proc.communicate(data, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            except BaseException:
                proc.kill()
                raise
    finally:
        if proc.poll() is None:
            proc.kill()
    _cancel.check()
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, args, out, err)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def run(args, **options):
    exe = str(args[0])
    _cancel.check()
    _cancel.acquire(_SLOTS)
    try:
        with _FAILURE_LOCK:
            failure = _FAILED_STARTS.get(exe)
        if failure:
            raise ToolStartError(failure)
        if _cancel.current() is not None:
            result = _run_cancellable(args, _options(options))
        else:
            result = subprocess.run(args, **_options(options))
    finally:
        _SLOTS.release()
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
