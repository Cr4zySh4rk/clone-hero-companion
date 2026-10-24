"""Download regressions for packaged apps without tools on PATH."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from chcompanion import youtube
from chcompanion.align import ffmpeg_path
from chcompanion.sources.install import _extract, install_files


class PackagedDownloadTests(unittest.TestCase):
    def test_ytdlp_uses_bundled_ffmpeg_even_without_path_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "bin" / "ffmpeg.exe"
            exe.parent.mkdir()
            exe.touch()
            with mock.patch("sys._MEIPASS", tmp, create=True), \
                    mock.patch("chcompanion.align.shutil.which", return_value=None), \
                    mock.patch("chcompanion.youtube._find_tool", return_value=None):
                self.assertEqual(ffmpeg_path(), str(exe))
                self.assertEqual(youtube._ydl_opts()["ffmpeg_location"], str(exe))

    def test_missing_ffmpeg_does_not_set_invalid_ytdlp_location(self):
        with mock.patch("chcompanion.align.ffmpeg_path", return_value=None), \
                mock.patch("chcompanion.youtube._find_tool", return_value=None):
            self.assertNotIn("ffmpeg_location", youtube._ydl_opts())

    def test_extractor_uses_resolved_windows_executable_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "chart.rar"
            out = Path(tmp) / "out"
            with mock.patch("chcompanion.sources.install.shutil.which",
                            side_effect=["missing.exe", "C:/tools/bsdtar.exe"]), \
                    mock.patch("chcompanion.sources.install.subprocess.run",
                               side_effect=[FileNotFoundError(2, "missing"), mock.Mock(returncode=0)]) as run:
                _extract(archive, out)
            self.assertEqual(run.call_args.args[0][0], "C:/tools/bsdtar.exe")

    def test_long_archive_name_is_not_repeated_in_extraction_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / ("chart" * 30 + ".zip")
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Artist - Song/song.ini", "[song]\nname = Song\n")
            from chcompanion.sources import install
            original = install._extract
            def extract(src, out):
                self.assertLess(len(out.name), 20)
                original(src, out)
            with mock.patch.object(install, "cache_dir", return_value=root), \
                    mock.patch.object(install, "_extract", side_effect=extract):
                paths = install_files([archive], root / "Songs", {})
            self.assertTrue((paths[0] / "song.ini").is_file())
