# Network License Monitor (Stage 1)

Local, read-only monitor for Autodesk FlexNet license servers (`lmgrd` and `adskflex`).
Part of the **Moldflow Mobile System**.

---

## Architecture Principles Enforced

1. **Strictly Read-Only (Rule 1):** The monitor only issues `lmutil lmstat -a -c <target>`. It never executes modifying commands (`lmdown`, `lmremove`, `lmreread`), never modifies license files, and never terminates processes.
2. **PACKAGE is the Seat Authority (Rule 2):** For Autodesk products (e.g. Moldflow Synergy), FlexNet reports both a `PACKAGE` feature (`77800MFS_T_F`) and a `COMPONENT` feature (`88232MFS_2027_0F`) in-use for a single physical user session. The monitor calculates capacity and usage exclusively from the `PACKAGE` feature. `1 PACKAGE + 1 COMPONENT = 1 physical seat`, never 2.
3. **Generic Parsing & Safe Cataloging (Rules 3 & 4):** Features not pre-configured in the catalog (e.g. `76800MFAA_T_F`) are preserved with raw codes and assigned `catalog_status: UNKNOWN`. The parser never crashes on unexpected layouts.
4. **Diagnostic Error Preservation (Rule 5):** FlexNet error diagnostics (e.g. `-15` WinSock Connection Refused, `-96` Host Down / Not Found) are extracted and preserved alongside process exit codes.
5. **Consolidated Server Health (Rule 6):** Reports `UP`, `DOWN`, `VENDOR_DOWN`, or `UNKNOWN`.
6. **Deterministic Checkout Identity (Rule 7):** Canonical SHA-256 hash derived from:
   `SHA256(server_hostname + ":" + username + ":" + machine_name + ":" + selected_component_code + ":" + checkout_time_minute_str + ":" + pid)`
   with strict lowercase/trim normalization and no random salts.
7. **Diagnostic-Only Server Handles (Rule 8):** FlexNet server handle numbers (e.g. `101`, `201`) vary between sessions and are recorded as diagnostic metadata, never as part of the identity.
8. **Precise Process Attribution (Rule 9):** The PID reported by FlexNet corresponded to `AdskLicensingAgent.exe` during observed Synergy sessions.
9. **Minute Precision (Rule 10):** FlexNet checkout timestamps are minute resolution (`checkout_time_precision: MINUTE`). Monitor observation time is recorded separately at high precision in UTC ISO-8601 (`captured_at`).

---

## Directory Structure

```text
license_monitor/
├── __init__.py
├── config.json          # Configuration (lmutil path, target, timeout, catalog)
├── models.py            # Dataclasses and domain models
├── parser.py            # FlexNet lmstat output parser and normalizer
├── monitor.py           # Subprocess execution layer and CLI runner
├── README.md            # Documentation
└── tests/
    ├── __init__.py
    ├── test_parser.py   # Comprehensive automated unit tests
    └── fixtures/        # Real captured FlexNet outputs
        ├── synergy_idle.txt
        ├── synergy_active.txt
        ├── synergy_released.txt
        ├── server_error_15.txt
        ├── server_error_96.txt
        └── server_mfaa.txt
```

---

## Configuration (`config.json`)

```json
{
  "monitor_version": "1.0.0",
  "schema_version": "1.0",
  "server": {
    "hostname": "LAPTOP-CA2QN87F",
    "port": 27000,
    "target": "27000@LAPTOP-CA2QN87F"
  },
  "lmutil_path": "C:\\Autodesk\\Network License Manager\\lmutil.exe",
  "query_timeout_seconds": 15,
  "poll_interval_seconds": 60,
  "known_packages": { ... }
}
```

---

## Usage

### Run Server A Monitor Instance
```bash
py -m license_monitor.monitor --config license_monitor/config_server_a.json --once
```

### Run Server B Monitor Instance
```bash
py -m license_monitor.monitor --config license_monitor/config_server_b.json --once
```

### Run Continuous Polling Loop (Per Server)
```bash
py -m license_monitor.monitor --config license_monitor/config_server_a.json --poll
py -m license_monitor.monitor --config license_monitor/config_server_b.json --poll
```

### Run Automated Test Suite
```bash
py -m unittest license_monitor.tests.test_parser -v
```

