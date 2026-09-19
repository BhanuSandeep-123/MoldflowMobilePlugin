# lib/scm/client.py
# ------------------
# Canonical SCM (Simulation Compute Manager) client library.
#
# This is the ONE authoritative implementation of SCM interaction.
# All consumers (plugin, monitor, tests) import from here.
#
# Extracted from plugin/compute_jobs.py during Phase 3.
# plugin/compute_jobs.py is now a thin backward-compatible shim.
#
# Public API (unchanged from original):
#   base_url()          -- probe + cache SCM base URL
#   available()         -- True when SCM is reachable
#   list_jobs()         -- full job history from SCM
#   get_job(job_id)     -- single job poll
#   cancel_job(...)     -- cancel active job via DELETE /jobs/{id}
#   find_job(...)       -- discover a newly queued job by name + time
#   job_ids(study_name) -- snapshot of existing job IDs before a new solve
#   summarize(job)      -- raw SCM job dict -> normalized flat dict
#   phases(job)         -- per-phase children list
#   viewer_exe()        -- path to ComputeBrowser.exe
#   open_viewer()       -- launch the Simulation Job Viewer
#
# Behavior is IDENTICAL to the original. Do NOT change logic here without
# understanding and testing the downstream callers in cad_diagnostics.py.
#
"""
compute_jobs.py
---------------
Read-only client for the LOCAL Autodesk Simulation Compute Manager queue.

That queue is what sits behind Synergy's "Job Manager" / "Simulation Job
Viewer" window: a separate product (Simulation Compute Manager 2) whose UI is
a React app served by SimulationCompute.exe on localhost. The window itself is
just an Electron shell (ComputeBrowser.exe) pointed at

    http://localhost:44100/ComputeQueue/v1/

None of that is reachable through the Synergy COM API -- there is no job or
queue object in synapi at all. What IS reachable is the service's own REST
API, unauthenticated, on localhost, which is what this module reads so the
docked panel can show the same live job progress in its own card instead of
opening a second 188MB Chromium window on top of Synergy.

Endpoints used (verified against SCM 2.8.1):
    GET    /ComputeQueue/v1/version      -> {"version": "2.8.1"}         (probe)
    GET    /ComputeQueue/v1/jobs         -> [ job, ... ]   full history, ~500KB
    GET    /ComputeQueue/v1/jobs/<id>    -> job            ~1.4KB
    DELETE /ComputeQueue/v1/jobs/<id>    -> cancel active SCM job

A job carries everything the card needs:
    status              QUEUED | PENDING | RUNNING | COMPLETED | FAILED | CANCELED
    progress.percent    int
    payload.name        "unoteam_study~42.sdy"
    payload.type        "mesh" | "study" | "mesh+study"  (child phases append
                        ":<phase>:<nn>", e.g. "study:warp3d:01")
    epoch               job creation time, ms since epoch, as a STRING

EVERY function here is best-effort and returns None/[] rather than raising.
The queue service is a separate product that may be stopped, upgraded or
absent, and the workflow must never fail because a progress card could not be
drawn. This API is also undocumented and version-bound, so every field is read
defensively -- treat all of them as optional.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The queue port is configuration, not a constant: it comes from the "queue"
# service block of ComputeService.config.json. 44100 is only the shipped
# default, so it is the LAST resort rather than the assumption.
DEFAULT_PORT = 44100
BASE_PATH = "/ComputeQueue/v1"

# Localhost JSON over a loopback socket -- a few seconds is already generous.
# Kept short on purpose: this runs inside the solver's poll loop, and a hung
# read here would stall the very loop that is watching for results.
HTTP_TIMEOUT = 4.0

# A PARENT job has a bare type; its per-phase children repeat the parent's
# study name with ":<phase>:<nn>" appended ("study:mhb3d:00", "study:warp3d:01")
# and carry a "parent" id. Both kinds appear side by side in the flat /jobs
# list, so the type must be matched EXACTLY -- matching only the part before
# the first ':' selects phase rows as if they were jobs, and since a child is
# stamped a few seconds BEFORE its parent, that is a real risk of reporting one
# phase's progress as the whole solve.
SOLVE_TYPES = ("study", "mesh+study")
MESH_TYPES = ("mesh", "mesh+study")

# The queue's full status vocabulary, from the frozen enum in the Job Viewer
# bundle: PRECREATED, CREATED, QUEUED, SCHEDULED, INPROGRESS, COMPLETED,
# CANCELED, FAILED, TIMEDOUT. Note there is NO "RUNNING" and no "PENDING" --
# a live solve reports INPROGRESS.
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELED", "TIMEDOUT")

_base_url = None          # resolved lazily, then cached for the process
_probed = False
_recently_canceled = {}   # scm_job_id -> timestamp (prevents duplicate cancel commands)


def _scm_bin_dirs():
    """Candidate Simulation Compute Manager bin directories, newest first."""
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            roots.append(Path(root) / "Autodesk")
    for root in roots:
        try:
            # The install is versioned ("Simulation Compute Manager 2"), so it
            # is globbed rather than named -- a future SCM 3 must not silently
            # fall back to the default port.
            for d in sorted(root.glob("Simulation Compute Manager*"),
                            reverse=True):
                yield d / "bin"
        except Exception:
            continue


def _config_paths():
    """Candidate ComputeService.config.json locations, most likely first."""
    for bin_dir in _scm_bin_dirs():
        yield bin_dir / "ComputeService.config.json"


def _configured_port():
    """Port from the queue service block, or the shipped default."""
    for cfg in _config_paths():
        try:
            if not cfg.exists():
                continue
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            for svc in data.get("services") or []:
                if str(svc.get("name") or "").lower() != "queue":
                    continue
                for param in svc.get("parameters") or []:
                    if str(param.get("key") or "").lower() == "port":
                        port = int(str(param.get("value") or "").strip())
                        if port > 0:
                            return port
        except Exception:
            continue
    return DEFAULT_PORT


def _get(url):
    """Parsed JSON from `url`, or None. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:
        return None


