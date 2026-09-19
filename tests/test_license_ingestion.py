"""
tests/test_license_ingestion.py
---------------------------------
Unit and integration tests for Stage 3: Backend License Ingestion API.
Validates:
- Authentication (Missing Authorization, Invalid Key, Correct Key)
- Schema Validation (Valid payloads, Malformed payloads, Missing fields, Invalid Enums)
- Semantic Validation (in_use > total_issued, negative counts, available arithmetic, cross-server mismatch)
- Unknown Features (MFAA accepted with catalog_status UNKNOWN)
- Server Error Snapshots (Server DOWN with empty packages, FlexNet -15, -96 diagnostics)
- Server Isolation (Server A and Server B independent identity)
- Backward Compatibility (Unrelated /reportJobStatus unchanged)
"""

import os
import sys
import json
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app_postgres_ready import app


TEST_INGESTION_KEY = "test-license-key-2026-secret"


async def asgi_request(
    app,
    method: str = "POST",
    path: str = "/internal/licenseStatus",
    headers: dict | None = None,
    json_body: dict | list | None = None,
):
    """
    Direct in-memory ASGI caller for FastAPI applications.
    Uses standard library asyncio without external dependencies like httpx.
    """
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


class TestLicenseIngestionAuth(unittest.TestCase):
    """Authentication tests for POST /internal/licenseStatus."""

    def test_01_missing_authorization_header_returns_401(self):
        """Missing Authorization header returns 401 Unauthorized."""
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers={},
                    json_body={},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Missing license ingestion bearer token", str(data))

    def test_02_invalid_key_returns_401(self):
        """Invalid Bearer token returns 401 Unauthorized."""
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers={"Authorization": "Bearer wrong-key"},
                    json_body={},
                )
            )
            self.assertEqual(status_code, 401)
            self.assertIn("Invalid license ingestion key", str(data))

    def test_03_server_key_unconfigured_returns_500(self):
        """If LICENSE_INGESTION_KEY is not set on server, fail securely with 500."""
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": ""}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers={"Authorization": "Bearer any-token"},
                    json_body={},
                )
            )
            self.assertEqual(status_code, 500)
            self.assertIn("not configured", str(data))


class TestLicenseIngestionSchema(unittest.TestCase):
    """Schema and contract tests for incoming monitor snapshots."""

    @classmethod
    def setUpClass(cls):
        cls.auth_headers = {"Authorization": f"Bearer {TEST_INGESTION_KEY}"}

    def _sample_server_a_payload(self):
        return {
            "schema_version": "1.0",
            "monitor_version": "1.0.0",
            "captured_at": "2026-09-18T07:40:24.193707+00:00",
            "server": {
                "hostname": "LAPTOP-CA2QN87F",
                "port": 27000,
                "status": "UP",
                "lmgrd_version": "v11.19.9",
                "adskflex_status": "UP",
                "adskflex_version": "v11.19.9",
                "error_code": None,
                "error_message": None,
            },
            "packages": [
                {
                    "feature_code": "77800MFS_T_F",
                    "total_issued": 1,
                    "in_use": 1,
                    "available": 0,
                    "utilization_pct": 100.0,
                    "product_family": "Synergy",
                    "product_name": "Autodesk Moldflow Synergy",
                    "catalog_status": "VERIFIED",
                    "components": ["88232MFS_2027_0F"],
                }
            ],
            "checkouts": [
                {
                    "checkout_id": "cd363ff49680767fd25572df8234fc5ce021b8f21d4560e3cbf1f3f4df09122f",
                    "server_hostname": "LAPTOP-CA2QN87F",
                    "username": "UnoTEAM-0144",
                    "machine_name": "LAPTOP-CA2QN87F",
                    "display": "LAPTOP-CA2QN87F",
                    "package_feature": "77800MFS_T_F",
                    "selected_component": "88232MFS_2027_0F",
                    "version": "v1.0",
                    "server_handle": "LAPTOP-CA2QN87F/27000 203",
                    "checkout_time": "Fri 9/18 13:09",
                    "checkout_time_precision": "MINUTE",
                    "pid": "14984",
                    "is_borrowed": False,
                    "is_incomplete": False,
                }
            ],
            "anomalies": [],
        }

    def test_04_valid_server_a_snapshot_accepted(self):
        """Valid Server A snapshot returns 200 OK with confirmation."""
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=self._sample_server_a_payload(),
                )
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(data["status"], "accepted")
            self.assertEqual(data["server_hostname"], "LAPTOP-CA2QN87F")
            self.assertEqual(data["captured_at"], "2026-09-18T07:40:24.193707+00:00")
            self.assertTrue(data["request_id"].startswith("req-"))

    def test_05_valid_server_b_snapshot_accepted(self):
        """Valid Server B snapshot returns 200 OK with server identity."""
        payload = {
            "schema_version": "1.0",
            "monitor_version": "1.0.0",
            "captured_at": "2026-09-18T07:45:00.000000+00:00",
            "server": {
                "hostname": "DESKTOP-23TMNR6",
                "port": 27000,
                "status": "UP",
                "lmgrd_version": "v11.19.9",
                "adskflex_status": "UP",
                "adskflex_version": "v11.19.9",
            },
            "packages": [
                {
                    "feature_code": "77400MFIA_T_F",
                    "total_issued": 12,
                    "in_use": 0,
                    "available": 12,
                    "utilization_pct": 0.0,
                    "product_family": "Insight",
                    "product_name": "Autodesk Moldflow Insight",
                    "catalog_status": "VERIFIED",
                }
            ],
            "checkouts": [],
            "anomalies": [],
        }
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(data["server_hostname"], "DESKTOP-23TMNR6")

    def test_06_malformed_payload_returns_422(self):
        """Malformed JSON or missing top-level structure returns 422."""
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body={"invalid_structure": 123},
                )
            )
            self.assertEqual(status_code, 422)

    def test_07_missing_server_block_returns_422(self):
        """Missing required server block returns 422."""
        payload = self._sample_server_a_payload()
        del payload["server"]
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 422)

    def test_08_invalid_server_status_returns_422(self):
        """Invalid server status enum returns 422."""
        payload = self._sample_server_a_payload()
        payload["server"]["status"] = "INVALID_STATUS"
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 422)


