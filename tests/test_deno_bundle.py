"""Bundled Deno is found before system runtimes, and Deno's cache goes to the settings folder."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import youtube

EXE = "deno.exe" if sys.platform == "win32" else "deno"


class DenoBundleTests(unittest.TestCase):
    def setUp(self):
        youtube._TOOL_CACHE.clear()
        youtube._DENO_VERSIONS.clear()
        self.addCleanup(youtube._TOOL_CACHE.clear)
        self.addCleanup(youtube._DENO_VERSIONS.clear)
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "bin").mkdir()
        self.bundled = self.tmp / "bin" / EXE
        self.bundled.write_bytes(b"x")
        self.bundled.chmod(0o755)

    def _frozen(self):
        return mock.patch.object(sys, "_MEIPASS", str(self.tmp), create=True)

    def test_bundled_deno_wins_over_system(self):
        with self._frozen(), mock.patch.object(youtube.shutil, "which", return_value="/usr/bin/deno"):
            self.assertEqual(youtube._find_tool("deno"), str(self.bundled))
            self.assertTrue(youtube.is_bundled(youtube._find_tool("deno")))
            self.assertEqual(youtube.js_runtime(), ("deno", str(self.bundled)))

    def test_system_deno_used_when_not_bundled(self):
        with mock.patch.object(youtube.shutil, "which", return_value="/usr/bin/deno"):
            self.assertEqual(youtube._find_tool("deno"), "/usr/bin/deno")
            self.assertFalse(youtube.is_bundled("/usr/bin/deno"))

    @unittest.skipIf(sys.platform == "win32", "execute bit")
    def test_non_executable_bundled_copy_is_skipped(self):
        self.bundled.chmod(0o644)
        with self._frozen(), mock.patch.object(youtube.shutil, "which", return_value="/usr/bin/deno"):
            self.assertEqual(youtube._find_tool("deno"), "/usr/bin/deno")

    def test_ydl_opts_pass_bundled_path_first(self):
        with self._frozen(), mock.patch.object(youtube.shutil, "which", return_value="/usr/bin/node"):
            opts = youtube._ydl_opts()
        self.assertEqual(opts["js_runtimes"]["deno"], {"path": str(self.bundled)})

    def test_deno_info_reports_bundled_version(self):
        done = mock.Mock(returncode=0, stdout="deno 2.9.5 (stable, release, x86_64)\nv8 1\n")
        with self._frozen(), mock.patch("chcompanion.process.run", return_value=done) as run:
            info = youtube.deno_info()
        self.assertEqual(info, {"version": "2.9.5", "path": str(self.bundled), "bundled": True})
        self.assertEqual(run.call_args.args[0], [str(self.bundled), "--version"])

    def test_deno_dir_points_into_config_dir(self):
        cfg = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("chcompanion.paths.config_dir", return_value=cfg):
            os.environ.pop("DENO_DIR", None)
            youtube.setup_deno_env()
            self.assertEqual(os.environ["DENO_DIR"], str(cfg / "deno"))
            self.assertTrue((cfg / "deno").is_dir())

    def test_existing_deno_dir_is_respected(self):
        with mock.patch.dict(os.environ, {"DENO_DIR": "/custom"}):
            youtube.setup_deno_env()
            self.assertEqual(os.environ["DENO_DIR"], "/custom")


if __name__ == "__main__":
    unittest.main()
