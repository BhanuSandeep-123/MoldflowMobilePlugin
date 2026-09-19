"""
moldflow_startup.py
-------------------
Startup workflow for Autodesk Moldflow Insight 2027.

Auto-runs when Synergy/Insight opens (via run_startup.vbs registered as a
startup command). If no project is open (blank page), it:

  1. Brings up the embedded workflow panel (docked beside Synergy's window --
     see embedded_ui.py) and shows the "Create New Project" form INSIDE it.
  2. On submit, creates the project via Synergy.NewProject(name, dir).
  3. Asks (in the same panel) whether to import a model file, and if so shows
     a native "Open File" picker via the panel.
  4. Imports the chosen file via Synergy's native import (ImportFile3, with a
     programmatic ImportFile/ImportOptions fallback).

Every step here is SILENT: no MessageBoxes, no InputBoxes, no browser
windows. If the embedded panel can't be created at all (Synergy's window
not found, tkinter unavailable, etc.), this logs the reason and returns
immediately without prompting -- the user just works manually for that
session, exactly as if automation weren't installed.

Run via run_startup.vbs (registered as a Synergy startup command).
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# Ensure the script's own directory is on sys.path so we can import siblings.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from synergy_connect import get_synergy, has_active_study
import session_context
import ui_bridge
import ui_launcher

LOG = os.path.join(HERE, "startup_log.txt")
# Per Synergy window: declining automation in one window must not stand the
# other window's observer down. The master switch (automation_disabled.flag,
# set by disable_automation.bat) stays install-wide by design.
MANUAL_SESSION_FLAG = str(session_context.session_path("_manual_session.flag"))


def _log(message: str) -> None:
    """Plain diagnostic logging -- never a dialog. Mirrors ui_bridge.log_silent
    but also goes to this module's own startup_log.txt for continuity with
    the previous version's log file."""
    try:
        import datetime
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(datetime.datetime.now().isoformat()
                     + " [" + session_context.session_key() + "] - "
                     + message + "\n")
    except Exception:
        pass
    ui_bridge.log_silent(message)


# --------------------------------------------------------------------------- #
#  Native Synergy calls (unchanged from before -- these were never the        #
#  problem; only the PROMPTING around them was)                               #
# --------------------------------------------------------------------------- #

IMPORT_EXT_FILTERS = [
    ("All Moldflow Models",
     "*.stp *.step *.igs *.iges *.stl *.udm *.sdy *.x_t *.x_b *.sat *.ipt *.catpart *.sldprt *.prt"),
    ("STEP Files", "*.stp *.step"),
    ("IGES Files", "*.igs *.iges"),
    ("STL Files", "*.stl"),
    ("Parasolid", "*.x_t *.x_b"),
    ("Moldflow ASCII", "*.udm"),
    ("Moldflow Study", "*.sdy"),
    ("All Files", "*.*"),
]


def create_project(syn, name: str, directory: str) -> bool:
    """Create the project directory (if needed) and open it in Moldflow via
    the documented Synergy.NewProject(name, dir) call."""
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as e:
        _log(f"Could not create project folder '{directory}': {e}")
        return False

    try:
        syn.NewProject(name, directory)
        return True
    except Exception as e:
        _log(f"Synergy.NewProject failed for name={name!r} dir={directory!r}: {e}")
        return False


def import_model(syn, file_path: str) -> bool:
    """Import a file using Synergy's native import options dialog
    (ImportFile3), falling back to programmatic ImportFile/ImportOptions if
    ImportFile3 is unavailable on this install."""
    try:
        ok = syn.ImportFile3(file_path)
        return bool(ok)
    except Exception as e:
        _log(f"ImportFile3 not available/failed ({e}); falling back to ImportFile.")

    try:
        opts = syn.ImportOptions()
        if opts is None:
            _log("ImportOptions() returned None; cannot import programmatically.")
            return False
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        opts.MeshType("3D")
        if ext in ("stp", "step", "igs", "iges", "ipt", "catpart", "sldprt", "prt", "x_t", "x_b", "sat"):
            try:
                opts.UseMDL(False)
                opts.MDLMesh(False)
            except Exception:
                pass
        return bool(syn.ImportFile(file_path, opts, False))
    except Exception as e:
        _log(f"ImportFile fallback failed: {e}")
        return False


def _enter_manual_session(reason: str) -> None:
    """Mirrors the previous VBScript behaviour: declining automation at
    startup (or having nothing to automate this session) tells the
    already-running observer to stand down for this session only, so a later
    manual CAD import does not trigger the automated workflow."""
    try:
        import datetime
        with open(MANUAL_SESSION_FLAG, "w", encoding="utf-8") as f:
            f.write(f"{reason} {datetime.datetime.now().isoformat()}\n")
        _log(f"Wrote _manual_session.flag ({reason}); observer stands down for this session.")
    except Exception as e:
        _log(f"Could not write _manual_session.flag: {e}")


def _mark_diagnostics_starting() -> None:
    """Tell the panel the CAD Diagnostics stage is starting, as soon as the
    import is done.

    Only when the observer is actually going to run it: with either manual-mode
    switch set nothing will ever follow the import, and an in-progress row that
    nobody clears would be a worse lie than the "Pending" one it replaces."""
    try:
        if os.path.exists(os.path.join(HERE, "automation_disabled.flag")):
            _log("Automation disabled; not marking CAD Diagnostics as starting.")
            return
        if os.path.exists(MANUAL_SESSION_FLAG):
            _log("Manual session; not marking CAD Diagnostics as starting.")
            return
        ui_bridge.set_step_in_progress(
            "cad_diagnostics", "Preparing CAD Diagnostics...")
    except Exception as e:
        _log(f"Could not publish the CAD Diagnostics starting status: {e}")


