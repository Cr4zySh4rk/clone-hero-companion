"""Job cancellation that reaches everything a job runs.

The worker binds the job's cancel Event to its thread (bind). Slow code anywhere underneath (subprocess
helpers, YouTube pacing, yt-dlp progress hooks, HTTP downloads) then looks it up with current() instead of
every function taking a parameter. Cancelled derives from BaseException on purpose: the many
`except Exception` fallbacks ("try the next candidate", "carry on without lyrics") must not swallow it.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager


class Cancelled(BaseException):
    """The user cancelled the job; unwinds to the worker, which marks the job cancelled."""


_local = threading.local()


@contextmanager
def bind(event: threading.Event | None):
    prev = getattr(_local, "event", None)
    _local.event = event
    try:
        yield
    finally:
        _local.event = prev


def current() -> threading.Event | None:
    return getattr(_local, "event", None)


def is_set() -> bool:
    ev = current()
    return bool(ev and ev.is_set())


def check() -> None:
    if is_set():
        raise Cancelled()


def sleep(secs: float, other: threading.Event | None = None) -> None:
    """Wait up to secs; raises Cancelled the moment the current job is cancelled (or `other` is set)."""
    ev = current()
    end = threading.Event()
    remaining = max(0.0, secs)
    while remaining > 0:
        step = min(remaining, 0.2)
        if ev is not None and ev.wait(step):
            raise Cancelled()
        if ev is None:
            end.wait(step)
        if other is not None and other.is_set():
            raise Cancelled()
        remaining -= step
    check()


def acquire(sem, blocking: bool = True) -> bool:
    """sem.acquire that gives up with Cancelled when the current job is cancelled while waiting."""
    if not blocking:
        return sem.acquire(blocking=False)
    while True:
        check()
        if sem.acquire(timeout=0.2):
            return True
