import unittest
from datetime import datetime, timedelta

from chcompanion import playstats


def play(cid, checksum, name, hit, total=100, score=1000, days=0):
    t = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    return {"client_id": cid, "played_at": t, "checksum": checksum, "artist": "Band", "name": name,
            "instrument": "guitar", "difficulty": "expert", "score": score, "stars": 3,
            "notes_hit": hit, "total_notes": total, "fc": hit == total}


class NeedsPracticeTests(unittest.TestCase):
    def test_lowest_personal_best_first_including_single_plays(self):
        plays = [
            play("a1", "A", "Played once, weak", 40),           # PB 40% from one play
            play("b1", "B", "Improved", 30, days=2),             # PB is 95%, not the 30% attempt
            play("b2", "B", "Improved", 95, days=1),
            play("c1", "C", "Middling", 70),
        ]
        names = [c["name"] for c in playstats.compute(plays)["needsPractice"]]
        self.assertEqual(names, ["Played once, weak", "Middling", "Improved"])

    def test_only_played_songs(self):
        self.assertEqual(playstats.compute([])["needsPractice"], [])


if __name__ == "__main__":
    unittest.main()
