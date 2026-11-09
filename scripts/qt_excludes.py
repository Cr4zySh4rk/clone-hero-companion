#!/usr/bin/env python3
"""Print PyInstaller --exclude-module flags for every Qt module the app does not use.

qtpy (needed by pywebview's Qt backend) references every PySide6 module, so without this PyInstaller bundles all of
Qt (~900 MB). The window needs QtCore/Gui/Widgets/Network/WebChannel and QtWebEngine; anything those load
themselves (Qml, Quick, Positioning, OpenGL, ...) is still found through the binaries' own dependencies.
"""
import importlib.util
from pathlib import Path

KEEP = {"QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtWebChannel", "QtWebEngineCore", "QtWebEngineWidgets",
        "QtOpenGL", "QtOpenGLWidgets", "QtPrintSupport", "QtDBus", "QtSvg", "QtPositioning"}

spec = importlib.util.find_spec("PySide6")
mods = sorted({p.name.split(".")[0] for p in Path(spec.origin).parent.glob("Qt*.*")
               if p.name.split(".")[0].startswith("Qt") and p.name.split(".")[0] not in KEEP})
print(" ".join(f"--exclude-module {pkg}.{m}" for m in mods for pkg in ("PySide6", "qtpy")))
