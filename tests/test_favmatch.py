"""Forgiving favourite matching (Encore/RhythmVerse result lists as returned live) and recovered-title cleanup."""
import unittest

from chcompanion import cloud, favrecover


def E(name, artist, charter, modified="2020-01-01"):
    return {"source": "encore", "id": f"e:{name}|{artist}|{charter}", "name": name, "artist": artist, "charter": charter,
            "downloadUrl": "u:" + name, "md5": name[:6], "modified": modified}


def S(*items):
    calls = []

    def f(q, per_page=50):
        assert per_page <= 48
        calls.append(q)
        return {"songs": list(items)}
    f.calls = calls
    return f


def row(name, artist, charter):
    return {"song_key": "k", "name": name, "artist": artist, "charter": charter, "checksum": "", "source": {}}


class Match(unittest.TestCase):
    def test_wavegroup_artist_and_prototype_not_chosen(self):
        res = [E("Iron Man (Sep 3, 2005 Prototype)", "Black Sabbath (WaveGroup)", "Harmonix", "2025-02-18"),
               E("Iron Man", "Black Sabbath", "SucreMondes"), E("Iron Man (Live)", "Black Sabbath", "Yoel805"),
               E("Iron Man", "Black Sabbath (WaveGroup)", "Harmonix")]
        got = cloud.resolve_on_encore(row("Iron Man", "Black Sabbath", "harmonix"), S(*res))
        self.assertEqual(got, res[3])

    def test_sweet_child_demo_not_chosen(self):
        res = [E("Sweet Child O' Mine (Late 2006 Retail Demo)", "Guns n' Roses (WaveGroup)", "Harmonix", "2025-01-01"),
               E("Sweet Child O' Mine", "Guns n' Roses (WaveGroup)", "Harmonix")]
        got = cloud.resolve_on_encore(row("Sweet Child o' Mine", "Guns N' Roses", "harmonix"), S(*res))
        self.assertEqual(got, res[1])
        only_demo = cloud.resolve_on_encore(row("Sweet Child o' Mine", "Guns N' Roses", "harmonix"), S(res[0]))
        self.assertEqual(only_demo, res[0])          # penalised, not forbidden

    def test_ace_of_spades_08_and_other_charter(self):
        res = [E("Ace of Spades", "Motörhead", "Neversoft"), E("Ace of Spades '08", "Motörhead", "Harmonix")]
        got = cloud.resolve_on_encore(row("Ace of Spades", "Motörhead", "harmonix"), S(*res))
        self.assertEqual(got, res[1])
        self.assertEqual(cloud.resolve_on_encore(row("Ace of Spades", "Motörhead", "neversoft"), S(*res)), res[0])

    def test_acdc_and_live_significant(self):
        live, studio = E("You Shook Me All Night Long (Live)", "AC/DC", "Harmonix"), E("You Shook Me All Night Long", "AC/DC", "Harmonix")
        self.assertEqual(cloud.resolve_on_encore(row("You Shook Me All Night Long (Live)", "ACDC", "harmonix"), S(studio, live)), live)
        self.assertEqual(cloud.resolve_on_encore(row("You Shook Me All Night Long", "ACDC", "harmonix"), S(live, studio)), studio)
        self.assertIsNone(cloud.resolve_on_encore(row("You Shook Me All Night Long (Live)", "ACDC", "harmonix"), S(studio)))

    def test_other_wavegroup_variants(self):
        for t, a in (("Cochise", "Audioslave"), ("War Pigs", "Black Sabbath"), ("Bad Reputation", "Thin Lizzy")):
            r = E(t, a + " (WaveGroup)", "Harmonix")
            self.assertEqual(cloud.resolve_on_encore(row(t, a, "harmonix"), S(r)), r)

    def test_wrong_charter_rejected_and_credit_list(self):
        r = E("Paint It Black", "The Rolling Stones", "Capta1n_GuitarHero, Neversoft")
        self.assertIsNone(cloud.resolve_on_encore(row("Paint It Black", "Rolling Stones", "harmonix"), S(r)))
        self.assertEqual(cloud.resolve_on_encore(row("Paint It Black", "Rolling Stones", "neversoft"), S(r)), r)

    def test_empty_charter_is_a_marked_guess_preferring_exact_title(self):
        res = [E("Welcome to the Jungle (Remastered)", "Guns N' Roses", "A"), E("Welcome to the Jungle", "Guns N' Roses", "Unknown Charter")]
        got = cloud.resolve_on_encore(row("Welcome to the Jungle", "Guns N' Roses", ""), S(*res))
        self.assertEqual(got["charter"], "Unknown Charter")
        self.assertTrue(got["guess"])
        self.assertNotIn("guess", cloud.resolve_on_encore(row("Welcome to the Jungle", "Guns N' Roses", "unknown charter"), S(*res)))

    def test_checksum_wins_and_queries_merge(self):
        a = E("Da Funk", "Daft Punk", "Justin?")
        b = {**E("Da Funk", "Daft Punk", "Justin?", "2024-01-01"), "md5": "other"}
        self.assertEqual(cloud.resolve_on_encore({**row("Da Funk", "Daft Punk", "justin"), "checksum": a["md5"].upper()}, S(b, {**a, "id": "beta", "name": "Da Funk (Beta)"})) ["name"], "Da Funk (Beta)")
        calls = []
        n = {"i": 0}

        def search(q, per_page=50):
            calls.append(q)
            n["i"] += 1
            return {"songs": [] if n["i"] < 2 else [a]}
        self.assertEqual(cloud.resolve_on_encore(row("Da Funk", "Daft Punk", "justin"), search), a)
        self.assertEqual(calls, ["Daft Punk Da Funk", "Da Funk"])

    def test_rhythmverse_same_matching(self):
        r = {**E("Cochise", "Audioslave (Harmonix)", "<color=#f00>Harmonix</color>"), "source": "rhythmverse", "id": "rv:1"}
        self.assertEqual(cloud.resolve_on_rhythmverse(row("Cochise", "Audioslave", "harmonix"), S(r)), r)


class Titles(unittest.TestCase):
    def test_strip_charter_suffix(self):
        f = favrecover.strip_charter_suffix
        self.assertEqual(f("Da Funk (Justin? )", "justin"), "Da Funk")
        self.assertEqual(f("Welcome to the Jungle (Unknown Charter)", ""), "Welcome to the Jungle")
        self.assertEqual(f("Song (Live) (Unknown Charter)", ""), "Song (Live)")
        self.assertEqual(f("Song (Live)", "harmonix"), "Song (Live)")
        self.assertEqual(f(f("Da Funk (Justin? )", "justin"), "justin"), "Da Funk")

    def test_parse_pid(self):
        p = favrecover.parse_pid("daft punk da funk|justin|Songs/Daft Punk - Da Funk (Justin? )")
        self.assertEqual((p["artist"], p["name"]), ("Daft Punk", "Da Funk"))
        p = favrecover.parse_pid("x|||Songs/Guns N' Roses - Welcome to the Jungle (Unknown Charter)")
        self.assertEqual(p["name"], "Welcome to the Jungle")


if __name__ == "__main__":
    unittest.main()
