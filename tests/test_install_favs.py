"""Installing missing favourites: Encore then RhythmVerse (recorded source first, charter must match),
Run in Favourites, and favourites surviving trash/delete/rescan."""
import shutil
import types
from pathlib import Path
import unittest
from unittest import mock

from chcompanion import api, cloud
from chcompanion.jobs import Job
from test_favourites import Base, add_chart

ROW = {"song_key": "k1", "artist": "Nickelback", "name": "Rockstar", "charter": "Harmonix", "checksum": "", "source": {}}


def enc(charter="Harmonix", md5="e1"):
    return {"source": "encore", "name": "Rockstar", "artist": "Nickelback", "charter": charter,
            "downloadUrl": "enc-url", "md5": md5}


def rv(charter="Harmonix", rid="rv:1", hashes=()):
    return {"source": "rhythmverse", "id": rid, "name": "Rockstar", "artist": "Nickelback", "charter": charter,
            "downloadUrl": "rv-url", "hashes": list(hashes)}


def S(*items):
    return lambda q, per_page=50: {"songs": list(items)}


class ResolveTests(unittest.TestCase):
    def test_encore_hit(self):
        got = cloud.resolve_favourite(ROW, S(enc()), mock.Mock(side_effect=AssertionError("RV must not be asked")))
        self.assertEqual(got["source"], "encore")

    def test_encore_miss_then_rhythmverse_hit(self):
        got = cloud.resolve_favourite(ROW, S(enc("Someone Else")), S(rv("<color=#f00>Harmonix</color>")))
        self.assertEqual(got["id"], "rv:1")

    def test_rhythmverse_wrong_charter_rejected(self):
        self.assertIsNone(cloud.resolve_favourite(ROW, S(), S(rv("Other Charter"))))
        self.assertTrue(cloud.resolve_favourite({**ROW, "charter": ""}, S(), S(rv()))["guess"])      # empty charter: any, marked as a guess

    def test_checksum_confirms_among_same_charter(self):
        row = {**ROW, "checksum": "ABCD"}
        got = cloud.resolve_on_rhythmverse(row, S(rv(rid="rv:1"), rv(rid="rv:2", hashes=["abcd"])))
        self.assertEqual(got["id"], "rv:2")

    def test_recorded_source_order(self):
        calls = []
        e = lambda q, per_page=50: calls.append("encore") or {"songs": [enc()]}
        r = lambda q, per_page=100: calls.append("rv") or {"songs": [rv()]}
        row = {**ROW, "source": {"rv_id": "rv:9", "rv_url": "https://rhythmverse.co/x"}}
        self.assertEqual(cloud.resolve_favourite(row, e, r)["source"], "rhythmverse")
        self.assertEqual(calls, ["rv"])
        calls.clear()
        self.assertEqual(cloud.resolve_favourite({**ROW, "source": {"encore_md5": "e1"}}, e, r)["source"], "encore")
        self.assertEqual(calls, ["encore"])

    def test_outage_is_not_reported_as_not_found(self):
        boom = mock.Mock(side_effect=OSError("offline"))
        with self.assertRaises(OSError):
            cloud.resolve_favourite(ROW, S(), boom)
        self.assertEqual(cloud.resolve_favourite(ROW, boom, S(rv()))["id"], "rv:1")


class FakeJobs:
    def __init__(self):
        self.submitted = []

    def submit(self, job):
        self.submitted.append(job)


class Harness(Base):
    def companion(self, signed_in=True, cloud_only=()):
        c = types.SimpleNamespace(
            libmgr=self.lm, jobs=FakeJobs(), queued=[],
            cloud=types.SimpleNamespace(signed_in=signed_in),
            cloudsync=types.SimpleNamespace(cloud_only=list(cloud_only), not_found=set()),
            settings=lambda: {"auto_process_downloads": False})
        c.queue_song = lambda s, kind, prio, **kw: c.queued.append(s)
        return c

    def call_jobs(self, c, ids, install_missing):
        out = {}
        h = types.SimpleNamespace(companion=c, _json=lambda o, code=200: out.update(o))
        api.handle(h, "POST", "/api/jobs", {}, {"ids": ids, "kind": "full", "options": {}, "installMissing": install_missing})
        return out


