"""Robust trash (failures reported, fallback move, empty-parent tidy, .chc-backup goes along) and the
temporary preview cache (wiped at start, never indexed)."""
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import paths
from chcompanion.library import Library, sweep_temp_files
from chcompanion.libmgr import LibraryManager, song_id
from chcompanion.store import Store


def chart(root: Path, rel: str, name: str = None):
    d = root / rel
    d.mkdir(parents=True)
    (d / "song.ini").write_text(f"[song]\nname = {name or d.name}\nartist = A\n")
    (d / "notes.chart").write_text("[Song]\n{\n}\n")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        for e in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t + "/cfg", "HOME": t + "/home"}),
                  mock.patch("tempfile.tempdir", t + "/tmp")):
            e.start()
            self.addCleanup(e.stop)
        os.makedirs(t + "/tmp")
        self.data = Path(t) / "data"
        self.songs = self.data / "Songs"
        self.trashed = []
        self.fail = lambda p: False

        def fake(path):
            if self.fail(path):
                raise OSError("simulated trash failure")
            self.trashed.append(path)
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.unlink(path)
        mod = types.ModuleType("send2trash")
        mod.send2trash = fake
        p = mock.patch.dict(sys.modules, {"send2trash": mod})
        p.start()
        self.addCleanup(p.stop)

    def mgr(self):
        lib = Library([self.songs])
        lib.scan()
        lm = LibraryManager(self.data, lib, Store(Path(self.tmp.name) / "store.json"))
        lm.rescan()
        return lm


