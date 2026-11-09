"""Internationalisation: catalogs, plural rules, backend messages, job re-translation, and the UI sources."""
import html.parser
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import i18n_check  # noqa: E402
import i18n_lint  # noqa: E402
from chcompanion import i18n  # noqa: E402
from chcompanion.i18n import _  # noqa: E402
from chcompanion.jobs import Job, JobManager  # noqa: E402

WEB = ROOT / "chcompanion" / "web"
EN = json.loads((WEB / "i18n" / "en.json").read_text("utf-8"))


class FakeCatalog(unittest.TestCase):
    """Runs with a made-up 'de' catalog so these tests do not depend on the shipped translations."""

    def setUp(self):
        self._saved = (i18n.LANG, dict(i18n._cache))
        i18n._cache["de"] = {
            "job.done": "fertig",
            "job.error_in_step": "{step}: {detail}",
            "job.installed.one": "{count} Chart installiert",
            "job.installed.other": "{count} Charts installiert",
            "job.starting": "startet",
            "install.connecting": "verbinde mit dem Chart-Download",
        }
        i18n._cache["ru"] = {
            "job.installed.one": "установлен {count} чарт", "job.installed.few": "установлено {count} чарта",
            "job.installed.many": "установлено {count} чартов", "job.installed.other": "установлено {count} чарта",
        }
        i18n.set_lang("en")

    def tearDown(self):
        i18n.LANG = self._saved[0]
        i18n._cache.clear()
        i18n._cache.update(self._saved[1])


class CatalogTests(unittest.TestCase):
    def test_english_catalog_is_valid(self):
        errors, _notes = i18n_check.check("en", strict=True)
        self.assertEqual(errors, [])
        self.assertGreater(len(EN), 500)

    def test_every_language_file_exists_and_is_valid_json(self):
        for lang in i18n.LANGUAGES:
            with self.subTest(lang=lang):
                data = json.loads((WEB / "i18n" / f"{lang}.json").read_text("utf-8"))
                self.assertIsInstance(data, dict)

    def test_translated_catalogs_are_complete(self):
        """Keys, placeholders and plural forms match en.json. A catalog that is still {} is skipped."""
        checked = 0
        for lang in i18n.LANGUAGES:
            if lang == "en":
                continue
            with self.subTest(lang=lang):
                data = json.loads((WEB / "i18n" / f"{lang}.json").read_text("utf-8"))
                if not data:
                    continue                  # not translated yet: the app falls back to English
                checked += 1
                errors, _notes = i18n_check.check(lang, strict=True)
                self.assertEqual(errors, [])

    def test_checker_flags_problems(self):
        base = {"a": "Hello {name}", "n.one": "{count} file", "n.other": "{count} files", "h": "<b>x</b>"}
        with mock.patch.object(i18n_check, "load", side_effect=lambda p: base if p.name == "en.json" else
                               {"a": "Hallo {nme}", "n.other": "{count} Dateien", "h": "x", "extra": "y"}):
            errors, _n = i18n_check.check("de")
        text = "\n".join(errors)
        self.assertIn("placeholders", text)
        self.assertIn("missing plural form .one", text)
        self.assertIn("unexpected key: extra", text)
        self.assertIn("HTML tags differ", text)

    def test_plural_categories(self):
        pc = i18n.plural_category
        self.assertEqual([pc("en", n) for n in (0, 1, 2)], ["other", "one", "other"])
        self.assertEqual([pc("ru", n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 101, 111)],
                         ["one", "few", "few", "many", "many", "many", "one", "few", "many", "one", "many"])
        self.assertEqual([pc("fr", n) for n in (0, 1, 2)], ["one", "one", "other"])
        self.assertEqual([pc("pt-BR", n) for n in (0, 1, 2)], ["one", "one", "other"])
        for lang in ("ja", "ko", "zh-CN"):
            self.assertEqual(pc(lang, 1), "other")
        self.assertEqual(set(i18n.PLURAL_FORMS), set(i18n.LANGUAGES))

    def test_language_resolution(self):
        r = i18n.resolve
        self.assertEqual(r("auto", ["pt_BR.UTF-8"]), "pt-BR")
        self.assertEqual(r("", ["xx", "ko-KR", "en"]), "ko")
        self.assertEqual(r("auto", ["zh-Hans-CN"]), "zh-CN")
        self.assertEqual(r("auto", ["zh-TW", "fr"]), "fr")      # Traditional Chinese is not shipped: next preference
        self.assertEqual(r("auto", ["xx"]), "en")
        self.assertEqual(r("de", ["fr"]), "de")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_all_nine_catalogs(self):
        import tempfile
        from types import SimpleNamespace
        from chcompanion import cli
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.print") as pr:
            cli.cmd_doctor(SimpleNamespace(data_dir=tmp, songs_dir=None, report=None))
        self.assertIn("\nLanguages   : 9 catalogs\n", pr.call_args[0][0] + "\n")


