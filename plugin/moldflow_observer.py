"""
moldflow_observer.py
---------------------
A background polling daemon that monitors the active Autodesk Moldflow Synergy session.
Detects when CAD files are imported (by checking StudyDoc and CAD bodies).
When a new import is found, spawns cad_diagnostics.py in the background.
Exits automatically when Synergy is closed to avoid orphaned processes.
"""

import sys
import os
import time
import subprocess
import traceback
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
LOG_FILE = HERE / "observer_log.txt"

# Each diagnostics run gets its OWN log file under logs/.  Previously every
# run's stdout was redirected into a single shared diagnostics_log.txt via a
# handle that the parent never closed; concurrent runs then wrote through
# independent buffered handles, so lines interleaved, landed out of
# chronological order (a slow run flushing after later ones had finished) and
# were silently lost -- 59 launches produced only 4 surviving "report written"
# lines against 10 reports actually on disk.  One file per run removes the
# shared-handle contention entirely and keeps each run readable in isolation.
LOGS_DIR = HERE / "logs"
# Kept as a lightweight index: one line per launch pointing at the real log.
LOG_INDEX = HERE / "diagnostics_log.txt"

def log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Tagged per Synergy window -- two observers share observer_log.txt.
    try:
        import session_context as _sc
        tag = f" [{_sc.session_key()}]"
    except Exception:
        tag = ""
    msg = f"{timestamp}{tag} - {message}"
    print(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def _publish_status(message):
    """Keep the workflow panel's CAD Diagnostics row showing that the
    automation is still moving. moldflow_startup sets the first message the
    moment the import ends; this one covers the launch itself, and
    cad_diagnostics.py takes over once it has attached to Synergy. Purely
    cosmetic -- never allowed to affect whether diagnostics run."""
    try:
        import ui_bridge
        ui_bridge.set_step_in_progress("cad_diagnostics", message)
    except Exception as e:
        log("Could not publish '{0}' to the panel: {1}".format(message, e))


def run_diagnostics_process():
    log("Triggering CAD diagnostics process...")
    _publish_status("Launching CAD Diagnostics...")
    # Find Python executable
    py_exe = sys.executable
    script_path = HERE / "cad_diagnostics.py"
    
    try:
        started = datetime.datetime.now()
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / "diagnostics_{0}.log".format(
            started.strftime("%Y%m%d_%H%M%S_%f")
        )

        # Not a 'with' block: the subprocess outlives this function and needs
        # the handle.  The parent closes its own copy right after Popen (see
        # below) -- the child holds an independent duplicate.
        lf = open(log_file, "w", encoding="utf-8")
        lf.write("=== Diagnostics run started {0} ===\n".format(started))
        lf.flush()
        try:
            # -u: unbuffered child stdout/stderr, so a run that dies mid-way
            # (Synergy's COM server dropping out is routine here) still leaves
            # everything it had already printed on disk instead of losing the
            # tail in a buffer that never flushed.
            proc = subprocess.Popen(
                [py_exe, "-u", str(script_path)],
                stdout=lf,
                stderr=lf,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
        finally:
            # Parent drops its handle regardless; leaking one per launch was
            # part of why the shared log misbehaved.
            lf.close()

        # Single short line into the index -- small enough that concurrent
        # appends don't tear, and it gives us run -> log-file traceability.
        try:
            with open(LOG_INDEX, "a", encoding="utf-8") as idx:
                idx.write("{0} pid={1} log={2}\n".format(
                    started.strftime("%Y-%m-%d %H:%M:%S"), proc.pid, log_file.name))
        except Exception:
            pass

        log("CAD diagnostics process launched (pid {0}) -> logs/{1}".format(
            proc.pid, log_file.name))
        return proc
    except Exception as e:
        log(f"Error launching CAD diagnostics process: {e}\n{traceback.format_exc()}")
        return None

# ---------------------------------------------------------------------------
# Synergy shutdown detection — WITHOUT touching COM.
#
# The post-close crash report: when the user closes Moldflow, this observer's
# 3-second COM poll (Build/StudyDoc/GetAllCadBodies) kept calling into
# synergy.exe WHILE it was tearing itself down. An automation call landing on
# a half-destroyed server raises RPC_E_SERVERFAULT ("The server threw an
# exception", -2147417851 — see observer_log.txt at 2026-07-22 17:13 and
# 2026-07-23 12:53), and Synergy's crash reporter appears a few seconds after
# the window closed. The old loop even RETRIED after each fault, poking the
# dying process three times in a row.
#
# Synergy destroys its top-level windows at the very START of shutdown,
# seconds before the process exits — so "no visible synergy.exe window left"
# is a reliable, COM-free signal to stop polling, release our proxies while
# the server can still process the Releases, and exit quietly.
# ---------------------------------------------------------------------------
def _make_synergy_ui_probe():
    """Return a zero-COM callable -> True while a visible synergy.exe
    top-level window exists. Any failure returns True (never blocks the
    observer on a probe bug — the COM error paths still catch shutdown)."""
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _image_is_synergy(pid):
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower() == "synergy.exe"
            return False
        finally:
            kernel32.CloseHandle(h)

    def probe():
        try:
            found = {"hit": False}

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def _cb(hwnd, _):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    pid = wt.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value and _image_is_synergy(pid.value):
                        found["hit"] = True
                        return False  # stop enumerating
                except Exception:
                    pass
                return True

            user32.EnumWindows(_cb, 0)
            return found["hit"]
        except Exception:
            return True

    return probe


def _com_call_is_fatal(exc):
    """True when a COM error means the server is dying/dead — never retry
    the call (each retry is another exception thrown INSIDE synergy.exe)."""
    try:
        hr = getattr(exc, "hresult", None)
        if hr is None and getattr(exc, "args", None):
            hr = exc.args[0]
        return (int(hr) & 0xFFFFFFFF) in (
            0x80010105,  # RPC_E_SERVERFAULT — server threw handling our call
            0x800706BA,  # RPC server unavailable
            0x800706BE,  # remote procedure call failed
            0x80010108,  # RPC_E_DISCONNECTED
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keeping the AI Assistant reachable across sessions.
#
# The Assistant panel is a persisted dock bar. The report workflow opens it,
# uses it, then closes it again to leave Synergy as it found it -- so Synergy
# writes Visible=False on exit, and the NEXT session starts with the panel
# closed. A closed panel means no WebView2, which means no channel, which means
# the deck silently falls back to the local rules: no Recommendations section
# and no Result Statistics table (2026-08-05: only 3 of the last 11 reports
# used the live panel).
#
# run_startup.vbs already undoes that, but ONLY when Synergy was launched
# through it. Any other launch -- a desktop shortcut, a second window, a
# developer running synergy.exe directly -- leaves the chain broken with no
# sign of it until a report comes out degraded. This observer is attached to
# EVERY session however it started, so it is the right place for the fixup.
#
# Timing is the whole trick: Synergy destroys its windows at the START of
# shutdown but rewrites the dock state as it exits, so writing on the window-
# closed signal would just be overwritten. We wait for the PROCESS to go.
# ---------------------------------------------------------------------------
_ASSISTANT_FIXUP_WAIT = 120.0   # seconds to wait for synergy.exe to exit
_ASSISTANT_FIXUP_POLL = 2.0


def ensure_assistant_panel_for_next_launch():
    """Mark the Assistant dock bar visible once synergy.exe has fully exited.

    Never raises and never blocks shutdown for long: if the process is still
    there when the wait runs out (a second Synergy window is open, or this
    observer stood down while Synergy kept running), we skip -- that session's
    own observer will do it when its turn comes.
    """
    try:
        import assistant_panel
    except Exception as e:
        log(f"Assistant panel fixup skipped (import failed): {e}")
        return

    deadline = time.time() + _ASSISTANT_FIXUP_WAIT
    while time.time() < deadline:
        try:
            if not assistant_panel.synergy_is_running():
                break
        except Exception as e:
            log(f"Assistant panel fixup skipped (process probe failed): {e}")
            return
        time.sleep(_ASSISTANT_FIXUP_POLL)
    else:
        log("Assistant panel fixup skipped: synergy.exe still running after "
            f"{_ASSISTANT_FIXUP_WAIT:.0f}s (another Synergy window is likely "
            "open; its observer will handle it).")
        return

    try:
        changed, message = assistant_panel.ensure_persisted_visible(log=log)
    except Exception as e:
        log(f"Assistant panel fixup failed: {e}")
        return
    if changed:
        log(f"Assistant panel will open on the next Synergy launch ({message}).")
    else:
        log(f"Assistant panel needs no change: {message}")


def _release_synergy_proxies():
    """Drop every COM proxy NOW (while synergy.exe can still process the
    Releases), so nothing of ours lingers into its shutdown."""
    try:
        from synergy_connect import release_com_objects
        release_com_objects()
    except Exception:
        pass


# Manual-mode switch: while this file exists the automation stands down.
# Toggle with disable_automation.bat / enable_automation.bat.
DISABLE_FLAG = HERE / "automation_disabled.flag"

# Per-SESSION manual switch: written by moldflow_startup.py when the user
# answers No to the startup prompt ("I want to work manually"), deleted by the
# next Synergy startup. While present, this observer must stand down so a
# MANUAL import does not trigger the automation workflow.
#
# It lives in THIS Synergy window's session directory. Shared, it meant
# declining automation in one window silenced the other window's observer too.
import session_context
MANUAL_SESSION_FLAG = session_context.session_path("_manual_session.flag")


def main():
    if DISABLE_FLAG.exists():
        log("automation_disabled.flag present - MANUAL MODE, observer not starting.")
        return
    if MANUAL_SESSION_FLAG.exists():
        log("_manual_session.flag present - MANUAL session, observer not starting.")
        return

    log("Moldflow Observer started.")
    
    # Try to connect to synergy_connect
    try:
        sys.path.insert(0, str(HERE))
        from synergy_connect import get_synergy
    except Exception as e:
        log(f"Failed to import synergy_connect: {e}")
        sys.exit(1)
        
    syn = None
    log("Waiting to attach to Synergy session...")
    
    # Attempt to attach to Synergy for up to 60 seconds
    start_attach = time.time()
    while time.time() - start_attach < 60:
        try:
            syn = get_synergy(allow_launch=False)
            if syn is not None:
                # Test connection
                syn.Build()
                log("Successfully attached to Synergy.")
                break
        except Exception:
            pass
        time.sleep(2)
        
    if syn is None:
        log("Timeout waiting to attach to Synergy. Exiting.")
        sys.exit(1)
        
    # Variables to track state
    last_bodies_str = None
    analyzed_studies = set()

    # COM-rundown guard. When a diagnostics child process exits, synergy.exe
    # spends a few seconds running down that client's automation stubs; a COM
    # call arriving mid-rundown is what made the server fault (observer log:
    # RPC_E_SERVERFAULT three polls in a row, then the Synergy crash dialog).
    # The child now releases its proxies cleanly on exit (synergy_connect
    # atexit teardown), but if it is killed or crashes that teardown never
    # runs -- so also pause OUR polls for a short grace window right after a
    # child exit. Detection of a later import is merely delayed by that
    # window; nothing else changes.
    active_procs = []            # diagnostics children still running
    last_child_exit = 0.0        # time.time() of the most recent child exit
    CHILD_EXIT_GRACE = 15.0      # seconds to stay off the COM channel

    synergy_ui_present = _make_synergy_ui_probe()
    # During EARLY STARTUP the vbs prompt blocks Synergy before its main
    # window is visible, so "no window" is normal then — only treat a missing
    # window as shutdown AFTER the window has been seen at least once.
    # (Without this latch the observer exited 3s after starting on 07-23
    # 16:32, while Synergy was demonstrably alive.)
    ui_seen = False

    log("Starting monitoring loop...")
    while True:
        # Manual-mode switches. Master flag (disable_automation.bat) or the
        # per-session flag (user answered No to the startup prompt): either
        # way, stand down cleanly.
        if DISABLE_FLAG.exists():
            log("automation_disabled.flag appeared - MANUAL MODE, observer "
                "releasing COM references and exiting.")
            _release_synergy_proxies()
            break
        if MANUAL_SESSION_FLAG.exists():
            log("_manual_session.flag present - user chose a MANUAL session; "
                "observer releasing COM references and exiting.")
            _release_synergy_proxies()
            break

        # FIRST, without any COM: is Synergy closing? Its windows vanish at
        # the very start of shutdown — stop here so we never call into a
        # server that is tearing itself down (that call is what produced the
        # post-close crash report), and release our proxies while it can
        # still handle the Releases cleanly.
        if synergy_ui_present():
            ui_seen = True
        elif ui_seen:
            log("Synergy window closed — releasing COM references and "
                "exiting observer without further Synergy calls.")
            _release_synergy_proxies()
            break
        # Reap finished diagnostics children and start the grace window.
        still = []
        for p in active_procs:
            try:
                if p.poll() is None:
                    still.append(p)
                else:
                    last_child_exit = time.time()
                    log("CAD diagnostics process (pid {0}) exited with code {1}; "
                        "pausing Synergy polls for {2:.0f}s while its COM "
                        "connection winds down.".format(
                            p.pid, p.returncode, CHILD_EXIT_GRACE))
            except Exception:
                pass
        active_procs = still

        if last_child_exit and time.time() - last_child_exit < CHILD_EXIT_GRACE:
            time.sleep(3)
            continue

        # Check if Synergy is still running
        try:
            syn.Build()
        except Exception:
            log("Synergy session disconnected. Exiting observer.")
            _release_synergy_proxies()
            break

        # While a diagnostics workflow is RUNNING, stand down completely:
        # (a) never launch a second concurrent workflow (this guard existed
        #     on 2026-07-18 and was lost in the build reset — it is how four
        #     python instances ended up running at once on 07-22), and
        # (b) skip the StudyDoc/GetAllCadBodies polling entirely — hammering
        #     Synergy with extra COM calls every 3s while it is meshing or
        #     solving is what makes the UI feel stuck during automation.
        if active_procs:
            time.sleep(3)
            continue

        try:
            study_doc = syn.StudyDoc()
            if study_doc is not None:
                study_name = study_doc.StudyName()
                
                # Get CAD bodies
                body_string = None
                for getter in (
                    lambda: study_doc.get_all_cad_bodies(False),
                    lambda: study_doc.GetAllCadBodies(False),
                ):
                    try:
                        body_string = getter()
                        if callable(body_string):
                            body_string = body_string()
                        break
                    except Exception:
                        pass
                
                if body_string is not None:
                    body_string = str(body_string).strip()
                    
                # If there are CAD bodies in the study, and either:
                # 1. This study name has not been analyzed yet, or
                # 2. The CAD bodies selection string has changed (new import or change)
                if body_string and (study_name not in analyzed_studies or body_string != last_bodies_str):
                    log(f"New CAD bodies detected in study '{study_name}'.")
                    log(f"CAD body string: {body_string!r}")
                    
                    # Update tracking state before running process to avoid duplicate triggers
                    analyzed_studies.add(study_name)
                    last_bodies_str = body_string
                    
                    # Run diagnostics in the background
                    child = run_diagnostics_process()
                    if child is not None:
                        active_procs.append(child)
            else:
                # No active study document
                if last_bodies_str is not None:
                    log("Study closed or no active study.")
                    last_bodies_str = None
        except Exception as e:
            # A dying/dead server must NOT be retried — every retry throws
            # another exception INSIDE synergy.exe (this was the 3-in-a-row
            # SERVERFAULT pattern right before each crash report).
            if _com_call_is_fatal(e):
                log(f"Synergy COM connection is gone ({e}). Exiting observer.")
                _release_synergy_proxies()
                break
            # Transient errors are still logged and survived, as before.
            log(f"Error in monitoring loop: {e}")

        time.sleep(3)

    # Every exit from the loop above lands here -- Synergy shutting down, a
    # dropped COM connection, or a manual-mode stand-down. The fixup itself
    # waits for synergy.exe to go and skips if it does not, so it is safe on
    # all of them and belongs on none of them specifically.
    ensure_assistant_panel_for_next_launch()
    log("Observer stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Observer crashed: {e}\n{traceback.format_exc()}")
