# Moldflow Mobile — IT Operations & Workstation Onboarding Runbook

**Document Version:** 1.0.0 (Production Release)  
**Target Systems:** Moldflow Workstation (Windows 10/11), Cloud Backend (Render/PostgreSQL), Mobile Client (.NET MAUI Android)

---

## Architecture & Infrastructure Overview

### User Experience Model
```
┌─────────────────────────┐     ┌───────────────────────┐     ┌────────────────────────┐
│  Install Moldflow Mobile│ ──> │ Login with Credentials│ ──> │ View Live Licenses     │
│  (Android Application)  │     │ (JWT Bearer Auth)     │     │ + Workstations & Jobs  │
│                         │     │                       │     │ + Push Notifications   │
└─────────────────────────┘     └───────────────────────┘     └────────────────────────┘
```

### Infrastructure Separation
```
Workstations (Distributed)                   Centralized Cloud & Licensing
┌───────────────────────────────┐            ┌────────────────────────────────────────┐
│ Moldflow Workstation A        │            │ Render Cloud Backend (FastAPI + PG)    │
│  - ProgramFiles Runtime Code  │ ──HTTPS──> │  - Multi-tenant Job Tracking           │
│  - ProgramData Configuration  │   POST     │  - FCM Push Dispatch                   │
│  - Background Post-Analyze    │            │  - Token-Based Workstation Enrollment  │
│  - SCM Local Queue (44100)    │            └────────────────────────────────────────┘
└───────────────────────────────┘                                 ▲
                                                                  │ HTTPS Poll
┌───────────────────────────────┐            ┌────────────────────────────────────────┐
│ Moldflow Workstation B        │            │ Centralized License Monitor (Host)     │
│  - Independent Runtime & Agent│ ──HTTPS──> │  - Polls Autodesk FlexLM (Port 27000)  │
│  - Machine-Isolated Identity  │            │  - Reports Company-Wide License State  │
└───────────────────────────────┘            └────────────────────────────────────────┘
```
- **Each Workstation:** Requires a background workstation runtime installed locally to detect SCM simulation solves, stream progress, and report job lifecycle events.
- **Network License Monitor:** Runs centrally (on the license server host or a designated server) to poll FlexLM license status and supply real-time seat inventory for all mobile users.

---

## 1. Prerequisites

### Workstation Requirements
- **Operating System:** Windows 10 or Windows 11 (64-bit).
- **Moldflow Installation:** Autodesk Moldflow Insight / Synergy 2026.
- **Simulation Compute Manager:** Autodesk SCM Service (`adskscm2`) running locally on port `44100`.
- **Python Environment:** Python 3.10+ (64-bit) available on system `PATH` or in standard local user installation directory.
- **Administrative Privileges:** Required once during installation to register Windows Scheduled Tasks and configure Program Files / ProgramData directory permissions.

### Mobile Device Requirements
- **Platform:** Android 8.0+ (API Level 26+).
- **Google Services:** Google Play Services enabled (required for Firebase Cloud Messaging).
- **Connectivity:** Internet access (Wi-Fi or cellular data) to connect to the cloud backend.

### Network Requirements
- Outbound HTTPS (port 443) access to `https://moldflowplugin-mobile-app.onrender.com`.
- Workstation access to the Autodesk License Server (`port 27000@<license_server>`).

---

## 2. Admin User Creation

IT administrators provision user accounts using the secure Admin Provisioning CLI. Plaintext passwords are never stored; they are hashed using **Argon2id** (`pwdlib`).

### Command
```powershell
python backend/admin_provisioning.py create-user `
  --email "<user_email>" `
  --name "<display_name>"
```
*(If `--password` is omitted, the CLI securely prompts for hidden input without terminal echo).*

### Example Output
```text
[SUCCESS] User created successfully:
  User ID:      USR-ALICE-3F8A
  Display Name: Alice Smith
  Email:        alice.smith@company.com
  Created At:   2026-09-24T10:00:00+00:00
  (Password securely hashed with Argon2id; plain password was not stored)
```

---

## 3. Workstation Enrollment Workflow

Enrollment connects a physical workstation to an existing user account using a one-time cryptographic token (`mf-enroll-...`).

### Standard 2-Step Workflow (Recommended)

#### Step 1: IT Admin generates user and token
```powershell
python backend/admin_provisioning.py onboard-user `
  --email "engineer.doe@company.com" `
  --name "John Doe" `
  --expires-hours 2.0 `
  --out-token-file "token.txt"
```

#### Step 2: Deploy Workstation Runtime on engineer's PC

##### Option A: Enterprise MSI Deployment (Recommended for IT / Intune / GPO)
Run from an elevated command prompt or via software distribution system:
```powershell
msiexec.exe /i "MoldflowMobileWorkstation.msi" `
  ENROLLMENT_TOKEN="<one_time_token>" `
  /qn /l*v "C:\ProgramData\MoldflowMobile\logs\msi_install.log"
```
Or with an enrollment token file:
```powershell
msiexec.exe /i "MoldflowMobileWorkstation.msi" `
  ENROLLMENT_TOKEN_FILE="C:\path\to\token.txt" `
  /qn /l*v "C:\ProgramData\MoldflowMobile\logs\msi_install.log"
```
*(For interactive deployment, IT can double-click `MoldflowMobileWorkstation.msi` to run the graphical wizard).*

