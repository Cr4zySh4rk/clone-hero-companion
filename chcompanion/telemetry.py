"""Anonymous usage statistics (always on in the app, no user opt-out; see PRIVACY.md).

What is sent: event names from SCHEMA below, with enum / bucket / count properties only, plus the app version, the
operating system family, the app language, a random install id and a random per-run session id. Nothing about songs,
names, e-mail addresses, file paths or search text, and no IP address is stored on the server.

Nothing is sent before the Terms of Use have been accepted (terms.py), or when CHC_NO_TELEMETRY=1 (developer / CI switch).
`event()` never raises."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

FLUSH_S = 60.0
HEARTBEAT_S = 300.0
MAX_QUEUE = 500
BATCH = 50
QUIT_FLUSH_S = 2.0

_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]{0,19}")
_LOCALE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
VIEWS = {"library", "favs", "charts", "spotify", "setlists", "dupes", "jobs", "friends", "profile",
         "leaderboards", "now", "setup", "settings", "other"}
RESULT = {"ok", "fail"}
SHARE_TARGETS = {"whatsapp", "reddit", "facebook", "instagram", "snapchat", "save", "copy", "system"}
COUNT_BUCKETS = ("1", "2-5", "6-20", "21-100", "100+")
LIBRARY_BUCKETS = ("0", "1-50", "51-200", "201-1000", "1000+")
GPU_KINDS = {"cpu", "nvidia", "apple", "d3d11", "vaapi", "unknown"}

# name -> {property: validator}. Validators: a set (enum), "bool", "int" or a compiled regex.
# Keep in step with the allow-list in supabase/migrations/0007_telemetry.sql.
SCHEMA: dict[str, dict] = {
    "app_start": {"arch": re.compile(r"[A-Za-z0-9_]{1,12}"), "signed_in": "bool",
                  "library_size_bucket": set(LIBRARY_BUCKETS), "gpu_kind": GPU_KINDS},
    "heartbeat": {"view": VIEWS, "focused": "bool"},
    "app_quit": {"session_s": "int"},
    "view": {"name": VIEWS},
    "download_chart": {"source": {"encore", "other"}},
    "add_video": {"result": RESULT},
    "add_lyrics": {"result": RESULT},
    "convert_videos": {"count": set(COUNT_BUCKETS)},
    "spotify_import": {},
    "setlist_create": {},
    "favourite": {"on": "bool"},
    "share_stats": {"target": SHARE_TARGETS},
    "friend_request": {},
    "friend_view_profile": {},
    "ch_link": {"result": RESULT},
    "update_install": {"result": RESULT},
    "job_cancel": {},
    "launch_game": {},
}
UI_EVENTS = frozenset(SCHEMA) - {"app_start", "app_quit"}      # what the web UI may report through /api/telemetry/event


def library_bucket(n: int) -> str:
    return "0" if n <= 0 else "1-50" if n <= 50 else "51-200" if n <= 200 else "201-1000" if n <= 1000 else "1000+"


def count_bucket(n: int) -> str:
    return "1" if n <= 1 else "2-5" if n <= 5 else "6-20" if n <= 20 else "21-100" if n <= 100 else "100+"


def os_family() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else "other")


def clean_props(name: str, props: dict | None) -> dict | None:
    """The allowed properties of `name` (unknown keys and invalid values dropped); None for an unknown event."""
    spec = SCHEMA.get(name)
    if spec is None:
        return None
    out = {}
    for k, v in (props or {}).items():
        rule = spec.get(k)
        if rule is None:
            continue
        if rule == "bool":
            if isinstance(v, bool):
                out[k] = v
        elif rule == "int":
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000_000:
                out[k] = v
        elif isinstance(rule, set):
            if isinstance(v, str) and v in rule:
                out[k] = v
        elif isinstance(v, str) and len(v) <= 40 and rule.fullmatch(v):
            out[k] = v
    return out


def _post(url: str, headers: dict, body: bytes, timeout: float) -> int:
    import urllib.request
    from . import net
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with net.urlopen(req, timeout=timeout) as r:
        return r.status


class Telemetry:
    def __init__(self, base_dir: Path | None = None, *, url: str | None = None, key: str | None = None,
                 post: Callable[[str, dict, bytes, float], int] = _post, version: str = "", locale: Callable[[], str] | None = None):
        from .cloud import PUBLISHABLE_KEY, URL
        self.dir = base_dir
        self.url = (url or URL).rstrip("/") + "/rest/v1/rpc/track"
        self.key = key or PUBLISHABLE_KEY
        self.post = post
        self.version = version
        self.locale = locale or (lambda: "en")
        self.session_id = str(uuid.uuid4())
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.queue: list[dict] = []
        self.active = False
        self.allowed: Callable[[], bool] = lambda: True      # the Companion sets this to "Terms accepted"
        self.on_start: Callable[[], dict] | None = None     # extra app_start props (set by the Companion)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._install_id: str | None = None

    # ---- files
    def _path(self, name: str) -> Path:
        d = self.dir
        if d is None:
            from .paths import config_dir
            d = config_dir()
        return d / name

    def install_id(self) -> str:
        if self._install_id:
            return self._install_id
        p = self._path("telemetry_id")
        try:
            v = str(uuid.UUID(p.read_text("utf-8").strip()))
        except Exception:
            v = str(uuid.uuid4())                    # random, never derived from the machine
            try:
                p.write_text(v, "utf-8")
            except OSError:
                pass
        self._install_id = v
        return v

    def _load_overflow(self) -> None:
        try:
            rows = json.loads(self._path("telemetry_queue.json").read_text("utf-8"))
            if isinstance(rows, list):
                self.queue = [r for r in rows if isinstance(r, dict)][-MAX_QUEUE:] + self.queue
        except Exception:
            pass

    def _save_overflow(self) -> None:
        try:
            p = self._path("telemetry_queue.json")
            if self.queue:
                p.write_text(json.dumps(self.queue[-MAX_QUEUE:]), "utf-8")
            else:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    # ---- state
    def apply(self) -> None:
        """Start collecting (called once at start-up). Does nothing when CHC_NO_TELEMETRY=1."""
        try:
            if os.environ.get("CHC_NO_TELEMETRY") == "1":
                self.active = False
                return
            if self.active or not self.allowed():
                return
            self.active = True
            self._load_overflow()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="telemetry")
            self._thread.start()
            props = {}
            try:
                props = self.on_start() if self.on_start else {}
            except Exception:
                pass
            self.event("app_start", **props)
        except Exception as e:
            log.debug("telemetry apply failed: %s", e)

    def event(self, name: str, /, **props) -> None:
        try:
            if not self.active:
                return
            clean = clean_props(name, props)
            if clean is None:
                return
            loc = self.locale()
            row = {"source": "app", "install_id": self.install_id(), "session_id": self.session_id, "name": name,
                   "props": clean, "app_version": self.version if _VERSION.fullmatch(self.version or "") else "",
                   "os": os_family(), "locale": loc if _LOCALE.fullmatch(loc or "") else ""}
            with self.lock:
                self.queue.append(row)
                del self.queue[:-MAX_QUEUE]
        except Exception:
            pass

    # ---- sending
    def flush(self, timeout: float = 10.0) -> bool:
        """Send everything queued; on failure the events stay queued (and are saved for the next run)."""
        try:
            deadline = time.monotonic() + timeout
            while True:
                with self.lock:
                    if not self.active or not self.queue:
                        break
                    batch, self.queue = self.queue[:BATCH], self.queue[BATCH:]
                left = deadline - time.monotonic()
                try:
                    if left <= 0:
                        raise TimeoutError
                    body = json.dumps({"events": batch}).encode()
                    code = self.post(self.url, {"apikey": self.key, "Authorization": "Bearer " + self.key,
                                                "Content-Type": "application/json"}, body, max(0.5, left))
                    if code >= 300:
                        raise OSError(code)
                except Exception:
                    with self.lock:
                        self.queue = (batch + self.queue)[-MAX_QUEUE:]
                    self._save_overflow()
                    return False
            self._save_overflow()
            return True
        except Exception:
            return False

    def _loop(self) -> None:
        while not self._stop.wait(FLUSH_S):
            self.flush()

    def shutdown(self) -> None:
        """Best effort, at most about two seconds."""
        try:
            if not self.active:
                return
            self.event("app_quit", session_s=int(time.monotonic() - self.t0))
            self._stop.set()
            self.flush(QUIT_FLUSH_S)
            self._save_overflow()
        except Exception:
            pass


_T: Telemetry | None = None


def get() -> Telemetry:
    global _T
    if _T is None:
        from . import __version__, i18n
        _T = Telemetry(version=__version__, locale=lambda: i18n.LANG)
    return _T


def event(name: str, /, **props) -> None:
    try:
        get().event(name, **props)
    except Exception:
        pass


def apply() -> None:
    try:
        get().apply()
    except Exception:
        pass


def shutdown() -> None:
    try:
        if _T is not None:
            _T.shutdown()
    except Exception:
        pass


def env_disabled() -> bool:
    return os.environ.get("CHC_NO_TELEMETRY") == "1"


def is_active() -> bool:
    return bool(_T and _T.active)
