"""
Moldflow Mobile System - Workstation Enrollment Client
=====================================================

Provides zero-dependency client functions for provisioning and enrolling
workstations into the Moldflow Mobile backend during installation or setup.

Compatible with standard Python 3.10+ without external third-party dependencies
(uses urllib standard library), enabling execution in Autodesk Moldflow Python,
standalone workstation environments, and installer provisioning scripts.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("moldflow_mobile.enrollment")


class EnrollmentError(Exception):
    """Raised when workstation enrollment fails."""

    def __init__(self, message: str, status_code: int | None = None, response_data: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_data = response_data or {}


def enroll_workstation(
    backend_url: str,
    machine_id: str,
    user_id: str,
    enrollment_token: str,
    machine_name: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """
    Enrolls a workstation with the Moldflow Mobile backend.

    Sends a secure enrollment request to POST /api/workstation/enroll.
    The backend verifies the enrollment authority token, registers or updates
    the workstation record, generates a cryptographically random API key, and
    returns the provisioned workstation identity.

    Args:
        backend_url: Base URL of the backend (e.g., "http://localhost:8000" or Render URL).
        machine_id: Workstation machine identifier (e.g. computer name).
        user_id: Associated user ID in the backend system.
        enrollment_token: Master enrollment token (WORKSTATION_ENROLLMENT_KEY or LICENSE_INGESTION_KEY) or JWT.
        machine_name: Optional human-readable machine display name.
        timeout: Network request timeout in seconds.

    Returns:
        dict containing:
            - status: "success"
            - machine_id: str
            - user_id: str
            - api_key: str (provisioned high-entropy API key)
            - registered_at: str (ISO timestamp)
            - message: str

    Raises:
        ValueError: If required arguments are missing or empty.
        EnrollmentError: If network fails, authority token is invalid, or backend rejects enrollment.
    """
    cleaned_url = backend_url.strip().rstrip("/")
    if not cleaned_url:
        raise ValueError("backend_url cannot be empty")
    if not machine_id or not machine_id.strip():
        raise ValueError("machine_id cannot be empty")
    if not user_id or not user_id.strip():
        raise ValueError("user_id cannot be empty")
    if not enrollment_token or not enrollment_token.strip():
        raise ValueError("enrollment_token cannot be empty")

    endpoint = f"{cleaned_url}/api/workstation/enroll"
    payload = {
        "machine_id": machine_id.strip(),
        "user_id": user_id.strip(),
        "machine_name": machine_name.strip() if machine_name else "",
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=endpoint,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Enrollment-Token": enrollment_token.strip(),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            response_body = response.read().decode("utf-8")
            data = json.loads(response_body) if response_body else {}

            if status_code not in (200, 201):
                raise EnrollmentError(
                    f"Enrollment returned unexpected HTTP status {status_code}",
                    status_code=status_code,
                    response_data=data,
                )

            if not data.get("api_key"):
                raise EnrollmentError(
                    "Enrollment response did not contain an api_key",
                    status_code=status_code,
                    response_data=data,
                )

            if not data.get("backend_url"):
                data["backend_url"] = cleaned_url

            return data

    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        err_data = {}
        err_detail = err_body
        try:
            err_data = json.loads(err_body)
            err_detail = err_data.get("detail", err_body)
        except Exception:
            pass

        if exc.code == 401:
            msg = f"Enrollment authorization failed: {err_detail}"
        elif exc.code == 400:
            msg = f"Enrollment request rejected: {err_detail}"
        else:
            msg = f"Enrollment failed with HTTP {exc.code}: {err_detail}"

        raise EnrollmentError(msg, status_code=exc.code, response_data=err_data) from exc

    except urllib.error.URLError as exc:
        raise EnrollmentError(
            f"Failed to connect to backend at {cleaned_url}: {exc.reason}",
            status_code=None,
        ) from exc


def build_config_from_enrollment(
    enrollment_result: dict[str, Any],
    backend_url: str | None = None,
    poll_interval_seconds: int = 10,
) -> dict[str, Any]:
    """
    Constructs a standard workstation configuration dictionary from enrollment result.
    Matches the schema expected by lib/mobile/reporter.py and standalone_job_monitor.py.
    """
    resolved_backend = (backend_url or enrollment_result.get("backend_url") or "").strip().rstrip("/")
    cfg: dict[str, Any] = {
        "enabled": True,
        "backend_url": resolved_backend,
        "api_key": enrollment_result["api_key"],
        "machine_id": enrollment_result["machine_id"],
        "user_id": enrollment_result["user_id"],
        "poll_interval_seconds": poll_interval_seconds,
    }
    if enrollment_result.get("machine_name"):
        cfg["machine_name"] = enrollment_result["machine_name"]
    return cfg


def resolve_default_config_path() -> Path:
    """
    Resolves target configuration path:
      1. MOLDFLOW_CONFIG environment variable if present.
      2. %PROGRAMDATA%\\MoldflowMobile\\config.json (production workstation target).
      3. Fallback to local config.json.
    """
    env_override = os.environ.get("MOLDFLOW_CONFIG")
    if env_override and env_override.strip():
        return Path(env_override.strip())

    prog_data = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE")
    if prog_data:
        return Path(prog_data) / "MoldflowMobile" / "config.json"

    return Path("config.json")


def save_workstation_config(
    config_dict: dict[str, Any],
    target_path: str | Path | None = None,
) -> Path:
    """
    Saves workstation configuration to the target JSON path.
    Creates parent directories if necessary and writes formatted UTF-8 JSON.

    Args:
        config_dict: Dictionary containing configuration options.
        target_path: Destination path. If None, resolves via resolve_default_config_path().

    Returns:
        Path: The absolute path of the written configuration file.
    """
    destination = Path(target_path) if target_path is not None else resolve_default_config_path()
    destination = destination.resolve()

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically via temporary file to prevent partial reads by live monitors
    temp_target = destination.with_suffix(".tmp")
    with open(temp_target, "w", encoding="utf-8") as f:
        json.dump(config_dict, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Atomic rename/replace
    temp_target.replace(destination)
    logger.info(f"Saved workstation configuration to {destination}")
    return destination
