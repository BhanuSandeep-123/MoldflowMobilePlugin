"""tests/test_phase1_isolated_bundle.py
------------------------------------
Regression test for Stage 8B Phase 1: Isolated Workstation Runtime.
Validates that:
1. lib/mobile/reporter config resolution supports %PROGRAMDATA% and env overrides.
2. The staging directory bundle is self-contained and importable without repo root on sys.path.
3. No non-client code (backend, mobile, tests) is present in the bundle.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

STAGING_DIR = Path(r"C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging")


class TestIsolatedBundle(unittest.TestCase):
    def test_01_staging_manifest_and_exclusions(self):
        self.assertTrue(STAGING_DIR.exists(), f"Staging dir missing: {STAGING_DIR}")
        manifest = STAGING_DIR / "MANIFEST.txt"
        self.assertTrue(manifest.exists(), "MANIFEST.txt missing")

        # Verify excluded subsystems are NOT present in staging
        excluded_dirs = ["backend", "mobile", "mobile-tests", "tests", "docs", "license_monitor", ".git"]
        for exc in excluded_dirs:
            p = STAGING_DIR / exc
            self.assertFalse(p.exists(), f"Forbidden subsystem found in bundle: {exc}")

    def test_02_config_resolution_hierarchy(self):
        from lib.mobile import reporter
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Env override
            cfg_env = tmp_path / "env_config.json"
            cfg_env.write_text('{"enabled": true}', encoding="utf-8")
            os.environ["MOLDFLOW_CONFIG"] = str(cfg_env)
            self.assertEqual(reporter._find_config_path(), cfg_env)
            del os.environ["MOLDFLOW_CONFIG"]

            # ProgramData override
            mock_pd = tmp_path / "MockProgramData"
            mock_app = mock_pd / "MoldflowMobile"
            mock_app.mkdir(parents=True, exist_ok=True)
            cfg_pd = mock_app / "config.json"
            cfg_pd.write_text('{"enabled": true}', encoding="utf-8")

            orig_pd = os.environ.get("PROGRAMDATA")
            os.environ["PROGRAMDATA"] = str(mock_pd)
            try:
                self.assertEqual(reporter._find_config_path(), cfg_pd)
            finally:
                if orig_pd:
                    os.environ["PROGRAMDATA"] = orig_pd
                else:
                    del os.environ["PROGRAMDATA"]

    def test_03_isolated_imports(self):
        # Verify that modules inside STAGING_DIR can be imported independently
        orig_sys_path = list(sys.path)
        orig_modules = {k: v for k, v in sys.modules.items() if k.startswith("lib")}
        for k in orig_modules:
            del sys.modules[k]
        try:
            sys.path = [str(STAGING_DIR)] + [p for p in orig_sys_path if "MoldflowMobileSystem" not in p]
            from lib.scm import client as scm_client
            from lib.mobile import reporter as mob_rep
            from lib.jobs import models as jm

            self.assertTrue(Path(scm_client.__file__).resolve().is_relative_to(STAGING_DIR.resolve()))
            self.assertTrue(Path(mob_rep.__file__).resolve().is_relative_to(STAGING_DIR.resolve()))
            self.assertTrue(Path(jm.__file__).resolve().is_relative_to(STAGING_DIR.resolve()))
        finally:
            sys.path = orig_sys_path
            for k in [k for k in sys.modules if k.startswith("lib")]:
                del sys.modules[k]
            sys.modules.update(orig_modules)


if __name__ == "__main__":
    unittest.main()
