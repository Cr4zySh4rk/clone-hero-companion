#!/usr/bin/env python3
"""Submit every URL in site/sitemap.xml to IndexNow (Bing, Yandex, Seznam, Naver).

Run after the site has been deployed, because the key file must be live at
https://cloneherocompanion.com/<key>.txt before the engines will accept it:

    python3 scripts/indexnow.py            # submit
    python3 scripts/indexnow.py --dry-run  # print the payload only
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOST = "cloneherocompanion.com"
ENDPOINT = "https://api.indexnow.org/indexnow"
SITE = Path(__file__).resolve().parent.parent / "site"


def find_key():
    keys = [p.stem for p in SITE.glob("*.txt") if re.fullmatch(r"[0-9a-f]{32}", p.stem)]
    if len(keys) != 1:
        sys.exit(f"expected exactly one 32-hex key file in {SITE}, found {len(keys)}")
    key = keys[0]
    if (SITE / f"{key}.txt").read_text().strip() != key:
        sys.exit("key file content does not match its file name")
    return key


def sitemap_urls():
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.parse(SITE / "sitemap.xml").getroot()
    return [e.text.strip() for e in root.findall("s:url/s:loc", ns)]


def main():
    key = find_key()
    payload = {
        "host": HOST,
        "key": key,
        "keyLocation": f"https://{HOST}/{key}.txt",
        "urlList": sitemap_urls(),
    }
    if "--dry-run" in sys.argv:
        print(json.dumps(payload, indent=2))
        return
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("IndexNow:", r.status, r.reason)
    except urllib.error.HTTPError as e:
        # 200 OK / 202 Accepted are success; 403 means the key file is not live yet.
        print("IndexNow error:", e.code, e.reason, e.read().decode(errors="replace"))
        sys.exit(1)


if __name__ == "__main__":
    main()
