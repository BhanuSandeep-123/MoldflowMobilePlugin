"""
Unit tests for FlexNet parser and normalizer (Stage 1).
Validates all rules:
- Rule 2: PACKAGE is seat authority (1 physical seat, not 2)
- Rule 3 & 4: Generic parsing, uncatalogued features preserved
- Rule 5: FlexNet error diagnostics preserved (-15, -96)
- Rule 6: Consolidated server status (UP, DOWN, VENDOR_DOWN, UNKNOWN)
- Rule 7: Deterministic checkout ID
- Rule 10: MINUTE precision
"""

import os
import json
import unittest
from pathlib import Path

from license_monitor.models import (
    ServerStatus,
    FeatureType,
    CatalogStatus,
    ExecutionResult,
    PhysicalCheckout,
)
from license_monitor.parser import FlexNetParser


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return f.read()


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestFlexNetParser(unittest.TestCase):

    def setUp(self):
        self.config = load_config()
        self.parser = FlexNetParser(self.config)

    def test_fixture_1_synergy_idle(self):
        """Fixture 1: Server A idle state (all features 0 in use)."""
        raw_text = load_fixture("synergy_idle.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.5,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.UP.value)
        self.assertEqual(snapshot.server.hostname, "LAPTOP-CA2QN87F")
        self.assertEqual(snapshot.server.lmgrd_version, "v11.19.9")
        self.assertEqual(snapshot.server.adskflex_status, "UP")
        self.assertEqual(snapshot.server.adskflex_version, "v11.19.9")
        self.assertIsNone(snapshot.server.error_code)

        # Check packages
        pkg_map = {p.feature_code: p for p in snapshot.packages}
        self.assertIn("77800MFS_T_F", pkg_map)
        self.assertIn("77400MFIA_T_F", pkg_map)

        synergy_pkg = pkg_map["77800MFS_T_F"]
        self.assertEqual(synergy_pkg.total_issued, 1)
        self.assertEqual(synergy_pkg.in_use, 0)
        self.assertEqual(synergy_pkg.available, 1)
        self.assertEqual(synergy_pkg.utilization_pct, 0.0)

        # No checkouts, no anomalies
        self.assertEqual(len(snapshot.checkouts), 0)
        self.assertEqual(len(snapshot.anomalies), 0)

    def test_fixture_2_synergy_active_package_authority(self):
        """
        Fixture 2: Synergy active checkout.
        Enforces Rule 2: 77800MFS_T_F (1/1) + 88232MFS_2027_0F (1/1) = EXACTLY ONE physical checkout.
        """
        raw_text = load_fixture("synergy_active.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.6,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.UP.value)

        # Package utilization
        pkg_map = {p.feature_code: p for p in snapshot.packages}
        synergy_pkg = pkg_map["77800MFS_T_F"]
        self.assertEqual(synergy_pkg.total_issued, 1)
        self.assertEqual(synergy_pkg.in_use, 1)
        self.assertEqual(synergy_pkg.available, 0)
        self.assertEqual(synergy_pkg.utilization_pct, 100.0)

        # Crucial: Exactly 1 physical checkout, NOT 2
        self.assertEqual(len(snapshot.checkouts), 1)
        co = snapshot.checkouts[0]

        self.assertEqual(co.username, "UnoTEAM-0144")
        self.assertEqual(co.machine_name, "LAPTOP-CA2QN87F")
        self.assertEqual(co.package_feature, "77800MFS_T_F")
        self.assertEqual(co.selected_component, "88232MFS_2027_0F")
        self.assertEqual(co.pid, "24336")
        self.assertEqual(co.checkout_time, "Fri 9/18 11:23")
        self.assertEqual(co.checkout_time_precision, "MINUTE")
        self.assertFalse(co.is_borrowed)
        self.assertFalse(co.is_incomplete)
        self.assertIsNone(co.anomaly_note)

        # Deterministic checkout ID
        expected_id = PhysicalCheckout.generate_id(
            server_hostname="LAPTOP-CA2QN87F",
            username="UnoTEAM-0144",
            machine_name="LAPTOP-CA2QN87F",
            selected_component_code="88232MFS_2027_0F",
            checkout_time_minute_str="Fri 9/18 11:23",
            pid="24336",
        )
        self.assertEqual(co.checkout_id, expected_id)
        self.assertEqual(len(snapshot.anomalies), 0)

    def test_fixture_3_synergy_released(self):
        """Fixture 3: Synergy release state."""
        raw_text = load_fixture("synergy_released.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.5,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.UP.value)
        pkg_map = {p.feature_code: p for p in snapshot.packages}
        self.assertEqual(pkg_map["77800MFS_T_F"].in_use, 0)
        self.assertEqual(len(snapshot.checkouts), 0)

    def test_fixture_4_flexnet_error_15(self):
        """Fixture 4: Cannot connect to server system (-15)."""
        raw_text = load_fixture("server_error_15.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=1,
            stdout=raw_text,
            stderr="",
            duration_seconds=2.1,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.DOWN.value)
        self.assertEqual(snapshot.server.error_code, -15)
        self.assertIn("Cannot connect to license server system", snapshot.server.error_message)
        self.assertEqual(len(snapshot.packages), 0)
        self.assertEqual(len(snapshot.checkouts), 0)

    def test_fixture_5_flexnet_error_96(self):
        """Fixture 5: License server machine is down or not responding (-96)."""
        raw_text = load_fixture("server_error_96.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=1,
            stdout=raw_text,
            stderr="",
            duration_seconds=5.3,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.DOWN.value)
        self.assertEqual(snapshot.server.error_code, -96)
        self.assertIn("License server machine is down or not responding", snapshot.server.error_message)

    def test_fixture_6_unknown_mfaa_preserved(self):
        """
        Fixture 6: Server B output with 76800MFAA_T_F.
        Enforces Rule 3 & 4: Unknown features preserved, classified as UNKNOWN catalog status, no crash.
        """
        raw_text = load_fixture("server_mfaa.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.6,
        )
        snapshot = self.parser.parse_execution_result(result)

        self.assertEqual(snapshot.server.status, ServerStatus.UP.value)
        self.assertEqual(snapshot.server.hostname, "DESKTOP-23TMNR6")

        # Find MFAA package
        pkg_map = {p.feature_code: p for p in snapshot.packages}
        self.assertIn("76800MFAA_T_F", pkg_map)
        mfaa_pkg = pkg_map["76800MFAA_T_F"]
        self.assertEqual(mfaa_pkg.total_issued, 4)
        self.assertEqual(mfaa_pkg.in_use, 0)
        self.assertEqual(mfaa_pkg.available, 4)
        self.assertEqual(mfaa_pkg.catalog_status, CatalogStatus.UNKNOWN.value)
        self.assertEqual(mfaa_pkg.product_family, "MFAA")

        # Check that child MFAA components are also preserved in features
        feat_map = {f.feature_code: f for f in snapshot.features}
        self.assertIn("88225MFAA_2027_0F", feat_map)
        comp = feat_map["88225MFAA_2027_0F"]
        self.assertEqual(comp.feature_type, FeatureType.COMPONENT.value)
        self.assertEqual(comp.total_issued, 4)

    def test_vendor_down_state(self):
        """Rule 6: lmgrd is UP, but adskflex is DOWN."""
        raw_text = (
            "LAPTOP-CA2QN87F: license server UP (MASTER) v11.19.9\n"
            "Vendor daemon status (on LAPTOP-CA2QN87F):\n"
            "  adskflex: DOWN\n"
        )
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.4,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(snapshot.server.status, ServerStatus.VENDOR_DOWN.value)
        self.assertIn("adskflex vendor daemon is down", snapshot.server.error_message)

    def test_timeout_execution_result(self):
        """Execution timeout sets status DOWN with error -96."""
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=1,
            stdout="",
            stderr="Query timed out",
            duration_seconds=15.0,
            timed_out=True,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(snapshot.server.status, ServerStatus.DOWN.value)
        self.assertEqual(snapshot.server.error_code, -96)
        self.assertIn("timed out", snapshot.server.error_message.lower())

    def test_empty_output_unknown_status(self):
        """Empty output results in UNKNOWN status."""
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(snapshot.server.status, ServerStatus.UNKNOWN.value)

    def test_missing_component_anomaly(self):
        """Package consumer exists, but component line is missing."""
        raw_text = (
            "LAPTOP-CA2QN87F: license server UP (MASTER) v11.19.9\n"
            "  adskflex: UP v11.19.9\n"
            "Users of 77800MFS_T_F:  (Total of 1 license issued;  Total of 1 license in use)\n"
            '  "77800MFS_T_F" v1.000, vendor: adskflex\n'
            "    UserA Host1 Host1 88232MFS_2027_0F (v1.000) (LAPTOP/27000 101), start Fri 9/18 11:00, PID: 1111\n"
            "Users of 88232MFS_2027_0F:  (Total of 1 license issued;  Total of 0 licenses in use)\n"
        )
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.5,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(len(snapshot.checkouts), 1)
        self.assertTrue(snapshot.checkouts[0].is_incomplete)
        self.assertIn("no matching component entry", snapshot.checkouts[0].anomaly_note)
        self.assertEqual(len(snapshot.anomalies), 1)

    def test_orphan_component_anomaly(self):
        """Component consumer exists, but package line is missing."""
        raw_text = (
            "LAPTOP-CA2QN87F: license server UP (MASTER) v11.19.9\n"
            "  adskflex: UP v11.19.9\n"
            "Users of 77800MFS_T_F:  (Total of 1 license issued;  Total of 0 licenses in use)\n"
            "Users of 88232MFS_2027_0F:  (Total of 1 license issued;  Total of 1 license in use)\n"
            '  "88232MFS_2027_0F" v1.000, vendor: adskflex\n'
            "    UserB Host2 Host2 88232MFS_2027_0F (v1.0) (LAPTOP/27000 201), start Fri 9/18 11:10, PID: 2222\n"
        )
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.5,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(len(snapshot.checkouts), 1)
        self.assertTrue(snapshot.checkouts[0].is_incomplete)
        self.assertIn("Orphan component checkout", snapshot.checkouts[0].anomaly_note)

    def test_multiple_concurrent_consumers(self):
        """Multiple users on multi-seat package (Server B simulation)."""
        raw_text = (
            "DESKTOP-23TMNR6: license server UP (MASTER) v11.19.9\n"
            "  adskflex: UP v11.19.9\n"
            "Users of 77800MFS_T_F:  (Total of 4 licenses issued;  Total of 2 licenses in use)\n"
            "    User1 Host1 Host1 88232MFS_2027_0F (v1.000) (SERVER/27000 101), start Fri 9/18 10:00, PID: 1001\n"
            "    User2 Host2 Host2 88068MFS_2026_0F (v1.000) (SERVER/27000 102), start Fri 9/18 10:15, PID: 1002\n"
            "Users of 88232MFS_2027_0F:  (Total of 4 licenses issued;  Total of 1 license in use)\n"
            "    User1 Host1 Host1 88232MFS_2027_0F (v1.0) (SERVER/27000 201), start Fri 9/18 10:00, PID: 1001\n"
            "Users of 88068MFS_2026_0F:  (Total of 4 licenses issued;  Total of 1 license in use)\n"
            "    User2 Host2 Host2 88068MFS_2026_0F (v1.0) (SERVER/27000 202), start Fri 9/18 10:15, PID: 1002\n"
        )
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.5,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(len(snapshot.checkouts), 2)
        u1 = [c for c in snapshot.checkouts if c.username == "User1"][0]
        u2 = [c for c in snapshot.checkouts if c.username == "User2"][0]
        self.assertEqual(u1.selected_component, "88232MFS_2027_0F")
        self.assertEqual(u2.selected_component, "88068MFS_2026_0F")
        self.assertFalse(u1.is_incomplete)
        self.assertFalse(u2.is_incomplete)

    def test_deterministic_checkout_id(self):
        """Rule 7: Deterministic ID without random salts."""
        id1 = PhysicalCheckout.generate_id("LAPTOP-CA2QN87F", "UnoTEAM-0144", "LAPTOP-CA2QN87F", "88232MFS_2027_0F", "Fri 9/18 11:23", "24336")
        id2 = PhysicalCheckout.generate_id("laptop-ca2qn87f", "unoteam-0144", "laptop-ca2qn87f", "88232mfs_2027_0f", "Fri 9/18 11:23", "24336")
        self.assertEqual(id1, id2)
        # Verify changes in PID or user changes the hash
        id3 = PhysicalCheckout.generate_id("LAPTOP-CA2QN87F", "User2", "LAPTOP-CA2QN87F", "88232MFS_2027_0F", "Fri 9/18 11:23", "24336")
        self.assertNotEqual(id1, id3)

    def test_server_b_multi_seat_capacities(self):
        """Stage 2: Validate Server B multi-seat pools (Synergy=4, Insight=12, MFAA=4)."""
        raw_text = load_fixture("server_mfaa.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.6,
        )
        snapshot = self.parser.parse_execution_result(result)
        self.assertEqual(snapshot.server.hostname, "DESKTOP-23TMNR6")
        pkg_map = {p.feature_code: p for p in snapshot.packages}

        # Multi-seat capacity checks
        self.assertEqual(pkg_map["77400MFIA_T_F"].total_issued, 12)
        self.assertEqual(pkg_map["77400MFIA_T_F"].available, 12)
        self.assertEqual(pkg_map["77400MFIA_T_F"].in_use, 0)

        self.assertEqual(pkg_map["77800MFS_T_F"].total_issued, 4)
        self.assertEqual(pkg_map["77800MFS_T_F"].available, 4)
        self.assertEqual(pkg_map["77800MFS_T_F"].in_use, 0)

        self.assertEqual(pkg_map["76800MFAA_T_F"].total_issued, 4)
        self.assertEqual(pkg_map["76800MFAA_T_F"].available, 4)
        self.assertEqual(pkg_map["76800MFAA_T_F"].in_use, 0)

    def test_checkout_id_isolation_cross_server(self):
        """
        Stage 2 Objective 8: Confirm Server A checkout and Server B checkout
        cannot generate the same ID when all other fields match.
        """
        id_server_a = PhysicalCheckout.generate_id(
            server_hostname="LAPTOP-CA2QN87F",
            username="UnoTEAM-0144",
            machine_name="WORKSTATION-1",
            selected_component_code="88232MFS_2027_0F",
            checkout_time_minute_str="Fri 9/18 11:23",
            pid="12345",
        )
        id_server_b = PhysicalCheckout.generate_id(
            server_hostname="DESKTOP-23TMNR6",
            username="UnoTEAM-0144",
            machine_name="WORKSTATION-1",
            selected_component_code="88232MFS_2027_0F",
            checkout_time_minute_str="Fri 9/18 11:23",
            pid="12345",
        )
        self.assertNotEqual(id_server_a, id_server_b)
        self.assertTrue(len(id_server_a) == 64)
        self.assertTrue(len(id_server_b) == 64)

    def test_unknown_mfaa_does_not_invent_product_name(self):
        """Stage 2 Objective 9: Unknown feature catalog status UNKNOWN, product_name None."""
        raw_text = load_fixture("server_mfaa.txt")
        result = ExecutionResult(
            command=["lmutil", "lmstat"],
            exit_code=0,
            stdout=raw_text,
            stderr="",
            duration_seconds=0.6,
        )
        snapshot = self.parser.parse_execution_result(result)
        pkg_map = {p.feature_code: p for p in snapshot.packages}
        mfaa = pkg_map["76800MFAA_T_F"]
        self.assertEqual(mfaa.catalog_status, CatalogStatus.UNKNOWN.value)
        self.assertIsNone(mfaa.product_name)


if __name__ == "__main__":
    unittest.main()

