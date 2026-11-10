import json
import struct
import tempfile
import unittest
from pathlib import Path

from chcompanion import playstats, scoredata
from chcompanion.cloud import CloudSync
from test_cloud import FakeLib, FakeStore, SESSION, make_cloud

MD5_A = "8d66422b8827baa1d09f1c7fd7033af8"
MD5_B = "00112233445566778899aabbccddeeff"
MD5_C = "ffeeddccbbaa99887766554433221100"


def build(charts, version=scoredata.VERSION, count=None):
    """charts: [(md5, playcount, [(instrument, difficulty, percent, fc, speed, stars, mods, score)])]"""
    out = struct.pack("<II", version, len(charts) if count is None else count)
    for md5, plays, rows in charts:
        out += bytes.fromhex(md5) + bytes([len(rows)]) + plays.to_bytes(3, "little")
        for r in rows:
            out += struct.pack("<HBBBHBII", *r)
    return out


GOOD = build([
    (MD5_A, 12, [(0, 3, 98, 1, 100, 5, 1, 123456), (6, 3, 80, 0, 100, 4, 1, 5000)]),   # guitar expert 5* FC; drums expert 4*
    (MD5_B, 3, [(1, 2, 100, 1, 100, 6, 1, 99999), (8, 3, 70, 0, 100, 3, 0, 1)]),        # bass hard gold FC; band row
])


class ParseTests(unittest.TestCase):
    def test_parse_rows(self):
        rows = scoredata.parse(GOOD)
        self.assertEqual(len(rows), 4)
        g = rows[0]
        self.assertEqual((g["checksum"], g["instrument"], g["difficulty"], g["stars"], g["score"], g["fc"],
                          g["percent"], g["speed"], g["play_count"]),
                         (MD5_A.upper(), "guitar", "expert", 5, 123456, True, 98, 100, 12))
        self.assertEqual((rows[1]["instrument"], rows[1]["fc"]), ("drums", False))
        self.assertEqual((rows[2]["instrument"], rows[2]["difficulty"], rows[2]["stars"]), ("bass", "hard", 6))
        self.assertEqual(rows[3]["instrument"], "band")
        self.assertTrue(scoredata.sane(rows))

    def test_truncated_returns_what_it_can_and_never_raises(self):
        rows, complete = scoredata._parse(GOOD[:-5])
        self.assertFalse(complete)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(scoredata.parse(GOOD[:-5])), 3)
        self.assertEqual(scoredata.parse(GOOD[:10]), [])
        self.assertEqual(scoredata.parse(b""), [])
        self.assertEqual(scoredata.parse(None), [])

    def test_unknown_version_and_trailing_bytes(self):
        self.assertEqual(scoredata.parse(build([(MD5_A, 1, [])], version=1)), [])
        _rows, complete = scoredata._parse(GOOD + b"x")
        self.assertFalse(complete)

    def test_sanity_rejects_out_of_range(self):
        bad = scoredata.parse(build([(MD5_A, 1, [(0, 3, 150, 0, 100, 5, 1, 10)])]))
        self.assertFalse(scoredata.sane(bad))
        bad = scoredata.parse(build([(MD5_A, 1, [(0, 3, 50, 7, 100, 5, 1, 10)])]))
        self.assertFalse(scoredata.sane(bad))
        self.assertFalse(scoredata.sane([]))


class CacheTests(unittest.TestCase):
    def test_reread_only_when_changed_and_unusable_files_are_ignored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            p = Path(tmp) / "scoredata.bin"
            cache = scoredata.ScoreDataCache(lambda: [Path(tmp) / "missing.bin", p])
            self.assertEqual(cache.read(), [])
            p.write_bytes(GOOD[:-5])                      # truncated: not used
            self.assertEqual(cache.read(), [])
            p.write_bytes(GOOD)
            self.assertEqual(len(cache.read()), 4)
            calls = []
            orig = scoredata._parse
            scoredata._parse = lambda d: calls.append(1) or orig(d)
            try:
                cache.read()
                cache.read()
                self.assertEqual(calls, [])               # unchanged: not re-parsed
                p.write_bytes(build([(MD5_C, 1, [(0, 3, 90, 0, 100, 5, 1, 1)])]))
                self.assertEqual(len(cache.read()), 1)
                self.assertEqual(len(calls), 1)
            finally:
                scoredata._parse = orig

    def test_candidate_locations(self):
        c = [str(p) for p in scoredata.candidate_files(Path("/data"), Path("/game"))]
        self.assertTrue(any(x.endswith("scoredata.bin") for x in c))
        self.assertIn(str(Path("/data/scoredata.bin")), c)
        self.assertIn(str(Path("/game/scoredata.bin")), c)


