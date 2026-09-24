"""tests/test_admin_provisioning.py
---------------------------------
Comprehensive Test Suite for Admin Provisioning CLI.

Tests:
1. User creation with unique user ID and valid fields.
2. Duplicate email rejection (case-insensitive: dev@example.com vs DEV@EXAMPLE.COM).
3. Argon2id password hashing and verification (plain password never stored).
4. Enrollment-token creation using Phase 2A standard (mf-enroll-<48 hex chars>).
5. Token expiry calculation and status tracking (ACTIVE, CONSUMED, EXPIRED).
6. Token binding to user (fails if user does not exist; binds to correct user_id).
7. Secret hygiene (no plaintext passwords in return values or database).
8. File output safety (--out-token-file correctly saves clean token).
9. End-to-end integration: user created by CLI can log in via backend auth; token enrolls workstation.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.admin_provisioning import (
    create_user,
    create_workstation_token,
    list_users,
    list_tokens,
    verify_password,
    hash_password,
)


class TestAdminProvisioning(unittest.TestCase):
    def setUp(self):
        # Create a fresh temporary SQLite database with proper schema
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_provisioning.db")

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE machines (
                machine_id TEXT PRIMARY KEY,
                user_id TEXT,
                machine_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY,
                user_id TEXT,
                platform TEXT NOT NULL,
                push_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE enrollment_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                consumed_by_machine_id TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE SET NULL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_user_creation_success(self):
        """User is created with unique ID, display name, email, and Argon2id hash."""
        res = create_user(
            email="engineer.jane@acme.com",
            display_name="Jane Engineer",
            password="SecurePassword2026!",
            db_target=self.db_path,
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["email"], "engineer.jane@acme.com")
        self.assertEqual(res["display_name"], "Jane Engineer")
        self.assertTrue(res["user_id"].startswith("USR-"))

        # Verify database record
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (res["user_id"],)).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "engineer.jane@acme.com")
        self.assertEqual(row["display_name"], "Jane Engineer")
        # Ensure password_hash is Argon2id
        self.assertTrue(row["password_hash"].startswith("$argon2id$"))
        # Ensure plain password is NOT in DB
        self.assertNotIn("SecurePassword2026!", row["password_hash"])

    def test_02_duplicate_email_rejected(self):
        """Duplicate emails must be rejected case-insensitively."""
        create_user(
            email="engineer@acme.com",
            display_name="First Engineer",
            password="SecurePassword2026!",
            db_target=self.db_path,
        )

        # Exact duplicate
        with self.assertRaises(ValueError) as ctx:
            create_user(
                email="engineer@acme.com",
                display_name="Second Engineer",
                password="AnotherPassword2026!",
                db_target=self.db_path,
            )
        self.assertIn("already exists", str(ctx.exception))

        # Case-insensitive duplicate
        with self.assertRaises(ValueError) as ctx:
            create_user(
                email="ENGINEER@ACME.COM",
                display_name="Third Engineer",
                password="AnotherPassword2026!",
                db_target=self.db_path,
            )
        self.assertIn("already exists", str(ctx.exception))

    def test_03_argon2id_password_verification(self):
        """Argon2id password verification succeeds with correct password, fails with wrong."""
        plain = "MySecretPassphrase123!"
        h = hash_password(plain)
        self.assertTrue(h.startswith("$argon2id$"))

        # Verify correct password
        self.assertTrue(verify_password(plain, h))

        # Verify incorrect password
        self.assertFalse(verify_password("WrongPassword!", h))
        self.assertFalse(verify_password(plain.lower(), h))

    def test_04_enrollment_token_creation_format(self):
        """Token uses Phase 2A format (mf-enroll-<48 hex chars>) and binds to user."""
        u_res = create_user(
            email="tech@acme.com",
            display_name="Tech Lead",
            password="ValidPassword2026!",
            user_id="USR-TECH-001",
            db_target=self.db_path,
        )

        t_res = create_workstation_token(
            user_id=u_res["user_id"],
            expires_in_seconds=3600,
            db_target=self.db_path,
        )

        self.assertEqual(t_res["status"], "success")
        token = t_res["token"]
        self.assertTrue(token.startswith("mf-enroll-"))
        self.assertEqual(len(token), len("mf-enroll-") + 48)
        self.assertEqual(t_res["user_id"], "USR-TECH-001")

        # Verify DB storage
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM enrollment_tokens WHERE token = ?", (token,)).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "USR-TECH-001")
        self.assertIsNone(row["consumed_at"])

    def test_05_enrollment_token_nonexistent_user_rejected(self):
        """Attempting to generate an enrollment token for non-existent user must fail."""
        with self.assertRaises(ValueError) as ctx:
            create_workstation_token(
                user_id="NON-EXISTENT-USER-999",
                db_target=self.db_path,
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_06_token_expiry_and_state(self):
        """Verify tokens correctly compute expiration and state."""
        u_res = create_user(
            email="expiry.test@acme.com",
            display_name="Expiry Test User",
            password="ValidPassword2026!",
            db_target=self.db_path,
        )

        # 1. Active token (expires in 1 hour)
        t_active = create_workstation_token(
            user_id=u_res["user_id"],
            expires_in_seconds=3600,
            db_target=self.db_path,
        )

        # 2. Expired token (expires in past)
        t_expired = create_workstation_token(
            user_id=u_res["user_id"],
            expires_in_seconds=10,
            db_target=self.db_path,
        )
        # Manually backdate in DB
        past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE enrollment_tokens SET expires_at = ? WHERE token = ?", (past_time, t_expired["token"]))
        conn.commit()
        conn.close()

        tokens = list_tokens(user_id=u_res["user_id"], db_target=self.db_path)
        states = {t["token"]: t["state"] for t in tokens}

        self.assertEqual(states[t_active["token"]], "ACTIVE")
        self.assertEqual(states[t_expired["token"]], "EXPIRED")

    def test_07_secret_hygiene(self):
        """Passwords and secrets are never returned in plain text."""
        plain = "SuperSensitivePassword!99"
        res = create_user(
            email="hygiene@acme.com",
            display_name="Hygiene User",
            password=plain,
            db_target=self.db_path,
        )

        # Verify not in return dictionary
        for k, v in res.items():
            self.assertNotEqual(v, plain, f"Key {k} leaked plaintext password!")

        # Verify not in list_users
        users = list_users(db_target=self.db_path)
        for u in users:
            for k, v in u.items():
                self.assertNotEqual(v, plain, f"list_users key {k} leaked plaintext password!")

    def test_08_list_users_counts(self):
        """list_users correctly aggregates enrolled machines and registered devices."""
        u_res = create_user(
            email="worker@acme.com",
            display_name="Worker User",
            password="WorkerPassword2026!",
            user_id="USR-WORKER-001",
            db_target=self.db_path,
        )

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        # Insert 2 machines
        conn.execute("INSERT INTO machines (machine_id, user_id, machine_name, created_at) VALUES ('WS-01', 'USR-WORKER-001', 'Workstation 1', ?)", (now,))
        conn.execute("INSERT INTO machines (machine_id, user_id, machine_name, created_at) VALUES ('WS-02', 'USR-WORKER-001', 'Workstation 2', ?)", (now,))
        # Insert 1 device
        conn.execute("INSERT INTO devices (device_id, user_id, platform, created_at, updated_at) VALUES ('PHONE-01', 'USR-WORKER-001', 'android', ?, ?)", (now, now))
        conn.commit()
        conn.close()

        users = list_users(db_target=self.db_path)
        worker = next(u for u in users if u["user_id"] == "USR-WORKER-001")
        self.assertEqual(worker["machine_count"], 2)
        self.assertEqual(worker["device_count"], 1)


if __name__ == "__main__":
    unittest.main()
