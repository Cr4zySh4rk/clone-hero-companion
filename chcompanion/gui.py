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

log = logging.getLogger(__name__)
TITLE = "Clone Hero Companion"


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
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                                         file_types=("Applications (*.app)", "All files (*.*)"))
        elif kind == "game":
            ft = ("Clone Hero (*.exe)", "All files (*.*)") if sys.platform == "win32" else ("All files (*.*)",)
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


def run_window(url: str, quit_event: threading.Event, profile: Path) -> None:
    _extra["profile"] = str(profile)
    """Blocks until the window is closed or quit_event is set."""
    if os.environ.get("CHC_NO_WEBVIEW") != "1":
        try:
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
            webview.start(private_mode=False, storage_path=str(profile))
            return
        except Exception as e:
            log.info("Native window unavailable (%s); using a browser window instead", e)
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
