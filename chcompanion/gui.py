"""Desktop window for the app: a native window (pywebview: WebKit on macOS, WebView2
on Windows, GTK/Qt on Linux) showing the local UI. Falls back to a Chrome/Edge
"app mode" window, then to the default browser."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import process

log = logging.getLogger(__name__)
TITLE = "Clone Hero Companion"
APP_ID = "CloneHeroCompanion"   # X11 window class / Wayland app_id; matches StartupWMClass in the .desktop file


class NativeWindowUnavailable(RuntimeError):
    """The native window cannot be started on this system (the message says why)."""


# --- Linux: pywebview's Qt backend (PySide6 + QtWebEngine, bundled in the AppImage) -------------------------

def _qt_dir() -> Path | None:
    import importlib.util
    try:
        spec = importlib.util.find_spec("PySide6")
    except (ImportError, ValueError):
        return None
    return Path(spec.origin).parent / "Qt" if spec and spec.origin else None


def _plugin_error(name: str) -> str | None:
    """None when the Qt platform plugin `name` ('xcb', 'wayland-generic') loads, else why not. Qt itself aborts the
    whole process on a missing platform library, so this has to be checked first (loading it here is harmless)."""
    import ctypes
    qt = _qt_dir()
    f = qt / "plugins" / "platforms" / f"libq{name}.so" if qt else None
    if f is None or not f.exists():
        return f"Qt platform plugin '{name}' is not installed"
    try:
        ctypes.CDLL(str(f))
    except OSError as e:
        return str(e)
    return None


XCB_HELP = ("Install the X11 libraries Qt needs: Debian/Ubuntu: sudo apt install libxcb-cursor0 libxcb-icccm4 "
            "libxcb-keysyms1 libxcb-shape0 libxkbcommon-x11-0; Fedora: sudo dnf install xcb-util-cursor "
            "xcb-util-wm xcb-util-keysyms libxkbcommon-x11; Arch: sudo pacman -S xcb-util-cursor xcb-util-wm "
            "xcb-util-keysyms libxkbcommon-x11")


def _sandbox_usable(root: bool | None = None, read=None) -> bool:
    """Chromium's sandbox needs unprivileged user namespaces, which Ubuntu 23.10+ restricts via AppArmor."""
    if root if root is not None else (hasattr(os, "geteuid") and os.geteuid() == 0):
        return False
    read = read or (lambda p: Path(p).read_text().strip() if Path(p).exists() else None)
    try:
        if read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") == "1":
            return False
        if read("/proc/sys/kernel/unprivileged_userns_clone") == "0":
            return False
        if read("/proc/sys/user/max_user_namespaces") == "0":
            return False
    except OSError:
        return False
    return True


def prepare_linux_qt(environ=None, plugin_error=None, sandbox_ok=None) -> str:
    """Pick the Qt backend and platform (Wayland or X11) and set the environment for it. Returns a short
    description such as 'qt/wayland'; raises NativeWindowUnavailable with a clear message otherwise.
    Returns '' (and changes nothing) when PySide6 is not installed, so pywebview may pick GTK."""
    env = os.environ if environ is None else environ
    if _qt_dir() is None and plugin_error is None:
        return ""
    plugin_error = plugin_error or _plugin_error
    env.setdefault("PYWEBVIEW_GUI", "qt")
    env.setdefault("QT_API", "pyside6")
    if env.get("QT_QPA_PLATFORM"):
        platform = env["QT_QPA_PLATFORM"]
    else:
        want_wl = bool(env.get("WAYLAND_DISPLAY")) and env.get("XDG_SESSION_TYPE") != "x11"
        have_x = bool(env.get("DISPLAY"))
        if not want_wl and not have_x:
            raise NativeWindowUnavailable("no display found (neither DISPLAY nor WAYLAND_DISPLAY is set)")
        wl_err = "not wanted"
        if want_wl:   # the plugin is called "wayland" (Qt 6.12) or "wayland-generic" (older Qt 6)
            wl_err = plugin_error("wayland")
            if wl_err and plugin_error("wayland-generic") is None:
                wl_err = None
        x_err = plugin_error("xcb") if have_x else "no DISPLAY"
        order = [p for p, err in (("wayland", wl_err), ("xcb", x_err)) if err is None]
        if not order:
            errs = [e for e in (wl_err if want_wl else None, x_err if have_x else None) if e]
            msg = "; ".join(errs)
            raise NativeWindowUnavailable(f"{msg}. {XCB_HELP}" if have_x and x_err else msg)
        platform = ";".join(order)
        env["QT_QPA_PLATFORM"] = platform
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    extra = []
    if not (sandbox_ok if sandbox_ok is not None else _sandbox_usable()) and "--no-sandbox" not in flags:
        extra.append("--no-sandbox")   # only ever shows our own local pages
    if env.get("CHC_SOFTWARE_RENDER") == "1":
        extra.append("--disable-gpu")
        env.setdefault("QT_QUICK_BACKEND", "software")
    if extra:
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " " + " ".join(extra)).strip()
    return "qt/" + platform.split(";")[0]


