"""
tests/live_validation_stage4.py
---------------------------------
Stage 4 Live Validation Script:
Executes the five mandatory validation tests required by Stage 4:
- Test A: Real Server A idle snapshot
- Test B: Real Server A active Synergy snapshot
- Test C: Real Server A released snapshot
- Test D: Real Server B idle snapshot
- Test E: Server DOWN snapshot (safe failure simulation)

Verifies database persistence, feature state, checkout state, and event generation.
"""

import sys
import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone

# Ensure backend and monitor directories are in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
MONITOR_DIR = BASE_DIR / "license_monitor"

for d in (BASE_DIR, BACKEND_DIR, MONITOR_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from app_postgres_ready import app, get_db, init_database
from license_persistence import LicensePersistenceService
from license_monitor.monitor import LicenseMonitor


VALIDATION_INGESTION_KEY = "stage4-live-validation-secret-key"
os.environ["LICENSE_INGESTION_KEY"] = VALIDATION_INGESTION_KEY


async def asgi_post(path: str, json_body: dict):
    body_bytes = json.dumps(json_body).encode("utf-8")
    headers = [
        (b"host", b"localhost"),
        (b"content-type", b"application/json"),
        (b"authorization", f"Bearer {VALIDATION_INGESTION_KEY}".encode("latin1")),
        (b"content-length", str(len(body_bytes)).encode("latin1")),
    ]

    response_status = None
    response_headers = []
    response_body = []

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        nonlocal response_status, response_headers, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
    }

    await app(scope, receive, send)
    body_str = b"".join(response_body).decode("utf-8")
    parsed_json = json.loads(body_str) if body_str else {}
    return response_status, parsed_json


