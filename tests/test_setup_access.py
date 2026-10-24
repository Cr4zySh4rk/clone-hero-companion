"""Setup shows blocked folders and never saves a false successful setup."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import api, paths


class SetupAccessTests(unittest.TestCase):
    def test_readable_but_blocked_folder_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "ensure_writable_dir", side_effect=RuntimeError("Blocked by Defender")):
                result = paths.describe_dir(tmp, "songs", check_write=True)
            self.assertTrue(result["ok"])
            self.assertFalse(result["writable"])
            self.assertEqual(result["writeError"], "Blocked by Defender")

    def test_read_only_discovery_does_not_probe_writes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "ensure_writable_dir") as probe:
            paths.describe_dir(tmp, "songs")
            probe.assert_not_called()

    def test_setup_rejects_blocked_location_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = types.SimpleNamespace(data_dir=root, libmgr=None, update_settings=mock.Mock(), reconfigure=mock.Mock())
            h = types.SimpleNamespace(companion=c, _json=mock.Mock())
            with mock.patch.object(paths, "ensure_writable_dir", side_effect=RuntimeError("Blocked by Defender")):
                api.handle(h, "POST", "/api/setup", {}, {"dataDir": tmp, "songDirs": [tmp]})
            self.assertEqual(h._json.call_args.args, ({"error": "Blocked by Defender"}, 400))
            c.update_settings.assert_not_called()
            c.reconfigure.assert_not_called()

    def test_setup_accepts_writable_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = types.SimpleNamespace(data_dir=root, libmgr=None, update_settings=mock.Mock(), reconfigure=mock.Mock())
            h = types.SimpleNamespace(companion=c, _json=mock.Mock())
            api.handle(h, "POST", "/api/setup", {}, {"dataDir": tmp, "songDirs": [tmp]})
            self.assertTrue(h._json.call_args.args[0]["ok"])
            c.update_settings.assert_called_once()
            c.reconfigure.assert_called_once()
