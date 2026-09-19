# agents/post_analyze — Standalone Post-Analyze Agent

**Canonical location** (Phase 7+):
`MoldflowMobileSystem\agents\post_analyze\`

This agent is intentionally **separate** from the existing Moldflow plugin and
canonical monitor. It runs as a separate Windows Scheduled Task
(`Moldflow Post-Analyze Agent`) and does not modify plugin or monitor behavior.

---

## Workflow

1. User opens Moldflow Synergy normally.
2. User manually creates the project/study and performs all setup.
3. User manually clicks **Analyze**.
4. Simulation Compute Manager (SCM) receives the solve.
5. This agent detects the newly-created **parent** solve job.
6. The agent sends `STARTED` to the mobile backend.
7. The agent sends `INSPECTION` and runs the non-destructive mesh inspection
   via `cad_diagnostics.py` (Synergy COM).
8. The agent monitors the solve through SCM and reports `INPROGRESS` / progress.
9. The agent reports `COMPLETED` or `FAILED` when the solve reaches a terminal state.

---

## Existing Code Reuse

The agent reuses, **by import path via `existing_plugin_path`**, the following
production modules from the canonical plugin:

- `compute_jobs.py` (shim → `lib/scm/client.py`)
- `mobile_reporter.py` (shim → `lib/mobile/reporter.py`)
- `synergy_connect.py` (Synergy COM session)
- `cad_diagnostics.py` (mesh inspection function only)

No copy of these modules is included in this directory.

---

## Files

| File | Purpose | Committed |
|---|---|---|
| `agent.py` | Main agent loop | ✅ |
| `job_detector.py` | SCM delta detection | ✅ |
| `inspection_engine.py` | Synergy COM mesh inspection | ✅ |
| `__init__.py` | Python package marker | ✅ |
| `config.json.example` | Config template (no secrets) | ✅ |
| `run_agent.bat` | Manual foreground launcher | ✅ |
| `README.md` | This file | ✅ |
| `config.json` | Live config (`.gitignore`'d) | ❌ |
| `standalone_agent.log` | Runtime log (`.gitignore`'d) | ❌ |
| `standalone_error.log` | Runtime error log (`.gitignore`'d) | ❌ |
| `inspection_results/` | Generated inspection data (`.gitignore`'d) | ❌ |

---

## Runtime Environment

**Python executable** (Scheduled Task):
```
C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\pythonw.exe
```

**Working directory** (Scheduled Task):
```
C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\agents\post_analyze
```

The agent uses the **canonical plugin virtual environment** which includes
`pywin32` / `pythoncom` required for Synergy COM inspection.

---

## Scheduled Task

```
Task name:    \Moldflow Post-Analyze Agent
Executable:   plugin\.venv\Scripts\pythonw.exe
Arguments:    agent.py
WorkingDir:   agents\post_analyze\
Trigger:      At log on / On-demand
Instances:    IgnoreNew (prevents duplicate processes)
```

---

## Configuration (config.json)

Copy `config.json.example` to `config.json` and update paths:

```json
{
    "existing_plugin_path": "C:\\Users\\UnoTEAM-0144\\Documents\\MoldflowMobileSystem\\plugin",
    "poll_interval_seconds": 3.0,
    "startup_baseline": true,
    "inspection_enabled": true,
    "inspection_status": "INSPECTION",
    "inspection_timeout_seconds": 45,
    "only_local_jobs": true,
    "log_file": "standalone_agent.log",
    "analysis_started_status": "STARTED"
}
```

`config.json` is `.gitignore`'d — it contains machine-specific paths.
Never commit `config.json` with production paths or credentials.

---

## Manual Foreground Launch (Diagnostics)

```bat
run_agent.bat
```

Or directly:
```powershell
& "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\python.exe" agent.py
```

---

## Rollback Reference

The original standalone project is preserved at:
```
C:\Users\UnoTEAM-0144\Documents\MoldflowStandaloneAgent_PostAnalyze_v1_4\
```
Do NOT delete it until the canonical location has been live-validated across
multiple production Moldflow solves.

---

## Testing

From the canonical repository root:

```powershell
& "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py"
```

Phase 7 tests for this agent: `tests/test_phase7_agent.py`