def base_url(refresh=False):
    """Root URL of the local queue API, or None when the service is not up.

    Cached only once SCM is confirmed reachable: the answer is then stable
    for the life of a workflow run, and re-probing inside the solver poll
    loop would put a doomed socket connect between every result check on a
    machine without SCM. A FAILED probe is deliberately NOT cached -- an
    unattended/background caller (e.g. standalone_job_monitor.py, which may
    start at Windows logon before the SCM service has finished coming up)
    must keep retrying, not get permanently stuck reporting "unavailable"
    for the rest of the process's life once SCM does come up."""
    global _base_url, _probed
    if _probed and not refresh:
        return _base_url
    _base_url = None
    port = _configured_port()
    # The configured port first, then the default -- a config naming a port the
    # service is not actually on should not mask a working default.
    for candidate in (port, DEFAULT_PORT):
        url = "http://localhost:{0}{1}".format(candidate, BASE_PATH)
        if _get(url + "/version"):
            _base_url = url
            break
    _probed = _base_url is not None
    return _base_url


def available():
    """True when the queue service answered its /version probe."""
    return base_url() is not None


def list_jobs():
    """Every job the local queue knows about. Returns [] when unavailable.

    This is the full history -- 200+ jobs / ~500KB on a machine that has been
    in use for a while. Call it to FIND a job; poll get_job() afterwards."""
    root = base_url()
    if not root:
        return []
    jobs = _get(root + "/jobs")
    return jobs if isinstance(jobs, list) else []


def _workstation_user():
    """Return the current workstation user (dynamic fallback when SCM user is unavailable)."""
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            import getpass
            user = getpass.getuser()
        return str(user or "").strip()
    except Exception:
        return ""


def get_job(job_id):
    """One job by id, or None. ~1.4KB -- this is the polling call.
    
    If SCM is reachable but returns 404 or 500 ('Failed to locate job'),
    indicates the job was deleted/canceled from SCM and returns a synthetic
    record with status='CANCELED' and _scm_deleted=True.
    """
    root = base_url()
    if not root or not job_id:
        return None
    url = "{0}/jobs/{1}".format(root, urllib.parse.quote(str(job_id), safe=""))
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as err:
        body = ""
        try:
            body = err.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = str(getattr(err, "msg", "") or "")
        reason = str(getattr(err, "reason", "") or "")
        err_text = (body + " " + msg + " " + reason + " " + str(err)).lower()
        if err.code == 404 or (err.code == 500 and "failed to locate job" in err_text):
            return {"jobID": job_id, "status": "CANCELED", "_scm_deleted": True}
        return None
    except Exception:
        return None


