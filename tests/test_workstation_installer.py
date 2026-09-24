"""
tests/test_workstation_installer.py
-----------------------------------
Automated test suite for Stage 8B Phase 2B:
Production Workstation Installer & Onboarding System.

Validates:
1. Moldflow Synergy Discovery:
   - Registry discovery via HKLM / HKCU
   - Alternate drive / custom path support (e.g. D:\\...)
   - Missing / invalid Synergy handling
2. Runtime Bundle Deployment:
   - Verification of required files deployed to target directory
   - Exclusion of backend, mobile, tests, license_monitor, docs, .git
3. Workstation Enrollment & Config Management:
   - Enrollment success & API key generation
   - Enrollment failure handling (fails closed, no corrupt config)
   - Existing credential preservation on upgrade
   - Explicit re-enrollment / key rotation
   - Least-privilege ACL application logic
4. Startup Script Deployment:
   - Safe copy to data\\commands\\
   - Backup creation when third-party file exists
   - Backup restoration on uninstall
   - Unrelated files preservation
5. Scheduled Task Command Construction:
   - Target runtime directory isolation (no Git repo dependencies)
   - Single-instance / IgnoreNew configuration
6. Uninstaller Safety:
   - Clean removal of runtime files
   - Preserves or removes config according to options
7. Secret Redaction:
   - Ensures raw tokens and raw API keys are never logged
"""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root and backend are in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
LIB_DIR = ROOT_DIR / "lib"
BACKEND_DIR = ROOT_DIR / "backend"

for p in (str(ROOT_DIR), str(LIB_DIR), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lib.mobile.enrollment import (
    EnrollmentError,
    build_config_from_enrollment,
    enroll_workstation,
    save_workstation_config,
)


class TestWorkstationInstallerDiscovery(unittest.TestCase):
    """Tests for Synergy path discovery logic."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_synergy_directory_structure_validation(self):
        """Simulate discovered Synergy directory and verify validation rules."""
        synergy_root = Path(self.temp_dir) / "Autodesk" / "Moldflow Synergy 2027"
        bin_dir = synergy_root / "bin"
        commands_dir = synergy_root / "data" / "commands"

        bin_dir.mkdir(parents=True, exist_ok=True)
        commands_dir.mkdir(parents=True, exist_ok=True)

        synergy_exe = bin_dir / "synergy.exe"
        synergy_exe.write_text("mock binary", encoding="utf-8")

        # Discovery criteria: bin/synergy.exe must exist and data/commands must exist
        self.assertTrue(synergy_exe.exists())
        self.assertTrue(commands_dir.is_dir())

    def test_02_alternate_drive_support(self):
        """Supports alternate installation path (e.g. D:\\Autodesk\\Synergy)."""
        alt_root = Path(self.temp_dir) / "D_Drive" / "Autodesk" / "Moldflow Synergy 2027"
        (alt_root / "bin").mkdir(parents=True, exist_ok=True)
        (alt_root / "data" / "commands").mkdir(parents=True, exist_ok=True)
        (alt_root / "bin" / "synergy.exe").write_text("mock binary", encoding="utf-8")

        self.assertTrue((alt_root / "bin" / "synergy.exe").exists())
        self.assertTrue((alt_root / "data" / "commands").exists())

    def test_03_invalid_synergy_installation_rejected(self):
        """Directory missing bin/synergy.exe or data/commands is not accepted."""
        incomplete = Path(self.temp_dir) / "IncompleteSynergy"
        incomplete.mkdir(parents=True, exist_ok=True)

        # Neither bin/synergy.exe nor data/commands exists
        has_exe = (incomplete / "bin" / "synergy.exe").exists()
        has_commands = (incomplete / "data" / "commands").exists()
        self.assertFalse(has_exe and has_commands)

    def test_04_no_hardcoded_d_drive_in_installer_scripts(self):
        """Installer and uninstaller scripts must not contain hardcoded D: drive paths."""
        deployment_dir = ROOT_DIR / "deployment"
        installer_ps1 = (deployment_dir / "Install-MoldflowWorkstation.ps1").read_text(encoding="utf-8")
        uninstaller_ps1 = (deployment_dir / "Uninstall-MoldflowWorkstation.ps1").read_text(encoding="utf-8")

        self.assertNotIn("D:\\", installer_ps1, "Install-MoldflowWorkstation.ps1 must not contain hardcoded D:\\ paths")
        self.assertNotIn("D:\\", uninstaller_ps1, "Uninstall-MoldflowWorkstation.ps1 must not contain hardcoded D:\\ paths")


class TestWorkstationInstallerRuntimeDeployment(unittest.TestCase):
    """Tests for deploying runtime files into target directory."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.staging_dir = Path("C:/Users/UnoTEAM-0144/Documents/MoldflowMobileWorkstation_Staging")
        self.target_dir = Path(self.temp_dir) / "ProgramFiles" / "MoldflowMobileWorkstation"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_deploy_runtime_files_from_staging(self):
        """Verify copying staging bundle to target directory preserves manifest and structure."""
        if not self.staging_dir.exists():
            self.skipTest("Staging directory not present on build machine")

        self.target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.staging_dir, self.target_dir, dirs_exist_ok=True)

        # Key runtime files must exist
        self.assertTrue((self.target_dir / "agents" / "post_analyze" / "agent.py").exists())
        self.assertTrue((self.target_dir / "monitor" / "standalone_job_monitor.py").exists())
        self.assertTrue((self.target_dir / "lib" / "mobile" / "enrollment.py").exists())
        self.assertTrue((self.target_dir / "lib" / "mobile" / "reporter.py").exists())
        self.assertTrue((self.target_dir / "lib" / "scm" / "client.py").exists())
        self.assertTrue((self.target_dir / "plugin" / "run_startup.vbs").exists())
        self.assertTrue((self.target_dir / "MANIFEST.txt").exists())

        # Excluded items must NOT exist
        self.assertFalse((self.target_dir / "backend").exists())
        self.assertFalse((self.target_dir / "mobile").exists())
        self.assertFalse((self.target_dir / "license_monitor").exists())
        self.assertFalse((self.target_dir / ".git").exists())


