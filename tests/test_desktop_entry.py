"""Linux desktop entry writer (Settings > Install desktop entry) and the AppDir helper."""
import tempfile
import unittest
from pathlib import Path

from chcompanion import desktop_entry as de


def fake_icons(root: Path) -> Path:
    d = root / "icons"
    d.mkdir()
    for s in de.ICON_SIZES:
        (d / f"{s}.png").write_bytes(b"png%d" % s)
    return d


class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"HOME": str(self.tmp), "XDG_DATA_HOME": str(self.tmp / "share")}
        self.icons = fake_icons(self.tmp)

    def test_text_has_required_keys(self):
        t = de.desktop_text("/x/App.AppImage")
        for line in ("Exec=/x/App.AppImage", "Icon=CloneHeroCompanion", "StartupWMClass=CloneHeroCompanion",
                     "Categories=Game;AudioVideo;", "Name=Clone Hero Companion", "Type=Application"):
            self.assertIn(line, t.splitlines())

    def test_exec_quoting(self):
        self.assertEqual(de.quote_exec("/opt/a.AppImage"), "/opt/a.AppImage")
        self.assertEqual(de.quote_exec("/My Apps/a.AppImage"), '"/My Apps/a.AppImage"')
        self.assertEqual(de.quote_exec("/a/50%/b"), '"/a/50%%/b"')

    def test_install_writes_entry_and_icons_then_uninstalls(self):
        app = self.tmp / "My Apps" / "CHC.AppImage"
        app.parent.mkdir()
        app.write_text("x")
        self.assertFalse(de.is_installed(self.env))
        target = de.install(self.env, launcher=str(app), icons=self.icons)
        self.assertEqual(target, self.tmp / "share" / "applications" / "CloneHeroCompanion.desktop")
        self.assertTrue(de.is_installed(self.env))
        self.assertIn(f'Exec="{app}"', target.read_text())
        for s in de.ICON_SIZES:
            self.assertTrue((self.tmp / "share" / "icons" / "hicolor" / f"{s}x{s}" / "apps"
                             / "CloneHeroCompanion.png").exists())
        self.assertTrue(de.uninstall(self.env))
        self.assertFalse(de.is_installed(self.env))

    def test_default_location_is_local_share(self):
        self.assertEqual(de.data_home({"HOME": "/h"}), Path("/h/.local/share"))

    def test_install_without_launcher_fails_clearly(self):
        with self.assertRaises(RuntimeError):
            de.install(self.env, launcher=None, icons=self.icons) if de.launcher_path(self.env) is None else self.skipTest("frozen")

    def test_install_reports_unwritable_target(self):
        (self.tmp / "share").write_text("a file, not a folder")
        with self.assertRaises(RuntimeError):
            de.install(self.env, launcher="/x/a", icons=self.icons)

    def test_launcher_prefers_appimage_env(self):
        f = self.tmp / "a.AppImage"
        f.write_text("x")
        self.assertEqual(de.launcher_path({"APPIMAGE": str(f)}), str(f))

    def test_build_appdir(self):
        ad = self.tmp / "AppDir"
        de.build_appdir(ad, self.icons)
        self.assertIn("Exec=CloneHeroCompanion", (ad / "CloneHeroCompanion.desktop").read_text())
        self.assertTrue((ad / ".DirIcon").exists() and (ad / "CloneHeroCompanion.png").exists())
        self.assertTrue((ad / "usr/share/icons/hicolor/512x512/apps/CloneHeroCompanion.png").exists())


if __name__ == "__main__":
    unittest.main()