class TrashTests(Base):
    def test_mixed_success_and_failure_with_fallback_and_reasons(self):
        for i in range(5):
            chart(self.songs, f"Pack/s{i}")
        lm = self.mgr()
        ids = [song_id(s) for s in lm.lib.songs]
        bad = {str(self.songs / "Pack/s1"), str(self.songs / "Pack/s3")}
        self.fail = lambda p: p in bad
        r = lm.delete(ids)
        self.assertEqual(r["deleted"], 5)            # failures fell back to Deleted Songs
        self.assertEqual(r["failed"], [])
        fallback = sorted(x.name for x in (self.data / "Deleted Songs").iterdir())
        self.assertEqual(fallback, ["s1", "s3"])
        self.assertFalse((self.songs / "Pack").exists())   # emptied parent removed, root kept
        self.assertTrue(self.songs.exists())

    def test_failure_list_when_fallback_also_fails(self):
        chart(self.songs, "Pack/ok")
        chart(self.songs, "Pack/bad")
        lm = self.mgr()
        ids = [song_id(s) for s in lm.lib.songs]
        self.fail = lambda p: p.endswith("bad")
        with mock.patch("shutil.move", side_effect=PermissionError("denied")):
            r = lm.delete(ids)
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(len(r["failed"]), 1)
        f = r["failed"][0]
        self.assertIn("simulated trash failure", f["reason"])
        self.assertIn("denied", f["reason"])
        self.assertTrue(f["name"] and f["id"] == song_id(next(s for s in lm.lib.songs)))
        self.assertTrue((self.songs / "Pack/bad").exists())    # nothing lost
        self.assertTrue((self.songs / "Pack").exists())        # not empty -> kept
        self.fail = lambda p: False                            # retry works
        self.assertEqual(lm.delete([f["id"]])["deleted"], 1)

    def test_large_batch_background_job(self):
        for i in range(822):
            chart(self.songs, f"Pack{i % 7}/s{i}")
        lm = self.mgr()
        self.assertEqual(len(lm.lib.songs), 822)
        ids = [song_id(s) for s in lm.lib.songs]
        self.fail = lambda p: p.endswith(("s5", "s500"))
        with mock.patch("shutil.move", side_effect=OSError("nope")):
            op = lm.start_delete(ids)
            for _ in range(600):
                st = lm.op_status(op)
                if st["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(st["status"], "done")
        self.assertEqual((st["deleted"], len(st["failed"])), (820, 2))
        self.assertEqual(len(lm.lib.songs), 2)

    def test_nested_selection_and_unknown_ids(self):
        chart(self.songs, "A/outer")
        chart(self.songs, "A/outer/inner")
        lm = self.mgr()
        r = lm.delete([song_id(s) for s in lm.lib.songs] + ["deadbeef"])
        self.assertEqual((r["deleted"], r["failed"]), (2, []))

    def test_chc_backup_of_sng_goes_to_trash_and_root_kept(self):
        from chcompanion.sng import write_sng
        self.songs.mkdir(parents=True)
        sng = self.songs / "x.sng"
        write_sng(str(sng), {"name": "x", "artist": "y"}, {"notes.chart": b"[Song]\n{\n}\n"})
        bak = self.songs / "x.sng.chc-backup"
        bak.write_bytes(sng.read_bytes())
        lm = self.mgr()
        self.assertEqual(len(lm.lib.songs), 1)
        self.assertEqual(lm.delete([song_id(lm.lib.songs[0])])["deleted"], 1)
        self.assertFalse(sng.exists() or bak.exists())
        self.assertIn(str(bak), self.trashed)
        self.assertTrue(self.songs.exists())


class CacheTests(Base):
    def test_wipe_cache_clears_everything_and_old_location(self):
        c = paths.cache_dir()
        self.assertTrue(str(c).startswith(tempfile.gettempdir()))
        (c / "video-cache").mkdir()
        (c / "video-cache" / "abc.mp4").write_bytes(b"x")
        old = paths.config_dir() / "video-cache"
        old.mkdir()
        (old / "old.mp4").write_bytes(b"x")
        leftover = paths.config_dir() / "chc-dl-123"
        leftover.mkdir()
        keep = paths.config_dir() / "settings.json"
        keep.write_text("{}")
        paths.wipe_cache()
        self.assertFalse(old.exists() or leftover.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(list(paths.cache_dir().iterdir()), [])

    def test_video_cache_lives_in_temp(self):
        from chcompanion import ingame
        self.assertEqual(ingame.video_cache_dir().parent, paths.cache_dir())

    def test_scanner_skips_artefacts_and_cache_dirs(self):
        chart(self.songs, "Real/song1")
        (self.songs / "Real/song1/x.sng.part").write_text("x")
        (self.songs / "stray.sng.chc-backup").write_text("x")
        (self.songs / "stray2.sng.chc-tmp").write_text("x")
        (self.songs / "a.sng.tmp").write_text("x")
        chart(self.songs, "chc-dl-9/inner")
        chart(self.songs, "CloneHeroCompanion-cache/video-cache/y")
        lm = self.mgr()
        self.assertEqual([s.path.name for s in lm.lib.songs], ["song1"])
        # even a songs root that contains the real cache dir
        cache_song = paths.cache_dir() / "foo"
        cache_song.mkdir()
        (cache_song / "song.ini").write_text("[song]\nname=z\n")
        lib = Library([Path(tempfile.gettempdir())])
        lib.scan()
        self.assertEqual(lib.songs, [])

    def test_startup_sweep_only_known_temp_files_never_backups(self):
        d = chart(self.songs, "Real/song1")
        (d / ".chc-video.tmp").write_text("x")
        (d / "notes.mid.chc-tmp").write_text("x")
        (d / "notes.mid.chc-backup").write_text("keep")
        (d / "mine.tmp").write_text("user file")
        (self.songs / "a.sng.chc-tmp").write_text("x")
        (self.songs / "a.sng.chc-backup").write_text("keep")
        self.assertEqual(sweep_temp_files([self.songs]), 3)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["mine.tmp", "notes.chart", "notes.mid.chc-backup", "song.ini"])
        self.assertTrue((self.songs / "a.sng.chc-backup").exists())

    def test_failed_temp_writes_are_cleaned(self):
        from chcompanion import ingame
        p = self.songs / "n.mid"
        self.songs.mkdir(parents=True)
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ingame._atomic_write(p, b"data")
        self.assertEqual(list(self.songs.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
