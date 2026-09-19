"""
standalone_job_monitor.py
--------------------------
Independent monitor that keeps mobile job reporting alive even after
Synergy (and cad_diagnostics.py's plugin automation) has closed.

Background
----------
Reporting job status to the mobile backend currently only happens from
inside cad_diagnostics.py's wait_for_results() poll loop, which only runs
for as long as Synergy's automation run is alive. Simulation Compute
Manager (SCM) itself, however, runs as an independent Windows service
(adskscm2, LocalSystem, AUTO_START -- verified via `sc qc adskscm2`) and
keeps executing/tracking the job regardless of Synergy: confirmed live by
querying http://127.0.0.1:44100/ComputeQueue/v1/version with Synergy
closed, and by the fact the Simulation Job Viewer itself (just another
client of that same API) keeps showing progress after Synergy closes.

This script closes that gap: it polls SCM directly (via the existing,
UNMODIFIED compute_jobs.py) for whatever jobs our backend says are still
active on this machine, and reports through the exact same
/reportJobStatus contract the plugin already uses -- so every downstream
piece (PostgreSQL, the terminal-state-lock guard, FCM, UNO) needs zero
changes.

What this script deliberately does NOT do
------------------------------------------
It cannot and does not attempt to actually stop/cancel the real SCM
computation -- no such mechanism exists (SCM's own REST cancel returns
HTTP 501, re-verified; Synergy's COM API has no Stop/Abort/StopAnalysis).
When it sees cancel_requested for a job, it reports CANCELED to our own
system, mirroring cad_diagnostics.py's cooperative-cancel behaviour
exactly -- it does not and cannot guarantee the underlying Moldflow
analysis actually stops. That limitation already existed even with
Synergy open; this script does not make it worse, and does not pretend
otherwise.

Design notes
------------
- Zero COM, zero Synergy dependency -- pure HTTP. compute_jobs.py talks to
  SCM on localhost:44100; mobile_reporter.py talks to our FastAPI. This
  script can run standalone, long before/after Synergy ever opens, and
  never touches the Synergy COM apartment that the rest of the plugin has
  to carefully tear down.
- Stateless by design: every cycle re-fetches "what's active" from our own
  backend (GET /internal/active-jobs) rather than keeping a local job
  list. A crash-and-restart loses nothing -- the next cycle just resumes
  from whatever PostgreSQL currently says. No local state file.
- No PID-lock / single-instance guard here on purpose. This project has
  already been bitten once by a hand-rolled Windows liveness check
  (os.kill(pid, 0) TERMINATES the target on Windows, it does not probe --
  see cad_diagnostics.py's lock-file fix). Single-instance is enforced by
  the Scheduled Task registration instead ("If the task is already
  running: Do not start a new instance"), which needs no custom code.
- Every per-job iteration is individually wrapped so one bad record (a
  malformed row, a transient SCM hiccup) never takes down the whole cycle
  or the other jobs being watched.
- Cannot produce the richer decoded-.out-file error text
  cad_diagnostics.py attaches to a FAILED report -- that logic
  (resolve_out_path/decode_solver_err) is tied to the locally open study
  in Synergy's own process, which this script has no access to by design.
  Failure/cancel reports from here carry SCM's bare status only.
"""

import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Ensure the repository root is on sys.path so lib/ is importable.
_repo_root = str(Path(__file__).resolve().parents[1])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from lib.scm import client as compute_jobs
    from lib.mobile import reporter as mobile_reporter
except ImportError:
    # Fallback for legacy deployment layouts where compute_jobs and
    # mobile_reporter sit directly beside this file.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import compute_jobs  # type: ignore
    import mobile_reporter  # type: ignore

# Matches the plugin's own <=5s cancel-propagation promise.
POLL_INTERVAL = 5.0

# Don't hammer a down SCM service every 5s -- back off further.
SCM_OFFLINE_BACKOFF = 30.0

LOG_PATH = Path(__file__).with_name("monitor_log.txt")

# Forensic instrumentation only (see DIAGNOSTIC INSTRUMENTATION below) --
# kept entirely separate from LOG_PATH/log() so it can't share log()'s
# stdout dependency.
DIAG_LOG_PATH = Path(__file__).with_name("monitor_diag.txt")

_PID = os.getpid()


def log(message):
    """Append one timestamped line to monitor_log.txt and stdout. Never
    raises -- a broken log must not take down the monitor loop itself."""
    try:
        line = "[{0}] {1}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)
    except Exception:
        pass


# ============================================================================
# DIAGNOSTIC INSTRUMENTATION (forensic only -- added to find the exact
# blocking point behind the two observed stalls, not to fix anything).
#
# diag() deliberately never touches print()/stdout, unlike log() above.
# Two stalls (~70s and ~40s) were observed with the process alive,
# Responding=True, CPU~0, both self-recovering without intervention -- a
# genuinely blocked print() to an unread pipe (the leading hypothesis, from
# launching via `Start-Process -WindowStyle Hidden` with no
# -RedirectStandardOutput/-RedirectStandardError) should NOT self-recover on
# its own, so this is still unconfirmed, not concluded. If diag() also used
# print()/stdout, a stall there would silently swallow the very evidence
# needed to tell "logging is blocked" apart from "the operation itself is
# slow" -- hence the separate, synchronously flushed+fsync'd file channel.
# ============================================================================

