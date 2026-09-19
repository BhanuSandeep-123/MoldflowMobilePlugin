# API Contract

Documents the REST API implemented in `backend/app_postgres_ready.py`.

## System Endpoints

### GET /health
Returns server health status and active database engine.

**Authentication:** None

**Response Body:**
```json
{
  "status": "healthy",
  "database": "sqlite"
}
```

---

## Authentication

### Plugin / Workstation
All plugin and monitor endpoints use:
```
X-Api-Key: <api_key from mobile_report_config.json>
```
The key is validated against the `api_clients` table joined with `users` and `machines`.

### Mobile Client
Mobile clients authenticate via JWT Bearer token:
```
Authorization: Bearer <jwt>
```
JWT is obtained from `POST /auth/login`.

---

## Workstation Endpoints (Plugin & Standalone Monitor)

### POST /reportJobStatus
Reports current simulation status from the workstation.

**Authentication:** `X-Api-Key`

**Request Body:**
```json
{
  "job_id":         "string (unique GUID/identifier per analysis)",
  "name":           "string (study name, e.g. 'unoteam_study.sdy')",
  "type":           "string (analysis sequence, e.g. 'Fill+Pack+Warp')",
  "status":         "string ('QUEUED' | 'INPROGRESS' | 'COMPLETED' | 'FAILED' | 'CANCELED')",
  "percent":        "number (0-100)",
  "started":        "number | null (epoch timestamp)",
  "finished":       "boolean",
  "error_message":  "string | null",
  "scm_job_id":     "string | null (Autodesk SCM job ID)",
  "scm_type":       "string | null",
  "compute_source": "string | null ('LOCAL' | 'CLOUD')",
  "scm_user":       "string | null",
  "worker":         "string | null",
  "parent_job_id":  "string | null"
}
```

**Response Body:**
```json
{
  "accepted":          true,
  "job_id":            "string",
  "status":            "string",
  "received_at":       "string (ISO 8601 UTC timestamp)",
  "user_id":           "string",
  "machine_id":        "string",
  "cancel_requested":  false
}
```

If `cancel_requested` is `true`, the plugin/monitor marks the job canceled locally and aborts polling.

### GET /jobs/{job_id}/cancel-status
Fast-path polling endpoint used by the plugin to check for remote mobile cancellation within <=5 seconds.

**Authentication:** `X-Api-Key`

**Response Body:**
```json
{
  "job_id": "string",
  "cancel_requested": false
}
```

### GET /internal/active-jobs
Returns non-terminal active jobs for this machine. Used exclusively by `standalone_job_monitor.py`.

**Authentication:** `X-Api-Key`

**Response Body:**
```json
[
  {
    "job_id": "string",
    "name": "string",
    "job_type": "string",
    "status": "INPROGRESS",
    "percent": 45,
    "started": 1757400000.0,
    "finished": false,
    "scm_job_id": "string",
    "scm_type": "string",
    "compute_source": "LOCAL",
    "scm_user": "string",
    "worker": "string",
    "parent_job_id": null,
    "cancel_requested": false
  }
]
```

---

## Mobile Endpoints

### POST /auth/login
Authenticates mobile user and issues JWT.
```json
Request:  { "email": "string", "password": "string" }
Response: { "access_token": "string", "token_type": "bearer" }
```

### GET /me
Returns authenticated user profile.

### GET /jobs
Returns jobs list for the authenticated user.

### GET /jobs/{job_id}
Returns full details for a single job.

### GET /jobs/{job_id}/events
Returns status change audit trail for a job.

### POST /jobs/{job_id}/cancel
Sets `cancel_requested = TRUE` for cooperative cancellation.

### GET /devices
Returns list of registered mobile devices for the authenticated user.

**Authentication:** `Authorization: Bearer <jwt>`

**Response Body:**
```json
[
  {
    "device_id": "string",
    "platform": "android",
    "push_token": "string",
    "updated_at": "string (ISO 8601 UTC timestamp)"
  }
]
```

### POST /devices/register
Registers or updates a mobile device FCM push token.

**Catch-up Semantics:**
Upon successful device registration, the backend checks for active `INPROGRESS` jobs owned by this user where the `STARTED` notification has not yet been delivered (e.g. analysis started before device registered or logged in). If found, the backend dispatches the `STARTED` notification immediately to this device. Completed, failed, or already-notified jobs are ignored.

```json
Request:
{
  "device_id":   "string (unique hardware/idiom ID)",
  "platform":    "android",
  "push_token":  "string (FCM registration token)"
}
Response:
{
  "registered": true,
  "device_id":  "string",
  "user_id":    "string",
  "platform":   "android"
}
```
