"""Mandatory update check: version compare, blocking rules, 426 gating, per-OS install commands. No real network or installs."""
import hashlib
import http.client
import io
import json
import os
import tarfile
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from chcompanion import app, cli, macperm, process, updater
from chcompanion.updater import Updater

PFX = updater.DOWNLOAD_PREFIX + "v2.5.0/"


def asset(name, data=b"payload", digest=None):
    return {"name": name, "browser_download_url": PFX + name, "size": len(data),
            "digest": digest or ("sha256:" + hashlib.sha256(data).hexdigest())}


def release(tag="v2.5.0", assets=(), **kw):
    return dict({"tag_name": tag, "draft": False, "prerelease": False, "body": "## Notes\n- fixed <b>x</b>",
                 "html_url": "https://github.com/Cr4zySh4rk/clone-hero-companion/releases/tag/" + tag,
                 "assets": list(assets)}, **kw)


class Resp:
    def __init__(self, body: bytes, length=True):
        self._b = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if length else {}

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, url="x"):
    return urllib.error.HTTPError(url, code, "err", {}, None)


def fake_net(api=None, files=None):
    """A net.urlopen replacement: `api` is a release dict (or an exception), `files` maps URL -> bytes."""
    def urlopen(req, timeout=15.0):
        url = req.full_url if hasattr(req, "full_url") else req
        if url == updater.API_URL:
            if isinstance(api, Exception):
                raise api
            return Resp(json.dumps(api).encode())
        if url in (files or {}):
            return Resp(files[url])
        raise http_error(404, url)
    return urlopen


class VersionTests(unittest.TestCase):
    def test_semver_ordering(self):
        self.assertTrue(updater.is_newer("2.10.0", "2.9.1"))
        self.assertTrue(updater.is_newer("v2.5.0", "2.4.0"))
        self.assertFalse(updater.is_newer("2.4.0", "2.4.0"))
        self.assertFalse(updater.is_newer("2.9.1", "2.10.0"))
        self.assertFalse(updater.is_newer("v2.4.0", "2.4.0"))
        self.assertTrue(updater.is_newer("2.4.1", "2.4.0"))

    def test_prerelease_sorts_below_release_and_numerically(self):
        self.assertTrue(updater.is_newer("2.5.0", "2.5.0-rc.1"))
        self.assertTrue(updater.is_newer("2.5.0-rc.10", "2.5.0-rc.2"))
        self.assertFalse(updater.is_newer("2.5.0-rc.1", "2.5.0"))

    def test_garbage_is_never_newer(self):
        for tag in ("", None, "latest", "2.5", "v2.5.x", "nightly-2026"):
            self.assertFalse(updater.is_newer(tag, "2.4.0"), tag)

    def test_prerelease_and_draft_releases_are_ignored(self):
        for payload in (release("v2.6.0-rc.1"), release(prerelease=True), release(draft=True), release("nightly")):
            with mock.patch.object(updater.net, "urlopen", fake_net(api=payload)):
                self.assertEqual(updater.fetch_latest(), ("ok", None), payload["tag_name"])

    def test_assets_outside_the_repo_are_dropped(self):
        payload = release(assets=[asset("a.msi"), {"name": "evil.msi", "browser_download_url": "https://evil.example/evil.msi", "size": 1}])
        with mock.patch.object(updater.net, "urlopen", fake_net(api=payload)):
            _, rel = updater.fetch_latest()
        self.assertEqual([a["name"] for a in rel["assets"]], ["a.msi"])


