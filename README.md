# MoldflowMobileSystem

Unified canonical source repository for the Autodesk Moldflow Synergy
automation system and mobile monitoring platform.

## Repository Structure

| Directory | Contents |
|-----------|----------|
| `lib/` | Shared core packages (`lib.scm`, `lib.mobile`, `lib.jobs`) |
| `plugin/` | Moldflow Synergy Python automation plugin (canonical from Documents) |
| `monitor/` | Standalone background SCM job monitor daemon |
| `backend/` | FastAPI mobile reporting backend (Python) |
| `mobile/` | .NET MAUI Android mobile application |
| `contracts/` | API and event contract documentation |
| `deployment/` | Deployment and infrastructure scripts |
| `tests/` | Integration and unit test suite |
| `docs/` | Architecture, dependency, and operational documentation |
| `tools/` | Developer tooling and utilities |

## Quick Reference

- **Live Moldflow Plugin (current):** `C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin`
- **Backend (live):** `https://moldflowplugin-mobile-app.onrender.com`
- **Architecture:** See `docs/current-architecture.md`
- **Runtime Mapping:** See `docs/runtime-mapping.md`

## Setup

### Plugin
See `plugin/README.md` and `plugin/setup.ps1`.

### Backend
```bash
cd backend
cp .env.example .env
# Edit .env with real credentials
pip install -r requirements.txt
uvicorn app_postgres_ready:app --host 0.0.0.0 --port 8001
```

### Mobile App
Open `mobile/MoldflowMobile.slnx` in Visual Studio 2022+.
See `mobile/PRODUCTION_RUNBOOK.md` for build and deployment.

## Phase Status

| Phase | Status | Description |
|-------|--------|-------------|
| 1     | COMPLETE | Forensic inspection and engineering analysis |
| 2     | COMPLETE | Repository assembly and baseline validation |
| 3     | COMPLETE | Architectural extraction (`lib/scm`, `lib/mobile`, `lib/jobs`, monitor decoupled) |
| 4     | PENDING  | Notification bug fix, consolidation, path migration |