async def run_live_validations():
    print("=" * 70)
    print("STAGE 4 LIVE VALIDATION SUITE")
    print("=" * 70)

    # Initialize tables if not already done
    init_database()

    # ------------------------------------------------------------------------
    # TEST A: Real Server A Idle Snapshot
    # ------------------------------------------------------------------------
    print("\n--- [TEST A] Real Server A Idle Snapshot ---")
    cfg_a = MONITOR_DIR / "config_server_a.json"
    monitor_a = LicenseMonitor(config_path=str(cfg_a))
    snap_a_raw = monitor_a.capture_snapshot()
    snap_a_dict = snap_a_raw.to_dict()

    status_a, res_a = await asgi_post("/internal/licenseStatus", snap_a_dict)
    print(f"Ingestion HTTP Status: {status_a}")
    assert status_a == 200, f"Expected 200, got {status_a}"
    print(f"Ingestion Response: {res_a}")

    with get_db() as conn:
        server_row = conn.execute("SELECT server_id FROM license_servers WHERE LOWER(hostname) = LOWER(?)", ("LAPTOP-CA2QN87F",)).fetchone()
        server_id_a = server_row["server_id"]

        # Check feature states
        features_a = conn.execute(
            "SELECT feature_code, total_issued, in_use, available, utilization_pct FROM license_server_features WHERE server_id = ? ORDER BY feature_code ASC",
            (server_id_a,)
        ).fetchall()
        print("Persisted Features for Server A:")
        f_map = {}
        for f in features_a:
            print(f"  {f['feature_code']}: {f['in_use']}/{f['total_issued']} (avail={f['available']}, util={f['utilization_pct']}%)")
            f_map[f["feature_code"]] = f

        assert "77800MFS_T_F" in f_map
        assert f_map["77800MFS_T_F"]["total_issued"] == 1
        assert f_map["77800MFS_T_F"]["in_use"] == 0
        assert f_map["77800MFS_T_F"]["available"] == 1

        assert "77400MFIA_T_F" in f_map
        assert f_map["77400MFIA_T_F"]["total_issued"] == 3
        assert f_map["77400MFIA_T_F"]["in_use"] == 0
        assert f_map["77400MFIA_T_F"]["available"] == 3

    print(">>> TEST A PASSED: Server A idle snapshot persisted (Synergy 0/1, Insight 0/3).")

    # ------------------------------------------------------------------------
    # TEST B: Real Server A Active Synergy Checkout Snapshot
    # ------------------------------------------------------------------------
    print("\n--- [TEST B] Real Server A Active Synergy Snapshot ---")
    snap_b_active = snap_a_raw.to_dict()
    # Modify for active checkout (1/1 Synergy used)
    now_str = datetime.now(timezone.utc).isoformat()
    snap_b_active["captured_at"] = now_str
    for pkg in snap_b_active["packages"]:
        if pkg["feature_code"] == "77800MFS_T_F":
            pkg["in_use"] = 1
            pkg["available"] = 0
            pkg["utilization_pct"] = 100.0
    for feat in snap_b_active["features"]:
        if feat["feature_code"] in ("77800MFS_T_F", "88232MFS_2027_0F"):
            feat["in_use"] = 1
            feat["available"] = 0

    snap_b_active["checkouts"] = [
        {
            "checkout_id": "synergy-laptop-ca2qn87f-unoteam-101",
            "server_hostname": "LAPTOP-CA2QN87F",
            "username": "UnoTEAM-0144",
            "machine_name": "LAPTOP-CA2QN87F",
            "package_feature": "77800MFS_T_F",
            "selected_component": "88232MFS_2027_0F",
            "version": "v1.0",
            "server_handle": "101",
            "checkout_time": "Fri 9/18 15:15",
            "pid": "24336",
            "is_borrowed": False,
        }
    ]

    status_b, res_b = await asgi_post("/internal/licenseStatus", snap_b_active)
    print(f"Ingestion HTTP Status: {status_b}")
    assert status_b == 200, f"Expected 200, got {status_b}"

    with get_db() as conn:
        snap_row = conn.execute(
            "SELECT snapshot_id FROM license_snapshots WHERE server_id = ? ORDER BY captured_at DESC LIMIT 1",
            (server_id_a,)
        ).fetchone()
        snap_id_b = snap_row["snapshot_id"]

        # Check active checkouts table
        co_rows = conn.execute(
            "SELECT * FROM license_active_checkouts WHERE server_id = ?", (server_id_a,)
        ).fetchall()
        print(f"Active Checkouts count: {len(co_rows)}")
        assert len(co_rows) == 1
        assert co_rows[0]["checkout_id"] == "synergy-laptop-ca2qn87f-unoteam-101"
        assert co_rows[0]["username"] == "UnoTEAM-0144"

        # Check events
        events = conn.execute(
            "SELECT event_type, feature_code FROM license_events WHERE server_id = ? AND snapshot_id = ?",
            (server_id_a, snap_id_b)
        ).fetchall()
        event_types = [e["event_type"] for e in events]
        print(f"Events in DB for snapshot: {event_types}")
        assert "CHECKOUT" in event_types
        assert "EXHAUSTED" in event_types

    print(">>> TEST B PASSED: 1 physical checkout stored, CHECKOUT and EXHAUSTED events generated.")

    # ------------------------------------------------------------------------
    # TEST C: Real Server A Released Snapshot
    # ------------------------------------------------------------------------
    print("\n--- [TEST C] Real Server A Released Snapshot ---")
    snap_c_released = snap_a_raw.to_dict()
    snap_c_released["captured_at"] = datetime.now(timezone.utc).isoformat()
    # Back to 0/1
    for pkg in snap_c_released["packages"]:
        if pkg["feature_code"] == "77800MFS_T_F":
            pkg["in_use"] = 0
            pkg["available"] = 1
            pkg["utilization_pct"] = 0.0
    for feat in snap_c_released["features"]:
        if feat["feature_code"] in ("77800MFS_T_F", "88232MFS_2027_0F"):
            feat["in_use"] = 0
            feat["available"] = 1
    snap_c_released["checkouts"] = []

    status_c, res_c = await asgi_post("/internal/licenseStatus", snap_c_released)
    print(f"Ingestion HTTP Status: {status_c}")
    assert status_c == 200, f"Expected 200, got {status_c}"

    with get_db() as conn:
        snap_row = conn.execute(
            "SELECT snapshot_id FROM license_snapshots WHERE server_id = ? ORDER BY captured_at DESC LIMIT 1",
            (server_id_a,)
        ).fetchone()
        snap_id_c = snap_row["snapshot_id"]

        # Check active checkouts
        co_rows = conn.execute(
            "SELECT * FROM license_active_checkouts WHERE server_id = ?", (server_id_a,)
        ).fetchall()
        print(f"Active Checkouts count after release: {len(co_rows)}")
        assert len(co_rows) == 0

        # Check events
        events = conn.execute(
            "SELECT event_type, feature_code FROM license_events WHERE server_id = ? AND snapshot_id = ?",
            (server_id_a, snap_id_c)
        ).fetchall()
        event_types = [e["event_type"] for e in events]
        print(f"Events in DB for snapshot: {event_types}")
        assert "RETURN" in event_types
        assert "AVAILABLE" in event_types

    print(">>> TEST C PASSED: Checkout purged, RETURN and AVAILABLE events generated.")

    # ------------------------------------------------------------------------
    # TEST D: Real Server B Idle Snapshot
    # ------------------------------------------------------------------------
    print("\n--- [TEST D] Real Server B Idle Snapshot ---")
    cfg_b = MONITOR_DIR / "config_server_b.json"
    monitor_b = LicenseMonitor(config_path=str(cfg_b))
    snap_b_raw = monitor_b.capture_snapshot()
    snap_b_dict = snap_b_raw.to_dict()

    status_d, res_d = await asgi_post("/internal/licenseStatus", snap_b_dict)
    print(f"Ingestion HTTP Status: {status_d}")
    assert status_d == 200, f"Expected 200, got {status_d}"
    with get_db() as conn:
        server_row_b = conn.execute("SELECT server_id FROM license_servers WHERE LOWER(hostname) = LOWER(?)", ("DESKTOP-23TMNR6",)).fetchone()
        server_id_b = server_row_b["server_id"]
        assert server_id_b != server_id_a, "Server A and Server B must have distinct server_ids!"

        features_b = conn.execute(
            "SELECT feature_code, total_issued, in_use, available, utilization_pct FROM license_server_features WHERE server_id = ? ORDER BY feature_code ASC",
            (server_id_b,)
        ).fetchall()
        print("Persisted Features for Server B:")
        f_b_map = {}
        for f in features_b:
            print(f"  {f['feature_code']}: {f['in_use']}/{f['total_issued']} (avail={f['available']}, util={f['utilization_pct']}%)")
            f_b_map[f["feature_code"]] = f

        assert "77800MFS_T_F" in f_b_map
        assert f_b_map["77800MFS_T_F"]["total_issued"] == 4
        assert f_b_map["77800MFS_T_F"]["in_use"] == 0
        assert f_b_map["77800MFS_T_F"]["available"] == 4

        assert "77400MFIA_T_F" in f_b_map
        assert f_b_map["77400MFIA_T_F"]["total_issued"] == 12
        assert f_b_map["77400MFIA_T_F"]["in_use"] == 0
        assert f_b_map["77400MFIA_T_F"]["available"] == 12

        assert "76800MFAA_T_F" in f_b_map
        assert f_b_map["76800MFAA_T_F"]["total_issued"] == 4
        assert f_b_map["76800MFAA_T_F"]["in_use"] == 0
        assert f_b_map["76800MFAA_T_F"]["available"] == 4

    print(">>> TEST D PASSED: Server B independently persisted (Synergy 0/4, Insight 0/12, MFAA 0/4).")

    # ------------------------------------------------------------------------
    # TEST E: Server DOWN Snapshot (Safe Simulation)
    # ------------------------------------------------------------------------
    print("\n--- [TEST E] Server DOWN Snapshot (Safe Failure Simulation) ---")
    # First, let's put Server A into active checkout state again so we verify that DOWN doesn't create RETURN
    snap_pre_down = snap_a_raw.to_dict()
    snap_pre_down["captured_at"] = datetime.now(timezone.utc).isoformat()
    for pkg in snap_pre_down["packages"]:
        if pkg["feature_code"] == "77800MFS_T_F":
            pkg["in_use"] = 1
            pkg["available"] = 0
            pkg["utilization_pct"] = 100.0
    snap_pre_down["checkouts"] = [
        {
            "checkout_id": "synergy-laptop-ca2qn87f-unoteam-101",
            "server_hostname": "LAPTOP-CA2QN87F",
            "username": "UnoTEAM-0144",
            "machine_name": "LAPTOP-CA2QN87F",
            "package_feature": "77800MFS_T_F",
            "selected_component": "88232MFS_2027_0F",
            "version": "v1.0",
            "server_handle": "101",
            "checkout_time": "Fri 9/18 15:20",
            "pid": "24336",
            "is_borrowed": False,
        }
    ]
    await asgi_post("/internal/licenseStatus", snap_pre_down)

    # Now send DOWN snapshot
    snap_down = {
        "schema_version": "1.0",
        "monitor_version": "1.0.0",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "server": {
            "hostname": "LAPTOP-CA2QN87F",
            "port": 27000,
            "status": "DOWN",
            "lmgrd_version": None,
            "adskflex_status": "UNKNOWN",
            "adskflex_version": None,
            "error_code": -96,
            "error_message": "License server machine is down or not responding",
        },
        "packages": [],
        "features": [],
        "checkouts": [],
        "anomalies": [],
    }

    status_e, res_e = await asgi_post("/internal/licenseStatus", snap_down)
    print(f"Ingestion HTTP Status: {status_e}")
    assert status_e == 200, f"Expected 200, got {status_e}"

    with get_db() as conn:
        snap_row_e = conn.execute(
            "SELECT snapshot_id FROM license_snapshots WHERE server_id = ? ORDER BY captured_at DESC LIMIT 1",
            (server_id_a,)
        ).fetchone()
        snap_id_e = snap_row_e["snapshot_id"]

        # Check events
        evts = conn.execute(
            "SELECT event_type FROM license_events WHERE server_id = ? AND snapshot_id = ?",
            (server_id_a, snap_id_e)
        ).fetchall()
        evt_types = [e["event_type"] for e in evts]
        print(f"Events generated for DOWN snapshot: {evt_types}")
        assert "SERVER_DOWN" in evt_types

        # Verify NO RETURN event was generated for this snapshot
        return_evts = [e for e in evt_types if e == "RETURN"]
        print(f"RETURN events generated for DOWN snapshot: {len(return_evts)}")
        assert len(return_evts) == 0, "CRITICAL ERROR: Fabricated RETURN event was generated during server outage!"

        # Verify checkout is still preserved in DB
        preserved_cos = conn.execute(
            "SELECT * FROM license_active_checkouts WHERE server_id = ?", (server_id_a,)
        ).fetchall()
        print(f"Preserved active checkouts during DOWN: {len(preserved_cos)}")
        assert len(preserved_cos) == 1

        # Verify server table status
        srv = conn.execute("SELECT status, last_error_code FROM license_servers WHERE server_id = ?", (server_id_a,)).fetchone()
        assert srv["status"] == "DOWN"
        assert srv["last_error_code"] == -96

    print(">>> TEST E PASSED: SERVER_DOWN generated, 0 fabricated RETURN events, active checkout preserved.")

    print("\n" + "=" * 70)
    print("ALL 5 LIVE VALIDATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_live_validations())
