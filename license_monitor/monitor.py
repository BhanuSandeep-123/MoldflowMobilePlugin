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
        servers = self.get_configured_servers()
        self.query_target = self.server_cfg.get("target") or (servers[0]["target"] if servers else None)
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
                "servers": [],
                "lmutil_path": r"C:\Autodesk\Network License Manager\lmutil.exe",
                "query_timeout_seconds": 15,
                "poll_interval_seconds": 60,
                "known_packages": {}
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def reload_config(self) -> None:
        """Reloads configuration from disk to dynamically capture server additions/removals."""
        if self.config_path.exists():
            try:
                self.config = self._load_config(self.config_path)
                self.poll_interval = self.config.get("poll_interval_seconds", self.poll_interval)
                self.timeout_seconds = self.config.get("query_timeout_seconds", self.timeout_seconds)
                self.parser.config = self.config
            except Exception:
                pass

    def get_configured_servers(self) -> list[dict[str, Any]]:
        """
        Returns list of server configuration dicts.
        Supports dynamic 'servers' list or single 'server' dict for backwards compatibility.
        """
        if "servers" in self.config and isinstance(self.config["servers"], list):
            return [s for s in self.config["servers"] if isinstance(s, dict) and s.get("target")]
        if "server" in self.config and isinstance(self.config["server"], dict) and self.config["server"].get("target"):
            return [self.config["server"]]
        return []

    def execute_query(self, target_override: Optional[str] = None) -> ExecutionResult:
        """
        Executes read-only `lmutil lmstat -a -c <target>` using subprocess.
        Strictly enforces:
        - NEVER runs modifying commands (lmdown, lmremove, lmreread, etc.)
        - Subprocess timeout budget
        - Captures stdout, stderr, exit code, duration
        - Silent windowless execution on Windows (no console flashing)
        """
        target = target_override or self.query_target
        if not target:
            servers = self.get_configured_servers()
            if servers:
                target = servers[0].get("target")

        if not target:
            return ExecutionResult(
                command=[],
                exit_code=1,
                stdout="",
                stderr="No query target configured or specified",
                duration_seconds=0.0,
                timed_out=False,
            )

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

        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                creationflags=creationflags,
                startupinfo=startupinfo,
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

    def push_snapshot(
        self,
        snapshot: Snapshot,
        backend_url: Optional[str] = None,
        ingestion_key: Optional[str] = None,
    ) -> bool:
        """
        Sends normalized snapshot payload to Stage 3 Backend Ingestion (/internal/licenseStatus).
        Uses standard urllib to maintain zero external dependencies.
        """
        import urllib.request
        import urllib.error

        base_url = (backend_url or os.getenv("BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
        endpoint = f"{base_url}/internal/licenseStatus"
        key = ingestion_key or os.getenv("LICENSE_INGESTION_KEY") or "dev-license-ingestion-key-2026"

        payload_bytes = json.dumps(snapshot.to_dict()).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:
                pass
            print(f"[LicenseMonitor] Ingestion warning ({endpoint}): HTTP {exc.code} {exc.reason} - {err_body}", flush=True)
            return False
        except Exception as exc:
            print(f"[LicenseMonitor] Ingestion warning ({endpoint}): {exc}", flush=True)
            return False

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
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push snapshot to backend ingestion API (/internal/licenseStatus)"
    )
    parser.add_argument(
        "--backend-url",
        type=str,
        default=None,
        help="Backend base URL for ingestion (default: BACKEND_URL env or http://127.0.0.1:8000)"
    )
    parser.add_argument(
        "--ingestion-key",
        type=str,
        default=None,
        help="License ingestion bearer key (default: LICENSE_INGESTION_KEY env)"
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Path to persistent log file (appends output with timestamps)"
    )
    args = parser.parse_args()

    monitor = LicenseMonitor(config_path=args.config)

    log_path = None
    if args.log_file:
        log_path = Path(args.log_file).resolve()
    elif monitor.config.get("log_file"):
        log_path = Path(monitor.config.get("log_file")).resolve()

    def log_output(msg: str):
        print(msg, flush=True)
        if log_path:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {msg}\n")
            except Exception:
                pass

    if args.poll:
        init_targets = [args.target] if args.target else [s.get("target") for s in monitor.get_configured_servers()]
        log_output(f"Starting license monitor polling loop for {len(init_targets)} target(s): {', '.join(filter(None, init_targets)) or monitor.query_target}...")
        log_output(f"Poll interval: {monitor.poll_interval}s. Log file: {log_path or 'stdout only'}. Press Ctrl+C to stop.")
        try:
            while True:
                monitor.reload_config()
                current_servers = [args.target] if args.target else [s.get("target") for s in monitor.get_configured_servers()]
                if not current_servers and monitor.query_target:
                    current_servers = [monitor.query_target]

                for s_target in current_servers:
                    if not s_target:
                        continue
                    snapshot = monitor.capture_snapshot(target_override=s_target)
                    summary = (
                        f"[{snapshot.captured_at}] Server: {snapshot.server.hostname} | "
                        f"Status: {snapshot.server.status.value if hasattr(snapshot.server.status, 'value') else snapshot.server.status} | "
                        f"Packages: {len(snapshot.packages)} | "
                        f"Active Checkouts: {len(snapshot.checkouts)}"
                    )
                    log_output(summary)
                    if args.push:
                        pushed = monitor.push_snapshot(
                            snapshot,
                            backend_url=args.backend_url,
                            ingestion_key=args.ingestion_key,
                        )
                        log_output(f"  -> Ingestion push ({snapshot.server.hostname}): {'OK' if pushed else 'FAILED'}")
                time.sleep(monitor.poll_interval)
        except KeyboardInterrupt:
            log_output("\nPolling stopped by user.")
    else:
        # Default is run once across all configured servers
        current_servers = [args.target] if args.target else [s.get("target") for s in monitor.get_configured_servers()]
        if not current_servers and monitor.query_target:
            current_servers = [monitor.query_target]

        for s_target in current_servers:
            if not s_target:
                continue
            snapshot = monitor.capture_snapshot(target_override=s_target)
            if args.push:
                pushed = monitor.push_snapshot(
                    snapshot,
                    backend_url=args.backend_url,
                    ingestion_key=args.ingestion_key,
                )
                log_output(f"Ingestion push ({snapshot.server.hostname}): {'OK' if pushed else 'FAILED'}")
            json_output = json.dumps(snapshot.to_dict(), indent=2)
            log_output(json_output)


if __name__ == "__main__":
    main()
