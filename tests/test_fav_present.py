"""A favourite whose chart is already in the library under another md5/path: skipped installs adopt it,
rescans merge the stale record, Install all never reinstalls it, and the cloud row is re-keyed (no tombstone)."""
import types
import unittest
from unittest import mock

from chcompanion import api
from chcompanion.app import Companion
from chcompanion.jobs import Job
from test_favourites import Base, add_chart
from test_install_favs import FakeJobs


def stale_record(lm, key="oldmd5", **kw):
    rec = {"md5": "0" * 32, "pid": "old|path", "t": 1, "artist": "Queen", "name": "Bohemian", "charter": "Harmonix", "source": {}}
    lm.store.set("favs", {key: {**rec, **kw}})
    return key


class Present(Base):
    def setUp(self):
        super().setUp()
        self.rekeys = []
        self.lm.on_rekey = lambda o, n: self.rekeys.append((o, n))

    def test_rescan_merges_fuzzy_present_record(self):
        key = stale_record(self.lm, artist="Queen (WaveGroup)", name="Bohemian (2011 Mix)")
        self.assertEqual(self.lm.missing_favs(), [])                         # merged, not listed
        self.lm.rescan()
        favs = self.store.get("favs")
        self.assertNotIn(key, favs)
        self.assertEqual(len(favs), 1)
        self.assertEqual(self.favs(), ["Harmonix"])                          # only that chart, not the Neversoft copy
        self.assertEqual(self.rekeys[0][0], key)
        self.lm.rescan()
        self.assertEqual(len(self.store.get("favs")), 1)                     # idempotent

    def test_other_charter_is_not_present(self):
        stale_record(self.lm, charter="Nobody")
        self.assertEqual(len(self.lm.missing_favs()), 1)
        self.assertEqual(self.favs(), [])

    def test_empty_charter_unique_match_merges_ambiguous_does_not(self):
        add_chart(self.data / "Songs", "Guns/GNR - Jungle", "Guns N' Roses", "Welcome to the Jungle", "Unknown Charter", "g")
        self.lm.rescan()
        key = stale_record(self.lm, artist="Guns N' Roses", name="Welcome to the Jungle", charter="")
        self.assertEqual(self.lm.missing_favs(), [])
        self.assertNotIn(key, self.store.get("favs"))
        self.assertEqual(self.favs(), ["Unknown Charter"])
        # two charters of the same song: don't guess
        stale_record(self.lm, key="k2", charter="")
        self.assertEqual([r["song_key"] for r in self.lm.missing_favs()], ["k2"])
        self.assertEqual(self.favs(), [])

    def test_empty_charter_matches_by_source_md5(self):
        h = self.chart("Neversoft")
        md5 = self.lm.chart_key(h, compute=True)
        stale_record(self.lm, charter="", source={"encore_md5": md5.lower()})
        self.assertEqual(self.lm.missing_favs(), [])
        self.assertEqual(self.favs(), ["Neversoft"])

    def test_best_ranked_among_several(self):
        add_chart(self.data / "Songs", "Demo/Queen - Bohemian Demo", "Queen", "Bohemian (Demo)", "Harmonix", "z")
        key = stale_record(self.lm)
        self.lm.reconcile_fav_records()
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertEqual(len(self.store.get("favs")), 1)
        self.assertEqual([s.name for s in self.lm.lib.songs if self.lm.is_fav(s)], ["Bohemian"])
        self.assertNotIn(key, self.store.get("favs"))