class BackendTests(FakeCatalog):
    def test_missing_keys_fall_back_to_english_then_to_the_key(self):
        i18n.set_lang("de")
        self.assertEqual(str(_("job.done")), "fertig")
        self.assertEqual(str(_("dl.mega")), EN["dl.mega"])                  # not in the fake de catalog
        self.assertEqual(str(_("no.such.key")), "no.such.key")
        i18n._cache["fr"] = {}                                              # a language with an empty catalog
        i18n.set_lang("fr")
        self.assertEqual(str(_("job.done")), EN["job.done"])

    def test_message_translated_per_language_with_plurals(self):
        self.assertEqual(i18n.translate("job.installed", "en", count=1), "installed 1 chart")
        self.assertEqual(i18n.translate("job.installed", "de", count=2), "2 Charts installiert")
        self.assertEqual([i18n.translate("job.installed", "ru", count=n) for n in (1, 2, 5)],
                         ["установлен 1 чарт", "установлено 2 чарта", "установлено 5 чартов"])

    def test_placeholders_left_alone_when_not_supplied(self):
        self.assertEqual(i18n.translate("dlg.rename_text"), "Use {artist}, {name}, {charter}, {album}, {year}.")

    def test_msg_is_a_str_and_keeps_key_and_vars(self):
        m = _("job.installed", count=3)
        self.assertIsInstance(m, str)
        self.assertEqual((m.key, m.vars), ("job.installed", {"count": 3}))
        self.assertEqual(m, "installed 3 charts")

    def test_deep_render_translates_nested_structures_in_the_active_language(self):
        data = {"a": _("job.done"), "b": [_("job.starting"), "plain"], "c": {"d": _("job.installed", count=1)}}
        i18n.set_lang("de")
        out = i18n.deep_render(data)
        self.assertEqual(out, {"a": "fertig", "b": ["startet", "plain"], "c": {"d": "1 Chart installiert"}})
        json.dumps(out)

    def test_join_and_nested_messages(self):
        m = i18n.join([_("job.done"), "x"], "; ")
        i18n.set_lang("de")
        self.assertEqual(i18n.render(m), "fertig; x")

    def test_exceptions_keep_their_message_key(self):
        e = RuntimeError(_("job.done"))
        self.assertEqual(i18n.msg_of(e).key, "job.done")
        self.assertEqual(i18n.msg_of(ValueError()), "ValueError")


class JobTranslationTests(FakeCatalog):
    def _run(self, runner, title="Artist - Song"):
        job = Job("download", title, status="running")
        m = JobManager(runner)
        m.active = 1
        with mock.patch.object(m, "_next", side_effect=[job, None]), self.assertLogs("chcompanion.jobs", level="ERROR"):
            m._worker()
        return m, job

    def test_job_result_retranslates_when_the_language_changes(self):
        m = JobManager(lambda j, p: None)
        job = Job("download", "Artist - Song", status="done", result=_("job.installed", count=2))
        m.jobs[job.id] = job
        self.assertEqual(i18n.deep_render(m.snapshot())["jobs"][0]["result"], "installed 2 charts")
        i18n.set_lang("de")
        self.assertEqual(i18n.deep_render(m.snapshot())["jobs"][0]["result"], "2 Charts installiert")
        i18n.set_lang("en")
        self.assertEqual(i18n.deep_render(m.snapshot())["jobs"][0]["result"], "installed 2 charts")

    def test_failed_job_error_keeps_the_step_prefix_in_every_language(self):
        def runner(job, progress):
            progress(_("install.connecting"))
            raise RuntimeError("HTTP Error 404: Not Found")      # third-party text stays as it is

        m, job = self._run(runner)
        self.assertEqual(job.status, "failed")
        self.assertEqual(i18n.render(job.error, "en"), "connecting to the chart download: HTTP Error 404: Not Found")
        self.assertEqual(i18n.render(job.error, "de"), "verbinde mit dem Chart-Download: HTTP Error 404: Not Found")

    def test_translated_runner_error_inside_a_step(self):
        def runner(job, progress):
            progress(_("install.connecting"))
            raise RuntimeError(_("job.done"))

        _m, job = self._run(runner)
        self.assertEqual(i18n.render(job.error, "de"), "verbinde mit dem Chart-Download: fertig")

    def test_waiting_steps_are_keyed_not_matched_by_text(self):
        m = JobManager(lambda j, p: None)
        seen = []

        def runner(job, progress):
            progress(_("job.wait_cooldown", time="0:05"))
            progress(_("install.connecting"))
            raise RuntimeError("boom")

        _m, job = self._run(runner)
        self.assertEqual(job.work_step.key, "install.connecting")   # the cool-down notice is not "the last real step"