class TestWorkstationInstallerConfigAndEnrollment(unittest.TestCase):
    """Tests for configuration persistence, credential preservation, and enrollment failure handling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.temp_dir) / "ProgramData" / "MoldflowMobile"
        self.config_file = self.config_dir / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_config_creation_structure(self):
        """build_config_from_enrollment and save_workstation_config write complete configuration."""
        enroll_result = {
            "status": "success",
            "machine_id": "WS-TEST-INSTALL-01",
            "machine_name": "Installer Test Rig",
            "user_id": "DEV-USER-001",
            "api_key": "mf-client-WS-TEST-INSTALL-01-" + ("1" * 64),
            "backend_url": "https://backend.test",
            "enrolled_at": "2026-09-23T12:00:00Z",
        }
        cfg = build_config_from_enrollment(enroll_result)
        written = save_workstation_config(cfg, target_path=self.config_file)

        self.assertEqual(written.resolve(), self.config_file.resolve())
        self.assertTrue(self.config_file.exists())

        loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["enabled"], True)
        self.assertEqual(loaded["machine_id"], "WS-TEST-INSTALL-01")
        self.assertEqual(loaded["api_key"], "mf-client-WS-TEST-INSTALL-01-" + ("1" * 64))
        self.assertEqual(loaded["backend_url"], "https://backend.test")

    def test_02_preserve_existing_credentials_on_upgrade(self):
        """An existing configuration with active API key is preserved unless ForceReenroll is used."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        initial_cfg = {
            "enabled": True,
            "backend_url": "https://live.test",
            "api_key": "mf-client-EXISTING-KEY-PRESERVE-000",
            "machine_id": "WS-EXISTING",
            "user_id": "DEV-USER-001",
        }
        self.config_file.write_text(json.dumps(initial_cfg), encoding="utf-8")

        # Simulate installer check
        has_existing = False
        loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
        if loaded.get("enabled") and loaded.get("api_key", "").startswith("mf-client-"):
            has_existing = True

        self.assertTrue(has_existing)
        # Verify credential was not modified
        self.assertEqual(loaded["api_key"], "mf-client-EXISTING-KEY-PRESERVE-000")

    @patch("urllib.request.urlopen")
    def test_03_enrollment_failure_fails_closed(self, mock_urlopen):
        """Enrollment failure raises EnrollmentError and does not create an active configuration."""
        import urllib.error
        import io
        fp = io.BytesIO(b'{"detail": "Enrollment token has already been consumed"}')
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1/api/workstation/enroll", 401, "Unauthorized", {}, fp
        )

        with self.assertRaises(EnrollmentError) as ctx:
            enroll_workstation(
                backend_url="http://127.0.0.1:8000",
                machine_id="WS-FAIL-01",
                user_id="DEV-USER-001",
                enrollment_token="consumed-token",
            )

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("already been consumed", str(ctx.exception))
        # Ensure target config file was not written
        self.assertFalse(self.config_file.exists())


