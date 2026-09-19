# Current System Architecture

Documents the system as it exists and is executing on 2026-09-09.
This is a description of current behavior, not a target design.

## High-Level System Diagram

```
Autodesk Moldflow Synergy 2027
      │
      │  (startup command)
      ▼
C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands\run_startup.vbs
      │
      │  PLUGIN_DIR = C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin
      │
      ├──► moldflow_observer.py          (event loop, COM event dispatch)
      │
      ├──► standalone_job_monitor.py     (launched if not already running)
      │         │
      │         │  GET /internal/active-jobs
      │         ├──► FastAPI Backend ──► PostgreSQL / Supabase
      │         │
      │         │  GET /ComputeQueue/v1/jobs/{scm_job_id}
      │         └──► SCM (port 44100)
      │
      └──► moldflow_startup.py           (plugin initialization, hidden)
                │
                └──► Embedded MFC WebBrowser UI Panel
                          │
                          └──► embedded_ui.py / ui_bridge.py


Moldflow Synergy UI (User triggers analysis)
      │
      ▼
cad_diagnostics.py
      │
      ├── start_job_watch()              (records baseline SCM job list)
      │
      ├── study_doc.AnalyzeNow()         (Synergy COM call)
      │         │
      │         ▼
      │   Autodesk Simulation Compute Manager (SCM 2.0)
      │   Service: adskscm2
      │   Ports: 44100 (queue), 44200 (solver), 44500 (internal)
      │         │
      │         ▼
      │   SCM Job: status QUEUED → INPROGRESS → COMPLETED/FAILED
      │
      ├── wait_for_results() polling loop
      │         │
      │         └── refresh_job_card() every 1.5-4.0 s
      │                   │
      │                   ├── compute_jobs.find_job()
      │                   │       └── GET http://127.0.0.1:44100/ComputeQueue/v1/jobs
      │                   │
      │                   └── mobile_reporter.report_status()
      │                           └── POST https://moldflowplugin-mobile-app.onrender.com/reportJobStatus
      │                                   X-Api-Key: <from mobile_report_config.json>
      │
      ▼
FastAPI Backend (app_postgres_ready.py)
Running on: Render Cloud (https://moldflowplugin-mobile-app.onrender.com)
Database: Supabase PostgreSQL (aws-0-ap-northeast-2.pooler.supabase.com)
      │
      ├── Upsert job record in `jobs` table
      ├── Insert event in `job_events` table
      │
      └── send_job_completion_notification()
                │
                ├── Check: status in {INPROGRESS, COMPLETED, FAILED, CANCELED}
                ├── Check: job_notifications UNIQUE constraint (dedup)
                ├── Query: devices table for user's Android push tokens
                │
                └── fcm_service.send_fcm_notification()
                          │
                          └── POST https://fcm.googleapis.com/v1/projects/{project_id}/messages:send
                                    Authorization: Bearer <OAuth2 token from service account>
                                          │
                                          ▼
                                  Android Device (MoldflowMobile app)
                                          │
                                          ├── Background: System tray notification
                                          └── Foreground: FirebaseService.OnNotificationReceived()
                                                            └── ShowLocalNotification()
```

## Standalone Monitor Path (Post-Synergy-Close)

When Synergy closes, `cad_diagnostics.py` terminates. The SCM service continues
running as a Windows service. The standalone monitor runs independently:

```
standalone_job_monitor.py (PID 25672, pythonw.exe)
Triggered by: Windows Scheduled Task "Moldflow Mobile Job Monitor"
Working directory: C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin
      │
      │  Every 5.0 seconds:
      │
      ├── mobile_reporter.list_active_jobs()
      │       └── GET /internal/active-jobs  (X-Api-Key)
      │               └── FastAPI Backend
      │
      └── For each active job:
              ├── compute_jobs.find_by_scm_id(scm_job_id)
              │       └── GET http://127.0.0.1:44100/ComputeQueue/v1/jobs/{scm_job_id}
              │               └── SCM
              │
              └── mobile_reporter.report_status(...)
                      └── POST /reportJobStatus  → FastAPI → FCM → Android
```

## Component Responsibilities

| Component | File(s) | Responsibility |
|-----------|---------|----------------|
| COM Startup | `run_startup.vbs` | Initializes plugin process, spawns Python workers |
| Observer | `moldflow_observer.py` | Synergy COM event dispatch (document open/close etc.) |
| Startup Logic | `moldflow_startup.py` | Plugin initialization, study detection |
| Analysis Engine | `cad_diagnostics.py` | Triggers analysis, polls SCM, reports mobile status |
| SCM Client | `compute_jobs.py` | REST client for SCM queue API |
| Mobile Reporter | `mobile_reporter.py` | HTTP client for backend `/reportJobStatus` |
| Standalone Monitor | `standalone_job_monitor.py` | Background daemon, polls SCM + reports |
| UI Backend | `ui_bridge.py` | WebSocket/IPC bridge between MFC panel and Python |
| UI Frontend | `embedded_ui.py` | Embedded HTML UI controller |
| Backend API | `app_postgres_ready.py` | FastAPI application, business logic, FCM dispatch |
| FCM Service | `fcm_service.py` | Firebase Cloud Messaging HTTP v1 sender (local) |
| FCM Cloud | `fcm_service_cloud_ready.py` | FCM sender for cloud environments (env var creds) |
| Mobile App | `MoldflowMobile/` | .NET MAUI Android client |

## Known Failure Modes

### Analysis Started Notification Missing

**Root Cause A — Device Registration Timing Race:**
The backend permanently records `(job_id, 'STARTED')` on the first `INPROGRESS`
report. If no mobile device is registered at that exact moment, the notification
is suppressed forever (dedup table blocks all subsequent retries).

**Root Cause B — Fast Analysis / Transient INPROGRESS:**
SCM transitions from `QUEUED` to `COMPLETED` before `cad_diagnostics.py` captures
`INPROGRESS` in its 1.5-4.0s polling cycle.

**Root Cause C — Cloud FCM Credentials Path:**
`fcm_service.py` uses a local Windows file path for `GOOGLE_APPLICATION_CREDENTIALS`.
On Render (Linux container), the local path does not exist. `fcm_service_cloud_ready.py`
solves this but is not yet imported in the active `app_postgres_ready.py`.

**Root Cause D — Foreground Notification Text Bug:**
`FirebaseService.cs` line 122: hardcodes `(100%)` for all foreground notifications,
causing `STARTED` events to display `Status: INPROGRESS (100%)`.

These are documented for Phase 3 remediation.
