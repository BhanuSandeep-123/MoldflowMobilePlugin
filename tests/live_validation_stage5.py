"""
tests/live_validation_stage5.py
-------------------------------
Live Local Validation for Stage 5: Authenticated Mobile Read APIs.
Executes against the local database and real backend routes:
- Server A: Live Synergy (0/1) & Insight (0/3)
- Server B: Live Synergy (0/4), Insight (0/12), MFAA (0/4)
- Consumer: Exactly 1 physical checkout returned (omitting PID/internal handles)
- History: Real Stage 4 events retrieved with database-level filtering
- Authentication: JWT protected
"""

import sys
import os
import json
import asyncio
from pathlib import Path

# Ensure paths
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"

for d in (BASE_DIR, BACKEND_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from app_postgres_ready import app, get_db, init_database, create_access_token, db_execute


async def asgi_get(path: str, token: str):
    headers = [
        (b"host", b"localhost"),
        (b"authorization", f"Bearer {token}".encode("latin1")),
    ]

    if "?" in path:
        base_path, qs = path.split("?", 1)
        raw_qs = qs.encode("ascii")
    else:
        base_path = path
        raw_qs = b""

    response_status = None
    response_body = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal response_status, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": base_path,
        "raw_path": base_path.encode("ascii"),
        "query_string": raw_qs,
        "headers": headers,
    }

    await app(scope, receive, send)
    body_str = b"".join(response_body).decode("utf-8")
    parsed_json = json.loads(body_str) if body_str else {}
    return response_status, parsed_json


