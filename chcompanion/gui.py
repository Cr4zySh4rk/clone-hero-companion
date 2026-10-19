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


def run_window(url: str, quit_event: threading.Event, profile: Path) -> None:
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
