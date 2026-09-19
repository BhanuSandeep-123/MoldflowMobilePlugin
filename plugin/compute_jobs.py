# plugin/compute_jobs.py
# -----------------------
# Backward-compatible shim.
#
# The canonical SCM client implementation now lives in:
#   lib/scm/client.py
#
# This file re-exports everything from the canonical source so that all
# existing callers (cad_diagnostics.py and any other plugin code that
# imports compute_jobs) continue to work without any changes.
#
# DO NOT add new logic here. Add it to lib/scm/client.py instead.

import sys
from pathlib import Path as _Path

# Ensure the repository root is on sys.path so lib/ is importable.
_repo_root = str(_Path(__file__).resolve().parents[1])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Re-export the full public API.
from lib.scm.client import (  # noqa: F401, E402
    DEFAULT_PORT,
    BASE_PATH,
    HTTP_TIMEOUT,
    SOLVE_TYPES,
    MESH_TYPES,
    TERMINAL_STATUSES,
    base_url,
    available,
    list_jobs,
    get_job,
    cancel_job,
    job_ids,
    find_job,
    summarize,
    phases,
    viewer_exe,
    open_viewer,
)
