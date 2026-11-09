#!/usr/bin/env python3
"""Headless layout check for translations (Playwright + Chromium).

    python3 scripts/i18n_layout_check.py --pseudo                  # 40% longer accented English, both window sizes
    python3 scripts/i18n_layout_check.py de ru ja                  # real languages (also reports leftover English)
    python3 scripts/i18n_layout_check.py --all --shots /tmp/shots  # every language, save a screenshot of every view

It starts the app on a throw-away data folder with a few songs (long names included), then for every view
(Library, Favourites, Get charts, Spotify, Setlists, Duplicates, Jobs, Profile, Leaderboards, Now Playing, Game & folders,
Settings, the song panel and the Now Playing window) and every window size it checks:
  * no two sibling interactive elements (buttons, chips, selects, inputs, links, tiles, pills) overlap,
  * no text sticks out of its box, and nothing is cut off silently (cut text must use text-overflow: ellipsis),
  * the page and the sidebar do not scroll sideways,
  * no console errors (blocked fonts / offline network calls are ignored), no uncaught exceptions,
  * no raw catalog keys on screen, and, for a real language, no visible text equal to an English catalog string.
Exit status 1 when anything is found. PLAYWRIGHT_BROWSERS_PATH defaults to /opt/pw-browsers.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
from chcompanion.i18n import LANGUAGES  # noqa: E402

SIZES = [(1400, 900), (980, 620)]           # 980x620 is the smallest main window the app allows
VIEWS = ["library", "favs", "charts", "spotify", "setlists", "dupes", "jobs", "profile", "friends", "leaderboards", "now", "setup", "settings"]
ALLOW_ENGLISH = {"Clone Hero", "Companion", "Clone Hero Companion", "Chorus Encore", "RhythmVerse", "CH Leaderboards", "YouTube",
                 "Spotify", "OBS", "Google", "Chrome", "Edge", "Firefox", "Safari", "Brave", "yt-dlp", "Deno", "Guitar", "Bass",
                 "Rhythm", "Drums", "Keys", "Vocals", "Expert", "Hard", "Medium", "Easy", "720p", "1080p", "OK", "FC", "HD",
                 "Windows", "macOS", "Linux", "Rock Band", "WebM", "MP4", "Pause", "Play", "Video", "Lyrics", "Metadata", "Charter",
                 "Album", "Jobs", "Profile", "Settings", "Folder", "Workers", "Preview", "Chart", "Setlists", "Library",
                 "Leaderboards", "Duplicates", "Full combos",
                 "Version", "Position", "Release", "Song", "Songs", "Videos"}      # also ordinary French/German words      # game terms and product names the app shows in English

CHECK_JS = r"""
() => {
  const out = { overlaps: [], overflow: [], cut: [], page: [], english: [], keys: [] };
  const vis = (el) => {
    if (!el.isConnected) return false;
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden" || e.hidden) return false;
      if (e.tagName === "DIALOG" && !e.open) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const clipped = (el, r) => {            // is the element scrolled/clipped away by an ancestor?
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (/(hidden|clip|auto|scroll)/.test(cs.overflowX + cs.overflowY)) {
        const pr = p.getBoundingClientRect();
        if (r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) return true;
      }
    }
    return false;
  };
  const desc = (el) => (el.id ? "#" + el.id : el.tagName.toLowerCase() + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/)[0] : "")) + " “" + (el.textContent || el.value || el.placeholder || "").trim().slice(0, 40) + "”";
  // 1. sibling interactive elements must not intersect
  const sel = "button, select, input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]), a[href], .tile, .pill, .chips > *, #nav > *";
  const items = [...document.querySelectorAll(sel)].filter(vis);
  const byParent = new Map();
  for (const el of items) { const p = el.parentElement; if (!byParent.has(p)) byParent.set(p, []); byParent.get(p).push(el); }
  for (const [, list] of byParent) {
    for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) {
      const a = list[i].getBoundingClientRect(), b = list[j].getBoundingClientRect();
      if (clipped(list[i], a) || clipped(list[j], b)) continue;
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 1.5 && h > 1.5) out.overlaps.push(desc(list[i]) + "  x  " + desc(list[j]));
    }
  }
  // 2. text must stay inside its box, and cut text must show an ellipsis
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const text = n.nodeValue.trim();
    const el = n.parentElement;
    if (!text || !el || ["SCRIPT", "STYLE", "OPTION", "TEXTAREA"].includes(el.tagName) || !vis(el)) continue;
    // English / key detection
    if (!seen.has(text)) {
      seen.add(text);
      if (/^[a-z]+(\.[a-z0-9_]+)+$/.test(text)) out.keys.push(text);
      if (window.__EN_VALUES && window.__EN_VALUES.has(text)) out.english.push(text);
    }
    if (el.closest("#libBody, .heat")) continue;       // virtualised rows: data, checked separately below
    const cs = getComputedStyle(el);
    const range = document.createRange(); range.selectNodeContents(n);
    const tr = range.getBoundingClientRect(), er = el.getBoundingClientRect();
    if (tr.width === 0 || clipped(el, er)) continue;
    const ox = cs.overflowX, hidden = /(hidden|clip)/.test(ox) || /(hidden|clip)/.test(cs.overflowY);
    const key = desc(el);
    if (!hidden && !/(auto|scroll)/.test(ox)) {
      // overflow visible: the text may not leave the element's own padding box (inline elements: skip, their parent is checked)
      if (cs.display !== "inline" && (tr.right > er.right + 1.5 || tr.left < er.left - 1.5)) out.overflow.push(key + " text " + Math.round(tr.width) + "px in box " + Math.round(er.width) + "px");
    } else if (hidden && el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== "ellipsis" && !/(auto|scroll)/.test(ox)) {
      out.cut.push(key + " is cut off without an ellipsis");
    }
  }
  // virtualised library rows: cell text must be ellipsised, not spilling
  for (const el of document.querySelectorAll("#libBody .row .cell, #libBody .row .t b, #libBody .row .t span, #libHead > div")) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    if (el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== "ellipsis") out.cut.push(desc(el) + " cut without ellipsis");
  }
  // 3. nothing scrolls sideways
  const de = document.documentElement;
  if (de.scrollWidth > innerWidth + 1) out.page.push("page scrolls sideways: " + de.scrollWidth + " > " + innerWidth);
  for (const id of ["side", "main", "dock"]) { const e = document.getElementById(id); if (e && e.scrollWidth > e.clientWidth + 1) out.page.push("#" + id + " overflows sideways"); }
  for (const e of document.querySelectorAll(".view.on, .settings, .profile, .results, .bar, .selbar, dialog[open]")) {
    if (vis(e) && e.scrollWidth > e.clientWidth + 1 && !/(auto|scroll)/.test(getComputedStyle(e).overflowX)) out.page.push(desc(e) + " content wider than its box (" + e.scrollWidth + " > " + e.clientWidth + ")");
  }
  return out;
}
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def seed(root: Path):
    songs = root / "Clone Hero" / "Songs"
    rows = [("Zeitgeist der Straßenmusikanten und ihrer Gitarren", "Die Übergroße Kapelle", "Eine sehr lange Albumbezeichnung", "Wolkenkratzer-Charter"),
            ("Short", "Band", "LP", "Al"), ("Подобный длинный заголовок композиции", "Исполнитель", "Альбом", "Чартер"),
            ("日本語のとても長い曲名", "アーティスト", "アルバム", "チャーター"), ("A", "B", "C", "D")]
    for i in range(14):
        t, a, al, ch = rows[i % len(rows)]
        d = songs / f"{a} - {t} {i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "song.ini").write_text(f"[song]\nname = {t} {i}\nartist = {a}\nalbum = {al}\ncharter = {ch}\nyear = {2000 + i}\n"
                                    f"song_length = {120000 + i * 7000}\ndiff_guitar = 3\ndiff_drums = 2\ndiff_bass = 1\n", "utf-8")
        (d / "notes.chart").write_text("[Song]\n{\n  Name = \"x\"\n  Resolution = 192\n}\n[SyncTrack]\n{\n  0 = TS 4\n  0 = B 120000\n}\n"
                                       "[ExpertSingle]\n{\n  192 = N 0 0\n}\n", "utf-8")
    return root / "Clone Hero"


