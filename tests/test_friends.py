"""Friends and sharing: the request lifecycle, what the client asks the server for (RLS itself is server-side),
401/403/empty/offline handling, installed-vs-missing, Install all missing, the .setlist copy and setlist sync.
Every REST call is mocked; nothing touches the network."""
import contextlib
import json
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from chcompanion import api, setlists
from chcompanion.app import Companion
from chcompanion.cloud import Cloud, CloudError, CloudSync
from chcompanion.friends import Friends
from chcompanion.jobs import Job
from chcompanion.library import Library
from chcompanion.store import Store
from test_favourites import SESSION, Base, add_chart
from test_install_favs import FakeJobs

ME, BOB, EVE = "u1", "11111111-aaaa", "22222222-bbbb"


class FakeSupabase:
    """http(method, url, headers, body): routes keyed by (METHOD, path-after-/rest/v1/); records every call."""
    def __init__(self):
        self.routes, self.calls = {}, []

    def __call__(self, method, url, headers, body, timeout=20.0):
        u = urllib.parse.urlparse(url)
        path = u.path.split("/rest/v1/")[-1]
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.calls.append((method, path, query, json.loads(body) if body else None))
        h = self.routes.get((method, path))
        if h is None:
            return 404, b"{}"
        res = h(query, json.loads(body) if body else None)
        if isinstance(res, Exception):
            raise res
        status, data = res
        return status, json.dumps(data).encode()

    def calls_to(self, path, method=None):
        return [c for c in self.calls if c[1] == path and (method is None or c[0] == method)]


def rpc_friendships(rows):
    return lambda q, b: (200, rows)


def fr_row(oid, direction, status, username, name=None):
    return {"other_id": oid, "direction": direction, "status": status, "created_at": "2026-11-01T00:00:00Z",
            "username": username, "display_name": name or username.title(), "avatar_url": None}


def song(key, artist, name, charter, checksum=None, **kw):
    return {"song_key": key, "checksum": checksum, "artist": artist, "name": name, "charter": charter, "album": "",
            "source": kw.get("source", {}), "added_at": "2026-11-01T00:00:00Z"}