class RunTests(Harness):
    def test_run_in_favourites_installs_missing_then_processes_once(self):
        c = self.companion(cloud_only=[ROW])
        ids = [self.lm.entries()[0]["id"]]
        with mock.patch("chcompanion.cloud.resolve_favourite", return_value=enc()):
            out = self.call_jobs(c, ids, True)
        self.assertEqual(out["queued"], 1)                                   # the existing song only
        self.assertEqual(out["installing"]["queued"], 1)
        job = c.jobs.submitted[0]
        self.assertTrue(job.options["process"])                              # the install job processes the new chart...
        self.assertEqual(len(c.queued), 1)                                   # ...so Run queues only the old ones (no repeats)

    def test_run_unchanged_when_nothing_missing_or_not_asked(self):
        c = self.companion(cloud_only=[])
        out = self.call_jobs(c, [self.lm.entries()[0]["id"]], True)
        self.assertNotIn("installing", out)
        c2 = self.companion(cloud_only=[ROW])
        self.assertNotIn("installing", self.call_jobs(c2, [], False))
        self.assertEqual(c2.jobs.submitted, [])

    def test_install_all_reports_not_found(self):
        c = self.companion(cloud_only=[ROW])
        with mock.patch("chcompanion.cloud.resolve_favourite", return_value=None):
            r = api._install_cloud_favs(c, {})
        self.assertEqual((r["queued"], r["notFound"]), (0, ["k1"]))


class TrashTests(Harness):
    def test_trash_keeps_favourite_and_lists_it_as_missing_signed_out(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        self.assertEqual(self.store.get("favs")[self.lm.chart_key(h)]["charter"], "Harmonix")
        shutil.rmtree(h.path)
        self.lm.rescan()                                                     # folder gone
        self.assertEqual(self.favs(), [])                                    # nothing on disk to star...
        self.assertEqual(len(self.store.get("favs")), 1)                     # ...but the favourite is kept
        c = self.companion(signed_in=False)
        miss = api._missing_favs(c)
        self.assertEqual([(r["artist"], r["name"], r["charter"]) for r in miss], [("Queen", "Bohemian", "Harmonix")])

    def test_reappearing_chart_is_starred_again(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        back = h.path
        saved = Path(self.tmp.name) / "x"
        shutil.move(str(back), str(saved))
        self.lm.rescan()
        self.assertEqual(self.favs(), [])
        shutil.move(str(saved), str(back))
        self.lm.rescan()
        self.assertEqual(self.favs(), ["Harmonix"])
        self.assertEqual(self.lm.missing_favs(), [])

    def test_reinstall_with_new_checksum_replaces_missing_record(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        shutil.rmtree(h.path)
        self.lm.rescan()
        add_chart(self.data / "Songs", "New/Queen - Bohemian", "Queen", "Bohemian", "Harmonix", "different body")
        self.lm.rescan()
        self.lm.set_fav([self.chart("Harmonix")], True)                      # what the install job does
        self.assertEqual(len(self.store.get("favs")), 1)
        self.assertEqual(self.lm.missing_favs(), [])
        self.assertEqual(self.favs(), ["Harmonix"])

    def test_delete_does_not_unfavourite_or_tombstone_or_drop_cloud_row(self):
        from test_favourites import CloudTests
        t = CloudTests("test_unstar_deletes_cloud_row_and_does_not_come_back")
        t.tmp, t.store, t.lm, t.data = self.tmp, self.store, self.lm, self.data
        remote = []
        cs, sent = t.make(remote)
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        cs.sync_favorites()
        self.assertEqual(len(remote), 1)
        with mock.patch.dict("sys.modules", {"send2trash": None}):          # force the "Deleted Songs" fallback
            self.assertEqual(self.lm.delete([api.song_id(h)])["deleted"], 1)
        self.assertEqual(cs.state["tombstones"], {})
        cs.sync_favorites()
        cs.sync_favorites()
        self.assertEqual(len(remote), 1)                                     # cloud row kept
        self.assertEqual(cs.state["tombstones"], {})
        self.assertEqual(len(self.store.get("favs")), 1)


if __name__ == "__main__":
    unittest.main()
