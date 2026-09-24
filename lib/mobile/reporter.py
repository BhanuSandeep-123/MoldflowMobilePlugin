# lib/mobile/reporter.py
# -----------------------
# Canonical mobile reporting library.
#
# This is the ONE authoritative implementation of mobile status reporting.
# All consumers (plugin, monitor, tests) import from here.
#
# Extracted from plugin/mobile_reporter.py during Phase 3.
# plugin/mobile_reporter.py is now a thin backward-compatible shim.
#
# Config path resolution: reads mobile_report_config.json by walking up
# from __file__, or from MOLDFLOW_CONFIG env var. Falls back to beside
# __file__ if not found (preserves original behavior in plugin/ layout).
#
# Public API (unchanged):
#   enabled()                    -- True when reporting is configured and on
#   report_status(payload, log)  -- POST /reportJobStatus
#   check_cancel(job_id, log)    -- GET /jobs/{id}/cancel-status
#   list_active_jobs(log)        -- GET /internal/active-jobs
#
"""
mobile_reporter.py
-------------------
Best-effort push of the local Job Manager status (already read by
compute_jobs.py) to the mobile-notification backend, so the same
started/running/completed/failed status shown in the panel's live job card
also reaches the user's phone.

This module owns none of the polling -- cad_diagnostics.py's existing
job-watch loop (see refresh_job_card in run_automation_workflow) already
polls compute_jobs every few seconds for the in-panel card; this module is
called from that same tick and only actually sends a request when the
status or progress has meaningfully changed, so it adds no new timers and
no new load on Synergy or the local Compute queue.

Disabled by default. Until mobile_report_config.json exists with
"enabled": true and a real backend_url + api_key, every call here is a
silent no-op -- a machine that hasn't been set up for this feature behaves
exactly as it did before this module existed.
"""

import json
import time
import urllib.request
from pathlib import Path

