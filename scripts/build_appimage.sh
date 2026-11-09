#!/usr/bin/env bash
# Wrap the PyInstaller onedir output in an AppDir and build the AppImage.
#   scripts/build_appimage.sh <onedir> <icons-dir> <out.AppImage> [appimagetool]
# <onedir> is dist/CloneHeroCompanion, <icons-dir> is build/icons (scripts/make_icon.py). Run from the repo root.
set -euo pipefail
ONEDIR=$(realpath "$1"); ICONS=$(realpath "$2"); OUT=$(realpath -m "$3"); TOOL=${4:-appimagetool}
APPDIR=$(mktemp -d)/CloneHeroCompanion.AppDir
mkdir -p "$APPDIR/usr/lib"
cp -a "$ONEDIR" "$APPDIR/usr/lib/CloneHeroCompanion"
install -m 755 packaging/linux/AppRun "$APPDIR/AppRun"
python3 -m chcompanion.desktop_entry --appdir "$APPDIR" "$ICONS"

# Trim what the window never uses: Qt UI translations (English is the fallback), Chromium locale packs except
# en-US, the on-screen keyboard, EGLFS, and the GTK theme plugin (it would drag the bundle's GTK into the host's).
I="$APPDIR/usr/lib/CloneHeroCompanion/_internal"
find "$I/PySide6/Qt/translations" -maxdepth 1 -name '*.qm' -delete 2>/dev/null || true
find "$I/PySide6/Qt/translations/qtwebengine_locales" -name '*.pak' ! -name 'en-US.pak' -delete 2>/dev/null || true
rm -rf "$I"/PySide6/Qt/plugins/platforminputcontexts "$I"/PySide6/Qt/plugins/egldeviceintegrations \
       "$I"/PySide6/Qt/plugins/platformthemes/libqgtk3.so "$I"/libgtk-3.so.0 \
       "$I"/libQt6VirtualKeyboard*.so* "$I"/libQt6EglFS*.so* "$I"/libQt6EglFsKms*.so*

mkdir -p "$(dirname "$OUT")"
chmod -R u+rwX,go+rX "$APPDIR"
ARCH=x86_64 "$TOOL" --comp zstd --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
ls -l "$OUT"
