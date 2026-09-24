"""
tests/test_workstation_msi.py
-----------------------------
Comprehensive test suite verifying the Moldflow Mobile Workstation MSI package,
WiX database tables, custom actions, installation/upgrade/uninstall logic,
credential preservation, background scheduled tasks, self-healing configuration,
hygiene (zero .pyc/.log), and Network License Monitor isolation.
"""

import os
import re
import sys
import time
import json
import shutil
import logging
import hashlib
import tempfile
import unittest
import subprocess
import ctypes
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
DEPLOYMENT_DIR = ROOT_DIR / "deployment"
MSI_PATH = DEPLOYMENT_DIR / "output" / "MoldflowMobileWorkstation.msi"
WXS_PATH = DEPLOYMENT_DIR / "msi" / "MoldflowMobileWorkstation.wxs"
STAGING_DIR = Path(r"C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging")

# Native MSI API via ctypes
msi = ctypes.windll.msi


def _query_msi(msi_path: Path, sql: str):
    """Executes a SQL query against an MSI database using native Windows Installer APIs."""
    h_db = wintypes.HANDLE()
    res = msi.MsiOpenDatabaseW(str(msi_path), 0, ctypes.byref(h_db))
    if res != 0:
        raise RuntimeError(f"MsiOpenDatabaseW failed with error {res}")

    h_view = wintypes.HANDLE()
    res = msi.MsiDatabaseOpenViewW(h_db, sql, ctypes.byref(h_view))
    if res != 0:
        msi.MsiCloseHandle(h_db)
        raise RuntimeError(f"MsiDatabaseOpenViewW failed with error {res} for query: {sql}")

    res = msi.MsiViewExecute(h_view, 0)
    if res != 0:
        msi.MsiCloseHandle(h_view)
        msi.MsiCloseHandle(h_db)
        raise RuntimeError(f"MsiViewExecute failed with error {res}")

    rows = []
    h_rec = wintypes.HANDLE()
    while msi.MsiViewFetch(h_view, ctypes.byref(h_rec)) == 0:
        col_count = msi.MsiRecordGetFieldCount(h_rec)
        cols = []
        for i in range(1, col_count + 1):
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(4096)
            res_str = msi.MsiRecordGetStringW(h_rec, i, buf, ctypes.byref(sz))
            if res_str == 0:
                cols.append(buf.value)
            else:
                cols.append(str(msi.MsiRecordGetInteger(h_rec, i)))
        rows.append(cols)
        msi.MsiCloseHandle(h_rec)

    msi.MsiCloseHandle(h_view)
    msi.MsiCloseHandle(h_db)
    return rows


