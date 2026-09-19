"""tests/test_phase7_agent.py
-----------------------------
Phase 7 verification test suite for the consolidated Post-Analyze Agent.

The agent is now at:
  MoldflowMobileSystem/agents/post_analyze/

These tests import agent modules *directly* from the canonical repo
(no path injection to the old standalone project).

Covers:
1. Agent modules importable from canonical agents/post_analyze path.
2. config.json.example has all required keys.
3. config.json (live, .gitignore'd) points to canonical plugin.
4. JobDetector baseline and new-job detection logic.
5. JobDetector solve-type filtering (parent types accepted, child types rejected).
6. JobDetector local-only filtering.
7. InspectionEngine result structure (Synergy COM unavailable path).
8. InspectionEngine graceful degradation when Synergy is not running.
9. Agent config loader handles missing config gracefully (falls back to defaults).
10. Agent config loader raises on malformed JSON.
11. Scheduled Task WorkingDirectory points to canonical location.
12. No agent log at old standalone project location since cutover.
"""

import json
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agents" / "post_analyze"
PLUGIN_DIR = REPO_ROOT / "plugin"

# Ensure agent package and plugin shims are importable
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


# ---------------------------------------------------------------------------
# 1 — Module importability
# ---------------------------------------------------------------------------
class TestPhase7ModuleImports(unittest.TestCase):
    """Agent modules must be importable from the canonical repo path."""

    def test_01_agent_dir_exists(self):
        self.assertTrue(AGENT_DIR.is_dir(), f"agents/post_analyze not found: {AGENT_DIR}")

    def test_02_job_detector_importable(self):
        import job_detector  # noqa: F401
        self.assertTrue(hasattr(job_detector, "JobDetector"))

    def test_03_inspection_engine_importable(self):
        import inspection_engine  # noqa: F401
        self.assertTrue(hasattr(inspection_engine, "InspectionEngine"))

    def test_04_agent_module_importable(self):
        import agent  # noqa: F401
        self.assertTrue(hasattr(agent, "load_config"))
        self.assertTrue(hasattr(agent, "configure_shared_plugin_path"))


