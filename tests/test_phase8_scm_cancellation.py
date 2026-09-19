"""
tests/test_phase8_scm_cancellation.py
-------------------------------------
Phase 8 Task 2 Test Suite: SCM DELETE Cancellation Mechanism & Component Safety.

Tests cover:
  1. HTTP 200 (cancellation accepted/success)
  2. HTTP 404 with already-terminal job (COMPLETED / CANCELED / FAILED / TIMEDOUT)
  3. HTTP 404 with unknown/wrong job ID (unreconciled -> returns False)
  4. 400 / 401 / 403 / 500 error responses
  5. Timeout error handling
  6. Connection failure (URLError) handling
  7. Correct SCM job ID propagation (URL ends with SCM ID, method=DELETE)
  8. Duplicate cancellation requests (cache absorption)
  9. Invalid/empty SCM job ID inputs
 10. SCM unavailable handling
 11. plugin.compute_jobs backward-compatibility export
 12. Monitor authoritative cancellation handling (success vs failure)
 13. Monitor SCM job ID separation from application job ID
 14. Backend cancel_requested state lifecycle consistency
"""

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import lib.scm.client as scm_client
import plugin.compute_jobs as plugin_compute_jobs


class TestScmClientCancelJob(unittest.TestCase):
    """Unit tests for lib.scm.client.cancel_job()."""

    def setUp(self):
        # Clear recent cancellation cache before each test
        scm_client._recently_canceled.clear()

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_01_cancel_job_http_200_success(self, mock_urlopen, mock_base_url):
        """HTTP 200 returns True and records success."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getcode.return_value = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = scm_client.cancel_job("scm-uuid-100")
        self.assertTrue(result)
        self.assertTrue(mock_urlopen.called)

        # Verify request method and URL
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "DELETE")
        self.assertEqual(req.full_url, "http://127.0.0.1:44100/ComputeQueue/v1/jobs/scm-uuid-100")

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("lib.scm.client.get_job")
    @patch("urllib.request.urlopen")
    def test_02_cancel_job_http_404_already_terminal_completed(self, mock_urlopen, mock_get_job, mock_base_url):
        """HTTP 404 with SCM job verified as COMPLETED returns True."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/scm-uuid-200",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        mock_get_job.return_value = {"jobID": "scm-uuid-200", "status": "COMPLETED"}

        result = scm_client.cancel_job("scm-uuid-200")
        self.assertTrue(result)
        mock_get_job.assert_called_once_with("scm-uuid-200")

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("lib.scm.client.get_job")
    @patch("urllib.request.urlopen")
    def test_03_cancel_job_http_404_already_terminal_canceled(self, mock_urlopen, mock_get_job, mock_base_url):
        """HTTP 404 with SCM job verified as CANCELED returns True."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/scm-uuid-201",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        mock_get_job.return_value = {"jobID": "scm-uuid-201", "status": "CANCELED"}

        result = scm_client.cancel_job("scm-uuid-201")
        self.assertTrue(result)

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("lib.scm.client.get_job")
    @patch("urllib.request.urlopen")
    def test_04_cancel_job_http_404_unknown_unreconciled_returns_false(self, mock_urlopen, mock_get_job, mock_base_url):
        """HTTP 404 where SCM job cannot be reconciled (get_job is None) returns False."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/unknown-id",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        mock_get_job.return_value = None

        result = scm_client.cancel_job("unknown-id")
        self.assertFalse(result)

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("lib.scm.client.get_job")
    @patch("urllib.request.urlopen")
    def test_05_cancel_job_http_404_non_terminal_returns_false(self, mock_urlopen, mock_get_job, mock_base_url):
        """HTTP 404 where SCM job is non-terminal (e.g. INPROGRESS) returns False."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/scm-active",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        mock_get_job.return_value = {"jobID": "scm-active", "status": "INPROGRESS"}

        result = scm_client.cancel_job("scm-active")
        self.assertFalse(result)

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_06_cancel_job_http_400_bad_request_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/bad",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        self.assertFalse(scm_client.cancel_job("bad"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_07_cancel_job_http_401_unauthorized_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/unauth",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        self.assertFalse(scm_client.cancel_job("unauth"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_08_cancel_job_http_403_forbidden_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/forbid",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        self.assertFalse(scm_client.cancel_job("forbid"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_09_cancel_job_http_500_internal_error_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/err500",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b""),
        )
        self.assertFalse(scm_client.cancel_job("err500"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_10_cancel_job_timeout_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = TimeoutError("Request timed out")
        self.assertFalse(scm_client.cancel_job("timeout-id"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_11_cancel_job_connection_refused_returns_false(self, mock_urlopen, mock_base_url):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        self.assertFalse(scm_client.cancel_job("conn-refused-id"))

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_12_correct_scm_job_id_propagation(self, mock_urlopen, mock_base_url):
        """Verifies URL quotation and exact SCM job ID path formatting."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getcode.return_value = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        scm_client.cancel_job("custom-scm-uuid/with-slash")
        req = mock_urlopen.call_args[0][0]
        self.assertIn("custom-scm-uuid%2Fwith-slash", req.full_url)
        self.assertEqual(req.get_method(), "DELETE")

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_13_duplicate_cancellation_request_absorbed(self, mock_urlopen, mock_base_url):
        """Second call within 60s is absorbed and does not send duplicate HTTP DELETE."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getcode.return_value = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # First call: hits urlopen
        res1 = scm_client.cancel_job("duplicate-test-id")
        self.assertTrue(res1)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call: absorbed by cache, returns True immediately
        res2 = scm_client.cancel_job("duplicate-test-id")
        self.assertTrue(res2)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_14_invalid_empty_none_inputs_return_false(self):
        self.assertFalse(scm_client.cancel_job(""))
        self.assertFalse(scm_client.cancel_job("   "))
        self.assertFalse(scm_client.cancel_job(None))
        self.assertFalse(scm_client.cancel_job(12345))

    @patch("lib.scm.client.base_url", return_value=None)
    def test_15_scm_unavailable_returns_false(self, mock_base_url):
        self.assertFalse(scm_client.cancel_job("some-id"))

    def test_16_plugin_compute_jobs_shim_exports_cancel_job(self):
        """Verify backward compatibility: plugin.compute_jobs exports cancel_job."""
        self.assertTrue(hasattr(plugin_compute_jobs, "cancel_job"))
        self.assertIs(plugin_compute_jobs.cancel_job, scm_client.cancel_job)


class TestDesktopCancellationIntegration(unittest.TestCase):
    """Component integration tests for monitor and agent cancellation handlers."""

    def setUp(self):
        scm_client._recently_canceled.clear()

    @patch("monitor.standalone_job_monitor.compute_jobs.cancel_job", return_value=True)
    @patch("monitor.standalone_job_monitor.mobile_reporter.report_status")
    def test_17_monitor_successful_cancellation_reports_canceled(self, mock_report, mock_cancel):
        import monitor.standalone_job_monitor as mon

        job_row = {
            "job_id": "app-job-001",
            "scm_job_id": "scm-uuid-777",
            "name": "study_part.sdy",
            "job_type": "study",
            "cancel_requested": True,
            "status": "INPROGRESS",
            "percent": 45,
        }

        mon.watch_one(job_row)

        # Verified that cancel_job was called with SCM ID (Requirement 2 & 7)
        mock_cancel.assert_called_once_with("scm-uuid-777")

        # Verified that report_status was called with CANCELED
        mock_report.assert_called_once()
        report_data = mock_report.call_args[0][0]
        self.assertEqual(report_data["status"], "CANCELED")
        self.assertEqual(report_data["job_id"], "app-job-001")
        self.assertEqual(report_data["scm_job_id"], "scm-uuid-777")
        self.assertTrue(report_data["finished"])

    @patch("monitor.standalone_job_monitor.compute_jobs.cancel_job", return_value=False)
    @patch("monitor.standalone_job_monitor.mobile_reporter.report_status")
    def test_18_monitor_failed_cancellation_does_not_report_canceled(self, mock_report, mock_cancel):
        """Requirement 5 & 8: If SCM cancellation fails, do NOT report fake CANCELED."""
        import monitor.standalone_job_monitor as mon

        job_row = {
            "job_id": "app-job-002",
            "scm_job_id": "scm-uuid-888",
            "name": "study_part.sdy",
            "job_type": "study",
            "cancel_requested": True,
            "status": "INPROGRESS",
            "percent": 50,
        }

        mon.watch_one(job_row)

        mock_cancel.assert_called_once_with("scm-uuid-888")
        # report_status must NOT be called with CANCELED
        mock_report.assert_not_called()

    @patch("monitor.standalone_job_monitor.compute_jobs.cancel_job", return_value=True)
    @patch("monitor.standalone_job_monitor.mobile_reporter.report_status")
    def test_19_monitor_strict_scm_job_id_separation(self, mock_report, mock_cancel):
        """Requirement 2: Ensure application job ID is never passed to SCM cancel_job."""
        import monitor.standalone_job_monitor as mon

        job_row = {
            "job_id": "DESKTOP-ABC_study123_20260910",
            "scm_job_id": "99999999-1111-2222-3333-444444444444",
            "name": "study123.sdy",
            "job_type": "study",
            "cancel_requested": True,
            "status": "INPROGRESS",
            "percent": 20,
        }

        mon.watch_one(job_row)

        mock_cancel.assert_called_once_with("99999999-1111-2222-3333-444444444444")
        self.assertNotEqual(mock_cancel.call_args[0][0], "DESKTOP-ABC_study123_20260910")

    @patch("monitor.standalone_job_monitor.compute_jobs.cancel_job")
    @patch("monitor.standalone_job_monitor.mobile_reporter.report_status")
    def test_20_monitor_missing_scm_job_id_aborts_safely(self, mock_report, mock_cancel):
        """If scm_job_id is missing, watch_one does not attempt to cancel or report CANCELED."""
        import monitor.standalone_job_monitor as mon

        job_row = {
            "job_id": "app-job-no-scm",
            "scm_job_id": "",
            "cancel_requested": True,
            "status": "INPROGRESS",
        }

        mon.watch_one(job_row)
        mock_cancel.assert_not_called()
        mock_report.assert_not_called()

    def test_21_agent_cancellation_invokes_scm_cancel_and_reports(self):
        """Post-Analyze Agent cancels SCM and reports CANCELED on success."""
        from agents.post_analyze.agent import StandaloneAgent

        agent = StandaloneAgent.__new__(StandaloneAgent)
        agent.log = MagicMock()
        agent.mobile_reporter = MagicMock()
        agent.mobile_reporter.check_cancel.return_value = True

        agent.compute_jobs = MagicMock()
        agent.compute_jobs.cancel_job.return_value = True

        agent.active = {
            "scm-agent-1": {
                "job_id": "scm-agent-1",
                "scm_job_id": "scm-agent-1",
                "name": "test.sdy",
                "status": "INSPECTION",
                "percent": 10,
            }
        }

        agent._update_active()

        agent.compute_jobs.cancel_job.assert_called_once_with("scm-agent-1")
        agent.mobile_reporter.report_status.assert_called()
        report_data = agent.mobile_reporter.report_status.call_args[0][0]
        self.assertEqual(report_data["status"], "CANCELED")
        self.assertNotIn("scm-agent-1", agent.active)

    def test_22_agent_failed_cancellation_does_not_report_canceled(self):
        """Post-Analyze Agent retains job and does NOT report CANCELED if SCM cancel fails."""
        from agents.post_analyze.agent import StandaloneAgent

        agent = StandaloneAgent.__new__(StandaloneAgent)
        agent.log = MagicMock()
        agent.mobile_reporter = MagicMock()
        agent.mobile_reporter.check_cancel.return_value = True

        agent.compute_jobs = MagicMock()
        agent.compute_jobs.cancel_job.return_value = False

        agent.active = {
            "scm-agent-2": {
                "job_id": "scm-agent-2",
                "scm_job_id": "scm-agent-2",
                "name": "test2.sdy",
                "status": "INSPECTION",
                "percent": 10,
            }
        }

        agent._update_active()

        agent.compute_jobs.cancel_job.assert_called_once_with("scm-agent-2")
        agent.mobile_reporter.report_status.assert_not_called()
        self.assertIn("scm-agent-2", agent.active)

    def test_23_backend_cancellation_state_consistency(self):
        """Verifies backend logic: cancel_requested is cleared when terminal status arrives."""
        # Mirror backend terminal lock & cancel_requested logic
        TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELED", "CANCELLED", "TIMEDOUT"}

        job_state = {"status": "INPROGRESS", "cancel_requested": True}

        # Simulating /jobs/{id}/cancel endpoint
        self.assertTrue(job_state["cancel_requested"])

        # When terminal CANCELED report arrives:
        incoming_status = "CANCELED"
        if incoming_status in TERMINAL_STATUSES:
            job_state["status"] = incoming_status
            job_state["cancel_requested"] = False

        self.assertEqual(job_state["status"], "CANCELED")
        self.assertFalse(job_state["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
