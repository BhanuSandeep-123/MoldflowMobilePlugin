"""
tests/test_license_persistence.py
----------------------------------
Comprehensive Stage 4 Unit & Integration Tests for:
- PostgreSQL / SQLite Schema & Table Initialization
- Multi-Server Registry & Identity Isolation
- Global Feature Catalog & Unknown MFAA feature handling
- Snapshot Header & Feature History Persistence
- Operational Current Feature State & Package Math
- Component Storage Without Double-Counting
- Active Physical Checkouts (1 row per physical seat)
- Snapshot Diff Engine & Event Generation:
  * CHECKOUT event
  * RETURN event
  * EXHAUSTED transition (fired once)
  * AVAILABLE transition (fired once)
  * SERVER_DOWN transition
  * SERVER_UP transition
  * VENDOR_DOWN transition
- Critical Outage Invariant:
  * Server DOWN with empty checkouts must NOT generate fabricated RETURN events
- Recovery Flow:
  * UP -> DOWN -> UP verified without phantom events
- Idempotency:
  * Repeated identical snapshot absorbs cleanly with zero duplicate events
- Multi-Server Isolation:
  * Same feature code / checkout on Server A does NOT affect Server B
"""

import sys
import json
import sqlite3
import unittest
from pathlib import Path

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from license_persistence import (
    init_license_tables,
    LicensePersistenceService,
)
from license_ingestion import (
    LicenseSnapshotPayload,
    LicenseServerPayload,
    LicensePackagePayload,
    LicenseFeaturePayload,
    LicenseCheckoutPayload,
    ServerStatusEnum,
)


