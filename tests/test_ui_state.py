"""Sidebar -> library filter mapping in web/app.js (Library always shows All, Favourites the fav filter)."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).parent.parent / "chcompanion" / "web" / "app.js").read_text("utf-8")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ViewFilterTests(unittest.TestCase):
    def run_js(self, expr):
        fn = re.search(r"function viewFilter\(.*?\n", JS).group(0)
        out = subprocess.run(["node", "-e", fn + f"console.log(JSON.stringify({expr}))"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_mapping(self):
        self.assertEqual(self.run_js('[viewFilter("favs","all"), viewFilter("library","fav"), '
                                     'viewFilter("library","novideo"), viewFilter("jobs","fav")]'),
                         '["fav","all","all","fav"]')

    def test_late_responses_are_dropped(self):
        self.assertIn("if (gen !== libGen) return;", JS)
        self.assertIn("if (gen !== cloudGen)", JS)
