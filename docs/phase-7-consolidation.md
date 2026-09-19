# Phase 7 — Agent Consolidation

**Date:** 2026-09-09
**Status:** Complete (pending live Moldflow validation)

---

## Summary

The Standalone Post-Analyze Agent was consolidated from its separate deployment
project (`MoldflowStandaloneAgent_PostAnalyze_v1_4`) into the canonical
`MoldflowMobileSystem` repository under `agents/post_analyze/`.

The agent remains a **separate Windows Scheduled Task** and separate Python
process. Consolidation only changes the file location and git management.

---

## What Changed

### Canonical Repository

| Change | Detail |
|---|---|
| New directory | `agents/post_analyze/` added to canonical repo |
| Files consolidated | `agent.py`, `job_detector.py`, `inspection_engine.py`, `config.json.example`, `run_agent.bat`, `README.md`, `__init__.py` |
| Files NOT copied | `standalone_agent.log`, `standalone_error.log`, `inspection_results/`, `__pycache__/`, `config.json` (created fresh) |
| `.gitignore` | Added exclusions for `agents/post_analyze/config.json`, `*.log`, `inspection_results/`, `__pycache__/` |

### Scheduled Task Update

| Field | Before | After |
|---|---|---|
| Executable | `plugin\.venv\Scripts\pythonw.exe` | `plugin\.venv\Scripts\pythonw.exe` (unchanged) |
| Arguments | `agent.py` | `agent.py` (unchanged) |
| WorkingDirectory | `MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent` | `MoldflowMobileSystem\agents\post_analyze` |

### Tests

- `tests/test_phase7_agent.py` — New: 24 tests covering module imports, config, JobDetector, InspectionEngine, and deployment state.
- `tests/test_phase6_notifications.py` — Updated: `STANDALONE_DIR` now references canonical `agents/post_analyze/`.

---

## What Did NOT Change

- Plugin behavior (`plugin/cad_diagnostics.py`, `run_startup.vbs`)
- Backend behavior (`backend/app_postgres_ready.py`)
- Canonical monitor (`monitor/standalone_job_monitor.py`)
- Scheduled Task name (`Moldflow Post-Analyze Agent`)
- Scheduled Task executable and arguments
- Agent Python runtime (canonical plugin venv)
- Old standalone project (`MoldflowStandaloneAgent_PostAnalyze_v1_4/`) — preserved as rollback reference, untouched

---

## Cutover Steps Executed

1. ✅ Stop `Moldflow Post-Analyze Agent` Scheduled Task
2. ✅ Verify `Moldflow Mobile Job Monitor` unaffected (still Running, PID 7884)
3. ✅ Create `agents/post_analyze/` directory structure
4. ✅ Selective file copy (source only, no runtime artifacts)
5. ✅ Update `.gitignore`
6. ✅ Verify all imports pass from new location (all canonical venv imports OK)
7. ✅ Export old Scheduled Task XML (backup)
8. ✅ Update Scheduled Task `WorkingDirectory` to canonical path
9. ✅ Start `Moldflow Post-Analyze Agent` task → Running
10. ✅ Verify `standalone_agent.log` written at new location
11. ✅ Verify old location log not updated since cutover
12. ✅ Run full test suite

---

## Rollback Procedure

If the new location fails, restore in < 2 minutes:

```powershell
# 1. Stop task
Stop-ScheduledTask -TaskName "Moldflow Post-Analyze Agent"

# 2. Reset WorkingDirectory to old location
$task = Get-ScheduledTask -TaskName "Moldflow Post-Analyze Agent"
$action = $task.Actions[0]
$oldAction = New-ScheduledTaskAction `
    -Execute $action.Execute `
    -Argument $action.Arguments `
    -WorkingDirectory "C:\Users\UnoTEAM-0144\Documents\MoldflowStandaloneAgent_PostAnalyze_v1_4\MoldflowStandaloneAgent"
Set-ScheduledTask -TaskName "Moldflow Post-Analyze Agent" -Action $oldAction

# 3. Restart
Start-ScheduledTask -TaskName "Moldflow Post-Analyze Agent"
```

Backup XML: `scratch/live_backup/post_analyze_agent_task_before_phase7.xml`

---

## Pending: Live Moldflow Validation

Awaiting user authorization for one controlled Moldflow analysis solve to verify:
- STARTED notification on Android
- INSPECTION status (Synergy COM working)
- INPROGRESS progress updates
- COMPLETED notification on Android
- Android tap-routing to job detail screen
- No duplicate monitor/agent process