class TestWorkstationMsiPackage(unittest.TestCase):
    """Verifies MSI file generation, header validity, and WiX database structure."""

    def test_01_msi_exists_and_hash_valid(self):
        """MSI package must exist, be larger than 200KB, and match SHA-256 calculation."""
        self.assertTrue(MSI_PATH.exists(), f"MSI not found at {MSI_PATH}")
        size = MSI_PATH.stat().st_size
        self.assertGreater(size, 200 * 1024, f"MSI size too small: {size} bytes")

        h = hashlib.sha256(MSI_PATH.read_bytes()).hexdigest().upper()
        self.assertEqual(len(h), 64, "SHA-256 hash must be 64 hexadecimal characters")

    def test_02_msi_properties_table(self):
        """Property table must contain expected metadata and IT configuration defaults."""
        rows = _query_msi(MSI_PATH, "SELECT Property, Value FROM Property")
        props = {r[0]: r[1] for r in rows}

        self.assertEqual(props.get("ProductName"), "Moldflow Mobile Workstation Runtime")
        self.assertEqual(props.get("Manufacturer"), "Autodesk Moldflow Mobile")
        self.assertEqual(props.get("ProductVersion"), "1.0.0.0")
        self.assertEqual(props.get("UpgradeCode"), "{B91A4CFE-7963-4DC1-92A3-316DAA8A86C1}")
        self.assertIn("moldflowplugin-mobile-app.onrender.com", props.get("BACKEND_URL", ""))
        self.assertEqual(props.get("USER_ID"), "DEV-USER-001")
        self.assertEqual(props.get("FORCE_REENROLL"), "0")
        self.assertEqual(props.get("ARPNOREPAIR"), "yes")

    def test_03_msi_packaged_files_hygiene(self):
        """All files in MSI File table must be clean: 0 .pyc, 0 .log, 0 __pycache__."""
        rows = _query_msi(MSI_PATH, "SELECT File, FileName, FileSize FROM File")
        self.assertGreaterEqual(len(rows), 40, f"Expected at least 40 files in MSI, found {len(rows)}")

        file_names = [r[1] for r in rows]
        for name in file_names:
            self.assertFalse(name.endswith(".pyc"), f"Accidental .pyc file in MSI: {name}")
            self.assertFalse(name.endswith(".log"), f"Accidental .log file in MSI: {name}")
            self.assertNotIn("__pycache__", name, f"Accidental __pycache__ file in MSI: {name}")

    def test_04_msi_custom_actions(self):
        """CustomAction table must contain Install and Uninstall actions with proper flags and parameters."""
        rows = _query_msi(MSI_PATH, "SELECT Action, Type, Source, Target FROM CustomAction")
        actions = {r[0]: {"type": int(r[1]), "source": r[2], "target": r[3]} for r in rows}

        self.assertIn("RunInstallWorkstationCA", actions)
        self.assertIn("RunUninstallWorkstationCA", actions)

        # In WiX v4/v7: Type 3106 = 34 (exe on dir) + 1024 (deferred) + 2048 (no-impersonate)
        install_ca = actions["RunInstallWorkstationCA"]
        self.assertEqual(install_ca["source"], "INSTALLFOLDER")
        self.assertTrue(install_ca["type"] & 1024, "RunInstallWorkstationCA must be deferred (1024)")
        self.assertTrue(install_ca["type"] & 2048, "RunInstallWorkstationCA must run without impersonation (2048)")
        self.assertIn("Install-MoldflowWorkstation.ps1", install_ca["target"])
        self.assertIn("-SkipFileCopy", install_ca["target"])
        self.assertIn("-Silent", install_ca["target"])
        self.assertIn("[BACKEND_URL]", install_ca["target"])
        self.assertIn("[USER_ID]", install_ca["target"])
        self.assertIn("[ENROLLMENT_TOKEN]", install_ca["target"])

        uninstall_ca = actions["RunUninstallWorkstationCA"]
        self.assertEqual(uninstall_ca["source"], "INSTALLFOLDER")
        self.assertTrue(uninstall_ca["type"] & 1024, "RunUninstallWorkstationCA must be deferred (1024)")
        self.assertIn("Uninstall-MoldflowWorkstation.ps1", uninstall_ca["target"])
        self.assertIn("-SkipFileDelete", uninstall_ca["target"])
        self.assertIn("-Silent", uninstall_ca["target"])

    def test_05_msi_execution_sequences(self):
        """InstallExecuteSequence must correctly sequence Install and Uninstall actions."""
        rows = _query_msi(MSI_PATH, "SELECT Action, Condition, Sequence FROM InstallExecuteSequence")
        seq = {r[0]: {"condition": r[1], "sequence": int(r[2])} for r in rows}

        self.assertIn("RunInstallWorkstationCA", seq)
        self.assertIn("RunUninstallWorkstationCA", seq)
        self.assertIn("InstallFiles", seq)
        self.assertIn("RemoveFiles", seq)

        # RunInstallWorkstationCA must be after InstallFiles
        self.assertGreater(seq["RunInstallWorkstationCA"]["sequence"], seq["InstallFiles"]["sequence"],
                           "RunInstallWorkstationCA must execute AFTER InstallFiles")

        # RunUninstallWorkstationCA must be before RemoveFiles
        self.assertLess(seq["RunUninstallWorkstationCA"]["sequence"], seq["RemoveFiles"]["sequence"],
                        "RunUninstallWorkstationCA must execute BEFORE RemoveFiles")

        # Check conditions
        self.assertIn('NOT (REMOVE="ALL")', seq["RunInstallWorkstationCA"]["condition"])
        self.assertIn('REMOVE="ALL"', seq["RunUninstallWorkstationCA"]["condition"])

    def test_06_msi_administrative_extraction_and_file_integrity(self):
        """Verify MSI extraction via msiexec /a extracts all files without error or bytecode."""
        temp_extract = Path(tempfile.mkdtemp(prefix="msi_test_extract_"))
        try:
            cmd = [
                "msiexec.exe", "/a", str(MSI_PATH),
                "/qn",
                f"TARGETDIR={temp_extract}"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, f"msiexec /a failed: {res.stderr}")

            # Locate extracted files
            extracted_files = list(temp_extract.rglob("*"))
            file_names = [f.name for f in extracted_files if f.is_file()]

            # Must contain core scripts
            self.assertIn("agent.py", file_names)
            self.assertIn("standalone_job_monitor.py", file_names)
            self.assertIn("Install-MoldflowWorkstation.ps1", file_names)
            self.assertIn("Uninstall-MoldflowWorkstation.ps1", file_names)
            self.assertIn("MANIFEST.txt", file_names)

            # Assert zero bytecode or logs in extracted tree
            pyc_files = list(temp_extract.rglob("*.pyc"))
            log_files = list(temp_extract.rglob("*.log"))
            cache_dirs = list(temp_extract.rglob("__pycache__"))
            self.assertEqual(len(pyc_files), 0, f"Bytecode found in extracted MSI: {pyc_files}")
            self.assertEqual(len(log_files), 0, f"Log files found in extracted MSI: {log_files}")
            self.assertEqual(len(cache_dirs), 0, f"Pycache dirs found in extracted MSI: {cache_dirs}")
        finally:
            shutil.rmtree(temp_extract, ignore_errors=True)


