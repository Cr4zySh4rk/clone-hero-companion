"""Download charts from the hosts charters use: direct links, Google Drive (files
and public folders), Mediafire, Dropbox and link shorteners. MEGA can't be fetched
over plain HTTP, so it is reported for manual download."""
from __future__ import annotations

import html as htmlmod
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..net import urlopen

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SHORTENERS = re.compile(r"(^|\.)(bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|rb\.gy|cutt\.ly|is\.gd|shorturl\.at|adf\.ly)$", re.I)


RV_BASE = "https://rhythmverse.co"
_SAFE_PATH = "/:@!$&'()*+,;=-._~%"
_SAFE_QUERY = "/:@!$&'()*+,;=-._~%?"
DRIVE_HOSTS = ("drive.google.com", "drive.usercontent.google.com", "docs.google.com")


class ManualDownload(Exception):
    """The host needs a browser (MEGA etc.)."""


class DownloadError(RuntimeError):
    """A download failed. kind: quota | private | http | network | page | empty. host is where it failed;
    source (set by the caller that knows it, e.g. 'RhythmVerse') says where the link came from."""

    def __init__(self, reason: str, host: str = "", kind: str = "http", status: int | None = None,
                 source: str = ""):
        super().__init__(reason)
        self.reason, self.host, self.kind, self.status, self.source = reason, host, kind, status, source

    def describe(self, source: str = "") -> str:
        src = source or self.source
        where = ", ".join(x for x in (src, self.host) if x)
        return f"{self.reason} ({where})" if where else self.reason

    def __str__(self):
        return self.describe()


def _quote(s: str, safe: str) -> str:
    s = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", s)      # a lone % is data; existing %XX is never encoded twice
    return urllib.parse.quote(s, safe=safe)


def normalize_url(url, base: str = RV_BASE) -> str:
    """Make a stored or API-supplied link safe for urllib: strip whitespace and CR/LF/TAB (a \\r left over from a
    file read on Windows), turn backslashes into slashes, join relative paths ("/download_file/x/y z.7z") onto
    base, and percent-encode the path and query (spaces, brackets, non-ASCII) without double-encoding."""
    s = re.sub(r"[\r\n\t\x00-\x1f\x7f]", "", str(url or "")).strip()
    if not s:
        return ""
    path_part, sep, query = s.partition("?")
    path_part = path_part.replace("\\", "/")
    s = path_part + sep + query
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s):
        if re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?/", s) and not s.startswith("/"):
            s = "https://" + s                                # "rhythmverse.co/download/x" without a scheme
        else:
            s = urllib.parse.urljoin(base.rstrip("/") + "/", s)
    parts = urllib.parse.urlsplit(s)
    host = parts.netloc
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        host = host.encode("idna").decode("ascii")
    return urllib.parse.urlunsplit((parts.scheme, host, _quote(parts.path, _SAFE_PATH),
                                    _quote(parts.query, _SAFE_QUERY), ""))


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url or "").hostname or ""


def _open(url: str, timeout: float = 60.0):
    url = normalize_url(url)
    return urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"}), timeout=timeout)


def expand_short(url: str) -> str:
    url = normalize_url(url)
    host = urllib.parse.urlparse(url).hostname or ""
    if not SHORTENERS.search(host):
        return url
    try:
        with _open(url, 20) as r:
            return r.geturl()
    except Exception:
        return url


def drive_id(url: str) -> str | None:
    for pat in (r"/file/d/([\w-]{10,})", r"[?&]id=([\w-]{10,})", r"/d/([\w-]{10,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def drive_folder_id(url: str) -> str | None:
    m = re.search(r"/folders/([\w-]{10,})", url)
    return m.group(1) if m else None


def list_drive_folder(folder_id: str) -> list[dict]:
    with _open(f"https://drive.google.com/drive/folders/{folder_id}", 30) as r:
        page = r.read().decode("utf-8", "replace")
    ids, pos = [], []
    for m in re.finditer(r'data-id="([^"]+)"', page):
        if m.group(1) == "_gd" or (ids and ids[-1] == m.group(1)):
            continue
        ids.append(m.group(1))
        pos.append(m.start())
    out = []
    for i, fid in enumerate(ids):
        chunk = page[pos[i]: pos[i + 1] if i + 1 < len(pos) else len(page)]
        fm = re.search(r'aria-label="([^"]+\.[a-z0-9]{2,5})(?:\s[^"]*)?"', chunk, re.I)
        gm = re.search(r'aria-label="([^"]+?)(?:\s+(?:Folder|File|Audio|Image|Video|Text|PDF)[^"]*)?"', chunk, re.I)
        name = htmlmod.unescape(fm.group(1) if fm else gm.group(1) if gm else fid)
        out.append({"id": fid, "name": re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(),
                    "folder": not re.search(r"\.[a-z0-9]{2,5}$", name, re.I)})
    return out


def _filename_from(r, fallback: str) -> str:
    cd = r.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1).strip()
    name = Path(urllib.parse.urlparse(r.geturl()).path).name
    return urllib.parse.unquote(name) or fallback


def _page_error(text: str, url: str) -> DownloadError:
    """Why a link gave a web page instead of the file."""
    host = host_of(url)
    if host in DRIVE_HOSTS or "Google Drive" in text:
        host = host or "drive.google.com"
        low = text.lower()
        if "quota exceeded" in low or "too many users have viewed or downloaded" in low:
            return DownloadError("Google Drive quota exceeded", host, "quota")
        if "sign in" in low or "request access" in low or "you need access" in low or "accounts.google.com" in low:
            return DownloadError("Google Drive file is private (not shared publicly)", host, "private")
        if "can't scan this file for viruses" in low or "virus scan warning" in low:
            return DownloadError("Google Drive virus-scan page had no download form", host, "page")
        return DownloadError("Google Drive refused the download (quota exceeded or not public)", host, "quota")
    return DownloadError("the link returned a web page instead of a file", host, "page")


