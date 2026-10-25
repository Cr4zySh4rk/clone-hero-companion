"""Two signed-in machines sharing one fake cloud show the same favourites count."""
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chcompanion import playstats
from chcompanion.api import fav_counts
from chcompanion.cloud import Cloud, CloudSync
from chcompanion.library import Library
from chcompanion.libmgr import LibraryManager
from chcompanion.store import Store

SESSION = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "expires_at": 9_999_999_999,
           "user": {"id": "u1", "email": "me@example.com", "user_metadata": {}}}


def add_chart(root: Path, folder: str, artist: str, name: str, charter: str, body: str):
    d = root / folder
    d.mkdir(parents=True)
    (d / "song.ini").write_text(f"[song]\nname = {name}\nartist = {artist}\ncharter = {charter}\n")
    (d / "notes.chart").write_text(f"[Song]\n{{\n  Name = \"{body}\"\n}}\n")


class Machine:
    def __init__(self, tmp: Path, name: str, charts, remote):
        self.data = tmp / name
        songs = self.data / "Songs"
        for folder, artist, title, charter, body in charts:
            add_chart(songs, folder, artist, title, charter, body)
        lib = Library([songs])
        lib.scan()
        self.lm = LibraryManager(self.data, lib, Store(tmp / f"{name}-store.json"))
        self.lm.fav_setlist_enabled = lambda: False

        def http(method, url, headers, body, timeout=20.0):
            if method == "GET":
                return 200, json.dumps(list(remote)).encode()
            b = json.loads(body) if body else None
            if method == "DELETE":
                k = urllib.parse.unquote_plus(url.split("song_key=eq.")[1])
                remote[:] = [r for r in remote if r["song_key"] != k]
            elif method == "POST":
                for r in b:
                    remote[:] = [x for x in remote if x["song_key"] != r["song_key"]] + [dict(r, added_at="2026-01-01T00:00:00Z")]
            return 200, b""
        cloud = Cloud(tmp / f"{name}-session.json", http=http, open_browser=lambda u: None)
        cloud._store_session(SESSION)
        self.cs = CloudSync(cloud, self.lm.store, self.lm,
                            playstats.PlayLog(tmp / f"{name}-p.jsonl", tmp / f"{name}-pc.jsonl"), tmp / f"{name}-state.json")
        self.cs.schedule = lambda *a, **k: None
        self.lm.on_fav = self.cs.fav_changed
        self.lm.on_rekey = self.cs.rekey
        self.api = SimpleNamespace(libmgr=self.lm, cloudsync=self.cs, cloud=cloud)

    def chart(self, charter, title=None):
        return [s for s in self.lm.lib.songs if self.lm.charter_of(s) == charter and (title is None or s.name == title)]

    def count(self):
        return fav_counts(self.api)


class TwoMachines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/cfg"})
        env.start()
        self.addCleanup(env.stop)
        self.remote: list = []
        t = Path(self.tmp.name)
        # A: two local copies of the Harmonix chart (different folders), Neversoft, Muse
        self.a = Machine(t, "mac", [("Pack1/Queen - Bohemian", "Queen", "Bohemian", "Harmonix", "a"),
                                    ("Dupes/Queen - Bohemian", "Queen", "Bohemian", "Harmonix", "a"),
                                    ("Pack1/Queen - Bohemian NS", "Queen", "Bohemian", "Neversoft", "b"),
                                    ("Pack1/Muse", "Muse", "Hysteria", "Someone", "c")], self.remote)
        # B: other paths, only some of A's charts, plus one A does not have
        self.b = Machine(t, "win", [("Z/Bohemian", "Queen", "Bohemian", "Harmonix", "a"),
                                    ("Z/Hysteria", "Muse", "Hysteria", "Someone", "c"),
                                    ("Z/Waterloo", "ABBA", "Waterloo", "Polydor", "d")], self.remote)

    def sync_all(self):
        for m in (self.a, self.b, self.a, self.b):
            m.cs.sync_favorites()

    def test_counts_match_and_equal_distinct_charts(self):
        a, b = self.a, self.b
        a.lm.set_fav(a.chart("Harmonix") + a.chart("Neversoft") + a.chart("Someone"), True)   # both Harmonix copies
        b.lm.set_fav(b.chart("Polydor"), True)
        # a favourite on A whose chart is gone from A (trashed), only A knows it
        a.lm.store.set("favs", {**a.lm.store.get("favs"), "DEAD": {
            "md5": "DEAD", "pid": "dancing queen|abba|x/y", "t": 1, "artist": "ABBA", "name": "Dancing Queen",
            "charter": "Polydor", "album": "", "source": {}}})
        self.assertEqual(sum(1 for e in a.lm.entries() if e["fav"]), 4)    # the naive sidebar number: two copies count twice
        self.sync_all()
        ca, cb = a.count(), b.count()
        self.assertEqual(ca, cb)
        self.assertEqual(ca["total"], 5)         # Harmonix, Neversoft, Muse, Waterloo, Dancing Queen
        self.assertEqual((ca["onDisk"], ca["notHere"]), (3, 2))
        self.assertEqual((cb["onDisk"], cb["notHere"]), (3, 2))    # Harmonix, Muse, Waterloo here
        self.assertEqual(len({r["song_key"] for r in self.remote}), len(self.remote))
        # a second round changes nothing (no ping-pong between machines)
        before = sorted(r["song_key"] for r in self.remote)
        self.sync_all()
        self.assertEqual(sorted(r["song_key"] for r in self.remote), before)
        self.assertEqual((a.count(), b.count()), (ca, cb))

    def test_missing_record_removed_elsewhere_is_dropped(self):
        a, b = self.a, self.b
        a.lm.store.set("favs", {"DEAD": {"md5": "DEAD", "pid": "p|x|y", "t": 1, "artist": "ABBA", "name": "Dancing Queen",
                                         "charter": "Polydor", "album": "", "source": {}}})
        self.sync_all()
        self.assertEqual(b.count()["total"], 1)
        b.cs.forget_keys(["DEAD"])                 # user removes it on B
        b.cs.sync_favorites()
        a.cs.sync_favorites()
        self.assertEqual(a.lm.store.get("favs"), {})
        self.assertEqual(a.count()["total"], 0)
        self.assertEqual(self.remote, [])

    def test_cloud_only_rows_survive_a_restart(self):
        a, b = self.a, self.b
        b.lm.set_fav(b.chart("Polydor"), True)
        self.sync_all()
        self.assertEqual(len(a.cs.cloud_only), 1)
        again = CloudSync(a.cs.cloud, a.lm.store, a.lm, a.cs.playlog, a.cs.state_path)
        self.assertEqual([r["charter"] for r in again.cloud_only], ["Polydor"])


if __name__ == "__main__":
    unittest.main()