@contextlib.contextmanager
def isolated():
    """A temp data folder with every module's config_dir pointed into it."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, contextlib.ExitStack() as st:
        for m in ("paths", "libmgr", "cloud", "playstats"):
            st.enter_context(mock.patch(f"chcompanion.{m}.config_dir", return_value=Path(td)))
        yield td


class Harness(Base):
    def setUp(self):
        super().setUp()
        self.sb = FakeSupabase()
        sess = Path(self.tmp.name) / "cloud_session.json"
        sess.write_text(json.dumps(SESSION))
        self.cloud = Cloud(sess, http=self.sb)
        self.profile = {"id": ME, "username": "me", "friend_code": "ABCD2345", "display_name": "Me"}
        self.friend_rows = []
        self.sb.routes.update({
            ("GET", "profiles"): lambda q, b: (200, [self.profile]),
            ("POST", "rpc/my_friendships"): lambda q, b: (200, self.friend_rows),
            ("GET", "share_settings"): lambda q, b: (200, []),
            ("GET", "setlists"): lambda q, b: (200, []),
            ("GET", "favorites"): lambda q, b: (200, []),
        })
        self.playlog = mock.Mock()
        self.playlog.local.return_value = []
        self.playlog.cloud.return_value = []
        self.playlog.add_cloud.return_value = 0
        self.sync = CloudSync(self.cloud, self.store, self.lm, self.playlog, state_path=Path(self.tmp.name) / "state.json")
        self.fr = Friends(self.cloud, self.sync, self.lm, Path(self.tmp.name) / "friends.json")

    def befriend(self, oid=BOB, username="bob"):
        self.friend_rows[:] = [fr_row(oid, "outgoing", "accepted", username)]
        self.fr.refresh()


class LifecycleTests(Harness):
    def test_request_accept_unfriend(self):
        self.sb.routes[("POST", "rpc/request_friend")] = lambda q, b: (200, "pending")
        self.friend_rows[:] = [fr_row(BOB, "outgoing", "pending", "bob")]
        out = self.fr.request(" @Bob ")
        self.assertEqual(out["status"], "pending")
        self.assertEqual(self.sb.calls_to("rpc/request_friend")[0][3], {"q": "Bob"})
        self.assertEqual([p["username"] for p in self.fr.view()["outgoing"]], ["bob"])
        # the other side accepts; we see an accepted friend
        self.friend_rows[:] = [fr_row(BOB, "outgoing", "accepted", "bob")]
        self.assertEqual([p["id"] for p in self.fr.refresh()["friends"]], [BOB])
        self.sb.routes[("DELETE", "friendships")] = lambda q, b: (204, None)
        self.friend_rows[:] = []
        self.fr.remove(BOB)
        d = self.sb.calls_to("friendships", "DELETE")[0]
        self.assertEqual(d[2]["or"], f"(requester.eq.{BOB},addressee.eq.{BOB})")
        self.assertEqual(self.fr.view()["friends"], [])

    def test_accept_only_my_incoming_row(self):
        self.sb.routes[("PATCH", "friendships")] = lambda q, b: (204, None)
        self.friend_rows[:] = [fr_row(BOB, "incoming", "pending", "bob")]
        self.assertEqual(len(self.fr.refresh()["incoming"]), 1)
        self.fr.respond(BOB, True)
        m, _p, q, body = self.sb.calls_to("friendships", "PATCH")[0]
        self.assertEqual((q["requester"], q["addressee"], body), (f"eq.{BOB}", f"eq.{ME}", {"status": "accepted"}))

    def test_decline_deletes(self):
        self.sb.routes[("DELETE", "friendships")] = lambda q, b: (204, None)
        self.fr.respond(BOB, False)
        self.assertEqual(len(self.sb.calls_to("friendships", "DELETE")), 1)
        self.assertEqual(self.sb.calls_to("friendships", "PATCH"), [])

    def test_request_errors_are_friendly(self):
        for body, status, key in ((json.dumps({"message": "not_found"}), 404, "friends.not_found"),
                                  (json.dumps({"message": "self"}), 400, "friends.is_self"),
                                  (json.dumps({"message": "exists"}), 409, "friends.already")):
            self.sb.routes[("POST", "rpc/request_friend")] = lambda q, b, s=status, t=body: (s, json.loads(t))
            with self.assertRaises(CloudError) as cm:
                self.fr.request("x")
            self.assertEqual(cm.exception.args[0].key, key)

    def test_empty_query_makes_no_call(self):
        with self.assertRaises(CloudError):
            self.fr.request("  ")
        self.assertEqual(self.sb.calls_to("rpc/request_friend"), [])

    def test_signed_out_raises_401_without_network(self):
        self.cloud.logout()
        self.sb.calls.clear()
        for fn in (lambda: self.fr.refresh(), lambda: self.fr.request("bob"), lambda: self.fr.friend_lists(BOB)):
            with self.assertRaises(CloudError) as cm:
                fn()
            self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.sb.calls, [])

    def test_status_counts_for_the_badge(self):
        self.friend_rows[:] = [fr_row(BOB, "incoming", "pending", "bob"), fr_row(EVE, "incoming", "pending", "eve"),
                               fr_row("33", "outgoing", "pending", "zed")]
        self.fr.refresh()
        st = self.fr.status()
        self.assertEqual((st["incoming"], st["outgoing"], st["friends"]), (2, 1, 0))
        self.assertEqual(st["friendCode"], "ABCD2345")

    def test_never_asks_for_email(self):
        self.befriend()
        self.fr.friend_lists(BOB)
        everything = json.dumps(self.sb.calls)
        self.assertNotIn("email", everything.replace("me@example.com", ""))
        self.assertEqual([c for c in self.sb.calls if c[1] == "profiles" and c[2].get("id") != f"eq.{ME}"], [])

    def test_poll_never_raises(self):
        self.sb.routes[("POST", "rpc/my_friendships")] = lambda q, b: CloudError("down", offline=True)
        self.assertEqual(self.fr.poll(), 0)


class UsernameTests(Harness):
    def test_invalid_is_not_sent(self):
        for bad in ("ab", "x" * 21, "has space", "dash-ed"):
            self.assertFalse(self.fr.check_username(bad)["valid"])
            with self.assertRaises(CloudError):
                self.fr.set_username(bad)
        self.assertEqual(self.sb.calls_to("profiles", "PATCH"), [])
        self.assertEqual(self.sb.calls_to("rpc/username_available"), [])

    def test_availability_and_save(self):
        self.sb.routes[("POST", "rpc/username_available")] = lambda q, b: (200, b["name"] != "taken_one")
        self.sb.routes[("PATCH", "profiles")] = lambda q, b: (204, None)
        self.assertTrue(self.fr.check_username("  New_Name ")["available"])
        self.assertEqual(self.sb.calls_to("rpc/username_available")[0][3], {"name": "new_name"})
        self.assertFalse(self.fr.check_username("taken_one")["available"])
        self.fr.set_username("New_Name")
        self.assertEqual(self.sb.calls_to("profiles", "PATCH")[0][3], {"username": "new_name"})

    def test_taken_on_save(self):
        self.sb.routes[("PATCH", "profiles")] = lambda q, b: (409, {"code": "23505"})
        with self.assertRaises(CloudError) as cm:
            self.fr.set_username("dupe")
        self.assertEqual(cm.exception.args[0].key, "friends.username_taken")

    def test_needs_username(self):
        self.profile["username"] = None
        self.assertTrue(self.fr.refresh()["needsUsername"])
        self.assertTrue(self.fr.status()["needsUsername"])


class SharingTests(Harness):
    def test_only_friends_are_asked_for(self):
        self.befriend(BOB)
        with self.assertRaises(CloudError) as cm:
            self.fr.friend_lists(EVE)            # not a friend: nothing is requested at all
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.sb.calls_to("favorites"), [])

    def test_requests_exactly_shared_rows_of_that_friend(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("k1", "Muse", "Hysteria", "Someone")])
        self.sb.routes[("GET", "setlists")] = lambda q, b: (200, [{"id": "s1", "name": "Night", "updated_at": "x", "setlist_items": [
            {"position": 1, "song_key": "b", "artist": "Queen", "name": "Bohemian", "charter": "Harmonix"},
            {"position": 0, "song_key": "a", "artist": "Muse", "name": "Hysteria", "charter": "Someone"}]}])
        out = self.fr.friend_lists(BOB)
        fav = self.sb.calls_to("favorites")[0]
        sl = self.sb.calls_to("setlists")[-1]
        self.assertEqual(fav[2]["user_id"], f"eq.{BOB}")
        self.assertEqual((sl[2]["owner"], sl[2]["shared"]), (f"eq.{BOB}", "eq.true"))
        self.assertEqual([i["song_key"] for i in out["setlists"][0]["items"]], ["a", "b"])      # ordered by position
        self.assertEqual(out["favorites"][0]["name"], "Hysteria")

    def test_forbidden_and_empty_mean_nothing_shared(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (403, {"code": "42501"})
        self.sb.routes[("GET", "setlists")] = lambda q, b: (200, [])
        out = self.fr.friend_lists(BOB)
        self.assertEqual((out["favorites"], out["setlists"], out["offline"]), ([], [], False))
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [])
        self.assertEqual(self.fr.friend_lists(BOB)["favorites"], [])

    def test_401_signs_out_cleanly(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (401, {})
        self.sb.routes[("POST", "/auth/v1/token")] = lambda q, b: (400, {})
        with self.assertRaises(CloudError) as cm:
            self.fr.friend_lists(BOB)
        self.assertEqual(cm.exception.status, 401)

    def test_offline_serves_the_cache_read_only(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("k1", "Muse", "Hysteria", "Someone")])
        self.fr.friend_lists(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: CloudError("no network", offline=True)
        out = self.fr.friend_lists(BOB)
        self.assertTrue(out["offline"])
        self.assertEqual(out["favorites"][0]["name"], "Hysteria")
        # a new Friends() on the same cache file (app restart, still offline) shows the friend and list too
        again = Friends(self.cloud, self.sync, self.lm, self.fr.path)
        self.assertEqual([f["username"] for f in again.view()["friends"]], ["bob"])
        self.assertEqual(again._cached_list(again.friend(BOB))["favorites"][0]["name"], "Hysteria")

    def test_unfriended_cache_is_forgotten(self):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("k1", "Muse", "Hysteria", "Someone")])
        self.fr.friend_lists(BOB)
        self.friend_rows[:] = []
        self.fr.refresh()
        self.assertEqual(self.fr.cache["lists"], {})

    def test_share_toggles(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        self.sb.routes[("PATCH", "setlists")] = lambda q, b: (204, None)
        self.sb.routes[("POST", "rpc/upsert_setlist")] = lambda q, b: (200, "id")
        self.fr.set_shared("favorites", None, True)
        c = self.sb.calls_to("share_settings", "POST")[0]
        self.assertEqual((c[2], c[3]), ({"on_conflict": "user_id"}, {"user_id": ME, "favorites_shared": True}))
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.fr.set_shared("setlist", "Night", True)
        self.assertTrue(self.sb.calls_to("rpc/upsert_setlist"))            # uploaded before it is switched on
        p = self.sb.calls_to("setlists", "PATCH")[0]
        self.assertEqual((p[2], p[3]), ({"owner": f"eq.{ME}", "name": "eq.Night"}, {"shared": True}))
        self.fr.set_shared("setlist", "Night", False)
        self.assertEqual(self.sb.calls_to("setlists", "PATCH")[-1][3], {"shared": False})
        self.assertEqual(self.fr.sharing(), {"favorites": True, "setlists": {"Night": False}})
        with self.assertRaises(CloudError):
            self.fr.set_shared("bogus", None, True)


class ListTests(Harness):
    def test_installed_vs_missing(self):
        rows = [
            song("a", "Queen", "Bohemian", "Harmonix"),                         # same artist/title/charter: here
            song("b", "Queen", "Bohemian", "Someone Else"),                     # other charter: not here
            song("c", "Muse", "Hysteria", "Someone", checksum=self.lm.chart_md5(self.chart("Someone"), compute=True)),
            song("d", "Nobody", "Nothing", "X"),
        ]
        got = self.fr.annotate(rows)
        self.assertEqual([r["present"] for r in got], [True, False, True, False])
        self.assertTrue(got[0]["id"] and got[1]["id"] is None)
        # an md5 match wins even when the friend's metadata differs
        md5 = self.lm.chart_md5(self.chart("Neversoft"), compute=True)
        self.assertTrue(self.fr.annotate([song("e", "QUEEN", "x", "y", checksum=md5)])[0]["present"])


class InstallTests(Harness):
    def companion(self):
        c = types.SimpleNamespace(libmgr=self.lm, jobs=FakeJobs(), friends=self.fr, data_dir=self.data, queued=[],
                                  cloud=self.cloud)
        c.queue_friend_setlist = lambda name, rows, jobs: c.queued.append((name, rows, jobs))
        return c

    def shared(self, fav=(), lists=None):
        self.befriend(BOB)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, list(fav))
        self.sb.routes[("GET", "setlists")] = lambda q, b: (200, [
            {"id": "s", "name": n, "updated_at": "x", "setlist_items": [{**r, "position": i} for i, r in enumerate(rs)]}
            for n, rs in (lists or {}).items()])

    ROCK = song("r", "Nickelback", "Rockstar", "Harmonix")
    OTHER = song("o", "Nickelback", "Photograph", "Harmonix")
    HERE = song("h", "Muse", "Hysteria", "Someone")

    def item(self, row):
        return {"source": "encore", "artist": row["artist"], "name": row["name"], "charter": row["charter"],
                "downloadUrl": "u-" + row["name"], "md5": row["name"]}

    def test_install_all_missing_queues_only_missing_with_charter(self):
        self.shared(fav=[self.ROCK, self.HERE, self.OTHER])
        c = self.companion()
        seen = []

        def resolve(r):
            seen.append((r["name"], r["charter"]))
            return None if r["name"] == "Photograph" else self.item(r)
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=resolve):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "favorites"}})
        self.assertEqual(sorted(seen), [("Photograph", "Harmonix"), ("Rockstar", "Harmonix")])      # Hysteria is here
        self.assertEqual((out["queued"], out["alreadyHave"], out["notFound"]), (1, 1, ["o"]))
        j = c.jobs.submitted[0]
        self.assertEqual((j.kind, j.options["fav_charter"], j.options["process"], j.options["mark_fav"]), ("download", "Harmonix", True, False))
        self.assertEqual(j.options["fav_row"]["charter"], "Harmonix")
        self.assertNotIn("fav_key", j.options)
        self.assertEqual(self.favs(), [])                       # installing is not favouriting

    def test_process_unticked_and_subset_and_favourite(self):
        self.shared(fav=[self.ROCK, self.OTHER, self.HERE])
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "favorites"}, "keys": ["r", "h"],
                                          "process": False, "favourite": True})
        self.assertEqual(out["queued"], 1)
        self.assertFalse(c.jobs.submitted[0].options["process"])
        self.assertTrue(c.jobs.submitted[0].options["mark_fav"])
        self.assertEqual(self.favs(), ["Someone"])             # the one already here is starred straight away

    def test_lookup_failure_is_counted_not_fatal(self):
        self.shared(fav=[self.ROCK])
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=RuntimeError("down")):
            out = api._install_friend(self.companion(), {"friend": BOB, "list": {"kind": "favorites"}})
        self.assertEqual((out["queued"], out["failed"]), (0, 1))

    def test_install_and_add_hands_jobs_to_the_setlist_queue(self):
        self.shared(lists={"Night": [self.ROCK, self.HERE]})
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "setlist": "Night"})
        name, rows, jobs = c.queued[0]
        self.assertEqual((out["setlist"], name, [r["name"] for r in rows], len(jobs)), ("Night", "Night", ["Rockstar", "Hysteria"], 1))
        self.assertEqual(c.jobs.submitted, [])                  # the companion submits them, after registering the setlist

    def test_copy_without_install(self):
        self.shared(lists={"Night": [self.ROCK, self.HERE]})
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite") as res:
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "setlist", "name": "Night"},
                                          "setlist": "Night", "install": False})
        res.assert_not_called()
        self.assertEqual((out["queued"], c.queued[0][2]), (0, []))

    def test_name_clash_gets_a_number(self):
        self.shared(lists={"Night": [self.HERE]})
        setlists.save(self.data, "Night", [])
        c = self.companion()
        out = api._install_friend(c, {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "setlist": "Night", "install": False})
        self.assertEqual(out["setlist"], "Night (2)")

    def test_list_that_is_no_longer_shared(self):
        self.shared(lists={})
        with self.assertRaises(CloudError) as cm:
            api._install_friend(self.companion(), {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "setlist": "x"})
        self.assertEqual(cm.exception.status, 404)


class CompanionSetlistTests(unittest.TestCase):
    """The real Companion: download jobs finish, then the 'setlist' job writes a Clone Hero .setlist from local charts."""
    def make(self, td):
        root = Path(td)
        for name, art, ch in (("a", "Queen", "Harmonix"), ("b", "Muse", "Someone")):
            add_chart(root / "Songs", name, art, name.upper(), ch, name)
        c = Companion(root, Library([root / "Songs"]), Store(root / "store.json"), use_youtube=False)
        c.library.scan()
        c.libmgr._reindex()
        c.libmgr.fav_setlist_enabled = lambda: False
        c.jobs.submit = lambda job: c.submitted.append(job)
        c.submitted = []
        return c

    def test_setlist_written_after_the_downloads_and_matches_local_charts(self):
        with isolated() as td:
            c = self.make(td)
            rows = [song("1", "Queen", "A", "Harmonix"), song("2", "Muse", "B", "Someone"), song("3", "Gone", "Gone", "Nobody")]
            dl = Job("download", "d", options={})
            c.queue_friend_setlist("Night", rows, [dl])
            self.assertEqual([j.kind for j in c.submitted], ["download"])      # setlist waits for the download
            dl.status = "failed"                                               # a failed download still lets the rest through
            c._job_changed(dl)
            sj = c.submitted[-1]
            self.assertEqual(sj.kind, "setlist")
            c._job_changed(dl)                                                 # repeated notifications do not queue it twice
            self.assertEqual(len([j for j in c.submitted if j.kind == "setlist"]), 1)
            steps = []
            result = c._run_job(sj, steps.append)
            self.assertEqual(steps, ["Finding the charts in your library", "Writing the setlist file"])
            got = setlists.decode((Path(td) / "Setlists" / "Night.setlist").read_bytes())
            want = [(c.libmgr.chart_md5(s, compute=True), 100) for s in (c.library.songs[0], c.library.songs[1])]
            self.assertEqual(sorted(got), sorted(want))
            self.assertEqual(len(got), 2)
            self.assertIn("2 songs", str(result))
            self.assertIn("1 not installed", str(result))

    def test_nothing_installed_fails_with_a_step_prefix(self):
        from chcompanion.jobs import JobManager
        with isolated() as td:
            c = self.make(td)
            job = Job("setlist", "t", options={"name": "N", "rows": [song("3", "Gone", "Gone", "Nobody")]}, status="running")
            m = JobManager(c._run_job)
            m.active = 1
            with mock.patch.object(m, "_next", side_effect=[job, None]), self.assertLogs("chcompanion.jobs", level="ERROR"):
                m._worker()
            self.assertEqual(job.status, "failed")
            self.assertTrue(job.error.startswith("Finding the charts in your library"), job.error)
            self.assertFalse((Path(td) / "Setlists" / "N.setlist").exists())

    def test_no_downloads_goes_straight_to_the_setlist_job(self):
        with isolated() as td:
            c = self.make(td)
            c.queue_friend_setlist("Solo", [song("1", "Queen", "A", "Harmonix")], [])
            self.assertEqual([j.kind for j in c.submitted], ["setlist"])


class SetlistSyncTests(Harness):
    def setUp(self):
        super().setUp()
        self.remote = []
        self.sb.routes[("GET", "setlists")] = lambda q, b: (200, [{"name": n} for n in self.remote])
        self.sb.routes[("POST", "rpc/upsert_setlist")] = lambda q, b: (self.remote.append(b["p_name"]) or (200, "id"))
        self.sb.routes[("DELETE", "setlists")] = lambda q, b: (204, None)

    def test_uploads_local_lists_with_chart_details_and_only_when_changed(self):
        h = self.lm.chart_md5(self.chart("Someone"), compute=True)
        setlists.save(self.data, "Night", [(h, 100), ("F" * 32, 100)])
        setlists.save(self.data, self.lm.FAV_SETLIST, [(h, 100)])            # derived from favourites: not uploaded
        self.assertEqual(self.sync.sync_setlists(), 1)
        up = self.sb.calls_to("rpc/upsert_setlist")
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0][3]["p_name"], "Night")
        items = up[0][3]["p_items"]
        self.assertEqual((items[0]["artist"], items[0]["name"], items[0]["charter"], items[0]["checksum"]), ("Muse", "Hysteria", "Someone", h))
        self.assertEqual((items[1]["song_key"], items[1]["name"]), ("F" * 32, ""))     # unknown chart: kept by hash
        self.assertEqual(self.sync.sync_setlists(), 0)                       # unchanged: nothing sent
        setlists.add(self.data, "Night", [self.lm.chart_md5(self.chart("Harmonix"), compute=True)])
        self.assertEqual(self.sync.sync_setlists(), 1)

    def test_deleted_locally_is_deleted_in_the_cloud(self):
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.sync.sync_setlists()
        setlists.delete(self.data, "Night")
        self.sync.sync_setlists()
        d = self.sb.calls_to("setlists", "DELETE")[0]
        self.assertEqual((d[2]["owner"], d[2]["name"]), (f"eq.{ME}", "eq.Night"))

    def test_cloud_lost_the_list_so_it_is_uploaded_again(self):
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.sync.sync_setlists()
        self.remote.clear()
        self.assertEqual(self.sync.sync_setlists(), 1)

    def test_part_of_sync_all_and_signed_out_does_nothing(self):
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [])
        self.sb.routes[("GET", "plays")] = lambda q, b: (200, [])
        res = self.sync.sync_all()
        self.assertTrue(res["ok"])
        self.assertEqual(res["setlists"], 1)
        self.cloud.logout()
        self.sb.calls.clear()
        self.assertFalse(self.sync.sync_all()["ok"])
        self.assertEqual(self.sb.calls, [])


if __name__ == "__main__":
    unittest.main()
