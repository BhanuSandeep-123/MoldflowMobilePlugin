# Phase 6 Documentation: Stabilization, Notification Reliability, and Standalone Post-Analyze Agent Integration

## 1. Overview & Objectives
Following the successful Phase 5C desktop cutover, Phase 6 addressed two operational defects:
1. **Issue 1**: Analysis STARTED notification missing during normal Moldflow plugin execution.
2. **Issue 2**: Standalone Post-Analyze Agent failing with `No module named 'pythoncom'`, using legacy launcher/config, and lacking automated background execution.

All tasks were executed following the strict progression:
`Step 0 (Inspection Only) -> Step 1 (Fix STARTED Issue) -> Step 2 (Validate Venv/COM) -> Step 3 (Fix Launcher/Config) -> Step 4 (Background Task) -> Step 5 (Tests & Docs) -> Step 6 (Controlled Solve)`.

---

## 2. Root Cause Analysis

### Issue 1: Normal Plugin Analysis STARTED Notification
- **Failure in Latest Run (15:14:44)**:
  - In `plugin/logs/diagnostics_20260909_151444_010158.log`, Moldflow Synergy raised COM RPC error `(-2147023170, 'The remote procedure call failed.', None, None)` during `AnalysisSequence` assignment (`Fill`).
  - Synergy terminated and the macro exited. `study_doc.AnalyzeNow()` was **never reached**, so SCM job creation and job discovery by the normal plugin never occurred.
- **Architectural Notification Fragility**:
  - In `backend/app_postgres_ready.py`, `send_job_completion_notification()` dropped any status not in `{"INPROGRESS", "COMPLETED", "FAILED", "CANCELED"}`. Initial SCM statuses like `CREATED` or `QUEUED` were dropped.
  - If a solve transitioned quickly from `CREATED` to `COMPLETED`, `INPROGRESS` was never seen by the backend, causing the backend to emit `COMPLETED` without ever emitting `STARTED`.
  - In `plugin/cad_diagnostics.py`, once `job_id` was discovered, the polling cadence immediately switched from the tight 1.0s window to the slower card cadence (1.5s), increasing the chance of missing the brief `INPROGRESS` window.

### Issue 2: Standalone Post-Analyze Agent
- **Missing `pythoncom`**:
  - Global system Python (`C:\Program Files\Python314\python.exe`) has no `pywin32` installed.
  - The canonical plugin virtual environment (`C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\python.exe`) already contains validated `pywin32 312` and loads `pythoncom314.dll` reliably.
- **Legacy Path Coupling**:
  - `run_agent.bat` and `config.json` previously referenced the old `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin` directory.
  - `agent.py` and `inspection_engine.py` use `existing_plugin_path` strictly to import `compute_jobs.py`, `mobile_reporter.py`, `synergy_connect.py`, and `cad_diagnostics.py`. All four exist in `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin`.

---

## 3. Exact Changes Made

### A. Backend (`backend/app_postgres_ready.py`)
1. **Supported `STARTED` Status**:
   - `send_job_completion_notification()` now accepts `normalized_status == "STARTED"` alongside `INPROGRESS` as an explicit trigger for `notification_type = "STARTED"`.
   - Notification titles and bodies are formatted correctly for both `INPROGRESS` and `STARTED`.
2. **Device Catch-Up**:
   - In `/devices` registration, catch-up checks include active jobs in both `INPROGRESS` and `STARTED` status.

### B. Plugin Automation (`plugin/cad_diagnostics.py`)
1. **Tight Cadence during Early Job Discovery**:
   - In `refresh_job_card()`, after `job_id` is found, the 1.0s polling interval (`JOB_SEARCH_FAST_INTERVAL`) is maintained during the fast window (`JOB_SEARCH_FAST_WINDOW = 10.0s`) until `INPROGRESS` or a terminal status is observed.
   - Prevents fast solve transitions from skipping the `INPROGRESS` observation.

### C. Standalone Post-Analyze Agent
1. **Launcher (`run_agent.bat`)**:
   - Updated executable to `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\python.exe`.
   - Added safe fallback to legacy venv if ever missing.
2. **Configuration (`config.json`)**:
   - Set `"existing_plugin_path": "C:\\Users\\UnoTEAM-0144\\Documents\\MoldflowMobileSystem\\plugin"`.
3. **Background Scheduled Task**:
   - Task Name: `Moldflow Post-Analyze Agent`
   - Command: `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\pythonw.exe`
   - Arguments: `agent.py`
   - Working Directory: `C:\Users\UnoTEAM-0144\Documents\MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent`
   - Setting: `MultipleInstancesPolicy = IgnoreNew`
   - Created `deployment/scheduled_task_post_analyze.xml` and `deployment/register_post_analyze_task.ps1`.

### D. Automated Tests (`tests/test_phase6_notifications.py`)
- Added 8 unit tests covering status normalization, deduplication, terminal locking, device catch-up, mobile reporter filtering, desktop SCM cadence, and environment verification. All 32 repository unit tests pass.

---

## 4. Architecture Boundaries & Operational Mapping

| Component | Responsibility | Runtime Path | Execution Mode |
| :--- | :--- | :--- | :--- |
| **Canonical Plugin** | Moldflow Synergy macro, sequence setup, mesh diagnostics, solver launch (`AnalyzeNow`) | `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin` | Foreground via Autodesk Synergy macro |
| **Canonical Job Monitor** | SCM queue monitoring, resilience outside Synergy | `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\monitor` | Background Scheduled Task (`\Moldflow Mobile Job Monitor`) running `standalone_job_monitor.py` |
| **Standalone Post-Analyze Agent** | Independent post-Analyze inspection, CAD/mesh checks via COM | `C:\Users\UnoTEAM-0144\Documents\MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent` | Background Scheduled Task (`\Moldflow Post-Analyze Agent`) running `agent.py` |
| **Render Backend** | HTTPS REST API, PostgreSQL storage, FCM dispatch | `https://moldflowplugin-mobile-app.onrender.com` | Cloud container (Linux) |

> [!IMPORTANT]
> - The old plugin (`C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin` and `C:\MF\MoldflowSynergyPlugin`) remains untouched for rollback.
> - Render is strictly the cloud API and database host, not a local SCM execution runner.
> - The two desktop background tasks run independently without mutual process contention or duplicate monitor loops.

---

## 5. Rollback Procedure

If rollback of Phase 6 is required:
1. **Scheduled Task**:
   ```powershell
   schtasks /Delete /TN "Moldflow Post-Analyze Agent" /F
   ```
2. **Standalone Configuration**:
   In `C:\Users\UnoTEAM-0144\Documents\MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent\config.json`, revert `existing_plugin_path` to `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin`.
3. **Repository Working Tree**:
   ```powershell
   git checkout master
   ```
