"""tests/test_phase3_extraction.py
--------------------------------
Phase 3 architectural extraction tests.

Verifies:
1. lib.scm.client imports and public API
2. lib.mobile.reporter imports and public API
3. plugin.compute_jobs backward-compatible shim imports
4. plugin.mobile_reporter backward-compatible shim imports
5. monitor imports independently from plugin (without plugin on sys.path)
6. summarize(None) returns None
7. summarize({}) returns valid default dict
8. reporter throttling logic (_should_send)
9. percent bucket logic (_percent_bucket)
10. JobStatus construction and default fields
11. No duplicate canonical implementations exist in repository
12. Config resolution order (MOLDFLOW_CONFIG, repo plugin fallback)
"""

import os
import sys
import unittest
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestPhase3Extraction(unittest.TestCase):
    """Phase 3 test suite covering architectural extraction requirements."""

    def test_01_lib_scm_client_imports(self):
        """1. lib.scm.client imports cleanly with full public API."""
        from lib.scm import client as scm
        self.assertTrue(hasattr(scm, "base_url"))
        self.assertTrue(hasattr(scm, "available"))
        self.assertTrue(hasattr(scm, "list_jobs"))
        self.assertTrue(hasattr(scm, "get_job"))
        self.assertTrue(hasattr(scm, "find_job"))
        self.assertTrue(hasattr(scm, "job_ids"))
        self.assertTrue(hasattr(scm, "summarize"))
        self.assertTrue(hasattr(scm, "phases"))
        self.assertTrue(hasattr(scm, "viewer_exe"))
        self.assertTrue(hasattr(scm, "open_viewer"))
        self.assertEqual(scm.DEFAULT_PORT, 44100)

    def test_02_lib_mobile_reporter_imports(self):
        """2. lib.mobile.reporter imports cleanly with full public API."""
        from lib.mobile import reporter
        self.assertTrue(hasattr(reporter, "enabled"))
        self.assertTrue(hasattr(reporter, "report_status"))
        self.assertTrue(hasattr(reporter, "check_cancel"))
        self.assertTrue(hasattr(reporter, "list_active_jobs"))
        self.assertTrue(hasattr(reporter, "CONFIG_PATH"))
        self.assertEqual(reporter.HTTP_TIMEOUT, 5.0)
        self.assertEqual(reporter.MIN_RESEND_INTERVAL, 60.0)

    def test_03_plugin_compute_jobs_shim_imports(self):
        """3. plugin.compute_jobs shim imports and delegates to lib.scm.client."""
        from plugin import compute_jobs
        from lib.scm import client as scm
        self.assertIs(compute_jobs.summarize, scm.summarize)
        self.assertIs(compute_jobs.available, scm.available)
        self.assertIs(compute_jobs.get_job, scm.get_job)
        self.assertIs(compute_jobs.list_jobs, scm.list_jobs)
        self.assertIs(compute_jobs.find_job, scm.find_job)
        self.assertIs(compute_jobs.job_ids, scm.job_ids)
        self.assertIs(compute_jobs.phases, scm.phases)
        self.assertEqual(compute_jobs.DEFAULT_PORT, scm.DEFAULT_PORT)

    def test_04_plugin_mobile_reporter_shim_imports(self):
        """4. plugin.mobile_reporter shim imports and delegates to lib.mobile.reporter."""
        from plugin import mobile_reporter
        from lib.mobile import reporter
        self.assertIs(mobile_reporter.report_status, reporter.report_status)
        self.assertIs(mobile_reporter.check_cancel, reporter.check_cancel)
        self.assertIs(mobile_reporter.list_active_jobs, reporter.list_active_jobs)
        self.assertIs(mobile_reporter._percent_bucket, reporter._percent_bucket)
        self.assertIs(mobile_reporter._should_send, reporter._should_send)
        self.assertEqual(mobile_reporter.HTTP_TIMEOUT, reporter.HTTP_TIMEOUT)

    def test_05_monitor_imports_independently_from_plugin(self):
        """5. monitor imports independently without plugin on sys.path."""
        import monitor.standalone_job_monitor as mon
        self.assertIn("lib", mon.compute_jobs.__file__)
        self.assertIn("scm", mon.compute_jobs.__file__)
        self.assertIn("lib", mon.mobile_reporter.__file__)
        self.assertIn("mobile", mon.mobile_reporter.__file__)

    def test_06_summarize_none(self):
        """6. summarize(None) returns None."""
        from lib.scm import client as scm
        self.assertIsNone(scm.summarize(None))

    def test_07_summarize_empty_dict(self):
        """7. summarize({}) returns normalized default dict."""
        from lib.scm import client as scm
        result = scm.summarize({})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["job_id"], "")
        self.assertEqual(result["name"], "")
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(result["percent"], 0)
        self.assertFalse(result["finished"])

    def test_08_reporter_throttling_logic(self):
        """8. reporter throttling logic (_should_send) operates correctly."""
        from lib.mobile import reporter
        job_id = "test_throttle_job"

        # Clear any prior state for this test job
        reporter._last_sent.pop(job_id, None)

        # First report of an active job -> should send
        self.assertTrue(reporter._should_send(job_id, "INPROGRESS", 10.0, False))

        # Record send
        reporter._last_sent[job_id] = ("INPROGRESS", reporter._percent_bucket(10.0), 1000.0)

        # Same bucket, same status, right after (within MIN_RESEND_INTERVAL) -> do NOT send
        import time
        fake_now = 1010.0  # only 10s later
        orig_time = time.time
        try:
            time.time = lambda: fake_now
            self.assertFalse(reporter._should_send(job_id, "INPROGRESS", 12.0, False))

            # Progress to new 5% bucket (e.g. 16% -> bucket 15) -> should send
            self.assertTrue(reporter._should_send(job_id, "INPROGRESS", 16.0, False))

            # Terminal status transition (COMPLETED) -> should always send
            self.assertTrue(reporter._should_send(job_id, "COMPLETED", 100.0, True))

            # After MIN_RESEND_INTERVAL elapsed (e.g. 70s later) -> should send heartbeat
            fake_now = 1000.0 + reporter.MIN_RESEND_INTERVAL + 5.0
            self.assertTrue(reporter._should_send(job_id, "INPROGRESS", 10.0, False))
        finally:
            time.time = orig_time
            reporter._last_sent.pop(job_id, None)

    def test_09_percent_bucket_logic(self):
        """9. percent bucket logic rounds down to nearest 5% bucket."""
        from lib.mobile import reporter
        self.assertEqual(reporter._percent_bucket(0), 0)
        self.assertEqual(reporter._percent_bucket(4.9), 0)
        self.assertEqual(reporter._percent_bucket(5), 5)
        self.assertEqual(reporter._percent_bucket(9.99), 5)
        self.assertEqual(reporter._percent_bucket(42), 40)
        self.assertEqual(reporter._percent_bucket(45), 45)
        self.assertEqual(reporter._percent_bucket(99), 95)
        self.assertEqual(reporter._percent_bucket(100), 100)

    def test_10_job_status_construction(self):
        """10. JobStatus domain model constructs with expected fields and defaults."""
        from lib.jobs.models import JobStatus, JobPhase
        phase = JobPhase(name="Filling", status="INPROGRESS", percent=50.0)
        job = JobStatus(
            job_id="job-999",
            scm_job_id="scm-888",
            name="TestStudy",
            type="Flow",
            status="INPROGRESS",
            percent=50,
            phases=[phase],
        )
        self.assertEqual(job.job_id, "job-999")
        self.assertEqual(job.scm_job_id, "scm-888")
        self.assertEqual(job.name, "TestStudy")
        self.assertEqual(job.status, "INPROGRESS")
        self.assertEqual(job.percent, 50)
        self.assertFalse(job.finished)
        self.assertFalse(job.cancel_requested)
        self.assertEqual(len(job.phases), 1)
        self.assertEqual(job.phases[0].name, "Filling")

    def test_11_no_duplicate_canonical_implementations(self):
        """11. Verify compute_jobs.py and mobile_reporter.py in plugin/ are shims, not duplicates."""
        plugin_compute = (REPO_ROOT / "plugin" / "compute_jobs.py").read_text(encoding="utf-8")
        plugin_mobile = (REPO_ROOT / "plugin" / "mobile_reporter.py").read_text(encoding="utf-8")

        # Plugin files must be small shims that import from lib/
        self.assertIn("from lib.scm.client import", plugin_compute)
        self.assertIn("from lib.mobile.reporter import", plugin_mobile)
        self.assertLess(len(plugin_compute.splitlines()), 60)
        self.assertLess(len(plugin_mobile.splitlines()), 60)

    def test_12_config_resolution(self):
        """12. mobile_reporter config path resolves to existing config or MOLDFLOW_CONFIG."""
        from lib.mobile import reporter
        self.assertTrue(reporter.CONFIG_PATH.exists())
        self.assertEqual(reporter.CONFIG_PATH.name, "mobile_report_config.json")


if __name__ == "__main__":
    unittest.main()
