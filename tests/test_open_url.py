import os
import subprocess
import sys
import unittest
from unittest import mock

from chcompanion import process


class FakeProc:
    def __init__(self, code):
        self.code = code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.code


class OpenUrlLinuxTests(unittest.TestCase):
    """2.11.0 Linux AppImage: "Sign in" opened no browser. webbrowser passed the bundle's LD_LIBRARY_PATH (Qt,
    OpenSSL) to the browser, which then failed to start."""

    def setUp(self):
        p = [mock.patch.object(process.sys, "platform", "linux"), mock.patch.object(process.sys, "frozen", True, create=True),
             mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/.mount_chc/usr/lib", "LD_LIBRARY_PATH_ORIG": "",
                                          "QT_PLUGIN_PATH": "/tmp/.mount_chc/qt", "PYTHONHOME": "/tmp/.mount_chc"})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_xdg_open_gets_the_host_environment(self):
        calls = []
        with mock.patch.object(process.subprocess, "Popen", side_effect=lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc(0)), \
                mock.patch("webbrowser.open") as wb:
            self.assertTrue(process.open_url("https://example.com/login"))
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["xdg-open", "https://example.com/login"])
        self.assertNotIn("LD_LIBRARY_PATH", kw["env"])          # the bundle's libraries stay in the bundle
        self.assertNotIn("QT_PLUGIN_PATH", kw["env"])
        self.assertNotIn("PYTHONHOME", kw["env"])
        self.assertTrue(kw["start_new_session"])
        wb.assert_not_called()

    def test_falls_through_openers_until_one_works(self):
        seq = iter([OSError("no xdg-open"), FakeProc(4), FakeProc(0)])
        def popen(cmd, **kw):
            r = next(seq)
            if isinstance(r, Exception):
                raise r
            popen.cmds.append(cmd[0])
            return r
        popen.cmds = []
        with mock.patch.object(process.subprocess, "Popen", side_effect=popen), mock.patch("webbrowser.open") as wb:
            self.assertTrue(process.open_url("https://x.test/"))
        self.assertEqual(popen.cmds, ["gio", "kde-open5"])
        wb.assert_not_called()

    def test_still_running_opener_counts_as_opened(self):
        with mock.patch.object(process.subprocess, "Popen", return_value=FakeProc(None)):
            self.assertTrue(process.open_url("https://x.test/"))

    def test_webbrowser_is_the_last_resort(self):
        with mock.patch.object(process.subprocess, "Popen", side_effect=OSError("none")), \
                mock.patch("webbrowser.open", return_value=True) as wb:
            self.assertTrue(process.open_url("https://x.test/"))
        wb.assert_called_once_with("https://x.test/")


class OpenUrlOtherPlatformsTests(unittest.TestCase):
    def test_mac_and_windows_use_webbrowser(self):
        for plat in ("darwin", "win32"):
            with mock.patch.object(process.sys, "platform", plat), mock.patch("webbrowser.open", return_value=True) as wb, \
                    mock.patch.object(process.subprocess, "Popen") as po:
                self.assertTrue(process.open_url("https://x.test/"))
            wb.assert_called_once()
            po.assert_not_called()


class SignInUsesOpenUrlTests(unittest.TestCase):
    def test_cloud_sign_in_opens_the_browser_through_open_url(self):
        import tempfile
        from pathlib import Path
        from chcompanion import cloud
        with tempfile.TemporaryDirectory() as d:
            c = cloud.Cloud(session_path=Path(d) / "s.json")
        self.assertIs(c.open_browser, process.open_url)


if __name__ == "__main__":
    unittest.main()


class NoBrowserFallbackTests(unittest.TestCase):
    def test_google_sign_in_reports_when_no_browser_opened(self):
        import tempfile
        from pathlib import Path
        from chcompanion import cloud
        with tempfile.TemporaryDirectory() as d:
            c = cloud.Cloud(session_path=Path(d) / "s.json", open_browser=lambda url: False)
            url = c.start_login(47823)
            self.assertIn("/auth/v1/authorize?", url)
            self.assertFalse(c.browser_opened)
            c2 = cloud.Cloud(session_path=Path(d) / "s2.json", open_browser=lambda url: True)
            c2.start_login(47823)
            self.assertTrue(c2.browser_opened)