##### Option B: Scripted PowerShell Installation
Open an elevated (Run as Administrator) PowerShell window on the workstation:
```powershell
powershell.exe -ExecutionPolicy Bypass -File Install-MoldflowWorkstation.ps1 `
  -EnrollmentTokenFile "token.txt" `
  -Silent
```

### What Happens Automatically:
1. MSI / Installer reads the enrollment token and calls `POST /api/workstation/enroll`.
2. Backend consumes the token, registers `machine_id`, and issues a dedicated 256-bit API key.
3. Runtime files are installed cleanly into `C:\Program Files\MoldflowMobileWorkstation`.
4. Secure workstation config is written to `%ProgramData%\MoldflowMobile\config.json`.
5. Least-privilege ACLs are configured on `%ProgramData%\MoldflowMobile\logs` and `inspection_results`.
6. Windows Scheduled Tasks (`Moldflow Mobile Job Monitor` and `Moldflow Post-Analyze Agent`) are registered and started with logon triggers, `IgnoreNew` single-instance protection, and self-healing auto-restart (`RestartCount=3`, `RestartInterval=PT1M`).
7. Temporary token file `token.txt` is securely deleted from disk immediately.

---

## 4. Android App Installation

1. Copy `com.companyname.moldflowmobile-Signed.apk` to the Android device (via direct USB transfer, enterprise MDM, or private internal download link).
2. On Android, tap the APK and select **Install** (allow unknown sources if prompted for internal distribution).
3. Open **Uno** (Moldflow Mobile) from the app drawer.

---

## 5. User Login & Device Registration

1. Launch the app.
2. In the **Email** field, enter the provisioned email (e.g., `engineer.doe@company.com`).
3. In the **Password** field, enter the provisioned password.
4. Tap **Sign In**.
5. The application:
   - Authenticates against `POST /auth/login` and receives a secure JWT token.
   - Stores the token in Android `SecureStorage` (persisting across app restarts).
   - Automatically registers the device's FCM push token via `POST /devices/register`.
   - Navigates directly to the Jobs dashboard.

---

## 6. License & Server Visibility

1. From the top-right of the Jobs dashboard, tap the **🔑 Licenses** button.
2. The Network License screen loads real-time data from `GET /licenses/overview`:
   - **Environment Inventory:** Total and available license seats across all servers (Autodesk Moldflow Insight, Moldflow Synergy, and MFAA solvers).
   - **Active License Servers:** Hostname, FlexLM port, live server status (`UP` / `DOWN`), vendor daemon version (`adskflex v11.19.9`), and last poll timestamp.
3. This information refreshes automatically and provides immediate visibility into whether license seats are available before launching large solve batches.

---

## 7. Job Monitoring Lifecycle

Once installed, workstation solve monitoring is completely automatic:
1. Engineer starts an analysis in Autodesk Moldflow Synergy or submits an SCM job.
2. The local SCM daemon registers the job on port `44100`.
3. The background `Moldflow Post-Analyze Agent` detects the new solve within seconds.
4. As the solver computes (`mhb3d`, flow, pack, warp), progress milestones (0% to 100%) are streamed via HTTPS to `POST /reportJobStatus`.
5. Mobile users see the job appear in the active card count and progress bar in real time.

---

## 8. Notifications

- **STARTED Notification:** Triggered on the first solve progress tick (0% – 3%). Notifies the user: `"<study_name> has started running."`
- **COMPLETED Notification:** Triggered when SCM signals job completion (100%). Notifies the user: `"<study_name> completed successfully."`
- **FAILED / CANCELED Notification:** Dispatched if the solver aborts or the job is cancelled.
- **Deduplication:** The backend enforces a database-level unique constraint (`job_id`, `notification_type`). Each notification is delivered **exactly once**.

---

## 9. Multi-Workstation Usage

A single engineer can have multiple computers enrolled (e.g., a primary desktop and a laptop):
1. **Enroll Both:** Run `Install-MoldflowWorkstation.ps1` on each machine using tokens bound to the same `user_id`.
2. **Unified Dashboard:** The mobile app displays jobs from all workstations belonging to that user.
3. **Machine Tagging:** Each job card explicitly labels originating machine identity (e.g., `Machine: DESKTOP-23TMNR6` vs `Machine: LAPTOP-CA2QN87F`).
4. **Workstation Picker:** Use the **Workstation** dropdown filter at the top of the Jobs screen to view jobs from a specific workstation or select **All Workstations**. Top summary counters update to reflect the selected workstation's breakdown.

---

## 10. Credential & Token Security

