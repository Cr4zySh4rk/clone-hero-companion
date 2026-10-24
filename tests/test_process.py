"""Windows helpers must not allocate consoles, and startup failures stay actionable."""
import subprocess
import sys
import unittest
from unittest import mock

from chcompanion import process


class ProcessTests(unittest.TestCase):
    def setUp(self):
        process._FAILED_STARTS.clear()
        self.addCleanup(process._FAILED_STARTS.clear)

    def test_windows_run_hides_console_and_preserves_flags_and_pipes(self):
        result = subprocess.CompletedProcess([], 0, b"output", b"")
        with mock.patch.object(process.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(subprocess, "run", return_value=result) as run:
            self.assertIs(process.run(["ffmpeg.exe", "-version"], capture_output=True,
                                      creationflags=0x200), result)
        opts = run.call_args.kwargs
        self.assertEqual(opts["creationflags"], 0x08000200)
        self.assertEqual(opts["stdin"], subprocess.DEVNULL)
        self.assertTrue(opts["capture_output"])

    def test_windows_streaming_process_is_also_hidden(self):
        with mock.patch.object(process.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(subprocess, "Popen") as popen:
            process.popen(["ffmpeg.exe"], stdout=subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_input_does_not_conflict_with_default_stdin(self):
        with mock.patch.object(process.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertNotIn("stdin", process._options({"input": b"audio"}))

    def test_posix_does_not_receive_windows_flags(self):
        with mock.patch.object(process.sys, "platform", "linux"):
            self.assertEqual(process._options({"timeout": 5}), {"timeout": 5})

    def test_startup_failure_keeps_path_code_and_does_not_launch_again(self):
        for code in (-1073741502, 0xC0000142):
            process._FAILED_STARTS.clear()
            with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code)) as run:
                for _ in range(2):
                    with self.assertRaises(process.ToolStartError) as cm:
                        process.run(["/installed/bin/ffmpeg.exe"])
                    self.assertIn("0xC0000142", str(cm.exception))
                    self.assertIn("/installed/bin/ffmpeg.exe", str(cm.exception))
                run.assert_called_once()

    def test_ffmpeg_probe_exit_one_is_not_a_startup_failure(self):
        result = subprocess.CompletedProcess([], 1, b"", b"Video: h264")
        with mock.patch.object(subprocess, "run", return_value=result):
            self.assertIs(process.run(["ffmpeg", "-i", "video.mp4"]), result)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows console API")
    def test_real_child_has_no_console(self):
        result = process.run([sys.executable, "-c",
                              "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")