def cancel_job(scm_job_id, timeout=HTTP_TIMEOUT, force=False):
    """Cancel an active SCM job via DELETE /ComputeQueue/v1/jobs/{scm_job_id}.

    Parameters:
        scm_job_id (str): The Autodesk SCM job ID (UUID). Must be non-empty.
        timeout (float): Request timeout in seconds (default HTTP_TIMEOUT).
        force (bool): If True, bypasses the recent cancellation cache.

    Returns:
        bool:
          - True if SCM responded with HTTP 200 (cancellation accepted/success).
          - True if the job was already cancelled recently in this process.
          - True if SCM returned HTTP 404, but query of the job verifies it is
            already in a terminal state (COMPLETED, CANCELED, FAILED, TIMEDOUT).
          - False if scm_job_id is invalid/empty, SCM is unavailable, SCM returns
            an error (400, 401, 403, 500, etc.), SCM returns 404 for an unknown
            job that cannot be reconciled, or on timeout/network failure.
    """
    if not scm_job_id or not isinstance(scm_job_id, str):
        return False
    scm_job_id = scm_job_id.strip()
    if not scm_job_id:
        return False

    now = time.time()
    if not force and scm_job_id in _recently_canceled:
        if now - _recently_canceled[scm_job_id] < 60.0:
            return True

    root = base_url()
    if not root:
        return False

    url = "{0}/jobs/{1}".format(root, urllib.parse.quote(scm_job_id, safe=""))
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("User-Agent", "MoldflowMobileSystem/1.0")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", resp.getcode() if hasattr(resp, "getcode") else 200)
            if 200 <= code < 300:
                _recently_canceled[scm_job_id] = now
                return True
            return False
    except urllib.error.HTTPError as err:
        if err.code == 404:
            # Validate whether the job is already terminal in SCM
            try:
                job = get_job(scm_job_id)
                if isinstance(job, dict):
                    status = str(job.get("status") or "").strip().upper()
                    if status in TERMINAL_STATUSES:
                        _recently_canceled[scm_job_id] = now
                        return True
            except Exception:
                pass
            return False
        return False
    except (urllib.error.URLError, TimeoutError, socket.timeout, Exception):
        return False


def _stem(name):
    """"unoteam_study~42.sdy" -> "unoteam_study~42", lowercased."""
    text = str(name or "").strip().lower()
    if text.endswith(".sdy"):
        text = text[:-4]
    return text


def _epoch_ms(job):
    try:
        return int(str(job.get("epoch") or "0"))
    except Exception:
        return 0


def _job_type(job):
    """The job's type EXACTLY as reported, lowercased. Parent jobs have a bare
    type ("study"); phase children keep their ":<phase>:<nn>" suffix, which is
    what keeps the two apart."""
    payload = job.get("payload") or {}
    return str(payload.get("type") or "").strip().lower()


def job_ids(study_name=None):
    """Ids of the jobs the queue currently knows, optionally for one study.

    Used to snapshot what already exists BEFORE a solve is queued, so the
    lookup afterwards cannot latch onto one of them. See `exclude_ids` in
    `find_job`."""
    wanted = _stem(study_name) if study_name else None
    out = set()
    for job in list_jobs():
        try:
            if wanted:
                payload = job.get("payload") or {}
                if _stem(payload.get("name")) != wanted:
                    continue
            jid = str(job.get("jobID") or "")
            if jid:
                out.add(jid)
        except Exception:
            continue
    return out


def find_job(study_name, since, types=SOLVE_TYPES, exclude_ids=None):
    """The job this run just queued, or None.

    `study_name` is the study file name (with or without .sdy) and `since` is a
    time.time() value read just before the job was started. Both filters are
    needed: the queue keeps every job ever run, so the name alone matches all of
    this study's previous runs, and the time alone matches whatever else the
    machine happens to be solving.

    `exclude_ids` is a set of job ids known to predate this solve. Name+time
    alone is NOT enough once the workflow runs a Gate Location analysis before
    the main solve: that is also a "study"-type job on the SAME study name,
    queued only a minute or two earlier, so it fell inside the 60s slack below
    and the card tracked it instead -- reporting "Canceled 100%, phases: gate"
    while the Fill was running perfectly well (live 2026-08-21).

    Returns the NEWEST match, so re-running the same study picks up this run
    rather than the first one."""
    wanted = _stem(study_name)
    if not wanted:
        return None
    exclude_ids = exclude_ids or set()
    # A minute of slack: `since` is read in this process just before AnalyzeNow
    # hands off, and the job is stamped by the service. It is the same clock,
    # but the ordering between the two is not worth trusting to the millisecond.
    floor = int((float(since) - 60.0) * 1000)
    best = None
    for job in list_jobs():
        try:
            payload = job.get("payload") or {}
            if _stem(payload.get("name")) != wanted:
                continue
            if str(job.get("jobID") or "") in exclude_ids:
                continue
            if _epoch_ms(job) < floor:
                continue
            if types and _job_type(job) not in types:
                continue
            if best is None or _epoch_ms(job) > _epoch_ms(best):
                best = job
        except Exception:
            continue
    return best