- **One-Time Tokens:** Enrollment tokens (`mf-enroll-...`) can only be used once. Replay attempts return `401 Unauthorized`.
- **Ephemeral Lifespan:** Tokens automatically expire after their configured duration (default: 1 hour).
- **Strict File ACLs:** `%ProgramData%\MoldflowMobile\config.json` is protected:
  - `SYSTEM`: Full Control
  - `Administrators`: Full Control
  - `Standard Users`: Read and Execute only (cannot tamper with or extract keys).
- **Zero Plaintext Passwords:** Argon2id one-way hashing ensures user passwords cannot be recovered from database dumps.

---

## 11. Upgrade & Reinstall Procedure
 
 To upgrade the workstation runtime or refresh files:
 
- **Via MSI (Recommended):**
  ```powershell
  msiexec.exe /i "MoldflowMobileWorkstation.msi" /qn /l*v "C:\ProgramData\MoldflowMobile\logs\msi_upgrade.log"
  ```
- **Via PowerShell:**
  ```powershell
  powershell.exe -ExecutionPolicy Bypass -File Install-MoldflowWorkstation.ps1 -Silent
  ```
 
 **Automatic Credential Preservation:**
 Both the MSI and the installer detect existing valid `%ProgramData%\MoldflowMobile\config.json` and preserve the workstation's active enrollment API key. No new enrollment token is required. Existing scheduled tasks are safely stopped during file update and re-registered/restarted upon completion.
 
 ---
 
 ## 12. Uninstall & Rollback Procedure
 
 To completely remove the workstation runtime software:
 
- **Via MSI (Recommended):**
  ```powershell
  msiexec.exe /x "MoldflowMobileWorkstation.msi" /qn /l*v "C:\ProgramData\MoldflowMobile\logs\msi_uninstall.log"
  ```
- **Via PowerShell:**
  ```powershell
  powershell.exe -ExecutionPolicy Bypass -File deployment/Uninstall-MoldflowWorkstation.ps1 -Silent
  ```
 
 **Uninstallation Actions:**
 - Safely stops and unregisters only `Moldflow Mobile Job Monitor` and `Moldflow Post-Analyze Agent` Scheduled Tasks.
 - Leaves the centralized `Moldflow Network License Monitor` completely untouched and running.
 - Restores the original Synergy startup hook backup (`run_startup.vbs.bak` → `run_startup.vbs`) if applicable.
 - Cleans up `C:\Program Files\MoldflowMobileWorkstation` runtime files.
 - Preserves `%ProgramData%\MoldflowMobile\config.json` and logs for audit and reinstall continuity.

---

## 13. Troubleshooting

### Scheduled Task Diagnostics
Check task states from PowerShell:
```powershell
Get-ScheduledTask -TaskName 'Moldflow*' | Select-Object TaskName, State
```
Both `Moldflow Mobile Job Monitor` and `Moldflow Post-Analyze Agent` should show `Running`. If stopped, restart manually:
```powershell
Start-ScheduledTask -TaskName "Moldflow Post-Analyze Agent"
```

### Log File Locations
- **Post-Analyze Agent Log:** `C:\ProgramData\MoldflowMobile\logs\standalone_agent.log`
- **Job Monitor Log:** `C:\ProgramData\MoldflowMobile\logs\job_monitor.log`
- **Network License Log (Central Host):** `<install_dir>\license_monitor\license_monitor.log`

### Cloud Backend Health Check
Verify network reachability from the workstation:
```powershell
Invoke-RestMethod -Uri "https://moldflowplugin-mobile-app.onrender.com/health" -Method Get
```
Expected response: `status: healthy`, `database: postgresql`.

---

## 14. Known Non-Blocking Item: pythoncom Inspection

### Description
On workstations where the system Python runtime does not include the optional `pywin32` library, the agent logs:
```text
WARNING Inspection could not access Synergy: No module named 'pythoncom'
INFO Inspection finished: UNAVAILABLE
```

### Operational Impact
**Non-blocking.** This warning only affects automated pre-flight CAD geometry and mesh inspection. Core solver tracking, SCM queue monitoring, progress milestones, and push notifications operate completely normally.

### Optional Resolution
If pre-flight geometry inspection is desired on that specific workstation:
```powershell
pip install pywin32
```

---

## 15. Production Support Checklist

| Component | Check | Normal State |
| :--- | :--- | :--- |
| **Backend** | Query `/health` | `HTTP 200`, `healthy`, `database: postgresql` |
| **Database** | Supabase Connection Pool | Active, 2–10 pooled connections |
| **Workstation Tasks** | `Get-ScheduledTask Moldflow*` | `Running` for both agent and monitor |
| **Workstation Cleanliness**| Inspect Program Files | 0 `.pyc`, 0 `__pycache__`, 0 `.log` |
| **Workstation ACL** | Inspect `%ProgramData%\MoldflowMobile` | Admin/System Full Control, Users Read-Only |
| **License Monitor** | Inspect `/licenses/overview` | Active servers show status `UP` with recent poll |
| **FCM Push** | Run test solve | STARTED and COMPLETED alerts received on phone |
