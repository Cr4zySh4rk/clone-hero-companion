"""The bgutil PO token server: start/stop, port, health, restart, yt-dlp options. No test starts a real Deno server
unless CHC_TEST_POT_SERVER=1 (then test_real_server runs the bundled file with the Deno on PATH)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("CHC_TEST_POT_SERVER"):
    os.environ["CHC_NO_POT"] = "1"        # also for unittest discover, which does not read conftest.py

from chcompanion import potserver, process, youtube

FAKE_SERVER = textwrap.dedent('''
    import http.server, json, sys
    port = int(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
    if mode == "exit":
        sys.exit(3)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"server_uptime": 1, "version": "2.0.2"} if mode == "ok" else {"nope": 1}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


class PotServerTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(potserver.OPT_OUT_ENV, None)
        self.addCleanup(self.env.stop)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.script = tmp / "fake_server.py"
        self.script.write_text(FAKE_SERVER)
        self.mode = "ok"
        self.bundle = tmp / "bgutil-server.js"
        self.bundle.write_text("//")
        self.server = potserver.PotServer()
        self.addCleanup(self.server.stop)
        self.cmds = []
        self.real_command = potserver.command

        def fake_command(deno, port):
            self.cmds.append((deno, port))
            return [sys.executable, str(self.script), str(port), self.mode]
        for p in (mock.patch.object(potserver, "command", fake_command),
                  mock.patch.object(potserver, "bundle_file", lambda: self.bundle),
                  mock.patch.object(youtube, "_find_tool", lambda name: "deno" if name == "deno" else None),
                  mock.patch.object(potserver, "START_TIMEOUT_S", 6.0)):
            p.start()
            self.addCleanup(p.stop)

    # ---- the real command line ----
    def test_command_uses_minimal_permissions_and_loopback(self):
        cmd = self.real_command("/x/deno", 4242)
        self.assertEqual(cmd[:2], ["/x/deno", "run"])
        self.assertIn("--allow-net", cmd)
        self.assertIn("--allow-env", cmd)
        self.assertIn("--no-prompt", cmd)
        self.assertFalse([a for a in cmd if a.startswith(("--allow-read", "--allow-write", "--allow-run", "--allow-ffi",
                                                           "--allow-sys", "--allow-all", "-A"))])
        self.assertEqual(cmd[cmd.index("-H") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("-p") + 1], "4242")
        self.assertTrue(cmd[cmd.index("-H") - 1].endswith("bgutil-server.js"))

    def test_vendored_bundle_files_exist(self):
        d = Path(potserver.__file__).parent / "vendor" / "bgutil"
        for name in ("bgutil-server.js", "LICENSE", "VERSION", "NOTICE"):
            self.assertTrue((d / name).is_file(), name)
        self.assertEqual((d / "VERSION").read_text().strip(), "2.0.2")

    def test_free_port_is_bindable_loopback(self):
        port = potserver.free_port()
        self.assertTrue(1024 <= port <= 65535)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))

    # ---- lifecycle ----
    def test_start_serves_ping_and_stop_kills_process(self):
        port = self.server.ensure()
        self.assertIsNotNone(port)
        self.assertEqual(self.cmds, [("deno", port)])
        self.assertEqual(potserver.ping(port)["version"], "2.0.2")
        self.assertEqual(self.server.ensure(), port)               # running: reused, not restarted
        self.assertEqual(len(self.cmds), 1)
        proc = self.server._proc
        self.server.stop()
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.server.port)
        self.assertIsNone(potserver.ping(port, timeout=0.5))

    def test_health_failure_gives_none_and_is_not_retried_at_once(self):
        self.mode = "bad"                                          # answers /ping without a version
        with mock.patch.object(potserver, "START_TIMEOUT_S", 1.0):
            self.assertIsNone(self.server.ensure())
        self.assertIn("did not start", self.server.reason)
        n = len(self.cmds)
        self.assertIsNone(self.server.ensure())                    # inside the retry window: no new process
        self.assertEqual(len(self.cmds), n)

    def test_process_that_exits_gives_none(self):
        self.mode = "exit"
        self.assertIsNone(self.server.ensure())
        self.assertIn("exited", self.server.reason)

    def test_retry_after_window(self):
        self.mode = "exit"
        self.assertIsNone(self.server.ensure())
        self.mode = "ok"
        self.server._failed_at -= potserver.RETRY_AFTER_S + 1
        self.assertIsNotNone(self.server.ensure())

    def test_restarts_once_when_it_dies_then_gives_up(self):
        first = self.server.ensure()
        self.assertIsNotNone(first)
        self.server._proc.kill(); self.server._proc.wait()
        second = self.server.ensure()                              # one restart
        self.assertIsNotNone(second)
        self.assertEqual(len(self.cmds), 2)
        self.server._proc.kill(); self.server._proc.wait()
        self.assertIsNone(self.server.ensure())                    # not a third time
        self.assertEqual(len(self.cmds), 2)

    def test_no_deno_or_no_bundle_gives_none(self):
        with mock.patch.object(youtube, "_find_tool", lambda name: None):
            self.assertIsNone(self.server.ensure())
            self.assertEqual(self.server.reason, "no Deno")
        s2 = potserver.PotServer()
        with mock.patch.object(potserver, "bundle_file", lambda: Path("/nonexistent/x.js")):
            self.assertIsNone(s2.ensure())
            self.assertIn("bundle missing", s2.reason)
        self.assertEqual(self.cmds, [])

    def test_opt_out_env(self):
        os.environ[potserver.OPT_OUT_ENV] = "1"
        self.assertIsNone(self.server.ensure())
        self.assertEqual(self.cmds, [])

    def test_loopback_bypasses_proxies(self):
        with mock.patch.dict(os.environ, {"NO_PROXY": "example.com", "no_proxy": ""}):
            self.server.ensure()
            self.assertIn("127.0.0.1", os.environ["NO_PROXY"].split(","))
            self.assertIn("example.com", os.environ["NO_PROXY"].split(","))
            self.assertIn("127.0.0.1", os.environ["no_proxy"].split(","))

    def test_windows_start_hides_console(self):
        captured = {}

        class FakeProc:
            returncode = None
            def poll(self): return None
            def terminate(self): pass
            def wait(self, *a): return 0
        def fake_popen(args, **kw):
            captured.update(kw)
            return FakeProc()
        with mock.patch.object(process.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(subprocess, "Popen", fake_popen), \
                mock.patch.object(potserver, "ping", lambda port, timeout=2.0: {"version": "2.0.2"}):
            self.assertIsNotNone(self.server.ensure())
        self.assertTrue(captured["creationflags"] & 0x08000000)
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["stdout"], subprocess.DEVNULL)

    # ---- yt-dlp options ----
    def test_extractor_args_when_server_up(self):
        with mock.patch.object(potserver, "ensure", return_value=5555):
            self.assertEqual(potserver.extractor_args(),
                             {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:5555"]}})
            opts = youtube._ydl_opts()
            self.assertEqual(opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"], ["http://127.0.0.1:5555"])
            opts = youtube._ydl_opts(["web", "mweb"])              # merged with the player-client choice
            self.assertEqual(opts["extractor_args"]["youtube"], {"player_client": ["web", "mweb"]})
            self.assertIn("youtubepot-bgutilhttp", opts["extractor_args"])

    def test_no_extractor_args_when_server_unavailable(self):
        with mock.patch.object(potserver, "ensure", return_value=None):
            self.assertIsNone(potserver.extractor_args())
            self.assertNotIn("extractor_args", youtube._ydl_opts())
            self.assertEqual(youtube._ydl_opts(["web"])["extractor_args"], {"youtube": {"player_client": ["web"]}})

    def test_ydl_opts_survive_a_crashing_server_module(self):
        with mock.patch.object(potserver, "ensure", side_effect=RuntimeError("boom")):
            self.assertNotIn("extractor_args", youtube._ydl_opts())

    def test_status_line(self):
        with mock.patch.object(potserver, "plugin_version", return_value="2.0.2"), \
                mock.patch.object(potserver, "ensure", return_value=4321):
            self.assertEqual(potserver.status_line(), "bgutil 2.0.2 plugin loaded; server ok (port 4321)")
        with mock.patch.object(potserver, "plugin_version", return_value="2.0.2"), \
                mock.patch.object(potserver, "ensure", return_value=None):
            line = potserver.status_line()
            self.assertIn("plugin loaded", line)
            self.assertNotIn("server ok", line)
        with mock.patch.object(potserver, "plugin_version", return_value=None):
            self.assertNotIn("plugin loaded", potserver.status_line())


@unittest.skipUnless(os.environ.get("CHC_TEST_POT_SERVER") and shutil.which("deno"),
                     "set CHC_TEST_POT_SERVER=1 with deno on PATH to start the real server")
class RealServerTests(unittest.TestCase):
    def test_real_server(self):
        os.environ.pop(potserver.OPT_OUT_ENV, None)
        server = potserver.PotServer()
        self.addCleanup(server.stop)
        port = server.ensure()
        self.assertIsNotNone(port, server.reason)
        self.assertEqual(potserver.ping(port)["version"], potserver.bundle_version())


if __name__ == "__main__":
    unittest.main()
