# Autodesk Moldflow Synergy Mobile Job Monitoring — Production Operations Runbook

## 1. System Architecture Overview

```text
+------------------------------------+
|  Autodesk Moldflow Synergy 2027    |
|  (cad_diagnostics.py + compute_jobs|
|   + mobile_reporter.py)            |
+-----------------+------------------+
                  | HTTP POST /reportJobStatus (X-Api-Key)
                  v
+------------------------------------+
|  FastAPI Backend (Port 8001)       |
|  - psycopg_pool ConnectionPool     |
|  - JWT Bearer Authentication       |
|  - Cross-User Isolation Enforcement|
+--------+------------------+--------+
         |                  |
         v                  v
+------------------+  +-------------------------------+
|  PostgreSQL 17   |  | Firebase Cloud Messaging (FCM)|
|  (moldflow_mobile|  | (HTTP v1 OAuth2 Push Delivery)|
|   database)      |  +---------------+---------------+
+------------------+                  |
                                      v
                      +-------------------------------+
                      | .NET MAUI Android Mobile App  |
                      | - SecureStorage JWT Session   |
                      | - Auto-reconnecting Polling   |
                      | - Interactive Event Timeline  |
                      | - FCM Push Listener & Routing |
                      +-------------------------------+
```

---

## 2. Server Configuration & Startup

### Environment Variables (`.env`)
Location: `C:\MF\MoldflowSynergyPlugin\mobile_backend\.env`

```ini
DATABASE_URL=postgresql://moldflow_app:Moldflow123@127.0.0.1:5432/moldflow_mobile
JWT_SECRET_KEY=moldflow-production-secret-key-32bytes-min-2026-secure!
GOOGLE_APPLICATION_CREDENTIALS=C:\MF\MoldflowSynergyPlugin\mobile_backend\firebase-service-account.json
DEV_USER_ID=DEV-USER-001
DEV_USER_EMAIL=dev@example.local
DEV_USER_PASSWORD=MoldflowDev@2026!
DEV_MACHINE_ID=DEV-PC-001
DEV_MACHINE_NAME=Development PC
```

### Starting the Backend
From PowerShell:
```powershell
& "C:\MF\MoldflowSynergyPlugin\mobile_backend\.cloud_venv\Scripts\python.exe" -m uvicorn app_postgres_ready:app --host 127.0.0.1 --port 8001
```

### Health Monitoring Endpoint
Query `http://127.0.0.1:8001/health`:
```json
{
  "status": "healthy",
  "service": "Moldflow Mobile Job Backend",
  "database": "postgresql",
  "pool": {
    "min_size": 2,
    "max_size": 10
  },
  "timestamp": "2026-08-31T07:33:53.303849+00:00"
}
```

---

## 3. Workstation Deployment (Synergy Plugin)

To install or configure the mobile reporting plugin on a Moldflow workstation:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\MF\MoldflowSynergyPlugin\MoldflowMobile\install_synergy_plugin.ps1" `
    -BackendUrl "http://127.0.0.1:8001" `
    -ApiKey "dev-moldflow-key-change-me" `
    -UserId "DEV-USER-001" `
    -MachineId $env:COMPUTERNAME
```

---

## 4. Mobile Application Deployment (.NET MAUI)

### Building Signed Release APK
```powershell
dotnet build "C:\MF\MoldflowSynergyPlugin\MoldflowMobile\MoldflowMobile\MoldflowMobile.csproj" -c Release -f net10.0-android
```
Release binary output:
`C:\MF\MoldflowSynergyPlugin\MoldflowMobile\MoldflowMobile\bin\Release\net10.0-android\com.companyname.moldflowmobile-Signed.apk`

### Installing to Android Device / Emulator
```powershell
& "C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe" install -r "C:\MF\MoldflowSynergyPlugin\MoldflowMobile\MoldflowMobile\bin\Release\net10.0-android\com.companyname.moldflowmobile-Signed.apk"
```

---

## 5. Database Maintenance & Disaster Recovery

### Automated Backup
Creates a compressed archive and prunes backups older than 14 days:
```powershell
powershell -ExecutionPolicy Bypass -File "C:\MF\MoldflowSynergyPlugin\MoldflowMobile\backup_database.ps1"
```
Backups stored in: `C:\MF\MoldflowSynergyPlugin\backups\`

### Database Restore
Restores tables, schemas, indexes, and foreign keys from the latest dump:
```powershell
powershell -ExecutionPolicy Bypass -File "C:\MF\MoldflowSynergyPlugin\MoldflowMobile\restore_database.ps1"
```

---

## 6. Verification & Concurrency Test Scripts

- **Live Concurrency Test (20 parallel requests)**:
  ```powershell
  & "C:\MF\MoldflowSynergyPlugin\mobile_backend\.cloud_venv\Scripts\python.exe" "C:\Users\UnoTEAM-0144\.gemini\antigravity-ide\brain\67ad0043-4738-4050-adb1-167c6bd45b7a\scratch\test_live_concurrency.py"
  ```
- **End-to-End Solver & Notification Simulation**:
  ```powershell
  & "C:\MF\MoldflowSynergyPlugin\mobile_backend\.cloud_venv\Scripts\python.exe" "C:\Users\UnoTEAM-0144\.gemini\antigravity-ide\brain\67ad0043-4738-4050-adb1-167c6bd45b7a\scratch\verify_e2e_workflow.py"
  ```
