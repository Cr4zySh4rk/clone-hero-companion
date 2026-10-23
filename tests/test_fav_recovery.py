"""Favourites starred under v2.1.1 (id only) whose charts were trashed: metadata is recovered, never hidden."""
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import api, favrecover
from test_favourites import Base, add_chart


class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.trash = Path(self.tmp.name) / "trash"
        self.trash.mkdir()
        self.lm.trash_roots = lambda: [self.trash]
        self.real_start = self.lm.start_fav_recovery
        self.lm.start_fav_recovery = lambda: None          # tests call recovery explicitly; no racing thread

    def star_old_style(self, charter="Harmonix"):
        """A v2.1.1 favourite (md5 + pid only) whose chart is then moved to the trash."""
        h = self.chart(charter)
        self.lm.set_fav([h], True)
        k = self.lm.chart_key(h)
        favs = self.store.get("favs")
        favs[k] = {x: y for x, y in favs[k].items() if x in ("md5", "pid", "t")}
        self.store.set("favs", favs)
        return h, k

    def trash_it(self, h, dest="x/Queen - Bohemian"):
        d = self.trash / dest
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(h.path), str(d))
        self.lm.rescan()

    def test_recovery_from_trash_by_md5(self):
        h, k = self.star_old_style()
        self.trash_it(h, "weird name")                     # folder name useless: only md5 can identify it
        self.assertEqual(self.lm.recover_fav_meta(), 1)
        v = self.store.get("favs")[k]
        self.assertEqual((v["artist"], v["name"], v["charter"], v["recovered"]), ("Queen", "Bohemian", "Harmonix", "trash"))
        self.assertEqual([r["name"] for r in self.lm.missing_favs()], ["Bohemian"])

    def test_recovery_from_trash_by_pid_part_when_md5_differs(self):
        h, k = self.star_old_style()
        shutil.rmtree(h.path)
        add_chart(self.trash, "t/anything", "Queen", "Bohemian", "Harmonix", "edited chart")
        self.lm.rescan()
        self.lm.recover_fav_meta()
        self.assertEqual(self.store.get("favs")[k]["recovered"], "trash")

    def test_recovery_from_pid(self):
        h, k = self.star_old_style()
        shutil.rmtree(h.path)
        self.lm.rescan()                                   # nothing in any trash
        self.lm.recover_fav_meta()
        v = self.store.get("favs")[k]
        self.assertEqual((v["artist"], v["name"], v["charter"], v["recovered"]), ("Queen", "Bohemian", "harmonix", "pid"))
        self.assertEqual(self.lm.missing_favs()[0]["artist"], "Queen")

    def test_parse_pid_partial_and_cache_fallback(self):
        self.assertEqual(favrecover.parse_pid("a b|c|Pack/Odd Name"), {"name": "Odd Name", "charter": "c"})
        h, k = self.star_old_style()
        favs = self.store.get("favs")
        favs[k]["pid"] = "queen bohemian|harmonix|Pack/oddfolder"
        self.store.set("favs", favs)
        shutil.rmtree(h.path)
        self.lm.cache["/old/Songs/Queen - Bohemian"] = {"md5": favs[k]["md5"]}
        self.lm.rescan()
        self.lm.recover_fav_meta()
        v = self.store.get("favs")[k]
        self.assertEqual((v["artist"], v["name"]), ("Queen", "Bohemian"))

    def test_unknown_is_shown_not_hidden_and_removable(self):
        self.store.set("favs", {"ABC123DEF456": {"md5": "ABC123DEF456", "pid": "", "t": 1}})
        self.lm.recover_fav_meta()
        rows = self.lm.missing_favs()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["unknown"])
        self.assertEqual(rows[0]["name"], "Unknown chart (checksum abc123…)")
        c = mock.Mock()
        c.cloud.signed_in = False
        c.libmgr = self.lm
        self.assertEqual(len(api._missing_favs(c)), 1)
        self.assertEqual(self.lm.remove_favs(["ABC123DEF456"]), 1)
        self.assertEqual(self.lm.missing_favs(), [])

    def test_partial_metadata_is_listed(self):
        self.store.set("favs", {"k": {"md5": "ZZ", "pid": "p", "name": "Only Name", "t": 1}})
        self.assertEqual([(r["name"], r["artist"], r["unknown"]) for r in self.lm.missing_favs()], [("Only Name", "", False)])

    def test_permission_error_handled(self):
        h, k = self.star_old_style()
        shutil.rmtree(h.path)
        self.lm.rescan()
        with mock.patch.object(favrecover.os, "walk", side_effect=PermissionError("Operation not permitted")):
            self.assertEqual(list(favrecover.scan_trash([self.trash])), [])
            self.lm.recover_fav_meta()                     # falls through to the pid
        with mock.patch.object(favrecover.Path, "exists", side_effect=PermissionError("denied")):
            self.assertEqual(list(favrecover.scan_trash([self.trash])), [])
        self.assertEqual(self.store.get("favs")[k]["recovered"], "pid")

    def test_runs_once(self):
        h, k = self.star_old_style()
        shutil.rmtree(h.path)
        with mock.patch.object(favrecover, "scan_trash", return_value=iter([])) as scan:
            self.assertEqual(self.lm.recover_fav_meta(), 1)
            self.assertEqual(self.lm.recover_fav_meta(), 0)
            self.real_start()                              # nothing nameless left: no thread, no scan
            self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.lm._nameless(), [])

    def test_background_start_bumps_version(self):
        h, k = self.star_old_style()
        self.trash_it(h)
        v0 = self.lm.version
        self.real_start()
        for _ in range(100):
            if self.lm.version != v0:
                break
            import time; time.sleep(0.05)
        self.assertNotEqual(self.lm.version, v0)


if __name__ == "__main__":
    unittest.main()