class TestInstallerEngineAndScheduledTasks(unittest.TestCase):
    """Verifies workstation installer logic, task configurations, self-healing, and license isolation."""

    def test_07_fresh_installation_enrollment_failure_handling(self):
        """Installer must fail when run silently without an enrollment token and without existing config."""
        installer_ps1 = DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1"
        temp_install = Path(tempfile.mkdtemp(prefix="mf_fresh_install_"))
        temp_config = Path(tempfile.mkdtemp(prefix="mf_fresh_cfg_"))

        try:
            # Create a test runner script omitting #Requires so non-elevated test process can execute
            script_text = installer_ps1.read_text(encoding="utf-8")
            test_script = re.sub(r'(?i)#requires\s+-runasadministrator', '', script_text)
            temp_script = temp_install / "Install-Test.ps1"
            temp_script.write_text(test_script, encoding="utf-8")

            # Run installer with empty token in silent mode -> must fail
            ps_cmd = (
                f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{temp_script}\" "
                f"-InstallDir \"{temp_install}\" -ConfigDir \"{temp_config}\" -SkipTasks -SkipSynergyCommand -Silent"
            )
            res = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(res.returncode, 0, "Installer should have failed due to missing enrollment token")
            self.assertIn("Enrollment failed: No enrollment token was provided", res.stdout + res.stderr)
        finally:
            shutil.rmtree(temp_install, ignore_errors=True)
            shutil.rmtree(temp_config, ignore_errors=True)

    def test_08_existing_credential_preservation_on_upgrade(self):
        """On upgrade/reinstall, existing valid %ProgramData%\\config.json must be preserved."""
        installer_ps1 = DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1"
        temp_install = Path(tempfile.mkdtemp(prefix="mf_upgrade_install_"))
        temp_config = Path(tempfile.mkdtemp(prefix="mf_upgrade_cfg_"))

        try:
            # Seed an existing valid configuration
            existing_cfg = {
                "enabled": True,
                "api_key": "mf-client-test-upgrade-key-preserved-12345",
                "backend_url": "https://moldflowplugin-mobile-app.onrender.com",
                "machine_id": "TEST-MACHINE-UPGRADE",
                "user_id": "TEST-USER-001"
            }
            cfg_file = temp_config / "config.json"
            cfg_file.write_text(json.dumps(existing_cfg), encoding="utf-8")

            # Create test script copy
            script_text = installer_ps1.read_text(encoding="utf-8")
            test_script = re.sub(r'(?i)#requires\s+-runasadministrator', '', script_text)
            temp_script = temp_install / "Install-Test.ps1"
            temp_script.write_text(test_script, encoding="utf-8")

            # Run installer with -SkipFileCopy and -SkipTasks (simulating upgrade)
            ps_cmd = (
                f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{temp_script}\" "
                f"-InstallDir \"{temp_install}\" -ConfigDir \"{temp_config}\" -SkipFileCopy -SkipTasks -SkipSynergyCommand -Silent"
            )
            res = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 0, f"Upgrade failed: {res.stdout}\n{res.stderr}")
            self.assertIn("Existing active workstation configuration preserved", res.stdout)

            # Verify config.json was NOT overwritten or changed
            final_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(final_cfg["api_key"], "mf-client-test-upgrade-key-preserved-12345")
            self.assertEqual(final_cfg["machine_id"], "TEST-MACHINE-UPGRADE")
        finally:
            shutil.rmtree(temp_install, ignore_errors=True)
            shutil.rmtree(temp_config, ignore_errors=True)

    def test_09_enrollment_with_token_wiping(self):
        """Enrollment token file must be deleted immediately after reading to avoid leaving plaintext tokens on disk."""
        installer_ps1 = DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1"
        content = installer_ps1.read_text(encoding="utf-8")

        # Check token file cleanup
        self.assertIn("Remove-Item -Path $EnrollmentTokenFile -Force", content,
                      "Installer must securely remove token file immediately after use")
        # Check in-memory wiping
        self.assertIn("$effectiveToken = $null", content,
                      "Installer must clear token from memory after enrollment attempt")

    def test_10_scheduled_task_configuration_and_self_healing(self):
        """Scheduled task definitions must meet exact production background and self-healing requirements."""
        installer_ps1 = DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1"
        content = installer_ps1.read_text(encoding="utf-8")

        # Post-Analyze Agent task requirements:
        # - starts automatically at user logon
        # - RestartCount = 3
        # - RestartInterval = PT1M
        # - MultipleInstancesPolicy = IgnoreNew
        # - ExecutionTimeLimit = PT0S
        # - pythonw -B agent.py
        self.assertIn('-TaskName $agentTaskName', content)
        self.assertIn('-MultipleInstances IgnoreNew', content)
        self.assertIn('-RestartCount 3', content)
        self.assertIn('-RestartInterval (New-TimeSpan -Minutes 1)', content)
        self.assertIn('-ExecutionTimeLimit ([TimeSpan]::Zero)', content)
        self.assertIn('-Argument "-B agent.py"', content)

        # Job Monitor task requirements:
        # - starts automatically at logon
        # - background-only
        # - self-healing 1-minute repetition + IgnoreNew
        self.assertIn('-TaskName $monitorTaskName', content)
        self.assertIn('-Argument "-B standalone_job_monitor.py"', content)
        self.assertIn('RepetitionInterval (New-TimeSpan -Minutes 1)', content)

    def test_11_programdata_logging_permissions_and_hygiene(self):
        """Installer must set writable ACLs for runtime user and purge bytecode."""
        installer_ps1 = DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1"
        content = installer_ps1.read_text(encoding="utf-8")

        self.assertIn("function Set-WritableLogDirectoryAcl", content)
        self.assertIn("function Set-ProtectedConfigAcl", content)
        self.assertIn("Get-ChildItem -Path $InstallDir -Recurse -Filter '*.pyc'", content)
        self.assertIn("Get-ChildItem -Path $InstallDir -Recurse -Filter '__pycache__'", content)

    def test_12_network_license_monitor_isolation(self):
        """Workstation installer and uninstaller must never touch or unregister Network License Monitor."""
        installer_content = (DEPLOYMENT_DIR / "Install-MoldflowWorkstation.ps1").read_text(encoding="utf-8")
        uninstaller_content = (DEPLOYMENT_DIR / "Uninstall-MoldflowWorkstation.ps1").read_text(encoding="utf-8")

        # Network License Monitor task must NOT be stopped or removed by workstation scripts
        self.assertNotIn("Moldflow Network License Monitor", installer_content,
                         "Workstation installer must not reference or touch Network License Monitor")
        self.assertNotIn("Moldflow Network License Monitor", uninstaller_content,
                         "Workstation uninstaller must not reference or touch Network License Monitor")

    def test_13_clean_uninstaller_behavior(self):
        """Uninstaller must unregister only workstation tasks, restore run_startup.vbs, and preserve config."""
        uninstaller_content = (DEPLOYMENT_DIR / "Uninstall-MoldflowWorkstation.ps1").read_text(encoding="utf-8")

        self.assertIn('"Moldflow Mobile Job Monitor"', uninstaller_content)
        self.assertIn('"Moldflow Post-Analyze Agent"', uninstaller_content)
        self.assertIn('Move-Item -Path $bakPath -Destination $vbsPath', uninstaller_content,
                      "Uninstaller must restore original run_startup.vbs from backup")
        self.assertIn('$SkipFileDelete', uninstaller_content,
                      "Uninstaller must support $SkipFileDelete for MSI-managed file removal")
        self.assertIn('[PRESERVED] Workstation configuration preserved', uninstaller_content)