def summarize(job):
    """Job -> the flat fields the panel card renders. None for a None job.

    Everything is read defensively and every value has a usable fallback: a
    field this SCM version does not supply must leave a gap in the card, not
    break the stage the card is reporting on."""
    if not isinstance(job, dict):
        return None
    payload = job.get("payload") or {}
    progress = job.get("progress")
    if isinstance(progress, dict):
        try:
            percent = int(progress.get("percent") or 0)
        except Exception:
            percent = 0
        details = progress.get("details") or {}
    else:
        try:
            percent = int(progress or 0)
        except Exception:
            percent = 0
        details = {}

    if not isinstance(details, dict):
        details = {}

    status = str(job.get("status") or "").strip().upper() or "QUEUED"

    # Reconcile multi-phase child stages (e.g. mesh+study) where a child phase
    # was cancelled in SCM Job Manager even if the parent row was marked completed.
    children = details.get("childDetails") or job.get("childDetails") or []
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                child_status = str(child.get("status") or "").strip().upper()
                if child_status in ("CANCELED", "CANCELLED"):
                    status = "CANCELED"
                    break

    payload_user = str(payload.get("user") or "").strip()
    scm_user = payload_user or _workstation_user()

    return {
        "job_id": str(job.get("jobID") or ""),
        "name": str(payload.get("name") or ""),
        "type": str(payload.get("type") or ""),
        "status": status,
        "percent": max(0, min(100, percent)),
        "cloud": bool(job.get("cloud")),
        "worker": str(job.get("workerMachine") or ""),
        "started": _epoch_ms(job) / 1000.0,
        "finished": status in TERMINAL_STATUSES,
        "scm_user": scm_user or None,
    }


def viewer_exe():
    """Path to Autodesk's Job Viewer shell, or None if it is not installed."""
    for bin_dir in _scm_bin_dirs():
        try:
            exe = bin_dir / "ComputeBrowser" / "ComputeBrowser.exe"
            if exe.exists():
                return exe
        except Exception:
            continue
    return None


def open_viewer():
    """Open the real "Simulation Job Viewer" window. Returns the Popen handle,
    or None if it could not be started.

    This is exactly how Synergy opens it: ComputeBrowser.exe is an Electron
    shell whose only argument is the URL to load. It is deliberately launched
    WITHOUT the --ipc option -- with --ipc the window starts hidden and stays
    hidden until something calls /show on the sidecar port it writes out, so a
    failed handshake would leave an invisible 188MB process behind. Without it
    the window shows itself and there is nothing to coordinate.

    The caller keeps the handle so a second click can raise the existing window
    instead of opening a duplicate (see poll()) -- Electron will happily open
    two."""
    exe = viewer_exe()
    root = base_url()
    if not exe or not root:
        return None
    try:
        import subprocess
        # CREATE_NEW_PROCESS_GROUP only. A Windows child already outlives its
        # parent, so DETACHED_PROCESS buys nothing here; the group keeps a
        # Ctrl-C aimed at the workflow from reaching the viewer.
        return subprocess.Popen(
            [str(exe), root + "/"],
            creationflags=0x00000200, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except Exception:
        return None


def phases(job):
    """The job's per-phase children as [{"name", "status", "percent"}], or [].

    These are the rows the Job Viewer nests under a study, and they are what
    tells the user WHICH part of a multi-phase sequence is running -- a
    Cool+Flow+Warp job sitting at 40% is much more legible as "warp3d, 40%".

    Note the child rows carry "type"/"status"/"percent" at their TOP level, not
    under a "payload" key like real jobs do; they are progress records, not
    jobs, even though they are also listed as jobs in /jobs."""
    out = []
    try:
        details = (job.get("progress") or {}).get("details") or {}
        for child in details.get("childDetails") or []:
            label = str(child.get("type") or "")
            parts = [p for p in label.split(":") if p]
            if len(parts) < 2:
                continue
            try:
                percent = int(child.get("percent") or 0)
            except Exception:
                percent = 0
            out.append({
                "name": parts[1],
                "status": str(child.get("status") or "").strip().upper(),
                "percent": max(0, min(100, percent)),
            })
    except Exception:
        return []
    return out
