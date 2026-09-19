# Job State Contract

Documents the job state machine and lifecycle transitions.

## States

| State | Source | Meaning | Terminal? |
|---|---|---|---|
| `QUEUED` | SCM / Plugin | Job submitted to SCM queue, waiting for solver process | No |
| `INPROGRESS` | SCM / Plugin | Solver is actively running analysis | No |
| `COMPLETED` | SCM / Plugin | Analysis completed successfully | Yes |
| `FAILED` | SCM / Plugin | Analysis failed (solver error or exception) | Yes |
| `CANCELED` | Plugin / Mobile | Cooperative cancellation confirmed | Yes |

## Notification Mapping

| Incoming Status | Triggered Notification | Notification Type |
|---|---|---|
| `INPROGRESS` | "Moldflow Analysis Started" | `STARTED` |
| `COMPLETED` | "Moldflow Analysis Completed" | `COMPLETED` |
| `FAILED` | "Moldflow Analysis Failed" | `FAILED` |
| `CANCELED` | "Moldflow Analysis Cancelled" | `CANCELED` |

## Deduplication & Catch-up Rules

1. **At-Most-Once per Stage**: Each `(job_id, notification_type)` is recorded in `job_notifications` upon successful delivery. Subsequent arrivals of the same stage are discarded.
2. **Device Registration Catch-up**: If a job enters `INPROGRESS` while no device is registered, `job_notifications` is NOT marked sent. When the device subsequently registers via `POST /devices/register`, the backend immediately dispatches the missing `STARTED` notification.
3. **No Stale Started Alerts**: Completed, failed, or canceled jobs are never candidates for catch-up `STARTED` notifications.
4. **Terminal State Lock**: Once a job enters a terminal state (`COMPLETED`, `FAILED`, `CANCELED`), its status in `jobs` is frozen to prevent delayed or out-of-order polling requests from reverting the record.
