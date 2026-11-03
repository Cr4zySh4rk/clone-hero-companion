#!/usr/bin/env python3
"""Flag hard-coded, user-visible English in the web UI sources (a heuristic, not a parser).

    python3 scripts/i18n_lint.py            # lints app.js, app.html, index.html, i18n.js

What it flags:
  * app.html / index.html: text nodes, and title / placeholder / aria-label values, that are not covered by data-i18n*
  * JS: string or template literals handed to toast( / alert( / confirm( / prompt( / ask( title:, assigned to
    .textContent / .innerHTML / .title / .placeholder, or HTML text between tags inside template literals,
    unless they go through tr( / t( / I18N.t(
Anything containing fewer than three consecutive ASCII letters (symbols, numbers, single keys) is ignored, as are the
product names and file names in ALLOW. Add a `// i18n-ok` comment on the line to exempt it.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "chcompanion" / "web"
ALLOW = {"Clone Hero", "Companion", "Clone Hero Companion", "Chorus Encore", "RhythmVerse", "CH Leaderboards", "YouTube",
         "Spotify", "Deno", "yt-dlp", "ffmpeg", "Chrome", "Edge", "Firefox", "Safari", "Brave", "cookies.txt", "OBS", "Google",
         "LRCLIB", "settings.ini", "MEGA", "WebM", "MP4", "Windows", "macOS", "Linux", "Rock Band", "SNG", ".sng", "720p", "1080p",
         "Barlow", "Companion"}
LETTERS = re.compile(r"[A-Za-z]{3,}")


def visible(text: str) -> bool:
    t = text
    for a in sorted(ALLOW, key=len, reverse=True):
        t = t.replace(a, " ")
    t = re.sub(r"\$\{[^}]*\}", " ", t)          # template expressions are code
    t = re.sub(r"\{\w+\}", " ", t)
    return bool(LETTERS.search(t))


class HtmlLint(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []     # (tag, covered by data-i18n)
        self.skip = 0
        self.problems: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        covered = any(k.startswith("data-i18n") and not k.startswith("data-i18n-t") and not k.startswith("data-i18n-p") and not k.startswith("data-i18n-a") for k in a)
        if tag not in ("br", "meta", "link", "input", "img", "hr", "source"):
            self.stack.append((tag, covered))
        for k in ("title", "placeholder", "aria-label", "alt"):
            if k in a and a[k] and visible(a[k]) and f"data-i18n-{k.replace('aria-label', 'aria')}" not in a:
                self.problems.append((self.getpos()[0], f'{k}="{a[k]}"'))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.skip or not data.strip():
            return
        if any(c for _t, c in self.stack):
            return
        if self.stack and self.stack[-1][0] == "title":      # <title> is replaced from data-title-key
            return
        if visible(data):
            self.problems.append((self.getpos()[0], data.strip()[:60]))


def lint_html(path: Path) -> list[tuple[int, str]]:
    p = HtmlLint()
    p.feed(path.read_text("utf-8"))
    return p.problems


CALLS = re.compile(r"""(?<![\w.$])(toast|alert|confirm|prompt)\(\s*(["'`])""")
ASSIGN = re.compile(r"""\.(textContent|title|placeholder|innerText)\s*=\s*(["'`])""")
ASK = re.compile(r"""\b(title|text|ok)\s*:\s*(["'`])""")
HTML_TEXT = re.compile(r">([^<>{}$`]*[A-Za-z]{3,}[^<>{}$`]*)<")
INNER = re.compile(r"""\.innerHTML\s*=\s*(["'`])""")
ATTR = re.compile(r"""\s(title|placeholder|aria-label)=\\?"([^"$]*[A-Za-z]{3,}[^"$]*)\\?\"""")


def literal_at(src: str, i: int, quote: str) -> str:
    """Text of the literal starting at src[i] (just after the opening quote), up to its closing quote."""
    out, depth = [], 0
    while i < len(src):
        c = src[i]
        if c == "\\":
            out.append(src[i:i + 2]); i += 2; continue
        if quote == "`" and src.startswith("${", i):
            depth += 1
        elif depth and c == "}":
            depth -= 1
        elif not depth and c == quote:
            break
        out.append(c); i += 1
    return "".join(out)


def lint_js(path: Path) -> list[tuple[int, str]]:
    src = path.read_text("utf-8")
    problems: list[tuple[int, str]] = []
    lines = src.splitlines()

    def add(pos: int, text: str):
        ln = src.count("\n", 0, pos) + 1
        if "i18n-ok" in lines[ln - 1]:
            return
        problems.append((ln, text.strip()[:70]))

    for rx in (CALLS, ASSIGN, ASK):
        for m in rx.finditer(src):
            lit = literal_at(src, m.end(), m.group(2))
            if visible(lit):
                add(m.start(), lit)
    # HTML text and attributes inside template literals / strings that become markup
    for m in re.finditer(r"`([^`]*)`", src, re.S):
        body = m.group(1)
        if "<" not in body:
            continue
        flat = re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "${}", body)
        for t in HTML_TEXT.finditer(flat):
            if visible(t.group(1)):
                add(m.start(), t.group(1))
        for t in ATTR.finditer(flat):
            if visible(t.group(2)):
                add(m.start(), f"{t.group(1)}={t.group(2)}")
    for m in re.finditer(r"'(<[^'\n]*)'|\"(<[^\"\n]*)\"", src):
        flat = m.group(1) or m.group(2)
        for t in HTML_TEXT.finditer(flat):
            if visible(t.group(1)):
                add(m.start(), t.group(1))
    return sorted(set(problems))


def lint_all() -> dict[str, list[tuple[int, str]]]:
    out = {}
    for name in ("app.html", "index.html"):
        out[name] = lint_html(WEB / name)
    for name in ("app.js", "i18n.js"):
        out[name] = lint_js(WEB / name)
    # the Now Playing page keeps its script inline
    inline = re.search(r"<script>\n(.*?)</script>", (WEB / "index.html").read_text("utf-8"), re.S)
    if inline:
        tmp = WEB / ".index-inline.js"
        tmp.write_text(inline.group(1), "utf-8")
        try:
            out["index.html (script)"] = lint_js(tmp)
        finally:
            tmp.unlink()
    return out


def main() -> int:
    bad = 0
    for name, probs in lint_all().items():
        for ln, text in probs:
            print(f"{name}:{ln}: {text}")
            bad += 1
    print("hard-coded English:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
