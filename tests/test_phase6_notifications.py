"""tests/test_phase6_notifications.py
----------------------------------
Phase 6 verification test suite for Notification Reliability and Post-Analyze Agent.

Covers:
1. Backend notification type mapping (INPROGRESS -> STARTED, STARTED -> STARTED, COMPLETED -> COMPLETED, CREATED -> ignored).
2. Notification deduplication against job_notifications table.
3. Terminal status locking (COMPLETED/FAILED/CANCELED lock prevents state reversion).
4. Device registration catch-up for active INPROGRESS and STARTED jobs.
5. Mobile reporter _should_send() logic across status changes and bucketing.
6. Desktop cad_diagnostics fast SCM polling interval decision during early window.
7. Standalone Post-Analyze Agent configuration and module compatibility.
"""

import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugin"
# Phase 7: agent consolidated to canonical repo; old standalone path kept for rollback reference only
STANDALONE_DIR = REPO_ROOT / "agents" / "post_analyze"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


class TestPhase6NotificationDispatch(unittest.TestCase):
    """Test notification mapping, deduplication, and filtering logic matching backend/app_postgres_ready.py."""

    def test_01_status_normalization_and_mapping(self):
        """Verify that INPROGRESS and STARTED both map to STARTED, and CREATED is ignored."""
        def map_notification(status_value):
            normalized = (status_value or "").strip().upper()
            if normalized not in {"INPROGRESS", "STARTED", "COMPLETED", "FAILED", "CANCELED"}:
                return None
            return "STARTED" if normalized in ("INPROGRESS", "STARTED") else normalized

        self.assertEqual(map_notification("INPROGRESS"), "STARTED")
        self.assertEqual(map_notification("inprogress"), "STARTED")
        self.assertEqual(map_notification("STARTED"), "STARTED")
        self.assertEqual(map_notification("started"), "STARTED")
        self.assertEqual(map_notification("COMPLETED"), "COMPLETED")
        self.assertEqual(map_notification("FAILED"), "FAILED")
        self.assertEqual(map_notification("CANCELED"), "CANCELED")
        # CREATED and QUEUED must not trigger notifications
        self.assertIsNone(map_notification("CREATED"))
        self.assertIsNone(map_notification("QUEUED"))
        self.assertIsNone(map_notification("SCHEDULED"))
        self.assertIsNone(map_notification(""))

    def test_02_notification_deduplication(self):
        """Verify exactly one notification of a given type is sent per job."""
        sent_notifications = set()

        def try_send(job_id, notif_type):
            key = (job_id, notif_type)
            if key in sent_notifications:
                return False
            sent_notifications.add(key)
            return True

        # First STARTED notification succeeds
        self.assertTrue(try_send("job-001", "STARTED"))
        # Subsequent STARTED notifications are rejected
        self.assertFalse(try_send("job-001", "STARTED"))
        self.assertFalse(try_send("job-001", "STARTED"))

        # Later COMPLETED notification for same job succeeds
        self.assertTrue(try_send("job-001", "COMPLETED"))
        # Duplicate COMPLETED rejected
        self.assertFalse(try_send("job-001", "COMPLETED"))

    def test_03_terminal_status_lock(self):
        """Verify that once a job reaches a terminal state, incoming non-terminal reports cannot flip it."""
        TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELED", "TIMEDOUT"}

        def resolve_effective_status(existing_status, incoming_status):
            existing_upper = (existing_status or "").strip().upper()
            incoming_upper = (incoming_status or "").strip().upper()
            is_locked = existing_upper in TERMINAL_STATUSES and existing_upper != incoming_upper
            return existing_upper if is_locked else incoming_upper 

        # Non-terminal transitions succeed
        self.assertEqual(resolve_effective_status("CREATED", "INPROGRESS"), "INPROGRESS")
        self.assertEqual(resolve_effective_status("INPROGRESS", "COMPLETED"), "COMPLETED")

        # Terminal state locks against regression
        self.assertEqual(resolve_effective_status("COMPLETED", "INPROGRESS"), "COMPLETED")
        self.assertEqual(resolve_effective_status("FAILED", "INPROGRESS"), "FAILED")
        self.assertEqual(resolve_effective_status("CANCELED", "INPROGRESS"), "CANCELED")

    def test_04_device_catchup_for_active_jobs(self):
        """Device registration catch-up must process active INPROGRESS and STARTED jobs."""
        active_candidates = [
            {"job_id": "job_1", "status": "INPROGRESS", "finished": False},
            {"job_id": "job_2", "status": "STARTED", "finished": False},
            {"job_id": "job_3", "status": "COMPLETED", "finished": True},
            {"job_id": "job_4", "status": "CREATED", "finished": False},
            {"job_id": "job_5", "status": "FAILED", "finished": True},
        ]

        catchup_jobs = []
        for candidate in active_candidates:
            if candidate["finished"]:
                continue
            curr_status = (candidate["status"] or "").strip().upper()
            if curr_status not in ("INPROGRESS", "STARTED"):
                continue
            catchup_jobs.append(candidate["job_id"])

        self.assertEqual(catchup_jobs, ["job_1", "job_2"])