def _drive_form_url(text: str, base_url: str) -> str | None:
    """The large-file virus-scan page of drive.usercontent.google.com carries a form with hidden id / export /
    confirm / uuid inputs; submitting it (GET) gives the file."""
    m = re.search(r'<form[^>]*id="download-form"[^>]*>(.*?)</form>', text, re.S | re.I)
    if not m:
        return None
    tag = re.search(r'<form[^>]*id="download-form"[^>]*>', text, re.I).group(0)
    am = re.search(r'action="([^"]+)"', tag)
    action = htmlmod.unescape(am.group(1)) if am else "https://drive.usercontent.google.com/download"
    fields = {}
    for im in re.finditer(r"<input\b[^>]*>", m.group(1), re.I):
        t = im.group(0)
        nm, val = re.search(r'name="([^"]+)"', t), re.search(r'value="([^"]*)"', t)
        if nm and re.search(r'type="hidden"', t, re.I):
            fields[htmlmod.unescape(nm.group(1))] = htmlmod.unescape(val.group(1)) if val else ""
    if not fields.get("id"):
        return None
    action = urllib.parse.urljoin(base_url, action)
    return action + ("&" if "?" in action else "?") + urllib.parse.urlencode(fields)


def _open_file(url: str):
    """Open a download link; on Google Drive follow the virus-scan confirmation form (confirm= / uuid tokens)
    once, and report quota / private pages as DownloadError."""
    r = _open(url)
    if host_of(r.geturl()) not in DRIVE_HOSTS or "text/html" not in (r.headers.get("Content-Type") or "").lower():
        return r
    with r:
        text = r.read(400_000).decode("utf-8", "replace")
        final = r.geturl()
    nxt = _drive_form_url(text, final)
    if nxt:
        r2 = _open(nxt)
        if "text/html" not in (r2.headers.get("Content-Type") or "").lower():
            return r2
        with r2:
            raise _page_error(r2.read(400_000).decode("utf-8", "replace"), r2.geturl())
    raise _page_error(text, final)


def _save(r, dest_dir: Path, fallback: str, progress=None, cancel=None) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", _filename_from(r, fallback)).strip(" .") or fallback
    if len(name) > 150:       # keep the extension: very long names break Windows paths
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + dot + ext[:8]) if dot else name[:150]
    total = int(r.headers.get("Content-Length") or 0)
    ctype = (r.headers.get("Content-Type") or "").lower()
    path = dest_dir / name
    done = 0
    with open(path, "wb") as f:
        while True:
            if cancel and cancel.is_set():
                raise RuntimeError("cancelled")
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if "text/html" in ctype and path.stat().st_size < 400_000:
        text = path.read_text("utf-8", "replace")
        path.unlink()
        raise _page_error(text, r.geturl())
    return path


def resolve(url: str) -> str:
    url = expand_short(url.strip())
    if re.search(r"mega\.(nz|co\.nz)", url, re.I):
        raise ManualDownload("Hosted on MEGA, which can't be downloaded automatically")
    if re.search(r"drive\.google\.com|drive\.usercontent\.google\.com|docs\.google\.com", url):
        fid = drive_id(url)
        if fid:
            return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    if "mediafire.com" in url:
        with _open(url, 30) as r:
            page = r.read().decode("utf-8", "replace")
        m = re.search(r'href="((?:https?:)?//download\d+\.mediafire\.com/[^"]+)"', page, re.I) or \
            re.search(r"(https?://download\d+\.mediafire\.com/[^\"'\s]+)", page, re.I)
        if not m:
            raise ManualDownload("Couldn't find the Mediafire download link")
        link = m.group(1).replace("&amp;", "&")
        return "https:" + link if link.startswith("//") else link
    if "dropbox.com" in url:
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        q["dl"] = "1"
        return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))
    return url


def download(url: str, dest_dir: Path, progress=None, cancel=None, depth: int = 0) -> list[Path]:
    """Download `url` into dest_dir; returns the downloaded files (several for a
    Google Drive folder, which is kept as a sub-folder). Failures are DownloadError (naming the host)."""
    try:
        return _download(url, dest_dir, progress, cancel, depth)
    except urllib.error.HTTPError as e:
        msg = f"{e.code} {e.reason}".strip()
        raise DownloadError(msg, host_of(e.url or url), "http", e.code) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise DownloadError(f"network error: {getattr(e, 'reason', e)}", host_of(url), "network") from e
    except ValueError as e:        # e.g. urllib's "URL can't contain control characters"
        raise DownloadError(f"invalid link: {e}", host_of(url), "network") from e


def _download(url: str, dest_dir: Path, progress=None, cancel=None, depth: int = 0) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = expand_short(url)
    folder = drive_folder_id(url)
    if folder:
        if depth > 4:
            return []
        out = []
        entries = list_drive_folder(folder)
        if not entries:
            raise DownloadError("Google Drive folder is empty or not public", "drive.google.com", "private")
        for e in entries:
            if e["folder"]:
                out += download(f"https://drive.google.com/drive/folders/{e['id']}", dest_dir / e["name"],
                                progress, cancel, depth + 1)
            else:
                with _open_file(f"https://drive.usercontent.google.com/download?id={e['id']}&export=download&confirm=t") as r:
                    out.append(_save(r, dest_dir, e["name"], progress, cancel))
        return out
    final = resolve(url)
    with _open_file(final) as r:
        return [_save(r, dest_dir, "chart.zip", progress, cancel)]
