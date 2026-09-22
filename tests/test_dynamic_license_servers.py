"""
Tests for Dynamic License Servers and Silent Monitor Execution.
Enforces:
1. Arbitrary server scalability: Server A, B, C -> 3 servers; Add Server D -> 4 servers; Remove Server C -> 3 servers.
2. Dynamic product cataloging: Unknown products accepted and displayed safely.
3. Silent subprocess execution: CREATE_NO_WINDOW and STARTUPINFO flags applied on Windows.
"""

import os
import sys
import json
import uuid
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Inject backend path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from license_monitor.monitor import LicenseMonitor
from backend.app_postgres_ready import app, get_db, db_execute
from backend.license_persistence import LicensePersistenceService, init_license_tables
from backend.license_ingestion import (
    LicenseSnapshotPayload,
    LicenseServerPayload,
    LicensePackagePayload,
    ServerStatusEnum,
)


class TestDynamicMonitorConfiguration(unittest.TestCase):
    """Verifies that LicenseMonitor dynamically discovers and adjusts to arbitrary servers."""

    def test_multi_server_config_discovery(self):
        """Monitor discovers arbitrary servers from 'servers' array in configuration."""
        cfg_data = {
            "monitor_version": "1.0.0",
            "schema_version": "1.0",
            "servers": [
                {"hostname": "SERVER-ALPHA", "port": 27000, "target": "27000@SERVER-ALPHA"},
                {"hostname": "SERVER-BETA", "port": 27000, "target": "27000@SERVER-BETA"},
                {"hostname": "SERVER-GAMMA", "port": 27000, "target": "27000@SERVER-GAMMA"},
            ],
            "poll_interval_seconds": 15,
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg_data, f)
            tmp_path = f.name

        try:
            monitor = LicenseMonitor(config_path=tmp_path)
            servers = monitor.get_configured_servers()
            self.assertEqual(len(servers), 3)
            self.assertEqual(servers[0]["hostname"], "SERVER-ALPHA")
            self.assertEqual(servers[1]["hostname"], "SERVER-BETA")
            self.assertEqual(servers[2]["hostname"], "SERVER-GAMMA")

            # Dynamically add Server Delta
            cfg_data["servers"].append({"hostname": "SERVER-DELTA", "port": 27000, "target": "27000@SERVER-DELTA"})
            with open(tmp_path, "w") as f:
                json.dump(cfg_data, f)

            monitor.reload_config()
            servers_expanded = monitor.get_configured_servers()
            self.assertEqual(len(servers_expanded), 4)
            self.assertEqual(servers_expanded[3]["hostname"], "SERVER-DELTA")

            # Dynamically remove Server Gamma
            cfg_data["servers"] = [s for s in cfg_data["servers"] if s["hostname"] != "SERVER-GAMMA"]
            with open(tmp_path, "w") as f:
                json.dump(cfg_data, f)

            monitor.reload_config()
            servers_contracted = monitor.get_configured_servers()
            self.assertEqual(len(servers_contracted), 3)
            hostnames = [s["hostname"] for s in servers_contracted]
            self.assertNotIn("SERVER-GAMMA", hostnames)
            self.assertIn("SERVER-DELTA", hostnames)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_legacy_single_server_backward_compatibility(self):
        """Legacy config using single 'server' object is automatically converted to servers list."""
        cfg_data = {
            "server": {"hostname": "LEGACY-SERVER", "port": 27000, "target": "27000@LEGACY-SERVER"}
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg_data, f)
            tmp_path = f.name

        try:
            monitor = LicenseMonitor(config_path=tmp_path)
            servers = monitor.get_configured_servers()
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["hostname"], "LEGACY-SERVER")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @unittest.skipUnless(sys.platform == "win32", "CREATE_NO_WINDOW is Windows-specific")
    def test_silent_subprocess_flags_enforced(self):
        """execute_query must pass CREATE_NO_WINDOW and SW_HIDE to prevent console flashing."""
        monitor = LicenseMonitor()
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "dummy stdout"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            with patch("os.path.exists", return_value=True):
                monitor.execute_query("27000@TEST-SERVER")

            self.assertTrue(mock_run.called)
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("creationflags"), 0x08000000)  # CREATE_NO_WINDOW
            startupinfo = kwargs.get("startupinfo")
            self.assertIsNotNone(startupinfo)
            self.assertEqual(startupinfo.wShowWindow, 0)  # SW_HIDE


class TestDynamicBackendIngestionAndOverview(unittest.TestCase):
    """Verifies that backend dynamically ingests arbitrary servers and products."""

    def setUp(self):
        with get_db() as conn:
            init_license_tables(conn)
            db_execute(conn, "DELETE FROM license_servers WHERE hostname LIKE '%.test'")
            conn.commit()

    def test_dynamic_servers_and_unknown_product_persistence(self):
        """Persisting 3 servers, adding a 4th, and ingesting an unknown product works with zero code changes."""
        with get_db() as conn:
            # 1. Ingest Server A, B, C
            srv_names = ["server-alpha.test", "server-beta.test", "server-gamma.test"]
            for s_name in srv_names:
                snap = LicenseSnapshotPayload(
                    schema_version="1.0",
                    monitor_version="1.0.0",
                    captured_at="2026-09-22T05:00:00+00:00",
                    server=LicenseServerPayload(hostname=s_name, status=ServerStatusEnum.UP),
                    packages=[
                        LicensePackagePayload(
                            feature_code="77400MFIA_T_F",
                            total_issued=5,
                            in_use=1,
                            available=4,
                            utilization_pct=20.0,
                            product_family="Insight",
                            product_name="Autodesk Moldflow Insight"
                        )
                    ],
                    features=[],
                    checkouts=[],
                    anomalies=[]
                )
                LicensePersistenceService.process_snapshot(conn, snap)
            conn.commit()

            # Verify 3 servers stored
            rows = db_execute(conn, "SELECT COUNT(*) as cnt FROM license_servers WHERE hostname LIKE '%.test'").fetchone()
            self.assertEqual(rows["cnt"], 3)

            # 2. Ingest Server D with an UNKNOWN future product
            snap_d = LicenseSnapshotPayload(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at="2026-09-22T05:01:00+00:00",
                server=LicenseServerPayload(hostname="server-delta.test", status=ServerStatusEnum.UP),
                packages=[
                    LicensePackagePayload(
                        feature_code="99999QUANTUM_T_F",
                        total_issued=10,
                        in_use=2,
                        available=8,
                        utilization_pct=20.0,
                        product_family="Quantum",
                        product_name="Autodesk Moldflow Quantum Solver",
                        catalog_status="UNKNOWN"
                    )
                ],
                features=[],
                checkouts=[],
                anomalies=[]
            )
            LicensePersistenceService.process_snapshot(conn, snap_d)
            conn.commit()

            # Verify 4 servers now stored
            rows4 = db_execute(conn, "SELECT COUNT(*) as cnt FROM license_servers WHERE hostname LIKE '%.test'").fetchone()
            self.assertEqual(rows4["cnt"], 4)

            # Verify unknown product stored in catalog without crashing
            cat_row = db_execute(
                conn,
                "SELECT * FROM license_feature_catalog WHERE feature_code = ?",
                ("99999QUANTUM_T_F",)
            ).fetchone()
            self.assertIsNotNone(cat_row)
            self.assertEqual(cat_row["catalog_status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