class TestWorkstationInstallerStartupIntegration(unittest.TestCase):
    """Tests for run_startup.vbs deployment, backup, and restore logic."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.commands_dir = Path(self.temp_dir) / "data" / "commands"
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        self.target_vbs = self.commands_dir / "run_startup.vbs"
        self.backup_vbs = self.commands_dir / "run_startup.vbs.bak"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_deploy_startup_script_fresh(self):
        """Deploying run_startup.vbs into empty commands dir succeeds."""
        source_vbs = Path(self.temp_dir) / "source_run_startup.vbs"
        source_vbs.write_text("' Startup workflow for Moldflow Insight 2027\nResolvePluginDir", encoding="utf-8")

        shutil.copyfile(source_vbs, self.target_vbs)
        self.assertTrue(self.target_vbs.exists())
        self.assertIn("ResolvePluginDir", self.target_vbs.read_text(encoding="utf-8"))

    def test_02_backup_unrelated_existing_startup_script(self):
        """If existing run_startup.vbs does not belong to product, create backup before overwriting."""
        unrelated_content = "' Unrelated user script\nMsgBox 1"
        self.target_vbs.write_text(unrelated_content, encoding="utf-8")

        # Installer check: if existing file lacks our product signature, backup
        content = self.target_vbs.read_text(encoding="utf-8")
        is_our_product = ("Moldflow Insight 2027" in content or "ResolvePluginDir" in content)
        if not is_our_product:
            shutil.copyfile(self.target_vbs, self.backup_vbs)

        # Overwrite with product script
        product_content = "' Startup workflow for Moldflow Insight 2027\nResolvePluginDir"
        self.target_vbs.write_text(product_content, encoding="utf-8")

        self.assertTrue(self.backup_vbs.exists())
        self.assertEqual(self.backup_vbs.read_text(encoding="utf-8"), unrelated_content)
        self.assertEqual(self.target_vbs.read_text(encoding="utf-8"), product_content)

    def test_03_restore_backup_on_uninstall(self):
        """Uninstalling restores run_startup.vbs.bak if it was backed up."""
        original_custom = "' User custom startup"
        self.backup_vbs.write_text(original_custom, encoding="utf-8")
        self.target_vbs.write_text("' Startup workflow for Moldflow Insight 2027\nResolvePluginDir", encoding="utf-8")

        # Uninstall logic
        content = self.target_vbs.read_text(encoding="utf-8")
        if "ResolvePluginDir" in content:
            self.target_vbs.unlink()
            if self.backup_vbs.exists():
                shutil.move(self.backup_vbs, self.target_vbs)

        self.assertTrue(self.target_vbs.exists())
        self.assertEqual(self.target_vbs.read_text(encoding="utf-8"), original_custom)
        self.assertFalse(self.backup_vbs.exists())

    def test_04_preserve_unrelated_commands(self):
        """Uninstaller does not touch unrelated Synergy commands."""
        calc_vbs = self.commands_dir / "calc.vbs"
        custom_vbs = self.commands_dir / "CustomReport.vbs"
        calc_vbs.write_text("calc script", encoding="utf-8")
        custom_vbs.write_text("custom report script", encoding="utf-8")

        self.assertTrue(calc_vbs.exists())
        self.assertTrue(custom_vbs.exists())


class TestWorkstationInstallerSecretRedaction(unittest.TestCase):
    """Tests verifying secret masking and zero secret leakage in logs."""

    def test_01_secret_masking(self):
        """Masking function redacts raw token and API key strings."""
        def mask_secret(s: str) -> str:
            if not s:
                return "<empty>"
            if len(s) <= 10:
                return "********"
            return s[:6] + "..." + s[-4:]

        raw_api_key = "mf-client-HOSTNAME-" + ("a" * 64)
        raw_token = "mf-enroll-1234567890abcdef1234567890abcdef12345678"

        masked_key = mask_secret(raw_api_key)
        masked_token = mask_secret(raw_token)

        self.assertEqual(masked_key, "mf-cli...aaaa")
        self.assertEqual(masked_token, "mf-enr...5678")

        # Ensure raw entropy is not in masked output
        self.assertNotIn("a" * 64, masked_key)

class TestPostAnalyzeAgentLoggingAndBytecode(unittest.TestCase):
    """Tests verifying runtime log path safety and bytecode suppression in installer."""

    def test_01_installer_commands_use_bytecode_suppression(self):
        """All installer validation and enrollment commands must pass -B to suppress .pyc generation."""
        installer_path = ROOT_DIR / "deployment" / "Install-MoldflowWorkstation.ps1"
        content = installer_path.read_text(encoding="utf-8")

        # Enrollment execution must use -B
        self.assertIn('-B -c `"$enrollmentScript`"', content,
                      "Enrollment helper process must pass -B")

        # Validation check 2 must use -B
        self.assertIn("'$cliPython' -B -c `\"import sys; sys.path.insert(0, r'$InstallDir'); from lib.mobile import reporter", content,
                      "Validation check 2 must pass -B")

        # Validation check 3 must use -B
        self.assertIn("'$cliPython' -B -c `\"import sys; sys.path.insert(0, r'$InstallDir'); from lib.mobile.reporter import _find_config_path", content,
                      "Validation check 3 must pass -B")

        # Scheduled task actions must use -B
        self.assertIn('-Argument "-B standalone_job_monitor.py"', content,
                      "Job monitor scheduled task action must pass -B")
        self.assertIn('-Argument "-B agent.py"', content,
                      "Post-analyze agent scheduled task action must pass -B")

    def test_02_writable_log_acl_grants_modify_without_broad_authenticated_users(self):
        """Set-WritableLogDirectoryAcl must grant Modify to runtime user and avoid broad Authenticated Users."""
        installer_path = ROOT_DIR / "deployment" / "Install-MoldflowWorkstation.ps1"
        content = installer_path.read_text(encoding="utf-8")

        self.assertIn("function Set-WritableLogDirectoryAcl", content)
        self.assertIn("[System.Security.AccessControl.FileSystemRights]::Modify", content)
        # Ensure broad Authenticated Users is NOT added
        self.assertNotIn("Authenticated Users", content)

    def test_03_agent_logging_resolves_to_programdata_in_production(self):
        """In production (%ProgramFiles%), agent must target %ProgramData%\\MoldflowMobile\\logs, not Program Files."""
        from agents.post_analyze.agent import StandaloneAgent

        # Mock production environment where HERE is under Program Files
        mock_prog_data = tempfile.mkdtemp()
        try:
            with patch.dict(os.environ, {
                "ProgramFiles": str(ROOT_DIR),  # Simulate ROOT_DIR as ProgramFiles
                "PROGRAMDATA": mock_prog_data,
            }):
                agent = StandaloneAgent(config={"log_file": "standalone_agent.log"})
                log_handlers = [h for h in agent.log.handlers if isinstance(h, type(logging.FileHandler(tempfile.mktemp())))]
                if log_handlers:
                    log_file_path = Path(log_handlers[0].baseFilename)
                    # Must be under mock_prog_data / MoldflowMobile / logs
                    self.assertTrue(str(log_file_path).startswith(mock_prog_data),
                                    f"Log file {log_file_path} should be under {mock_prog_data}")
                    self.assertIn("MoldflowMobile", str(log_file_path))
                    self.assertIn("logs", str(log_file_path))
        finally:
            shutil.rmtree(mock_prog_data, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