class UiSourceTests(unittest.TestCase):
    def test_every_static_key_in_the_html_exists(self):
        missing = []
        for name in ("app.html", "index.html"):
            src = (WEB / name).read_text("utf-8")
            for m in re.finditer(r'data-(?:i18n(?:-html|-title|-placeholder|-aria)?|title-key)="([^"]+)"', src):
                if m.group(1) not in EN:
                    missing.append((name, m.group(1)))
        self.assertEqual(missing, [])

    def test_node_check_of_html_keys_and_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r"""
const fs = require("fs");
const en = JSON.parse(fs.readFileSync(process.argv[1] + "/i18n/en.json", "utf8"));
const bad = [];
for (const f of ["app.html", "index.html"]) {
  const src = fs.readFileSync(process.argv[1] + "/" + f, "utf8");
  for (const m of src.matchAll(/data-(?:i18n(?:-html|-title|-placeholder|-aria)?|title-key)="([^"]+)"/g)) if (!(m[1] in en)) bad.push(f + ": " + m[1]);
  for (const m of src.matchAll(/\btr\("([a-z0-9_.]+)"\s*[,)]/g)) { const k = m[1]; if (!(k in en) && !(k + ".other" in en)) bad.push(f + " tr(): " + k); }
}
for (const f of ["app.js", "i18n.js"]) {
  const src = fs.readFileSync(process.argv[1] + "/" + f, "utf8");
  for (const m of src.matchAll(/\btr\("([a-z0-9_.]+)"\s*[,)]/g)) { const k = m[1]; if (!(k in en) && !(k + ".other" in en)) bad.push(f + " tr(): " + k); }
}
if (bad.length) { console.error(bad.join("\n")); process.exit(1); }
"""
        r = subprocess.run([node, "-e", script, str(WEB)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in ("app.js", "i18n.js"):
            r = subprocess.run([node, "--check", str(WEB / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_i18n_js_runtime_matches_the_python_rules(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r"""
global.window = {}; global.location = { search: "" }; Object.defineProperty(globalThis, "navigator", { value: { languages: ["ru-RU"] }, configurable: true });
global.localStorage = { getItem() { return null; } }; global.document = { documentElement: { dataset: {} } };
require(process.argv[1] + "/i18n.js");
const I = window.I18N; const out = {};
out.norm = ["pt_BR", "zh-Hans-CN", "zh-TW", "ko-KR", "xx"].map(I.normalize);
out.detect = I.detect();
process.stdout.write(JSON.stringify(out));
"""
        r = subprocess.run([node, "-e", script, str(WEB)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["norm"], [i18n.normalize(x) for x in ("pt_BR", "zh-Hans-CN", "zh-TW", "ko-KR", "xx")])
        self.assertEqual(got["detect"], "ru")

    def test_python_keys_exist_in_english(self):
        missing = []
        for py in (ROOT / "chcompanion").rglob("*.py"):
            if py.name == "i18n.py":
                continue
            for m in re.finditer(r"""(?<![\w.])_\(\s*["']([a-z0-9_.]+)["']""", py.read_text("utf-8")):
                k = m.group(1)
                if not k.endswith(".") and k not in EN and k + ".other" not in EN:
                    missing.append((py.name, k))
        self.assertEqual(missing, [])

    def test_no_new_hardcoded_english_in_the_ui(self):
        found = {k: v for k, v in i18n_lint.lint_all().items() if v}
        self.assertEqual(found, {}, "user-visible English must go through tr()/data-i18n (or add `// i18n-ok`)")

    def test_lint_flags_hardcoded_text(self):
        tmp = WEB / ".lint-sample.js"
        tmp.write_text('toast("Hello world");\nel.textContent = `Saving files`;\nx.innerHTML = \'<p class="hint">Nothing here.</p>\';\n'
                       'toast(tr("ok.key"));\nels.textContent = "×";\n', "utf-8")
        try:
            lines = [ln for ln, _t in i18n_lint.lint_js(tmp)]
        finally:
            tmp.unlink()
        self.assertEqual(lines, [1, 2, 3])

    def test_catalogs_are_shipped_with_the_web_folder(self):
        wf = (ROOT / ".github" / "workflows" / "build.yml").read_text("utf-8")
        adds = re.findall(r'--add-data "chcompanion/web:chcompanion/web"', wf)
        self.assertGreaterEqual(len(adds), 3)          # macOS, Windows and Linux bundles copy the whole folder, i18n/ included
        for lang in i18n.LANGUAGES:
            self.assertTrue((WEB / "i18n" / f"{lang}.json").is_file())
        self.assertTrue((WEB / "i18n" / "GLOSSARY.md").is_file())


if __name__ == "__main__":
    unittest.main()


def test_no_template_placeholders_in_plain_string_literals():
    """A '${...}' inside a quoted (non-template) JS string is shown literally in the UI."""
    import re, pathlib
    web = pathlib.Path(__file__).resolve().parent.parent / "chcompanion" / "web"
    bad = []
    for f in ["app.js", "update.js", "i18n.js"]:
        src = (web / f).read_text(encoding="utf-8")
        # single- or double-quoted string on one line that contains ${...} and isn't inside a template literal
        for n, line in enumerate(src.splitlines(), 1):
            for m in re.finditer(r"""(?<![\w`])'([^'\n`]*\$\{[^}\n]+\}[^'\n`]*)'""", line):
                before = line[:m.start()]
                if before.count("`") % 2 == 0 and "<" in m.group(1):
                    bad.append(f"{f}:{n}: {m.group(1)[:80]}")
    assert not bad, "template placeholders in plain strings:\n" + "\n".join(bad)
