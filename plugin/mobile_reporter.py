# plugin/mobile_reporter.py
# -------------------------
# Backward-compatible shim.
#
# The canonical mobile reporting implementation now lives in:
#   lib/mobile/reporter.py
#
# This file re-exports everything from the canonical source so that all
# existing callers (cad_diagnostics.py, monitor, and any other plugin code
# that imports mobile_reporter) continue to work without any changes.
#
# DO NOT add new logic here. Add it to lib/mobile/reporter.py instead.

import sys
from pathlib import Path as _Path

# Ensure the repository root is on sys.path so lib/ is importable.
_repo_root = str(_Path(__file__).resolve().parents[1])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Re-export the public API and compatibility helpers from the canonical module.
from lib.mobile.reporter import (  # noqa: F401, E402
    CONFIG_PATH,
    HTTP_TIMEOUT,
    MIN_RESEND_INTERVAL,
    enabled,
    report_status,
    check_cancel,
    list_active_jobs,
    _percent_bucket,
    _should_send,
    _load_config,
    _find_config_path,
)