def _name_qt_app() -> None:
    """Create the QApplication ourselves, named so the window class / app_id matches the .desktop file
    (taskbar icon and grouping). pywebview reuses an existing instance."""
    from PySide6 import QtWebEngineWidgets  # noqa: F401  (must be imported before the application exists)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([APP_ID])
    QApplication.setApplicationName(APP_ID)
    QApplication.setApplicationDisplayName(TITLE)
    QApplication.setDesktopFileName(APP_ID)
    try:
        from PySide6.QtGui import QIcon
        from .desktop_entry import icon_source_dir
        d = icon_source_dir()
        if d and (d / "256.png").exists():
            app.setWindowIcon(QIcon(str(d / "256.png")))
    except Exception:
        pass


def describe_backend() -> str:
    """One line for `doctor`: which window the app would use here (nothing is opened)."""
    if sys.platform == "darwin":
        return "native (WebKit via pywebview)"
    if sys.platform == "win32":
        return "native (WebView2 via pywebview)"
    env = dict(os.environ)
    try:
        return "native (" + (prepare_linux_qt(env) or "GTK via pywebview, if installed") + ")"
    except NativeWindowUnavailable as e:
        return f"browser fallback ({str(e).split('. ')[0]})"


def _notice_once(profile: Path, reason: str) -> None:
    """Tell the user once that the app is using the browser because the native window failed."""
    from .i18n import _, render
    log.warning("Native window unavailable: %s", reason)
    marker = Path(profile) / "native_window_notice"
    try:
        if marker.exists():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(reason[:500], encoding="utf-8")
    except OSError:
        pass
    text = render(_("gui.native_failed", reason=reason.split(". ")[0][:200]))
    print(text, file=sys.stderr)
    exe = shutil.which("notify-send")
    if exe:
        try:
            process.popen([exe, "-a", TITLE, TITLE, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


SMOKE = {"ok": False, "detail": ""}


def _browser_app(url: str, profile: Path) -> subprocess.Popen | None:
    cands = []
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            p = Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
            if p.exists():
                cands.append(str(p))
    elif sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                                  os.environ.get("LOCALAPPDATA"))):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                p = Path(base) / rel
                if p.exists():
                    cands.append(str(p))
    else:
        for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                    "brave-browser"):
            p = shutil.which(exe)
            if p:
                cands.append(p)
    for exe in cands:
        try:
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
                                     "--no-first-run", "--no-default-browser-check"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=process.host_env())
        except Exception:
            continue
    return None


_extra = {"webview": None, "profile": None, "procs": []}


def open_extra_window(url: str, title: str = "Now Playing", fullscreen: bool = False) -> str:
    """Open another app window (e.g. the Now Playing screen for a second monitor)."""
    wv = _extra["webview"]
    if wv is not None:
        kw = {}
        try:
            screens = list(wv.screens)
            if len(screens) > 1:          # put it on the second display
                kw["screen"] = screens[1]
        except Exception:
            pass
        try:
            w = wv.create_window(title, url, width=1280, height=720, background_color="#160d2b",
                                 fullscreen=fullscreen, **kw)
        except TypeError:
            w = wv.create_window(title, url, width=1280, height=720, background_color="#160d2b",
                                 fullscreen=fullscreen)
        return "window" if w else "failed"
    proc = _browser_app(url, Path(_extra["profile"] or Path.home()))
    if proc:
        _extra["procs"].append(proc)
        return "app-window"
    webbrowser.open(url)
    return "browser"