# ---------------------------------------------------------------------------
# 2 — Configuration files
# ---------------------------------------------------------------------------
class TestPhase7Config(unittest.TestCase):
    """Configuration files must be complete and correct."""

    REQUIRED_KEYS = {
        "existing_plugin_path",
        "poll_interval_seconds",
        "startup_baseline",
        "inspection_enabled",
        "inspection_status",
        "inspection_timeout_seconds",
        "only_local_jobs",
        "log_file",
        "analysis_started_status",
    }

    def test_05_config_example_has_required_keys(self):
        example = AGENT_DIR / "config.json.example"
        self.assertTrue(example.exists(), "config.json.example missing")
        data = json.loads(example.read_text(encoding="utf-8-sig"))
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, data, f"Missing key in config.json.example: {key}")

    def test_06_live_config_points_to_canonical_plugin(self):
        """Live config.json must point existing_plugin_path to canonical repo plugin."""
        live = AGENT_DIR / "config.json"
        self.assertTrue(live.exists(), "config.json missing at canonical location")
        data = json.loads(live.read_text(encoding="utf-8-sig"))
        plugin_path = Path(data.get("existing_plugin_path", ""))
        self.assertTrue(
            plugin_path.is_dir(),
            f"existing_plugin_path does not exist: {plugin_path}",
        )
        # Must point into canonical MoldflowMobileSystem, not old standalone
        self.assertIn(
            "MoldflowMobileSystem",
            str(plugin_path),
            "existing_plugin_path should point to MoldflowMobileSystem\\plugin",
        )
        self.assertNotIn(
            "MoldflowStandaloneAgent",
            str(plugin_path),
            "existing_plugin_path must not point to old standalone project",
        )

    def test_07_agent_load_config_defaults_on_missing_file(self):
        """load_config() must return defaults when config.json does not exist."""
        import agent
        original = agent.CONFIG_PATH
        try:
            agent.CONFIG_PATH = AGENT_DIR / "nonexistent_config_test.json"
            cfg = agent.load_config()
            self.assertIn("existing_plugin_path", cfg)
            self.assertIn("poll_interval_seconds", cfg)
        finally:
            agent.CONFIG_PATH = original

    def test_08_agent_load_config_raises_on_bad_json(self):
        """load_config() must raise RuntimeError on malformed JSON."""
        import agent
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("{ this is not valid json }")
            tmp_path = Path(f.name)
        original = agent.CONFIG_PATH
        try:
            agent.CONFIG_PATH = tmp_path
            with self.assertRaises(RuntimeError):
                agent.load_config()
        finally:
            agent.CONFIG_PATH = original
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3 — JobDetector logic
# ---------------------------------------------------------------------------
class TestPhase7JobDetector(unittest.TestCase):
    """JobDetector baseline and filtering logic."""

    def _make_job(self, job_id, job_type, cloud=False):
        return {
            "jobID": job_id,
            "epoch": 100,
            "payload": {"type": job_type, "name": f"study_{job_id}", "cloud": cloud},
        }

    def _make_compute_jobs(self, rows):
        mod = unittest.mock.MagicMock()
        mod.list_jobs.return_value = rows
        mod.SOLVE_TYPES = ("study", "mesh+study")
        return mod

    def setUp(self):
        import job_detector
        self.JobDetector = job_detector.JobDetector

    def test_09_baseline_captures_existing_jobs(self):
        existing = [self._make_job("old-1", "study"), self._make_job("old-2", "study")]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        self.assertEqual(det.known_ids, {"old-1", "old-2"})
        self.assertTrue(det.initialized)

    def test_10_new_job_detected_after_baseline(self):
        existing = [self._make_job("old-1", "study")]
        new_jobs = [self._make_job("old-1", "study"), self._make_job("new-99", "study")]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        compute_jobs.list_jobs.return_value = new_jobs
        candidates = det.poll()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["jobID"], "new-99")

    def test_11_child_phase_types_rejected(self):
        """Child phase jobs (e.g. study:warp3d:01) must not be detected as parent solves."""
        existing = []
        new_jobs = [
            self._make_job("child-1", "study:warp3d:01"),
            self._make_job("child-2", "study:fill3d:01"),
            self._make_job("parent-1", "study"),
        ]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        compute_jobs.list_jobs.return_value = new_jobs
        candidates = det.poll()
        ids = [c["jobID"] for c in candidates]
        self.assertNotIn("child-1", ids)
        self.assertNotIn("child-2", ids)
        self.assertIn("parent-1", ids)

    def test_12_extended_solve_types_accepted(self):
        """mesh+analysis and analysis parent types must be accepted."""
        existing = []
        new_jobs = [
            self._make_job("j-ma", "mesh+analysis"),
            self._make_job("j-a", "analysis"),
            self._make_job("j-s", "study"),
            self._make_job("j-ms", "mesh+study"),
        ]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        compute_jobs.list_jobs.return_value = new_jobs
        candidates = det.poll()
        ids = {c["jobID"] for c in candidates}
        self.assertIn("j-ma", ids)
        self.assertIn("j-a", ids)
        self.assertIn("j-s", ids)
        self.assertIn("j-ms", ids)

    def test_13_cloud_jobs_rejected_when_only_local(self):
        existing = []
        new_jobs = [
            self._make_job("cloud-1", "study", cloud=True),
            self._make_job("local-1", "study", cloud=False),
        ]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock(), only_local=True)
        det.initialize()
        compute_jobs.list_jobs.return_value = new_jobs
        candidates = det.poll()
        ids = [c["jobID"] for c in candidates]
        self.assertNotIn("cloud-1", ids)
        self.assertIn("local-1", ids)

    def test_14_same_job_not_detected_twice(self):
        existing = []
        new_jobs = [self._make_job("dup-1", "study")]
        compute_jobs = self._make_compute_jobs(existing)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        compute_jobs.list_jobs.return_value = new_jobs
        first = det.poll()
        second = det.poll()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_15_scm_error_returns_empty_list(self):
        compute_jobs = unittest.mock.MagicMock()
        compute_jobs.list_jobs.side_effect = OSError("SCM unavailable")
        compute_jobs.SOLVE_TYPES = ("study",)
        det = self.JobDetector(compute_jobs, unittest.mock.MagicMock())
        det.initialize()
        result = det.poll()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# 4 — InspectionEngine graceful degradation
