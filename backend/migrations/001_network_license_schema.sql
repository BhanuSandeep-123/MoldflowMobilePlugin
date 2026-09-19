-- ============================================================================
-- Migration: 001_network_license_schema.sql
-- Description: Independent PostgreSQL schema for Moldflow Mobile Network License Subsystem
-- ============================================================================

-- 1. License Servers Registry
CREATE TABLE IF NOT EXISTS license_servers (
    server_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    lmgrd_port INTEGER NOT NULL DEFAULT 27000,
    vendor_daemon TEXT NOT NULL DEFAULT 'adskflex',
    vendor_daemon_port INTEGER,
    status TEXT NOT NULL DEFAULT 'UNKNOWN',
    last_successful_poll TEXT,
    last_poll_attempt TEXT,
    last_error_code INTEGER,
    last_error_message TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Global Feature Catalog (Metadata description of features)
CREATE TABLE IF NOT EXISTS license_feature_catalog (
    feature_code TEXT PRIMARY KEY,
    feature_type TEXT NOT NULL,
    product_family TEXT,
    product_name TEXT,
    year_version TEXT,
    parent_package_code TEXT,
    catalog_status TEXT NOT NULL DEFAULT 'VERIFIED',
    description TEXT
);

-- 3. Snapshots Header History
CREATE TABLE IF NOT EXISTS license_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    server_status TEXT NOT NULL,
    lmgrd_version TEXT,
    adskflex_status TEXT,
    adskflex_version TEXT,
    error_code INTEGER,
    error_message TEXT,
    monitor_version TEXT NOT NULL DEFAULT '1.0.0',
    schema_version TEXT NOT NULL DEFAULT '1.0',
    raw_payload_hash TEXT NOT NULL,
    query_duration_ms REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
);

-- 4. Snapshot Feature States (Historical record per snapshot)
CREATE TABLE IF NOT EXISTS license_snapshot_features (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    feature_code TEXT NOT NULL,
    feature_type TEXT NOT NULL,
    total_issued INTEGER NOT NULL DEFAULT 0,
    in_use INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    utilization_pct REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES license_snapshots(snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
);

-- 5. Operational Current Feature State (Per server + feature)
CREATE TABLE IF NOT EXISTS license_server_features (
    server_id TEXT NOT NULL,
    feature_code TEXT NOT NULL,
    feature_type TEXT NOT NULL,
    total_issued INTEGER NOT NULL DEFAULT 0,
    in_use INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    utilization_pct REAL,
    last_snapshot_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (server_id, feature_code),
    FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
);

-- 6. Current Active Physical Checkouts (1 row per physical seat session)
CREATE TABLE IF NOT EXISTS license_active_checkouts (
    checkout_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    username TEXT NOT NULL,
    machine_name TEXT NOT NULL,
    display TEXT,
    package_feature_code TEXT NOT NULL,
    selected_component_code TEXT NOT NULL,
    version TEXT,
    server_handle TEXT,
    checkout_time TEXT NOT NULL,
    checkout_time_precision TEXT NOT NULL DEFAULT 'MINUTE',
    pid TEXT,
    is_borrowed BOOLEAN NOT NULL DEFAULT FALSE,
    is_incomplete BOOLEAN NOT NULL DEFAULT FALSE,
    anomaly_note TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_snapshot_id TEXT NOT NULL,
    PRIMARY KEY (server_id, checkout_id),
    FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
);

-- 7. License Events Audit History
CREATE TABLE IF NOT EXISTS license_events (
    event_id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    feature_code TEXT,
    checkout_id TEXT,
    username TEXT,
    machine_name TEXT,
    selected_component_code TEXT,
    previous_in_use INTEGER,
    new_in_use INTEGER,
    total_issued INTEGER,
    detected_at TEXT NOT NULL,
    snapshot_id TEXT,
    previous_snapshot_id TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (server_id) REFERENCES license_servers(server_id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_lic_snapshots_server_time ON license_snapshots(server_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_lic_snap_features_snap ON license_snapshot_features(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_lic_checkouts_server ON license_active_checkouts(server_id);
CREATE INDEX IF NOT EXISTS idx_lic_events_server_time ON license_events(server_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_lic_events_type_time ON license_events(event_type, detected_at DESC);
