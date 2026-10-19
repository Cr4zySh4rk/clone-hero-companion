"""Locate the Clone Hero data folder and our own config/cache folder."""
from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

APP_NAME = "CloneHeroCompanion"


def _windows_documents() -> Path | None:
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        g = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                 (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = Path(p.value)
            ctypes.windll.ole32.CoTaskMemFree(p)
            return path
    except Exception:
        pass
    return None


def candidate_data_dirs() -> list[Path]:
    home = Path.home()
    c: list[Path] = []
    env = os.environ.get("CLONEHERO_DATA_DIR")
    if env:
        c.append(Path(env))
    if sys.platform == "win32":
        docs = _windows_documents()
        if docs:
            c.append(docs / "Clone Hero")
        c += [home / "Documents" / "Clone Hero", home / "OneDrive" / "Documents" / "Clone Hero"]
    elif sys.platform == "darwin":
        c += [home / "Clone Hero", home / "Documents" / "Clone Hero",
              home / "Library" / "Application Support" / "Clone Hero"]
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        c += [home / ".clonehero", home / "Clone Hero"]
        if xdg:
            c.append(Path(xdg) / "clonehero")
        c.append(home / ".local" / "share" / "clonehero")
    return c


def looks_like_data_dir(p: Path) -> bool:
    try:
        return (p / "settings.ini").is_file() or (p / "Logs").is_dir()
    except OSError:
        return False


def find_data_dir(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    cands = candidate_data_dirs()
    for p in [c for c in cands if not is_protected(c)] + [c for c in cands if is_protected(c)]:
        if looks_like_data_dir(p):
            return p
    for g in find_games():               # portable installs keep their data next to the game
        d = game_dir(g)
        if looks_like_data_dir(d):
            return d
    return None


# ---------------------------------------------------------------- game location
def _game_names() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("Clone Hero.app", "CloneHero.app", "clonehero.app")
    if sys.platform == "win32":
        return ("Clone Hero.exe", "CloneHero.exe", "clonehero.exe")
    return ("clonehero", "Clone Hero", "clonehero.x86_64", "Clone Hero.x86_64", "CloneHero.AppImage",
            "clonehero.AppImage", "Clone Hero.AppImage")


def is_game(p: Path) -> bool:
    if "companion" in p.name.lower():      # that's us, not the game
        return False
    try:
        if sys.platform == "darwin":
            return p.suffix.lower() == ".app" and p.is_dir() and "clone" in p.name.lower()
        squashed = p.name.lower().replace(" ", "").replace("_", "").replace("-", "")
        return p.is_file() and (p.name.lower() in {n.lower() for n in _game_names()}
                                or (squashed.startswith("clonehero")
                                    and not squashed.endswith((".ini", ".log", ".txt", ".json", ".dll", ".so"))))
    except OSError:
        return False


def game_dir(game: Path) -> Path:
    """Folder that holds the game (for an .app bundle, the folder the bundle is in)."""
    return game.parent


def _search_roots() -> list[Path]:
    home = Path.home()
    roots = [home, home / "Desktop", home / "Downloads", home / "Documents", home / "Games", home / "Applications"]
    if sys.platform == "darwin":
        roots += [Path("/Applications")]
    elif sys.platform == "win32":
        for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
            v = os.environ.get(k)
            if v:
                roots += [Path(v), Path(v) / "Programs"]
        docs = _windows_documents()
        if docs:
            roots.append(docs)
        for drive in "CDEFG":
            for sub in ("Games", "Program Files", "Clone Hero"):
                roots.append(Path(f"{drive}:/") / sub)
    else:
        roots += [home / ".local" / "share", home / "bin", Path("/opt"), Path("/usr/local/games"), Path("/usr/games")]
    return roots


def _protected_roots() -> list[Path]:
    """macOS asks the user before an app may look inside these; only go there when nothing
    was found elsewhere (a folder picked with Browse is always allowed)."""
    if sys.platform != "darwin":
        return []
    home = Path.home()
    return [home / "Desktop", home / "Documents", home / "Downloads", home / "Library" / "Mobile Documents"]


def is_protected(p: Path) -> bool:
    for r in _protected_roots():
        if p == r or r in p.parents:
            return True
    return False


def find_games(configured: str | None = None, limit: int = 8, deep: bool = False) -> list[Path]:
    """Clone Hero installs: the configured one first, then a shallow search of the usual places.
    On macOS, Desktop/Documents/Downloads are searched only if nothing else turned up (or deep)."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path):
        k = str(p)
        if k not in seen and is_game(p):
            seen.add(k)
            out.append(p)

    if configured:
        add(Path(configured).expanduser())
    names = _game_names()
    roots = [r for r in _search_roots() if not is_protected(r)]
    late = [r for r in _search_roots() if is_protected(r)]
    for root in roots + late:
        if root in late and out and not deep:
            break
        if len(out) >= limit:
            break
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        # depth 0-2 below each root, only looking into folders that mention clone hero (cheap)
        for n in names:
            add(root / n)
        try:
            kids = [c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")]
        except OSError:
            continue
        for c in kids:
            low = c.name.lower().replace(" ", "")
            if "clone" not in low and "hero" not in low:
                continue
            add(c)                                     # e.g. /Applications/Clone Hero.app
            for n in names:
                add(c / n)
            try:
                for g in c.iterdir():
                    if g.is_dir():
                        add(g)
                        for n in names:
                            add(g / n)
            except OSError:
                pass
    return out[:limit]


def detect(configured_game: str | None = None) -> dict:
    """Everything we can find automatically, for the setup screen."""
    games = find_games(configured_game)
    datas: list[Path] = []
    cands = candidate_data_dirs() + [game_dir(g) for g in games]
    for p in [c for c in cands if not is_protected(c)]:
        if p not in datas and looks_like_data_dir(p):
            datas.append(p)
    if not datas:                          # only now look in places macOS asks about
        for p in [c for c in cands if is_protected(c)]:
            if p not in datas and looks_like_data_dir(p):
                datas.append(p)
    songs: list[Path] = []
    for d in datas:
        for s in song_dirs(d):
            if s not in songs:
                songs.append(s)
    for g in games:
        s = game_dir(g) / "Songs"
        if s.is_dir() and s not in songs:
            songs.append(s)
    return {"games": [str(g) for g in games], "dataDirs": [str(d) for d in datas],
            "songDirs": [str(s) for s in songs], "platform": sys.platform,
            "defaultDataDir": str(candidate_data_dirs()[0] if candidate_data_dirs() else Path.home() / "Clone Hero")}


def describe_dir(p: str | Path, kind: str) -> dict:
    """Quick validation info for a chosen folder."""
    p = Path(p).expanduser()
    d = {"path": str(p), "exists": p.exists()}
    if kind == "data":
        d.update(settingsIni=(p / "settings.ini").is_file(), logs=(p / "Logs").is_dir(),
                 ok=looks_like_data_dir(p))
    elif kind == "songs":
        n = 0
        if p.is_dir():
            try:
                for _root, _dirs, files in os.walk(p):
                    low = [f.lower() for f in files]
                    if "song.ini" in low:
                        n += 1
                    n += sum(1 for f in low if f.endswith(".sng"))
                    if n >= 5000:
                        break
            except OSError:
                pass
        d.update(songs=n, ok=p.is_dir())
    elif kind == "game":
        d.update(ok=is_game(p))
    return d


def read_settings(data_dir: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(data_dir / "settings.ini", encoding="utf-8")
    except Exception:
        pass
    return cp


def song_dirs(data_dir: Path, extra: list[str] | None = None, only: list[str] | None = None) -> list[Path]:
    """Song folders: the ones chosen in setup (`only`), else Clone Hero's own list + <data>/Songs."""
    if only:
        out = []
        for e in only:
            p = Path(e).expanduser()
            if p.is_dir() and p not in out:
                out.append(p)
        for e in extra or []:
            p = Path(e).expanduser()
            if p.is_dir() and p not in out:
                out.append(p)
        if out:
            return out
    dirs: list[Path] = []
    cp = read_settings(data_dir)
    for sect in ("directories", "custom_content_directories"):
        if cp.has_section(sect) and sect == "directories":
            for _k, v in cp.items(sect):
                v = v.strip()
                if v:
                    dirs.append(Path(v).expanduser())
    dirs.append(data_dir / "Songs")
    for e in extra or []:
        dirs.append(Path(e).expanduser())
    out, seen = [], set()
    for d in dirs:
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


def config_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(*parts: str) -> Path:
    """Path to bundled resources (works under PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = base / "chcompanion" / Path(*parts)
    if p.exists():
        return p
    return Path(__file__).resolve().parent / Path(*parts)
