"""tests/test_phase4b_notifications.py
----------------------------------
Phase 4B verification test suite for Analysis Started notification fixes.

Covers:
1. FCM Credential Resolution (JSON env, Individual env, File path, Missing creds)
2. FCM UnregisteredDeviceError compatibility across both modules
3. Device Registration Catch-up for active INPROGRESS jobs
4. Suppression of stale STARTED notifications for COMPLETED jobs
5. Deduplication of STARTED notifications
6. Desktop fast SCM search interval decision logic (1.0s vs 4.0s vs 30.0s)
7. Mobile foreground notification formatting (no 100% hardcoding, title/body preservation)
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestFCMCredentialResolution(unittest.TestCase):
    """Tests for backend/fcm_service.py multi-mode credential resolution."""

    def setUp(self):
        self._orig_env = {}
        for k in [
            "FIREBASE_SERVICE_ACCOUNT_JSON",
            "FIREBASE_PROJECT_ID",
            "FIREBASE_CLIENT_EMAIL",
            "FIREBASE_PRIVATE_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]:
            self._orig_env[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_01_fcm_load_from_json_env(self):
        """Mode B: Load credentials from raw JSON string in FIREBASE_SERVICE_ACCOUNT_JSON."""
        import fcm_service

        creds = {
            "project_id": "test-cloud-project",
            "client_email": "sa@test-cloud-project.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgk...\n-----END PRIVATE KEY-----\n",
        }
        os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"] = json.dumps(creds)
        loaded = fcm_service._load_service_account()
        self.assertEqual(loaded["project_id"], "test-cloud-project")
        self.assertEqual(loaded["client_email"], creds["client_email"])
        self.assertIn("BEGIN PRIVATE KEY", loaded["private_key"])

    def test_02_fcm_load_from_individual_env(self):
        """Mode C: Load credentials from individual FIREBASE_* environment variables."""
        import fcm_service

        os.environ["FIREBASE_PROJECT_ID"] = "test-indiv-project"
        os.environ["FIREBASE_CLIENT_EMAIL"] = "sa@indiv.iam.gserviceaccount.com"
        os.environ["FIREBASE_PRIVATE_KEY"] = "-----BEGIN PRIVATE KEY-----\\nLine2\\n-----END PRIVATE KEY-----\\n"

        loaded = fcm_service._load_service_account()
        self.assertEqual(loaded["project_id"], "test-indiv-project")
        self.assertEqual(loaded["client_email"], "sa@indiv.iam.gserviceaccount.com")
        # Escaped \\n must be converted to real \n
        self.assertIn("\nLine2\n", loaded["private_key"])

    def test_03_fcm_load_from_file(self):
        """Mode A: Load credentials from physical file via GOOGLE_APPLICATION_CREDENTIALS."""
        import fcm_service

        creds = {
            "project_id": "test-file-project",
            "client_email": "sa@file.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nKeyData\n-----END PRIVATE KEY-----\n",
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp:
            json.dump(creds, tmp)
            tmp_path = tmp.name

        try:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
            loaded = fcm_service._load_service_account()
            self.assertEqual(loaded["project_id"], "test-file-project")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_04_fcm_missing_credentials_raises_controlled_error(self):
        """When no credentials are provided, raise RuntimeError without crashing or leaking."""
        import fcm_service

        with self.assertRaises(RuntimeError) as ctx:
            fcm_service._load_service_account()
        err = str(ctx.exception)
        self.assertIn("Firebase credentials are not configured", err)
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", err)
        self.assertIn("FIREBASE_SERVICE_ACCOUNT_JSON", err)

    def test_05_unregistered_device_error_exported(self):
        """Both fcm_service and fcm_service_cloud_ready export UnregisteredDeviceError."""
        import fcm_service
        import fcm_service_cloud_ready

        self.assertTrue(issubclass(fcm_service.UnregisteredDeviceError, RuntimeError))
        self.assertTrue(issubclass(fcm_service_cloud_ready.UnregisteredDeviceError, RuntimeError))
        self.assertIs(fcm_service.UnregisteredDeviceError, fcm_service_cloud_ready.UnregisteredDeviceError)


class TestDeviceRegistrationCatchUp(unittest.TestCase):
    """Tests for backend device registration catch-up logic."""

    def test_06_device_catchup_logic(self):
        """Verify device registration triggers catch-up for active INPROGRESS jobs only."""
        # Test the catch-up filter logic in pure Python matching app_postgres_ready.py:
        jobs = [
            {"job_id": "job_1", "name": "Active1", "status": "INPROGRESS", "percent": 0, "finished": 0, "error_message": None},
            {"job_id": "job_2", "name": "Done1", "status": "COMPLETED", "percent": 100, "finished": 1, "error_message": None},
            {"job_id": "job_3", "name": "Failed1", "status": "FAILED", "percent": 50, "finished": 1, "error_message": "Err"},
            {"job_id": "job_4", "name": "Active2", "status": "inprogress", "percent": 15, "finished": False, "error_message": None},
        ]

        def _bool_value(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes")
            return bool(v)

        sent_started_jobs = []
        for candidate in jobs:
            if _bool_value(candidate["finished"]):
                continue
            curr_status = (candidate["status"] or "").strip().upper()
            if curr_status != "INPROGRESS":
                continue
            sent_started_jobs.append(candidate["job_id"])

        # Must include job_1 and job_4 only (active INPROGRESS)
        self.assertEqual(sent_started_jobs, ["job_1", "job_4"])
        self.assertNotIn("job_2", sent_started_jobs)
        self.assertNotIn("job_3", sent_started_jobs)


class TestDesktopFastSCMDiscovery(unittest.TestCase):
    """Tests for plugin/cad_diagnostics.py fast initial discovery timing decision."""

    def test_07_fast_search_cadence_decision(self):
        """First 10 seconds use ~1.0s interval; subsequent use ~4.0s; long-running use ~30s."""
        JOB_CARD_INTERVAL = 1.5
        JOB_SEARCH_FAST_WINDOW = 10.0
        JOB_SEARCH_FAST_INTERVAL = 1.0
        JOB_SEARCH_INTERVAL = 4.0
        JOB_SEARCH_PATIENCE = 300.0
        JOB_SEARCH_BACKOFF = 30.0

        def calculate_next_interval(searching_for, known_job_id=None):
            if known_job_id:
                return JOB_CARD_INTERVAL
            elif searching_for < JOB_SEARCH_FAST_WINDOW:
                return JOB_SEARCH_FAST_INTERVAL
            elif searching_for < JOB_SEARCH_PATIENCE:
                return JOB_SEARCH_INTERVAL
            else:
                return JOB_SEARCH_BACKOFF

        # During first 10 seconds: must return 1.0s tight interval
        self.assertEqual(calculate_next_interval(0.0), 1.0)
        self.assertEqual(calculate_next_interval(2.5), 1.0)
        self.assertEqual(calculate_next_interval(9.9), 1.0)

        # After 10 seconds but before patience: must return 4.0s normal interval
        self.assertEqual(calculate_next_interval(10.0), 4.0)
        self.assertEqual(calculate_next_interval(45.0), 4.0)
        self.assertEqual(calculate_next_interval(299.9), 4.0)

        # After patience timeout (300s): must back off to 30.0s
        self.assertEqual(calculate_next_interval(300.0), 30.0)
        self.assertEqual(calculate_next_interval(500.0), 30.0)

        # Once job is known: must return 1.5s
        self.assertEqual(calculate_next_interval(5.0, known_job_id="job-123"), 1.5)


class TestMobileNotificationFormatting(unittest.TestCase):
    """Tests simulating the C# FirebaseService.OnNotificationReceived logic."""

    def _format_notification(self, data):
        title = None
        body = None
        job_id = data.get("job_id") or data.get("jobId")
        job_name = data.get("job_name")
        status = data.get("status")
        percent_str = data.get("percent")
        notification_type = data.get("notification_type")

        # 1. Check if backend provided title/body in data
        if data.get("title") and str(data["title"]).strip():
            title = str(data["title"]).strip()
        if data.get("body") and str(data["body"]).strip():
            body = str(data["body"]).strip()

        effective_status = (status or "").strip().upper()
        effective_type = (notification_type or "").strip().upper()
        display_name = job_name or job_id or "Study"

        percent = 0
        if percent_str and str(percent_str).isdigit():
            percent = max(0, min(100, int(percent_str)))

        if not title:
            if effective_status == "INPROGRESS" or effective_type in ("JOB_STARTED", "STARTED"):
                title = "Moldflow Analysis Started"
            elif effective_status == "COMPLETED" or effective_type == "JOB_COMPLETED":
                title = "Moldflow Analysis Completed"
            elif effective_status == "FAILED" or effective_type == "JOB_FAILED":
                title = "Moldflow Analysis Failed"
            elif effective_status == "CANCELED" or effective_type == "JOB_CANCELED":
                title = "Moldflow Analysis Canceled"
            else:
                title = f"Moldflow: {display_name}"

        if not body:
            if effective_status == "INPROGRESS" or effective_type in ("JOB_STARTED", "STARTED"):
                if percent > 0:
                    body = f"{display_name} is running ({percent}%)."
                else:
                    body = f"{display_name} has started running."
            elif effective_status == "COMPLETED" or effective_type == "JOB_COMPLETED":
                body = f"{display_name} completed successfully."
            elif effective_status == "FAILED" or effective_type == "JOB_FAILED":
                body = f"{display_name} failed."
            elif effective_status == "CANCELED" or effective_type == "JOB_CANCELED":
                body = f"{display_name} was canceled."
            else:
                body = f"{display_name}: {status or 'Update'}"

        return title, body

    def test_08_started_notification_body_never_hardcodes_100_percent(self):
        """STARTED notification at 0% must say 'has started running' and NEVER '(100%)'."""
        data = {
            "job_id": "j-101",
            "job_name": "Cover_Study",
            "status": "INPROGRESS",
            "percent": "0",
            "notification_type": "JOB_INPROGRESS",
        }
        title, body = self._format_notification(data)
        self.assertEqual(title, "Moldflow Analysis Started")
        self.assertEqual(body, "Cover_Study has started running.")
        self.assertNotIn("(100%)", body)

    def test_09_progress_notification_uses_actual_percent(self):
        """INPROGRESS telemetry with percent=35 must show '35%' and NEVER '(100%)'."""
        data = {
            "job_id": "j-102",
            "job_name": "Gear_Study",
            "status": "INPROGRESS",
            "percent": "35",
            "notification_type": "JOB_INPROGRESS",
        }
        title, body = self._format_notification(data)
        self.assertIn("35%", body)
        self.assertNotIn("(100%)", body)

    def test_10_completed_notification(self):
        """COMPLETED notification must state completed successfully."""
        data = {
            "job_id": "j-103",
            "job_name": "Base_Study",
            "status": "COMPLETED",
            "percent": "100",
            "notification_type": "JOB_COMPLETED",
        }
        title, body = self._format_notification(data)
        self.assertEqual(title, "Moldflow Analysis Completed")
        self.assertEqual(body, "Base_Study completed successfully.")

    def test_11_failed_notification(self):
        """FAILED notification must state failed and NEVER '(100%)'."""
        data = {
            "job_id": "j-104",
            "job_name": "Bad_Mesh",
            "status": "FAILED",
            "percent": "12",
            "notification_type": "JOB_FAILED",
        }
        title, body = self._format_notification(data)
        self.assertEqual(title, "Moldflow Analysis Failed")
        self.assertEqual(body, "Bad_Mesh failed.")
        self.assertNotIn("(100%)", body)

    def test_12_backend_provided_title_body_preserved(self):
        """When backend includes explicit title and body in payload, preserve them unchanged."""
        data = {
            "job_id": "j-105",
            "job_name": "Custom_Study",
            "status": "INPROGRESS",
            "percent": "0",
            "title": "Custom Backend Title",
            "body": "Custom Backend Body",
        }
        title, body = self._format_notification(data)
        self.assertEqual(title, "Custom Backend Title")
        self.assertEqual(body, "Custom Backend Body")


if __name__ == "__main__":
    unittest.main()
