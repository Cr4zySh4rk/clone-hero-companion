"""LIVE check (not a unit test): resolve real favourites and GET only the first bytes of each candidate's
download URL. Prints the candidates in order with the HTTP status (up to 5 per favourite, in the order the app would try them).
Run: python3 scripts/live_check_downloads.py"""
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chcompanion import cloud                       # noqa: E402
from chcompanion.net import urlopen                 # noqa: E402
from chcompanion.sources import fallback, hosts     # noqa: E402

FAVS = [   # (title, artist, charter) as RhythmVerse / Encore list them
    ("Rockstars", "(Germany) Malik Harris", "Sad Normie"),              # RV-hosted: path with spaces and brackets
    ("Rockstar - Extended", "LiSA", "thomas2OO80k (thomas2OO80k)"),     # RV-hosted zip
    ("Rockstar (Post Malone Cover)", "Daddy Rock", "Chezy"),            # Drive file (+ a second upload)
    ("Rockstar Nailbomb!", "The Fall of Troy", "xX760Xx"),              # Drive file that is over quota
    ("Rockstar", "Nickelback", "Harmonix"),                             # on Encore and RV
    ("Flames (feat. Avril Lavigne)", "Mod Sun, Avril Lavigne", "Yhughu"),   # Drive folder, RV only
    ("Rockstar", "Nickelback", "Nobody Charts"),                        # no such charter: must find nothing
]


def probe(item):
    url = hosts.resolve(hosts.normalize_url(item["downloadUrl"]))
    if hosts.drive_folder_id(url):
        return "folder", "Google Drive folder (not fetched)"
    req = urllib.request.Request(url, headers={"User-Agent": hosts.UA, "Range": "bytes=0-63"})
    try:
        with urlopen(req, timeout=30) as r:
            head = r.read(64)
            ctype = r.headers.get("Content-Type", "")
            note = ctype
            if "html" in ctype:
                note += " -> " + str(hosts._page_error(head.decode("utf-8", "replace") + "", r.geturl()))
            return r.status, f"{note} first bytes={head[:4]!r} host={hosts.host_of(r.geturl())}"
    except urllib.error.HTTPError as e:
        return e.code, f"{e.reason} host={hosts.host_of(e.url or url)}"
    except Exception as e:
        return "ERR", repr(e)


for name, artist, charter in FAVS:
    row = {"artist": artist, "name": name, "charter": charter, "source": {}}
    print(f"\n== {artist} - {name} [{charter}]")
    try:
        cands = cloud.resolve_candidates(row)
    except Exception as e:
        print("  lookup failed:", e)
        continue
    if not cands:
        print("  no candidate by that charter")
    for c in cands[:5]:
        status, note = probe(c)
        print(f"  {fallback.source_name(c):12} {status}  {c['downloadUrl'][:90]}  {note}")