class FetchFallbackTests(unittest.TestCase):
    def test_api_rate_limited_falls_back_to_redirect_tag(self):
        loc = "https://github.com/Cr4zySh4rk/clone-hero-companion/releases/tag/v2.5.0"
        with mock.patch.object(updater.net, "urlopen", fake_net(api=http_error(403))), \
                mock.patch.object(updater, "_redirect_location", return_value=loc):
            status, rel = updater.fetch_latest()
        self.assertEqual((status, rel["version"], rel["source"], rel["assets"]), ("ok", "2.5.0", "redirect", []))

    def test_redirect_to_prerelease_tag_is_ignored(self):
        with mock.patch.object(updater.net, "urlopen", fake_net(api=http_error(429))), \
                mock.patch.object(updater, "_redirect_location", return_value="https://github.com/x/y/releases/tag/v3.0.0-beta.1"):
            self.assertEqual(updater.fetch_latest(), ("ok", None))

    def test_both_unreachable_is_offline(self):
        with mock.patch.object(updater.net, "urlopen", fake_net(api=urllib.error.URLError("no network"))), \
                mock.patch.object(updater, "_redirect_location", side_effect=urllib.error.URLError("no network")):
            self.assertEqual(updater.fetch_latest(), ("offline", None))
        self.assertIn("no network", updater.LAST_ERROR)

    def test_failed_check_retries_soon_not_in_six_hours(self):
        u = Updater(current="2.5.0")
        waits = []
        stop = u._stop
        real_set = stop.set
        def fake_wait(t):
            waits.append(t)
            if len(waits) >= 3:
                real_set()
            return stop.is_set()
        results = iter([("offline", None), ("offline", None), ("ok", None)])
        with mock.patch.object(updater, "fetch_latest", side_effect=lambda *a, **k: next(results)), \
                mock.patch.object(updater, "disabled", return_value=False), \
                mock.patch.object(u._stop, "wait", side_effect=fake_wait), \
                mock.patch.object(updater.threading, "Thread", side_effect=lambda target, **k: mock.Mock(start=target)):
            u.start()
        self.assertEqual(waits, [updater.RETRY_AFTER_S[0], updater.RETRY_AFTER_S[1], updater.CHECK_EVERY_S])
        self.assertEqual(u.public()["status"], "current")

    def test_redirect_location_reads_the_location_header(self):
        err = urllib.error.HTTPError(updater.PAGE_URL, 302, "Found", {"Location": "https://github.com/a/b/releases/tag/v9.9.9"}, None)
        opener = mock.Mock()
        opener.open.side_effect = err
        with mock.patch.object(updater.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(updater._redirect_location(updater.PAGE_URL, 1).endswith("/tag/v9.9.9"))


class BlockingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(updater.SKIP_ENV, None)

    def checked(self, current, state, rel):
        u = Updater(current=current)
        with mock.patch.object(updater, "fetch_latest", return_value=(state, rel)):
            u.check()
        return u

    def rel(self, tag):
        return {"tag": tag, "version": updater.plain_version(tag), "notes": "n", "url": "u", "assets": [], "source": "api"}

    def test_blocks_when_newer(self):
        u = self.checked("2.4.0", "ok", self.rel("v2.5.0"))
        self.assertTrue(u.required)
        p = u.public()
        self.assertEqual((p["status"], p["latest"], p["current"], p["notes"]), ("available", "2.5.0", "2.4.0", "n"))

    def test_does_not_block_when_equal_older_or_no_release(self):
        for cur, rel in (("2.5.0", self.rel("v2.5.0")), ("2.6.0", self.rel("v2.5.0")), ("2.5.0", None)):
            u = self.checked(cur, "ok", rel)
            self.assertFalse(u.required)
            self.assertEqual(u.public()["status"], "current")

    def test_offline_does_not_block(self):
        u = self.checked("2.4.0", "offline", None)
        self.assertFalse(u.required)
        self.assertEqual(u.public()["status"], "offline")

    def test_going_offline_does_not_unblock_a_known_update(self):
        u = self.checked("2.4.0", "ok", self.rel("v2.5.0"))
        with mock.patch.object(updater, "fetch_latest", return_value=("offline", None)):
            u.check()
        self.assertTrue(u.required)

    def test_update_applied_elsewhere_unblocks(self):
        u = self.checked("2.4.0", "ok", self.rel("v2.5.0"))
        u.current = "2.5.0"
        with mock.patch.object(updater, "fetch_latest", return_value=("ok", self.rel("v2.5.0"))):
            u.check()
        self.assertFalse(u.required)


class SkipEnvTests(unittest.TestCase):
    def test_skip_env_disables_everything(self):
        with mock.patch.dict(os.environ, {updater.SKIP_ENV: "1"}), \
                mock.patch.object(updater, "fetch_latest", side_effect=AssertionError("no network when skipped")):
            u = Updater(current="2.4.0")
            u.check()
            u.start()
            self.assertFalse(u.required)
            self.assertEqual(u.public()["status"], "disabled")
            self.assertEqual(updater.check_once(), {"state": "skipped", "latest": None})
            self.assertIn("skipped", cli.update_check_line())

    def test_skip_env_unblocks_even_a_known_update(self):
        u = Updater(current="2.4.0")
        u.status, u.release = "available", {"version": "2.5.0", "tag": "v2.5.0", "notes": "", "url": "u", "assets": []}
        with mock.patch.dict(os.environ, {updater.SKIP_ENV: "1"}):
            self.assertFalse(u.required)
        with mock.patch.dict(os.environ):
            os.environ.pop(updater.SKIP_ENV, None)
            self.assertTrue(u.required)

    def test_doctor_line_never_blocks_and_reports_each_state(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(updater.SKIP_ENV, None)
            for state, latest, want in (("current", "2.5.0", "up to date"), ("available", "9.0.0", "update available 9.0.0"),
                                        ("offline", None, "offline")):
                with mock.patch.object(updater, "check_once", return_value={"state": state, "latest": latest}):
                    self.assertIn(want, cli.update_check_line())
            with mock.patch.object(updater, "check_once", side_effect=RuntimeError("boom")):
                self.assertIn("offline", cli.update_check_line())


class GatingTests(unittest.TestCase):
    """The real HTTP handler with a stub companion."""

    def setUp(self):
        self.addCleanup(mock.patch.dict(os.environ).stop)
        os.environ.pop(updater.SKIP_ENV, None)
        os.environ["CHC_ACCEPT_TERMS"] = "1"       # unittest (CI) doesn't read conftest.py; these tests are about the update gate
        self.u = Updater(quit_event=threading.Event(), current="2.4.0")
        self.comp = types.SimpleNamespace(updater=self.u, adjust_offset=mock.Mock(), quit_requested=self.u.quit_event,
                                          settings=lambda: {}, simulate=mock.Mock(), libmgr=mock.Mock())
        self.old = app.Handler.companion
        self.srv = app.serve(self.comp, "127.0.0.1", 0)
        self.addCleanup(lambda: setattr(app.Handler, "companion", self.old))
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, json.dumps(body or {}) if method == "POST" else None, {"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def require_update(self):
        self.u.status = "available"
        self.u.release = {"tag": "v2.5.0", "version": "2.5.0", "notes": "## hi", "url": "https://example/r", "assets": [], "source": "api"}

    def test_state_changing_calls_pass_when_no_update_is_required(self):
        self.assertEqual(self.call("POST", "/api/offset", {"kind": "lyrics", "delta": 0.1})[0], 200)
        self.comp.adjust_offset.assert_called_once()

    def test_state_changing_calls_get_426_when_update_is_required(self):
        self.require_update()
        for path in ("/api/offset", "/api/library/rescan", "/api/settings", "/api/songs/action", "/api/install"):
            code, body = self.call("POST", path, {})
            self.assertEqual(code, 426, path)
            self.assertTrue(body["updateRequired"])
            self.assertEqual((body["latest"], body["current"]), ("2.5.0", "2.4.0"))
        self.comp.adjust_offset.assert_not_called()

    def test_update_screen_progress_about_and_quit_stay_available(self):
        self.require_update()
        code, body = self.call("GET", "/api/update")
        self.assertEqual((code, body["required"], body["latest"], body["notes"]), (200, True, "2.5.0", "## hi"))
        self.assertEqual(self.call("GET", "/static/update.js")[0], 200)
        self.assertEqual(self.call("GET", "/")[0], 200)
        with mock.patch.object(updater, "fetch_latest", return_value=("ok", self.u.release)):
            self.assertEqual(self.call("POST", "/api/update/check")[0], 200)
        self.assertEqual(self.call("POST", "/api/quit")[0], 200)
        self.assertTrue(self.u.quit_event.is_set())

    def test_install_endpoint_starts_the_install_while_blocked(self):
        self.require_update()
        with mock.patch.object(Updater, "_run_install") as run:
            code, body = self.call("POST", "/api/update/install")
            self.assertEqual((code, body["started"]), (200, True))
            for _ in range(50):
                if run.called:
                    break
                threading.Event().wait(0.02)
            run.assert_called_once()

    def test_install_refused_when_nothing_to_install(self):
        self.assertFalse(self.u.start_install())


class AssetTests(unittest.TestCase):
    A = [{"name": n, "url": PFX + n, "size": 1, "digest": ""} for n in (
        "CloneHeroCompanion-windows-x64.msi", "CloneHeroCompanion-macOS-universal.dmg", "CloneHeroCompanion-linux-x64.tar.gz",
        "CloneHeroCompanion-linux-x86_64.AppImage", "CloneHeroCompanion-linux-aarch64.AppImage", "SHA256SUMS.txt")]

    def names(self, plat, appimage=False, machine="x86_64"):
        return [a["name"] for a in updater.pick_assets(self.A, plat, appimage, machine)]

    def test_per_os_asset_choice(self):
        self.assertEqual(self.names("win32"), ["CloneHeroCompanion-windows-x64.msi"])
        self.assertEqual(self.names("darwin"), ["CloneHeroCompanion-macOS-universal.dmg"])
        self.assertEqual(self.names("linux")[0], "CloneHeroCompanion-linux-x64.tar.gz")
        lin = self.names("linux", appimage=True)
        self.assertEqual(lin[0], "CloneHeroCompanion-linux-x86_64.AppImage")
        self.assertEqual(self.names("linux", True, "aarch64")[0], "CloneHeroCompanion-linux-aarch64.AppImage")

    def test_guessed_names_when_the_asset_list_is_unknown(self):
        g = updater.guess_assets("v2.5.0", "linux", appimage=True)
        self.assertTrue(g[0]["name"].endswith(".AppImage") and g[0]["url"].startswith(PFX) and g[-1]["name"].endswith(".tar.gz"))


class InstallBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.quit = threading.Event()
        self.u = Updater(quit_event=self.quit, current="2.4.0")
        self.u.quit_delay = 0
        self.u.status = "available"
        self.popen = mock.Mock()
        for target, new in ((process, "popen"), (tempfile, "gettempdir")):
            p = mock.patch.object(target, new, self.popen if new == "popen" else (lambda: str(self.tmp / "tmp")))
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(updater.SKIP_ENV, None)
        os.environ.pop("APPIMAGE", None)

    def release(self, assets, files):
        self.u.release = {"tag": "v2.5.0", "version": "2.5.0", "notes": "", "url": "https://example/r", "source": "api",
                          "assets": [{"name": a["name"], "url": a["browser_download_url"], "size": a["size"], "digest": a["digest"]} for a in assets]}
        p = mock.patch.object(updater.net, "urlopen", fake_net(files=files))
        p.start()
        self.addCleanup(p.stop)

    def wait_quit(self):
        self.assertTrue(self.quit.wait(2))


class WindowsInstallTests(InstallBase):
    def test_downloads_the_msi_and_launches_msiexec_detached_then_quits(self):
        a = asset("CloneHeroCompanion-windows-x64.msi", b"MSI" * 1000)
        self.release([a], {a["browser_download_url"]: b"MSI" * 1000})
        self.u._install_windows()
        self.wait_quit()
        args, kw = self.popen.call_args
        msi = self.tmp / "tmp" / "CloneHeroCompanion-update" / "CloneHeroCompanion-windows-x64.msi"
        self.assertEqual(args[0], ["msiexec", "/i", str(msi)])
        self.assertEqual(msi.read_bytes(), b"MSI" * 1000)
        self.assertTrue(kw["creationflags"] & 0x8 and kw["creationflags"] & 0x200)     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        self.assertEqual(self.u.install["phase"], "done")

    def test_first_click_already_reports_the_download_starting(self):
        # 2.7.0 replied "idle" and the screen didn't change for 5 s, so people clicked Update now twice
        a = asset("CloneHeroCompanion-windows-x64.msi", b"MSI" * 1000)
        self.release([a], {a["browser_download_url"]: b"MSI" * 1000})
        started = threading.Event()
        with mock.patch.object(updater.Updater, "_run_install", lambda self: started.wait(1)), \
                mock.patch.object(updater.sys, "platform", "win32"):
            self.assertTrue(self.u.start_install())
            self.assertEqual(self.u.public()["install"]["phase"], "downloading")
            started.set()

    def test_truncated_download_is_rejected_and_nothing_is_launched(self):
        a = asset("CloneHeroCompanion-windows-x64.msi", b"MSI" * 1000)
        self.release([a], {a["browser_download_url"]: b"MSI" * 10})
        with mock.patch.object(updater.sys, "platform", "win32"):
            self.u.start_install()
            for _ in range(100):
                if self.u.install["phase"] == "error":
                    break
                threading.Event().wait(0.02)
        self.assertEqual(self.u.install["phase"], "error")
        self.assertIn("incomplete", str(self.u.install["error"]))
        self.popen.assert_not_called()
        self.assertFalse(self.quit.is_set())
        self.assertFalse(list((self.tmp / "tmp" / "CloneHeroCompanion-update").glob("*.msi")))

    def test_checksum_mismatch_is_rejected(self):
        a = asset("CloneHeroCompanion-windows-x64.msi", b"good", digest="sha256:" + "0" * 64)
        self.release([a], {a["browser_download_url"]: b"good"})
        with self.assertRaises(updater.UpdateError) as cm:
            self.u._install_windows()
        self.assertIn("checksum", str(cm.exception.msg))
        self.popen.assert_not_called()

    def test_release_without_an_installer_reports_it(self):
        self.release([asset("notes.txt")], {})
        with self.assertRaises(updater.UpdateError):
            self.u._install_windows()

    def test_command_builders(self):
        self.assertEqual(updater.windows_command(Path("a.msi")), ["msiexec", "/i", "a.msi"])


class MacInstallTests(InstallBase):
    def setUp(self):
        super().setUp()
        self.app = self.tmp / "Applications" / "Clone Hero Companion.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "version").write_text("old")
        a = asset("CloneHeroCompanion-macOS-universal.dmg", b"DMG" * 100)
        self.release([a], {a["browser_download_url"]: b"DMG" * 100})
        p = mock.patch.object(macperm, "app_bundle", return_value=self.app)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def fake_run(self, args, **kw):
        self.calls.append(list(args))
        if args[0] == "hdiutil" and args[1] == "attach":
            mnt = Path(args[args.index("-mountpoint") + 1])
            (mnt / "Clone Hero Companion.app" / "Contents").mkdir(parents=True)
            (mnt / "Clone Hero Companion.app" / "Contents" / "version").write_text("new")
        elif args[0] == "ditto":
            import shutil
            shutil.copytree(args[1], args[2])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_swaps_the_bundle_attaches_and_detaches_then_relaunches_with_open_n(self):
        with mock.patch.object(process, "run", side_effect=self.fake_run):
            self.u._install_macos()
        self.wait_quit()
        self.assertEqual((self.app / "Contents" / "version").read_text(), "new")
        self.assertEqual([c[0:2] for c in self.calls if c[0] == "hdiutil"], [["hdiutil", "attach"], ["hdiutil", "detach"]])
        ditto = next(c for c in self.calls if c[0] == "ditto")
        self.assertEqual(Path(ditto[2]).parent, self.app.parent)                       # staged beside the app: same volume, atomic swap
        self.assertEqual(sorted(p.name for p in self.app.parent.iterdir()), ["Clone Hero Companion.app"])   # no staging leftovers
        relaunch = self.popen.call_args[0][0]
        self.assertIn("open -n", relaunch[2])
        self.assertEqual(relaunch[-1], str(self.app))

    def test_falls_back_to_opening_the_dmg_when_the_app_location_is_not_writable(self):
        def deny(args, **kw):
            if args[0] == "ditto":
                raise PermissionError("read-only")
            return self.fake_run(args, **kw)
        with mock.patch.object(process, "run", side_effect=deny):
            self.u._install_macos()
        self.assertEqual(self.u.install["phase"], "manual")
        self.assertEqual((self.app / "Contents" / "version").read_text(), "old")
        self.assertEqual(self.popen.call_args[0][0][0], "open")
        self.assertTrue(str(self.popen.call_args[0][0][1]).endswith(".dmg"))
        self.assertFalse(self.quit.wait(0.2))
        self.assertTrue(any(c[:2] == ["hdiutil", "detach"] for c in self.calls))

    def test_failed_swap_keeps_the_old_app(self):
        real_rename = os.rename

        def rename(src, dst):
            if str(dst) == str(self.app) and str(src).endswith('.new'):   # the staged copy can't be moved in; rollback must work
                raise PermissionError("no")
            return real_rename(src, dst)
        with mock.patch.object(process, "run", side_effect=self.fake_run), mock.patch.object(updater.os, "rename", rename):
            self.u._install_macos()
        self.assertEqual(self.u.install["phase"], "manual")
        self.assertEqual((self.app / "Contents" / "version").read_text(), "old")

    def test_not_running_from_a_bundle_opens_the_dmg(self):
        with mock.patch.object(macperm, "app_bundle", return_value=None), mock.patch.object(process, "run") as run:
            self.u._install_macos()
        run.assert_not_called()
        self.assertEqual(self.u.install["phase"], "manual")


class LinuxInstallTests(InstallBase):
    def setUp(self):
        super().setUp()
        (self.tmp / "home").mkdir()
        p = mock.patch.object(Path, "home", return_value=self.tmp / "home")
        p.start()
        self.addCleanup(p.stop)

    def tarball(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), 0o755
                tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_appimage_is_replaced_in_place_and_relaunched(self):
        img = b"\x7fELF-new-appimage"
        a = asset("CloneHeroCompanion-linux-x86_64.AppImage", img)
        self.release([a, asset("CloneHeroCompanion-linux-x64.tar.gz", b"t")], {a["browser_download_url"]: img})
        target = self.tmp / "bin" / "Companion.AppImage"
        target.parent.mkdir()
        target.write_bytes(b"old")
        os.environ.update(APPIMAGE=str(target), APPDIR="/tmp/.mount_x", ARGV0="x")
        self.u._install_linux()
        self.wait_quit()
        self.assertEqual(target.read_bytes(), img)
        self.assertTrue(os.access(target, os.X_OK))
        args, kw = self.popen.call_args
        self.assertEqual(args[0][-1], str(target))
        self.assertTrue(kw["start_new_session"])
        self.assertNotIn("APPDIR", kw["env"])
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["Companion.AppImage"])

    def test_targz_install_is_swapped_in_and_relaunched(self):
        data = self.tarball({"CloneHeroCompanion": b"new-binary"})
        a = asset("CloneHeroCompanion-linux-x64.tar.gz", data)
        self.release([a], {a["browser_download_url"]: data})
        exe = self.tmp / "opt" / "CloneHeroCompanion"
        exe.parent.mkdir()
        exe.write_bytes(b"old-binary")
        with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(updater.sys, "executable", str(exe)):
            self.u._install_linux()
        self.wait_quit()
        self.assertEqual(exe.read_bytes(), b"new-binary")
        self.assertEqual(sorted(p.name for p in exe.parent.iterdir()), ["CloneHeroCompanion"])
        self.assertEqual(self.popen.call_args[0][0][-1], str(exe))

    def test_targz_with_a_folder_layout(self):
        data = self.tarball({"CloneHeroCompanion/CloneHeroCompanion": b"new", "CloneHeroCompanion/_internal/lib.so": b"lib2"})
        a = asset("CloneHeroCompanion-linux-x64.tar.gz", data)
        self.release([a], {a["browser_download_url"]: data})
        inst = self.tmp / "opt"
        (inst / "_internal").mkdir(parents=True)
        (inst / "CloneHeroCompanion").write_bytes(b"old")
        (inst / "_internal" / "lib.so").write_bytes(b"lib1")
        with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(updater.sys, "executable", str(inst / "CloneHeroCompanion")):
            self.u._install_linux()
        self.assertEqual((inst / "CloneHeroCompanion").read_bytes(), b"new")
        self.assertEqual((inst / "_internal" / "lib.so").read_bytes(), b"lib2")
        self.assertEqual(sorted(p.name for p in inst.iterdir()), ["CloneHeroCompanion", "_internal"])

    def test_cannot_replace_opens_the_download_folder(self):
        data = self.tarball({"CloneHeroCompanion": b"new"})
        a = asset("CloneHeroCompanion-linux-x64.tar.gz", data)
        self.release([a], {a["browser_download_url"]: data})
        with mock.patch.object(updater.sys, "frozen", False, create=True):          # running from source: nothing to replace
            self.u._install_linux()
        self.assertEqual(self.u.install["phase"], "manual")
        self.assertEqual(self.popen.call_args[0][0][0], "xdg-open")
        self.assertFalse(self.quit.wait(0.2))

    def test_unsafe_archive_is_refused(self):
        data = self.tarball({"../evil": b"x", "CloneHeroCompanion": b"new"})
        a = asset("CloneHeroCompanion-linux-x64.tar.gz", data)
        self.release([a], {a["browser_download_url"]: data})
        exe = self.tmp / "opt" / "CloneHeroCompanion"
        exe.parent.mkdir()
        exe.write_bytes(b"old")
        with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(updater.sys, "executable", str(exe)):
            self.u._install_linux()
        self.assertEqual(exe.read_bytes(), b"old")
        self.assertFalse((self.tmp / "evil").exists())
        self.assertEqual(self.u.install["phase"], "manual")

    def test_dispatch_picks_the_installer_for_the_platform(self):
        for plat, name in (("win32", "_install_windows"), ("darwin", "_install_macos"), ("linux", "_install_linux")):
            with mock.patch.object(updater.sys, "platform", plat), mock.patch.object(Updater, name) as m:
                self.u._run_install()
                m.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class BannerCssTests(unittest.TestCase):
    def test_hidden_attribute_beats_the_banner_display_rule(self):
        # #updBanner{display:flex} overrode [hidden], so "Couldn't check for updates" never went away (2.6.1)
        js = (Path(__file__).resolve().parent.parent / "chcompanion" / "web" / "update.js").read_text(encoding="utf-8")
        self.assertIn("#updBanner[hidden]", js)
        self.assertIn("display:none!important", js)
