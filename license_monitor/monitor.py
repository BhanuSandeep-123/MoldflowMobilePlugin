"""
Network License Monitor (Stage 1)
Local read-only monitor for Autodesk FlexNet license servers.
Executes `lmutil lmstat`, parses output, and produces normalized snapshots.
"""

import os
import sys
import json
import time
import subprocess
import argparse
import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from .models import ExecutionResult, Snapshot, ServerStatus
from .parser import FlexNetParser


class LicenseMonitor:
    """
    Local read-only license monitor.
    Enforces Rule 1 (Read-only status queries only).
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_config(self.config_path)

        self.lmutil_path = self.config.get(
            "lmutil_path",
            r"C:\Autodesk\Network License Manager\lmutil.exe"
        )
        self.server_cfg = self.config.get("server", {})
        self.query_target = self.server_cfg.get("target", "27000@LAPTOP-CA2QN87F")
        self.timeout_seconds = self.config.get("query_timeout_seconds", 15)
        self.poll_interval = self.config.get("poll_interval_seconds", 60)

        self.parser = FlexNetParser(self.config)

    def _resolve_config_path(self, config_path: Optional[str]) -> Path:
        if config_path:
            return Path(config_path).resolve()
        # Default: config.json in the same directory as this file
        return Path(__file__).parent.resolve() / "config.json"

    def _load_config(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            # Return minimal default configuration if file does not exist
            return {
                "monitor_version": "1.0.0",
                "schema_version": "1.0",
                "server": {
                    "hostname": "LAPTOP-CA2QN87F",
                    "port": 27000,
                    "target": "27000@LAPTOP-CA2QN87F"
                },
                "lmutil_path": r"C:\Autodesk\Network License Manager\lmutil.exe",
                "query_timeout_seconds": 15,
                "poll_interval_seconds": 60,
                "known_packages": {}
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def execute_query(self, target_override: Optional[str] = None) -> ExecutionResult:
        """
        Executes read-only `lmutil lmstat -a -c <target>` using subprocess.
        Strictly enforces:
        - NEVER runs modifying commands (lmdown, lmremove, lmreread, etc.)
        - Subprocess timeout budget
        - Captures stdout, stderr, exit code, duration
        """
        target = target_override or self.query_target
        cmd = [self.lmutil_path, "lmstat", "-a", "-c", target]

        if not os.path.exists(self.lmutil_path):
            return ExecutionResult(
                command=cmd,
                exit_code=1,
                stdout="",
                stderr=f"lmutil binary not found at path: {self.lmutil_path}",
                duration_seconds=0.0,
                timed_out=False,
            )

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False
            )
            duration = time.perf_counter() - start_time
            return ExecutionResult(
                command=cmd,
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                duration_seconds=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start_time
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ExecutionResult(
                command=cmd,
                exit_code=1,
                stdout=stdout,
                stderr=stderr or f"Query timed out after {self.timeout_seconds} seconds",
                duration_seconds=duration,
                timed_out=True,
            )
        except Exception as exc:
            duration = time.perf_counter() - start_time
            return ExecutionResult(
                command=cmd,
                exit_code=1,
                stdout="",
                stderr=f"Exception executing lmutil: {str(exc)}",
                duration_seconds=duration,
                timed_out=False,
            )

    def capture_snapshot(self, target_override: Optional[str] = None) -> Snapshot:
        """
        Executes query, parses output, and returns normalized Snapshot.
        """
        captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        exec_result = self.execute_query(target_override)
        return self.parser.parse_execution_result(exec_result, captured_at=captured_at)

    def run_once(self, target_override: Optional[str] = None, indent: int = 2) -> str:
        """
        Runs a single snapshot capture and returns formatted JSON string.
        """
        snapshot = self.capture_snapshot(target_override)
        return json.dumps(snapshot.to_dict(), indent=indent)


def main():
    parser = argparse.ArgumentParser(
        description="Autodesk FlexNet Read-Only License Monitor (Stage 1)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.json"
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="License server target (e.g. 27000@LAPTOP-CA2QN87F)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run query once and output JSON to stdout"
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Run continuous polling loop"
    )
    args = parser.parse_args()

    monitor = LicenseMonitor(config_path=args.config)

    if args.poll:
        print(f"Starting license monitor polling loop for target {args.target or monitor.query_target}...")
        print(f"Poll interval: {monitor.poll_interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                snapshot = monitor.capture_snapshot(target_override=args.target)
                summary = (
                    f"[{snapshot.captured_at}] Status: {snapshot.server.status} | "
                    f"Packages: {len(snapshot.packages)} | "
                    f"Active Checkouts: {len(snapshot.checkouts)}"
                )
                print(summary)
                time.sleep(monitor.poll_interval)
        except KeyboardInterrupt:
            print("\nPolling stopped by user.")
    else:
        # Default is run once
        json_output = monitor.run_once(target_override=args.target)
        print(json_output)


if __name__ == "__main__":
    main()