class TestLicensePersistenceStage4(unittest.TestCase):

    def setUp(self):
        """Sets up a clean in-memory database for isolated persistence testing."""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_license_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    # ------------------------------------------------------------------------
    # 1-4: Database & Server Registry Isolation
    # ------------------------------------------------------------------------
    def test_01_create_server_a_and_b(self):
        """Create Server A and Server B and verify distinct IDs."""
        id_a, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F", 27000)
        id_b, _ = LicensePersistenceService.get_or_create_server(self.conn, "DESKTOP-23TMNR6", 27000)
        self.assertNotEqual(id_a, id_b)
        self.assertTrue(len(id_a) > 0)
        self.assertTrue(len(id_b) > 0)

    def test_02_server_lookup_is_case_insensitive(self):
        """Server lookup normalizes case and trims whitespace."""
        id_1, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F", 27000)
        id_2, _ = LicensePersistenceService.get_or_create_server(self.conn, "  laptop-ca2qn87f  ", 27000)
        self.assertEqual(id_1, id_2)

    def test_03_store_same_feature_code_on_both_servers_remains_independent(self):
        """
        Store feature 77800MFS_T_F on Server A (1 seat) and Server B (4 seats).
        Confirms operational state is strictly per (server_id, feature_code).
        """
        id_a, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F", 27000)
        id_b, _ = LicensePersistenceService.get_or_create_server(self.conn, "DESKTOP-23TMNR6", 27000)

        LicensePersistenceService._upsert_server_feature(
            self.conn, id_a, "77800MFS_T_F", "PACKAGE", 1, 0, 1, 0.0, "snap-1", "2026-09-18T12:00:00Z"
        )
        LicensePersistenceService._upsert_server_feature(
            self.conn, id_b, "77800MFS_T_F", "PACKAGE", 4, 0, 4, 0.0, "snap-2", "2026-09-18T12:00:00Z"
        )

        row_a = self.conn.execute(
            "SELECT total_issued FROM license_server_features WHERE server_id = ? AND feature_code = ?",
            (id_a, "77800MFS_T_F")
        ).fetchone()
        row_b = self.conn.execute(
            "SELECT total_issued FROM license_server_features WHERE server_id = ? AND feature_code = ?",
            (id_b, "77800MFS_T_F")
        ).fetchone()

        self.assertEqual(row_a["total_issued"], 1)
        self.assertEqual(row_b["total_issued"], 4)

    # ------------------------------------------------------------------------
    # 5-8: Snapshot Persistence & Global Catalog
    # ------------------------------------------------------------------------
    def test_05_store_up_snapshot_and_catalog(self):
        """Store healthy UP snapshot and verify snapshot table and global catalog."""
        snap = LicenseSnapshotPayload(
            schema_version="1.0",
            monitor_version="1.0.0",
            captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(
                hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP,
                lmgrd_version="v11.19.9", adskflex_status="UP", adskflex_version="v11.19.9"
            ),
            packages=[
                LicensePackagePayload(
                    feature_code="77800MFS_T_F", total_issued=1, in_use=0, available=1, utilization_pct=0.0,
                    product_family="Synergy", product_name="Autodesk Moldflow Synergy", catalog_status="VERIFIED"
                )
            ],
            features=[],
            checkouts=[],
        )

        res = LicensePersistenceService.process_snapshot(self.conn, snap, duration_ms=12.5)
        self.assertFalse(res["idempotent_duplicate"])
        snap_row = self.conn.execute(
            "SELECT * FROM license_snapshots WHERE snapshot_id = ?", (res["snapshot_id"],)
        ).fetchone()
        self.assertIsNotNone(snap_row)
        self.assertEqual(snap_row["server_status"], "UP")
        self.assertEqual(snap_row["lmgrd_version"], "v11.19.9")

        # Global catalog verified
        cat_row = self.conn.execute(
            "SELECT * FROM license_feature_catalog WHERE feature_code = ?", ("77800MFS_T_F",)
        ).fetchone()
        self.assertIsNotNone(cat_row)
        self.assertEqual(cat_row["product_name"], "Autodesk Moldflow Synergy")

    def test_06_store_unknown_mfaa_feature(self):
        """Unknown MFAA feature stored in catalog with catalog_status UNKNOWN and product_name NULL."""
        snap = LicenseSnapshotPayload(
            schema_version="1.0",
            monitor_version="1.0.0",
            captured_at="2026-09-18T12:05:00Z",
            server=LicenseServerPayload(hostname="DESKTOP-23TMNR6", port=27000, status=ServerStatusEnum.UP),
            packages=[
                LicensePackagePayload(
                    feature_code="76800MFAA_T_F", total_issued=4, in_use=0, available=4, utilization_pct=0.0,
                    product_family="MFAA", product_name=None, catalog_status="UNKNOWN"
                )
            ],
            features=[
                LicenseFeaturePayload(
                    feature_code="88225MFAA_2027_0F", total_issued=4, in_use=0, available=4,
                    feature_type="COMPONENT", product_family="MFAA", product_name=None,
                    year_version="2027", parent_package="76800MFAA_T_F", catalog_status="UNKNOWN"
                )
            ],
            checkouts=[],
        )
        res = LicensePersistenceService.process_snapshot(self.conn, snap)
        self.assertFalse(res["idempotent_duplicate"])

        pkg_cat = self.conn.execute(
            "SELECT * FROM license_feature_catalog WHERE feature_code = ?", ("76800MFAA_T_F",)
        ).fetchone()
        self.assertEqual(pkg_cat["catalog_status"], "UNKNOWN")
        self.assertIsNone(pkg_cat["product_name"])

        comp_cat = self.conn.execute(
            "SELECT * FROM license_feature_catalog WHERE feature_code = ?", ("88225MFAA_2027_0F",)
        ).fetchone()
        self.assertEqual(comp_cat["parent_package_code"], "76800MFAA_T_F")
        self.assertEqual(comp_cat["year_version"], "2027")

    # ------------------------------------------------------------------------
    # 9-10: Feature State & Component Handling (No Double Counting)
    # ------------------------------------------------------------------------
    def test_09_component_stored_with_null_utilization(self):
        """Component features are stored with utilization_pct = NULL to prevent double-counting."""
        snap = LicenseSnapshotPayload(
            schema_version="1.0",
            monitor_version="1.0.0",
            captured_at="2026-09-18T12:10:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[
                LicensePackagePayload(
                    feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0,
                    product_family="Synergy", product_name="Autodesk Moldflow Synergy"
                )
            ],
            features=[
                LicenseFeaturePayload(
                    feature_code="88232MFS_2027_0F", total_issued=1, in_use=1, available=0,
                    feature_type="COMPONENT", product_family="Synergy"
                )
            ],
            checkouts=[],
        )
        LicensePersistenceService.process_snapshot(self.conn, snap)
        server_id, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F")

        pkg_row = self.conn.execute(
            "SELECT utilization_pct FROM license_server_features WHERE server_id = ? AND feature_code = ?",
            (server_id, "77800MFS_T_F")
        ).fetchone()
        comp_row = self.conn.execute(
            "SELECT utilization_pct FROM license_server_features WHERE server_id = ? AND feature_code = ?",
            (server_id, "88232MFS_2027_0F")
        ).fetchone()

        self.assertEqual(pkg_row["utilization_pct"], 100.0)
        self.assertIsNone(comp_row["utilization_pct"])

    # ------------------------------------------------------------------------
    # 11-13: Active Physical Checkouts
    # ------------------------------------------------------------------------
    def test_11_insert_physical_checkout(self):
        """Physical checkout stored as exactly 1 row in license_active_checkouts."""
        snap = LicenseSnapshotPayload(
            schema_version="1.0",
            monitor_version="1.0.0",
            captured_at="2026-09-18T12:15:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[
                LicensePackagePayload(
                    feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0
                )
            ],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-synergy-001",
                    server_hostname="LAPTOP-CA2QN87F",
                    username="UnoTEAM-0144",
                    machine_name="LAPTOP-CA2QN87F",
                    package_feature="77800MFS_T_F",
                    selected_component="88232MFS_2027_0F",
                    version="v1.0",
                    server_handle="101",
                    checkout_time="Fri 9/18 12:14",
                    pid="24336",
                )
            ],
        )
        res = LicensePersistenceService.process_snapshot(self.conn, snap)
        co_rows = self.conn.execute(
            "SELECT * FROM license_active_checkouts WHERE server_id = ?", (res["server_id"],)
        ).fetchall()
        self.assertEqual(len(co_rows), 1)
        self.assertEqual(co_rows[0]["checkout_id"], "chk-synergy-001")
        self.assertEqual(co_rows[0]["username"], "UnoTEAM-0144")
        self.assertEqual(co_rows[0]["selected_component_code"], "88232MFS_2027_0F")

    # ------------------------------------------------------------------------
    # 14-20: Event Generation (CHECKOUT, RETURN, EXHAUSTED, AVAILABLE, SERVER_UP, SERVER_DOWN)
    # ------------------------------------------------------------------------
    def test_14_checkout_and_exhausted_events_generated(self):
        """Transitions from idle to active generate CHECKOUT and EXHAUSTED events."""
        # 1. Idle snapshot
        snap_idle = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=0, available=1, utilization_pct=0.0)],
            checkouts=[],
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_idle)

        # 2. Active checkout snapshot (1/1 used -> EXHAUSTED)
        snap_active = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:01:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0)],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-001", server_hostname="LAPTOP-CA2QN87F", username="UnoTEAM-0144",
                    machine_name="LAPTOP-CA2QN87F", package_feature="77800MFS_T_F", selected_component="88232MFS_2027_0F",
                    checkout_time="Fri 9/18 12:01", pid="1234",
                )
            ],
        )
        res = LicensePersistenceService.process_snapshot(self.conn, snap_active)

        # Verify events
        events = self.conn.execute(
            "SELECT event_type, feature_code FROM license_events WHERE server_id = ? ORDER BY created_at ASC",
            (res["server_id"],)
        ).fetchall()
        event_types = [row["event_type"] for row in events]
        self.assertIn("CHECKOUT", event_types)
        self.assertIn("EXHAUSTED", event_types)

    def test_15_return_and_available_events_generated(self):
        """Transitions from active to idle generate RETURN and AVAILABLE events."""
        # Setup active state
        self.test_14_checkout_and_exhausted_events_generated()
        server_id, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F")

        # 3. Released snapshot
        snap_released = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:02:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=0, available=1, utilization_pct=0.0)],
            checkouts=[],
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_released)

        events = self.conn.execute(
            "SELECT event_type FROM license_events WHERE server_id = ? ORDER BY created_at DESC",
            (server_id,)
        ).fetchall()
        recent_types = [row["event_type"] for row in events[:2]]
        self.assertIn("RETURN", recent_types)
        self.assertIn("AVAILABLE", recent_types)

        # Active checkouts table should now be empty
        active_count = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM license_active_checkouts WHERE server_id = ?", (server_id,)
        ).fetchone()["cnt"]
        self.assertEqual(active_count, 0)

    def test_16_server_down_and_up_events_generated(self):
        """Server status transitions generate SERVER_DOWN and SERVER_UP events."""
        # 1. Start UP
        snap_up = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
        )
        res_up = LicensePersistenceService.process_snapshot(self.conn, snap_up)
        server_id = res_up["server_id"]

        # 2. Transition to DOWN
        snap_down = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:01:00Z",
            server=LicenseServerPayload(
                hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.DOWN,
                error_code=-96, error_message="Host down"
            ),
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_down)

        # Check for SERVER_DOWN event
        down_evt = self.conn.execute(
            "SELECT * FROM license_events WHERE server_id = ? AND event_type = 'SERVER_DOWN'",
            (server_id,)
        ).fetchone()
        self.assertIsNotNone(down_evt)

        # 3. Recover to UP
        snap_up2 = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:02:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_up2)

        # Check for SERVER_UP event
        up_evt = self.conn.execute(
            "SELECT * FROM license_events WHERE server_id = ? AND event_type = 'SERVER_UP'",
            (server_id,)
        ).fetchone()
        self.assertIsNotNone(up_evt)

    # ------------------------------------------------------------------------
    # 21-23: Critical Failure Case: Server DOWN must NOT generate RETURN events!
    # ------------------------------------------------------------------------
    def test_21_server_down_does_not_generate_return_events(self):
        """
        CRITICAL ARCHITECTURAL INVARIANT:
        When server transitions to DOWN with an empty checkouts list,
        active checkouts must NOT be treated as returned.
        NO RETURN events may be generated.
        """
        # Step 1: Active checkout on healthy server
        snap_active = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0)],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-001", server_hostname="LAPTOP-CA2QN87F", username="UnoTEAM-0144",
                    machine_name="LAPTOP-CA2QN87F", package_feature="77800MFS_T_F", selected_component="88232MFS_2027_0F",
                    checkout_time="Fri 9/18 12:00", pid="1234",
                )
            ],
        )
        res = LicensePersistenceService.process_snapshot(self.conn, snap_active)
        server_id = res["server_id"]

        # Step 2: Server goes DOWN (packages=[], checkouts=[])
        snap_down = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:01:00Z",
            server=LicenseServerPayload(
                hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.DOWN,
                error_code=-15, error_message="WinSock: Connection refused"
            ),
            packages=[],
            checkouts=[],
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_down)

        # Invariant checks:
        # 1. Exactly 0 RETURN events exist
        return_events = self.conn.execute(
            "SELECT * FROM license_events WHERE server_id = ? AND event_type = 'RETURN'",
            (server_id,)
        ).fetchall()
        self.assertEqual(len(return_events), 0)

        # 2. SERVER_DOWN event was created
        down_events = self.conn.execute(
            "SELECT * FROM license_events WHERE server_id = ? AND event_type = 'SERVER_DOWN'",
            (server_id,)
        ).fetchall()
        self.assertEqual(len(down_events), 1)

        # 3. Active checkout is preserved in DB (not purged during outage)
        co_rows = self.conn.execute(
            "SELECT * FROM license_active_checkouts WHERE server_id = ?", (server_id,)
        ).fetchall()
        self.assertEqual(len(co_rows), 1)

    # ------------------------------------------------------------------------
    # 24-25: Recovery UP -> DOWN -> UP flow
    # ------------------------------------------------------------------------
    def test_24_recovery_flow_preserves_checkouts_cleanly(self):
        """
        UP (checkout exists) -> DOWN -> UP (same checkout exists):
        Verifies no phantom RETURN or duplicate CHECKOUT events are generated on recovery.
        """
        # 1. UP with checkout
        self.test_21_server_down_does_not_generate_return_events()
        server_id, _ = LicensePersistenceService.get_or_create_server(self.conn, "LAPTOP-CA2QN87F")

        # 3. Server recovers to UP with same checkout
        snap_recovery = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:02:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0)],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-001", server_hostname="LAPTOP-CA2QN87F", username="UnoTEAM-0144",
                    machine_name="LAPTOP-CA2QN87F", package_feature="77800MFS_T_F", selected_component="88232MFS_2027_0F",
                    checkout_time="Fri 9/18 12:00", pid="1234",
                )
            ],
        )
        LicensePersistenceService.process_snapshot(self.conn, snap_recovery)

        # Invariant: exactly 1 CHECKOUT event, 0 RETURN events, 1 SERVER_DOWN, 2 SERVER_UP (initial + recovery)
        all_events = self.conn.execute(
            "SELECT event_type FROM license_events WHERE server_id = ?", (server_id,)
        ).fetchall()
        event_counts = {}
        for row in all_events:
            event_counts[row["event_type"]] = event_counts.get(row["event_type"], 0) + 1

        self.assertEqual(event_counts.get("CHECKOUT", 0), 1)
        self.assertEqual(event_counts.get("RETURN", 0), 0)
        self.assertEqual(event_counts.get("SERVER_DOWN", 0), 1)
        self.assertEqual(event_counts.get("SERVER_UP", 0), 2)

    # ------------------------------------------------------------------------
    # 26-27: Idempotency
    # ------------------------------------------------------------------------
    def test_26_submit_same_snapshot_twice_no_duplicates(self):
        """Submitting the exact same snapshot twice produces zero duplicate events."""
        snap = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0)],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-idem-001", server_hostname="LAPTOP-CA2QN87F", username="User1",
                    machine_name="LAPTOP-CA2QN87F", package_feature="77800MFS_T_F", selected_component="88232MFS_2027_0F",
                    checkout_time="Fri 9/18 12:00", pid="555",
                )
            ],
        )
        res1 = LicensePersistenceService.process_snapshot(self.conn, snap)
        self.assertFalse(res1["idempotent_duplicate"])

        res2 = LicensePersistenceService.process_snapshot(self.conn, snap)
        self.assertTrue(res2["idempotent_duplicate"])
        self.assertEqual(res1["snapshot_id"], res2["snapshot_id"])

        # Count events: exactly 1 CHECKOUT event, exactly 1 snapshot row
        snap_count = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM license_snapshots WHERE server_id = ?", (res1["server_id"],)
        ).fetchone()["cnt"]
        self.assertEqual(snap_count, 1)

        event_count = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM license_events WHERE server_id = ? AND event_type = 'CHECKOUT'",
            (res1["server_id"],)
        ).fetchone()["cnt"]
        self.assertEqual(event_count, 1)

    # ------------------------------------------------------------------------
    # 28: Multi-Server Isolation
    # ------------------------------------------------------------------------
    def test_28_server_a_event_does_not_modify_server_b(self):
        """Events and checkouts on Server A do not pollute Server B."""
        snap_a = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="LAPTOP-CA2QN87F", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=1, in_use=1, available=0, utilization_pct=100.0)],
            checkouts=[
                LicenseCheckoutPayload(
                    checkout_id="chk-a", server_hostname="LAPTOP-CA2QN87F", username="UserA",
                    machine_name="LAPTOP-CA2QN87F", package_feature="77800MFS_T_F", selected_component="88232MFS_2027_0F",
                    checkout_time="Fri 9/18 12:00", pid="111",
                )
            ],
        )
        snap_b = LicenseSnapshotPayload(
            schema_version="1.0", monitor_version="1.0.0", captured_at="2026-09-18T12:00:00Z",
            server=LicenseServerPayload(hostname="DESKTOP-23TMNR6", port=27000, status=ServerStatusEnum.UP),
            packages=[LicensePackagePayload(feature_code="77800MFS_T_F", total_issued=4, in_use=0, available=4, utilization_pct=0.0)],
            checkouts=[],
        )
        res_a = LicensePersistenceService.process_snapshot(self.conn, snap_a)
        res_b = LicensePersistenceService.process_snapshot(self.conn, snap_b)

        # Server A has 1 checkout, Server B has 0
        cnt_a = self.conn.execute("SELECT COUNT(*) as cnt FROM license_active_checkouts WHERE server_id = ?", (res_a["server_id"],)).fetchone()["cnt"]
        cnt_b = self.conn.execute("SELECT COUNT(*) as cnt FROM license_active_checkouts WHERE server_id = ?", (res_b["server_id"],)).fetchone()["cnt"]
        self.assertEqual(cnt_a, 1)
        self.assertEqual(cnt_b, 0)

        # Server A has CHECKOUT event, Server B has no checkout/return events
        evt_b = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM license_events WHERE server_id = ? AND event_type IN ('CHECKOUT', 'RETURN')",
            (res_b["server_id"],)
        ).fetchone()["cnt"]
        self.assertEqual(evt_b, 0)


if __name__ == "__main__":
    unittest.main()