class SkipPath(Present):
    def companion(self):
        c = Companion.__new__(Companion)
        c.libmgr = self.lm
        c.cloudsync = types.SimpleNamespace(rekey=lambda o, n: self.rekeys.append((o, n)))
        return c

    def job(self, key, **opts):
        item = {"source": "encore", "artist": "Queen", "name": "Bohemian", "charter": "Harmonix", "downloadUrl": "u", "md5": "e1"}
        return Job("download", "d", item=item, options={"mark_fav": True, "fav_key": key, "fav_charter": "Harmonix", **opts})

    def test_skip_drops_record_and_stars_existing_chart(self):
        key = stale_record(self.lm)
        c = self.companion()
        self.lm.start_fav_recovery = lambda: None
        j = self.job(key)
        with mock.patch.object(type(self.lm), "reconcile_fav_records", lambda self_: 0):
            got = c.adopt_existing(j, j.item)
        self.assertEqual(got.charter if hasattr(got, "charter") else self.lm.charter_of(got), "Harmonix")
        self.assertNotIn(key, self.store.get("favs"))
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertEqual(self.rekeys[0][0], key)

    def test_already_installed_exception_adopts(self):
        from chcompanion.sources import install as inst
        key = stale_record(self.lm)
        c = self.companion()
        c.settings = lambda: {"download_subdir": "", "auto_process_downloads": False}
        c.library = self.lm.lib
        c.data_dir = self.data
        j = self.job(key, force=False)
        j.item["charter"] = "Other"                                           # so the pre-check cannot hit; the favourite's own record does
        with mock.patch.object(inst, "install", side_effect=inst.AlreadyInstalled("x.sng")), \
                mock.patch.object(Companion, "adopt_existing", wraps=c.adopt_existing) as spy:
            out = Companion._run_download(c, j, lambda *_: None)
        self.assertIn("already in your library", out)
        self.assertNotIn(key, self.store.get("favs"))
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertTrue(spy.called)


class InstallAll(Present):
    def test_install_all_does_not_reinstall_fuzzy_present(self):
        key = stale_record(self.lm)
        row = {"song_key": key, "artist": "Queen", "name": "Bohemian", "charter": "Harmonix", "checksum": "", "source": {}}
        c = types.SimpleNamespace(libmgr=self.lm, jobs=FakeJobs(), cloud=types.SimpleNamespace(signed_in=True),
                                  cloudsync=types.SimpleNamespace(cloud_only=[row], not_found=set(),
                                                                  rekey=lambda o, n: self.rekeys.append((o, n))),
                                  settings=lambda: {"auto_process_downloads": False})
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=AssertionError("must not be looked up")):
            r = api._install_cloud_favs(c, {})
        self.assertEqual((r["queued"], r["alreadyHave"]), (0, 1))
        self.assertEqual(c.jobs.submitted, [])
        self.assertNotIn(key, self.store.get("favs"))
        self.assertEqual(self.favs(), ["Harmonix"])


class CloudRekey(Present):
    def test_rekey_deletes_old_row_uploads_new_without_tombstone(self):
        from test_favourites import CloudTests
        t = CloudTests("test_unstar_records_tombstone_before_sync_and_is_sent")
        t.tmp, t.store, t.lm, t.data = self.tmp, self.store, self.lm, self.data
        h = self.chart("Harmonix")
        key = stale_record(self.lm)
        remote = [{"song_key": key, "checksum": "0" * 32, "artist": "Queen", "name": "Bohemian", "charter": "Harmonix",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}}]
        cs, _ = t.make(remote)
        self.lm.on_rekey = cs.rekey
        cs.state["synced"] = [key]
        self.lm.reconcile_fav_records()
        self.assertEqual(cs.state["tombstones"], {})
        self.assertEqual(cs.state["aliases"][key], self.lm.chart_key(h))
        cs.sync_favorites()
        cs.sync_favorites()
        self.assertEqual([r["song_key"] for r in remote], [self.lm.chart_key(h)])
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertEqual(cs.state["tombstones"], {})

    def test_cloud_pull_fuzzy_matches_cloud_only_row(self):
        from test_favourites import CloudTests
        t = CloudTests("test_unstar_records_tombstone_before_sync_and_is_sent")
        t.tmp, t.store, t.lm, t.data = self.tmp, self.store, self.lm, self.data
        remote = [{"song_key": "zzz", "checksum": "F" * 32, "artist": "Queen", "name": "Bohemian", "charter": "Harmonix",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}}]
        cs, _ = t.make(remote)
        cs.sync_favorites()
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertEqual(cs.cloud_only, [])
        self.assertNotIn("zzz", [r["song_key"] for r in remote])


if __name__ == "__main__":
    unittest.main()
