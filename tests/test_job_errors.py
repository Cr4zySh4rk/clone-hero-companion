"""Failures retain the operation and full traceback for remote diagnostics."""
import unittest
from unittest import mock

from chcompanion.jobs import Job, JobManager


class JobErrorTests(unittest.TestCase):
    def test_missing_file_reports_stage_and_logs_traceback(self):
        def runner(job, progress):
            progress("connecting to the chart download")
            raise FileNotFoundError(2, "The system cannot find the file specified")

        job = Job("download", "Artist - Song", status="running")
        manager = JobManager(runner)
        manager.active = 1
        with mock.patch.object(manager, "_next", side_effect=[job, None]), \
                self.assertLogs("chcompanion.jobs", level="ERROR") as logs:
            manager._worker()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.step, "connecting to the chart download")
        self.assertIn(job.step, job.public()["error"])
        self.assertIn("cannot find the file", job.error)
        self.assertIn("Traceback", logs.output[0])
        self.assertIn("FileNotFoundError", logs.output[0])
        self.assertEqual(manager.active, 0)
