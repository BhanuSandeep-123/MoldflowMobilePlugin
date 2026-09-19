"""
run_diagnostics.py
------------------
Run CAD Diagnostics on the CURRENTLY OPEN Synergy study, print the result, and
stop.

Assign this to a macro button in Synergy. Open a project and a study, click the
button, read the numbers.

WHAT IT DOES NOT DO
    No repair prompt. No Fusion round trip. No automation workflow. Nothing
    about the study is changed and nothing is sent anywhere. That is the whole
    point: the same study can be measured twice and the numbers compared. If
    two consecutive runs on an untouched study disagree, the diagnostic itself
    is unstable; if they agree, the geometry really does contain what it
    reports.

    For the full automated workflow (diagnostics -> repair prompt -> Fusion
    round trip -> meshing -> solve), use the normal startup path instead. This
    is the read-only measurement tool.

HOW IT WORKS
    It sets MF_CHECK_ONLY before importing cad_diagnostics, so the exact same
    measurement code runs as in the full workflow -- same entity-list build,
    same CADDiagnostic.Compute call, same result parsing. Nothing is
    reimplemented here, because a second implementation would measure something
    subtly different and the comparison would be worthless.

MUST BE RUN FROM INSIDE SYNERGY
    Synergy only exposes its automation object to scripts it launches itself,
    via the SAInstance environment variable. Running this from a plain terminal
    always fails with "No Moldflow Synergy session available".

OUTPUT
    Printed to the console, and appended to the plugin's logs\\ folder along
    with cad_diagnostics_report.json.
"""

import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Must be set BEFORE importing cad_diagnostics -- its CHECK_ONLY constant is
# evaluated at import time.
os.environ["MF_CHECK_ONLY"] = "1"
if "--check-only" not in sys.argv:
    sys.argv.append("--check-only")


def _pause():
    """Keep the window open when a human is watching, never block a headless
    run (input() on a closed stdin would hang the macro forever)."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nPress Enter to close...")
    except Exception:
        pass


def main():
    print("=" * 62)
    print(" CAD Diagnostics - check only (nothing will be modified)")
    print("=" * 62)

    try:
        import cad_diagnostics
    except Exception:
        print("\nCould not import cad_diagnostics.py from:\n  {0}\n".format(HERE))
        traceback.print_exc()
        _pause()
        return 1

    if not getattr(cad_diagnostics, "CHECK_ONLY", False):
        # Refuse rather than silently running the full workflow -- that would
        # launch Fusion and change the study, which is the opposite of the
        # intent here.
        print("\nERROR: cad_diagnostics did not pick up check-only mode.")
        print("Refusing to continue, because the full automation workflow would")
        print("start a Fusion round trip and modify the study.")
        _pause()
        return 1

    try:
        cad_diagnostics.main()
    except SystemExit:
        raise
    except Exception:
        print("\nDiagnostics failed:\n")
        traceback.print_exc()
        _pause()
        return 1

    print("\n" + "=" * 62)
    print(" Done. Run this again on the SAME study and compare the numbers.")
    print("=" * 62)
    _pause()
    return 0


if __name__ == "__main__":
    sys.exit(main())
