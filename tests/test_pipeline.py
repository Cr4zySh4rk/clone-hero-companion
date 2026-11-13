"""2.11.0: VP8 conversions run on a bounded conversion stage while the job workers go on with the next songs'
search and download. Fake slow conversions and fast downloads (sleeps); no ffmpeg or network."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import cancel, ingame
from chcompanion.i18n import _
from chcompanion.jobs import Deferred, Job, JobManager

from test_video_convert import make_folder_song
from test_youtube_limits import Store as _Store


class Store(_Store):
    def get(self, key, default=None):
        return default


def wait_for(pred, secs=4.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def final(job):
    d = job.public()
    return {k: d[k] for k in ("kind", "title", "status", "step", "result", "error", "videoAdded")}


class Rig:
    """A runner shaped like _run_job: gated lookup, download, then the conversion either inline (holding the
    encoder slot, as before) or as a Deferred for the stage."""
    LOOKUP, DOWNLOAD, CONVERT = 0.06, 0.16, 0.12

    def __init__(self, staged: bool, convert=None):
        self.staged = staged
        self.gate = threading.Lock()                 # the RateGate: one lookup at a time
        self.slot = threading.Semaphore(1)           # the inline path's encoder slot
        self.lookup_start, self.conv_start, self.conv_end = {}, {}, {}
        self.convert = convert or self.default_convert

    def default_convert(self, job, progress):
        self.conv_start[job.title] = time.time()
        progress(_("ingame.encoding_pct", pct=50))
        cancel.sleep(self.CONVERT)
        self.conv_end[job.title] = time.time()
        job.video_added = True
        return "video saved"

    def runner(self, job, progress):
        self.lookup_start[job.title] = time.time()
        with self.gate:
            progress(_("yt.searching"))
            cancel.sleep(self.LOOKUP)
        progress(_("ingame.downloading_video", pct=100))
        cancel.sleep(self.DOWNLOAD)
        if self.staged:
            return Deferred(lambda progress: self.convert(job, progress))
        with self.slot:
            return self.convert(job, progress)


class PipelineTests(unittest.TestCase):
    def manager(self, rig, workers=2, slots=1):
        m = JobManager(rig.runner, workers=workers, conversion_slots=slots)
        m.start()
        self.addCleanup(m.shutdown)
        return m

    def run_batch(self, rig, n=6, **kw):
        m = self.manager(rig, **kw)
        jobs = [m.submit(Job("media", f"song{i}", song_id=f"s{i}")) for i in range(1, n + 1)]
        t0 = time.time()
        self.assertTrue(wait_for(lambda: all(j.status == "done" for j in jobs), 8))
        return time.time() - t0, jobs, m

    def test_pipelined_batch_is_clearly_faster_and_overlaps(self):
        seq = Rig(staged=False)
        t_seq, seq_jobs, _m = self.run_batch(seq)
        pipe = Rig(staged=True)
        t_pipe, pipe_jobs, m = self.run_batch(pipe)
        print(f"\nsequential {t_seq:.2f}s, pipelined {t_pipe:.2f}s ({t_seq / t_pipe:.2f}x)")
        self.assertLess(t_pipe, t_seq * 0.88)
        # job 3's lookup starts while job 1 is still converting
        self.assertLess(pipe.lookup_start["song3"], pipe.conv_end["song1"])
        self.assertEqual(m.active, 0)
        # one conversion at a time with one slot
        spans = sorted((pipe.conv_start[k], pipe.conv_end[k]) for k in pipe.conv_start)
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a[1], b[0] + 0.01)
        # final job fields identical to the sequential path
        self.assertEqual([final(j) for j in pipe_jobs], [final(j) for j in seq_jobs])
        self.assertTrue(all(j.finished >= j.started > 0 for j in pipe_jobs))

    def test_job_in_the_stage_is_running_with_its_progress(self):
        started, release = threading.Event(), threading.Event()

        def convert(job, progress):
            progress(_("ingame.encoding_pct", pct=42))
            started.set()
            release.wait(3)
            return "ok"
        rig = Rig(True, convert)
        m = self.manager(rig, workers=1)
        j = m.submit(Job("media", "a"))
        self.assertTrue(started.wait(3))
        self.assertTrue(wait_for(lambda: m.active == 1 and m.runner is not None))
        snap = m.snapshot()
        self.assertEqual(snap["counts"].get("running"), 1)
        self.assertEqual(str(j.step), str(_("ingame.encoding_pct", pct=42)))
        # the single worker is free again: a second job is looked up meanwhile
        m.submit(Job("media", "b"))
        self.assertTrue(wait_for(lambda: "b" in rig.lookup_start))
        release.set()
        self.assertTrue(wait_for(lambda: j.status == "done"))

    def test_cancel_during_conversion_stops_it_and_cleans_up(self):
        started, cleaned = threading.Event(), []

        def convert(job, progress):
            try:
                started.set()
                cancel.sleep(30)           # stands for ffmpeg, killed on cancel
            finally:
                cleaned.append(job.title)
        rig = Rig(True, convert)
        m = self.manager(rig, workers=2)
        j1 = m.submit(Job("media", "a"))
        j2 = m.submit(Job("media", "b"))
        self.assertTrue(started.wait(3))
        self.assertTrue(wait_for(lambda: j2.status == "running" and "b" in rig.lookup_start))
        time.sleep(0.2)                                  # j2 is queued behind j1 in the stage
        t0 = time.time()
        m.cancel(j1.id)
        self.assertTrue(wait_for(lambda: "a" in cleaned, 2))
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(j1.status, "cancelled")
        m.cancel(j2.id)
        self.assertTrue(wait_for(lambda: m.active == 0, 2))
        self.assertEqual((j2.status, j2.error), ("cancelled", ""))
        self.assertEqual(j1.error, "")

    def test_conversion_failure_fails_the_job_like_before(self):
        def convert(job, progress):
            progress(_("ingame.encoding_pct", pct=10))
            raise RuntimeError("ffmpeg exploded")
        seq_t, seq_jobs, _m = self.run_failure(Rig(False, convert))
        pipe_t, pipe_jobs, m = self.run_failure(Rig(True, convert))
        self.assertEqual(pipe_jobs[0].status, "failed")
        self.assertEqual(final(pipe_jobs[0]), final(seq_jobs[0]))
        self.assertIn("ffmpeg exploded", pipe_jobs[0].error)
        self.assertEqual(m.active, 0)

    def run_failure(self, rig):
        m = self.manager(rig, workers=1)
        j = m.submit(Job("media", "x"))
        self.assertTrue(wait_for(lambda: j.status == "failed"))
        return 0, [j], m

    def test_queued_conversions_are_bounded(self):
        release = threading.Event()
        peak = [0]
        rig = None

        def convert(job, progress):
            peak[0] = max(peak[0], m.stage().waiting())
            release.wait(5)
            return "ok"
        rig = Rig(True, convert)
        rig.DOWNLOAD = rig.LOOKUP = 0.01
        m = self.manager(rig, workers=4, slots=1)
        jobs = [m.submit(Job("media", f"s{i}")) for i in range(8)]
        # 1 converting + 2 waiting (slots * 2); every worker then holds one downloaded file at the handoff
        # and waits; nothing else starts: 1 + 2 + 4 jobs are in flight, the 8th stays queued
        self.assertTrue(wait_for(lambda: m.stage().waiting() == 2 and len(rig.lookup_start) == 7))
        time.sleep(0.15)
        self.assertEqual(m.stage().waiting(), 2)
        self.assertEqual(len(rig.lookup_start), 7)
        self.assertEqual(sum(j.status == "queued" for j in jobs), 1)
        release.set()
        self.assertTrue(wait_for(lambda: all(j.status == "done" for j in jobs), 6))
        self.assertLessEqual(peak[0], 2)

    def test_quit_aborts_waiting_conversions(self):
        release, aborted = threading.Event(), []

        def runner(job, progress):
            return Deferred(lambda progress: release.wait(3) and "ok", lambda: aborted.append(job.title))
        m = JobManager(runner, workers=3, conversion_slots=1)
        m.start()
        jobs = [m.submit(Job("media", f"s{i}")) for i in range(3)]
        self.assertTrue(wait_for(lambda: m.stage().waiting() == 2))
        m.shutdown()
        release.set()
        self.assertEqual(len(aborted), 2)
        self.assertTrue(wait_for(lambda: m.active == 0))
        self.assertEqual(sum(j.status == "cancelled" for j in jobs), 2)


class StagedPrepareTests(unittest.TestCase):
    """ingame.prepare(stage=True): the same files as the inline path, the song locked until the pack is done."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cache = Path(self.tmp.name) / "cache"
        cache.mkdir()
        self.cache = cache

        def fake_download(song, yt, store, progress=None, force=False, defer=False, convert=True, later=None):
            d = Path(tempfile.mkdtemp(prefix="chc-", dir=self.cache))
            raw = d / "video.mp4"
            raw.write_bytes(b"RAW")

            def conv(progress=None):
                raw.unlink()
                (d / "video.webm").write_bytes(b"WEBM")
                return d / "video.webm"
            if later is not None:
                later.append(conv)
                return raw
            return conv(progress)
        p = mock.patch.object(ingame, "download_video", fake_download)
        p.start()
        self.addCleanup(p.stop)

    def song(self, sub):
        root = Path(self.tmp.name) / sub
        root.mkdir()
        return make_folder_song(root)

    def test_same_result_as_inline_and_song_locked_until_done(self):
        a, b = self.song("a"), self.song("b")
        inline = ingame.prepare(a, None, Store(), lyrics=False, force_video=True)
        r = ingame.prepare(b, None, Store(), lyrics=False, force_video=True, stage=True)
        cont = r["continuation"]
        self.assertTrue(ingame._lock_for(b.key).locked())
        self.assertFalse((b.path / "video.webm").exists())
        out = cont(None)
        self.assertFalse(ingame._lock_for(b.key).locked())
        self.assertNotIn("continuation", out)
        self.assertEqual(out, inline)
        self.assertEqual(sorted(p.name for p in a.path.iterdir()), sorted(p.name for p in b.path.iterdir()))
        self.assertEqual(list(self.cache.iterdir()), [])          # work folders removed

    def test_abort_and_cancel_clean_up_and_unlock(self):
        s = self.song("c")
        cont = ingame.prepare(s, None, Store(), lyrics=False, force_video=True, stage=True)["continuation"]
        cont.abort()
        self.assertFalse(ingame._lock_for(s.key).locked())
        self.assertEqual(list(self.cache.iterdir()), [])
        s2 = self.song("d")
        cont = ingame.prepare(s2, None, Store(), lyrics=False, force_video=True, stage=True)["continuation"]
        ev = threading.Event()
        ev.set()
        with cancel.bind(ev):
            with self.assertRaises(cancel.Cancelled):
                cont(None)
        self.assertFalse(ingame._lock_for(s2.key).locked())
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertTrue((s2.path / "video.mp4").exists())         # untouched


if __name__ == "__main__":
    unittest.main()
