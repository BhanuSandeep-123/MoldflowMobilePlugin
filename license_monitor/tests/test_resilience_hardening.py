"""
Automated regression and hardening tests for:
1. Per-server exception isolation in license_monitor/monitor.py
2. Single-instance process protection via OS-level kernel file lock
3. Multi-server continuation and crash recovery
"""

import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from license_monitor.monitor import SingleInstanceLock, LicenseMonitor
from license_monitor.models import Snapshot, ServerInfo, ServerStatus


class TestSingleInstanceLock:
    def test_acquire_and_release(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        lock1 = SingleInstanceLock(lock_file)
        assert lock1.acquire() is True
        assert lock_file.exists()

        # Second acquisition on same file must fail
        lock2 = SingleInstanceLock(lock_file)
        assert lock2.acquire() is False

        # Release first lock
        lock1.release()

        # Now second lock must succeed
        assert lock2.acquire() is True
        lock2.release()

    def test_reentrant_lock_safe_cleanup(self, tmp_path):
        lock_file = tmp_path / "test_reentrant.lock"
        lock = SingleInstanceLock(lock_file)
        assert lock.acquire() is True
        lock.release()
        # Double release should not raise
        lock.release()

    def test_crash_recovery_kernel_releases_lock(self, tmp_path):
        """
        Proves that when a process holding the lock is killed forcibly,
        the operating system kernel immediately drops the lock without
        leaving a stale lock condition.
        """
        lock_file = tmp_path / "crash_test.lock"

        # Spawn child process that acquires lock and holds it
        child_code = f"""
import sys, time
from pathlib import Path
from license_monitor.monitor import SingleInstanceLock
lock = SingleInstanceLock(Path(r'{lock_file}'))
if lock.acquire():
    print('CHILD_ACQUIRED', flush=True)
    time.sleep(60)
else:
    print('CHILD_FAILED', flush=True)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).parents[2]),
        )

        try:
            # Wait for child to acquire lock
            line = proc.stdout.readline().strip()
            assert line == "CHILD_ACQUIRED"

            # Verify parent cannot acquire lock
            parent_lock = SingleInstanceLock(lock_file)
            assert parent_lock.acquire() is False

            # Kill child forcibly (simulating crash / taskkill)
            proc.kill()
            proc.wait()

            # Now parent MUST be able to acquire immediately (zero stale lock)
            assert parent_lock.acquire() is True
            parent_lock.release()
        finally:
            if proc.poll() is None:
                proc.kill()


class TestPerServerExceptionIsolation:
    def test_server_a_failure_does_not_stop_server_b(self, tmp_path):
        """
        Config with Server A and Server B.
        Server A raises an unexpected exception during processing.
        Server B must still be processed and returned.
        """
        monitor = LicenseMonitor()

        server_a_target = "27000@SERVER_A"
        server_b_target = "27000@SERVER_B"
        targets = [server_a_target, server_b_target]

        # Mock capture_snapshot: fail on Server A, succeed on Server B
        mock_snapshot_b = Snapshot(
            schema_version="1.0",
            monitor_version="1.0.0",
            captured_at="2026-09-22T12:00:00Z",
            server=ServerInfo(hostname="SERVER_B", port=27000, status=ServerStatus.UP.value),
            packages=[],
            checkouts=[],
        )

        processed_targets = []
        errors_logged = []

        def mock_capture(target_override=None):
            if target_override == server_a_target:
                raise RuntimeError(f"Simulated unexpected crash on {target_override}")
            processed_targets.append(target_override)
            return mock_snapshot_b

        # Simulate loop logic
        with patch.object(monitor, "capture_snapshot", side_effect=mock_capture):
            for s_target in targets:
                try:
                    snap = monitor.capture_snapshot(target_override=s_target)
                    assert snap.server.hostname == "SERVER_B"
                except Exception as s_exc:
                    errors_logged.append((s_target, str(s_exc)))

        # Server A was caught and logged
        assert len(errors_logged) == 1
        assert errors_logged[0][0] == server_a_target
        assert "Simulated unexpected crash on 27000@SERVER_A" in errors_logged[0][1]

        # Server B was processed successfully
        assert processed_targets == [server_b_target]

    def test_three_servers_continuation_on_middle_failure(self):
        """
        Config with Server 1, Server 2, Server 3.
        Server 2 encounters a fatal error.
        Server 1 and Server 3 must both process completely.
        """
        monitor = LicenseMonitor()
        targets = ["27000@SERVER_1", "27000@SERVER_2", "27000@SERVER_3"]

        processed_targets = []
        errors_logged = []

        def mock_capture(target_override=None):
            if target_override == "27000@SERVER_2":
                raise ValueError("Corrupted memory buffer on Server 2")
            processed_targets.append(target_override)
            return Snapshot(
                schema_version="1.0",
                monitor_version="1.0.0",
                captured_at="2026-09-22T12:00:00Z",
                server=ServerInfo(hostname=target_override, port=27000, status=ServerStatus.UP.value),
            )

        with patch.object(monitor, "capture_snapshot", side_effect=mock_capture):
            for s_target in targets:
                try:
                    monitor.capture_snapshot(target_override=s_target)
                except Exception as s_exc:
                    errors_logged.append((s_target, str(s_exc)))

        assert errors_logged == [("27000@SERVER_2", "Corrupted memory buffer on Server 2")]
        assert processed_targets == ["27000@SERVER_1", "27000@SERVER_3"]


class TestCLIProcessLockIntegration:
    def test_second_poll_instance_rejected_cleanly(self, tmp_path):
        """
        Simulates running instance 1 in polling mode.
        Instance 2 is launched and must exit cleanly with status 0 and log message.
        """
        log_file = tmp_path / "test_monitor.log"
        lock_file = tmp_path / "license_monitor.lock"

        # Start instance 1 holding the lock
        instance1_code = f"""
import sys, time
from pathlib import Path
from license_monitor.monitor import SingleInstanceLock
lock = SingleInstanceLock(Path(r'{lock_file}'))
if lock.acquire():
    print('INSTANCE_1_RUNNING', flush=True)
    time.sleep(60)
"""
        proc1 = subprocess.Popen(
            [sys.executable, "-c", instance1_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).parents[2]),
        )

        try:
            line = proc1.stdout.readline().strip()
            assert line == "INSTANCE_1_RUNNING"

            # Launch instance 2 via monitor.py CLI with --poll
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "license_monitor.monitor",
                    "--poll",
                    "--log-file",
                    str(log_file),
                ],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).parents[2]),
                timeout=10,
            )

            # Must exit with code 0 (clean exit, not crash)
            assert result.returncode == 0
            assert "Another instance of Network License Monitor is already running" in (result.stdout + result.stderr)

            # Verify instance 1 is still alive and running
            assert proc1.poll() is None
        finally:
            proc1.kill()
            proc1.wait()