def _start_assistant_init_and_hide() -> None:
    """Kick off the AI Assistant startup step as a DETACHED process.

    The persisted dock state opens the Assistant panel at Synergy startup --
    that showing is what makes Synergy create the WebView2, and there is no way
    around it (a hidden dock bar produces no channel at all; an off-screen
    float gets re-docked -- both tested 2026-08-05). But the panel only needs to
    exist, not to stay on screen, so a helper waits for the channel to come up
    and then closes it. The user gets the workspace back and never interacts
    with the Assistant.

    Detached and fire-and-forget for two reasons: the wait is up to three
    minutes, and THIS function runs before several early returns below (a study
    already open, the user declining automation). A thread would die with the
    process; a child outlives it and still does the job.

    Failure here is cosmetic by design -- if the helper never runs, the panel
    simply stays open, which is how it behaved before and still works.
    """
    try:
        import subprocess
        script = os.path.join(HERE, "assistant_panel.py")
        if not os.path.exists(script):
            return
        # pythonw where available: this must never flash a console window at
        # Synergy startup, which would defeat the point of hiding a panel.
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        # Keep the helper's own output. It used to go to DEVNULL, which threw
        # away the only record of what the hide step actually did -- including
        # the two lines that matter most: the 180s "channel did not come up"
        # timeout, and the loud WARNING that fires if hiding the panel ALSO
        # drops the channel on this build. That warning guards the invariant
        # this whole design rests on, and nobody could ever have seen it.
        # A separate file, opened append: the helper outlives this process, so
        # it cannot share the handle _log() writes through.
        log_path = os.path.join(HERE, "assistant_panel_log.txt")
        try:
            sink = open(log_path, "a", encoding="utf-8", errors="replace")
            sink.write("\n--- init-and-hide {0} ---\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S")))
            sink.flush()
        except Exception:
            sink = subprocess.DEVNULL   # logging must never block the startup
        subprocess.Popen(
            # -u: the helper polls for up to three minutes, so a buffered
            # stdout would hold every line until it exited -- useless for
            # watching a startup that is still in progress.
            [exe, "-u", script, "--init-and-hide"],
            cwd=HERE,
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Popen duplicates the handle for the child, so this process can let go
        # of its own copy straight away; the child keeps writing after we exit.
        if sink is not subprocess.DEVNULL:
            sink.close()
        _log("AI Assistant init-and-hide helper started (detached); its output "
             "goes to assistant_panel_log.txt.")
    except Exception as e:
        _log(f"Could not start the AI Assistant init-and-hide helper: {e}")


def set_isometric_view(syn) -> None:
    try:
        viewer = syn.Viewer()
        if viewer is not None:
            viewer.GoToStandardView("Isometric")
            viewer.Fit()
    except Exception as e:
        _log(f"Could not set isometric view: {e}")


# =========================================================================== #
#  Main orchestrator                                                          #
# =========================================================================== #

def main() -> int:
    """Run the full startup workflow. Never raises a dialog; every failure
    path logs and returns quietly so Moldflow's own startup is unaffected."""
    try:
        _log("Startup workflow beginning.")

        # FIRST, before any early return below: the Assistant panel is opening
        # right now because of the persisted dock state, and it should be shut
        # again as soon as its channel is live. This matters in every session,
        # including the ones where the rest of this function bails out.
        _start_assistant_init_and_hide()

        syn = get_synergy(allow_launch=False)

        if has_active_study(syn):
            _log("A study is already open; skipping startup workflow.")
            return 0

        # Give Moldflow a moment to finish loading its UI before we go
        # looking for its window to dock against.
        time.sleep(1.0)

        if not ui_launcher.launch():
            _log("Embedded workflow panel unavailable; skipping automated "
                 "startup prompts for this session (manual mode). "
                 "See embedded_ui_log.txt / ui_bridge_log.txt for the reason.")
            return 0

        _log("Embedded panel is up. Requesting new-project details.")
        form = ui_bridge.request_project_form(
            default_name="MyProject",
            default_dir=str(os.path.join(os.path.expanduser("~"), "Documents", "Moldflow Projects")),
        )
        if not form:
            _log("User skipped project creation. Done.")
            _enter_manual_session("user declined automation at startup")
            return 0

        name = (form.get("name") or "").strip()
        directory = (form.get("dir") or "").strip()
        if not name or not directory:
            _log(f"Incomplete project form (name={name!r}, dir={directory!r}); skipping.")
            _enter_manual_session("incomplete project form at startup")
            return 0

        _log(f"Creating project '{name}' in '{directory}'...")
        if not create_project(syn, name, directory):
            _log("Project creation failed; skipping import step.")
            return 0
        _log("Project created successfully.")
        ui_bridge.update_state("params", {"project_name": name, "location": directory})

        _log("Requesting model file import...")
        file_path = ui_bridge.request_file_path("Import Model", filters=IMPORT_EXT_FILTERS)
        if not file_path:
            _log("No file selected. Done.")
            return 0

        _log(f"Importing '{file_path}'...")
        if import_model(syn, file_path):
            set_isometric_view(syn)
            _log("Import completed successfully.")
            # The automation does NOT stop here: moldflow_observer sees the new
            # CAD bodies on its next poll and launches cad_diagnostics.py, which
            # then attaches to Synergy and runs the checks before its card can
            # appear. That is several seconds during which the panel used to show
            # CAD Diagnostics as "Pending..." with nothing else happening, so the
            # window arriving afterwards looked unprompted. Say so immediately --
            # the observer and the diagnostics process each refine this message
            # as they take over.
            _mark_diagnostics_starting()
        else:
            _log("Import failed or was cancelled in the native import dialog.")

        return 0

    except Exception:
        tb = traceback.format_exc()
        _log("STARTUP FAILED:\n" + tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
