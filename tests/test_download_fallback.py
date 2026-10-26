"""URL normalisation, Google Drive handling, and falling back through ranked candidates (HTTP mocked)."""
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from chcompanion import cloud
from chcompanion.sources import fallback, hosts, rhythmverse

QUOTA = b"<html><head><title>Google Drive - Quota exceeded</title></head><body>Sorry, quota exceeded</body></html>"
PRIVATE = b"<html><body>You need access. Request access, or switch to an account with access. Sign in</body></html>"
FORM = (b'<html><body><form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
        b'<input type="hidden" name="id" value="FILEID123456"><input type="hidden" name="export" value="download">'
        b'<input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="u-u-i-d">'
        b'<input type="submit" value="Download anyway"></form>We can\'t scan this file for viruses</body></html>')


class Resp(io.BytesIO):
    def __init__(self, body, url, ctype="application/octet-stream", disp=None):
        super().__init__(body)
        self._url = url
        self.headers = {"Content-Type": ctype, **({"Content-Disposition": disp} if disp else {})}

    def geturl(self):
        return self._url


class Web:
    """urlopen(Request) stand-in: routes[url prefix] -> bytes | Resp factory | int (HTTP error status)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=0):
        url = req.full_url
        self.calls.append(url)
        for prefix, v in self.routes.items():
            if url.startswith(prefix):
                if isinstance(v, int):
                    raise urllib.error.HTTPError(url, v, "Not Found" if v == 404 else "Error", {}, None)
                if isinstance(v, bytes) and v.startswith(b"<"):
                    return Resp(v, url, "text/html; charset=utf-8")
                return Resp(v, url, disp='attachment; filename="chart.zip"') if isinstance(v, bytes) else v(url)
        raise AssertionError("unexpected request " + url)


def item(source, url, **kw):
    return {"source": source, "downloadUrl": url, "artist": "Nickelback", "name": "Rockstar", "charter": "Harmonix",
            "pageUrl": kw.pop("pageUrl", None), **kw}


DRIVE = "https://drive.google.com/file/d/1zLapyI0g7Ec9pwYh_r5W2P00hECeB-GA"
DRIVE_DL = "https://drive.usercontent.google.com/download?id=1zLapyI0g7Ec9pwYh_r5W2P00hECeB-GA"


class NormalizeTests(unittest.TestCase):
    def test_cases(self):
        n = hosts.normalize_url
        # real-shaped values from the RhythmVerse API
        self.assertEqual(n("/download_file/chaotic-bean/b0fc97e022814f618f2a69a5aae7b67a/Germany (Deutschland).7z"),
                         "https://rhythmverse.co/download_file/chaotic-bean/b0fc97e022814f618f2a69a5aae7b67a/Germany%20(Deutschland).7z")
        self.assertEqual(n("/download_file/thomas2oo80k-thomas2oo80k/52a9/LISA - Rockstar - Extended (thomas20080k).zip"),
                         "https://rhythmverse.co/download_file/thomas2oo80k-thomas2oo80k/52a9/LISA%20-%20Rockstar%20-%20Extended%20(thomas20080k).zip")
        self.assertEqual(n("/download/d1435a66d75e42d8af2dadc9173ffec2"), "https://rhythmverse.co/download/d1435a66d75e42d8af2dadc9173ffec2")
        self.assertEqual(n("download/abc"), "https://rhythmverse.co/download/abc")
        self.assertEqual(n("rhythmverse.co/download/abc"), "https://rhythmverse.co/download/abc")
        self.assertEqual(n("//files.enchor.us/abc.sng"), "https://files.enchor.us/abc.sng")
        self.assertEqual(n("https://drive.google.com/drive/u/0/folders/1oyPxqpJQ0XYunLobtOFgQaTjZP2pht1q"),
                         "https://drive.google.com/drive/u/0/folders/1oyPxqpJQ0XYunLobtOFgQaTjZP2pht1q")

    def test_whitespace_crlf_and_backslashes(self):
        n = hosts.normalize_url
        self.assertEqual(n("  https://files.enchor.us/abc.sng\r\n"), "https://files.enchor.us/abc.sng")
        self.assertEqual(n("/download_file/u/id/Some\r\nSong.zip\r"), "https://rhythmverse.co/download_file/u/id/SomeSong.zip")
        self.assertEqual(n("\\download_file\\user\\id\\My Song.7z"), "https://rhythmverse.co/download_file/user/id/My%20Song.7z")
        self.assertEqual(n("https://rhythmverse.co\\download\\abc\t"), "https://rhythmverse.co/download/abc")

    def test_no_double_encoding_and_query(self):
        n = hosts.normalize_url
        once = n("/download_file/u/id/A B [x] é.zip?x=1 2&y=%41")
        self.assertEqual(once, "https://rhythmverse.co/download_file/u/id/A%20B%20%5Bx%5D%20%C3%A9.zip?x=1%202&y=%41")
        self.assertEqual(n(once), once)
        self.assertEqual(n("/a/100%.zip"), "https://rhythmverse.co/a/100%25.zip")

    def test_rhythmverse_normalize_uses_it(self):
        s = rhythmverse.normalize({"data": {"title": "Germany"}, "file": {
            "download_url": "/download_file/chaotic-bean/b0fc/Germany (Deutschland).7z", "download_page_url": "/download/b0fc",
            "file_id": "1"}})
        self.assertEqual(s["downloadUrl"], "https://rhythmverse.co/download_file/chaotic-bean/b0fc/Germany%20(Deutschland).7z")
        self.assertEqual(s["pageUrl"], "https://rhythmverse.co/download/b0fc")

    def test_control_characters_no_longer_reach_urllib(self):
        web = Web({"https://rhythmverse.co/download_file/": b"PK\x03\x04data"})
        with mock.patch.object(hosts, "urlopen", web), tempfile.TemporaryDirectory() as d:
            files = hosts.download("/download_file/chaotic-bean/id/Germany (Deutschland).7z\r\n", Path(d))
            self.assertEqual(files[0].read_bytes(), b"PK\x03\x04data")
        self.assertEqual(web.calls, ["https://rhythmverse.co/download_file/chaotic-bean/id/Germany%20(Deutschland).7z"])


class DriveTests(unittest.TestCase):
    def dl(self, web):
        with mock.patch.object(hosts, "urlopen", web), tempfile.TemporaryDirectory() as d:
            return [f.read_bytes() for f in hosts.download(DRIVE, Path(d))]

    def test_confirm_token_form_is_followed(self):
        web = Web({DRIVE_DL: lambda u: Resp(FORM, u, "text/html") if "uuid" not in u else Resp(b"Rar!data", u, disp='attachment; filename="a.rar"'),
                   "https://drive.usercontent.google.com/download?id=FILEID": lambda u: Resp(b"Rar!data", u, disp='attachment; filename="a.rar"')})
        self.assertEqual(self.dl(web), [b"Rar!data"])
        self.assertIn("confirm=t", web.calls[0])
        self.assertTrue(web.calls[1].startswith("https://drive.usercontent.google.com/download?"))
        self.assertIn("uuid=u-u-i-d", web.calls[1])
        self.assertIn("id=FILEID123456", web.calls[1])

    def test_quota_and_private_are_classified_with_host(self):
        for page, kind, text in ((QUOTA, "quota", "quota exceeded"), (PRIVATE, "private", "private")):
            with self.assertRaises(hosts.DownloadError) as cm:
                self.dl(Web({DRIVE_DL: page}))
            self.assertEqual(cm.exception.kind, kind)
            self.assertIn(text, str(cm.exception))
            self.assertIn("drive.usercontent.google.com", str(cm.exception))

    def test_folder_links_with_user_segment(self):
        self.assertEqual(hosts.drive_folder_id("https://drive.google.com/drive/u/0/folders/1oyPxqpJQ0XYunLobtOFgQaTjZP2pht1q?usp=sharing"),
                         "1oyPxqpJQ0XYunLobtOFgQaTjZP2pht1q")
        self.assertEqual(hosts.drive_id(DRIVE), "1zLapyI0g7Ec9pwYh_r5W2P00hECeB-GA")

    def test_http_errors_name_the_host(self):
        with self.assertRaises(hosts.DownloadError) as cm:
            self.dl(Web({DRIVE_DL: 404}))
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("404 Not Found", str(cm.exception))
        self.assertIn("drive.usercontent.google.com", str(cm.exception))


class FallbackTests(unittest.TestCase):
    def run_chain(self, items, web, more=None):
        def do(it):
            with tempfile.TemporaryDirectory() as d:
                return [f.name for f in hosts.download(it["downloadUrl"], Path(d))]
        with mock.patch.object(hosts, "urlopen", web):
            return fallback.download_first_working(items, do, more)

    def test_drive_quota_falls_back_to_encore(self):
        rv = item("rhythmverse", DRIVE, pageUrl="https://rhythmverse.co/download/d14")
        enc = item("encore", "https://files.enchor.us/abc.sng")
        web = Web({DRIVE_DL: QUOTA, "https://files.enchor.us/abc.sng": b"SNG"})
        calls = []
        got, used = self.run_chain(rv, web, more=lambda tried: calls.append(set(tried)) or [enc])
        self.assertEqual(used["source"], "encore")
        self.assertEqual(got, ["chart.zip"])
        self.assertEqual(len(calls), 1)

    def test_recorded_404_re_resolves_fresh(self):
        row = {"artist": "Nickelback", "name": "Rockstar", "charter": "Harmonix", "source": {"encore_md5": "dead" * 8}}
        recorded = cloud.recorded_candidate(row)
        self.assertEqual(recorded["downloadUrl"], "https://files.enchor.us/" + "dead" * 8 + ".sng")
        fresh = {"source": "encore", "name": "Rockstar", "artist": "Nickelback", "charter": "Harmonix",
                 "downloadUrl": "https://files.enchor.us/beef.sng", "md5": "beef"}
        other = {**fresh, "charter": "Someone Else", "downloadUrl": "https://files.enchor.us/bad.sng", "md5": "bad"}
        search = lambda q, per_page=50: {"songs": [other, fresh]}
        web = Web({"https://files.enchor.us/" + "dead" * 8: 404, "https://files.enchor.us/beef.sng": b"SNG"})
        seen = []

        def more(tried):
            seen.append(tried)
            return cloud.resolve_candidates(row, search, lambda q, per_page=50: {"songs": []}, skip_urls=tried)
        got, used = self.run_chain(recorded, web, more)
        self.assertEqual(used["downloadUrl"], "https://files.enchor.us/beef.sng")     # never the other charter
        self.assertNotIn("https://files.enchor.us/bad.sng", web.calls)
        self.assertIn("https://files.enchor.us/" + "dead" * 8 + ".sng", seen[0])
        self.assertEqual(web.calls.count("https://files.enchor.us/" + "dead" * 8 + ".sng"), 1)

    def test_all_fail_combined_message(self):
        rv = item("rhythmverse", DRIVE, pageUrl="https://rhythmverse.co/download/d14")
        enc = item("encore", "https://files.enchor.us/abc.sng", pageUrl="https://www.enchor.us/chart/abc")
        web = Web({DRIVE_DL: QUOTA, "https://files.enchor.us/abc.sng": 404})
        with self.assertRaises(fallback.AllFailed) as cm:
            self.run_chain(rv, web, more=lambda tried: [enc])
        msg = str(cm.exception)
        self.assertIn("Couldn't download: Google Drive quota exceeded (RhythmVerse, drive.usercontent.google.com), "
                      "404 Not Found (Encore, files.enchor.us).", msg)
        self.assertIn("Try again later, or download it manually from https://rhythmverse.co/download/d14", msg)

    def test_folder_errors_do_not_fall_back(self):
        def do(it):
            raise RuntimeError('Cannot write to folder "X". nope')
        with self.assertRaises(RuntimeError) as cm:
            fallback.download_first_working(item("encore", "u"), do, lambda t: [item("encore", "v")])
        self.assertIn("Cannot write", str(cm.exception))


class CandidateOrderTests(unittest.TestCase):
    def test_encore_then_rhythmverse_then_rest_same_charter_only(self):
        row = {"artist": "Nickelback", "name": "Rockstar", "charter": "Harmonix", "source": {}}
        e = lambda md5, c="Harmonix": {"source": "encore", "id": "encore:" + md5, "md5": md5, "name": "Rockstar",
                                       "artist": "Nickelback", "charter": c, "downloadUrl": f"https://files.enchor.us/{md5}.sng"}
        r = lambda i, c="Harmonix": {"source": "rhythmverse", "id": f"rv:{i}", "name": "Rockstar", "artist": "Nickelback",
                                     "charter": c, "downloadUrl": f"https://rhythmverse.co/download_file/u/{i}/A B.zip", "hashes": []}
        got = cloud.resolve_candidates(row, lambda q, per_page=50: {"songs": [e("1"), e("2"), e("3", "Other")]},
                                       lambda q, per_page=50: {"songs": [r(1), r(2, "Other")]})
        self.assertEqual([c["id"] for c in got], ["encore:1", "rv:1", "encore:2"])