class TestLicenseIngestionSemanticValidation(unittest.TestCase):
    """Semantic domain rule validation tests."""

    @classmethod
    def setUpClass(cls):
        cls.auth_headers = {"Authorization": f"Bearer {TEST_INGESTION_KEY}"}

    def _base_payload(self):
        return {
            "schema_version": "1.0",
            "monitor_version": "1.0.0",
            "captured_at": "2026-09-18T07:40:24.193707+00:00",
            "server": {
                "hostname": "LAPTOP-CA2QN87F",
                "port": 27000,
                "status": "UP",
            },
            "packages": [],
            "checkouts": [],
            "anomalies": [],
        }

    def test_09_in_use_exceeds_total_issued_rejected(self):
        """Package validation: in_use > total_issued is rejected with 422."""
        payload = self._base_payload()
        payload["packages"].append({
            "feature_code": "77800MFS_T_F",
            "total_issued": 1,
            "in_use": 2,  # Invalid: 2 in use out of 1 issued
            "available": 0,
            "utilization_pct": 100.0,
        })
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 422)
            self.assertIn("exceeds total_issued", str(data))

    def test_10_negative_license_count_rejected(self):
        """Package validation: negative total_issued is rejected with 422."""
        payload = self._base_payload()
        payload["packages"].append({
            "feature_code": "77800MFS_T_F",
            "total_issued": -1,  # Invalid
            "in_use": 0,
            "available": -1,
            "utilization_pct": 0.0,
        })
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 422)

    def test_11_available_inconsistent_with_math_rejected(self):
        """Package validation: available != total_issued - in_use rejected with 422."""
        payload = self._base_payload()
        payload["packages"].append({
            "feature_code": "77800MFS_T_F",
            "total_issued": 4,
            "in_use": 1,
            "available": 5,  # Inconsistent: 4 - 1 != 5
            "utilization_pct": 25.0,
        })
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 422)
            self.assertIn("does not equal", str(data))

    def test_12_cross_server_checkout_mismatch_rejected_with_400(self):
        """
        Cross-server isolation: Checkout claiming Server B submitted under Server A
        must be rejected with HTTP 400.
        """
        payload = self._base_payload()
        payload["server"]["hostname"] = "LAPTOP-CA2QN87F"
        payload["checkouts"].append({
            "checkout_id": "abc123hash",
            "server_hostname": "DESKTOP-23TMNR6",  # Mismatch!
            "username": "UnoTEAM-0144",
            "machine_name": "LAPTOP-CA2QN87F",
            "package_feature": "77800MFS_T_F",
            "selected_component": "88232MFS_2027_0F",
            "checkout_time": "Fri 9/18 13:09",
            "pid": "14984",
        })
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 400)
            self.assertIn("does not match snapshot server", data["detail"])

    def test_13_unknown_mfaa_feature_accepted(self):
        """Unknown MFAA feature with catalog_status UNKNOWN is cleanly accepted."""
        payload = self._base_payload()
        payload["server"]["hostname"] = "DESKTOP-23TMNR6"
        payload["packages"].append({
            "feature_code": "76800MFAA_T_F",
            "total_issued": 4,
            "in_use": 0,
            "available": 4,
            "utilization_pct": 0.0,
            "product_family": "MFAA",
            "product_name": None,  # No invented product name
            "catalog_status": "UNKNOWN",
            "components": ["88225MFAA_2027_0F"],
        })
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(data["server_hostname"], "DESKTOP-23TMNR6")

    def test_14_server_down_with_empty_packages_accepted(self):
        """
        Server DOWN snapshot with empty packages and error_code -96
        must be accepted without error.
        """
        payload = {
            "schema_version": "1.0",
            "monitor_version": "1.0.0",
            "captured_at": "2026-09-18T07:47:19.320440+00:00",
            "server": {
                "hostname": "DESKTOP-23TMNR6",
                "port": 27000,
                "status": "DOWN",
                "error_code": -96,
                "error_message": "License server machine is down or not responding.",
            },
            "packages": [],
            "features": [],
            "checkouts": [],
            "anomalies": [],
        }
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(data["status"], "accepted")

    def test_15_server_down_flexnet_error_15_accepted(self):
        """Server DOWN with FlexNet error -15 (connection refused) is preserved and accepted."""
        payload = {
            "schema_version": "1.0",
            "monitor_version": "1.0.0",
            "captured_at": "2026-09-18T07:48:00.000000+00:00",
            "server": {
                "hostname": "LAPTOP-CA2QN87F",
                "port": 27001,
                "status": "DOWN",
                "error_code": -15,
                "error_message": "Cannot connect to license server system (-15,10:10061)",
            },
            "packages": [],
            "checkouts": [],
            "anomalies": [],
        }
        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            status_code, data = run_async(
                asgi_request(
                    app,
                    method="POST",
                    path="/internal/licenseStatus",
                    headers=self.auth_headers,
                    json_body=payload,
                )
            )
            self.assertEqual(status_code, 200)

    def test_16_server_a_and_b_submissions_isolated(self):
        """Server A and Server B submissions remain independently identified."""
        payload_a = self._base_payload()
        payload_a["server"]["hostname"] = "LAPTOP-CA2QN87F"

        payload_b = self._base_payload()
        payload_b["server"]["hostname"] = "DESKTOP-23TMNR6"

        with patch.dict(os.environ, {"LICENSE_INGESTION_KEY": TEST_INGESTION_KEY}):
            # Send Server A
            code_a, data_a = run_async(
                asgi_request(app, method="POST", path="/internal/licenseStatus", headers=self.auth_headers, json_body=payload_a)
            )
            # Send Server B
            code_b, data_b = run_async(
                asgi_request(app, method="POST", path="/internal/licenseStatus", headers=self.auth_headers, json_body=payload_b)
            )
            self.assertEqual(code_a, 200)
            self.assertEqual(code_b, 200)
            self.assertEqual(data_a["server_hostname"], "LAPTOP-CA2QN87F")
            self.assertEqual(data_b["server_hostname"], "DESKTOP-23TMNR6")
            self.assertNotEqual(data_a["request_id"], data_b["request_id"])


class TestBackwardCompatibility(unittest.TestCase):
    """Confirm existing /reportJobStatus behavior remains intact."""

    def test_17_report_job_status_rejects_missing_api_key(self):
        """Existing /reportJobStatus still requires X-Api-Key."""
        status_code, data = run_async(
            asgi_request(
                app,
                method="POST",
                path="/reportJobStatus",
                headers={},
                json_body={"job_id": "job-test-1"},
            )
        )
        self.assertEqual(status_code, 401)
        self.assertIn("Missing Moldflow API key", str(data))


if __name__ == "__main__":
    unittest.main()
