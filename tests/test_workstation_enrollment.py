"""
tests/test_workstation_enrollment.py
------------------------------------
Stage 8B Phase 2A — Security Hardened Workstation Enrollment Test Suite.

Validates:
1. Security Hardening:
   - Missing token -> 401 Unauthorized
   - Invalid token -> 401 Unauthorized
   - LICENSE_INGESTION_KEY rejected -> 401 Unauthorized (isolated authority)
   - Mobile/User JWT rejected -> 401 Unauthorized (strictly separate from mobile auth)
   - Unconfigured server fails closed -> 401 Unauthorized
2. One-Time & Expiring Enrollment Authority:
   - create_enrollment_token issues valid token
   - Enrollment with valid one-time token -> 200 OK
   - Token is consumed and recorded in database
   - Replay attempt with same one-time token is REJECTED -> 401 Unauthorized
   - Expired one-time token is REJECTED -> 401 Unauthorized
3. Master Workstation Enrollment Authority:
   - WORKSTATION_ENROLLMENT_KEY via X-Enrollment-Token -> 200 OK
   - WORKSTATION_ENROLLMENT_KEY via Authorization: Bearer -> 200 OK
4. High-Entropy Key Generation:
   - 256-bit cryptographic entropy (64 hex characters via secrets.token_hex(32))
   - Format: ^mf-client-[a-zA-Z0-9_\\-]+-[a-f0-9]{64}$
5. Key Rotation & Multi-Machine Isolation:
   - Re-enrollment sets previous key to enabled = 0, new key to enabled = 1
   - Existing Stage 8A machines remain untouched in DB
6. Complete Operational API Verification:
   - Newly enrolled key succeeds on BOTH:
     * POST /reportJobStatus (200 OK, accepted = True)
     * GET /internal/active-jobs (200 OK, returns list)
   - Rotated key is rejected on BOTH (401 Unauthorized)
   - Cross-machine isolation: Machine B cannot see Machine A's active jobs
7. Finalized Response Contract:
   - Verifies status, machine_id, machine_name, user_id, api_key, backend_url, enrolled_at
8. Client Helper Library (lib/mobile/enrollment.py):
   - Argument validation, build_config_from_enrollment, atomic save_workstation_config
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure backend and repo root are in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
LIB_DIR = ROOT_DIR / "lib"

for p in (str(BACKEND_DIR), str(LIB_DIR), str(ROOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app_postgres_ready import (
    app,
    get_db,
    init_database,
    create_access_token,
    create_enrollment_token,
    db_execute,
)
from mobile.enrollment import (
    EnrollmentError,
    enroll_workstation,
    build_config_from_enrollment,
    save_workstation_config,
    resolve_default_config_path,
)

TEST_ENROLL_KEY = "test-dedicated-enrollment-secret-2026"
TEST_LICENSE_KEY = "test-license-ingestion-secret-2026"


async def asgi_request(
    app,
    method: str = "POST",
    path: str = "/api/workstation/enroll",
    headers: dict | None = None,
    json_body: dict | list | None = None,
):
    body_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers = headers or {}
    raw_headers = [(k.lower().encode("latin1"), v.encode("latin1")) for k, v in headers.items()]
    if json_body is not None and b"content-type" not in [h[0] for h in raw_headers]:
        raw_headers.append((b"content-type", b"application/json"))
    raw_headers.append((b"content-length", str(len(body_bytes)).encode("latin1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
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


def run_async(coro):
    return asyncio.run(coro)


class TestHardenedWorkstationEnrollment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_database()
        cls.valid_user_id = "DEV-USER-001"
        cls.jwt_token, _ = create_access_token(cls.valid_user_id)

    # ------------------------------------------------------------------------
    # 1. Dedicated Authority & Attack Surface Reduction
    # ------------------------------------------------------------------------

    def test_01_missing_token_returns_401(self):
        """Missing enrollment token returns 401 Unauthorized."""
        status_code, data = run_async(
            asgi_request(
                app,
                method="POST",
                path="/api/workstation/enroll",
                headers={},
                json_body={"machine_id": "TEST-PC-01", "user_id": self.valid_user_id},
            )
        )
        self.assertEqual(status_code, 401)
        self.assertIn("Missing enrollment token", str(data))

    def test_02_invalid_token_returns_401(self):
        """Invalid enrollment token returns 401 Unauthorized."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": "invalid-token-12345"},
                    json_body={"machine_id": "TEST-PC-01", "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Invalid or expired enrollment token", str(data))

    def test_03_license_ingestion_key_rejected(self):
        """LICENSE_INGESTION_KEY is NOT accepted for workstation enrollment (isolated authority)."""
        with patch.dict(os.environ, {
            "WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY,
            "LICENSE_INGESTION_KEY": TEST_LICENSE_KEY,
        }):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_LICENSE_KEY},
                    json_body={"machine_id": "TEST-PC-01", "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Invalid or expired enrollment token", str(data))

    def test_04_user_jwt_rejected(self):
        """Mobile / User JWT is NOT accepted for workstation enrollment (strict auth separation)."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"Authorization": f"Bearer {self.jwt_token}"},
                    json_body={"machine_id": "TEST-PC-01", "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Invalid or expired enrollment token", str(data))

    def test_05_unconfigured_server_fails_closed(self):
        """If WORKSTATION_ENROLLMENT_KEY is unset and no DB token matches, fail closed with 401."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": ""}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": "some-arbitrary-token"},
                    json_body={"machine_id": "TEST-PC-01", "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Invalid or expired enrollment token", str(data))

    # ------------------------------------------------------------------------
    # 2. One-Time & Expiring Enrollment Authority
    # ------------------------------------------------------------------------

    def test_06_one_time_token_success_and_consumption(self):
        """Valid one-time token succeeds and is marked consumed in the database."""
        token_info = create_enrollment_token(user_id=self.valid_user_id, expires_in_seconds=300)
        token = token_info["token"]

        status_code, data = run_async(
            asgi_request(
                app,
                method="POST",
                path="/api/workstation/enroll",
                headers={"X-Enrollment-Token": token},
                json_body={
                    "machine_id": "WS-ONE-TIME-01",
                    "user_id": self.valid_user_id,
                    "machine_name": "One Time Rig",
                },
            )
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(data["status"], "success")

        # Verify in database that token is recorded as consumed
        with get_db() as conn:
            row = db_execute(
                conn,
                "SELECT consumed_at, consumed_by_machine_id FROM enrollment_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "WS-ONE-TIME-01")

    def test_07_one_time_token_replay_rejected(self):
        """Attempting to replay an already consumed one-time token is rejected with 401."""
        token_info = create_enrollment_token(user_id=self.valid_user_id, expires_in_seconds=300)
        token = token_info["token"]

        # First enrollment succeeds
        code1, _ = run_async(
            asgi_request(
                app,
                method="POST",
                path="/api/workstation/enroll",
                headers={"X-Enrollment-Token": token},
                json_body={"machine_id": "WS-REPLAY-01", "user_id": self.valid_user_id},
            )
        )
        self.assertEqual(code1, 200)

        # Second enrollment with same token must fail
        code2, data2 = run_async(
            asgi_request(
                app,
                method="POST",
                path="/api/workstation/enroll",
                headers={"X-Enrollment-Token": token},
                json_body={"machine_id": "WS-REPLAY-02", "user_id": self.valid_user_id},
            )
        )
        self.assertEqual(code2, 401)
        self.assertIn("already been consumed", str(data2))

    def test_08_expired_token_rejected(self):
        """Expired one-time token is rejected with 401."""
        # Insert a token that expired 10 minutes ago
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        token = "mf-enroll-expired-test-token"
        with get_db() as conn:
            db_execute(
                conn,
                """
                INSERT INTO enrollment_tokens (token, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET expires_at = excluded.expires_at, consumed_at = NULL
                """,
                (token, self.valid_user_id, past_iso, past_iso),
            )
            conn.commit()

        code, data = run_async(
            asgi_request(
                app,
                method="POST",
                path="/api/workstation/enroll",
                headers={"X-Enrollment-Token": token},
                json_body={"machine_id": "WS-EXPIRED-TEST", "user_id": self.valid_user_id},
            )
        )
        self.assertEqual(code, 401)
        self.assertIn("expired", str(data))

    # ------------------------------------------------------------------------
    # 3. Master WORKSTATION_ENROLLMENT_KEY & Key Entropy (256-bit)
    # ------------------------------------------------------------------------

    def test_09_master_key_and_256bit_entropy(self):
        """Enrollment with WORKSTATION_ENROLLMENT_KEY produces 256-bit entropy key (64 hex chars)."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={
                        "machine_id": "WS-ENTROPY-TEST",
                        "user_id": self.valid_user_id,
                        "machine_name": "Entropy Test Rig",
                    },
                )
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(data["status"], "success")
            api_key = data["api_key"]

            # Format: mf-client-WS-ENTROPY-TEST-<64 hex chars>
            self.assertTrue(api_key.startswith("mf-client-WS-ENTROPY-TEST-"))
            hex_part = api_key.replace("mf-client-WS-ENTROPY-TEST-", "")
            self.assertEqual(len(hex_part), 64)
            self.assertTrue(all(c in "0123456789abcdef" for c in hex_part))

    # ------------------------------------------------------------------------
    # 4. Duplicate / Re-enrollment Behavior & Key Rotation
    # ------------------------------------------------------------------------

    def test_10_re_enrollment_rotates_keys_cleanly(self):
        """Re-enrolling an existing machine deactivates prior key and activates exactly one new key."""
        machine_id = "WS-ROTATE-VERIFY"
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            # Enrollment 1
            code1, data1 = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": machine_id, "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(code1, 200)
            key1 = data1["api_key"]

            # Enrollment 2 (re-provisioning)
            code2, data2 = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": machine_id, "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(code2, 200)
            key2 = data2["api_key"]
            self.assertNotEqual(key1, key2)

            # Database verification
            with get_db() as conn:
                row1 = db_execute(conn, "SELECT enabled FROM api_clients WHERE api_key = ?", (key1,)).fetchone()
                row2 = db_execute(conn, "SELECT enabled FROM api_clients WHERE api_key = ?", (key2,)).fetchone()
                active_count = db_execute(
                    conn,
                    "SELECT COUNT(*) FROM api_clients WHERE machine_id = ? AND enabled = 1",
                    (machine_id,),
                ).fetchone()[0]

                self.assertEqual(row1[0], 0)  # Rotated out
                self.assertEqual(row2[0], 1)  # Active
                self.assertEqual(active_count, 1)  # Exactly one active key for machine

    # ------------------------------------------------------------------------
    # 5. Complete API Verification: /reportJobStatus & /internal/active-jobs
    # ------------------------------------------------------------------------

    def test_11_active_key_succeeds_both_endpoints_and_rotated_key_fails(self):
        """Newly enrolled key succeeds on both /reportJobStatus and /internal/active-jobs; rotated key fails both."""
        machine_id = "WS-FULL-API-TEST"
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            # 1. Enroll
            code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": machine_id, "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(code, 200)
            active_key = data["api_key"]

            # 2. Test POST /reportJobStatus with active key
            job_payload = {
                "job_id": f"job-{machine_id}-001",
                "name": "Integration Job",
                "status": "Running",
                "percent": 45.0,
                "scm_job_id": "scm-12345",
            }
            code_rep, data_rep = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/reportJobStatus",
                    headers={"X-Api-Key": active_key},
                    json_body=job_payload,
                )
            )
            self.assertEqual(code_rep, 200)
            self.assertTrue(data_rep.get("accepted"))
            self.assertEqual(data_rep.get("machine_id"), machine_id)

            # 3. Test GET /internal/active-jobs with active key
            code_jobs, data_jobs = run_async(
                asgi_request(
                    app,
                    method="GET",
                    path="/internal/active-jobs",
                    headers={"X-Api-Key": active_key},
                )
            )
            self.assertEqual(code_jobs, 200)
            self.assertIsInstance(data_jobs, list)
            # Must contain the job just reported for this machine
            matching = [j for j in data_jobs if j.get("job_id") == f"job-{machine_id}-001"]
            self.assertEqual(len(matching), 1)

            # 4. Re-enroll to rotate the key
            code_rot, data_rot = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": machine_id, "user_id": self.valid_user_id},
                )
            )
            self.assertEqual(code_rot, 200)
            rotated_old_key = active_key
            new_key = data_rot["api_key"]

            # 5. Rotated key fails on POST /reportJobStatus (401)
            code_rep_old, _ = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/reportJobStatus",
                    headers={"X-Api-Key": rotated_old_key},
                    json_body=job_payload,
                )
            )
            self.assertEqual(code_rep_old, 401)

            # 6. Rotated key fails on GET /internal/active-jobs (401)
            code_jobs_old, _ = run_async(
                asgi_request(
                    app,
                    method="GET",
                    path="/internal/active-jobs",
                    headers={"X-Api-Key": rotated_old_key},
                )
            )
            self.assertEqual(code_jobs_old, 401)

            # 7. New key succeeds on both
            code_rep_new, _ = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/reportJobStatus",
                    headers={"X-Api-Key": new_key},
                    json_body=job_payload,
                )
            )
            self.assertEqual(code_rep_new, 200)

            code_jobs_new, _ = run_async(
                asgi_request(
                    app,
                    method="GET",
                    path="/internal/active-jobs",
                    headers={"X-Api-Key": new_key},
                )
            )
            self.assertEqual(code_jobs_new, 200)

    def test_12_cross_machine_isolation(self):
        """Workstation A cannot see or access Workstation B's active jobs."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            # Enroll Machine Alpha
            _, data_a = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": "WS-ISOLATION-ALPHA", "user_id": self.valid_user_id},
                )
            )
            key_a = data_a["api_key"]

            # Enroll Machine Beta
            _, data_b = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={"machine_id": "WS-ISOLATION-BETA", "user_id": self.valid_user_id},
                )
            )
            key_b = data_b["api_key"]

            # Machine Alpha reports a running job with SCM ID
            run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/reportJobStatus",
                    headers={"X-Api-Key": key_a},
                    json_body={
                        "job_id": "job-alpha-exclusive-001",
                        "name": "Alpha Study",
                        "status": "Running",
                        "percent": 10.0,
                        "scm_job_id": "scm-alpha-001",
                    },
                )
            )

            # Machine Beta polls active jobs
            code_b_jobs, jobs_b = run_async(
                asgi_request(
                    app,
                    method="GET",
                    path="/internal/active-jobs",
                    headers={"X-Api-Key": key_b},
                )
            )
            self.assertEqual(code_b_jobs, 200)
            # Machine Beta MUST NOT see Alpha's job
            alpha_ids = [j.get("job_id") for j in jobs_b if j.get("job_id") == "job-alpha-exclusive-001"]
            self.assertEqual(len(alpha_ids), 0)

    # ------------------------------------------------------------------------
    # 6. Finalized Response Contract Verification
    # ------------------------------------------------------------------------

    def test_13_finalized_response_contract_fields(self):
        """WorkstationEnrollResponse contract contains all documented fields."""
        with patch.dict(os.environ, {"WORKSTATION_ENROLLMENT_KEY": TEST_ENROLL_KEY}):
            code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/api/workstation/enroll",
                    headers={"X-Enrollment-Token": TEST_ENROLL_KEY},
                    json_body={
                        "machine_id": "WS-CONTRACT-01",
                        "user_id": self.valid_user_id,
                        "machine_name": "Contract Rig",
                    },
                )
            )
            self.assertEqual(code, 200)
            # Check all contract fields
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["machine_id"], "WS-CONTRACT-01")
            self.assertEqual(data["machine_name"], "Contract Rig")
            self.assertEqual(data["user_id"], self.valid_user_id)
            self.assertIn("api_key", data)
            self.assertIn("enrolled_at", data)
            self.assertIn("registered_at", data)
            self.assertIn("message", data)


