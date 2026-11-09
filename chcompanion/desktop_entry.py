"""Linux desktop integration: the .desktop file and icons.

One template serves both the AppImage's AppDir (built in CI: `python -m chcompanion.desktop_entry --appdir DIR`)
and the optional "Install desktop entry" action in Settings, which copies the entry and icons into the user's
`~/.local/share` so the app shows up in the application menu with the right name and icon.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import process

APP_ID = "CloneHeroCompanion"          # also the window class / Wayland app_id (see gui.py)
NAME = "Clone Hero Companion"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
CATEGORIES = "Game;AudioVideo;"


def applicable() -> bool:
    return sys.platform.startswith("linux")


def desktop_text(exec_cmd: str, try_exec: str | None = None) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={NAME}",
             "Comment=Music videos and lyrics for Clone Hero", f"Exec={exec_cmd}"]
    if try_exec:
        lines.append(f"TryExec={try_exec}")
    lines += [f"Icon={APP_ID}", "Terminal=false", f"Categories={CATEGORIES}",
              f"StartupWMClass={APP_ID}", "StartupNotify=true", "Keywords=clone hero;guitar hero;lyrics;video;", ""]
    return "\n".join(lines)


def quote_exec(path: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec (spaces, quotes, %, $, backticks)."""
    if not any(c in path for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return path
    esc = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return f'"{esc}"'


def launcher_path(environ=None) -> str | None:
    """The file a menu entry should start: the AppImage itself, or the frozen executable."""
    env = os.environ if environ is None else environ
    appimage = env.get("APPIMAGE")
    if appimage and Path(appimage).exists():
        return appimage
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def icon_source_dir() -> Path | None:
    """Folder with <size>.png files: bundled as `icons/` (PyInstaller data) or built into build/icons."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for d in (base / "icons", base / "build" / "icons"):
        if (d / "256.png").exists():
            return d
    return None


def data_home(environ=None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME") or Path.home()) / ".local" / "share")


def entry_path(environ=None) -> Path:
    return data_home(environ) / "applications" / f"{APP_ID}.desktop"


def is_installed(environ=None) -> bool:
    return entry_path(environ).exists()


def install(environ=None, launcher: str | None = None, icons: Path | None = None) -> Path:
    """Write the .desktop file and icons under ~/.local/share. Raises RuntimeError with the reason."""
    launcher = launcher or launcher_path(environ)
    if not launcher:
        raise RuntimeError("not running from an AppImage or packaged executable")
    icons = icons or icon_source_dir()
    home = data_home(environ)
    try:
        if icons:
            for size in ICON_SIZES:
                src = icons / f"{size}.png"
                if src.exists():
                    dst = home / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
        target = entry_path(environ)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(desktop_text(quote_exec(launcher), try_exec=launcher), encoding="utf-8")
        target.chmod(0o755)
    except OSError as e:
        raise RuntimeError(str(e)) from e
    _refresh_caches(home)
    return target


def uninstall(environ=None) -> bool:
    home = data_home(environ)
    removed = False
    paths = [entry_path(environ)] + [home / "icons" / "hicolor" / f"{s}x{s}" / "apps" / f"{APP_ID}.png"
                                     for s in ICON_SIZES]
    for p in paths:
        try:
            p.unlink()
            removed = True
        except OSError:
            pass
    if removed:
        _refresh_caches(home)
    return removed


def _refresh_caches(home: Path) -> None:
    """Best effort: menus pick the entry up on their own, these just make it instant."""
    for cmd in (["update-desktop-database", str(home / "applications")],
                ["gtk-update-icon-cache", "-q", "-t", str(home / "icons" / "hicolor")]):
        if shutil.which(cmd[0]):
            try:
                process.run(cmd, timeout=10, capture_output=True, check=False)
            except (OSError, subprocess.TimeoutExpired, process.ToolStartError):
                pass


def build_appdir(appdir: Path, icons: Path) -> None:
    """CI helper: fill an AppDir with the desktop file and the hicolor icons."""
    (appdir / "usr" / "share" / "applications").mkdir(parents=True, exist_ok=True)
    text = desktop_text(APP_ID)
    (appdir / f"{APP_ID}.desktop").write_text(text, encoding="utf-8")
    (appdir / "usr" / "share" / "applications" / f"{APP_ID}.desktop").write_text(text, encoding="utf-8")
    for size in ICON_SIZES:
        dst = appdir / "usr" / "share" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icons / f"{size}.png", dst)
    shutil.copyfile(icons / "256.png", appdir / f"{APP_ID}.png")
    shutil.copyfile(icons / "256.png", appdir / ".DirIcon")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--appdir":
        build_appdir(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit("usage: python -m chcompanion.desktop_entry --appdir APPDIR ICONS_DIR")
