"""Copy the yt-dlp plugin packages the app needs (bgutil PO token provider) to build/ytdlp_plugins/yt_dlp_plugins.

yt-dlp finds plugins by looking for a `yt_dlp_plugins/` folder on disk (sys.path entries), so in a PyInstaller build
they must ship as plain data files (--add-data "build/ytdlp_plugins/yt_dlp_plugins:yt_dlp_plugins"), not inside
the compressed Python archive. A relative source path keeps the Windows drive-letter colon out of --add-data."""
import shutil
import sys
from pathlib import Path

import yt_dlp_plugins

dst = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ytdlp_plugins") / "yt_dlp_plugins"
shutil.rmtree(dst.parent, ignore_errors=True)
src = next(Path(p) for p in yt_dlp_plugins.__path__ if (Path(p) / "extractor" / "getpot_bgutil_http.py").is_file())
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
print("staged", src, "->", dst, sorted(p.name for p in (dst / "extractor").iterdir()))