def pick_path(kind: str = "folder", start: str = "") -> str | None:
    """Native folder/file picker (kind: 'folder' or 'game'). None when no native window exists."""
    from .i18n import _
    wv = _extra["webview"]
    if wv is None or not wv.windows:
        return None
    win = wv.windows[0]
    try:
        FD = getattr(wv, "FileDialog", None)
        folder_t = FD.FOLDER if FD else wv.FOLDER_DIALOG
        open_t = FD.OPEN if FD else wv.OPEN_DIALOG
        if kind == "game" and sys.platform == "darwin":
            # an .app bundle is a folder; the open dialog lets you pick it
            res = win.create_file_dialog(open_t, directory=start or "/Applications", allow_multiple=False,
                                         file_types=(str(_("gui.apps")), str(_("gui.all_files"))))
        elif kind == "game":
            ft = (str(_("gui.clone_hero_exe")), str(_("gui.all_files"))) if sys.platform == "win32" else (str(_("gui.all_files")),)
            res = win.create_file_dialog(open_t, directory=start or str(Path.home()), allow_multiple=False,
                                         file_types=ft)
        else:
            res = win.create_file_dialog(folder_t, directory=start or str(Path.home()))
    except Exception as e:
        log.warning("file dialog failed: %s", e)
        return ""
    if not res:
        return ""
    return str(res[0] if isinstance(res, (list, tuple)) else res)


def run_window(url: str, quit_event: threading.Event, profile: Path, smoke: bool = False) -> None:
    """Blocks until the window is closed or quit_event is set. With smoke=True: open the native window, wait for
    the page to load, record the result in SMOKE and close (no browser fallback)."""
    _extra["profile"] = str(profile)
    SMOKE.update(ok=False, detail="")
    reason = ""
    if os.environ.get("CHC_NO_WEBVIEW") == "1":
        reason = "CHC_NO_WEBVIEW=1"
    else:
        try:
            qt = prepare_linux_qt() if sys.platform.startswith("linux") else ""
            if qt:
                _name_qt_app()
            import webview  # pywebview

            window = webview.create_window(TITLE, url, width=1440, height=900, min_size=(980, 620),
                                           background_color="#160d2b", text_select=True)

            def watch():
                quit_event.wait()
                try:
                    window.destroy()
                except Exception:
                    pass
            threading.Thread(target=watch, daemon=True).start()
            _extra["webview"] = webview

            def on_closed():
                # closing the main window quits the app (and any extra windows)
                quit_event.set()
                for w in list(webview.windows):
                    try:
                        w.destroy()
                    except Exception:
                        pass
            window.events.closed += on_closed
            if smoke:
                def on_loaded():
                    try:
                        gl = getattr(webview, "guilib", None)
                        name = (getattr(gl, "__name__", "") or "").rsplit(".", 1)[-1]
                        title = window.evaluate_js("document.title")
                        SMOKE.update(ok=bool(name), detail=f"backend={name} platform={qt or '-'} title={title!r}")
                    except Exception as e:
                        SMOKE.update(ok=False, detail=f"page script failed: {e}")
                    window.destroy()
                window.events.loaded += on_loaded
                guard = threading.Timer(90, lambda: (SMOKE.update(detail=SMOKE["detail"] or "timed out waiting for the page"),
                                                     window.destroy()))
                guard.daemon = True
                guard.start()
            webview.start(gui=("qt" if qt else None), private_mode=False, storage_path=str(profile))
            if smoke and not SMOKE["detail"]:
                SMOKE["detail"] = "window closed before the page loaded"
            return
        except Exception as e:
            reason = str(e) or type(e).__name__
    if smoke:
        SMOKE["detail"] = f"native window unavailable: {reason}"
        return
    if reason != "CHC_NO_WEBVIEW=1":
        _notice_once(profile, reason)
    log.info("Native window unavailable (%s); using a browser window instead", reason)
    proc = _browser_app(url, profile)
    if proc is None:
        webbrowser.open(url)
        log.info("Opened %s in your browser. Close this window (or press Ctrl+C) to quit.", url)
        try:
            quit_event.wait()
        except KeyboardInterrupt:
            pass
        return
    try:
        while proc.poll() is None and not quit_event.is_set():
            quit_event.wait(1.0)
    except KeyboardInterrupt:
        pass
    if proc.poll() is None:
        proc.terminate()
