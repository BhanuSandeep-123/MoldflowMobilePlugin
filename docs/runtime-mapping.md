# Runtime Mapping

Documents the CURRENT LIVE configuration as of 2026-09-09.
Do NOT change these values in the live installation during Phase 2.

## Live Plugin Location

| Item | Value |
|------|-------|
| **Canonical Plugin Directory** | `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin` |
| **Moldflow Synergy Version** | 2027 |
| **Synergy Command File** | `C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands\run_startup.vbs` |
| **PLUGIN_DIR (hardcoded in VBS)** | `c:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin` |
| **VBS Hash (SHA-256)** | `2546FBD8E76FA85CE6648452027C6E79301E2AC51F9D0C7340CE66336BF7DE5D` |

## Active Processes (As of Inspection)

| PID | Process | Command |
|-----|---------|---------|
| 19504 | `synergy.exe` | `/script ...\run_startup.vbs` |
| 22192 | `wscript.exe` | `run_startup.vbs` |
| 25672 | `pythonw.exe` | `standalone_job_monitor.py` |
| 12944 | `python.exe` | Plugin `.venv\Scripts\python.exe` |

## Python Environment

| Item | Value |
|------|-------|
| **Plugin Runtime Python** | `.venv\Scripts\python.exe` (Python 3.14.0rc1) |
| **System Python** | `C:\Program Files\Python314\python.exe` |
| **Virtual Environment** | `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin\.venv` |
| **Python Alias (shell)** | `C:\Users\UnoTEAM-0144\AppData\Local\Microsoft\WindowsApps\python.exe` (stub, non-functional) |

## Windows Scheduled Task

| Item | Value |
|------|-------|
| **Task Name** | `Moldflow Mobile Job Monitor` |
| **Trigger** | User logon |
| **Action** | `C:\Program Files\Python314\pythonw.exe standalone_job_monitor.py` |
| **Working Directory** | `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin` |
| **Log File** | `monitor_diag.txt` in Working Directory |

## Backend Configuration

| Item | Value |
|------|-------|
| **Active Backend** | `https://moldflowplugin-mobile-app.onrender.com` (Render Cloud) |
| **Database** | Supabase PostgreSQL (`aws-0-ap-northeast-2.pooler.supabase.com`) |
| **Backend Source** | `C:\MF\MoldflowSynergyPlugin\mobile_backend\app_postgres_ready.py` |
| **Health Endpoint** | `GET https://moldflowplugin-mobile-app.onrender.com/health` (200 OK verified) |
| **Local PostgreSQL** | Running on port 5432 (but local FastAPI on 8001 is offline) |

## Plugin Configuration File

`plugin/mobile_report_config.json` on the workstation:
```json
{
  "enabled":      true,
  "backend_url":  "https://moldflowplugin-mobile-app.onrender.com",
  "api_key":      "<workstation api key>",
  "user_id":      "DEV-USER-001",
  "machine_id":   "DEV-PC-001"
}
```

## SCM Configuration

| Item | Value |
|------|-------|
| **Service Name** | `adskscm2` |
| **Queue API Port** | `44100` |
| **Solver Port** | `44200` |
| **Internal Port** | `44500` |
| **Queue API Base** | `http://127.0.0.1:44100/ComputeQueue/v1` |

## Stale / Historical Locations

| Location | Status |
|----------|--------|
| `C:\MF\MoldflowSynergyPlugin` (root plugin files) | Stale snapshot (Aug 24, 2026). NOT executed by Moldflow. |
| `C:\MF\MoldflowSynergyPlugin\mobile_backend` | Active backend source. Backend deployed to Render from here. |
| `C:\MF\MoldflowSynergyPlugin\MoldflowMobile` | Active mobile app source. |

## Phase 2 New Repository

| Item | Value |
|------|-------|
| **New Repository** | `C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem` |
| **Status** | Assembled. NOT yet the live runtime. |
| **Live Runtime Redirect** | Pending Phase 4 (after Phase 3 bug fixes are validated) |
