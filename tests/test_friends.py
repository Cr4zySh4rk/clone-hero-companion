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
        self.assertEqual(self.sb.calls_to("rpc/lists_visible")[0][3], {"owner": BOB})
        self.assertEqual(fav[2]["user_id"], f"eq.{BOB}")
        self.assertEqual(sl[2]["owner"], f"eq.{BOB}")
        self.assertNotIn("shared", sl[2])                  # the single switch is enforced by RLS, not by a per-list filter
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

    def test_switch_off_asks_for_nothing_and_shows_the_empty_state(self):
        self.befriend(BOB)
        self.sb.routes[("POST", "rpc/lists_visible")] = lambda q, b: (200, False)
        out = self.fr.friend_lists(BOB)
        self.assertEqual((out["shared"], out["favorites"], out["setlists"]), (False, [], []))
        self.assertEqual(self.sb.calls_to("favorites") + self.sb.calls_to("setlists"), [])      # nothing requested
        self.assertFalse(self.fr.friend_lists(BOB)["shared"])

    def test_switch_on_requests_and_shows_both_lists(self):
        self.befriend(BOB)
        self.sb.routes[("POST", "rpc/lists_visible")] = lambda q, b: (200, True)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("k1", "Muse", "Hysteria", "Someone")])
        self.sb.routes[("GET", "setlists")] = lambda q, b: (200, [{"id": "s", "name": "A", "updated_at": "x", "setlist_items": []},
                                                                  {"id": "t", "name": "B", "updated_at": "x", "setlist_items": []}])
        out = self.fr.friend_lists(BOB)
        self.assertEqual((out["shared"], len(out["favorites"]), [x["name"] for x in out["setlists"]]), (True, 1, ["A", "B"]))

    def test_older_database_without_lists_visible_falls_back_to_the_rows(self):
        self.befriend(BOB)                                  # rpc/lists_visible answers 404 (migration 0003 not applied yet)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("k1", "Muse", "Hysteria", "Someone")])
        out = self.fr.friend_lists(BOB)
        self.assertEqual((out["shared"], len(out["favorites"])), (True, 1))

    def test_one_switch_writes_lists_shared_only(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        self.fr.set_lists_shared(True)
        c = self.sb.calls_to("share_settings", "POST")[0]
        self.assertEqual((c[2], c[3]), ({"on_conflict": "user_id"}, {"user_id": ME, "lists_shared": True}))
        self.assertEqual(self.fr.sharing(), {"lists": True, "stats": True})
        self.assertEqual(self.fr.status()["sharing"], {"lists": True, "stats": True})
        self.fr.set_lists_shared(False)
        self.assertEqual(self.sb.calls_to("share_settings", "POST")[-1][3], {"user_id": ME, "lists_shared": False})
        self.assertEqual(self.fr.sharing(), {"lists": False, "stats": True})
        self.assertEqual(self.sb.calls_to("setlists", "PATCH"), [])      # no per-setlist flags any more

    def test_refresh_reads_the_switch(self):
        self.sb.routes[("GET", "share_settings")] = lambda q, b: (200, [{"lists_shared": True}])
        self.assertEqual(self.fr.refresh()["sharing"], {"lists": True, "stats": True})
        self.assertEqual(self.sb.calls_to("share_settings")[0][2]["select"], "lists_shared,stats_shared")

    def test_switching_on_uploads_every_setlist_now(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        up = []
        self.sb.routes[("POST", "rpc/upsert_setlist")] = lambda q, b: (up.append(b["p_name"]) or (200, "id"))
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        setlists.save(self.data, "Morning", [("B" * 32, 100)])
        with mock.patch.object(self.sync, "schedule"):
            self.fr.set_lists_shared(True)
        self.assertEqual(sorted(up), ["Morning", "Night"])

    def test_old_2_3_payload_maps_to_the_single_switch(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        with mock.patch.object(self.sync, "schedule"):
            self.fr.set_shared("setlist", "Night", True)
        self.assertEqual(self.sb.calls_to("share_settings", "POST")[0][3], {"user_id": ME, "lists_shared": True})
        with self.assertRaises(CloudError):
            self.fr.set_shared("bogus", None, True)

    def test_share_route_takes_new_and_old_payloads(self):
        self.sb.routes[("POST", "share_settings")] = lambda q, b: (201, None)
        sent = []
        h = types.SimpleNamespace(_json=lambda obj, code=200: sent.append((obj, code)))
        c = types.SimpleNamespace(friends=self.fr, cloud=self.cloud)
        with mock.patch.object(self.sync, "schedule"):
            api._friends(h, c, "POST", "/api/friends/share", {}, {"lists_shared": True})
            api._friends(h, c, "POST", "/api/friends/share", {}, {"kind": "favorites", "shared": False})
        self.assertEqual([x[0] for x in sent], [{"lists": True, "stats": True}, {"lists": False, "stats": True}])
        self.assertEqual([c_[3]["lists_shared"] for c_ in self.sb.calls_to("share_settings", "POST")], [True, False])


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
        c.queue_friend_setlist = lambda name, rows, jobs, append=False: c.queued.append((name, rows, jobs, append))
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
        name, rows, jobs, _ap = c.queued[0]
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

    def test_name_clash_appends_the_friends_name(self):
        self.shared(lists={"Night": [self.HERE]})
        setlists.save(self.data, "Night", [])
        c = self.companion()
        body = {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "setlist": "Night", "install": False}
        self.assertEqual(api._install_friend(c, body)["setlist"], "Night (from Bob)")
        setlists.save(self.data, "Night (from Bob)", [])
        self.assertEqual(api._install_friend(c, body)["setlist"], "Night (from Bob) (2)")     # even that is taken
        self.assertEqual(api._install_friend(c, {**body, "setlist": "Fresh"})["setlist"], "Fresh")   # no clash: same name

    def test_favourites_list_never_overwrites_the_derived_favourites_setlist(self):
        self.shared(fav=[self.HERE])
        out = api._install_friend(self.companion(), {"friend": BOB, "list": {"kind": "favorites"}, "setlist": "Favourites", "install": False})
        self.assertEqual(out["setlist"], "Favourites (from Bob)")

    def test_download_missing_installs_only(self):
        self.shared(lists={"Night": [self.ROCK, self.HERE]})
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "process": True})
        self.assertEqual((out["queued"], out["setlist"], c.queued), (1, None, []))
        j = c.jobs.submitted[0]
        self.assertEqual((j.kind, j.options["process"], j.options["mark_fav"]), ("download", True, False))

    def test_add_to_my_setlists_installs_then_writes_the_setlist(self):
        self.shared(lists={"Night": [self.ROCK, self.HERE]})
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "setlist", "name": "Night"}, "setlist": "Night"})
        name, rows, jobs, append = c.queued[0]
        self.assertEqual((out["setlist"], len(jobs), append, c.jobs.submitted), ("Night", 1, False, []))

    def test_add_to_my_favourites_queues_marked_downloads(self):
        self.shared(fav=[self.ROCK, self.HERE])
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "favorites"}, "favourite": True})
        self.assertEqual((out["queued"], c.jobs.submitted[0].options["mark_fav"]), (1, True))
        self.assertEqual(self.favs(), ["Someone"])

    def test_one_song_into_an_existing_setlist_appends(self):
        self.shared(fav=[self.ROCK, self.HERE])
        setlists.save(self.data, "Mine", [("C" * 32, 100)])
        c = self.companion()
        with mock.patch("chcompanion.cloud.resolve_favourite", side_effect=self.item):
            out = api._install_friend(c, {"friend": BOB, "list": {"kind": "favorites"}, "keys": ["r"], "addToSetlist": "Mine"})
        name, rows, jobs, append = c.queued[0]
        self.assertEqual((out["setlist"], name, append, [r["song_key"] for r in rows], len(jobs)), ("Mine", "Mine", True, ["r"], 1))

    def test_one_song_into_a_new_setlist_uses_the_name_given(self):
        self.shared(fav=[self.HERE])
        c = self.companion()
        out = api._install_friend(c, {"friend": BOB, "list": {"kind": "favorites"}, "keys": ["h"], "setlist": "Fresh"})
        self.assertEqual((out["setlist"], c.queued[0][3]), ("Fresh", False))

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

    def test_append_adds_to_the_existing_setlist_after_the_download(self):
        with isolated() as td:
            c = self.make(td)
            h = c.libmgr.chart_md5(next(x for x in c.library.songs if x.artist == "Muse"), compute=True)
            setlists.save(c.data_dir, "Mine", [("C" * 32, 100)])
            c.queue_friend_setlist("Mine", [song("2", "Muse", "B", "Someone")], [], append=True)
            c._run_job(c.submitted[-1], lambda *_: None)
            got = [x[0] for x in setlists.decode((Path(td) / "Setlists" / "Mine.setlist").read_bytes())]
            self.assertEqual(got[0], "C" * 32)
            self.assertEqual(len(got), 2)
            self.assertEqual(got[1], h)

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
        self.sync.set_lists_shared(True)                    # the switch is on unless a test says otherwise

    def test_switch_off_uploads_nothing(self):
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.sync.set_lists_shared(False)
        self.assertEqual(self.sync.sync_setlists(), 0)
        self.assertEqual(self.sb.calls_to("rpc/upsert_setlist"), [])
        self.sync.set_lists_shared(True)                    # switched on later: the list that was waiting goes up
        self.assertEqual(self.sync.sync_setlists(), 1)

    def test_switch_is_read_from_the_cloud_once_when_unknown(self):
        self.sync._shared = None
        self.sb.routes[("GET", "share_settings")] = lambda q, b: (200, [{"lists_shared": True}])
        setlists.save(self.data, "Night", [("A" * 32, 100)])
        self.assertEqual(self.sync.sync_setlists(), 1)
        self.sync.sync_setlists()
        self.assertEqual(len(self.sb.calls_to("share_settings")), 1)

    def test_all_setlists_go_up_including_new_ones_but_not_the_derived_favourites(self):
        for n in ("One", "Two", "Three"):
            setlists.save(self.data, n, [("A" * 32, 100)])
        setlists.save(self.data, self.lm.FAV_SETLIST, [("A" * 32, 100)])
        self.assertEqual(self.sync.sync_setlists(), 3)
        setlists.save(self.data, "Four", [("B" * 32, 100)])         # created later: no per-list switch needed
        self.assertEqual(self.sync.sync_setlists(), 1)
        self.assertEqual(sorted(self.remote), ["Four", "One", "Three", "Two"])

    def test_rename_is_a_delete_and_an_upload(self):
        setlists.save(self.data, "Old", [("A" * 32, 100)])
        self.sync.sync_setlists()
        (setlists.setlists_dir(self.data) / "Old.setlist").rename(setlists.setlists_dir(self.data) / "New.setlist")
        self.assertEqual(self.sync.sync_setlists(), 1)
        self.assertEqual(self.sb.calls_to("setlists", "DELETE")[0][2]["name"], "eq.Old")

    def test_watcher_schedules_a_debounced_sync_only_on_change_and_when_on(self):
        with mock.patch.object(self.sync, "schedule") as sch:
            last = self.sync.watch_setlists(self.data, None)
            self.assertEqual(self.sync.watch_setlists(self.data, last), last)
            sch.assert_not_called()
            setlists.save(self.data, "Night", [("A" * 32, 100)])
            last = self.sync.watch_setlists(self.data, last)
            self.assertEqual(sch.call_count, 1)
            self.sync.set_lists_shared(False)
            setlists.save(self.data, "More", [("A" * 32, 100)])
            self.sync.watch_setlists(self.data, last)
            self.assertEqual(sch.call_count, 1)
        from chcompanion.cloud import DEBOUNCE_S
        self.assertLessEqual(DEBOUNCE_S, 3.0)

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


