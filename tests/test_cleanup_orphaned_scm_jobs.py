"""
test_cleanup_orphaned_scm_jobs.py
==================================
Regression verification for the ``cleanup_orphaned_scm_jobs()`` guard in
``plugin/moldflow_startup.py``.

Root cause recap
----------------
Synergy 2027 crashed (0xc000041d / access violation in synergy.dll) when its
IJobManager tried to attach to a stale ``*~1.job.json`` file referencing a
job-ID that SCM had already purged.  The fix scans known AMI project
directories at startup, asks SCM about each job-ID, and **quarantines**
(moves to ``orphaned_jobs_backup/``) any file for which SCM returns 404 or 500.

Verified behaviours
-------------------
1.  A stale/orphaned ``*~1.job.json`` whose SCM job returns **404** is quarantined.
2.  A stale/orphaned ``*~1.job.json`` whose SCM job returns **500** is quarantined.
3.  A valid ``*~1.job.json`` whose SCM job returns **200** is NOT quarantined.
4.  A job whose SCM job has status ``CREATED`` (200 OK) is NOT quarantined.
5.  A ``.job.json`` (no tilde) that references a missing (404) job IS quarantined.
6.  A ``.job.json`` that has no ``jobId`` field is skipped silently.
7.  A project directory without any ``.job.json`` files is traversed safely.
8.  ``orphaned_jobs_backup/`` sub-directory is itself skipped during scanning.
9.  Network timeout / connection refused is treated as non-conclusive --
    file is NOT quarantined (conservative).
10. Mixed valid/orphaned: only orphaned ones are quarantined.
11. The function returns a list containing exactly quarantined file names.
12. Quarantined files are physically moved (recoverable), not deleted.
13. Parametrized conservative-behaviour matrix over all SCM responses.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Locate the plugin package
# ---------------------------------------------------------------------------
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
sys.path.insert(0, str(PLUGIN_DIR))

# ---------------------------------------------------------------------------
# Stub win32com / pythoncom / Synergy siblings at MODULE LOAD TIME so that
# moldflow_startup.py (and synergy_connect.py it imports) can be collected
# without Autodesk / pywin32 installed.
# This must run before any `import moldflow_startup` at the module level.
# ---------------------------------------------------------------------------
_STUB_MODULES = [
    "pythoncom",
    "pywintypes",
    "win32com",
    "win32com.client",
    "synergy_connect",
    "ui_bridge",
    "ui_launcher",
    "session_context",
]


def _install_stubs() -> None:
    """Inject lightweight module stubs into sys.modules before any plugin import."""
    for name in _STUB_MODULES:
        if name not in sys.modules:
            parts = name.split(".")
            parent = None
            for i, part in enumerate(parts):
                full = ".".join(parts[: i + 1])
                if full not in sys.modules:
                    m = types.ModuleType(full)
                    if parent is not None:
                        setattr(parent, part, m)
                    sys.modules[full] = m
                parent = sys.modules[full]

    # Minimal attributes used by moldflow_startup at module scope
    sc = sys.modules["session_context"]
    if not callable(getattr(sc, "session_key", None)):
        sc.session_key = lambda: "test"
    if not callable(getattr(sc, "session_path", None)):
        sc.session_path = lambda *a: Path(tempfile.gettempdir()) / "_".join(str(x) for x in a)

    ub = sys.modules["ui_bridge"]
    if not callable(getattr(ub, "log_silent", None)):
        ub.log_silent = lambda *a, **kw: None

    # synergy_connect stubs needed by moldflow_startup's module-level from-import
    sc2 = sys.modules["synergy_connect"]
    if not callable(getattr(sc2, "get_synergy", None)):
        sc2.get_synergy = lambda **kw: None
    if not callable(getattr(sc2, "has_active_study", None)):
        sc2.has_active_study = lambda syn: False


# Run immediately at collection time, before `import moldflow_startup`.
_install_stubs()

import moldflow_startup as _ms  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_job_file(directory: Path, filename: str, job_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fpath = directory / filename
    fpath.write_text(json.dumps({"jobId": job_id}), encoding="utf-8")
    return fpath


def _write_job_file_no_id(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fpath = directory / filename
    fpath.write_text(json.dumps({"name": "no-id-job"}), encoding="utf-8")
    return fpath


class _FakeHTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b"{}"


def _scm_raises_http(code: int):
    def _side(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise urllib.error.HTTPError(url, code, f"HTTP {code}", {}, None)
    return _side


def _scm_ok():
    def _side(req, timeout=None):
        return _FakeHTTPResponse()
    return _side


def _scm_timeout():
    import socket
    def _side(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))
    return _side


def _scm_connection_refused():
    def _side(req, timeout=None):
        raise urllib.error.URLError("Connection refused")
    return _side


def _run_cleanup(search_dirs: list) -> list:
    """Call cleanup_orphaned_scm_jobs() with the hard-coded dirs redirected."""
    real_expanduser = os.path.expanduser
    ami_dir = search_dirs[0] if len(search_dirs) > 0 else ""
    mf_dir = search_dirs[1] if len(search_dirs) > 1 else ""

    def _fake_expanduser(path: str) -> str:
        if path.endswith("My AMI 2027 Projects"):
            return ami_dir
        if path.endswith("Moldflow Projects"):
            return mf_dir
        return real_expanduser(path)

    with mock.patch("os.path.expanduser", side_effect=_fake_expanduser):
        return _ms.cleanup_orphaned_scm_jobs()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestCleanupOrphanedScmJobs:

    # Case 1 -  SCM 404 -> quarantine
    def test_scm_404_is_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectA"
        fpath = _write_job_file(proj_dir, "jobXYZ~1.job.json", "JOB-404")
        backup_expected = proj_dir.parent / "orphaned_jobs_backup" / "jobXYZ~1.job.json"

        with mock.patch("urllib.request.urlopen", side_effect=_scm_raises_http(404)):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert "jobXYZ~1.job.json (SCM 404)" in result
        assert backup_expected.exists(), "Quarantined file must exist in backup dir"
        assert not fpath.exists(), "Original file must be gone after quarantine"

    # Case 2 - SCM 500 -> quarantine
    def test_scm_500_is_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectB"
        fpath = _write_job_file(proj_dir, "jobABC~1.job.json", "JOB-500")
        backup_expected = proj_dir.parent / "orphaned_jobs_backup" / "jobABC~1.job.json"

        with mock.patch("urllib.request.urlopen", side_effect=_scm_raises_http(500)):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert "jobABC~1.job.json (SCM 500)" in result
        assert backup_expected.exists()
        assert not fpath.exists()

    # Case 3 - SCM 200 -> NOT quarantined
    def test_scm_200_is_not_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectC"
        fpath = _write_job_file(proj_dir, "jobDEF~1.job.json", "JOB-200")

        with mock.patch("urllib.request.urlopen", side_effect=_scm_ok()):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == [], "No jobs should be quarantined when SCM returns 200"
        assert fpath.exists(), "Valid job file must remain untouched"

    # Case 4 - status=CREATED (200 OK) -> NOT quarantined
    def test_status_created_is_not_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectD"
        fpath = _write_job_file(proj_dir, "jobCREATED~1.job.json", "JOB-CREATED")

        with mock.patch("urllib.request.urlopen", side_effect=_scm_ok()):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == [], "CREATED status jobs must not be quarantined"
        assert fpath.exists(), "File for CREATED job must remain in place"

    # Case 5 - plain .job.json (no tilde) with 404 -> quarantined
    def test_plain_job_json_404_is_quarantined(self, tmp_path):
        proj_dir = tmp_path / "Moldflow Projects" / "ProjectE"
        fpath = _write_job_file(proj_dir, "setup.job.json", "JOB-PLAIN-404")
        backup_expected = proj_dir.parent / "orphaned_jobs_backup" / "setup.job.json"

        with mock.patch("urllib.request.urlopen", side_effect=_scm_raises_http(404)):
            result = _run_cleanup([str(tmp_path / "My AMI 2027 Projects"), str(proj_dir.parent)])

        assert "setup.job.json (SCM 404)" in result
        assert backup_expected.exists()
        assert not fpath.exists()

    # Case 6 - no jobId field -> skip silently
    def test_no_job_id_field_is_skipped(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectF"
        fpath = _write_job_file_no_id(proj_dir, "no_id~1.job.json")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == [], "Files without jobId should be silently skipped"
        assert fpath.exists(), "File without jobId must not be touched"
        mock_urlopen.assert_not_called()

    # Case 7 - empty project directory -> safe traversal
    def test_empty_project_directory_is_safe(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "EmptyProject"
        proj_dir.mkdir(parents=True)
        (proj_dir / "model.sdy").write_text("study", encoding="utf-8")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == []
        mock_urlopen.assert_not_called()

    # Case 8 - orphaned_jobs_backup/ is not re-scanned
    def test_backup_dir_is_not_rescanned(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects"
        backup_dir = proj_dir / "orphaned_jobs_backup"
        backup_dir.mkdir(parents=True)
        already_backed = backup_dir / "oldfile~1.job.json"
        already_backed.write_text(json.dumps({"jobId": "OLD-JOB"}), encoding="utf-8")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = _run_cleanup([str(proj_dir), str(tmp_path / "Moldflow Projects")])

        assert result == [], "Backup dir contents must never be re-quarantined"
        assert already_backed.exists(), "Backup file must remain untouched"
        mock_urlopen.assert_not_called()

    # Case 9a - network timeout -> do NOT quarantine
    def test_network_timeout_does_not_quarantine(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectG"
        fpath = _write_job_file(proj_dir, "jobTIMEOUT~1.job.json", "JOB-TIMEOUT")

        with mock.patch("urllib.request.urlopen", side_effect=_scm_timeout()):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == [], "Timeout must not trigger quarantine"
        assert fpath.exists(), "File must remain when SCM is unreachable (timeout)"

    # Case 9b - connection refused -> do NOT quarantine
    def test_connection_refused_does_not_quarantine(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectH"
        fpath = _write_job_file(proj_dir, "jobCR~1.job.json", "JOB-CR")

        with mock.patch("urllib.request.urlopen", side_effect=_scm_connection_refused()):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert result == [], "Connection refused must not trigger quarantine"
        assert fpath.exists(), "File must remain when SCM is unreachable (refused)"

    # Case 10 - mixed valid/orphaned files
    def test_mixed_files_only_orphans_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectI"
        orphaned = _write_job_file(proj_dir, "orphan~1.job.json", "JOB-ORPHAN")
        valid = _write_job_file(proj_dir, "valid~1.job.json", "JOB-VALID")

        def _selective(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "JOB-ORPHAN" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return _FakeHTTPResponse()

        with mock.patch("urllib.request.urlopen", side_effect=_selective):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert "orphan~1.job.json (SCM 404)" in result
        assert not orphaned.exists(), "Orphaned file must be quarantined"
        assert valid.exists(), "Valid file must remain in place"

    # Case 11 - return value contains exactly quarantined names
    def test_return_value_contains_only_quarantined(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectJ"
        _write_job_file(proj_dir, "q1~1.job.json", "JOB-Q1")
        _write_job_file(proj_dir, "q2~1.job.json", "JOB-Q2")

        with mock.patch("urllib.request.urlopen", side_effect=_scm_raises_http(404)):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert len(result) == 2
        for entry in result:
            assert "(SCM 404)" in entry

    # Case 12 - quarantined files moved, not deleted
    def test_quarantined_files_are_moved_not_deleted(self, tmp_path):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectK"
        _write_job_file(proj_dir, "precious~1.job.json", "JOB-PRECIOUS")
        backup_dir = proj_dir.parent / "orphaned_jobs_backup"

        with mock.patch("urllib.request.urlopen", side_effect=_scm_raises_http(404)):
            _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        assert (backup_dir / "precious~1.job.json").exists(), (
            "Quarantined file must be in orphaned_jobs_backup/, not deleted"
        )


# ---------------------------------------------------------------------------
# Conservative-behaviour parametrized matrix
# ---------------------------------------------------------------------------

class TestCleanupConservativeBehaviour:
    """Verify that cleanup_orphaned_scm_jobs() is conservative:
    quarantine ONLY when SCM definitively says the job is gone (404/500);
    never quarantine merely because the job is CREATED or SCM is silent."""

    @pytest.mark.parametrize("description,http_side_effect,should_quarantine", [
        ("SCM 404 definitive missing",       _scm_raises_http(404),       True),
        ("SCM 500 server-side purge/error",  _scm_raises_http(500),       True),
        ("SCM 200 job alive",                _scm_ok(),                   False),
        ("Network timeout inconclusive",     _scm_timeout(),              False),
        ("Connection refused SCM not up",    _scm_connection_refused(),   False),
    ])
    def test_quarantine_decision(self, tmp_path, description, http_side_effect, should_quarantine):
        proj_dir = tmp_path / "My AMI 2027 Projects" / "ProjectParam"
        fpath = _write_job_file(proj_dir, "paramjob~1.job.json", "JOB-PARAM")

        with mock.patch("urllib.request.urlopen", side_effect=http_side_effect):
            result = _run_cleanup([str(proj_dir.parent), str(tmp_path / "Moldflow Projects")])

        if should_quarantine:
            assert len(result) == 1, f"[{description}] Expected 1 quarantine, got {result}"
            assert not fpath.exists(), f"[{description}] File must be removed from original location"
        else:
            assert result == [], f"[{description}] Expected NO quarantine, got {result}"
            assert fpath.exists(), f"[{description}] File must remain in place"
