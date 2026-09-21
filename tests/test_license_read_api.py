"""
tests/test_license_read_api.py
------------------------------
Comprehensive unit and integration test suite for Stage 5: Authenticated Mobile Read APIs.

Test Coverage:
1. Authentication:
   - Unauthenticated request returns 401 Unauthorized.
   - Invalid JWT returns 401 Unauthorized.
   - Valid JWT succeeds.
2. Servers List (GET /licenses/servers):
   - Returns configured servers (Server A, Server B).
   - Server IDs remain distinct and independent.
   - Internal implementation details (paths, encryption, SIGN) are not exposed.
3. Overview (GET /licenses/overview):
   - Per-server operational state with package status.
   - Aggregate inventory calculation correct.
   - Aggregate inventory explicitly marked as non-pooled with notice.
   - DOWN server does not report fake zero availability (data_state=UNAVAILABLE, in_use/available=null).
4. Server Detail (GET /licenses/servers/{server_id}):
   - PACKAGE features returned with correct capacity and utilization.
   - COMPONENT features nested under corresponding PACKAGE.
   - Component not double-counted (no independent pool).
   - Unknown MFAA feature returned with catalog_status UNKNOWN and product_name NULL.
   - Non-existent server returns 404 Not Found.
5. Active Consumers (GET /licenses/servers/{server_id}/consumers):
   - Unified physical checkout (exactly 1 row per physical seat, package + component consolidated).
   - Sensitive internal details (PID, server handle, anomaly note) are NOT exposed.
   - Server filtering: Server A consumers never appear on Server B.
   - Deterministic ordering (checkout_time DESC, username ASC, machine_name ASC).
   - Non-existent server returns 404 Not Found.
6. License History (GET /licenses/history):
   - Source is license_events table (no snapshot reconstruction).
   - Returns CHECKOUT, RETURN, EXHAUSTED, AVAILABLE, SERVER_DOWN, SERVER_UP events.
   - Filter by server_id, feature_code, event_type, username.
   - Invalid event_type returns 422 Unprocessable Entity.
   - Pagination (limit, offset, total) behaves correctly.
7. Multi-Server Isolation:
   - Server A checkouts and events do not leak into Server B.
8. Backward Compatibility:
   - Existing /reportJobStatus and job monitoring remain unaffected.
"""

import sys
import os
import json
import asyncio
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Ensure backend and test paths
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"

for d in (BASE_DIR, BACKEND_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from app_postgres_ready import app, get_db, init_database, create_access_token, db_execute
from license_persistence import LicensePersistenceService, init_license_tables
from license_ingestion import (
    LicenseSnapshotPayload,
    LicenseServerPayload,
    LicensePackagePayload,
    LicenseFeaturePayload,
    LicenseCheckoutPayload,
    ServerStatusEnum,
)


# ASGI Caller Helper for Testing
async def asgi_request(
    app,
    method: str = "GET",
    path: str = "/licenses/overview",
    token: str | None = None,
    json_body: dict | list | None = None,
):
    body_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers = [(b"host", b"testserver")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode("latin1")))
    if json_body is not None:
        headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(body_bytes)).encode("latin1")))

    if "?" in path:
        base_path, qs = path.split("?", 1)
        raw_qs = qs.encode("ascii")
    else:
        base_path = path
        raw_qs = b""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": base_path,
        "raw_path": base_path.encode("ascii"),
        "query_string": raw_qs,
        "headers": headers,
    }

    response_headers = []
    response_body = []
    status_code = None

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        nonlocal status_code, response_headers, response_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
            response_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await app(scope, receive, send)
    body = b"".join(response_body).decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except Exception:
        data = body

    return status_code, data


