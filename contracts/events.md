# Event & Notification Contract

Documents event tracking and push notification delivery.

## Database Audit Events (`job_events`)

Every `POST /reportJobStatus` appends an immutable record to `job_events`:

```sql
CREATE TABLE job_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    status        TEXT NOT NULL,
    percent       REAL NOT NULL,
    finished      INTEGER NOT NULL,
    error_message TEXT,
    received_at   TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
```

## Push Notifications (`job_notifications`)

Push notification lifecycle events are tracked in `job_notifications` to guarantee at-most-once delivery per lifecycle stage:

```sql
CREATE TABLE job_notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    sent_at           TEXT NOT NULL,
    UNIQUE(job_id, notification_type),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
```

## Lifecycle Notification vs Telemetry

| Concept | Purpose | Frequency | Carrier |
|---|---|---|---|
| **Lifecycle Notification** | Alert user of milestone events (`STARTED`, `COMPLETED`, `FAILED`, `CANCELED`) | Exactly once per stage | Push notification (FCM) & persistent alert |
| **Progress Telemetry** | Update percent gauge and progress bar | Every 5% bucket change or 60s heartbeat | REST poll / in-app refresh (`jobs` table) |

### INPROGRESS -> STARTED Semantic Mapping
When status `INPROGRESS` is first reported:
- It represents the solver entering the active compute phase.
- Surfaced to the mobile user as **"Moldflow Analysis Started"** (`notification_type: "STARTED"`).
- Deduplicated via `(job_id, 'STARTED')` in `job_notifications`.

## FCM Payload Specification (HTTP v1)

```json
{
  "message": {
    "token": "<device_push_token>",
    "notification": {
      "title": "Moldflow Analysis Started",
      "body": "StudyName has started running."
    },
    "android": {
      "priority": "HIGH",
      "notification": {
        "channel_id": "moldflow_jobs",
        "sound": "default",
        "default_sound": true,
        "default_vibrate_timings": true,
        "notification_priority": "PRIORITY_MAX",
        "visibility": "PUBLIC"
      }
    },
    "data": {
      "job_id": "string",
      "job_name": "string",
      "status": "INPROGRESS | COMPLETED | FAILED | CANCELED",
      "notification_type": "JOB_INPROGRESS | JOB_COMPLETED | JOB_FAILED | JOB_CANCELED",
      "percent": "0-100",
      "title": "string",
      "body": "string"
    }
  }
}
```
