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


def _bundle_dirs() -> list[str]:
    """Where this app's own files live (PyInstaller's _internal dir, the AppImage mount)."""
    dirs = [getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable), os.environ.get("APPDIR")]
    return [os.path.realpath(d) for d in dirs if d]


def host_env() -> dict | None:
    """Linux AppImage/PyInstaller: the bundle's libraries are first on LD_LIBRARY_PATH. Programs of the host
    (browser, xdg-open, notify-send, a system ffmpeg, Clone Hero) must not load them, so give them the
    original environment. None means "inherit as is" (every other platform, or running from source).

    PyInstaller only saves LD_LIBRARY_PATH_ORIG when the variable existed before the app started; on a normal
    desktop it doesn't, so the bundle's entries have to be removed from LD_LIBRARY_PATH itself (2.11.1:
    Linux Mint, "Sign in" still opened no browser)."""
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return None
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        orig = env.pop("LD_LIBRARY_PATH_ORIG")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        return env
    bundle = _bundle_dirs()

    def ours(entry: str) -> bool:
        if not entry:
            return True
        real = os.path.realpath(entry)
        return any(real == b or real.startswith(b + os.sep) for b in bundle)

    keep = [e for e in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if not ours(e)]
    if keep:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(keep)
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


# Set by the bundle (PyInstaller, Qt WebEngine, our AppRun) for this app only; a browser must not inherit them.
_BUNDLE_ONLY_VARS = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QTWEBENGINEPROCESS_PATH", "QTWEBENGINE_CHROMIUM_FLAGS",
                     "QTWEBENGINE_RESOURCES_PATH", "QTWEBENGINE_LOCALES_PATH", "QT_QPA_PLATFORM", "PYTHONHOME",
                     "PYTHONPATH", "SSL_CERT_FILE", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                     "_PYI_PARENT_PROCESS_LEVEL", "_PYI_LINUX_PROCESS_NAME")


def _browser_env() -> dict | None:
    env = host_env()
    if env is None and not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return None
    env = dict(env if env is not None else os.environ)
    for k in _BUNDLE_ONLY_VARS:
        env.pop(k, None)
    return env


def open_url(url: str) -> bool:
    """Open a web page in the user's default browser. Every "open in browser" in the app goes through here.

    Linux: Python's webbrowser launches the browser with this process's environment, and in the AppImage that
    means the bundle's Qt/OpenSSL libraries on LD_LIBRARY_PATH: the browser then fails to start and nothing
    happens (2.11.0: "Sign in" opened no browser). So run the desktop's own opener with the host environment,
    check it actually worked, and only then fall back to webbrowser."""
    import logging
    log = logging.getLogger(__name__)
    if sys.platform.startswith("linux"):
        env = _browser_env()
        for cmd in (["xdg-open", url], ["gio", "open", url], ["kde-open5", url], ["kde-open", url],
                    ["gnome-open", url], ["sensible-browser", url], ["x-www-browser", url]):
            try:
                p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            except (OSError, ValueError):
                continue                       # not installed
            try:
                code = p.wait(3)
            except subprocess.TimeoutExpired:
                return True                    # still running: it is the browser itself (sensible-browser etc.)
            if code == 0:
                return True
            log.info("%s could not open the link (exit %s); trying the next opener", cmd[0], code)
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception as e:                     # noqa: BLE001
        log.warning("Couldn't open %s in a browser: %s", url, e)
        return False


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
