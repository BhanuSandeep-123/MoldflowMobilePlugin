# Dependency Graph

Recorded 2026-09-09 during Phase 3 dependency analysis.

## Inter-Module Dependency Matrix (Before Phase 3 Extraction)

```
standalone_job_monitor  ->  compute_jobs, mobile_reporter   [PROBLEM: cross-directory import]
cad_diagnostics         ->  session_context, synergy_connect (+ runtime imports of compute_jobs/mobile_reporter)
synergy_connect         ->  session_context, ui_bridge
moldflow_startup        ->  session_context, synergy_connect, ui_bridge, ui_launcher
moldflow_observer       ->  assistant_panel, session_context, synergy_connect, ui_bridge
ui_bridge               ->  session_context
embedded_ui             ->  session_context, ui_bridge
assistant_live          ->  assistant_panel
assistant_panel         ->  assistant_live
ai_report_summary       ->  ai_assistant
ui_launcher             ->  session_context, ui_bridge

compute_jobs            ->  (none)   [pure stdlib + HTTP]
mobile_reporter         ->  (none)   [pure stdlib + HTTP]
session_context         ->  (none)
ai_assistant            ->  (none)
report_style            ->  (none)
```

## Module Catalog

### `compute_jobs.py` (14,767 bytes)
- **Local imports:** none
- **Stdlib:** json, os, pathlib, subprocess, urllib
- **Third-party:** none
- **Uses:** SCM HTTP API (port 44100, /ComputeQueue/v1/*)
- **Global state:** `_base_url`, `_probed` (SCM URL cache, never cached on failure)
- **Extractable to `lib/scm/client.py`:** YES

### `mobile_reporter.py` (7,642 bytes)
- **Local imports:** none
- **Stdlib:** json, pathlib, time, urllib
- **Third-party:** none
- **Uses:** Backend HTTP (/reportJobStatus, /internal/active-jobs, /jobs/*/cancel-status)
- **Config:** reads `mobile_report_config.json` via `Path(__file__).with_name(...)`
- **Global state:** `_config`, `_config_loaded`, `_last_sent` (config + throttle cache)
- **Extractable to `lib/mobile/reporter.py`:** YES (with config path fix)

### `standalone_job_monitor.py` (12,234 bytes)
- **Local imports:** `compute_jobs`, `mobile_reporter`
- **Import path:** `sys.path.insert(0, Path(__file__).parent)` — inserts own directory
- **Problem:** When run from `monitor/`, `compute_jobs` and `mobile_reporter` are not found
- **Fix (Phase 3g):** Change path insert to repo root; import from `lib.scm.client` and `lib.mobile.reporter`

### `cad_diagnostics.py` (601,139 bytes)
- **Local imports:** `session_context`, `synergy_connect` (static); `compute_jobs`, `mobile_reporter` (runtime, via `importlib` or direct import inside functions)
- **Uses:** COM (win32com, pythoncom), Synergy study API, `AnalyzeNow()`, SCM HTTP
- **Status:** NOT extracted — too large, COM-coupled, requires live Synergy testing

### `synergy_connect.py` (12,580 bytes)
- **Local imports:** `session_context`, `ui_bridge`
- **Uses:** win32com.client, pythoncom, SAInstance COM moniker
- **Status:** NOT extracted — Windows COM specific

### `moldflow_startup.py` (14,062 bytes)
- **Local imports:** `session_context`, `synergy_connect`, `ui_bridge`, `ui_launcher`
- **Status:** NOT extracted — Synergy startup lifecycle

### `moldflow_observer.py` (20,535 bytes)
- **Local imports:** `assistant_panel`, `session_context`, `synergy_connect`, `ui_bridge`
- **Uses:** COM event dispatch, StudyDoc events
- **Status:** NOT extracted — COM event coupled

### `ui_bridge.py` (39,756 bytes)
- **Local imports:** `session_context`
- **Uses:** Windows MFC IPC, JSON state files
- **Status:** NOT extracted — Windows-specific IPC

### `embedded_ui.py` (109,097 bytes)
- **Local imports:** `session_context`, `ui_bridge`
- **Uses:** win32gui, win32ui, PIL/Pillow, tkinter, MFC dockable panel
- **Status:** NOT extracted — Windows MFC/GUI specific

