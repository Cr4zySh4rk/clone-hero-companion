"""2.10.0: the OS secure-storage backends (subprocess / ctypes mocked), launching Clone Hero with `--lbsToken`, and the
token never reaching the logs."""
import base64
import ctypes
import json
import logging
import os
import stat
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import api, chlink, libmgr, paths, secrets
from chcompanion.app import Companion as App

TOKEN = "SECRET-CH-SESSION-TOKEN-0123456789"


class LogCapture:
    def __init__(self):
        self.lines = []
        lines = self.lines

        class H(logging.Handler):
            def emit(h, rec):
                lines.append(rec.getMessage())
        self.h = H(level=logging.DEBUG)

    def __enter__(self):
        self.old = logging.getLogger().level
        logging.getLogger().addHandler(self.h)
        logging.getLogger().setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        logging.getLogger().removeHandler(self.h)
        logging.getLogger().setLevel(self.old)

    @property
    def text(self):
        return "\n".join(self.lines)


class SecretsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(paths, "config_dir", lambda: Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def platform(self, name):
        p = mock.patch.object(secrets.sys, "platform", name)
        p.start()
        self.addCleanup(p.stop)


class MacTests(SecretsBase):
    def setUp(self):
        super().setUp()
        self.platform("darwin")
        self.calls = []
        self.keychain = {}

    def fake_run(self, args, input=None, **kw):
        self.calls.append((list(args), input))
        if args[:2] == ["security", "-i"]:
            parts = input.split()
            self.keychain[parts[parts.index("-a") + 1]] = parts[parts.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "find-generic-password":
            name = args[args.index("-a") + 1]
            if name in self.keychain:
                return subprocess.CompletedProcess(args, 0, self.keychain[name] + "\n", "")
            return subprocess.CompletedProcess(args, 44, "", "not found")
        if args[1] == "delete-generic-password":
            self.keychain.pop(args[args.index("-a") + 1], None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    def test_set_get_delete_and_the_secret_is_never_in_argv(self):
        value = json.dumps({"token": TOKEN, "expires": 1.0})
        with mock.patch.object(secrets.subprocess, "run", self.fake_run):
            self.assertTrue(secrets.set("ch-session", value))
            self.assertEqual(secrets.get("ch-session"), value)
            secrets.delete("ch-session")
            self.assertIsNone(secrets.get("ch-session"))
        for args, _stdin in self.calls:
            self.assertNotIn(TOKEN, " ".join(args))
            self.assertNotIn(base64.b64encode(value.encode()).decode(), " ".join(args))
        first_args, first_stdin = self.calls[0]
        self.assertEqual(first_args, ["security", "-i"])                       # the secret goes on stdin
        self.assertTrue(first_stdin.startswith("add-generic-password -U -s CloneHeroCompanion -a ch-session -w "))
        self.assertEqual(self.calls[2][0], ["security", "delete-generic-password", "-s", "CloneHeroCompanion", "-a", "ch-session"])

    def test_failures_return_none_or_false(self):
        with mock.patch.object(secrets.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(secrets.set("ch-session", "x"))
            self.assertIsNone(secrets.get("ch-session"))
            secrets.delete("ch-session")                                      # no exception


class LinuxTests(SecretsBase):
    def setUp(self):
        super().setUp()
        self.platform("linux")

    def test_secret_tool_when_present(self):
        store = {}

        def run(args, input=None, **kw):
            if args[1] == "store":
                store[args[-1]] = input
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1] == "lookup":
                return subprocess.CompletedProcess(args, 0 if args[-1] in store else 1, store.get(args[-1], ""), "")
            if args[1] == "clear":
                store.pop(args[-1], None)
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)
        with mock.patch("shutil.which", lambda n: "/usr/bin/secret-tool"), mock.patch.object(secrets.subprocess, "run", run):
            self.assertTrue(secrets.set("ch-session", TOKEN))
            self.assertEqual(secrets.get("ch-session"), TOKEN)
            secrets.delete("ch-session")
            self.assertIsNone(secrets.get("ch-session"))
        self.assertEqual(list(Path(self.tmp.name).rglob("*.secret")), [])        # no plaintext file when a keyring is used

    def test_secret_tool_args_carry_no_secret(self):
        seen = []
        with mock.patch("shutil.which", lambda n: "/usr/bin/secret-tool"), \
                mock.patch.object(secrets.subprocess, "run", lambda a, input=None, **k: seen.append((a, input)) or subprocess.CompletedProcess(a, 0, "", "")):
            secrets.set("ch-session", TOKEN)
        self.assertEqual(seen[0][1], TOKEN)
        self.assertNotIn(TOKEN, " ".join(seen[0][0]))

    def test_file_fallback_is_mode_600_and_logs_no_value(self):
        with LogCapture() as logs, mock.patch("shutil.which", lambda n: None):
            self.assertTrue(secrets.set("ch-session", TOKEN))
            self.assertEqual(secrets.get("ch-session"), TOKEN)
            f = Path(self.tmp.name) / "secrets" / "ch-session.secret"
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            secrets.delete("ch-session")
            self.assertFalse(f.exists())
            self.assertIsNone(secrets.get("ch-session"))
        self.assertIn("private file", logs.text)                                  # the fallback is noted
        self.assertNotIn(TOKEN, logs.text)

    def test_falls_back_to_the_file_when_the_keyring_refuses(self):
        with mock.patch("shutil.which", lambda n: "/usr/bin/secret-tool"), \
                mock.patch.object(secrets.subprocess, "run", lambda a, input=None, **k: subprocess.CompletedProcess(a, 1, "", "no dbus")):
            self.assertTrue(secrets.set("ch-session", TOKEN))
            self.assertEqual(secrets.get("ch-session"), TOKEN)


class WindowsTests(SecretsBase):
    """DPAPI through a fake ctypes.windll whose 'encryption' is a reversible byte transform."""

    def setUp(self):
        super().setUp()
        self.platform("win32")
        self.keep = []
        self.freed = []

        def xform(inb, outb, enc):
            data = ctypes.string_at(inb._obj.pbData, inb._obj.cbData)
            data = (b"DPAPI:" + data[::-1]) if enc else data[len(b"DPAPI:"):][::-1]
            buf = ctypes.create_string_buffer(data, len(data))
            self.keep.append(buf)
            outb._obj.cbData = len(data)
            outb._obj.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
            return 1

        def protect(inb, desc, ent, res, prompt, flags, outb):
            self.flags = flags
            return xform(inb, outb, True)

        def unprotect(inb, desc, ent, res, prompt, flags, outb):
            return xform(inb, outb, False)
        windll = types.SimpleNamespace(crypt32=types.SimpleNamespace(CryptProtectData=protect, CryptUnprotectData=unprotect),
                                       kernel32=types.SimpleNamespace(LocalFree=lambda p: self.freed.append(p)))
        p = mock.patch.object(ctypes, "windll", windll, create=True)
        p.start()
        self.addCleanup(p.stop)

    def test_round_trip_encrypts_on_disk_and_deletes(self):
        self.assertTrue(secrets.set("ch-session", TOKEN))
        f = Path(self.tmp.name) / "secrets" / "ch-session.dpapi"
        self.assertTrue(f.exists())
        self.assertNotIn(TOKEN.encode(), f.read_bytes())                          # the file holds the encrypted blob only
        self.assertEqual(self.flags, 0x1)                                         # no UI; no LOCAL_MACHINE flag = current user
        self.assertEqual(secrets.get("ch-session"), TOKEN)
        self.assertTrue(self.freed)
        secrets.delete("ch-session")
        self.assertIsNone(secrets.get("ch-session"))

    def test_a_dpapi_failure_returns_false_and_none(self):
        ctypes.windll.crypt32.CryptProtectData = lambda *a: 0
        with LogCapture() as logs:
            self.assertFalse(secrets.set("ch-session", TOKEN))
        self.assertNotIn(TOKEN, logs.text)
        self.assertIsNone(secrets.get("ch-session"))


class LaunchArgsTests(unittest.TestCase):
    def launch(self, platform, path, token):
        calls = []
        with mock.patch.object(libmgr.sys, "platform", platform), \
                mock.patch.object(libmgr.subprocess, "Popen", lambda *a, **k: calls.append((a, k))), \
                mock.patch.object(libmgr.os, "startfile", lambda p: calls.append((("startfile", p), {})), create=True):
            libmgr.launch_game(Path(path), token)
        return calls

    def test_macos_app_uses_open_a_with_args_only_when_there_is_a_token(self):
        (a, k), = self.launch("darwin", "/Applications/Clone Hero.app", TOKEN)
        self.assertEqual(a[0], ["open", "-a", "/Applications/Clone Hero.app", "--args", "--lbsToken", TOKEN])
        self.assertNotIn("shell", k)
        (a, k), = self.launch("darwin", "/Applications/Clone Hero.app", None)
        self.assertEqual(a[0], ["open", "/Applications/Clone Hero.app"])

    def test_macos_plain_binary_is_exec_ed_with_the_argument(self):
        (a, k), = self.launch("darwin", "/x/Clone Hero.app/Contents/MacOS/Clone Hero", TOKEN)
        self.assertEqual(a[0], ["/x/Clone Hero.app/Contents/MacOS/Clone Hero", "--lbsToken", TOKEN])

    def test_windows_runs_the_exe_with_the_argument_or_startfile_without(self):
        (a, k), = self.launch("win32", "C:/Games/Clone Hero/Clone Hero.exe", TOKEN)
        self.assertEqual(a[0], [str(Path("C:/Games/Clone Hero/Clone Hero.exe")), "--lbsToken", TOKEN])
        self.assertFalse(k.get("shell"))
        (a, k), = self.launch("win32", "C:/Games/Clone Hero/Clone Hero.exe", None)
        self.assertEqual(a[0], "startfile")

    def test_linux_binary_gets_the_argument_without_a_shell(self):
        (a, k), = self.launch("linux", "/home/u/Clone Hero/clonehero", TOKEN)
        self.assertEqual(a[0], ["/home/u/Clone Hero/clonehero", "--lbsToken", TOKEN])
        self.assertFalse(k.get("shell"))
        self.assertEqual(k["cwd"], "/home/u/Clone Hero")
        (a, k), = self.launch("linux", "/home/u/Clone Hero/clonehero", None)
        self.assertEqual(a[0], ["/home/u/Clone Hero/clonehero"])

    def test_the_token_is_never_logged_while_launching(self):
        with LogCapture() as logs:
            self.launch("linux", "/g/clonehero", TOKEN)
            self.launch("darwin", "/g/Clone Hero.app", TOKEN)
            self.launch("win32", "C:/g/Clone Hero.exe", TOKEN)
        self.assertNotIn(TOKEN, logs.text)


class LaunchSessionTests(unittest.TestCase):
    """App.ch_launch_session: which state the launch is in, and whether a token is offered to launch_game."""

    def setUp(self):
        self.store = {}
        fake = types.SimpleNamespace(get=lambda n: self.store.get(n), set=lambda n, v: self.store.__setitem__(n, v) or True,
                                     delete=lambda n: self.store.pop(n, None) and None)
        p = mock.patch.object(chlink, "secrets", fake)
        p.start()
        self.addCleanup(p.stop)
        self.settings = {"ch_username": "W4RL0CKM0NK3Y", "ch_account_id": 83556, "ch_verified": True}

    def app(self):
        s = dict(App.DEFAULTS, **self.settings)
        return types.SimpleNamespace(settings=lambda: s, ch_account_id=lambda: App.ch_account_id(types.SimpleNamespace(settings=lambda: s)))

    def state(self):
        return App.ch_launch_session(self.app())

    def test_default_is_on_and_a_valid_token_is_offered(self):
        self.assertTrue(App.DEFAULTS["launch_signed_in"])
        chlink.save_session(TOKEN, time.time() + 3600)
        r = self.state()
        self.assertEqual((r["state"], r["token"], r["username"]), ("ready", TOKEN, "W4RL0CKM0NK3Y"))

    def test_setting_off_means_no_token(self):
        chlink.save_session(TOKEN, time.time() + 3600)
        self.settings["launch_signed_in"] = False
        r = self.state()
        self.assertEqual(r["state"], "off")
        self.assertNotIn("token", r)

    def test_expired_token_is_not_used_and_is_deleted(self):
        chlink.save_session(TOKEN, time.time() - 5)
        r = self.state()
        self.assertEqual(r["state"], "needs_signin")
        self.assertNotIn("token", r)
        self.assertEqual(self.store, {})

    def test_linked_without_a_token_needs_a_sign_in(self):
        self.assertEqual(self.state()["state"], "needs_signin")

    def test_not_linked_never_uses_a_token(self):
        chlink.save_session(TOKEN, time.time() + 3600)
        self.settings.update(ch_verified=False)
        r = self.state()
        self.assertEqual(r, {"state": "unlinked"})


class ApiTests(unittest.TestCase):
    """/api/game/launch hands the token to launch_game; /api/ch/session never shows it; Companion sign-out drops it."""

    def handle(self, c, method, path, body=None):
        h = types.SimpleNamespace(companion=c, _json=mock.Mock())
        out = []
        with mock.patch.object(api, "_json", lambda h_, data, status=200: out.append((data, status))):
            api.handle(h, method, path, {}, body or {})
        return out[-1]

    def companion(self, state, token=None):
        return types.SimpleNamespace(libmgr=None, data_dir=Path("."), settings=lambda: {"game_path": ""},
                                     ch_launch_session=lambda: dict({"state": state, "username": "u"}, **({"token": token} if token else {})))

    def test_launch_passes_the_token_only_when_ready(self):
        for state, token, needs in (("ready", TOKEN, False), ("needs_signin", None, True), ("off", None, False), ("unlinked", None, False)):
            with mock.patch.object(api, "find_game", lambda *a, **k: Path("/g/clonehero")), \
                    mock.patch.object(api, "launch_game") as lg, LogCapture() as logs:
                data, status = self.handle(self.companion(state, token), "POST", "/api/game/launch")
            lg.assert_called_once_with(Path("/g/clonehero"), token)
            self.assertEqual((data["signedIn"], data["needsSignIn"]), (state == "ready", needs))
            self.assertNotIn(TOKEN, json.dumps(data) + logs.text)

    def test_session_endpoint_never_returns_the_token(self):
        data, _ = self.handle(self.companion("ready", TOKEN), "GET", "/api/ch/session")
        self.assertEqual(data["state"], "ready")
        self.assertNotIn(TOKEN, json.dumps(data))

    def test_companion_sign_out_discards_the_ch_session(self):
        c = types.SimpleNamespace(libmgr=None, cloud=mock.Mock(), chlink=mock.Mock(), cloudsync=mock.Mock(), friends=mock.Mock())
        c.cloudsync.status.return_value = {"signedIn": False}
        self.handle(c, "POST", "/api/cloud/logout")
        c.cloud.logout.assert_called_once()
        c.chlink.discard_session.assert_called_once()


if __name__ == "__main__":
    unittest.main()
