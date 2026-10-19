"""Download charts from the hosts charters use: direct links, Google Drive (files
and public folders), Mediafire, Dropbox and link shorteners. MEGA can't be fetched
over plain HTTP, so it is reported for manual download."""
from __future__ import annotations

import html as htmlmod
import re
import urllib.parse
import urllib.request
from pathlib import Path

from ..net import urlopen

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SHORTENERS = re.compile(r"(^|\.)(bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|rb\.gy|cutt\.ly|is\.gd|shorturl\.at|adf\.ly)$", re.I)


class ManualDownload(Exception):
    """The host needs a browser (MEGA etc.)."""


def _open(url: str, timeout: float = 60.0):
    return urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"}), timeout=timeout)


def expand_short(url: str) -> str:
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


def _save(r, dest_dir: Path, fallback: str, progress=None, cancel=None) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", _filename_from(r, fallback)) or fallback
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
        if "drive.google" in r.geturl() or "Google Drive" in text:
            raise RuntimeError("Google Drive refused the download (quota exceeded or not public)")
        raise RuntimeError("The link returned a web page instead of a file")
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
    Google Drive folder, which is kept as a sub-folder)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = expand_short(url)
    folder = drive_folder_id(url)
    if folder:
        if depth > 4:
            return []
        out = []
        entries = list_drive_folder(folder)
        if not entries:
            raise RuntimeError("The Google Drive folder is empty or not public")
        for e in entries:
            if e["folder"]:
                out += download(f"https://drive.google.com/drive/folders/{e['id']}", dest_dir / e["name"],
                                progress, cancel, depth + 1)
            else:
                with _open(f"https://drive.usercontent.google.com/download?id={e['id']}&export=download&confirm=t") as r:
                    out.append(_save(r, dest_dir, e["name"], progress, cancel))
        return out
    final = resolve(url)
    with _open(final) as r:
        return [_save(r, dest_dir, "chart.zip", progress, cancel)]