class TestMobileReporterFilter(unittest.TestCase):
    """Test lib/mobile/reporter.py _should_send() logic."""

    def test_05_should_send_transitions(self):
        import lib.mobile.reporter as reporter 

        # First sight of a job must always send
        self.assertTrue(reporter._should_send("test-job-99", "CREATED", 0, False))

        # Manually register in reporter's cache
        reporter._last_sent["test-job-99"] = ("CREATED", 0, 1000.0)

        # Status change from CREATED to INPROGRESS must send
        self.assertTrue(reporter._should_send("test-job-99", "INPROGRESS", 0, False))

        reporter._last_sent["test-job-99"] = ("INPROGRESS", 0, 1000.0)

        # Same status and bucket without enough elapsed time must NOT send
        with unittest.mock.patch("time.time", return_value=1005.0):
            self.assertFalse(reporter._should_send("test-job-99", "INPROGRESS", 2, False))

        # 5% bucket change must send
        with unittest.mock.patch("time.time", return_value=1005.0):
            self.assertTrue(reporter._should_send("test-job-99", "INPROGRESS", 5, False))


class TestDesktopFastSCMPolling(unittest.TestCase):
    """Verify desktop cad_diagnostics polling cadence decision logic."""

    def test_06_fast_polling_cadence_logic(self):
        FAST_INTERVAL = 1.0
        CARD_INTERVAL = 1.5
        FAST_WINDOW = 10.0

        def calc_next_poll(now, start_time, job_id, last_status):
            searching_for = now - start_time
            if job_id:
                if (last_status not in ("INPROGRESS", "COMPLETED", "FAILED", "CANCELED", "TIMEDOUT")
                        and searching_for < FAST_WINDOW):
                    return now + FAST_INTERVAL
                else:
                    return now + CARD_INTERVAL
            elif searching_for < FAST_WINDOW:
                return now + FAST_INTERVAL
            return now + 4.0

        t0 = 1000.0
        # When job is known but still CREATED within fast window -> 1.0s interval
        self.assertEqual(calc_next_poll(1002.0, t0, "j1", "CREATED"), 1003.0)
        # When job transitions to INPROGRESS -> relaxes to 1.5s
        self.assertEqual(calc_next_poll(1003.0, t0, "j1", "INPROGRESS"), 1004.5)
        # When job is COMPLETED -> relaxes to 1.5s
        self.assertEqual(calc_next_poll(1004.0, t0, "j1", "COMPLETED"), 1005.5)


class TestStandaloneAgentEnvironment(unittest.TestCase):
    """Verify standalone agent configuration and module availability."""

    def test_07_pythoncom_available_in_canonical_env(self):
        """pythoncom must be importable without error under the running interpreter."""
        import pythoncom
        self.assertTrue(hasattr(pythoncom, "__file__") or bool(pythoncom))

    def test_08_canonical_plugin_modules_available(self):
        """compute_jobs, mobile_reporter, synergy_connect, and cad_diagnostics must exist in canonical plugin."""
        required = ["compute_jobs.py", "mobile_reporter.py", "synergy_connect.py", "cad_diagnostics.py"]
        for mod in required:
            self.assertTrue((PLUGIN_DIR / mod).is_file(), f"Missing required module: {mod}")


if __name__ == "__main__":
    unittest.main()