class App:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chc-layout-")
        base = Path(self.tmp.name)
        self.data = seed(base)
        self.port = free_port()
        env = dict(os.environ, CHC_SKIP_UPDATE_CHECK="1", XDG_CONFIG_HOME=str(base / "cfg"), HOME=str(base / "home"), APPDATA=str(base / "cfg"),
                   CLONEHERO_DATA_DIR=str(self.data))
        (base / "home").mkdir(exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "chcompanion", "run", "--no-browser", "--no-youtube", "--no-lrclib", "--no-in-game",
             "--port", str(self.port), "--data-dir", str(self.data)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/api/library/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("the app did not start")

    def post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except Exception:
            self.proc.kill()
        self.tmp.cleanup()


def run(langs, sizes, pseudo, shots: Path | None, views) -> int:
    from playwright.sync_api import sync_playwright
    app = App()
    en = json.loads((ROOT / "chcompanion/web/i18n/en.json").read_text("utf-8"))
    problems = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for lang in langs:
                app.post("/api/settings", {"settings": {"language": lang, "setup_done": True}})
                en_values = sorted({v for k, v in en.items() if len(v) >= 4 and v not in ALLOW_ENGLISH and "{" not in v and "<" not in v})
                for (w, h) in sizes:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, locale="en-US" if lang == "en" else lang)
                    page = ctx.new_page()
                    errors = []
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
                    page.on("pageerror", lambda e: errors.append("uncaught: " + str(e)))
                    q = "?pseudo=1" if pseudo else ""
                    page.goto(app.url + "/" + q)
                    page.wait_for_selector("#libBody .row", state="attached", timeout=15000)
                    page.click('#nav button[data-view="library"]')           # dismiss the first-run setup screen
                    if lang != "en" and not pseudo:
                        page.evaluate("v => { window.__EN_VALUES = new Set(v); }", en_values)
                    tag = f"{'pseudo' if pseudo else lang}@{w}x{h}"
                    scenarios = list(views)
                    for view in scenarios:
                        try:
                            if view == "detail":
                                page.click('#nav button[data-view="library"]')
                                page.click("#libBody .row >> nth=0")
                            elif view == "selection":
                                page.click('#nav button[data-view="library"]')
                                page.click("#libBody .row input[type=checkbox] >> nth=0")
                                page.click("#libBody .row input[type=checkbox] >> nth=1")
                                page.click("#runBtn")
                            elif view == "dialog":
                                page.click('#nav button[data-view="setlists"]')
                                page.click("#slNew")
                            elif view == "nowwin":
                                page.goto(app.url + "/now" + q)
                                page.wait_for_selector("#controls"); page.wait_for_timeout(800)
                                page.mouse.move(300, 300)
                                page.evaluate("document.body.classList.remove('idle-ui'); document.getElementById('help').classList.add('on')")
                            else:
                                page.goto(app.url + "/" + q) if page.url.split("?")[0].endswith("/now") else None
                                page.click(f'#nav button[data-view="{view}"]')
                        except Exception as e:                                 # a view that cannot be opened is a finding too
                            print(f"[{tag}] {view}: could not open: {str(e).splitlines()[0]}")
                            problems += 1
                            continue
                        page.wait_for_timeout(700 if view not in ("leaderboards", "profile", "charts") else 1500)
                        res = page.evaluate(CHECK_JS)
                        if view == "nowwin":
                            page.evaluate("document.getElementById('help').classList.remove('on')")
                            res2 = page.evaluate(CHECK_JS)
                            for k in res:
                                res[k] += res2[k]
                        if shots:
                            shots.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(shots / f"{lang}-{w}x{h}-{view}.png"))
                        for kind, items in res.items():
                            if kind == "english" and (lang == "en" or pseudo):
                                continue
                            for it in dict.fromkeys(items):
                                problems += 1
                                print(f"[{tag}] {view}: {kind}: {it}")
                        if view in ("selection", "dialog"):
                            page.keyboard.press("Escape")
                    for e in dict.fromkeys(errors):
                        problems += 1
                        print(f"[{tag}] console error: {e[:200]}")
                    ctx.close()
            browser.close()
    finally:
        app.close()
    print(f"layout check: {problems} problem(s)")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("langs", nargs="*", help="language codes (default: en)")
    ap.add_argument("--all", action="store_true", help="every language")
    ap.add_argument("--pseudo", action="store_true", help="pseudo-localised English (+40%% text)")
    ap.add_argument("--size", action="append", help="WxH, repeatable (default 1400x900 and 980x620)")
    ap.add_argument("--shots", type=Path, help="save a screenshot of every view into this folder")
    ap.add_argument("--views", help="comma separated subset (default: everything)")
    a = ap.parse_args(argv)
    langs = list(LANGUAGES) if a.all else (a.langs or ["en"])
    sizes = [tuple(int(x) for x in s.lower().split("x")) for s in a.size] if a.size else SIZES
    views = a.views.split(",") if a.views else VIEWS + ["detail", "selection", "dialog", "nowwin"]
    return run(langs, sizes, a.pseudo, a.shots, views)


if __name__ == "__main__":
    sys.exit(main())
