"""LIVE check (not a unit test): resolve the favourites from the 'not on this machine' screenshot against the
real Chorus Encore / RhythmVerse APIs and print what each resolves to.  python3 scripts/live_check_favs.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chcompanion import cloud  # noqa: E402

CASES = [
    ("Knockin' On Heaven's Door (Bob Dylan Cover)", "Guns N' Roses", "guitarzero132"),
    ("You Shook Me All Night Long (Live)", "ACDC", "harmonix"),
    ("Da Funk", "Daft Punk", "justin"),
    ("Welcome to the Jungle", "Guns N' Roses", ""),
    ("Sweet Child o' Mine", "Guns N' Roses", "harmonix"),
    ("Ace of Spades", "Motörhead", "harmonix"),
    ("Cochise", "Audioslave", "harmonix"),
    ("Iron Man", "Black Sabbath", "harmonix"),
    ("War Pigs", "Black Sabbath", "harmonix"),
    ("The Metal", "Tenacious D", "neversoft"),
    ("Paint It Black", "The Rolling Stones", "neversoft"),
    ("Bad Reputation", "Thin Lizzy", "harmonix"),
]

for name, artist, charter in CASES:
    row = {"song_key": "x", "name": name, "artist": artist, "charter": charter, "checksum": "", "source": {}}
    try:
        r = cloud.resolve_favourite(row)
    except Exception as e:
        r = None
        print(f"{name} / {artist} / {charter or '(none)'}\n    ERROR {type(e).__name__}: {e}")
        continue
    if r:
        print(f"{name} / {artist} / {charter or '(none)'}\n    -> [{r['source']}] {r['name']} / {r['artist']} / {r['charter']}"
              f"{'  (guess)' if r.get('guess') else ''}")
    else:
        print(f"{name} / {artist} / {charter or '(none)'}\n    -> NOT FOUND")