def play(cid, artist="Band", name="Song A", inst="guitar", diff="expert", stars=4, fc=False, checksum=MD5_A.upper(),
         source=None, score=1000):
    return {"client_id": cid, "played_at": "2026-10-20T10:01:00Z", "checksum": checksum, "artist": artist,
            "name": name, "instrument": inst, "difficulty": diff, "score": score, "stars": stars,
            "notes_hit": 90, "total_notes": 100, "fc": fc, **({"source": source} if source else {})}


class MergeTests(unittest.TestCase):
    def rows(self, plays, lookup=None):
        return playstats.resolve_scoredata(scoredata.parse(GOOD), plays, lookup)

    def test_union_takes_best_stars_and_fc_per_chart(self):
        plays = [play("1", stars=4, fc=False)]                      # same chart as the guitar row (5 stars, FC)
        t = playstats.compute(plays, scoredata=self.rows(plays))["totals"]
        # guitar chart: 5 stars + FC from scoredata beat the play; the bass chart (gold, FC) is another chart
        self.assertEqual(t["starCounts"], {"5": 1, "6": 1, "7": 0})
        self.assertEqual(t["fcs"], 2)
        # without scoredata the app's own play says 4 stars, no FC
        t0 = playstats.compute(plays)["totals"]
        self.assertEqual((t0["starCounts"]["5"], t0["fcs"]), (0, 0))

    def test_scoredata_never_overwrites_the_day_streak(self):
        """2.6.5: the scoredata loop reused `cur` (the current streak) and left a [stars, fc] list in totals.streak."""
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        plays = []
        for i in range(3):
            p = play(str(i), stars=4)
            p["played_at"] = (now - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            plays.append(p)
        res = playstats.compute(plays, now=now, scoredata=self.rows(plays))
        t = res["totals"]
        self.assertEqual((t["streak"], t["longestStreak"]), (3, 3))
        for k, v in t.items():
            self.assertTrue(isinstance(v, (int, float, dict)) and not isinstance(v, bool) or v is None, (k, v))
        snap = playstats.career_snapshot(res, "x")["totals"]
        self.assertEqual((snap["streak"], snap["longestStreak"]), (3, 3))
        self.assertTrue(all(isinstance(snap[k], int) for k in ("plays", "songs", "fcs", "streak", "longestStreak", "fiveOrMore")))

    def test_plays_can_beat_scoredata_and_nothing_is_double_counted(self):
        plays = [play("1", stars=6, fc=True)]
        res = playstats.compute(plays, scoredata=self.rows(plays))
        # the play's 6 stars replace the guitar row's 5 (one chart, counted once); bass is the second gold chart
        self.assertEqual(res["totals"]["starCounts"], {"5": 0, "6": 2, "7": 0})
        self.assertEqual(res["totals"]["fcs"], 2)

    def test_unresolved_rows_count_by_checksum_and_band_rows_never(self):
        res = playstats.compute([], scoredata=self.rows([]))
        t = res["totals"]
        # guitar 5*, drums 4*, bass gold; the band row is ignored. Two FCs (guitar, bass).
        self.assertEqual((t["starCounts"], t["fcs"], t["fiveStars"]), ({"5": 1, "6": 1, "7": 0}, 2, 2))
        self.assertEqual(res["sources"], {"plays": 0, "lbScores": 0, "scoredataSongs": 2})

    def test_unresolved_rows_skipped_when_leaderboard_scores_present(self):
        lb = playstats.lb_row_to_play({"scoreId": 1, "songHash": "H", "name": "Other", "artist": "X", "instrument": "guitar",
                                       "difficulty": "expert", "speed": 100, "isFc": False, "totalScore": 10, "stars": 3,
                                       "notesHit": 1, "noteCount": 2, "createdAt": "2026-10-20T10:00:00.000Z"})
        res = playstats.compute([lb], scoredata=self.rows([lb]))
        self.assertEqual(res["totals"]["starCounts"]["5"], 0)
        self.assertEqual(res["sources"]["scoredataSongs"], 0)
        self.assertEqual(res["sources"]["lbScores"], 1)

    def test_library_lookup_resolves_to_the_leaderboard_song(self):
        lb = playstats.lb_row_to_play({"scoreId": 1, "songHash": "H", "name": "Song A", "artist": "Band",
                                       "instrument": "guitar", "difficulty": "expert", "speed": 100, "isFc": False,
                                       "totalScore": 10, "stars": 3, "notesHit": 1, "noteCount": 2,
                                       "createdAt": "2026-10-20T10:00:00.000Z"})
        rows = self.rows([lb], lambda c: ("Band", "Song A") if c == MD5_A.upper() else None)
        t = playstats.compute([lb], scoredata=rows)["totals"]
        self.assertEqual((t["starCounts"]["5"], t["fcs"]), (1, 1))   # merged into the LB chart, not added next to it

    def test_filters_use_scoredata_instrument_and_difficulty(self):
        plays = []
        rows = self.rows(plays)
        self.assertEqual(playstats.compute(plays, instrument="bass", scoredata=rows)["totals"]["starCounts"]["6"], 1)
        self.assertEqual(playstats.compute(plays, instrument="guitar", scoredata=rows)["totals"]["starCounts"]["6"], 0)
        self.assertEqual(playstats.compute(plays, difficulty="hard", scoredata=rows)["totals"]["fcs"], 1)
        self.assertEqual(playstats.compute(plays, instrument="drums", difficulty="easy", scoredata=rows)["totals"]["fcs"], 0)

    def test_merge_account_keeps_max_rule(self):
        res = playstats.compute([], scoredata=self.rows([]))
        playstats.merge_account(res, {"stats": {"totalFcs": 5}}, "me")
        self.assertEqual(res["totals"]["fcs"], 5)
        res = playstats.compute([], scoredata=self.rows([]))
        playstats.merge_account(res, {"stats": {"totalFcs": 1}}, "me")
        self.assertEqual(res["totals"]["fcs"], 2)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_has_no_ch_account_data(self):
        res = playstats.compute([play("1")])
        stats = {"totalPfcs": 8, "totalFcs": 8, "totalScore": 8927958, "songsPlayed": 118, "totalTimePlayedMs": 45605411,
                 "notesHit": 56487, "totalStarsEarned": 581, "highestCombo": 327}
        ranks = {"totalScore": 15109, "submissions": 6928, "totalFcs": 5879, "totalPfcs": 5764, "hoursPlayed": 6647,
                 "starsEarned": 6574, "notesHit": 15201}
        s = playstats.career_snapshot(res, "2.7.0")
        self.assertEqual(s["app"], "2.7.0")
        for k in ("stats", "ranks", "ch_username", "ch_account_id", "topArtists", "topInstruments"):
            self.assertNotIn(k, s)                                             # CH data comes from the server (ch_profile)
        self.assertEqual(s["totals"]["fcs"], 0)
        self.assertEqual(set(s["totals"]["starCounts"]), {"5", "6", "7"})
        json.dumps(s)
        prof = {"accountId": 83556, "username": "me", "stats": stats, "ranks": ranks}
        self.assertEqual(playstats.account_section(prof)["stats"], stats)
        self.assertIsNone(playstats.account_section({"stats": {}}))

    def test_push_only_when_changed_and_quiet_on_failure(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sent = []
            fail = []

            def h(method, url, headers, body):
                if fail:
                    return 400, {"message": "column career does not exist"}
                sent.append((method, url, body))
                return 200, None
            c, _ = make_cloud(tmp, h)
            c._store_session(SESSION)
            sync = CloudSync(c, FakeStore(), FakeLib(), playstats.PlayLog(Path(tmp) / "p.jsonl", Path(tmp) / "pc.jsonl"),
                             Path(tmp) / "state.json")
            snap = {"v": 1, "totals": {"fcs": 1}}
            sync.career = lambda: snap
            self.assertEqual(sync.push_career(), 1)
            self.assertEqual(sent[0][0], "PATCH")
            self.assertIn("profiles", sent[0][1])
            self.assertEqual(sent[0][2]["career"], snap)
            self.assertIn("career_at", sent[0][2])
            self.assertEqual(sync.push_career(), 0)       # unchanged
            self.assertEqual(len(sent), 1)
            snap = {"v": 1, "totals": {"fcs": 2}}
            fail.append(1)
            self.assertEqual(sync.push_career(), 0)       # failure is quiet, and retried later
            fail.clear()
            self.assertEqual(sync.push_career(), 1)


if __name__ == "__main__":
    unittest.main()
