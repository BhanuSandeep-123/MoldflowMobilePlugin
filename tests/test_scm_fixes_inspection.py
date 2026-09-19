"""
tests/test_scm_fixes_inspection.py
----------------------------------
Targeted validation tests for:
  Issue 1: SCM User preservation across Moldflow Synergy close.
  Issue 2: SCM-side cancellation reflection in Mobile (Case A: missing/deleted job, Case B: child stage cancellation).
  Integrity: Preserving existing Mobile -> Backend -> SCM cancellation.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import lib.scm.client as scm_client
import monitor.standalone_job_monitor as standalone_monitor
import plugin.standalone_job_monitor as plugin_monitor
from agents.post_analyze.agent import StandaloneAgent


class TestScmUserPreservation(unittest.TestCase):
    """Issue 1: Validate SCM User extraction and propagation across components."""

    def test_scm_client_summarize_extracts_user_from_payload(self):
        job = {
            "jobID": "test-scm-001",
            "name": "study1.sdy",
            "status": "INPROGRESS",
            "progress": 45,
            "payload": {
                "user": "DOMAIN\\TestEngineer",
                "type": "study",
            },
        }
        summary = scm_client.summarize(job)
        self.assertEqual(summary["scm_user"], "DOMAIN\\TestEngineer")

    def test_scm_client_summarize_falls_back_to_workstation_user_when_not_signed_in(self):
        job = {
            "jobID": "test-scm-002",
            "name": "study2.sdy",
            "status": "INPROGRESS",
            "progress": 50,
            "payload": {},
        }
        with patch.dict("os.environ", {"USERNAME": "MockWorkstationUser"}):
            summary = scm_client.summarize(job)
            self.assertEqual(summary["scm_user"], "MockWorkstationUser")

    def test_monitor_watch_one_propagates_scm_user(self):
        """Monitor includes scm_user in mobile_reporter.report_status payload."""
        job_row = {
            "job_id": "app-job-1",
            "scm_job_id": "scm-job-1",
            "name": "part.sdy",
            "job_type": "study",
            "status": "INPROGRESS",
            "scm_user": None,
        }
        mock_scm_job = {
            "jobID": "scm-job-1",
            "status": "INPROGRESS",
            "progress": 30,
            "payload": {"user": "UnoTEAM-0144"},
        }

        with patch.object(standalone_monitor.compute_jobs, "get_job", return_value=mock_scm_job), \
             patch.object(standalone_monitor.mobile_reporter, "report_status") as mock_report:
            standalone_monitor.watch_one(job_row)
            self.assertTrue(mock_report.called)
            reported_data = mock_report.call_args[0][0]
            self.assertEqual(reported_data["scm_user"], "UnoTEAM-0144")

    def test_agent_job_payload_and_refresh_preserves_scm_user(self):
        """Agent sets scm_user on start and updates on refresh."""
        agent = StandaloneAgent.__new__(StandaloneAgent)
        agent.log = MagicMock()
        agent.compute_jobs = MagicMock()
        agent.compute_jobs.summarize.return_value = {
            "name": "part.sdy",
            "status": "INPROGRESS",
            "percent": 25,
            "finished": False,
            "scm_user": "UnoTEAM-0144",
            "type": "study",
            "worker": "local",
            "cloud": False,
        }

        row = {
            "jobID": "scm-job-2",
            "payload": {"name": "part.sdy", "user": "UnoTEAM-0144", "type": "study"},
        }
        payload = agent._job_payload(row)
        self.assertEqual(payload["scm_user"], "UnoTEAM-0144")

        # Refresh
        agent.compute_jobs.get_job.return_value = row
        agent._refresh_job_from_scm(payload)
        self.assertEqual(payload["scm_user"], "UnoTEAM-0144")


class TestScmSideCancellation(unittest.TestCase):
    """Issue 2: Reverse cancellation (SCM Job Manager -> Mobile)."""

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_case_a_get_job_catches_500_failed_to_locate_job(self, mock_urlopen, mock_base_url):
        """SCM returning 500 'Failed to locate job' returns _scm_deleted: True."""
        err_msg = "Failed to locate job test-scm-deleted"
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/test-scm-deleted",
            code=500,
            msg=err_msg,
            hdrs={},
            fp=io.BytesIO(err_msg.encode("utf-8")),
        )
        mock_urlopen.side_effect = err
        result = scm_client.get_job("test-scm-deleted")
        self.assertIsNotNone(result)
        self.assertTrue(result.get("_scm_deleted"))
        self.assertEqual(result.get("status"), "CANCELED")

    @patch("lib.scm.client.base_url", return_value="http://127.0.0.1:44100/ComputeQueue/v1")
    @patch("urllib.request.urlopen")
    def test_case_a_get_job_catches_404_not_found(self, mock_urlopen, mock_base_url):
        """SCM returning 404 returns _scm_deleted: True."""
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:44100/ComputeQueue/v1/jobs/test-scm-404",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b"Not Found"),
        )
        mock_urlopen.side_effect = err
        result = scm_client.get_job("test-scm-404")
        self.assertIsNotNone(result)
        self.assertTrue(result.get("_scm_deleted"))
        self.assertEqual(result.get("status"), "CANCELED")

    def test_case_a_monitor_reconciles_scm_deleted_flag(self):
        """Monitor receives _scm_deleted and reports CANCELED to backend."""
        job_row = {
            "job_id": "app-job-deleted",
            "scm_job_id": "scm-job-deleted",
            "name": "part.sdy",
            "job_type": "study",
            "status": "INPROGRESS",
        }
        deleted_scm_job = {
            "jobID": "scm-job-deleted",
            "status": "CANCELED",
            "_scm_deleted": True,
        }

        with patch.object(standalone_monitor.compute_jobs, "get_job", return_value=deleted_scm_job), \
             patch.object(standalone_monitor.mobile_reporter, "report_status") as mock_report:
            standalone_monitor.watch_one(job_row)
            self.assertTrue(mock_report.called)
            reported = mock_report.call_args[0][0]
            self.assertEqual(reported["status"], "CANCELED")
            self.assertTrue(reported["finished"])

    def test_case_a_monitor_reconciles_missing_job_when_scm_healthy(self):
        """When get_job returns None and SCM is healthy without the job, report CANCELED."""
        job_row = {
            "job_id": "app-job-vanished",
            "scm_job_id": "scm-job-vanished",
            "name": "part.sdy",
            "job_type": "study",
            "status": "INPROGRESS",
        }

        with patch.object(standalone_monitor.compute_jobs, "get_job", return_value=None), \
             patch.object(standalone_monitor.compute_jobs, "available", return_value=True), \
             patch.object(standalone_monitor.compute_jobs, "job_ids", return_value={"other-job-1"}), \
             patch.object(standalone_monitor.mobile_reporter, "report_status") as mock_report:
            standalone_monitor.watch_one(job_row)
            self.assertTrue(mock_report.called)
            reported = mock_report.call_args[0][0]
            self.assertEqual(reported["status"], "CANCELED")
            self.assertTrue(reported["finished"])

    def test_case_b_child_stage_cancellation_in_mesh_plus_study(self):
        """When childDetails has a CANCELED phase, summarize() returns status CANCELED and finished True."""
        multi_stage_job = {
            "jobID": "parent-mesh-study-001",
            "status": "COMPLETED",  # Parent says COMPLETED or INPROGRESS
            "progress": 50,
            "childDetails": [
                {
                    "jobID": "child-phase-1",
                    "status": "COMPLETED",
                    "progress": 100,
                },
                {
                    "jobID": "child-phase-2",
                    "status": "CANCELED",  # Child was canceled by user in SCM Job Manager
                    "progress": 20,
                },
            ],
            "payload": {
                "name": "bracket_mesh_study.sdy",
                "type": "mesh+study",
                "user": "UnoTEAM-0144",
            },
        }

        summary = scm_client.summarize(multi_stage_job)
        self.assertEqual(summary["status"], "CANCELED")
        self.assertTrue(summary["finished"])

    def test_agent_handles_scm_deleted_job(self):
        """Agent detects deleted SCM job, reports CANCELED, and cleans up active job."""
        agent = StandaloneAgent.__new__(StandaloneAgent)
        agent.log = MagicMock()
        agent.mobile_reporter = MagicMock()
        agent.mobile_reporter.check_cancel.return_value = False
        agent.compute_jobs = MagicMock()
        agent.compute_jobs.get_job.return_value = {
            "jobID": "scm-agent-del",
            "status": "CANCELED",
            "_scm_deleted": True,
        }
        agent.compute_jobs.available.return_value = True

        agent.active = {
            "scm-agent-del": {
                "job_id": "scm-agent-del",
                "scm_job_id": "scm-agent-del",
                "name": "test.sdy",
                "status": "INPROGRESS",
            }
        }

        agent._update_active()
        agent.mobile_reporter.report_status.assert_called()
        reported = agent.mobile_reporter.report_status.call_args[0][0]
        self.assertEqual(reported["status"], "CANCELED")
        self.assertTrue(reported["finished"])
        self.assertNotIn("scm-agent-del", agent.active)


class TestMobileToScmCancellationPreserved(unittest.TestCase):
    """Verify that existing Mobile -> SCM forward cancellation path remains intact."""

    def test_forward_cancellation_still_calls_scm_delete(self):
        """Mobile cancel_requested still invokes compute_jobs.cancel_job."""
        job_row = {
            "job_id": "app-forward-cancel",
            "scm_job_id": "scm-uuid-forward",
            "name": "part.sdy",
            "job_type": "study",
            "cancel_requested": True,
            "status": "INPROGRESS",
        }

        with patch.object(standalone_monitor.compute_jobs, "cancel_job", return_value=True) as mock_cancel, \
             patch.object(standalone_monitor.mobile_reporter, "report_status") as mock_report:
            standalone_monitor.watch_one(job_row)
            mock_cancel.assert_called_once_with("scm-uuid-forward")
            self.assertTrue(mock_report.called)
            reported = mock_report.call_args[0][0]
            self.assertEqual(reported["status"], "CANCELED")


if __name__ == "__main__":
    unittest.main()
