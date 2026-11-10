"""Cancelling a job is immediate: running subprocesses are killed, the job ends as cancelled (never failed),
waiting jobs never start, and cancelling one job leaves the others alone."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import cancel, ingame, process
from chcompanion.i18n import _
from chcompanion.jobs import Cancelled, Job, JobManager

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def wait_for(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


class JobCancelTests(unittest.TestCase):
    def make(self, runner, workers=2, busy=None):
        m = JobManager(runner, workers=workers, busy=busy)
        m.start()
        self.addCleanup(lambda: m.set_workers(1))
        return m

    def test_cancel_kills_running_subprocess_quickly(self):
        started = threading.Event()

        def runner(job, progress):
            started.set()
            process.run(SLEEPER, capture_output=True)
            return "done"

        m = self.make(runner)
        job = m.submit(Job("media", "slow"))
        self.assertTrue(started.wait(5))
        time.sleep(0.2)
        t0 = time.time()
        m.cancel(job.id)
        self.assertTrue(wait_for(lambda: m.active == 0, 2.0))
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.error, "")

    def test_cancel_stops_streaming_process(self):
        started = threading.Event()

        def runner(job, progress):
            started.set()
            proc = process.popen(SLEEPER, stdout=-1)
            with process.kill_on_cancel(proc):
                proc.stdout.read()
                proc.wait()
            cancel.check()
            return "done"

        m = self.make(runner)
        job = m.submit(Job("convert", "encode"))
        self.assertTrue(started.wait(5))
        t0 = time.time()
        m.cancel(job.id)
        self.assertTrue(wait_for(lambda: m.active == 0, 2.0))
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(job.status, "cancelled")

    def test_runtime_error_job_cancelled_maps_to_cancelled(self):
        def runner(job, progress):
            raise RuntimeError(_("job.cancelled"))

        m = self.make(runner)
        job = m.submit(Job("download", "x"))
        self.assertTrue(wait_for(lambda: job.status not in ("queued", "running")))
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.error, "")

    def test_cancelled_is_not_swallowed_by_except_exception(self):
        def runner(job, progress):
            try:
                raise Cancelled()
            except Exception:       # a "carry on" fallback must not eat a cancel
                return "swallowed"

        m = self.make(runner)
        job = m.submit(Job("media", "x"))
        self.assertTrue(wait_for(lambda: job.status not in ("queued", "running")))
        self.assertEqual(job.status, "cancelled")

    def test_waiting_job_is_cancelled_at_once_and_never_starts(self):
        ran = []
        m = self.make(lambda job, progress: ran.append(job.id) or "ok", workers=1)
        j = Job("media", "cooling", song_id="s1")
        j.not_before = time.time() + 600
        m.submit(j)
        self.assertTrue(wait_for(lambda: j.status == "waiting"))
        m.cancel(j.id)
        self.assertEqual(j.status, "cancelled")
        busy = self.make(lambda job, progress: ran.append(job.id), workers=1, busy=lambda job: True)
        w = busy.submit(Job("media", "playing", song_id="s2"))
        self.assertTrue(wait_for(lambda: w.status == "waiting"))
        busy.cancel(w.id)
        self.assertEqual(w.status, "cancelled")
        time.sleep(2.3)
        self.assertEqual(ran, [])
        self.assertEqual((j.status, w.status), ("cancelled", "cancelled"))

    def test_cancel_by_id_leaves_others_running_and_cancel_all_stops_everything(self):
        release = threading.Event()
        running = []

        def runner(job, progress):
            running.append(job.id)
            while not release.is_set():
                progress("working")
                time.sleep(0.02)
            return "ok"

        m = self.make(runner, workers=2)
        a = m.submit(Job("media", "a", song_id="1"))
        b = m.submit(Job("media", "b", song_id="2"))
        c = m.submit(Job("media", "c", song_id="3"))
        self.addCleanup(release.set)
        self.assertTrue(wait_for(lambda: len(running) == 2))
        victim = next(j for j in (a, b) if j.status == "running")
        m.cancel(victim.id)
        self.assertEqual(victim.status, "cancelled")
        self.assertTrue(wait_for(lambda: c.status == "running" or a.status == b.status == "running"))
        others = [j for j in (a, b, c) if j is not victim]
        self.assertTrue(wait_for(lambda: all(j.status == "running" for j in others)))
        self.assertFalse(any(j.cancel.is_set() for j in others))
        m.cancel("no-such-job")
        self.assertTrue(all(j.status == "running" for j in others))
        m.cancel()
        self.assertTrue(wait_for(lambda: m.active == 0))
        self.assertEqual({j.status for j in (a, b, c)}, {"cancelled"})

    def test_pacer_sleep_wakes_on_job_cancel(self):
        from chcompanion.youtube import RateGate
        gate = RateGate(spacing=0)
        gate.paused_until = time.time() + 300

        def runner(job, progress):
            gate.wait_cooldown(progress)

        m = self.make(runner)
        job = m.submit(Job("media", "paused"))
        self.assertTrue(wait_for(lambda: job.status == "running"))
        time.sleep(0.3)
        t0 = time.time()
        m.cancel(job.id)
        self.assertTrue(wait_for(lambda: m.active == 0, 2.0))
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(job.status, "cancelled")

    def test_encode_kills_ffmpeg_and_removes_partial_output(self):
        ev = threading.Event()
        errs = []
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "out.webm"
            dst.write_bytes(b"half")
            real = process.popen

            def go():
                with cancel.bind(ev):
                    try:
                        ingame._encode("ffmpeg", Path(td) / "in.mp4", dst, [])
                    except BaseException as e:
                        errs.append(e)

            with mock.patch.object(process, "popen", lambda cmd, **kw: real(SLEEPER, **kw)):
                t = threading.Thread(target=go)
                t.start()
                time.sleep(0.5)
                t0 = time.time()
                ev.set()
                t.join(3)
            self.assertFalse(t.is_alive())
            self.assertLess(time.time() - t0, 2.0)
            self.assertIsInstance(errs[0], Cancelled)
            self.assertFalse(dst.exists())


if __name__ == "__main__":
    unittest.main()