def diag(message):
    """One forensic line to monitor_diag.txt: timestamp with milliseconds,
    PID, message. Writes, flushes, and fsyncs synchronously so a line that
    successfully appears here proves the write genuinely completed at that
    wall-clock moment, independent of whatever else the process is doing.
    Never raises -- instrumentation must not itself become a new failure
    mode."""
    try:
        now = datetime.now()
        ts = "{0}.{1:03d}".format(
            now.strftime("%H:%M:%S"), now.microsecond // 1000)
        line = "[{0}] PID={1} {2}".format(ts, _PID, message)
        with open(DIAG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _elapsed_ms(start):
    return int((time.perf_counter() - start) * 1000)


def watch_one(job_row):
    """Poll SCM for one job our backend still considers active, and report
    whatever it finds -- or a cooperative CANCELED if requested. Never
    raises; the caller wraps this per job."""
    job_id = str(job_row.get("job_id") or "")
    scm_job_id = str(job_row.get("scm_job_id") or "")
    if not job_id or not scm_job_id:
        return

    # True SCM cancellation: send DELETE to SCM REST queue.
    # Verify the SCM job ID explicitly (Requirement 2).
    if job_row.get("cancel_requested"):
        if not scm_job_id:
            log("Job {0}: cancel requested but scm_job_id is missing; cannot cancel.".format(job_id))
            return

        diag("job={0} cancel_job START".format(scm_job_id))
        t0 = time.perf_counter()
        cancelled = False
        try:
            cancelled = compute_jobs.cancel_job(scm_job_id)
        except Exception as exc:
            log("Job {0} ({1}): exception during SCM cancel_job: {2}".format(
                job_id, scm_job_id, exc))
            cancelled = False

        diag("job={0} cancel_job END elapsed={1}ms (result={2})".format(
            scm_job_id, _elapsed_ms(t0), cancelled))

        if not cancelled:
            # SCM cancellation failed or could not be verified. Do NOT report fake CANCELED.
            log("Job {0} ({1}): SCM cancellation failed or unverified; preserving status.".format(
                job_id, scm_job_id))
            return

        # SCM cancellation confirmed. Report CANCELED to backend.
        diag("job={0} report_status START (status=CANCELED)".format(scm_job_id))
        t0 = time.perf_counter()
        mobile_reporter.report_status({
            "job_id": job_id,
            "name": job_row.get("name"),
            "type": job_row.get("job_type"),
            "status": "CANCELED",
            "percent": job_row.get("percent") or 0,
            "started": job_row.get("started"),
            "finished": True,
            "error_message": "Cancelled remotely via mobile app",
            "scm_job_id": scm_job_id,
            "scm_type": job_row.get("scm_type"),
            "compute_source": job_row.get("compute_source"),
            "scm_user": job_row.get("scm_user"),
            "worker": job_row.get("worker"),
            "parent_job_id": job_row.get("parent_job_id"),
        }, log=log)
        diag("job={0} report_status END elapsed={1}ms (status=CANCELED)".format(
            scm_job_id, _elapsed_ms(t0)))
        log("Job {0} ({1}): SCM cancellation confirmed -- reported CANCELED "
            "(monitor-driven).".format(job_id, job_row.get("name")))
        return

    diag("job={0} get_job START".format(scm_job_id))
    t0 = time.perf_counter()
    scm_job = compute_jobs.get_job(scm_job_id)
    diag("job={0} get_job END elapsed={1}ms".format(scm_job_id, _elapsed_ms(t0)))
    if scm_job is None:
        # Check if SCM is reachable and confirming this job is no longer present
        if compute_jobs.available():
            try:
                all_ids = compute_jobs.job_ids()
            except Exception:
                all_ids = set()
            if scm_job_id not in all_ids:
                log("Job {0} ({1}): not located in SCM queue; SCM-side cancellation confirmed."
                    .format(job_id, scm_job_id))
                diag("job={0} report_status START (status=CANCELED, missing from SCM)".format(scm_job_id))
                t1 = time.perf_counter()
                mobile_reporter.report_status({
                    "job_id": job_id,
                    "name": job_row.get("name"),
                    "type": job_row.get("job_type"),
                    "status": "CANCELED",
                    "percent": job_row.get("percent") or 0,
                    "started": job_row.get("started"),
                    "finished": True,
                    "error_message": "Cancelled or deleted in SCM Job Manager",
                    "scm_job_id": scm_job_id,
                    "scm_type": job_row.get("scm_type"),
                    "compute_source": job_row.get("compute_source"),
                    "scm_user": job_row.get("scm_user"),
                    "worker": job_row.get("worker"),
                    "parent_job_id": job_row.get("parent_job_id"),
                }, log=log)
                diag("job={0} report_status END elapsed={1}ms (status=CANCELED)".format(
                    scm_job_id, _elapsed_ms(t1)))
                return
        return

    if scm_job.get("_scm_deleted"):
        log("Job {0} ({1}): SCM reported job deleted/canceled in SCM Job Manager."
            .format(job_id, scm_job_id))
        diag("job={0} report_status START (status=CANCELED, _scm_deleted)".format(scm_job_id))
        t1 = time.perf_counter()
        mobile_reporter.report_status({
            "job_id": job_id,
            "name": job_row.get("name"),
            "type": job_row.get("job_type"),
            "status": "CANCELED",
            "percent": job_row.get("percent") or 0,
            "started": job_row.get("started"),
            "finished": True,
            "error_message": "Cancelled or deleted in SCM Job Manager",
            "scm_job_id": scm_job_id,
            "scm_type": job_row.get("scm_type"),
            "compute_source": job_row.get("compute_source"),
            "scm_user": job_row.get("scm_user"),
            "worker": job_row.get("worker"),
            "parent_job_id": job_row.get("parent_job_id"),
        }, log=log)
        diag("job={0} report_status END elapsed={1}ms (status=CANCELED)".format(
            scm_job_id, _elapsed_ms(t1)))
        return

    summary = compute_jobs.summarize(scm_job)
    if not summary:
        return

    diag("job={0} report_status START (status={1})".format(
        scm_job_id, summary["status"]))
    t0 = time.perf_counter()
    mobile_reporter.report_status({
        "job_id": job_id,
        "name": summary["name"] or job_row.get("name"),
        "type": job_row.get("job_type"),
        "status": summary["status"],
        "percent": summary["percent"],
        "started": job_row.get("started"),
        "finished": summary["finished"],
        "error_message": None,
        "scm_job_id": scm_job_id,
        "scm_type": summary["type"] or job_row.get("scm_type"),
        "compute_source": "CLOUD" if summary["cloud"] else "LOCAL",
        "scm_user": (
            summary.get("scm_user")
            or job_row.get("scm_user")
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
        ),
        "worker": summary["worker"] or job_row.get("worker"),
        "parent_job_id": job_row.get("parent_job_id"),
    }, log=log)
    diag("job={0} report_status END elapsed={1}ms".format(
        scm_job_id, _elapsed_ms(t0)))


def poll_once():
    """One monitoring cycle. Never raises."""
    diag("POLL START")
    t_poll = time.perf_counter()

    if not mobile_reporter.enabled():
        diag("POLL END elapsed={0}ms (reporting disabled)".format(
            _elapsed_ms(t_poll)))
        return

    # compute_jobs.available() probes SCM's /version endpoint and caches the
    # result ONLY once SCM answers -- expect a real elapsed time on every
    # cycle until SCM is actually reachable (e.g. still starting up after a
    # Windows restart), then ~0ms on every later cycle once cached.
    diag("compute_jobs.available START")
    t0 = time.perf_counter()
    scm_ok = compute_jobs.available()
    diag("compute_jobs.available END elapsed={0}ms result={1}".format(
        _elapsed_ms(t0), scm_ok))

    if not scm_ok:
        log("SCM queue service not reachable this cycle; backing off.")
        diag("POLL END elapsed={0}ms (SCM unavailable)".format(
            _elapsed_ms(t_poll)))
        time.sleep(SCM_OFFLINE_BACKOFF)
        return

    diag("list_active_jobs START")
    t0 = time.perf_counter()
    jobs = mobile_reporter.list_active_jobs(log=log)
    diag("list_active_jobs END elapsed={0}ms count={1}".format(
        _elapsed_ms(t0), len(jobs)))

    if not jobs:
        diag("POLL END elapsed={0}ms (no active jobs)".format(
            _elapsed_ms(t_poll)))
        return

    for job_row in jobs:
        try:
            watch_one(job_row)
        except Exception as e:
            log("Job {0}: unexpected error ({1}).".format(
                job_row.get("job_id"), e))
            diag("job={0} watch_one EXCEPTION {1}".format(
                job_row.get("scm_job_id"), e))

    diag("POLL END elapsed={0}ms".format(_elapsed_ms(t_poll)))


def main():
    log("Standalone job monitor starting (independent of Synergy).")
    diag("MONITOR STARTING")
    while True:
        try:
            poll_once()
        except Exception:
            log("Monitor cycle failed unexpectedly:\n" + traceback.format_exc())
            diag("MONITOR CYCLE EXCEPTION -- see monitor_log.txt for traceback")
        diag("SLEEP START duration={0}s".format(POLL_INTERVAL))
        time.sleep(POLL_INTERVAL)
        diag("SLEEP END")


if __name__ == "__main__":
    main()
