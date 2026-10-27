import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from chcompanion import leaderboard, playstats


def lb_row(sid, name="Song A", artist="Band", inst="guitar", diff="expert", score=1000, fc=False,
           stars=4, created="2026-10-20T10:00:00.000Z", song="HASHA", acct=7):
    return {"scoreId": sid, "songHash": song, "name": name, "artist": artist, "charter": "Cha", "instrument": inst,
            "difficulty": diff, "speed": 100, "isFc": fc, "totalScore": score, "stars": stars, "notesHit": 90,
            "noteCount": 100, "accuracy": "90.0", "maxCombo": 50, "failedInRun": False, "createdAt": created,
            "accountId": acct, "duration": 120000}


def local(cid, name="Song A", score=1000, played="2026-10-20T10:01:00Z", fc=False, stars=4, diff="expert"):
    return {"client_id": cid, "played_at": played, "checksum": "ABCDEF", "artist": "Band", "name": name,
            "instrument": "guitar", "difficulty": diff, "score": score, "stars": stars,
            "notes_hit": 90, "total_notes": 100, "fc": fc}


class FCCountTests(unittest.TestCase):
    def test_fcs_and_five_stars_count_distinct_charts(self):
        plays = [local("1", fc=True, stars=5), local("2", fc=True, stars=5, score=2000),   # same chart twice
                 local("3", diff="hard", fc=True, stars=5), local("4", name="Other", fc=False, stars=3)]
        t = playstats.compute(plays)["totals"]
        self.assertEqual((t["plays"], t["songs"], t["fcs"], t["fiveStars"]), (4, 2, 2, 2))

    def test_filter_applies_to_distinct_counts(self):
        plays = [local("1", fc=True), local("2", diff="hard", fc=True)]
        self.assertEqual(playstats.compute(plays, difficulty="hard")["totals"]["fcs"], 1)


class ImportTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.log = playstats.PlayLog(d / "p.jsonl", d / "c.jsonl", d / "lb.jsonl")

    def test_row_mapping(self):
        p = playstats.lb_row_to_play(lb_row(11, fc=True, stars=5, score=4321, diff="hard", inst="prodrums"))
        self.assertEqual((p["source"], p["client_id"], p["score"], p["stars"], p["fc"], p["difficulty"], p["instrument"],
                          p["speed"], p["accuracy"], p["notes_hit"], p["total_notes"], p["played_at"]),
                         ("leaderboard", "lb|11", 4321, 5, True, "hard", "drums", 100, 0.9, 90, 100,
                          "2026-10-20T10:00:00Z"))
        self.assertIsNone(playstats.lb_row_to_play({"name": "no id"}))

    def test_account_scores_pages_through_mocked_api(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if "/profile" in url:
                return {"latestSubmissions": [lb_row(1), lb_row(2, name="B", song="HB", fc=True)],
                        "mostPlayed": [{"songHash": "HC", "name": "C", "artist": "Band", "plays": 3}]}
            return {"near": {"scoreData": [lb_row(3, name="C", song="HC", acct=7, fc=True),
                                           lb_row(99, name="C", song="HC", acct=8)]}}
        c = leaderboard.Client(fetch=fetch, sleep=lambda s: None)
        rows = c.account_scores(7)
        self.assertEqual(sorted(r["scoreId"] for r in rows), [1, 2, 3])      # other players' rows ignored
        self.assertTrue(all("accountId=7" in u for u in calls if "/scores?" in u))
        c.account_scores(7)
        self.assertEqual(len(calls), 2)                                     # second call served from the 10 min cache

    def test_dedupe_against_local_plays(self):
        self.log.append([local("l1", score=1000)])
        self.log.set_leaderboard([playstats.lb_row_to_play(r) for r in (
            lb_row(1, score=1000),                                          # same play as l1 (1 minute apart)
            lb_row(2, score=1000, created="2026-10-20T12:00:00Z"),          # same score, hours later: kept
            lb_row(3, score=1500),                                          # different score: kept
            lb_row(4, name="Only on LB", song="HZ"))])
        allp = self.log.all()
        self.assertEqual(sorted(p["client_id"] for p in allp), ["l1", "lb|2", "lb|3", "lb|4"])
        res = playstats.compute(allp)
        self.assertEqual(res["playSources"], {"app": 1, "leaderboard": 3})
        self.assertEqual(res["totals"]["songs"], 2)                          # 'Song A' is one song across both sources

    def test_not_uploaded_to_cloud(self):
        self.log.append([local("l1")])
        self.log.set_leaderboard([playstats.lb_row_to_play(lb_row(5, name="X"))])
        self.assertEqual([r["client_id"] for r in self.log.local()], ["l1"])   # what cloud sync pushes
        self.assertEqual(self.log.cloud(), [])
        self.assertIn("lb|5", [r["client_id"] for r in self.log.all()])

    def test_lists_mix_sources_and_needs_practice_keeps_lowest_pb(self):
        weak = lb_row(6, name="Weak", song="HW"); weak["notesHit"] = 40
        self.log.append([local("l1", name="Strong")])
        self.log.set_leaderboard([playstats.lb_row_to_play(weak)])
        res = playstats.compute(self.log.all())
        self.assertEqual([c["name"] for c in res["needsPractice"]], ["Weak", "Strong"])
        self.assertEqual({c["name"] for c in res["mostPlayed"]}, {"Weak", "Strong"})

    def test_caption_data_and_age(self):
        self.assertIsNone(self.log.lb_age())
        self.log.set_leaderboard([playstats.lb_row_to_play(lb_row(1))])
        self.assertLess(self.log.lb_age(), 5)
        self.assertEqual(playstats.compute(self.log.all())["playSources"], {"app": 0, "leaderboard": 1})



class ChartVersionTests(unittest.TestCase):
    def test_same_title_different_song_hash_counts_separately(self):
        rows = [lb_row(1, fc=True, song="H1"), lb_row(2, fc=True, song="H2"), lb_row(3, fc=True, song="H1", score=5)]
        t = playstats.compute([playstats.lb_row_to_play(r) for r in rows])["totals"]
        self.assertEqual((t["fcs"], t["songs"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
