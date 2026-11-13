"""Anonymous YouTube Proof-of-Origin (PO) tokens: a local bgutil token server for yt-dlp.

YouTube wants a PO token with many streaming requests. The bgutil-ytdlp-pot-provider plugin for yt-dlp fetches one
from a small HTTP server. This module runs that server (chcompanion/vendor/bgutil/bgutil-server.js, a single bundled
file) with the Deno the app already carries, on a free loopback port, lazily on the first YouTube request. Tokens
are minted on this computer; the server talks to YouTube/Google the way a browser does. No account, no cookies.

Everything here is best effort: if Deno, the bundle or the plugin is missing, or the server does not come up, ensure()
returns None, nothing is added to yt-dlp's options and downloads behave exactly as before.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
START_TIMEOUT_S = 25.0           # the 8 MB bundle takes a second or two to compile; a slow disk gets more
PING_TIMEOUT_S = 2.0
RETRY_AFTER_S = 600.0            # after giving up, try again no sooner than this
MAX_RESTARTS = 1                 # a server that dies is restarted once
OPT_OUT_ENV = "CHC_NO_POT"       # set to 1: never start the server
# Deno permissions: network (YouTube/Google, plus whatever proxy the user has) and env (proxy variables). Nothing
# else: no file access (the bundle itself is loaded by Deno without a grant), no subprocesses, no FFI.
DENO_FLAGS = ("--no-prompt", "--no-config", "--no-lock", "--no-remote", "--allow-net", "--allow-env")


def bundle_dir() -> Path:
    return Path(__file__).resolve().parent / "vendor" / "bgutil"


def bundle_file() -> Path:
    return bundle_dir() / "bgutil-server.js"


def bundle_version() -> str:
    try:
        return (bundle_dir() / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def free_port() -> int:
    """A currently unused TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def command(deno: str, port: int) -> list[str]:
    return [deno, "run", *DENO_FLAGS, str(bundle_file()), "-H", HOST, "-p", str(port)]


def ping(port: int, timeout: float = PING_TIMEOUT_S) -> dict | None:
    """The server's /ping answer, or None. Never goes through a proxy."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{HOST}:{port}/ping", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) and "version" in data else None
    except Exception:
        return None


def _keep_loopback_direct() -> None:
    """yt-dlp reads proxy settings from the environment; its request to our server must never go through one."""
    for var in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(var, "")
        if HOST not in cur.split(","):
            os.environ[var] = f"{cur},{HOST}" if cur else HOST


class PotServer:
    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._port: int | None = None
        self._errfile = None
        self._restarts = 0
        self._failed_at = 0.0
        self.reason = ""             # why it is unavailable, for doctor and the log

    # ---- state ----
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _fail(self, why: str) -> None:
        self.reason = why
        self._failed_at = time.monotonic()
        log.info("PO token server unavailable: %s", why)
        self._kill()

    def _kill(self) -> None:
        proc, self._proc, self._port = self._proc, None, None
        if proc is not None:
            try:
                from .process import terminate
                terminate(proc)
            except Exception:
                log.debug("could not stop PO token server", exc_info=True)
        if self._errfile is not None:
            try:
                self._errfile.close()
                os.unlink(self._errfile.name)
            except OSError:
                pass
            self._errfile = None

    def _stderr_tail(self) -> str:
        try:
            self._errfile.flush()
            with open(self._errfile.name, "rb") as f:
                return f.read()[-300:].decode("utf-8", "replace").strip().replace("\n", " | ")
        except Exception:
            return ""

    # ---- start ----
    def _start(self) -> bool:
        from . import youtube
        from .process import popen
        deno = youtube._find_tool("deno")
        if not deno:
            self._fail("no Deno")
            return False
        if not bundle_file().is_file():
            self._fail("server bundle missing")
            return False
        _keep_loopback_direct()
        last = ""
        for _attempt in range(2):                      # the port could be taken between probing and binding
            port = free_port()
            self._errfile = tempfile.NamedTemporaryFile("w+b", prefix="chc-pot-", suffix=".log", delete=False)
            try:
                # stdin is a lifeline: the server exits when this pipe closes, i.e. when the app exits, even if killed
                self._proc = popen(command(deno, port), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._errfile,
                                   env=dict(os.environ, CHC_POT_LIFELINE="1"))
            except Exception as e:                      # OSError, ToolStartError
                self._fail(f"could not start Deno ({type(e).__name__})")
                return False
            deadline = time.monotonic() + START_TIMEOUT_S
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    break
                if ping(port):
                    self._port = port
                    self.reason = ""
                    log.info("PO token server up on %s:%s", HOST, port)
                    return True
                time.sleep(0.2)
            last = self._stderr_tail() or ("exited" if self._proc.poll() is not None else "no answer in time")
            self._kill()
        self._fail(f"server did not start ({last})")
        return False

    # ---- public ----
    def ensure(self) -> int | None:
        """The port of a healthy server, starting (or once restarting) it if needed; None when unavailable."""
        if os.environ.get(OPT_OUT_ENV):
            self.reason = f"disabled ({OPT_OUT_ENV})"
            return None
        with self._lock:
            if self._alive() and self._port:
                return self._port
            if self._proc is not None:                  # it died
                self._kill()
                if self._restarts >= MAX_RESTARTS:
                    self._fail("server stopped repeatedly")
                    return None
                self._restarts += 1
            elif self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER_S:
                return None
            elif self._failed_at:                       # retry window passed
                self._restarts = 0
            try:
                return self._port if self._start() else None
            except Exception as e:                      # never let this break a download
                self._fail(f"{type(e).__name__}: {e}")
                return None

    def stop(self) -> None:
        with self._lock:
            self._kill()
            self._failed_at = 0.0
            self._restarts = 0

    @property
    def port(self) -> int | None:
        return self._port if self._alive() else None


SERVER = PotServer()
atexit.register(SERVER.stop)


def ensure() -> int | None:
    return SERVER.ensure()


def stop() -> None:
    SERVER.stop()


def extractor_args() -> dict | None:
    """yt-dlp extractor_args pointing the bgutil provider at our server; None when there is no server."""
    port = ensure()
    if not port:
        return None
    return {"youtubepot-bgutilhttp": {"base_url": [f"http://{HOST}:{port}"]}}


def plugin_version() -> str | None:
    """Version of the bgutil yt-dlp plugin once yt-dlp has loaded it, else None."""
    try:
        from yt_dlp.plugins import load_all_plugins
        from yt_dlp.extractor.youtube.pot._registry import _pot_providers
        load_all_plugins()
        if not any(n.startswith("BgUtilHTTP") for n in _pot_providers.value):
            return None
        mod = sys.modules.get("yt_dlp_plugins.extractor.getpot_bgutil")
        return getattr(mod, "__version__", "?")
    except Exception:
        return None


def status_line() -> str:
    """The doctor line: 'bgutil 2.0.2 plugin loaded; server ok (port N)' or why not."""
    pv = plugin_version()
    if not pv:
        return "plugin missing (yt_dlp_plugins bgutil provider not found); downloads run without PO tokens"
    port = ensure()
    if port:
        return f"bgutil {pv} plugin loaded; server ok (port {port})"
    return f"bgutil {pv} plugin loaded; server unavailable ({SERVER.reason or 'unknown'}); downloads run without PO tokens"