### `session_context.py` (11,131 bytes)
- **Local imports:** none
- **Uses:** SAInstance env var, ctypes, COM path detection
- **Status:** NOT extracted — Synergy session specific

### `assistant_live.py` (26,986 bytes)
- **Local imports:** `assistant_panel`
- **Uses:** winreg, browser CDP, env vars
- **Status:** NOT extracted — Windows + browser specific

### `assistant_panel.py` (29,966 bytes)
- **Local imports:** `assistant_live`
- **Uses:** comtypes, COM (CreateObject), subprocess
- **Status:** NOT extracted — COM specific

### `ai_assistant.py` (7,851 bytes)
- **Local imports:** none
- **Uses:** subprocess (external AI CLI), env vars
- **Status:** NOT extracted — subprocess launcher

### `ai_report_summary.py` (24,806 bytes)
- **Local imports:** `ai_assistant`
- **Status:** NOT extracted — wraps ai_assistant

### `ui_launcher.py` (5,207 bytes)
- **Local imports:** `session_context`, `ui_bridge`
- **Uses:** subprocess, Popen
- **Status:** NOT extracted — session + process management

### `report_style.py` (27,404 bytes)
- **Local imports:** none
- **Uses:** python-pptx
- **Status:** NOT extracted — report generation, Phase 3 scope does not include

## Phase 3 Extraction Target

```
lib/
  scm/
    __init__.py
    client.py       <- compute_jobs.py (canonical)
  mobile/
    __init__.py
    reporter.py     <- mobile_reporter.py (canonical, config path fixed)
  jobs/
    __init__.py
    models.py       <- JobStatus dataclass (new, docs only in Phase 3)
  __init__.py
```

## After Extraction

```
standalone_job_monitor  ->  lib.scm.client, lib.mobile.reporter   [CLEAN]
plugin/compute_jobs     ->  lib.scm.client  (shim, backward compatible)
plugin/mobile_reporter  ->  lib.mobile.reporter  (shim, backward compatible)
cad_diagnostics         ->  plugin/compute_jobs, plugin/mobile_reporter  (unchanged via shim)
```

## SCM API Reference

Base URL: `http://localhost:44100/ComputeQueue/v1`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/version` | GET | Availability probe |
| `/jobs` | GET | Full job history list |
| `/jobs/{id}` | GET | Single job polling |
| `/jobs/{id}/cancel` | POST | Cancel (returns 501 Not Implemented) |

**Status vocabulary:** QUEUED, SCHEDULED, STARTING, INPROGRESS, COMPLETED, FAILED, CANCELED, TIMEDOUT

## Backend API Reference (mobile_reporter)

Base URL: from `mobile_report_config.json > backend_url`

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/reportJobStatus` | POST | X-Api-Key | Report job status |
| `/internal/active-jobs` | GET | X-Api-Key | List active jobs for this machine |
| `/jobs/{id}/cancel-status` | GET | X-Api-Key | Poll cancel flag |


---

## Post-Extraction Architecture (After Phase 3)

Following Phase 3 extraction, shared SCM interaction and mobile status
reporting have been extracted into canonical packages under `lib/`:

```
lib/
├── scm/
│   ├── __init__.py
│   └── client.py          <- Canonical SCM client (extracted from compute_jobs.py)
├── mobile/
│   ├── __init__.py
│   └── reporter.py        <- Canonical mobile reporter (extracted from mobile_reporter.py)
└── jobs/
    ├── __init__.py
    └── models.py          <- Domain model (JobStatus, JobPhase dataclasses)
```

### Decoupled Dependency Graph

```
monitor/standalone_job_monitor.py
  ├── imports: lib.scm.client
  └── imports: lib.mobile.reporter
  (Zero dependency on plugin/)

plugin/cad_diagnostics.py
  ├── imports: compute_jobs (shim -> lib.scm.client)
  └── imports: mobile_reporter (shim -> lib.mobile.reporter)
  (100% backward compatible without changing cad_diagnostics.py)

plugin/compute_jobs.py
  └── re-exports: lib.scm.client

plugin/mobile_reporter.py
  └── re-exports: lib.mobile.reporter
```