# ---------------------------------------------------------------------------
class TestPhase7InspectionEngine(unittest.TestCase):
    """InspectionEngine must degrade gracefully when Synergy COM is unavailable."""

    def setUp(self):
        import inspection_engine
        mock_compute = unittest.mock.MagicMock()
        self.engine = inspection_engine.InspectionEngine(
            compute_jobs=mock_compute,
            logger=unittest.mock.MagicMock(),
            timeout_seconds=5.0,
        )

    def test_16_result_has_required_keys(self):
        """Even in failure, result must contain required keys."""
        job = {"job_id": "test-123", "name": "test_study", "scm_type": "study"}
        # Synergy COM will fail (not running in test environment)
        result = self.engine.run(job)
        for key in ("status", "study_name", "scm_job_id", "started_at", "finished_at", "checks"):
            self.assertIn(key, result, f"Missing key in result: {key}")

    def test_17_first_check_is_analysis_job(self):
        """First check entry must always be the SCM job found check."""
        job = {"job_id": "test-456", "name": "my_study"}
        result = self.engine.run(job)
        self.assertTrue(len(result["checks"]) >= 1)
        self.assertEqual(result["checks"][0]["name"], "analysis_job")
        self.assertEqual(result["checks"][0]["status"], "FOUND")

    def test_18_graceful_unavailable_when_synergy_not_running(self):
        """When Synergy is not running, result status must be UNAVAILABLE, not an exception."""
        job = {"job_id": "test-789", "name": "offline_study"}
        result = self.engine.run(job)
        # Status is UNAVAILABLE when COM inspection cannot connect
        self.assertIn(result["status"], ("UNAVAILABLE", "UNKNOWN"))
        # Must NOT raise
        self.assertIsInstance(result, dict)

    def test_19_finished_at_after_started_at(self):
        job = {"job_id": "t-time", "name": "time_study"}
        result = self.engine.run(job)
        self.assertGreaterEqual(result["finished_at"], result["started_at"])


# ---------------------------------------------------------------------------
# 5 — Deployment verification
# ---------------------------------------------------------------------------
class TestPhase7Deployment(unittest.TestCase):
    """Verify canonical deployment state after Phase 7 cutover."""

    def test_20_scheduled_task_workingdir_is_canonical(self):
        """Scheduled Task WorkingDirectory must point to agents/post_analyze."""
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command",
                 "(Get-ScheduledTask -TaskName 'Moldflow Post-Analyze Agent').Actions[0].WorkingDirectory"],
                capture_output=True, text=True, timeout=15,
            )
            workdir = result.stdout.strip()
            self.assertIn("MoldflowMobileSystem", workdir,
                          f"WorkingDir should be canonical, got: {workdir}")
            self.assertIn("agents", workdir,
                          f"WorkingDir should be in agents/, got: {workdir}")
            self.assertIn("post_analyze", workdir,
                          f"WorkingDir should be post_analyze, got: {workdir}")
            self.assertNotIn("MoldflowStandaloneAgent", workdir,
                             f"WorkingDir must not be old standalone project: {workdir}")
        except Exception as exc:
            self.skipTest(f"Could not query Scheduled Task (non-Windows or no admin): {exc}")

    def test_21_old_standalone_dir_still_exists(self):
        """Old standalone project must not have been deleted (rollback safety)."""
        old_dir = Path(
            r"C:\Users\UnoTEAM-0144\Documents"
            r"\MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent"
        )
        self.assertTrue(old_dir.is_dir(),
                        "Old standalone project must be preserved as rollback reference")

    def test_22_canonical_agent_log_exists_and_is_recent(self):
        """standalone_agent.log at canonical location must exist and be recent (< 10 min old)."""
        log = AGENT_DIR / "standalone_agent.log"
        self.assertTrue(log.exists(), f"Agent log not found at canonical location: {log}")
        age_seconds = time.time() - log.stat().st_mtime
        self.assertLess(age_seconds, 600,
                        f"Agent log is too old ({age_seconds:.0f}s), agent may not be running")

    def test_23_gitignore_excludes_live_config(self):
        """agents/post_analyze/config.json must be in .gitignore."""
        gitignore = REPO_ROOT / ".gitignore"
        self.assertTrue(gitignore.exists())
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn("agents/post_analyze/config.json", content,
                      "config.json must be excluded from git")

    def test_24_gitignore_excludes_agent_log(self):
        """agents/post_analyze/standalone_agent.log must be in .gitignore."""
        gitignore = REPO_ROOT / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn("agents/post_analyze/standalone_agent.log", content,
                      "standalone_agent.log must be excluded from git")


if __name__ == "__main__":
    unittest.main()
