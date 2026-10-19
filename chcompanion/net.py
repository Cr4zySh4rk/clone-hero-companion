"""HTTP helpers. Frozen (PyInstaller) builds on macOS have no system CA bundle
for OpenSSL, so always verify against certifi's bundle when available."""
from __future__ import annotations

import ssl
import urllib.request

_ctx: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _ctx
    if _ctx is None:
        try:
            import certifi
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
    return _ctx


def urlopen(req, timeout: float = 15.0):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
