"""HTTP helpers. Frozen (PyInstaller) builds on macOS have no system CA bundle
for OpenSSL, so always verify against certifi's bundle when available.

Connections race the host's addresses ("happy eyeballs", RFC 8305): Python's own
socket.create_connection tries them one at a time and waits the whole timeout on an
address that doesn't answer. GitHub's download CDN publishes four addresses and one
can be unreachable from a given network, which left app updates at 0% for 30 s."""
from __future__ import annotations

import errno
import http.client
import selectors
import socket
import ssl
import time
import urllib.request

from . import cancel

_ctx: ssl.SSLContext | None = None
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035}   # 10035 = WSAEWOULDBLOCK
STAGGER_S = 0.25            # start the next address if the previous one hasn't connected yet
CONNECT_TIMEOUT_S = 10.0    # give up on connecting (all addresses) after this; reads keep the caller's timeout


def ssl_context() -> ssl.SSLContext:
    global _ctx
    if _ctx is None:
        try:
            import certifi
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
    return _ctx


def _interleave(infos: list) -> list:
    """Alternate address families (IPv6, IPv4, IPv6, ...) keeping the resolver's order within each."""
    fams: dict = {}
    for info in infos:
        fams.setdefault(info[0], []).append(info)
    out, lists = [], list(fams.values())
    while any(lists):
        for lst in lists:
            if lst:
                out.append(lst.pop(0))
    return out


def fast_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *a, **kw):
    """Drop-in for socket.create_connection that races the resolved addresses."""
    host, port = address
    infos = _interleave(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
    if not infos:
        raise OSError(f"getaddrinfo returned no addresses for {host}")
    limit = CONNECT_TIMEOUT_S
    if isinstance(timeout, (int, float)) and timeout is not None:
        limit = min(limit, timeout) if timeout > 0 else limit
    deadline = time.monotonic() + limit
    sel = selectors.DefaultSelector()
    pending: dict = {}
    errors: list = []
    winner = None
    nxt = 0
    next_start = time.monotonic()
    try:
        while winner is None:
            now = time.monotonic()
            if nxt < len(infos) and (now >= next_start or not pending):
                fam, typ, proto, _c, sa = infos[nxt]
                nxt += 1
                s = socket.socket(fam, typ, proto)
                try:
                    s.setblocking(False)
                    if source_address:
                        s.bind(source_address)
                    err = s.connect_ex(sa)
                    if err == 0:
                        winner = s
                        break
                    if err not in _IN_PROGRESS:
                        raise OSError(err, f"connect to {sa[0]} failed")
                    sel.register(s, selectors.EVENT_WRITE)
                    pending[s] = sa
                except OSError as e:
                    errors.append(e)
                    s.close()
                next_start = now + STAGGER_S
                continue
            if not pending and nxt >= len(infos):
                break
            if now >= deadline:
                errors.append(socket.timeout(f"connect to {host} timed out"))
                break
            wait = deadline - now
            if nxt < len(infos):
                wait = min(wait, max(0.0, next_start - now))
            for key, _ev in sel.select(wait):
                s = key.fileobj
                sel.unregister(s)
                sa = pending.pop(s)
                err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0 and winner is None:
                    winner = s
                else:
                    errors.append(OSError(err, f"connect to {sa[0]} failed"))
                    s.close()
    finally:
        for s in pending:
            try:
                sel.unregister(s)
            except Exception:
                pass
            s.close()
        sel.close()
    if winner is None:
        raise errors[-1] if errors else OSError(f"could not connect to {host}")
    winner.setblocking(True)
    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
        winner.settimeout(timeout)
    return winner


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = fast_create_connection


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = fast_create_connection


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


def handlers() -> list:
    """Handlers for urllib.request.build_opener(*net.handlers(), ...)."""
    return [HTTPHandler(), HTTPSHandler(context=ssl_context())]


_opener = None


def urlopen(req, timeout: float = 15.0):
    global _opener
    cancel.check()          # a cancelled job starts no further request
    if _opener is None:
        _opener = urllib.request.build_opener(*handlers())
    return _opener.open(req, timeout=timeout)
