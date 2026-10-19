import struct
import unittest
from pathlib import Path

from chcompanion import lyrics
from chcompanion.logwatch import LogWatcher


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def meta(delta, mtype, payload):
    return vlq(delta) + bytes([0xFF, mtype]) + vlq(len(payload)) + payload


def track(events):
    body = b"".join(events) + meta(0, 0x2F, b"")
    return b"MTrk" + struct.pack(">I", len(body)) + body


def make_mid():
    res = 480
    t0 = track([meta(0, 0x03, b"tempo"), meta(0, 0x51, (500000).to_bytes(3, "big"))])  # 120 bpm
    ev = [meta(0, 0x03, b"PART VOCALS")]
    # phrase 105 from tick 0 to 1920, syllables "Hel-" "lo" "world" at beats 0,1,2
    ev.append(vlq(0) + bytes([0x90, 105, 100]))
    for i, (word) in enumerate([b"Hel-", b"lo", b"world"]):
        d = 0 if i == 0 else 480 - 240
        ev.append(meta(d, 0x05, word))
        ev.append(vlq(0) + bytes([0x90, 60, 100]))
        ev.append(vlq(240) + bytes([0x80, 60, 0]))
    ev.append(vlq(1920 - 1200) + bytes([0x80, 105, 0]))
    t1 = track(ev)
    head = b"MThd" + struct.pack(">IHHH", 6, 1, 2, res)
    return head + t0 + t1


class LyricsTests(unittest.TestCase):
    def test_midi(self):
        lines = lyrics.from_midi(make_mid())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "Hello world")
        s = lines[0]["syls"]
        self.assertAlmostEqual(s[1]["t"], 0.5, places=3)
        self.assertAlmostEqual(s[1]["e"], 0.75, places=3)

    def test_clean(self):
        self.assertEqual(lyrics._clean_syllable("Hel-"), ("Hel", True))
        self.assertEqual(lyrics._clean_syllable("know#"), ("know", False))
        self.assertEqual(lyrics._clean_syllable("[play]"), None)
        self.assertEqual(lyrics._clean_syllable("ro="), ("ro-", True))

    def test_lrc(self):
        lines = lyrics.parse_lrc("[00:01.00] One two\n[00:04.50]\n[00:05.00] three")
        self.assertEqual([l["text"] for l in lines], ["One two", "three"])
        self.assertAlmostEqual(lines[0]["start"], 1.0)

    def test_chart(self):
        chart = """[Song]\n{\n  Resolution = 192\n}\n[SyncTrack]\n{\n  0 = B 120000\n}\n[Events]\n{\n  0 = E "phrase_start"\n  192 = E "lyric Hey"\n  384 = E "lyric there"\n  768 = E "phrase_end"\n}\n"""
        lines = lyrics.from_chart(chart)
        self.assertEqual(lines[0]["text"], "Hey there")
        self.assertAlmostEqual(lines[0]["start"], 0.5)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.learned = []
        self.w = LogWatcher(Path("."), lambda s, why: self.events.append(why),
                            start_lead=lambda: 2.0, learn_lead=self.learned.append)

    def feed(self, line, t):
        self.w.handle_line(line, now=t)

    def test_menu_seeks_ignored(self):
        self.feed("[Log] Seeking to song time:51.3", 0)
        self.assertEqual(self.w.state.status, "idle")

    def test_play_pause_resume_quit(self):
        # Sequence observed in a real session (Clone Hero v1.1, offline)
        self.feed("[Log] Loading Song: Tame Impala - Loser", 100.0)
        st = self.w.state
        self.assertEqual(st.status, "playing")
        self.assertEqual(st.song, "Tame Impala - Loser")
        self.feed("[Log] OnCountdownStart", 101.0)
        self.feed("[Log] Seeking to song time:5.739", 109.0)   # pause
        self.assertEqual(st.status, "paused")
        self.assertAlmostEqual(st.now(), 5.739)
        self.assertAlmostEqual(self.learned[0], 109.0 - 5.739 - 100.0, places=3)
        self.feed("[Log] Seeking to song time:4.239", 115.0)   # resume (1.5 s rewind)
        self.assertEqual(st.status, "playing")
        self.assertAlmostEqual(st.anchor_time, 4.239)
        self.feed("[Log] OnCountdownEnd", 118.0)
        self.assertEqual(st.status, "playing")
        self.feed("[Log] Seeking to song time:16.84", 127.6)   # pause again
        self.assertEqual(st.status, "paused")
        self.feed("[Log] Seeking to song time:82.3", 130.0)    # menu preview => quit
        self.assertEqual(st.status, "idle")

    def test_restart_and_finish(self):
        self.feed("[Log] Loading Song: A - B", 0)
        self.feed("[Log] Seeking to song time:132.268", 135)
        self.feed("[Log] Loading Song: A - B", 136)
        self.assertEqual(self.w.state.status, "playing")
        self.feed("[Log] Replay input count: 674", 400)
        self.assertEqual(self.w.state.status, "ended")
        self.feed("[Log] Seeking to song time:69", 410)       # back in menus
        self.assertEqual(self.w.state.status, "ended")

    def test_online_session_pause(self):
        self.feed("[Log] Loading Song: A - B", 0)
        self.feed("[Log] Getting all scores from : https://api.clonehero.net/v1/scores/all-scores?trackHash=x&controllerType=5Fret&speed=150", 1)
        self.assertEqual(self.w.state.speed, 1.5)
        self.feed("[Log] Pausing session: abc== at time: 26.949", 20)
        self.feed("[Log] Seeking to song time:26.9825", 20.01)
        self.assertEqual(self.w.state.status, "paused")
        self.feed("[Log] Resuming session: abc==", 25)
        self.feed("[Log] Seeking to song time:25.48", 25.01)
        self.assertEqual(self.w.state.status, "playing")
        self.assertAlmostEqual(self.w.state.rate, 1.5)


if __name__ == "__main__":
    unittest.main()