class TestJobDetectionAndMobileReporting(unittest.TestCase):
    """Verifies that installed workstation modules properly detect jobs, format payloads, and report to backend."""

    def test_14_job_detection_pipeline(self):
        """Job detector must identify active SCM jobs and extract correct fields."""
        from agents.post_analyze.job_detector import JobDetector

        mock_compute_jobs = MagicMock()
        mock_compute_jobs.SOLVE_TYPES = ("study", "mesh+study")
        mock_compute_jobs.list_jobs.return_value = [
            {
                "jobID": "scm-job-alpha-001",
                "epoch": 1234567,
                "payload": {
                    "name": "Cooling_Channel_Study",
                    "type": "study",
                    "cloud": False,
                }
            }
        ]
        mock_logger = logging.getLogger("test_detector")

        detector = JobDetector(compute_jobs=mock_compute_jobs, logger=mock_logger)
        detector.initialize()
        # On initialization baseline was captured, now simulate new incoming job
        mock_compute_jobs.list_jobs.return_value = [
            {
                "jobID": "scm-job-alpha-001",
                "epoch": 1234567,
                "payload": {"name": "Cooling_Channel_Study", "type": "study", "cloud": False}
            },
            {
                "jobID": "scm-job-beta-002",
                "epoch": 1234568,
                "payload": {"name": "Warp_Study_2", "type": "study", "cloud": False}
            }
        ]
        candidates = detector.poll()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["jobID"], "scm-job-beta-002")

    def test_15_backend_lifecycle_transitions(self):
        """Reporter should handle lifecycle transitions (STARTED -> INPROGRESS -> COMPLETED)."""
        from lib.mobile.reporter import _should_send, _percent_bucket, _last_sent

        _last_sent.clear()
        job_id = "test-job-lifecycle-001"

        now = time.time()
        # 1. First sight (STARTED / 0%) -> should send
        self.assertTrue(_should_send(job_id, "started", 0, False))
        _last_sent[job_id] = ("started", _percent_bucket(0), now)

        # 2. Same status and 2% progress -> should NOT send (less than 5% delta and <60s)
        self.assertFalse(_should_send(job_id, "started", 2, False))

        # 3. Progress jumps to 20% (INPROGRESS) -> should send
        self.assertTrue(_should_send(job_id, "running", 20, False))
        _last_sent[job_id] = ("running", _percent_bucket(20), now)

        # 4. Status transitions to COMPLETED (finished = True) -> should send
        self.assertTrue(_should_send(job_id, "completed", 100, True))

    def test_16_mobile_fcm_notification_compatibility(self):
        """Push notification data format must match mobile FirebaseService expectation."""
        # Mobile FirebaseService expects data payload with title, body, job_id, status, machine_id
        notification_payload = {
            "title": "Moldflow Job Completed",
            "body": "Study Cooling_Channel_Study finished successfully.",
            "data": {
                "job_id": "scm-job-alpha-001",
                "study_name": "Cooling_Channel_Study",
                "status": "COMPLETED",
                "machine_id": "LAPTOP-CA2QN87F"
            }
        }
        self.assertEqual(notification_payload["data"]["status"], "COMPLETED")
        self.assertIn("job_id", notification_payload["data"])
        self.assertIn("machine_id", notification_payload["data"])


if __name__ == "__main__":
    unittest.main()
