#!/usr/bin/env python3
"""Validate a translation catalog against en.json.

    python3 scripts/i18n_check.py <lang> [--strict]     (e.g. ru, zh-CN, pt-BR)
    python3 scripts/i18n_check.py --all

Checks: valid JSON object of non-empty strings; exactly the keys of en.json (plural families compared by base
key); the same {placeholders} (a plural form other than `other` may leave out {count}); the plural forms the
language needs (see PLURAL_FORMS in chcompanion/i18n.py); the same HTML tags in markup strings.
An empty catalog ({}) is reported as "not translated yet" and passes unless --strict is given
(the app then shows English).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from chcompanion.i18n import LANGUAGES, OPTIONAL_FORMS, PLURAL_FORMS  # noqa: E402

I18N_DIR = ROOT / "chcompanion" / "web" / "i18n"
FORMS = ("zero", "one", "two", "few", "many", "other")
VAR = re.compile(r"\{(\w+)\}")
TAG = re.compile(r"</?\w+[^>]*>")


def split_plural(key: str):
    head, _, last = key.rpartition(".")
    return (head, last) if last in FORMS and head else (key, None)


def families(cat: dict) -> dict[str, dict[str, str]]:
    """base key -> {form or '': text}. A key is a plural family when a '.other' form exists."""
    bases = {split_plural(k)[0] for k in cat if split_plural(k)[1] == "other"}
    out: dict[str, dict[str, str]] = {}
    for k, v in cat.items():
        base, form = split_plural(k)
        if base in bases and form:
            out.setdefault(base, {})[form] = v
        else:
            out.setdefault(k, {})[""] = v
    return out


def load(path: Path):
    return json.loads(path.read_text("utf-8"))


def check(lang: str, strict: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    if lang not in LANGUAGES:
        return [f"unknown language {lang!r} (supported: {', '.join(LANGUAGES)})"], notes
    en = load(I18N_DIR / "en.json")
    path = I18N_DIR / f"{lang}.json"
    try:
        cat = load(path)
    except (OSError, ValueError) as e:
        return [f"{path.name}: cannot read as JSON: {e}"], notes
    if not isinstance(cat, dict):
        return [f"{path.name}: top level must be an object"], notes
    if lang == "en":
        pass
    elif not cat:
        (errors if strict else notes).append(f"{lang}: empty catalog, not translated yet (the app falls back to English)")
        return errors, notes
    for k, v in cat.items():
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{k}: value must be a non-empty string")
    fen, fl = families(en), families(cat)
    for k in sorted(set(fen) - set(fl)):
        errors.append(f"missing key: {k}")
    for k in sorted(set(fl) - set(fen)):
        errors.append(f"unexpected key: {k}")
    need = set(PLURAL_FORMS[lang])
    ok_forms = need | set(OPTIONAL_FORMS.get(lang, ()))
    for base in sorted(set(fen) & set(fl)):
        en_forms, forms = fen[base], fl[base]
        en_plural = "" not in en_forms
        if en_plural:
            if "" in forms:
                errors.append(f"{base}: must use plural forms {sorted(need)} (got a single string)")
                continue
            for f in sorted(need - set(forms)):
                errors.append(f"{base}: missing plural form .{f}")
            for f in sorted(set(forms) - ok_forms):
                errors.append(f"{base}: plural form .{f} is not used by {lang} (use {sorted(need)})")
        elif set(forms) != {""}:
            errors.append(f"{base}: must be a single string, not plural forms")
            continue
        want_vars = set(VAR.findall(en_forms.get("other", en_forms.get("", ""))))
        want_tags = Counter(TAG.findall(en_forms.get("other", en_forms.get("", ""))))
        for f, text in forms.items():
            name = f"{base}.{f}" if f else base
            got = set(VAR.findall(text))
            allowed_missing = {"count"} if (f and f != "other" and "count" in want_vars) else set()
            if got - want_vars or (want_vars - got) - allowed_missing:
                errors.append(f"{name}: placeholders {sorted(got)} != English {sorted(want_vars)}")
            if Counter(TAG.findall(text)) != want_tags:
                errors.append(f"{name}: HTML tags differ from English")
            if "{{" in text or "}}" in text or re.search(r"\{\s+\w+\s*\}|\{\w+\s+\}", text):
                errors.append(f"{name}: malformed placeholder braces")
    return errors, notes


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    args = [a for a in argv if not a.startswith("--")]
    langs = list(LANGUAGES) if "--all" in argv else args
    if not langs:
        print(__doc__)
        return 2
    bad = 0
    for lang in langs:
        errors, notes = check(lang, strict)
        for n in notes:
            print("note:", n)
        for e in errors:
            print(f"{lang}: {e}")
        print(f"{lang}: {'OK' if not errors else str(len(errors)) + ' problem(s)'}")
        bad += bool(errors)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
