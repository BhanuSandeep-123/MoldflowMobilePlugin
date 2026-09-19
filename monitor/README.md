# monitor/

Contains `standalone_job_monitor.py` - the background daemon that monitors
SCM jobs after Synergy closes.

## Architecture & Dependencies

As of Phase 3 architectural extraction, this module imports directly from
the shared `lib/` package:

- `lib.scm.client` (`compute_jobs`) - SCM REST API interaction (localhost:44100)
- `lib.mobile.reporter` (`mobile_reporter`) - Backend HTTP reporting (/reportJobStatus)

The monitor is **fully decoupled** from `plugin/` and independently runnable
from the repository root without `plugin/` on `sys.path`.

For backward compatibility with legacy standalone folder deployments,
it includes a fallback to local modules if `lib/` is not on `sys.path`.
