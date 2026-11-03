"""Backend translations: `_("key", name=value)` looks the key up in the same JSON catalogs the web UI uses
(chcompanion/web/i18n/<lang>.json). English is the source and the fallback for any missing key.

`_()` returns a `Msg`, a `str` subclass that remembers its key and variables. Anything that stores one (a job's
step/result/error) keeps the key, and the HTTP layer renders it with `render()` when it is served, so switching
the language re-translates what is already in the queue. A Msg also reads as ordinary English text in code
that handles it as a plain string.

Plurals: pass `count=n`; the catalog holds `key.one` / `key.few` / `key.many` / `key.other`.
"""
from __future__ import annotations

import json
import locale
import re
import threading

from .paths import resource_path

DEFAULT = "en"
LANGUAGES = {"en": "English", "ru": "Русский", "zh-CN": "简体中文", "ko": "한국어", "ja": "日本語",
             "pt-BR": "Português (Brasil)", "es": "Español", "fr": "Français", "de": "Deutsch"}

LANG = DEFAULT
_cache: dict[str, dict[str, str]] = {}
_lock = threading.Lock()
_VAR = re.compile(r"\{(\w+)\}")


def plural_category(lang: str, n) -> str:
    """CLDR plural category for an integer count (the subset the app ships)."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return "other"
    base = lang.split("-")[0]
    if base in ("ja", "ko", "zh"):
        return "other"
    if base == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if base in ("fr", "pt"):
        return "one" if n in (0, 1) else "other"
    return "one" if n == 1 else "other"


PLURAL_FORMS = {"en": ("one", "other"), "de": ("one", "other"), "es": ("one", "other"), "fr": ("one", "other"),
                "pt-BR": ("one", "other"), "ru": ("one", "few", "many", "other"),
                "ja": ("other",), "ko": ("other",), "zh-CN": ("other",)}
OPTIONAL_FORMS = {"es": ("many",), "fr": ("many",), "pt-BR": ("many",)}   # CLDR "many" (millions); falls back to other


def normalize(tag: str | None) -> str:
    """Best supported language for a locale tag such as 'pt_BR', 'zh-Hans-CN' or 'ko-KR'; '' if none."""
    if not tag:
        return ""
    t = tag.replace("_", "-").split(".")[0].split("@")[0]
    low = t.lower()
    if low.startswith("zh"):
        return "zh-CN" if not any(x in low for x in ("hant", "-tw", "-hk", "-mo")) else ""
    base = low.split("-")[0]
    if base == "pt":
        return "pt-BR"
    for code in LANGUAGES:
        if code.lower() == base:
            return code
    return ""


def resolve(setting: str | None, locales=()) -> str:
    """The language to use: an explicit setting, else the first supported locale, else English."""
    if setting and setting != "auto":
        return normalize(setting) or DEFAULT
    for tag in locales or ():
        n = normalize(tag)
        if n:
            return n
    return DEFAULT


def os_locales() -> list[str]:
    try:
        loc = locale.getlocale()[0] or locale.getdefaultlocale()[0]   # noqa: deprecated but handy
    except Exception:
        loc = None
    return [loc] if loc else []


def catalog(lang: str) -> dict[str, str]:
    with _lock:
        if lang not in _cache:
            try:
                data = json.loads(resource_path("web", "i18n", f"{lang}.json").read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
            _cache[lang] = data if isinstance(data, dict) else {}
        return _cache[lang]


def has(key: str) -> bool:
    return key in catalog(DEFAULT)


def set_lang(lang: str | None) -> str:
    global LANG
    LANG = normalize(lang) or DEFAULT
    return LANG


def _find(key: str, lang: str, count) -> str | None:
    for code in ([lang, DEFAULT] if lang != DEFAULT else [DEFAULT]):
        cat = catalog(code)
        if count is not None:
            c = plural_category(code, count)
            for k in (f"{key}.{c}", f"{key}.other"):
                if k in cat:
                    return cat[k]
        if key in cat:
            return cat[key]
    return None


def translate(key: str, lang: str | None = None, **vars) -> str:
    lang = lang or LANG
    if key == "_join":      # a list of Msgs/strings joined in the active language
        return str(vars.get("sep", "; ")).join(str(render(i, lang)) for i in vars.get("items", ()))
    text = _find(key, lang, vars.get("count"))
    if text is None:
        return key
    vars = {k: render(v, lang) if isinstance(v, Msg) else v for k, v in vars.items()}
    return _VAR.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text)


class Msg(str):
    """Translatable text: the str value is the text in the language active when it was made."""
    key: str
    vars: dict

    def __new__(cls, key: str, **vars):
        o = super().__new__(cls, translate(key, None, **vars))
        o.key, o.vars = key, vars
        return o

    def __reduce__(self):
        return (_rebuild, (self.key, self.vars))


def _rebuild(key, vars):
    return Msg(key, **vars)


def _(key: str, **vars) -> Msg:
    return Msg(key, **vars)


def join(items, sep: str = "; ") -> Msg:
    return Msg("_join", items=list(items), sep=sep)


def render(x, lang: str | None = None) -> str:
    """Translate a Msg in `lang` (default: the active language); plain strings are returned unchanged."""
    if isinstance(x, Msg):
        return translate(x.key, lang, **x.vars)
    return x


def deep_render(obj, lang: str | None = None):
    """Copy of a JSON-able structure with every Msg replaced by its translation in the active language."""
    if isinstance(obj, Msg):
        return render(obj, lang)
    if isinstance(obj, dict):
        return {k: deep_render(v, lang) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [deep_render(v, lang) for v in obj]
    return obj


def msg_of(exc: BaseException):
    """The Msg an exception was raised with (so it can be re-translated), else its plain text."""
    as_msg = getattr(exc, "as_msg", None)
    if callable(as_msg):
        return as_msg()
    if exc.args and isinstance(exc.args[0], Msg):
        return exc.args[0]
    return str(exc) or type(exc).__name__