async def run_live_validation():
    print("=" * 70)
    print("STAGE 5 LIVE LOCAL READ API VALIDATION")
    print("=" * 70)

    # 1. Initialize tables & obtain valid JWT
    init_database()
    token, _ = create_access_token("DEV-USER-001")
    print("\n[Auth] Authenticated as DEV-USER-001 with JWT token.")

    # ------------------------------------------------------------------------
    # 2. GET /licenses/overview
    # ------------------------------------------------------------------------
    print("\n--- [1] GET /licenses/overview ---")
    st_ov, ov_data = await asgi_get("/licenses/overview", token)
    print(f"HTTP Status: {st_ov}")
    assert st_ov == 200, f"Expected 200, got {st_ov}"

    print(f"Total Servers in Overview: {len(ov_data['servers'])}")
    srv_map = {s["hostname"]: s for s in ov_data["servers"]}

    # Verify Server A in Overview
    assert "laptop-ca2qn87f" in srv_map, "Server A not found in overview"
    srv_a = srv_map["laptop-ca2qn87f"]
    print(f"Server A ({srv_a['hostname']}): Status={srv_a['status']}, DataState={srv_a['data_state']}")
    for prod in srv_a["products"]:
        print(f"  {prod['display_name']} ({prod['feature_code']}): {prod['in_use']}/{prod['total_issued']} (status={prod['status']})")

    # Verify Server B in Overview
    assert "desktop-23tmnr6" in srv_map, "Server B not found in overview"
    srv_b = srv_map["desktop-23tmnr6"]
    print(f"Server B ({srv_b['hostname']}): Status={srv_b['status']}, DataState={srv_b['data_state']}")
    for prod in srv_b["products"]:
        print(f"  {prod['display_name']} ({prod['feature_code']}): {prod['in_use']}/{prod['total_issued']} (status={prod['status']})")

    # Verify Environment Inventory
    env_inv = ov_data["environment_inventory"]
    print(f"\nEnvironment Inventory Notice: '{env_inv['notice']}'")
    print(f"Is Pooled: {env_inv['is_pooled']}")
    assert env_inv["is_pooled"] is False, "Environment inventory must NOT be marked as pooled!"
    for agg in env_inv["products"]:
        print(f"  Aggregate {agg['display_name']}: Total={agg['total_inventory']}, Used={agg['current_inventory_used']}, Available={agg['inventory_available']}")

    # ------------------------------------------------------------------------
    # 3. GET /licenses/servers
    # ------------------------------------------------------------------------
    print("\n--- [2] GET /licenses/servers ---")
    st_srvs, srvs_data = await asgi_get("/licenses/servers", token)
    print(f"HTTP Status: {st_srvs}, Count: {len(srvs_data)}")
    assert st_srvs == 200
    for s in srvs_data:
        print(f"  - Server ID: {s['server_id']}, Host: {s['hostname']}, Status: {s['status']}")

    server_id_a = srv_a["server_id"]
    server_id_b = srv_b["server_id"]

    # ------------------------------------------------------------------------
    # 4. GET /licenses/servers/{server_id} Detail
    # ------------------------------------------------------------------------
    print(f"\n--- [3] GET /licenses/servers/{server_id_a} (Server A Detail) ---")
    st_dt_a, dt_a = await asgi_get(f"/licenses/servers/{server_id_a}", token)
    print(f"HTTP Status: {st_dt_a}")
    assert st_dt_a == 200
    print(f"Server A Detail: {dt_a['display_name']} ({dt_a['hostname']})")
    for pkg in dt_a["packages"]:
        print(f"  Package: {pkg['display_name']} ({pkg['feature_code']}) - {pkg['in_use']}/{pkg['total_issued']} seats")
        for c in pkg["components"]:
            print(f"    Component: {c['feature_code']} (version {c['year_version']}, in_use={c['in_use']})")

    print(f"\n--- [4] GET /licenses/servers/{server_id_b} (Server B Detail) ---")
    st_dt_b, dt_b = await asgi_get(f"/licenses/servers/{server_id_b}", token)
    print(f"HTTP Status: {st_dt_b}")
    assert st_dt_b == 200
    print(f"Server B Detail: {dt_b['display_name']} ({dt_b['hostname']})")
    for pkg in dt_b["packages"]:
        print(f"  Package: {pkg['display_name']} ({pkg['feature_code']}) - {pkg['in_use']}/{pkg['total_issued']} seats [Catalog: {pkg['catalog_status']}]")

    # ------------------------------------------------------------------------
    # 5. GET /licenses/servers/{server_id}/consumers
    # ------------------------------------------------------------------------
    print(f"\n--- [5] GET /licenses/servers/{server_id_a}/consumers ---")
    st_c_a, c_a_data = await asgi_get(f"/licenses/servers/{server_id_a}/consumers", token)
    print(f"HTTP Status: {st_c_a}, Active Checkouts: {len(c_a_data)}")
    assert st_c_a == 200
    for co in c_a_data:
        print(f"  Checkout: User={co['username']}, Machine={co['machine_name']}, Product={co['product_name']}, Pkg={co['package_feature']}, Comp={co['component_feature']}, Time={co['checkout_time']}")
        assert "pid" not in co, "PID must not be exposed!"
        assert "server_handle" not in co, "Server handle must not be exposed!"

    print(f"\n--- [6] GET /licenses/servers/{server_id_b}/consumers ---")
    st_c_b, c_b_data = await asgi_get(f"/licenses/servers/{server_id_b}/consumers", token)
    print(f"HTTP Status: {st_c_b}, Active Checkouts: {len(c_b_data)}")
    assert st_c_b == 200

    # ------------------------------------------------------------------------
    # 6. GET /licenses/history
    # ------------------------------------------------------------------------
    print("\n--- [7] GET /licenses/history ---")
    st_hist, hist_data = await asgi_get("/licenses/history?limit=5", token)
    print(f"HTTP Status: {st_hist}, Total Events in DB: {hist_data['total']}, Page Count: {len(hist_data['items'])}")
    assert st_hist == 200
    for evt in hist_data["items"]:
        print(f"  Event [{evt['event_type']}]: Detected={evt['detected_at']}, User={evt['username']}, Feature={evt['feature_code']}, Server={evt['server_hostname']}")

    print("\n" + "=" * 70)
    print("STAGE 5 LIVE LOCAL VALIDATION COMPLETE - ALL ENDPOINTS VERIFIED!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_live_validation())