class MigrationTests(unittest.TestCase):
    """0003_share_all.sql cannot run here (no Postgres); check its shape: idempotent, one batch, and the compat rules."""
    SQL = (Path(__file__).parent.parent / "supabase" / "migrations" / "0003_share_all.sql").read_text("utf-8")
    CODE = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))

    def test_idempotent_and_batch_safe(self):
        low = self.CODE.lower()
        self.assertNotIn("concurrently", low)
        self.assertNotRegex(low, r"(?m)^\\")           # no psql meta-commands
        self.assertEqual(low.count("create policy"), low.count("drop policy if exists"))
        self.assertIn("create or replace function public.lists_visible", low)
        self.assertIn("drop trigger if exists share_settings_sync_flags", low)
        self.assertRegex(low, r"if not exists \(select 1 from information_schema\.columns")      # backfill runs once

    def test_helper_is_security_definer_with_empty_search_path(self):
        seg = self.CODE.split("create or replace function public.lists_visible")[1][:400]
        self.assertIn("security definer set search_path = ''", seg)
        self.assertIn("are_friends(auth.uid(), owner)", seg)
        self.assertIn("lists_shared", seg)

    def test_friend_policies_use_lists_visible_not_the_old_flags(self):
        for pol in ("friends read shared favorites", "friends read shared setlists", "friends read shared setlist items"):
            seg = self.CODE.split(f'create policy "{pol}"')[1].split(";")[0]
            self.assertIn("lists_visible", seg, pol)
            self.assertNotIn("favorites_shared", seg)
            self.assertNotRegex(seg, r"\bshared\b")
        fav = self.CODE.split("create or replace function public.favorites_visible")[1].split("$$;")[0]
        self.assertIn("lists_visible", fav)
        self.assertNotIn("favorites_shared", fav)

    def test_old_clients_can_still_turn_sharing_on(self):
        """Python model of the trigger's rules, asserted against the SQL text so the two cannot drift apart."""
        def trig(old, new):
            if old is None:
                if new["lists"]:
                    new["fav"] = True
                elif new["fav"]:
                    new["lists"] = True
            elif new["lists"] != old["lists"]:
                new["fav"] = new["lists"]
            elif new["fav"] != old["fav"]:
                new["lists"] = new["fav"]
            return new
        self.assertEqual(trig({"lists": False, "fav": False}, {"lists": False, "fav": True}), {"lists": True, "fav": True})
        self.assertEqual(trig({"lists": True, "fav": True}, {"lists": True, "fav": False}), {"lists": False, "fav": False})
        self.assertEqual(trig({"lists": False, "fav": False}, {"lists": True, "fav": False}), {"lists": True, "fav": True})
        self.assertEqual(trig(None, {"lists": False, "fav": True}), {"lists": True, "fav": True})
        body = self.CODE.split("create or replace function public.share_settings_sync_flags")[1].split("$$;")[0]
        for needle in ("new.lists_shared := true", "new.favorites_shared := new.lists_shared", "new.lists_shared := new.favorites_shared"):
            self.assertIn(needle, body)
        self.assertIn("before insert or update on public.share_settings", self.CODE)

    def test_backfill_is_favourites_or_any_shared_setlist(self):
        low = self.CODE.lower()
        self.assertIn("where s.favorites_shared", low)
        self.assertIn("where l.shared", low)
        self.assertIn("lists_shared boolean not null default false", low)


class FriendPageApiTests(Harness):
    """/api/friends/list: what the friend page is given, and the empty state when the friend is not sharing."""
    def call(self, rows_visible):
        self.befriend(BOB)
        self.sb.routes[("POST", "rpc/lists_visible")] = lambda q, b: (200, rows_visible)
        self.sb.routes[("GET", "favorites")] = lambda q, b: (200, [song("a", "Queen", "Bohemian", "Harmonix")])
        sent = []
        h = types.SimpleNamespace(_json=lambda obj, code=200: sent.append((obj, code)))
        api._friends(h, types.SimpleNamespace(friends=self.fr, cloud=self.cloud), "GET", "/api/friends/list", {"friend": BOB}, {})
        return sent[0][0]

    def test_not_sharing_is_the_empty_state(self):
        d = self.call(False)
        self.assertEqual((d["shared"], d["favorites"], d["setlists"], d["friend"]["username"]), (False, [], [], "bob"))

    def test_sharing_annotates_present_and_missing(self):
        self.chart("Harmonix")
        d = self.call(True)
        self.assertTrue(d["shared"])
        self.assertEqual(len(d["favorites"]), 1)
        self.assertIn("present", d["favorites"][0])


if __name__ == "__main__":
    unittest.main()
