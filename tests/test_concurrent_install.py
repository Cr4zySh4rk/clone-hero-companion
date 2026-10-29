"""Two charts of one song (different charters) install and process independently; vanished files and
failed installs give clear job failures."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import ingame
from chcompanion.app import Companion
from chcompanion.jobs import Job
from chcompanion.library import Library
from chcompanion.sng import open_sng, write_sng
from chcompanion.store import Store


def item(charter, md5):
    return {"source": "encore", "artist": "Linkin Park", "name": "What I've Done", "charter": charter,
            "downloadUrl": "u-" + charter, "md5": md5}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/cfg"}),):
            p.start()
            self.addCleanup(p.stop)
        self.data = Path(self.tmp.name) / "data"
        self.songs = self.data / "Songs"
        self.songs.mkdir(parents=True)
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()
        self.c = Companion(self.data, Library([self.songs]), Store(Path(self.tmp.name) / "store.json"),
                           use_youtube=False, in_game=False, prefetch=False, game_lyrics=False)
        self.c.settings = lambda: {"download_subdir": "", "auto_process_downloads": True,
                                   "metadata_overwrite": False, "replace_low_res": False}
        self.packed = {}          # path -> video bytes packed
        self.gate = threading.Barrier(2, timeout=5)
        self.fail_download = set()
        self.processed = []

        def fake_download(url, dest, progress=None, cancel=None, depth=0):
            ch = url[2:]
            if ch in self.fail_download:
                raise RuntimeError("HTTP 500 for " + ch)
            try:
                self.gate.wait()          # make both downloads overlap
            except threading.BrokenBarrierError:
                pass
            f = Path(dest) / "chart.zip"
            write_sng(str(f), {"artist": "Linkin Park", "name": "What I've Done", "charter": ch},
                      {"notes.chart": b"[Song]\n{\n}\n" + ch.encode()})
            return [f]

        def fake_video(song, yt, store, progress=None, force=False, defer=False):
            d = Path(tempfile.mkdtemp(prefix="chc-", dir=self.tmp.name))
            v = d / "video.webm"
            v.write_bytes(b"VIDEO-" + song.path.name.encode())
            self.processed.append(song.path.name)
            return v

        for p in (mock.patch("chcompanion.sources.hosts.download", fake_download),
                  mock.patch("chcompanion.ingame.download_video", fake_video),
                  mock.patch("chcompanion.metadata.apply", lambda *a, **k: {})):
            p.start()
            self.addCleanup(p.stop)

    def run_jobs(self, *jobs):
        self.c.jobs.start()
        for j in jobs:
            self.c.jobs.submit(j)
        end = time.time() + 20
        while time.time() < end:
            snap = self.c.jobs.snapshot()["jobs"]
            if snap and all(x["status"] in ("done", "failed", "cancelled") for x in snap):
                break
            time.sleep(0.05)
        return self.c.jobs.snapshot()["jobs"]

    def dl(self, charter, md5):
        return Job("download", "Linkin Park - What I've Done", item=item(charter, md5),
                   options={"process": True, "video": True, "lyrics": False})


class ConcurrentTests(Env):
    def test_two_charters_install_and_process_independently(self):
        jobs = self.run_jobs(self.dl("Harmonix", "5c5e"), self.dl("Neversoft", "9a4b"))
        # download jobs + one full job per chart
        self.assertEqual([j["status"] for j in jobs if j["kind"] == "download"], ["done", "done"], jobs)
        full = [j for j in jobs if j["kind"] == "full"]
        self.assertEqual(sorted(j["status"] for j in full), ["done", "done"], jobs)
        names = sorted(p.name for p in self.songs.glob("*.sng"))
        self.assertEqual(names, ["Linkin Park - What I've Done (Harmonix).sng",
                                 "Linkin Park - What I've Done (Neversoft).sng"])
        self.assertEqual(sorted(self.processed), names)
        for n in names:
            vids = [e for k, e in open_sng(str(self.songs / n)).files.items() if k.startswith("video.")]
            self.assertEqual(len(vids), 1)
            self.assertEqual(open_sng(str(self.songs / n)).read("video.webm"), b"VIDEO-" + n.encode())


class RobustTests(Env):
    def test_install_failure_never_reaches_processing(self):
        self.gate = threading.Barrier(1)
        self.fail_download.add("Neversoft")
        jobs = self.run_jobs(self.dl("Neversoft", "9a4b"))
        self.assertEqual([j["status"] for j in jobs], ["failed"])
        self.assertIn("HTTP 500", jobs[0]["error"])
        self.assertEqual(self.processed, [])

    def installed(self, charter="Neversoft"):
        self.gate = threading.Barrier(1)
        self.run_jobs(self.dl(charter, "9a4b"))
        self.processed.clear()
        return self.songs / f"Linkin Park - What I've Done ({charter}).sng"

    def test_vanished_file_fails_clearly(self):
        sng = self.installed()
        sid = next(iter(self.c.libmgr.by_id))
        j = self.c.queue_song(self.c.libmgr.get(sid), "full")     # queued while the file exists...
        self.c.jobs.cancel(j.id)
        j = Job("full", "x", song_id=sid, options={})
        j.chart = {"path": str(sng), "md5": None, "charter": "Neversoft", "key": self.c.libmgr.get(sid).key}
        sng.unlink()                                                # ...then trashed before it runs
        with self.assertRaises(RuntimeError) as cm:
            self.c._run_job(j, lambda m: None)
        self.assertNotIn("Errno", str(cm.exception))
        self.assertIn("no longer there", str(cm.exception))
        self.assertIn("nothing was changed", str(cm.exception))
        self.assertEqual(self.processed, [])

    def test_file_removed_while_waiting_on_the_song_lock(self):
        sng = self.installed()
        song = self.c.libmgr.get(next(iter(self.c.libmgr.by_id)))
        sng.unlink()
        with self.assertRaises(ingame.InGameError) as cm:
            ingame.prepare(song, None, self.c.store)
        self.assertIn("no longer there", str(cm.exception))

    def test_moved_file_is_relocated_by_md5(self):
        sng = self.installed()
        song = self.c.libmgr.get(next(iter(self.c.libmgr.by_id)))
        j = self.c.queue_song(song, "full", force=True)
        self.c.jobs.cancel(j.id)
        sng.rename(self.songs / "renamed.sng")
        self.c._run_job(j, lambda m: None)
        self.assertEqual(self.processed, ["renamed.sng"])
