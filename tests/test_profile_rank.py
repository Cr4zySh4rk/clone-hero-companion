"""2.6.5: the Profile's rank / XP (same formula as site/career.js), the per-instrument cards and the extras."""
import unittest
from datetime import datetime, timedelta, timezone

from chcompanion import playstats

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def play(i, inst="guitar", diff="expert", hit=100, fc=False, source=None, **kw):
    t = NOW - timedelta(days=i % 5, minutes=i)
    p = {"client_id": f"c{i}", "played_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "artist": "Band", "name": f"Song {i % 3}", "instrument": inst,
         "difficulty": diff, "score": 1000 * (i + 1), "stars": 4, "notes_hit": hit, "total_notes": 120, "max_streak": 50 + i, "fc": fc,
         "duration_s": 120.0, **kw}
    if source:
        p["source"] = source
    return p


class RankTests(unittest.TestCase):
    def test_xp_curve_matches_the_website(self):
        self.assertEqual([playstats.xp_for_rank(r) for r in (1, 2, 3, 10, 50, 100)], [0, 500, 1625, 20950, 373511, 1234667])

    def test_xp_total(self):
        self.assertEqual(playstats.xp_total(plays=3, notes_hit=1999), 99 + 150)

    def test_rank_info_edges(self):
        self.assertEqual(playstats.rank_info(0)["rank"], 1)
        self.assertEqual(playstats.rank_info(499)["rank"], 1)
        r = playstats.rank_info(500)
        self.assertEqual((r["rank"], r["lo"], r["hi"], r["pct"]), (2, 500, 1625, 0))
        r = playstats.rank_info(1062)
        self.assertEqual(r["rank"], 2)
        self.assertAlmostEqual(r["pct"], (1062 - 500) / (1625 - 500))

    def test_compute_rank_ignores_filters_and_leaderboard_rows(self):
        plays = [play(i, inst="guitar" if i % 2 else "drums", hit=200) for i in range(10)] + [play(50 + i, source=playstats.LB_SOURCE) for i in range(5)]
        full = playstats.compute(plays, now=NOW)
        filtered = playstats.compute(plays, instrument="drums", now=NOW)
        self.assertEqual(full["rank"]["xp"], playstats.xp_total(10, 2000))
        self.assertEqual(filtered["rank"], full["rank"])
        self.assertLess(filtered["totals"]["plays"], full["totals"]["plays"])

    def test_empty(self):
        r = playstats.compute([], now=NOW)
        self.assertEqual((r["rank"]["rank"], r["rank"]["xp"]), (1, 0))
        self.assertIsNone(r["extras"]["firstPlay"])
        self.assertEqual(r["instruments"], [])


class ExtrasTests(unittest.TestCase):
    def test_instrument_cards(self):
        plays = [play(0, "guitar", "expert", fc=True), play(1, "guitar", "hard"), play(2, "guitar", "hard"), play(3, "drums", "easy")]
        r = playstats.compute(plays, now=NOW)
        g, d = r["instruments"]
        self.assertEqual((g["instrument"], g["plays"], g["fcs"], g["favDifficulty"]), ("guitar", 3, 1, "hard"))
        self.assertEqual((d["instrument"], d["plays"], d["favDifficulty"]), ("drums", 1, "easy"))
        self.assertAlmostEqual(g["seconds"], 360.0)
        self.assertAlmostEqual(r["totals"]["seconds"], 480.0)

    def test_extras(self):
        r = playstats.compute([play(i) for i in range(6)], now=NOW)
        x = r["extras"]
        self.assertEqual(x["highestScore"]["score"], 6000)
        self.assertEqual(x["longestNoteStreak"]["streak"], 55)
        self.assertEqual(x["avgStars"], 4)
        self.assertEqual(x["topWeekday"], r["weekday"].index(max(r["weekday"])))
        self.assertGreater(x["playsPerWeek"], 0)


if __name__ == "__main__":
    unittest.main()


class ProfileFreshnessTests(unittest.TestCase):
    def test_profile_max_age_bypasses_the_response_cache_and_reports_fetched_at(self):
        from chcompanion import leaderboard
        clock = [1000.0]
        calls = []

        def fetch(url):
            calls.append(url)
            return {"accountId": 7, "username": "me", "stats": {"totalFcs": len(calls)}}
        c = leaderboard.Client(fetch=fetch, now=lambda: clock[0], sleep=lambda s: None)
        a = c.profile(7)
        clock[0] += 120                                  # inside the 10-minute cache: no new request
        self.assertEqual(c.profile(7)["stats"]["totalFcs"], 1)
        b = c.profile(7, max_age=60)                     # older than 60 s: fetched again
        self.assertEqual((b["stats"]["totalFcs"], len(calls)), (2, 2))
        self.assertEqual((a["fetchedAt"], b["fetchedAt"]), (1000.0, 1120.0))
        self.assertEqual(c.profile(7, max_age=0)["stats"]["totalFcs"], 3)     # 0 = always live

    def test_account_section_carries_fetched_at_but_the_snapshot_stays_timestamp_free(self):
        prof = {"accountId": 7, "username": "me", "fetchedAt": 5.0, "stats": {"totalFcs": 8}, "ranks": {}}
        self.assertEqual(playstats.account_section(prof)["fetchedAt"], 5.0)
        snap = playstats.career_snapshot(playstats.compute([], now=NOW), "x")
        self.assertNotIn("fetchedAt", snap)
        for k in ("stats", "ranks", "ch_username", "ch_account_id", "topArtists"):     # 2.7.0: CH data is server-written, not uploaded
            self.assertNotIn(k, snap)
