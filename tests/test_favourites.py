"""Favourites are per chart (not per song): unstar, independence, migration, cloud merge with tombstones."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from chcompanion.cloud import Cloud, CloudSync
from chcompanion.library import Library
from chcompanion.libmgr import LibraryManager
from chcompanion.store import Store
from chcompanion import playstats

SESSION = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "expires_at": 9_999_999_999,
           "user": {"id": "u1", "email": "me@example.com", "user_metadata": {}}}


def add_chart(root: Path, folder: str, artist: str, name: str, charter: str, body: str):
    d = root / folder
    d.mkdir(parents=True)
    (d / "song.ini").write_text(f"[song]\nname = {name}\nartist = {artist}\ncharter = {charter}\n")
    (d / "notes.chart").write_text(f"[Song]\n{{\n  Name = \"{body}\"\n}}\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/cfg"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.data = Path(self.tmp.name) / "data"
        songs = self.data / "Songs"
        add_chart(songs, "Harmonix/Queen - Bohemian", "Queen", "Bohemian", "Harmonix", "a")
        add_chart(songs, "Neversoft/Queen - Bohemian", "Queen", "Bohemian", "Neversoft", "b")
        add_chart(songs, "Other/Muse - Hysteria", "Muse", "Hysteria", "Someone", "c")
        self.lib = Library([songs])
        self.lib.scan()
        self.store = Store(Path(self.tmp.name) / "store.json")
        self.lm = LibraryManager(self.data, self.lib, self.store)
        self.lm.fav_setlist_enabled = lambda: False
        self.lm.on_fav = None

    def chart(self, charter):
        return next(s for s in self.lm.lib.songs if self.lm.charter_of(s) == charter)

    def favs(self):
        return sorted(e["charter"] for e in self.lm.entries() if e["fav"])


class LocalTests(Base):
    def test_star_and_unstar(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        self.assertEqual(self.favs(), ["Harmonix"])
        self.lm.set_fav([h], False)
        self.assertEqual(self.favs(), [])
        self.assertEqual(self.store.get("favs"), {})

    def test_charts_of_one_song_are_independent(self):
        h, n = self.chart("Harmonix"), self.chart("Neversoft")
        self.assertEqual(h.key, n.key)                          # same song-level key
        self.lm.set_fav([h], True)
        self.assertEqual(self.favs(), ["Harmonix"])             # the other copy is not starred
        self.lm.set_fav([n], True)
        self.assertEqual(self.favs(), ["Harmonix", "Neversoft"])
        self.lm.set_fav([h], False)                             # removal touches only that chart
        self.assertEqual(self.favs(), ["Neversoft"])
        self.assertEqual({e["copies"] for e in self.lm.entries() if e["name"] == "Bohemian"}, {2})

    def test_survives_move_via_checksum(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        self.assertTrue(self.lm.chart_md5(h))
        self.assertEqual(self.lm.chart_key(h), self.lm.chart_md5(h))

    def test_migration_song_level_to_charts_is_idempotent(self):
        self.store.update_song(self.chart("Harmonix").key, fav=True)     # old v2.1.0 song-level favourite
        self.store.update_song("ghost song", fav=True)                   # not in the library: kept, not lost
        self.assertEqual(self.favs(), ["Harmonix", "Neversoft"])
        self.assertNotIn("fav", self.store.song(self.chart("Harmonix").key))
        self.assertTrue(self.store.song("ghost song").get("fav"))
        before = dict(self.store.get("favs"))
        self.lm._ensure_fav_migrated()
        self.assertEqual(self.store.get("favs"), before)
        self.lm.set_fav([self.chart("Harmonix")], False)                  # and unstar sticks afterwards
        self.assertEqual(self.favs(), ["Neversoft"])


class CloudTests(Base):
    def sync(self, remote):
        sent = []

        def h(method, url, headers, body):
            if method == "GET" and "/favorites" in url:
                return 200, list(remote)
            sent.append((method, url, body))
            if method == "DELETE":
                import urllib.parse
                k = urllib.parse.unquote_plus(url.split("song_key=eq.")[1])
                remote[:] = [r for r in remote if r["song_key"] != k]
            elif method == "POST":
                for r in body:
                    remote[:] = [x for x in remote if x["song_key"] != r["song_key"]] + [dict(r, added_at="2026-01-01T00:00:00Z")]
            return 200, None
        def http(m, u, hd, b, timeout=20.0):
            import json
            s, r = h(m, u, hd, json.loads(b) if b else None)
            return s, json.dumps(r).encode() if r is not None else b""
        c = Cloud(Path(self.tmp.name) / "session.json", http=http, open_browser=lambda u: None)
        c._store_session(SESSION)
        cs = CloudSync(c, self.store, self.lm, playstats.PlayLog(Path(self.tmp.name) / "p.jsonl", Path(self.tmp.name) / "pc.jsonl"),
                       Path(self.tmp.name) / f"state{len(sent)}.json")
        return cs, sent

    def make(self, remote):
        cs, sent = self.sync(remote)
        self.lm.on_fav = cs.fav_changed
        cs.schedule = lambda *a, **k: None
        return cs, sent

    def test_unstar_deletes_cloud_row_and_does_not_come_back(self):
        remote = []
        cs, sent = self.make(remote)
        h, n = self.chart("Harmonix"), self.chart("Neversoft")
        self.lm.set_fav([h, n], True)
        cs.sync_favorites()
        self.assertEqual(len(remote), 2)
        self.lm.set_fav([h], False)
        cs.sync_favorites()
        self.assertEqual([r["charter"] for r in remote], ["Neversoft"])
        cs.sync_favorites()                                                # next pull must not re-add it
        self.assertEqual(self.favs(), ["Neversoft"])
        self.assertEqual([r["charter"] for r in remote], ["Neversoft"])
        self.assertEqual(cs.state["tombstones"], {})

    def test_unstar_records_tombstone_before_sync_and_is_sent(self):
        remote = []
        cs, sent = self.make(remote)
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        cs.sync_favorites()
        self.lm.set_fav([h], False)                                        # tombstone is recorded regardless
        self.assertTrue(cs.state["tombstones"])
        cs.sync_favorites()
        self.assertEqual(remote, [])
        self.assertEqual(self.favs(), [])

    def test_stale_pull_does_not_resurrect_a_just_unstarred_chart(self):
        h = self.chart("Harmonix")
        self.lm.set_fav([h], True)
        key = self.lm.chart_key(h)
        remote = [{"song_key": key, "checksum": key, "artist": "Queen", "name": "Bohemian", "charter": "Harmonix",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}}]
        cs, sent = self.make(remote)
        cs.state["synced"] = [key]
        self.lm.set_fav([h], False)
        cs.sync_favorites()
        self.assertEqual(self.favs(), [])
        self.assertEqual(remote, [])

    def test_removal_on_another_machine_propagates(self):
        remote = []
        cs, sent = self.make(remote)
        h, n = self.chart("Harmonix"), self.chart("Neversoft")
        self.lm.set_fav([h, n], True)
        cs.sync_favorites()
        remote[:] = [r for r in remote if r["charter"] != "Harmonix"]      # other machine unstarred Harmonix
        cs.sync_favorites()
        self.assertEqual(self.favs(), ["Neversoft"])
        self.assertEqual([r["charter"] for r in remote], ["Neversoft"])    # and we did not re-upload it

    def test_addition_on_another_machine_stars_only_that_chart(self):
        n = self.chart("Neversoft")
        key = self.lm.chart_key(n, compute=True)
        remote = [{"song_key": key, "checksum": key, "artist": "Queen", "name": "Bohemian", "charter": "Neversoft",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}}]
        cs, _ = self.make(remote)
        cs.sync_favorites()
        self.assertEqual(self.favs(), ["Neversoft"])

    def test_legacy_cloud_row_is_rekeyed_per_chart(self):
        remote = [{"song_key": "queen bohemian|harmonix", "artist": "Queen", "name": "Bohemian", "charter": "Harmonix",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}},
                  {"song_key": "abba waterloo|polydor", "artist": "ABBA", "name": "Waterloo", "charter": "Polydor",
                   "added_at": "2026-01-01T00:00:00Z", "source": {}}]
        cs, _ = self.make(remote)
        cs.sync_favorites()
        self.assertEqual(self.favs(), ["Harmonix"])
        keys = {r["song_key"] for r in remote}
        self.assertIn("abba waterloo|polydor", keys)                      # not on this machine: stays cloud-only
        self.assertNotIn("queen bohemian|harmonix", keys)
        self.assertEqual([r["song_key"] for r in cs.cloud_only], ["abba waterloo|polydor"])

    def test_resolve_on_encore_matches_charter_and_checksum(self):
        from chcompanion import cloud
        res = [{"name": "Bohemian", "artist": "Queen", "charter": "Neversoft", "downloadUrl": "u1", "md5": "aa"},
               {"name": "Bohemian", "artist": "Queen", "charter": "Harmonix", "downloadUrl": "u2", "md5": "bb"}]
        search = lambda q, per_page=50: {"songs": res}
        row = {"artist": "Queen", "name": "Bohemian", "charter": "Harmonix", "checksum": "AA", "source": {}}
        self.assertEqual(cloud.resolve_on_encore(row, search)["downloadUrl"], "u1")      # checksum wins
        self.assertEqual(cloud.resolve_on_encore({**row, "checksum": ""}, search)["downloadUrl"], "u2")
        self.assertIsNone(cloud.resolve_on_encore({**row, "checksum": "", "charter": "Nobody"}, search))


if __name__ == "__main__":
    unittest.main()