def _find_config_path():
    r"""Resolve mobile_report_config.json robustly regardless of where this
    module is installed.

    Resolution order:
      1. MOLDFLOW_CONFIG environment variable (full path to .json file).
      2. %PROGRAMDATA%\MoldflowMobile\config.json (production workstation target).
      3. repo/plugin/mobile_report_config.json (when running from repository).
      4. Walk up from this file's directory, checking each level for
         plugin/mobile_report_config.json, mobile_report_config.json, or config.json.
      5. Fallback: beside this file (original behavior for plugin/ layout).
    """
    import os
    env = os.environ.get("MOLDFLOW_CONFIG")
    if env:
        return Path(env)

    # 2. Production system-wide configuration
    prog_data = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE")
    if prog_data:
        p = Path(prog_data) / "MoldflowMobile" / "config.json"
        if p.exists():
            return p

    here = Path(__file__).resolve().parent
    candidates = []
    if len(here.parents) >= 2:
        repo_root = here.parents[1]
        candidates.append(repo_root / "plugin" / "mobile_report_config.json")
        candidates.append(repo_root / "mobile_report_config.json")
        candidates.append(repo_root / "config" / "mobile_report_config.json")
        candidates.append(repo_root / "config" / "config.json")
    for directory in [here, here.parent, *here.parents]:
        candidates.append(directory / "plugin" / "mobile_report_config.json")
        candidates.append(directory / "mobile_report_config.json")
        candidates.append(directory / "config" / "mobile_report_config.json")
        candidates.append(directory / "config" / "config.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Final fallback: beside this file (original behavior for plugin/ layout)
    return here / "mobile_report_config.json"


CONFIG_PATH = _find_config_path()


# Same reasoning as compute_jobs.HTTP_TIMEOUT: this call sits inside the
# solver poll loop, so a hung request must not stall the wait for results.
# Slightly longer than the local-only call since this one leaves the
# machine and a real network round-trip is involved.
HTTP_TIMEOUT = 5.0

# Re-send at most this often for an unchanged status, so a stalled network
# doesn't turn into a retry storm and a long-running job still refreshes
# "last seen" on the backend periodically instead of going silent.
MIN_RESEND_INTERVAL = 60.0

_config = None
_config_loaded = False
_last_sent = {}          # job_id -> (status, percent_bucket, sent_at)


def _load_config():
    global _config, _config_loaded, CONFIG_PATH
    if _config_loaded:
        return _config
    _config_loaded = True
    _config = None
    try:
        CONFIG_PATH = _find_config_path()
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                _config = data
    except Exception:
        _config = None
    return _config


def enabled():
    """True only when a real backend has been configured and switched on."""
    cfg = _load_config()
    if not cfg or not cfg.get("enabled"):
        return False
    return bool(cfg.get("backend_url")) and bool(cfg.get("api_key"))


def _percent_bucket(percent):
    try:
        return int(percent) // 5 * 5
    except Exception:
        return 0


def _should_send(job_id, status, percent, finished):
    """Send on first sight of a job, on any status change, on a >=5%
    progress step, or after MIN_RESEND_INTERVAL of silence -- never on
    every 4s tick for an unchanged mid-solve percent."""
    bucket = _percent_bucket(percent)
    prev = _last_sent.get(job_id)
    if prev is None:
        return True
    prev_status, prev_bucket, prev_sent = prev
    if status != prev_status or bucket != prev_bucket:
        return True
    if finished:
        return status != prev_status
    return (time.time() - prev_sent) >= MIN_RESEND_INTERVAL


def report_status(payload, log=None):
    """Best-effort push of one job's status. `payload` is the dict built by
    the job-watch loop -- see the call site in cad_diagnostics.py for the
    exact fields. Never raises; `log` (if given) records only real attempts
    and failures, not the many no-op calls before a job_id is known.

    Returns True if the backend response signals that cancellation has been
    requested by the mobile user, False otherwise.
    """
    try:
        job_id = str(payload.get("job_id") or "")
        if not job_id or not enabled():
            return False
        status = str(payload.get("status") or "")
        percent = payload.get("percent") or 0
        finished = bool(payload.get("finished"))
        if not _should_send(job_id, status, percent, finished):
            return False

        cfg = _load_config()
        body = dict(payload)
        body["user_id"] = cfg.get("user_id") or ""
        body["machine_id"] = cfg.get("machine_id") or ""
        body["api_key"] = cfg.get("api_key") or ""

        url = str(cfg.get("backend_url")).rstrip("/") + "/reportJobStatus"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Api-Key": cfg.get("api_key") or ""})

        resp_data = {}
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
            if ok:
                try:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    resp_data = {}

        if ok:
            _last_sent[job_id] = (status, _percent_bucket(percent), time.time())
            cancel_requested = bool(resp_data.get("cancel_requested", False))
            if log:
                log("Mobile notify: sent '{0}' {1}% for job {2}{3}.".format(
                    status, percent, job_id,
                    " (cancel requested)" if cancel_requested else ""))
            return cancel_requested
        elif log:
            log("Mobile notify: backend rejected the update for job {0}.".format(
                job_id))
    except Exception as e:
        if log:
            log("Mobile notify: could not reach the backend ({0}).".format(e))
    return False


def check_cancel(job_id, log=None):
    """Fast-path query to /jobs/{job_id}/cancel-status using X-Api-Key.
    Returns True if mobile requested cancellation. Never raises.

    Called from the job-watch loop on each pass to guarantee <=5s
    cancellation propagation latency.
    """
    try:
        job_id = str(job_id or "")
        if not job_id or not enabled():
            return False
        cfg = _load_config()
        url = str(cfg.get("backend_url")).rstrip("/") + "/jobs/{0}/cancel-status".format(job_id)
        req = urllib.request.Request(
            url, method="GET",
            headers={"X-Api-Key": cfg.get("api_key") or ""})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if 200 <= getattr(resp, "status", 200) < 300:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("cancel_requested", False))
    except Exception as e:
        pass
    return False


def list_active_jobs(log=None):
    """GET /internal/active-jobs -- jobs on THIS machine (scoped by the
    same X-Api-Key identity as every other call in this module) that a
    standalone monitor should keep polling. Returns [] on any failure or
    when reporting is not configured; never raises.

    Used only by standalone_job_monitor.py -- cad_diagnostics.py's own
    solve-wait loop already knows its one job directly via job_watch and
    has no need to ask the backend what is active.
    """
    try:
        if not enabled():
            return []
        cfg = _load_config()
        url = str(cfg.get("backend_url")).rstrip("/") + "/internal/active-jobs"
        req = urllib.request.Request(
            url, method="GET",
            headers={"X-Api-Key": cfg.get("api_key") or ""})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if 200 <= getattr(resp, "status", 200) < 300:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, list) else []
    except Exception as e:
        if log:
            log("Mobile notify: could not list active jobs ({0}).".format(e))
    return []