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


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PollJobsRefreshTests(unittest.TestCase):
    def test_finished_jobs_reload_library_and_cloud_box(self):
        fn = re.search(r"function jobsNewlyFinished\(.*?\n}\n", JS, re.S).group(0)
        poll = re.search(r"async function pollJobs\(.*?\n}\n", JS, re.S).group(0)
        script = "let jobsSnap = null, jobsSeen = null;" + fn + poll + r"""
const calls = []; let snaps = [];
const api = async () => snaps.shift();
const loadLibrary = () => calls.push("lib"), loadCloud = () => calls.push("cloud");
const renderDock = () => {}, renderJobs = () => {};
const currentView = "library", activeId = null, detail = null;
const $ = () => ({ classList: { toggle() {} } });
const J = (id, status) => ({ id, status });
(async () => {
  snaps = [{ jobs: [J("a", "running")] }]; await pollJobs(true);          // first poll seeds
  snaps = [{ jobs: [J("a", "running")] }]; await pollJobs(true);          // nothing finished
  snaps = [{ jobs: [J("a", "done")] }]; await pollJobs(true);             // a finished
  snaps = [{ jobs: [J("a", "done")] }]; await pollJobs(true);             // nothing new
  console.log(JSON.stringify(calls));
})();
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), '["lib","cloud"]')
