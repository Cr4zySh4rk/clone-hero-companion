"""Forgiving matching of a favourite (often recovered from a folder name or pid, so its metadata is rough)
against the charts a source returns. Artist: bracketed suffixes such as '(WaveGroup)' and a leading 'The'
are ignored. Title: equal, or equal once bracketed qualifiers and a trailing '08 are dropped (with 'Live' and
'Acoustic' significant), or very similar. Charter: must match (or be one of several credited); an empty
charter on the favourite means any, which makes the result a guess."""
from __future__ import annotations

import difflib
import re
import unicodedata

from .library import strip_tags

BAD_RE = re.compile(r"\b(demo|prototype|beta|tutorial|practice)\b", re.I)
LIVE_RE = re.compile(r"\blive\b", re.I)
ACOUSTIC_RE = re.compile(r"\bacoustic\b", re.I)
BRACKET_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
YEAR_TAG_RE = re.compile(r"\s*['’]\d\d\s*$")
FUZZY = 0.85


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", strip_tags(s or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"['’`´]", "", s)              # o' -> o, don't -> dont
    s = s.replace("/", "")                    # AC/DC -> acdc
    return re.sub(r"[^0-9a-z]+", " ", s).strip()


def artist_key(a: str) -> str:
    k = fold(BRACKET_RE.sub("", strip_tags(a or "")))
    return re.sub(r"^the ", "", k)


def title_flags(t: str) -> tuple:
    return (bool(LIVE_RE.search(t)), bool(ACOUSTIC_RE.search(t)))


def title_base(t: str) -> str:
    t = BRACKET_RE.sub("", strip_tags(t or ""))
    t = YEAR_TAG_RE.sub("", t.strip())
    k = fold(t)
    return re.sub(r"^the ", "", k)


def is_bad(t: str) -> bool:
    return bool(BAD_RE.search(t or ""))


def charters(c: str) -> set:
    """Normalised charter names, split on ',', ';', '&', '/', '+' and ' and '."""
    c = strip_tags(c or "")
    parts = re.split(r"[,;&/+]|\band\b", c, flags=re.I)
    return {k for k in (fold(p) for p in parts) if k} | ({fold(c)} if fold(c) else set())


def charter_ok(want: str, got: str) -> bool:
    w = fold(strip_tags(want or ""))
    if not w:
        return True
    have = charters(got)
    return w in have or w.replace(" ", "") in {h.replace(" ", "") for h in have}


def title_tier(want: str, got: str) -> int | None:
    """2 exact, 3 same once qualifiers are dropped, 4 similar; None when different."""
    if fold(want) == fold(got):
        return 2
    if title_flags(want) != title_flags(got):
        return None
    a, b = title_base(want), title_base(got)
    if not a or not b:
        return None
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return 3
    if difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY:
        return 4
    return None


def rank_all(row: dict, results, hashes_of, id_hit=None) -> list:
    """Every acceptable chart among results for a favourite row, best first (same charter only). Charts
    returned for an empty charter are copies marked guess=True."""
    src = row.get("source") or {}
    want_a, want_n = artist_key(row.get("artist") or ""), row.get("name") or ""
    want_c = row.get("charter") or ""
    md5s = {(row.get("checksum") or "").lower(), (src.get("encore_md5") or "").lower()} - {""}
    want_bad = is_bad(want_n)
    scored = []
    for r in results:
        if not r.get("downloadUrl") or r.get("needsConversion"):
            continue
        if id_hit and id_hit(r):
            scored.append(((0, 0, []), r, 0))
            continue
        sure = bool(md5s & hashes_of(r))
        if sure:
            tier = 1
        else:
            if want_a and artist_key(r.get("artist") or "") != want_a:
                continue
            tier = title_tier(want_n, r.get("name") or "")
            if tier is None or not charter_ok(want_c, r.get("charter") or ""):
                continue
        pen = 1 if (is_bad(r.get("name") or "") and not want_bad) else 0
        # lowest tier, no penalty, then newest: compare the first two ascending, the date descending
        scored.append(((tier, pen, _neg(str(r.get("modified") or ""))), r, tier))
    scored.sort(key=lambda t: t[0])           # stable: equal scores keep search order
    guess = not fold(want_c)
    return [{**r, "guess": True} if guess and tier != 1 and sc != (0, 0, []) else r for sc, r, tier in scored]


def rank(row: dict, results, hashes_of, id_hit=None):
    """The best chart among results for a favourite row, or None. hashes_of(r) gives the checksums of r.
    A chart returned for an empty charter is a copy marked guess=True."""
    ranked = rank_all(row, results, hashes_of, id_hit)
    return ranked[0] if ranked else None


def _neg(s: str):
    return [-ord(c) for c in s]


def find(row: dict, search, queries, per_page: int, hashes_of, id_hit=None):
    ranked = find_all(row, search, queries, per_page, hashes_of, id_hit)
    return ranked[0] if ranked else None


def find_all(row: dict, search, queries, per_page: int, hashes_of, id_hit=None) -> list:
    """Run the queries in order, merging their results; stop at the first one that gives an exact title (or
    checksum) match. Raises the first lookup error only when nothing at all could be searched."""
    seen, pool, errors, done = set(), [], [], 0
    for q in queries:
        try:
            res = (search(q, per_page=per_page) or {}).get("songs") or []
        except Exception as e:
            errors.append(e)
            continue
        done += 1
        for r in res:
            k = r.get("id") or r.get("md5") or id(r)
            if k not in seen:
                seen.add(k)
                pool.append(r)
        ranked = rank_all(row, pool, hashes_of, id_hit)
        best = ranked[0] if ranked else None
        if best is not None and (id_hit and id_hit(best) or title_tier(row.get("name") or "", best.get("name") or "") == 2
                                 or hashes_of(best) & _row_md5s(row)):
            return ranked
    if errors and not done:
        raise errors[0]
    return rank_all(row, pool, hashes_of, id_hit)


def _row_md5s(row: dict) -> set:
    src = row.get("source") or {}
    return {(row.get("checksum") or "").lower(), (src.get("encore_md5") or "").lower()} - {""}


def queries_for(row: dict, rv: bool = False) -> list[str]:
    a = re.sub(r"\s+", " ", BRACKET_RE.sub("", strip_tags(row.get("artist") or ""))).strip()
    n = (row.get("name") or "").strip()
    qs = [n, a, f"{n} {a}"] if rv else [f"{a} {n}", n, a]
    return list(dict.fromkeys(q.strip() for q in qs if q and q.strip()))