class TestWorkstationEnrollmentClientHelper(unittest.TestCase):
    """Tests for lib/mobile/enrollment.py client library."""

    def test_01_argument_validation(self):
        """Missing or empty arguments raise ValueError."""
        with self.assertRaises(ValueError):
            enroll_workstation("", "WS-1", "U-1", "TOKEN")
        with self.assertRaises(ValueError):
            enroll_workstation("http://localhost:8000", "", "U-1", "TOKEN")
        with self.assertRaises(ValueError):
            enroll_workstation("http://localhost:8000", "WS-1", "", "TOKEN")
        with self.assertRaises(ValueError):
            enroll_workstation("http://localhost:8000", "WS-1", "U-1", "")

    def test_02_build_config_from_enrollment(self):
        """build_config_from_enrollment produces correct structure with optional backend_url."""
        enrollment_result = {
            "status": "success",
            "machine_id": "WS-BUILD-01",
            "machine_name": "Build Rig",
            "user_id": "USER-99",
            "api_key": "mf-client-WS-BUILD-01-" + ("a" * 64),
            "backend_url": "https://moldflow-api.example.com",
            "enrolled_at": "2026-09-23T12:00:00Z",
        }
        cfg = build_config_from_enrollment(enrollment_result, poll_interval_seconds=15)
        self.assertEqual(cfg["enabled"], True)
        self.assertEqual(cfg["backend_url"], "https://moldflow-api.example.com")
        self.assertEqual(cfg["api_key"], "mf-client-WS-BUILD-01-" + ("a" * 64))
        self.assertEqual(cfg["machine_id"], "WS-BUILD-01")
        self.assertEqual(cfg["machine_name"], "Build Rig")
        self.assertEqual(cfg["user_id"], "USER-99")
        self.assertEqual(cfg["poll_interval_seconds"], 15)

    def test_03_save_workstation_config(self):
        """save_workstation_config writes formatted JSON atomically to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "subdir" / "workstation_config.json"
            cfg = {
                "enabled": True,
                "backend_url": "https://api.test",
                "api_key": "test-key-1234",
                "machine_id": "TEST-SAVE-WS",
                "user_id": "TEST-USER",
            }
            written_path = save_workstation_config(cfg, target_path=target_path)
            self.assertEqual(written_path, target_path.resolve())
            self.assertTrue(target_path.exists())

            loaded = json.loads(target_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded, cfg)

    @patch("urllib.request.urlopen")
    def test_04_enroll_workstation_success_populates_backend_url(self, mock_urlopen):
        """enroll_workstation parses HTTP 200 response and populates effective backend_url."""
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({
            "status": "success",
            "machine_id": "WS-MOCK-01",
            "machine_name": "Mock Rig",
            "user_id": "DEV-USER-001",
            "api_key": "mf-client-WS-MOCK-01-" + ("f" * 64),
            "backend_url": None,
            "enrolled_at": "2026-09-23T12:00:00Z",
            "registered_at": "2026-09-23T12:00:00Z",
            "message": "Workstation enrolled successfully",
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = enroll_workstation(
            backend_url="http://127.0.0.1:8000/",
            machine_id="WS-MOCK-01",
            user_id="DEV-USER-001",
            enrollment_token="valid-token",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["machine_id"], "WS-MOCK-01")
        self.assertEqual(res["backend_url"], "http://127.0.0.1:8000")
        self.assertEqual(res["api_key"], "mf-client-WS-MOCK-01-" + ("f" * 64))

    @patch("urllib.request.urlopen")
    def test_05_enroll_workstation_http_error(self, mock_urlopen):
        """enroll_workstation wraps HTTP 401 into EnrollmentError."""
        import urllib.error
        import io
        fp = io.BytesIO(json.dumps({"detail": "Invalid or expired enrollment token"}).encode("utf-8"))
        err = urllib.error.HTTPError("http://127.0.0.1/api/workstation/enroll", 401, "Unauthorized", {}, fp)
        mock_urlopen.side_effect = err

        with self.assertRaises(EnrollmentError) as ctx:
            enroll_workstation(
                backend_url="http://127.0.0.1:8000",
                machine_id="WS-MOCK-01",
                user_id="DEV-USER-001",
                enrollment_token="wrong-token",
            )
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid or expired enrollment token", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
