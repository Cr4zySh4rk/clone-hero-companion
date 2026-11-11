"""Link a CH Leaderboards account by signing in to it (verified on the server).

Flow (all against https://api.clonehero.net):
  POST /v2/auth/start?clientType=launcher   -> {authRequestId, authorizationUrl}   (opened in the system browser)
  GET  /v2/auth/status?authRequestId=X       -> pending | pendingAccountCreation | complete | failed | expired
  POST /v2/auth/complete?authRequestId=X     -> {sessionToken, ...}
The session token is handed straight to the `ch-link` Edge Function (which checks it with CH, stores the verified link
and logs the CH session out). The token lives only in a local variable for those few seconds: it is never written to
disk, to the settings, to the log, nor kept on any object.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from .i18n import _
from .net import urlopen

log = logging.getLogger(__name__)

API = "https://api.clonehero.net"
UA = "CloneHeroCompanion"
POLL_S = 2.0
TIMEOUT_S = 600.0


class ChLinkError(Exception):
    pass


def _http(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception as e:
        raise ChLinkError(_("chlink.unreachable", detail=type(e).__name__))


def _json(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


class ChLink:
    """One sign-in attempt at a time. `state` is what the Profile screen polls."""

    def __init__(self, app, http: Callable = _http, open_url: Callable[[str], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic):
        self.app, self.http, self.sleep, self.now = app, http, sleep, now
        self.open_url = open_url
        self.lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {"state": "idle", "message": ""}

    # ------------------------------------------------------------ public
    def status(self) -> dict:
        with self.lock:
            return dict(self._state)

    def _set(self, state: str, message: str = "", **extra):
        with self.lock:
            self._state = {"state": state, "message": message, **extra}
        if state in ("done", "failed", "expired"):
            from . import telemetry
            telemetry.event("ch_link", result="ok" if state == "done" else "fail")

    def start(self) -> dict:
        """Begin a sign-in: returns the status (state 'waiting'). Raises ChLinkError when Companion is not signed in."""
        if not self.app.cloud.signed_in:
            raise ChLinkError(_("chlink.need_companion"))
        with self.lock:
            if self._thread and self._thread.is_alive():
                return dict(self._state)
        status, raw = self.http("POST", f"{API}/v2/auth/start?clientType=launcher",
                                {"User-Agent": UA, "Accept": "application/json"}, None)
        d = _json(raw)
        rid, url = d.get("authRequestId"), d.get("authorizationUrl")
        if status >= 400 or not rid or not url:
            raise ChLinkError(_("chlink.start_failed"))
        self._cancel.clear()
        self._set("waiting", _("chlink.waiting"), url=url)
        (self.open_url or self.app.cloud.open_browser)(url)
        t = threading.Thread(target=self._run, args=(str(rid),), daemon=True, name="ch-link")
        with self.lock:
            self._thread = t
        t.start()
        return self.status()

    def cancel(self) -> dict:
        self._cancel.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        if self.status()["state"] in ("waiting", "pendingAccountCreation", "linking"):
            self._set("cancelled", _("chlink.cancelled"))
        return self.status()

    def unlink(self) -> None:
        """Remove the verified link (server first, so a failure leaves everything as it was) and forget it locally."""
        if self.app.cloud.signed_in:
            status, data = self.app.cloud.call_function("ch-link", {"unlink": True})
            if status >= 400:
                raise ChLinkError(_("chlink.unlink_failed"))
        self.app.set_ch_link("", 0, False)
        self._set("idle", "")

    @staticmethod
    def _failed(detail: str) -> str:
        detail = detail.strip()[:160]
        return _("chlink.failed_detail", detail=detail) if detail else _("chlink.failed")

    # ------------------------------------------------------------ the polling thread
    def _poll_status(self, rid: str) -> dict:
        _s, raw = self.http("GET", f"{API}/v2/auth/status?authRequestId={rid}", {"User-Agent": UA, "Accept": "application/json"}, None)
        return _json(raw)

    def _run(self, rid: str):
        deadline = self.now() + TIMEOUT_S
        try:
            while not self._cancel.is_set():
                if self.now() >= deadline:
                    return self._set("expired", _("chlink.expired"))
                try:
                    st = self._poll_status(rid)
                except ChLinkError:
                    st = {}                                    # a network blip: keep polling until the deadline
                kind = st.get("status")
                if kind == "complete":
                    return self._finish(rid)
                if kind == "failed":
                    return self._set("failed", self._failed(str(st.get("errorMessage") or "")))
                if kind == "expired":
                    return self._set("expired", _("chlink.expired"))
                if kind == "pendingAccountCreation":
                    self._set("pendingAccountCreation", _("chlink.pending_account"), url=self.status().get("url", ""))
                self.sleep(POLL_S)
        except Exception as e:
            log.info("CH sign-in stopped: %s", type(e).__name__)
            self._set("failed", self._failed(""))

    def _finish(self, rid: str):
        self._set("linking", _("chlink.linking"))
        token = None
        try:
            status, raw = self.http("POST", f"{API}/v2/auth/complete?authRequestId={rid}",
                                    {"User-Agent": UA, "Accept": "application/json"}, None)
            token = _json(raw).get("sessionToken")
            if status >= 400 or not token:
                return self._set("failed", self._failed(""))
            if self._cancel.is_set():
                return                                         # cancelled right now: never hand the token on
            status, data = self.app.cloud.call_function("ch-link", {"sessionToken": token})
        finally:
            token = None                                       # the only copy goes out of scope
        if status == 401 and data.get("error") == "ch_session_invalid":
            return self._set("failed", _("chlink.invalid"))
        if status >= 400 or not data.get("accountId"):
            return self._set("failed", _("chlink.link_failed"))
        self.app.set_ch_link(str(data.get("username") or ""), int(data["accountId"]), True)
        self._set("done", _("chlink.done", name=data.get("username") or ""), username=data.get("username") or "",
                  accountId=int(data["accountId"]), alsoLinkedElsewhere=bool(data.get("alsoLinkedElsewhere")))
