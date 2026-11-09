"""Linux native-window selection (Qt platform, sandbox flags) and the browser fallback in gui.run_window."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import gui


def ok(_name):
    return None


def only(*good):
    return lambda name: None if name in good else f"{name} broken"


class PrepareQtTests(unittest.TestCase):
    def prep(self, env, probe=ok, sandbox=True):
        with mock.patch.object(gui, "_qt_dir", return_value=Path("/qt")):
            return gui.prepare_linux_qt(env, plugin_error=probe, sandbox_ok=sandbox)

    def test_noop_without_pyside(self):
        env = {}
        with mock.patch.object(gui, "_qt_dir", return_value=None):
            self.assertEqual(gui.prepare_linux_qt(env), "")
        self.assertEqual(env, {})            # pywebview may then pick GTK

    def test_x11_session(self):
        env = {"DISPLAY": ":0"}
        self.assertEqual(self.prep(env), "qt/xcb")
        self.assertEqual((env["PYWEBVIEW_GUI"], env["QT_API"], env["QT_QPA_PLATFORM"]), ("qt", "pyside6", "xcb"))

    def test_wayland_prefers_wayland_with_xcb_fallback(self):
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(self.prep(env), "qt/wayland")
        self.assertEqual(env["QT_QPA_PLATFORM"], "wayland;xcb")

    def test_wayland_plugin_broken_uses_xcb(self):
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(self.prep(env, only("xcb")), "qt/xcb")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")

    def test_x11_session_type_ignores_wayland_socket(self):
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "x11"}
        self.assertEqual(self.prep(env), "qt/xcb")

    def test_pure_wayland(self):
        env = {"WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(self.prep(env, only("wayland")), "qt/wayland")

    def test_user_platform_is_respected(self):
        env = {"QT_QPA_PLATFORM": "offscreen"}
        self.assertEqual(self.prep(env, probe=only()), "qt/offscreen")

    def test_missing_xcb_library_gives_install_hint(self):
        env = {"DISPLAY": ":0"}
        with self.assertRaises(gui.NativeWindowUnavailable) as cm:
            self.prep(env, probe=lambda n: "libxcb-cursor.so.0: cannot open shared object file")
        self.assertIn("libxcb-cursor.so.0", str(cm.exception))
        self.assertIn("apt install libxcb-cursor0", str(cm.exception))

    def test_no_display(self):
        with self.assertRaises(gui.NativeWindowUnavailable) as cm:
            self.prep({})
        self.assertIn("no display", str(cm.exception))

    def test_sandbox_flag_only_when_unusable(self):
        env = {"DISPLAY": ":0"}
        self.prep(env, sandbox=True)
        self.assertNotIn("QTWEBENGINE_CHROMIUM_FLAGS", env)
        env = {"DISPLAY": ":0", "QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
        self.prep(env, sandbox=False)
        self.assertEqual(env["QTWEBENGINE_CHROMIUM_FLAGS"], "--foo --no-sandbox")

    def test_software_render_switch(self):
        env = {"DISPLAY": ":0", "CHC_SOFTWARE_RENDER": "1"}
        self.prep(env)
        self.assertIn("--disable-gpu", env["QTWEBENGINE_CHROMIUM_FLAGS"])

    def test_sandbox_probe(self):
        self.assertFalse(gui._sandbox_usable(root=True))
        self.assertTrue(gui._sandbox_usable(root=False, read=lambda p: None))
        self.assertFalse(gui._sandbox_usable(
            root=False, read=lambda p: "1" if p.endswith("apparmor_restrict_unprivileged_userns") else None))
        self.assertFalse(gui._sandbox_usable(
            root=False, read=lambda p: "0" if p.endswith("unprivileged_userns_clone") else None))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_it(self, smoke=False, env=None):
        stop = threading.Event()
        stop.set()                              # the fallback loop returns at once
        with mock.patch.object(gui, "_browser_app", return_value=None) as app, \
                mock.patch.object(gui.webbrowser, "open") as opened, \
                mock.patch.object(gui, "_notice_once") as notice, \
                mock.patch.dict(gui.os.environ, env or {}):
            gui.run_window("http://127.0.0.1:1/", stop, self.tmp, smoke=smoke)
        return app, opened, notice

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux backend")
    def test_qt_failure_falls_back_to_browser_with_notice(self):
        err = gui.NativeWindowUnavailable("libxcb-cursor.so.0 missing")
        with mock.patch.object(gui, "prepare_linux_qt", side_effect=err):
            app, opened, notice = self.run_it()
        opened.assert_called_once()
        notice.assert_called_once()
        self.assertIn("libxcb-cursor", notice.call_args[0][1])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux backend")
    def test_smoke_mode_never_falls_back(self):
        err = gui.NativeWindowUnavailable("no display")
        with mock.patch.object(gui, "prepare_linux_qt", side_effect=err):
            app, opened, notice = self.run_it(smoke=True)
        opened.assert_not_called()
        app.assert_not_called()
        self.assertFalse(gui.SMOKE["ok"])
        self.assertIn("no display", gui.SMOKE["detail"])

    def test_explicit_opt_out_is_silent(self):
        app, opened, notice = self.run_it(env={"CHC_NO_WEBVIEW": "1"})
        opened.assert_called_once()
        notice.assert_not_called()

    def test_notice_is_shown_once(self):
        with mock.patch.object(gui.shutil, "which", return_value=None), mock.patch("builtins.print") as pr:
            gui._notice_once(self.tmp, "boom")
            gui._notice_once(self.tmp, "boom")
        self.assertEqual(pr.call_count, 1)

    def test_describe_backend_reports_fallback(self):
        with mock.patch.object(gui.sys, "platform", "linux"), \
                mock.patch.object(gui, "prepare_linux_qt", side_effect=gui.NativeWindowUnavailable("no display. hint")):
            self.assertEqual(gui.describe_backend(), "browser fallback (no display)")


class HostEnvTests(unittest.TestCase):
    """Helper programs started from the AppImage must not inherit the bundle's LD_LIBRARY_PATH."""

    def test_restores_original_library_path(self):
        from chcompanion import process
        env = {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib", "X": "1"}
        with mock.patch.object(process.sys, "platform", "linux"), mock.patch.object(process.sys, "frozen", True, create=True), \
                mock.patch.dict(process.os.environ, env, clear=True):
            out = process.host_env()
        self.assertEqual(out["LD_LIBRARY_PATH"], "/usr/local/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", out)

    def test_drops_path_when_there_was_none_and_noop_elsewhere(self):
        from chcompanion import process
        with mock.patch.object(process.sys, "platform", "linux"), mock.patch.object(process.sys, "frozen", True, create=True), \
                mock.patch.dict(process.os.environ, {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": ""}, clear=True):
            self.assertNotIn("LD_LIBRARY_PATH", process.host_env())
        with mock.patch.object(process.sys, "platform", "win32"):
            self.assertIsNone(process.host_env())


if __name__ == "__main__":
    unittest.main()
