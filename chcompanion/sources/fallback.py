"""Download a chart from a ranked list of candidates: try each in order, move on when one fails, and fail
only when all did, with a message that says what was tried."""
from __future__ import annotations

import logging

from . import hosts

log = logging.getLogger(__name__)
SOURCE_NAMES = {"encore": "Encore", "rhythmverse": "RhythmVerse", "link": "recorded link"}


def source_name(item: dict) -> str:
    return SOURCE_NAMES.get(item.get("source") or "", str(item.get("source") or "").title() or "unknown source")


def describe(item: dict, e: Exception) -> str:
    """One failed attempt: the reason and where it came from, e.g. 'Google Drive quota exceeded
    (RhythmVerse, drive.google.com)'."""
    if isinstance(e, hosts.DownloadError):
        return e.describe(source_name(item))
    host = hosts.host_of(item.get("downloadUrl") or item.get("url") or "")
    return f"{e or type(e).__name__} ({', '.join(x for x in (source_name(item), host) if x)})"


class AllFailed(RuntimeError):
    def __init__(self, attempts: list[tuple[dict, str]], page: str | None):
        self.attempts, self.page = attempts, page
        msg = "Couldn't download: " + ", ".join(a for _i, a in attempts) + "."
        if page:
            msg += f" Try again later, or download it manually from {page}"
        super().__init__(msg)


def _page(items: list[dict]) -> str | None:
    for it in items:
        if it.get("pageUrl"):
            return it["pageUrl"]
    return None


def download_first_working(item: dict, do_install, more=None, progress=None):
    """do_install(item) installs one candidate. After the first fails, more(tried_urls) is asked once for the
    other candidates (it may return []); a recorded link that 404ed is passed in tried_urls so the lookup
    ignores it and searches fresh. hosts.ManualDownload and AlreadyInstalled/folder errors pass through when
    nothing else can be tried. Returns (do_install's result, the candidate that worked)."""
    queue, tried, attempts, extra_asked = [item], set(), [], False
    seen_items = [item]
    while queue:
        cur = queue.pop(0)
        url = hosts.normalize_url(cur.get("downloadUrl") or cur.get("url") or "")
        tried.add(url)
        if progress and attempts:
            progress(f"trying {source_name(cur)} ({hosts.host_of(url)})")
        try:
            return do_install(cur), cur
        except RuntimeError as e:
            if str(e).startswith(("Cannot write to folder", "cancelled")):
                raise            # our own folder or a user cancel: another candidate cannot help
            reason = describe(cur, e)
        except hosts.ManualDownload as e:
            reason = f"{e} ({source_name(cur)}, {hosts.host_of(url) or 'no direct link'})"
        log.info("candidate %s failed: %s", cur.get("id") or url, reason)
        attempts.append((cur, reason))
        if not queue and more and not extra_asked:
            extra_asked = True
            try:
                for c in more(set(tried)) or []:
                    cu = hosts.normalize_url(c.get("downloadUrl") or c.get("url") or "")
                    if cu not in tried:
                        queue.append(c)
                        seen_items.append(c)
            except Exception as e:      # a lookup outage must not hide why the download failed
                log.info("re-resolving failed: %s", e)
                attempts.append(({}, f"lookup failed: {e}"))
    if progress:
        progress("downloading the chart")
    raise AllFailed(attempts, _page(seen_items))
