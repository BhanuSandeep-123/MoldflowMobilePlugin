"""
ui_launcher.py
--------------
Brings up the plugin's embedded workflow panel (embedded_ui.py) so prompts
have somewhere to be answered when Moldflow Insight 2027 opens.

Earlier version of this file started a local HTTP server and opened the
dashboard in the user's default browser. That is no longer what's wanted:
startup must be silent, nothing should open a separate browser window, and
the workflow should appear docked inside the Moldflow environment instead.
This version:

  1. Resets ui_state.json to a clean default (fresh session).
  2. Starts embedded_ui.py (pythonw, no console) as a background process --
     it finds Synergy's window and docks a chrome-less panel beside it.
  3. Waits briefly for its heartbeat to confirm the panel actually came up.

If the panel can't be created (Synergy's window not found, tkinter/display
unavailable, etc.), this logs the reason to ui_bridge_log.txt and returns
False. Nothing here ever shows a dialog or opens a browser -- callers
(moldflow_startup.py) are expected to skip their startup prompts silently
when this returns False.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import session_context
import ui_bridge

# Windows: launch without flashing a console window.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _python_exe(prefer_windowed: bool = True) -> str:
    """Pick pythonw.exe (no console at all) next to the interpreter running
    this script if available, else fall back to sys.executable."""
    exe = Path(sys.executable)
    if prefer_windowed:
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe) if exe.exists() else "python"


def reset_state() -> None:
    """Start a session with a clean slate.

    This runs once per Synergy bring-up, which is exactly one workflow run, so
    everything the last run left behind goes: the journey params, the activity
    log, the progress bars and any prompt still pending. Keeping the params
    (the previous behaviour) is why a freshly started session opened showing
    the last run's project name, model, mesh and solver rows -- a completed
    workflow that had not actually happened yet."""
    try:
        import json
        fresh = json.loads(json.dumps(ui_bridge.DEFAULT_STATE))
        ui_bridge.save_state(fresh)
        ui_bridge.log_silent(
            "reset_state: journey cleared for a new session "
            f"({session_context.describe()}, dir={session_context.session_dir()}).")
    except Exception as e:
        ui_bridge.log_silent(f"reset_state failed: {e}")

    # Housekeeping for sessions that ended without cleaning up. Best-effort
    # and never fatal: a failure here only leaves disk in use.
    try:
        gone = session_context.prune_stale_sessions()
        if gone:
            ui_bridge.log_silent(f"reset_state: pruned {gone} stale session folder(s).")
    except Exception:
        pass


def launch_embedded_panel(wait_seconds: float = 6.0) -> bool:
    """Start embedded_ui.py if it is not already alive. Returns True once its
    heartbeat confirms the panel is up and docked; False (with the reason
    logged) otherwise. Never raises, never shows anything to the user.

    "Already alive" means a panel for THIS Synergy window -- the heartbeat is
    per-session (see session_context.py). When it was one shared file, opening
    a second Synergy window found the first window's beat, returned True here
    without starting anything, and left that window with no panel at all."""
    if ui_bridge.ui_alive():
        return True

    py_exe = _python_exe()
    script = HERE / "embedded_ui.py"
    try:
        subprocess.Popen(
            [py_exe, str(script)],
            cwd=str(HERE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        ui_bridge.log_silent(f"launch_embedded_panel: could not start embedded_ui.py: {e}")
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if ui_bridge.ui_alive():
            return True
        time.sleep(0.2)

    # Didn't come up in time -- embedded_ui.py logs its own reason
    # (embedded_ui_log.txt, e.g. "Synergy window not found") on failure;
    # this just records that the launcher gave up waiting.
    ui_bridge.log_silent(
        "launch_embedded_panel: embedded panel did not report a heartbeat "
        f"within {wait_seconds:.0f}s; see embedded_ui_log.txt for details."
    )
    return False


def launch(open_browser: bool = False) -> bool:
    """Full bring-up: reset state, start the embedded panel. Returns True if
    the panel is alive afterwards. ``open_browser`` is accepted for backward
    compatibility with older callers but is always ignored -- nothing here
    ever opens a browser."""
    reset_state()
    return launch_embedded_panel()


if __name__ == "__main__":
    ok = launch()
    sys.exit(0 if ok else 1)
