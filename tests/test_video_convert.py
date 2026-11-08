"""VP8 WebM everywhere: encode arguments, one-pass padding, detecting MP4 videos, converting them in place
(folder and .sng), progress, and the hardware paths (all mocked: no GPU is needed)."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chcompanion import hwvideo, ingame, youtube
from chcompanion.jobs import Cancelled
from chcompanion.library import Song
from chcompanion.sng import open_sng, write_sng

from test_youtube_limits import Store


def fake_encoder(src, dst, pad=0.0, progress=None):
    Path(dst).write_bytes(b"WEBM-VP8" * 10)


def make_folder_song(root: Path, name="video.mp4", with_ini=True) -> Song:
    d = root / "Artist - Song"
    d.mkdir()
    (d / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 40)
    (d / "notes.chart").write_text("[Song]\n{\n}\n")
    if with_ini:
        (d / "song.ini").write_text("[song]\nname = Song\nartist = Artist\nvideo_start_time = 1500\n")
    return Song(d, False, {"name": "Song", "artist": "Artist", "video_start_time": "1500"})


def make_sng_song(root: Path, vname="video.mp4") -> Song:
    p = root / "Artist - Song.sng"
    write_sng(str(p), {"name": "Song", "artist": "Artist", "video_start_time": "1500"},
              {"notes.chart": b"[Song]\n{\n}\n", vname: b"\x00\x00\x00\x18ftypmp42" + b"x" * 40})
    return Song(p, True, {"name": "Song", "artist": "Artist", "video_start_time": "1500"})


class EncodeArgs(unittest.TestCase):
    def test_vp8_args_are_fast_capped_and_silent(self):
        a = hwvideo.vp8_args(threads=4)
        self.assertEqual(a[a.index("-c:v") + 1], "libvpx")
        self.assertEqual(a[a.index("-threads") + 1], "4")
        self.assertEqual(a[a.index("-deadline") + 1], "good")
        self.assertEqual(a[a.index("-cpu-used") + 1], "5")
        self.assertEqual(a[a.index("-slices") + 1], "4")      # token partitions: what lets libvpx VP8 use threads
        self.assertEqual(a[a.index("-lag-in-frames") + 1], "0")
        self.assertEqual(a[a.index("-map") + 1], "0:v:0")      # video only, no audio
        vf = a[a.index("-vf") + 1]
        self.assertIn("min(720,ih)", vf)
        self.assertNotIn("tpad", vf)
        self.assertNotIn("-r", a)                              # source fps is kept

    def test_padding_goes_into_the_same_filter_chain(self):
        vf = hwvideo.vp8_args(threads=2, pad=2.5)
        vf = vf[vf.index("-vf") + 1]
        self.assertTrue(vf.startswith("tpad=start_duration=2.500:color=black,"))

    def test_slots_and_threads_follow_the_cpu_count(self):
        for cpus, slots in ((1, 1), (2, 1), (8, 2), (16, 4), (64, 4)):
            with mock.patch("os.cpu_count", return_value=cpus):
                self.assertEqual(hwvideo.encode_slots(), slots)
                self.assertGreaterEqual(hwvideo.encode_threads(), 2)
                self.assertLessEqual(hwvideo.encode_threads(), 8)


class Progress(unittest.TestCase):
    def run_encode(self, lines, rc=0, progress=None, total=100.0):
        proc = mock.Mock()
        proc.stdout = io.StringIO("".join(lines))
        proc.wait.return_value = rc
        proc.poll.return_value = rc
        with mock.patch("chcompanion.process.popen", return_value=proc), tempfile.TemporaryDirectory() as d:
            ingame._encode("ff", Path(d) / "a.mp4", Path(d) / "o.webm", ["-c:v", "libvpx"], progress, total)

    def test_percent_comes_from_ffmpeg_progress(self):
        got = []
        self.run_encode(["frame=10\n", "out_time_us=25000000\n", "progress=continue\n", "out_time_us=50000000\n",
                         "out_time_us=50500000\n", "progress=end\n"], progress=lambda m: got.append(int(str(m).split()[-1].rstrip("%"))))
        self.assertEqual(got, [25, 50])

    def test_cancel_stops_ffmpeg(self):
        def cancel(_m):
            raise Cancelled()
        proc = mock.Mock()
        proc.stdout = io.StringIO("out_time_us=5000000\n")
        proc.poll.return_value = None
        with mock.patch("chcompanion.process.popen", return_value=proc), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Cancelled):
                ingame._encode("ff", Path(d) / "a", Path(d) / "o", [], cancel, 100.0)
        proc.terminate.assert_called_once()

    def test_failure_carries_ffmpeg_text(self):
        with self.assertRaises(ingame.InGameError) as cm:
            self.run_encode(["Error opening output\n"], rc=1)
        self.assertIn("Error opening output", str(cm.exception))


class SinglePassPadding(unittest.TestCase):
    def download(self, offset, vcodec="avc1", ext="mp4", height=720):
        store = Store()
        store.update_song("artist - song", video_offset=offset)
        tmp = tempfile.mkdtemp()
        enc = mock.Mock(side_effect=fake_encoder)

        def fetch(cand, dest, with_audio=False, progress=None):
            p = Path(dest + "." + ext)
            p.write_bytes(b"src")
            return str(p), {"vcodec": vcodec, "res_class": height}, None
        yt = SimpleNamespace(search=lambda s: [{"id": "v1", "score": 9.0}], fetch=fetch)
        sg = SimpleNamespace(key="artist - song", artist="Artist", name="Song", display="Artist - Song", length_s=200)
        with mock.patch.object(ingame, "cache_dir", return_value=Path(tmp)), \
                mock.patch.object(ingame, "cached_video", return_value=None), \
                mock.patch.object(ingame, "per_manual_offset", return_value=True), \
                mock.patch.object(ingame, "looks_static", return_value=False), \
                mock.patch.object(ingame, "video_res", return_value=height), \
                mock.patch.object(ingame, "encode_vp8", enc), \
                mock.patch.object(youtube, "GATE", youtube.RateGate(2, 0.0)):
            out = ingame.download_video(sg, yt, store)
        return out, enc

    def test_negative_offset_is_one_encode_with_the_padding_inside(self):
        out, enc = self.download(-2.5)
        self.assertEqual(enc.call_count, 1)                 # not encoded and then padded (re-encoded) again
        self.assertAlmostEqual(enc.call_args[0][2], 2.5)
        self.assertEqual(out.name, "video.webm")
        self.assertAlmostEqual(ingame.padded_seconds(out), 2.5)

    def test_no_offset_no_padding(self):
        out, enc = self.download(0.0)
        self.assertEqual(enc.call_count, 1)
        self.assertEqual(enc.call_args[0][2], 0.0)
        self.assertEqual(ingame.padded_seconds(out), 0.0)

    def test_existing_vp8_webm_is_used_without_encoding(self):
        out, enc = self.download(0.0, vcodec="vp8", ext="webm")
        enc.assert_not_called()
        self.assertEqual(out.suffix, ".webm")

    def test_vp8_that_needs_padding_is_encoded_once(self):
        _out, enc = self.download(-1.0, vcodec="vp8", ext="webm")
        self.assertEqual(enc.call_count, 1)

    def test_oversized_vp8_is_scaled_down(self):
        _out, enc = self.download(0.0, vcodec="vp8", ext="webm", height=1080)
        self.assertEqual(enc.call_count, 1)


class Detection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_mp4_in_a_folder_and_in_an_sng_need_converting(self):
        a = make_folder_song(self.tmp)
        b = make_sng_song(self.tmp)
        self.assertTrue(ingame.needs_vp8(a))
        self.assertTrue(ingame.needs_vp8(b))
        self.assertEqual(len(ingame.unplayable_songs([a, b])), 2)

    def test_webm_and_ogv_are_fine_and_so_is_no_video(self):
        t = Path(tempfile.mkdtemp())
        self.assertFalse(ingame.needs_vp8(make_folder_song(self.tmp, "video.webm")))
        d = t / "x"
        d.mkdir()
        (d / "notes.chart").write_text("x")
        self.assertFalse(ingame.needs_vp8(Song(d, False, {})))
        self.assertFalse(ingame.needs_vp8(make_sng_song(t, "video.webm")))

    def test_detection_does_not_depend_on_the_os(self):
        s = make_folder_song(self.tmp)
        for plat in ("linux", "darwin", "win32"):
            with mock.patch("sys.platform", plat):
                self.assertTrue(ingame.needs_vp8(s))


class ConvertInPlace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(ingame, "cache_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_folder_gets_video_webm_and_loses_the_mp4(self):
        s = make_folder_song(self.tmp)
        steps = []
        with mock.patch.object(ingame, "encode_vp8", side_effect=fake_encoder):
            name = ingame.convert_song_video(s, Store(), steps.append)
        self.assertEqual(name, "video.webm")
        self.assertTrue((s.path / "video.webm").is_file())
        self.assertFalse((s.path / "video.mp4").exists())
        self.assertIn("1500", (s.path / "song.ini").read_text())      # start time unchanged
        self.assertFalse(ingame.needs_vp8(s))
        self.assertEqual(steps[0].key, "ingame.converting_webm")      # errors are prefixed with this step

    def test_failed_encode_keeps_the_mp4(self):
        s = make_folder_song(self.tmp)
        with mock.patch.object(ingame, "encode_vp8", side_effect=ingame.InGameError("boom")):
            with self.assertRaises(ingame.InGameError):
                ingame.convert_song_video(s, Store())
        self.assertTrue((s.path / "video.mp4").exists())
        self.assertFalse((s.path / "video.webm").exists())

    def test_sng_is_repacked_with_a_backup(self):
        s = make_sng_song(self.tmp)
        before = s.path.read_bytes()
        with mock.patch.object(ingame, "encode_vp8", side_effect=fake_encoder):
            name = ingame.convert_song_video(s, Store())
        self.assertTrue(name.startswith("video.webm"))
        sng = open_sng(str(s.path))
        self.assertIn("video.webm", sng.files)
        self.assertNotIn("video.mp4", sng.files)
        self.assertEqual(sng.read("video.webm"), b"WEBM-VP8" * 10)
        self.assertEqual(sng.read("notes.chart"), b"[Song]\n{\n}\n")
        self.assertEqual(sng.metadata.get("video_start_time"), "1500")
        self.assertEqual(Path(str(s.path) + ingame.BACKUP).read_bytes(), before)

    def test_nothing_to_convert_returns_none(self):
        s = make_folder_song(self.tmp, "video.webm")
        self.assertIsNone(ingame.convert_song_video(s, Store()))


class Hardware(unittest.TestCase):
    ACCELS = "Hardware acceleration methods:\ncuda\nvaapi\nvideotoolbox\nd3d11va\n"
    ENCODERS = (" V....D libvpx               libvpx VP8 (codec vp8)\n"
                " V....D vp8_vaapi            VP8 (VAAPI) (codec vp8)\n A....D aac   AAC\n")

    def setUp(self):
        hwvideo.reset()
        self.addCleanup(hwvideo.reset)

    def test_probe_output_is_parsed(self):
        self.assertEqual(hwvideo.parse_hwaccels(self.ACCELS), {"cuda", "vaapi", "videotoolbox", "d3d11va"})
        self.assertEqual(hwvideo.parse_encoders(self.ENCODERS), {"libvpx", "vp8_vaapi"})
        self.assertEqual(hwvideo.parse_hwaccels("Hardware acceleration methods:\n"), set())

    def test_cpu_path_is_the_default(self):
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps()):
            self.assertEqual([p.key for p in hwvideo.plan()], ["cpu"])
        pre, post = hwvideo.build(hwvideo.CPU, 1280, 720)
        self.assertEqual(pre, [])
        self.assertIn("libvpx", post)

    def test_commands_per_path(self):
        cuda = hwvideo.HwPath("cuda", "NVIDIA", "", gpu_scale=True)
        pre, post = hwvideo.build(cuda, 1280, 720)
        self.assertEqual(pre, ["-hwaccel", "cuda"])                 # decode on the GPU, libvpx encodes
        self.assertIn("libvpx", post)
        pre, post = hwvideo.build(cuda, 1920, 1080)                 # taller than 720p: scale on the GPU too
        self.assertIn("scale_cuda=1280:720", post[post.index("-vf") + 1])
        self.assertIn("cuda", pre)
        pre, post = hwvideo.build(cuda, 1920, 1080, pad=2.0)        # padding is a CPU filter: no GPU scale
        self.assertNotIn("scale_cuda", " ".join(post))
        self.assertIn("tpad", post[post.index("-vf") + 1])
        vt = hwvideo.HwPath("videotoolbox", "VideoToolbox", "")
        self.assertEqual(hwvideo.build(vt, 1920, 1080)[0], ["-hwaccel", "videotoolbox"])
        va = hwvideo.HwPath("vaapi", "VAAPI", "", "/dev/dri/renderD128", gpu_scale=True)
        pre, post = hwvideo.build(va, 1920, 1080)
        self.assertIn("/dev/dri/renderD128", pre)
        self.assertIn("scale_vaapi=w=1280:h=720", post[post.index("-vf") + 1])
        enc = hwvideo.HwPath("vaapi_enc", "VAAPI", "VAAPI", "/dev/dri/renderD128", gpu_scale=True)
        pre, post = hwvideo.build(enc, 1280, 720)
        self.assertEqual(post[post.index("-c:v") + 1], "vp8_vaapi")
        pre, post = hwvideo.build(enc, 1280, 720, pad=1.0)
        self.assertIn("hwupload", post[post.index("-vf") + 1])

    def test_gpu_encoders_come_first_then_discrete_decode_then_cpu(self):
        paths = [hwvideo.HwPath("vaapi", "VAAPI", "", "/dev/dri/renderD129", rank=5),
                 hwvideo.HwPath("cuda", "NVIDIA", "", rank=1),
                 hwvideo.HwPath("vaapi_enc", "VAAPI", "VAAPI", "/dev/dri/renderD129", rank=-5)]
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps(paths)):
            self.assertEqual([p.key for p in hwvideo.plan()], ["vaapi_enc", "cuda", "vaapi", "cpu"])

    def test_probe_selftests_each_candidate_and_drops_the_broken_ones(self):
        calls = []

        def fake_try(ff, args, timeout=60):
            calls.append(args)
            return ("cuda" not in args), ""
        with mock.patch.object(hwvideo, "run_process") as rp, \
                mock.patch.object(hwvideo, "_try", side_effect=fake_try), \
                mock.patch("sys.platform", "win32"):
            rp.side_effect = [SimpleNamespace(stdout=self.ACCELS, stderr=""),
                              SimpleNamespace(stdout=self.ENCODERS, stderr="")]
            caps = hwvideo.probe("ffmpeg")
        self.assertEqual([p.key for p in caps.paths], ["d3d11va"])    # cuda failed its self-test

    def test_hardware_failure_falls_back_to_the_next_path_and_stays_off(self):
        paths = [hwvideo.HwPath("vaapi_enc", "VAAPI", "VAAPI", "/dev/dri/renderD128", rank=-5),
                 hwvideo.HwPath("cuda", "NVIDIA", "", rank=1)]
        used = []

        def enc(ff, src, dst, args, progress=None, total=0.0, pre=()):
            used.append(list(pre) + list(args))
            if "vp8_vaapi" in args or "cuda" in pre:
                raise ingame.InGameError("device lost")
            Path(dst).write_bytes(b"cpu")
        info = {"w": 1280, "h": 720, "kbps": 0, "secs": 10.0}
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps(paths)), \
                mock.patch.object(ingame, "_ffmpeg", return_value="ff"), \
                mock.patch.object(ingame, "media_info", return_value=info), \
                mock.patch.object(ingame, "_encode", side_effect=enc), \
                tempfile.TemporaryDirectory() as d:
            ingame.encode_vp8(Path(d) / "a.mp4", Path(d) / "o.webm")
            self.assertEqual((Path(d) / "o.webm").read_bytes(), b"cpu")
            self.assertEqual(len(used), 3)                                # vaapi_enc, cuda, cpu
            ingame.encode_vp8(Path(d) / "a.mp4", Path(d) / "o.webm")      # next job: broken paths are skipped
            self.assertEqual(len(used), 4)

    def test_gpu_made_file_must_verify_or_the_cpu_redoes_it(self):
        paths = [hwvideo.HwPath("vaapi_enc", "VAAPI", "VAAPI", "/dev/dri/renderD128", rank=-5)]

        def enc(ff, src, dst, args, progress=None, total=0.0, pre=()):
            Path(dst).write_bytes(b"gpu" if "vp8_vaapi" in args else b"cpu")
        info = {"w": 1280, "h": 720, "kbps": 0, "secs": 10.0}
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps(paths)), \
                mock.patch.object(ingame, "_ffmpeg", return_value="ff"), \
                mock.patch.object(ingame, "media_info", return_value=info), \
                mock.patch.object(ingame, "_encode", side_effect=enc), \
                mock.patch.object(hwvideo, "count_frames", return_value=300), \
                mock.patch.object(hwvideo, "codec_of", return_value="h264"), \
                tempfile.TemporaryDirectory() as d:
            ingame.encode_vp8(Path(d) / "a.mp4", Path(d) / "o.webm")
            self.assertEqual((Path(d) / "o.webm").read_bytes(), b"cpu")

    def test_verify_checks_codec_and_frame_count(self):
        with mock.patch.object(hwvideo, "codec_of", return_value="vp8"), \
                mock.patch.object(hwvideo, "count_frames", return_value=299):
            self.assertTrue(hwvideo.verify_output("ff", Path("x"), 300))
        with mock.patch.object(hwvideo, "codec_of", return_value="vp8"), \
                mock.patch.object(hwvideo, "count_frames", return_value=100):
            self.assertFalse(hwvideo.verify_output("ff", Path("x"), 300))

    def test_description_for_doctor_and_settings(self):
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps([hwvideo.HwPath("cuda", "NVIDIA", "")])):
            self.assertEqual(hwvideo.describe(), "Video conversion: NVIDIA decode + CPU VP8 encode")
        hwvideo.reset()
        with mock.patch.object(hwvideo, "probe", return_value=hwvideo.Caps()):
            self.assertIn("CPU only", hwvideo.describe())


if __name__ == "__main__":
    unittest.main()