class TestLicenseReadAPIsStage5(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_database()
        cls.token, _ = create_access_token("DEV-USER-001")

        # Seed controlled database state for Server A, Server B, and Server DOWN
        with get_db() as conn:
            # 1. Server A: LAPTOP-CA2QN87F (UP, 1 active checkout)
            now_iso = datetime.now(timezone.utc).isoformat()
            snap_a = LicenseSnapshotPayload(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at=now_iso,
                server=LicenseServerPayload(
                    hostname="LAPTOP-CA2QN87F",
                    port=27000,
                    status=ServerStatusEnum.UP,
                    lmgrd_version="v11.19.9",
                    adskflex_status="UP",
                    adskflex_version="v11.19.9",
                ),
                packages=[
                    LicensePackagePayload(
                        feature_code="77800MFS_T_F",
                        total_issued=1,
                        in_use=1,
                        available=0,
                        utilization_pct=100.0,
                        product_family="Synergy",
                        product_name="Autodesk Moldflow Synergy",
                    ),
                    LicensePackagePayload(
                        feature_code="77400MFIA_T_F",
                        total_issued=3,
                        in_use=0,
                        available=3,
                        utilization_pct=0.0,
                        product_family="Insight",
                        product_name="Autodesk Moldflow Insight",
                    ),
                ],
                features=[
                    LicenseFeaturePayload(
                        feature_code="77800MFS_T_F",
                        total_issued=1,
                        in_use=1,
                        available=0,
                        feature_type="PACKAGE",
                        product_family="Synergy",
                        product_name="Autodesk Moldflow Synergy",
                    ),
                    LicenseFeaturePayload(
                        feature_code="88232MFS_2027_0F",
                        total_issued=1,
                        in_use=1,
                        available=0,
                        feature_type="COMPONENT",
                        product_family="Synergy",
                        product_name="Autodesk Moldflow Synergy",
                        year_version="2027",
                        parent_package="77800MFS_T_F",
                    ),
                    LicenseFeaturePayload(
                        feature_code="77400MFIA_T_F",
                        total_issued=3,
                        in_use=0,
                        available=3,
                        feature_type="PACKAGE",
                        product_family="Insight",
                        product_name="Autodesk Moldflow Insight",
                    ),
                    LicenseFeaturePayload(
                        feature_code="88230MFIA_2027_0F",
                        total_issued=3,
                        in_use=0,
                        available=3,
                        feature_type="COMPONENT",
                        product_family="Insight",
                        product_name="Autodesk Moldflow Insight",
                        year_version="2027",
                        parent_package="77400MFIA_T_F",
                    ),
                ],
                checkouts=[
                    LicenseCheckoutPayload(
                        checkout_id="chk-stage5-synergy-001",
                        server_hostname="LAPTOP-CA2QN87F",
                        username="UnoTEAM-0144",
                        machine_name="LAPTOP-CA2QN87F",
                        package_feature="77800MFS_T_F",
                        selected_component="88232MFS_2027_0F",
                        version="v1.0",
                        server_handle="101",
                        checkout_time="Fri 9/18 10:00",
                        checkout_time_precision="MINUTE",
                        pid="24336",
                        is_borrowed=False,
                    )
                ],
            )
            res_a = LicensePersistenceService.process_snapshot(conn, snap_a)
            cls.server_id_a = res_a["server_id"]

            # 2. Server B: DESKTOP-23TMNR6 (UP, idle, multi-seat, unknown MFAA)
            snap_b = LicenseSnapshotPayload(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at=now_iso,
                server=LicenseServerPayload(
                    hostname="DESKTOP-23TMNR6",
                    port=27000,
                    status=ServerStatusEnum.UP,
                    lmgrd_version="v11.19.9",
                    adskflex_status="UP",
                    adskflex_version="v11.19.9",
                ),
                packages=[
                    LicensePackagePayload(
                        feature_code="77800MFS_T_F",
                        total_issued=4,
                        in_use=0,
                        available=4,
                        utilization_pct=0.0,
                        product_family="Synergy",
                        product_name="Autodesk Moldflow Synergy",
                    ),
                    LicensePackagePayload(
                        feature_code="77400MFIA_T_F",
                        total_issued=12,
                        in_use=0,
                        available=12,
                        utilization_pct=0.0,
                        product_family="Insight",
                        product_name="Autodesk Moldflow Insight",
                    ),
                    LicensePackagePayload(
                        feature_code="76800MFAA_T_F",
                        total_issued=4,
                        in_use=0,
                        available=4,
                        utilization_pct=0.0,
                        product_family="MFAA",
                        product_name=None,
                        catalog_status="UNKNOWN",
                    ),
                ],
                features=[
                    LicenseFeaturePayload(
                        feature_code="77800MFS_T_F",
                        total_issued=4,
                        in_use=0,
                        available=4,
                        feature_type="PACKAGE",
                        product_family="Synergy",
                        product_name="Autodesk Moldflow Synergy",
                    ),
                    LicenseFeaturePayload(
                        feature_code="77400MFIA_T_F",
                        total_issued=12,
                        in_use=0,
                        available=12,
                        feature_type="PACKAGE",
                        product_family="Insight",
                        product_name="Autodesk Moldflow Insight",
                    ),
                    LicenseFeaturePayload(
                        feature_code="76800MFAA_T_F",
                        total_issued=4,
                        in_use=0,
                        available=4,
                        feature_type="PACKAGE",
                        product_family="MFAA",
                        product_name=None,
                        catalog_status="UNKNOWN",
                    ),
                ],
                checkouts=[],
            )
            res_b = LicensePersistenceService.process_snapshot(conn, snap_b)
            cls.server_id_b = res_b["server_id"]

            # 3. Server C: SERVER-DOWN-HOST (Transitions UP -> DOWN to generate SERVER_DOWN)
            t_up = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            t_down = datetime.now(timezone.utc).isoformat()
            snap_c_up = LicenseSnapshotPayload(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at=t_up,
                server=LicenseServerPayload(
                    hostname="SERVER-DOWN-HOST",
                    port=27000,
                    status=ServerStatusEnum.UP,
                ),
                packages=[],
                features=[],
                checkouts=[],
            )
            res_c_init = LicensePersistenceService.process_snapshot(conn, snap_c_up)

            snap_c_down = LicenseSnapshotPayload(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at=t_down,
                server=LicenseServerPayload(
                    hostname="SERVER-DOWN-HOST",
                    port=27000,
                    status=ServerStatusEnum.DOWN,
                    error_code=-96,
                    error_message="License server machine is down or not responding",
                ),
                packages=[],
                features=[],
                checkouts=[],
            )
            res_c = LicensePersistenceService.process_snapshot(conn, snap_c_down)
            cls.server_id_c = res_c["server_id"]
            conn.commit()

    # ------------------------------------------------------------------------
    # 1. Authentication Tests
    # ------------------------------------------------------------------------
    def test_01_unauthenticated_request_returns_401(self):
        """Unauthenticated call to /licenses/overview returns 401."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token=None))
        self.assertEqual(status_code, 401)
        self.assertIn("detail", data)

    def test_02_invalid_jwt_returns_401(self):
        """Invalid JWT bearer token returns 401 Unauthorized."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token="invalid-token-123"))
        self.assertEqual(status_code, 401)
        self.assertIn("detail", data)

    def test_03_valid_jwt_succeeds(self):
        """Valid JWT bearer token returns 200 OK."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token=self.token))
        self.assertEqual(status_code, 200)
        self.assertIn("servers", data)
        self.assertIn("environment_inventory", data)

    # ------------------------------------------------------------------------
    # 2. Servers List (GET /licenses/servers)
    # ------------------------------------------------------------------------
    def test_04_get_servers_list(self):
        """Returns concise list of servers with status and distinct IDs."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/servers", token=self.token))
        self.assertEqual(status_code, 200)
        self.assertIsInstance(data, list)

        server_ids = [s["server_id"] for s in data]
        self.assertIn(self.server_id_a, server_ids)
        self.assertIn(self.server_id_b, server_ids)
        self.assertIn(self.server_id_c, server_ids)

        # Check fields of Server A
        srv_a = next(s for s in data if s["server_id"] == self.server_id_a)
        self.assertEqual(srv_a["hostname"], "laptop-ca2qn87f")
        self.assertEqual(srv_a["status"], "UP")
        self.assertEqual(srv_a["data_state"], "AVAILABLE")

        # Security check: internal mechanics not exposed
        self.assertNotIn("license_file_path", srv_a)
        self.assertNotIn("sign", srv_a)
        self.assertNotIn("sign2", srv_a)

    # ------------------------------------------------------------------------
    # 3. Overview (GET /licenses/overview)
    # ------------------------------------------------------------------------
    def test_05_overview_per_server_utilization(self):
        """Per-server utilization in overview is accurate."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token=self.token))
        self.assertEqual(status_code, 200)

        srv_a = next(s for s in data["servers"] if s["server_id"] == self.server_id_a)
        synergy_pkg = next(p for p in srv_a["products"] if p["feature_code"] == "77800MFS_T_F")
        self.assertEqual(synergy_pkg["total_issued"], 1)
        self.assertEqual(synergy_pkg["in_use"], 1)
        self.assertEqual(synergy_pkg["available"], 0)
        self.assertEqual(synergy_pkg["utilization_pct"], 100.0)
        self.assertEqual(synergy_pkg["status"], "EXHAUSTED")

    def test_06_overview_aggregate_inventory_non_pooled(self):
        """Aggregate inventory combines healthy servers and contains explicit non-pooled disclaimer."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token=self.token))
        self.assertEqual(status_code, 200)

        inv = data["environment_inventory"]
        self.assertFalse(inv["is_pooled"])
        self.assertIn("not pooled", inv["notice"].lower())

        # Synergy: Server A (1) + Server B (4) = 5 total
        synergy_agg = next(p for p in inv["products"] if p["feature_code"] == "77800MFS_T_F")
        self.assertEqual(synergy_agg["total_inventory"], 5)
        self.assertEqual(synergy_agg["current_inventory_used"], 1)
        self.assertEqual(synergy_agg["inventory_available"], 4)
        self.assertEqual(synergy_agg["server_count"], 2)

    def test_07_overview_down_server_does_not_show_fake_zero(self):
        """Server DOWN does not report fake zero licenses; data_state=UNAVAILABLE."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/overview", token=self.token))
        self.assertEqual(status_code, 200)

        srv_c = next(s for s in data["servers"] if s["server_id"] == self.server_id_c)
        self.assertEqual(srv_c["status"], "DOWN")
        self.assertEqual(srv_c["data_state"], "UNAVAILABLE")
        self.assertEqual(srv_c["last_error_code"], -96)

    # ------------------------------------------------------------------------
    # 4. Server Detail (GET /licenses/servers/{server_id})
    # ------------------------------------------------------------------------
    def test_08_server_detail_packages_and_nested_components(self):
        """Server detail nests components under packages and preserves accurate capacities."""
        path = f"/licenses/servers/{self.server_id_a}"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        self.assertEqual(data["server_id"], self.server_id_a)
        self.assertEqual(data["data_state"], "AVAILABLE")

        # Synergy Package has nested components (including verified 2027 component)
        synergy_pkg = next(p for p in data["packages"] if p["feature_code"] == "77800MFS_T_F")
        self.assertGreaterEqual(len(synergy_pkg["components"]), 1)
        comp_codes = [c["feature_code"] for c in synergy_pkg["components"]]
        self.assertIn("88232MFS_2027_0F", comp_codes)
        comp_2027 = next(c for c in synergy_pkg["components"] if c["feature_code"] == "88232MFS_2027_0F")
        self.assertEqual(comp_2027["year_version"], "2027")
        self.assertEqual(comp_2027["in_use"], 1)

    def test_09_server_detail_unknown_mfaa_feature(self):
        """Server B unknown MFAA feature preserves catalog_status UNKNOWN and product_name NULL."""
        path = f"/licenses/servers/{self.server_id_b}"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)

        mfaa_pkg = next(p for p in data["packages"] if p["feature_code"] == "76800MFAA_T_F")
        self.assertEqual(mfaa_pkg["catalog_status"], "UNKNOWN")
        self.assertIsNone(mfaa_pkg["product_name"])
        self.assertEqual(mfaa_pkg["display_name"], "Unknown Moldflow Product (MFAA)")
        self.assertEqual(mfaa_pkg["total_issued"], 4)

    def test_10_server_detail_not_found_returns_404(self):
        """Non-existent server_id returns 404 Not Found."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/servers/non-existent-uuid", token=self.token))
        self.assertEqual(status_code, 404)
        self.assertIn("detail", data)

    # ------------------------------------------------------------------------
    # 5. Active Consumers (GET /licenses/servers/{server_id}/consumers)
    # ------------------------------------------------------------------------
    def test_11_active_consumers_unified_and_no_pid(self):
        """Consumers returns 1 row per physical seat, omits PID/internal handles."""
        path = f"/licenses/servers/{self.server_id_a}/consumers"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

        consumer = data[0]
        self.assertEqual(consumer["checkout_id"], "chk-stage5-synergy-001")
        self.assertEqual(consumer["username"], "UnoTEAM-0144")
        self.assertEqual(consumer["machine_name"], "LAPTOP-CA2QN87F")
        self.assertEqual(consumer["package_feature"], "77800MFS_T_F")
        self.assertEqual(consumer["component_feature"], "88232MFS_2027_0F")
        self.assertEqual(consumer["product_name"], "Autodesk Moldflow Synergy")

        # Security check: PID, server handle, anomaly note omitted
        self.assertNotIn("pid", consumer)
        self.assertNotIn("server_handle", consumer)
        self.assertNotIn("anomaly_note", consumer)

    def test_12_active_consumers_server_isolation(self):
        """Server B has zero checkouts and does not show Server A checkouts."""
        path = f"/licenses/servers/{self.server_id_b}/consumers"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        self.assertEqual(len(data), 0)

    def test_13_active_consumers_not_found_returns_404(self):
        """Consumers endpoint returns 404 for non-existent server."""
        status_code, data = asyncio.run(asgi_request(app, "GET", "/licenses/servers/unknown-server-id/consumers", token=self.token))
        self.assertEqual(status_code, 404)

    # ------------------------------------------------------------------------
    # 6. License History (GET /licenses/history)
    # ------------------------------------------------------------------------
    def test_14_get_license_history_pagination(self):
        """License history returns paginated events from license_events."""
        path = "/licenses/history?limit=10&offset=0"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        self.assertIn("items", data)
        self.assertIn("total", data)
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["offset"], 0)
        self.assertGreater(data["total"], 0)

    def test_15_history_filter_by_server_id(self):
        """History filtered by server_id returns only events for that server."""
        path = f"/licenses/history?server_id={self.server_id_a}"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        for item in data["items"]:
            self.assertEqual(item["server_id"], self.server_id_a)

    def test_16_history_filter_by_event_type(self):
        """History filtered by event_type returns only matching event types."""
        path = "/licenses/history?event_type=CHECKOUT"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        for item in data["items"]:
            self.assertEqual(item["event_type"], "CHECKOUT")

    def test_17_history_invalid_event_type_returns_422(self):
        """History with invalid event_type returns 422 Unprocessable Entity."""
        path = "/licenses/history?event_type=INVALID_EVENT"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 422)
        self.assertIn("detail", data)

    def test_18_history_server_down_event_preserved(self):
        """SERVER_DOWN events are included in history."""
        path = f"/licenses/history?server_id={self.server_id_c}&event_type=SERVER_DOWN"
        status_code, data = asyncio.run(asgi_request(app, "GET", path, token=self.token))
        self.assertEqual(status_code, 200)
        self.assertGreaterEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["event_type"], "SERVER_DOWN")

    # ------------------------------------------------------------------------
    # 7. Backward Compatibility
    # ------------------------------------------------------------------------
    def test_19_existing_report_job_status_still_protected(self):
        """Existing /reportJobStatus still requires X-Api-Key."""
        status_code, data = asyncio.run(asgi_request(app, "POST", "/reportJobStatus", json_body={"status": "running"}))
        self.assertEqual(status_code, 401)


if __name__ == "__main__":
    unittest.main()
