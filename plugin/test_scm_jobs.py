from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any


SCM_BASE_URL = "http://localhost:44100/ComputeQueue/v1"
JOBS_URL = f"{SCM_BASE_URL}/jobs"


def get_jobs(timeout: float = 5.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        JOBS_URL,
        method="GET",
        headers={
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not connect to SCM at {JOBS_URL}: {exc}"
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SCM returned invalid JSON: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            f"Expected SCM jobs response to be a list, got {type(data).__name__}"
        )

    return [
        item for item in data
        if isinstance(item, dict)
    ]


def print_job(job: dict[str, Any]) -> None:
    payload = job.get("payload") or {}
    progress = job.get("progress") or {}
    details = progress.get("details") or {}

    print("=" * 80)
    print(f"SCM Job ID       : {job.get('jobID', '')}")
    print(f"Name             : {payload.get('name', '')}")
    print(f"Type             : {payload.get('type', '')}")
    print(f"Status           : {job.get('status', '')}")
    print(f"Percent          : {progress.get('percent', '')}")
    print(f"Cloud            : {job.get('cloud', '')}")
    print(f"User             : {payload.get('user', '')}")
    print(f"Worker           : {job.get('worker', '')}")
    print(f"Worker Machine   : {job.get('workerMachine', '')}")
    print(f"Service          : {job.get('service', '')}")
    print(f"Epoch            : {job.get('epoch', '')}")

    child_details = details.get("childDetails")
    if isinstance(child_details, list):
        print(f"Child Jobs       : {len(child_details)}")

        for index, child in enumerate(child_details, start=1):
            if not isinstance(child, dict):
                continue

            print(f"  Child {index}")
            print(f"    Job ID        : {child.get('jobID', '')}")
            print(f"    Parent        : {child.get('parent', '')}")
            print(f"    Type          : {child.get('type', '')}")
            print(f"    Status        : {child.get('status', '')}")
            print(f"    Percent       : {child.get('percent', '')}")
            print(f"    Worker        : {child.get('workerMachine', '')}")
            print(f"    Error         : {child.get('errors', '')}")

            assets = child.get("assets")
            if isinstance(assets, list):
                print(f"    Assets        : {len(assets)}")

                for asset in assets:
                    if not isinstance(asset, dict):
                        continue

                    print(
                        "      - "
                        f"{asset.get('type', '')}: "
                        f"{asset.get('name', '')}"
                    )


def main() -> int:
    try:
        jobs = get_jobs()
    except Exception as exc:
        print(f"[SCM TEST] ERROR: {exc}")
        return 1

    print(f"[SCM TEST] Retrieved {len(jobs)} job records.")

    # Show the newest 10 records only.
    for job in jobs[-10:]:
        print_job(job)

    return 0


if __name__ == "__main__":
    sys.exit(main())