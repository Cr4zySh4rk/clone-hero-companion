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


def find_data_dir(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    for p in candidate_data_dirs():
        if (p / "settings.ini").exists() or (p / "Logs").is_dir():
            return p
    return None


def read_settings(data_dir: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(data_dir / "settings.ini", encoding="utf-8")
    except Exception:
        pass
    return cp


def song_dirs(data_dir: Path, extra: list[str] | None = None) -> list[Path]:
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
