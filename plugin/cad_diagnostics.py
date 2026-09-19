"""
cad_diagnostics.py
-------------------
Runs CAD diagnostics on the active Moldflow Synergy study CAD bodies.
Logs results and writes cad_diagnostics_report.json.
Supports both native 'moldflow' python import and 'synergy_connect' COM wrappers.
"""

from pathlib import Path
import json
import traceback
import datetime
import os
import re
import sys

# Add current directory to path so synergy_connect can be found if needed
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Per-Synergy-window scratch space. The generated dialog templates
# (_automation_prompt.hta, _proc_settings.hta, _seq_select.vbs, ...) and the
# result files they write back through used to be written to ONE fixed path
# next to this script. With two Synergy windows open, whichever workflow wrote
# last replaced the template the other was about to show, and both mshta
# processes wrote their answers into the same result file -- so one window's
# answer landed in the other window's workflow, and the template itself was
# deleted out from under the dialog still using it. See session_context.py.
import session_context
SESSION_DIR = session_context.session_dir()

# CHECK-ONLY MODE (run_check.vbs / MF_CHECK_ONLY=1 / --check-only)
# Runs the CAD diagnostic checks on the active study, writes the report, and
# STOPS. No repair prompt, no Fusion round trip, no automation workflow.
# Exists so a study can be measured repeatedly without the act of measuring it
# changing anything -- which is exactly what is needed to tell a genuine
# geometry defect apart from an unstable diagnostic.
CHECK_ONLY = (os.environ.get("MF_CHECK_ONLY", "").strip() not in ("", "0")
              or "--check-only" in sys.argv)

# ---------------------------------------------------------------------------
# Dynamic Member Resolution (snake_case -> PascalCase COM translation)
# ---------------------------------------------------------------------------
def snake_to_pascal(name):
    return "".join(word.capitalize() for word in name.split("_"))

def get_member(obj, name):
    # Try as-is
    try:
        return getattr(obj, name)
    except AttributeError:
        pass
        
    # Try PascalCase
    pascal_name = snake_to_pascal(name)
    try:
        return getattr(obj, pascal_name)
    except AttributeError:
        pass
        
    # Try uppercase acronym fallback (e.g. cad_diagnostic -> CADDiagnostic)
    if "cad" in name.lower():
        acronym_name = pascal_name.replace("Cad", "CAD")
        try:
            return getattr(obj, acronym_name)
        except AttributeError:
            pass
            
    raise AttributeError(f"Attribute '{name}' not found on {obj}")

def call_member(obj, name, *args):
    member = get_member(obj, name)
    if callable(member):
        return member(*args)
    return member

def _prop_id(prop):
    """Occurrence ID of a property as int, or None (win32com may expose
    Property.ID as either an attribute or a callable)."""
    try:
        v = prop.ID
        return int(v() if callable(v) else v)
    except Exception:
        return None

def _iter_props_of_type(pe, ptype):
    """Yield every property occurrence of the given type."""
    try:
        p = pe.GetFirstProperty(ptype)
    except Exception:
        return
    while p is not None:
        yield p
        try:
            p = pe.GetNextPropertyOfType(p)
        except Exception:
            return

def _com_is_dead(exc):
    """True if a COM error means the Synergy connection is gone
    (RPC unavailable / call failed / connection lost). Transient
    RPC_E_CALL_REJECTED is deliberately NOT treated as dead."""
    try:
        hr = getattr(exc, "hresult", None)
        if hr is None and getattr(exc, "args", None):
            hr = exc.args[0]
        return (int(hr) & 0xFFFFFFFF) in (0x800706BA, 0x800706BE, 0x80010108)
    except Exception:
        return False

def _exit_if_com_dead(exc, context=""):
    """If the Synergy connection is dead, stop the automation IMMEDIATELY.

    The gate-placement and node-count pollers used to swallow dead-COM
    errors and return 0/empty, so after the user closed Moldflow the loop
    kept re-showing its prompts forever against a dead server — orphaned
    python processes piling up in the taskbar, each nagging with dialogs.
    A dead connection can never recover (the moniker dies with the
    session), so exiting is the only correct behaviour."""
    if _com_is_dead(exc):
        try:
            print("Synergy connection lost{0}: {1} — stopping the automation "
                  "process.".format(" ({0})".format(context) if context else "",
                                    exc), flush=True)
        except Exception:
            pass
        os._exit(0)

def _start_synergy_watchdog():
    """Kill this automation process — INCLUDING any open dialog child
    (mshta / cscript) — as soon as Moldflow Synergy closes.

    Pure Win32 (no COM): a daemon thread watches for a visible top-level
    window belonging to synergy.exe. Once such a window has been seen and
    then stays absent for ~6s, Synergy is gone: a blocking MessageBox or
    mshta dialog would otherwise keep this process (and its prompts) alive
    indefinitely. taskkill /T takes down the whole process tree, so the
    dialogs vanish with us; os._exit is the fallback."""
    import threading
    import time as _t
    import ctypes
    import ctypes.wintypes as wt
    import subprocess as _sp

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    def _is_synergy_pid(pid):
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

    def _window_present():
        found = {"hit": False}

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wt.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value and _is_synergy_pid(pid.value):
                    found["hit"] = True
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(_cb, 0)
        except Exception:
            return True  # never kill on a probe failure
        return found["hit"]

    def _run():
        seen = False
        misses = 0
        while True:
            try:
                if _window_present():
                    seen = True
                    misses = 0
                elif seen:
                    misses += 1
                    if misses >= 3:
                        try:
                            print("Moldflow Synergy closed — terminating the "
                                  "automation process and its dialogs.",
                                  flush=True)
                        except Exception:
                            pass
                        try:
                            _sp.run(
                                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                                creationflags=_sp.CREATE_NO_WINDOW
                                if os.name == "nt" else 0)
                        except Exception:
                            pass
                        os._exit(0)
            except Exception:
                pass
            _t.sleep(2)

    threading.Thread(target=_run, daemon=True,
                     name="synergy-watchdog").start()

# --------------------------------------------------------------------------
# Solver-message decoding.
#
# Moldflow's per-job `.out` files are NUMERIC message-code streams (almost no
# readable text), so grepping them for "** ERROR **" finds nothing.  The human
# reason for a failure is only in the tiny `<study>~<job>.err` files as
# `IDMSG=<id>` codes, which resolve to text against the install's message
# catalog `...\data\dat\cmmesage.dat` (blocks: `MSCD <id> ...` then the
# indented message up to a `----` separator).  This is why a Cool (FEM) run on
# a study with no mold block failed with the opaque "analysis reported a
# failure status" — the real message, `** ERROR 312060 ** No mold block (3D)
# mesh found`, was sitting decodable in the .err the whole time.
# --------------------------------------------------------------------------
_MSG_DB_CACHE = {}  # cmmesage.dat path -> {id: (severity, text)}

def _find_cmmesage_dat():
    """Locate cmmesage.dat in any installed Moldflow Synergy, newest first."""
    roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Autodesk",
             Path(r"C:\Program Files\Autodesk")]
    seen = set()
    for root in roots:
        if not root.exists() or root in seen:
            continue
        seen.add(root)
        cands = sorted(root.glob("Moldflow Synergy */data/dat/cmmesage.dat"),
                       reverse=True)
        if cands:
            return cands[0]
    return None

def _load_moldflow_messages():
    """Parse cmmesage.dat into {id: (severity, text)} (cached)."""
    path = _find_cmmesage_dat()
    if path is None:
        return {}
    key = str(path)
    if key in _MSG_DB_CACHE:
        return _MSG_DB_CACHE[key]

    db = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        _MSG_DB_CACHE[key] = db
        return db

    mid = None
    buf = []

    def _flush(mid, buf):
        if mid is None:
            return
        # Keep the human sentence(s); drop trailing arg-type descriptor lines
        # like "K,1,1" / "s,2,2" that follow messages with %f placeholders.
        lines = [ln.strip() for ln in buf
                 if ln.strip() and not re.match(r"^\S{0,4},\d+,\d+$", ln.strip())]
        if not lines:
            return
        joined = " ".join(lines)
        sev = "INFO"
        m = re.search(r"\*\*\s*(ERROR|WARNING)\s*\d+\s*\*\*", joined)
        if m:
            sev = m.group(1)
        # Strip the "** ERROR 312060 **" banner for a clean sentence.
        clean = re.sub(r"\*\*\s*(?:ERROR|WARNING)\s*\d+\s*\*\*\s*", "", joined).strip()
        db[mid] = (sev, clean or joined)

    for raw in text.splitlines():
        m = re.match(r"^MSCD\s+(\d+)\b", raw)
        if m:
            _flush(mid, buf)
            mid = int(m.group(1))
            buf = []
        elif raw.strip() == "----":
            _flush(mid, buf)
            mid = None
            buf = []
        elif mid is not None:
            buf.append(raw)
    _flush(mid, buf)

    _MSG_DB_CACHE[key] = db
    return db

def decode_solver_err(project_dir, study_stem, since_mtime=0.0):
    """Human-readable solver message(s) from this run's `<stem>~*.err` files.

    Returns (severity, text) where severity is 'ERROR' if any error was found
    (preferred for a failure dialog), else the first message seen, else None.
    Only `.err` files modified at/after `since_mtime` are read so a stale error
    from a previous run can't be misattributed.
    """
    try:
        project_dir = Path(project_dir)
        if not project_dir.exists():
            return None
    except Exception:
        return None

    db = _load_moldflow_messages()
    errors, others = [], []
    # The STUDY name can itself carry a ~N suffix (a copy), while the job files
    # get their own: 'unoteam_study~52.sdy' solves into 'unoteam_study~256.err'.
    # Globbing the raw stem looks for 'unoteam_study~52~*.err' and finds
    # nothing, which is why a live 14:34 failure reported no reason at all.
    study_stem = re.sub(r"~\d+$", "", str(study_stem))
    try:
        err_files = sorted(project_dir.glob("{0}~*.err".format(study_stem)))
    except Exception:
        err_files = []

    for ef in err_files:
        try:
            if since_mtime and ef.stat().st_mtime < since_mtime - 2:
                continue
            content = ef.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for mid in re.findall(r"IDMSG=(\d+)", content):
            entry = db.get(int(mid))
            if not entry:
                continue
            sev, txt = entry
            label = "{0} (message {1})".format(txt, mid)
            (errors if sev == "ERROR" else others).append(label)

    if errors:
        # Preserve order, drop duplicates.
        seen, uniq = set(), []
        for e in errors:
            if e not in seen:
                seen.add(e); uniq.append(e)
        return ("ERROR", "\n".join(uniq))
    if others:
        return ("WARNING", others[0])
    return None


# Message ids from `...\Moldflow Synergy 2027\data\dat\cmmesage.dat` that the
# Gate Location analysis emits (verified against a real run's .out, 2026-08-21):
#   1204085  "Number of Potential Gate Locations  = %10d"
#   1200005  "Searching optimum gate locations: %d%% done"
#   1200070  "Recommended gate location(s) are:"      (no arguments)
#   1200080  "Near node                           = %11.0f"  (1 argument)
GATE_MSG_NEAR_NODE = "1200080"


def _parse_gate_nodes_numeric(text):
    """Recommended node labels from a NUMERIC solver .out stream.

    Moldflow's `.out` is not prose -- it is a stream of cmmesage.dat message
    codes that Synergy renders into readable text only when it displays the
    Analysis Log.  Each record is four-ish lines:

        1200080     <- message id ("Near node = %11.0f")
        0           <- flags
        1           <- argument count
        16619       <- the argument: the node label

    So the node is read positionally from the record, not matched as text.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    found = []
    i = 0
    while i < len(lines):
        if lines[i] != GATE_MSG_NEAR_NODE:
            i += 1
            continue
        try:
            count = int(lines[i + 2])
        except (IndexError, ValueError):
            i += 1
            continue
        # A sane argument count is the guard against mistaking a bare 1200080
        # somewhere else in the stream for a real record.
        if not 1 <= count <= 8:
            i += 1
            continue
        try:
            # The label is formatted %11.0f, so it can arrive as '16619.0'.
            node = int(float(lines[i + 3]))
        except (IndexError, ValueError):
            i += 1
            continue
        if node > 0 and node not in found:
            found.append(node)
        i += 3 + count
    return found


def _parse_gate_nodes_prose(text):
    """Recommended node labels from a READABLE gate-location log, i.e. text
    copied out of Synergy's Analysis Log tab or written by a build that
    renders the .out itself.

    Only lines following the 'Recommended gate location' heading count --
    'node' appears elsewhere in the log, and matching it loosely would hand
    the gate placer an arbitrary number from the mesh summary."""
    heading = re.compile(r"recommended\s+gate\s+location", re.I)
    near = re.compile(r"near\s+node\s*=\s*(\d+)", re.I)
    # Anything that is neither blank nor a 'Near node' line ends the block,
    # so a later section of the log cannot leak nodes into the result.
    found, in_block = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if heading.search(stripped):
            in_block = True
            continue
        if not in_block:
            continue
        if not stripped:
            continue
        m = near.search(stripped)
        if m:
            n = int(m.group(1))
            if n not in found:
                found.append(n)
            continue
        in_block = False
    return found


def com_nothing():
    """VBScript `Nothing` for a COM object argument, or None if unavailable.

    Autodesk's own `data\\commands\\injpts_3d.vbs` creates injection points
    with `BoundaryConditions.CreateNDBC(Ent, Normal, InjTcodeSet, Nothing)` --
    the fourth argument being VB's null object reference, which tells Synergy
    to create or select the property itself.

    Python's `None` is NOT that. The Syn wrapper passes arguments straight to
    `IDispatch::Invoke`, where `None` marshals as VT_EMPTY/VT_NULL and Synergy
    rejects it with "Type mismatch" on argument 4 (observed live 2026-08-21).
    The real equivalent is an explicit null IDispatch VARIANT -- which is what
    Autodesk's own Python wrapper builds in
    `moldflow/helper.py: variant_null_idispatch()`.
    """
    try:
        import pythoncom
        from win32com.client import VARIANT
        return VARIANT(pythoncom.VT_DISPATCH, None)
    except Exception:
        return None


def parse_recommended_gate_nodes(text):
    """Mesh-node labels Moldflow's Gate Location analysis recommends.

    Handles BOTH forms the log can take: the numeric message-code stream that
    the solver actually writes to disk, and readable prose (what Synergy shows
    in its Analysis Log tab, and what a user pastes when reporting a run).

    Returns node labels as ints, in the solver's own order, duplicates
    removed.  Empty list when the log holds no recommendation.
    """
    if not text:
        return []
    return _parse_gate_nodes_prose(text) or _parse_gate_nodes_numeric(text)


# Smallest node count we will accept as a genuine 3D mesh.  Healthy runs of
# this workflow report ~22,600-22,850 nodes; a dying COM server has been
# observed returning 1 alongside a 'Completed' mesh status, which previously
# sailed through as success and left the solver running against a dead
# session.  Any real tetrahedral mesh clears this floor by orders of
# magnitude, so it only ever rejects garbage.
MIN_MESH_NODES = 50

# ---------------------------------------------------------------------------
# Engineering reference for the reported results.
#
# Source: Autodesk's "Top 12 Results to View from a Cool + Flow + Warp
# Simulation" review deck. The deck defines the master engineering result
# categories, what each plot means, what to investigate in it, and the
# recommended targets. It is a REFERENCE ONLY — nothing here drives which
# results are exported; availability is still decided at runtime from the
# study's own datasets, so a sequence that cannot produce a result simply
# reports it as missing.
#
# Keys MUST match the display labels in `result_specs` exactly.
# `unit` is the SI storage unit from data\dat\results.dat and is used to
# format the extracted min/max values (K -> degC, Pa -> MPa, N -> kN, m -> mm).
# ---------------------------------------------------------------------------
RESULT_GUIDE = {
    "Fill time": {
        "unit": "s",
        "description": (
            "Contours of the melt front at successive instants, illustrating "
            "the flow pattern of the resin filling the mould cavity."),
        "purpose": (
            "Confirm the cavity fills completely and in balance, and expose "
            "filling-phase defects before the tool is cut."),
        "investigate": [
            "Short shot — does the part fill completely?",
            "Race-tracking and hesitation.",
            "Flow lines, hesitation lines and gate blush.",
            "Melt front speed: wide contour spacing means relatively fast "
            "speed, narrow spacing means relatively slow speed.",
            "Weld lines and air traps formed by the fill pattern.",
        ],
        "targets": (
            "Contours should be evenly spaced and all extremities of the part "
            "should fill at the same time."),
    },
    "Injection pressure": {
        "unit": "Pa",
        "description": (
            "The plastic injection pressure required to fill the mould cavity, "
            "calculated as if measured from the nozzle."),
        "purpose": (
            "Check the pressure demand against the moulding machine's "
            "capability and locate regions that will be over-packed."),
        "investigate": [
            "Is the pressure required to fill the mould higher than the "
            "pressure available from the moulding machine?",
            "Locate areas of possible hydrostatic pressure — they tend to be "
            "over-packed.",
            "Develop an appropriate pack profile that accounts for areas that "
            "are solidifying, so density stays consistent.",
        ],
        "targets": (
            "Target 75% maximum of the machine's available pressure. As a "
            "basic guide the cavity should fill at less than 80 MPa, and the "
            "cavity and feed system combined should not exceed 100 MPa."),
    },
    "Clamp force": {
        "unit": "N",
        "description": "The clamp force required over the moulding cycle.",
        "purpose": (
            "Confirm the machine can hold the mould closed throughout filling "
            "and packing."),
        "investigate": [
            "Is the clamp force required to keep the mould closed exceeded "
            "during filling or packing? The line of draw should be in the "
            "'Z' direction.",
            "Exceeded in filling may cause flashing.",
            "Exceeded in packing may cause larger dimensions, parts sticking "
            "in the tool, and problems ejecting.",
        ],
        "targets": (
            "Peak clamp force should stay within the machine's rating with "
            "margin in both the filling and packing phases."),
    },
    "Bulk temperature": {
        "unit": "K",
        "description": (
            "Velocity-weighted part temperature averaged through the "
            "thickness."),
        "purpose": (
            "Track the melt's thermal history to set gate freeze time and "
            "overall cycle time."),
        "investigate": [
            "Areas during filling that show excessive heating or cooling.",
            "Optimal gate freeze time — the point at which the gate reaches "
            "the material transition temperature.",
            "Optimal cycle time for the moulding.",
        ],
        "targets": (
            "Compare against the grade's transition temperature (gate freeze) "
            "and its ejection temperature (cycle time). The reference deck's "
            "worked example uses a transition temperature of 135 degC and an "
            "ejection temperature of 93.5 degC."),
    },
    "Shear rate": {
        "unit": "1/s",
        "description": (
            "A measure of how quickly the layers of plastic are sliding past "
            "each other."),
        "purpose": (
            "Prevent polymer chain breakage and material degradation, which "
            "degrade the mechanical properties of the part."),
        "investigate": [
            "Shear rate in the runner, gate and part should not exceed the "
            "manufacturer's recommended level.",
            "The highest values are typically found in the gates and sprue.",
            "Excessive gate shear risks shear burning — the gate size would "
            "have to be enlarged.",
        ],
        "targets": (
            "Use the grade's own recommended limit. The reference deck's "
            "worked example uses a maximum allowable shear rate of "
            "24,000 1/s."),
    },
    "Shear stress": {
        "unit": "Pa",
        "description": (
            "The shear force at the solid-liquid interface per unit area, "
            "proportional to the pressure gradient at each location."),
        "purpose": (
            "Limit the residual stress locked into the part, which drives "
            "premature failure."),
        "investigate": [
            "Shear stress in the part should not exceed the manufacturer's "
            "recommended level.",
            "High non-uniform shear stress becomes locked in as higher "
            "residual stress, which can lead to premature failures and "
            "stress cracking.",
        ],
        "targets": (
            "As a guide, roughly 1% of the material's tensile strength. The "
            "reference deck's worked example uses a maximum allowable shear "
            "stress of 0.24 MPa."),
    },
    "Orientation": {
        "unit": "",
        "description": (
            "The direction in which the polymer chains — or the fibres in a "
            "filled grade — are aligned by the flow."),
        "purpose": (
            "Relate the part's strength and flexibility to the direction of "
            "flow."),
        "investigate": [
            "Does the orientation take advantage of the material's strength "
            "in the direction of flow, or does it represent possible failure "
            "under loading?",
        ],
        "targets": (
            "Orientation should align with the part's principal load paths."),
    },
    "Volumetric shrinkage": {
        "unit": "%",
        "description": (
            "The volumetric shrinkage of each element, as a percent of its "
            "original volume."),
        "purpose": (
            "Predict sinks and voids, and quantify the shrinkage variation "
            "that drives warpage."),
        "investigate": [
            "Potential sinks and voids created by high localised shrinkage. "
            "Read the high-shrinkage areas together with the very hot spots "
            "in Top temperature, part — where the two coincide is where a "
            "sink is likely to appear on the moulded surface.",
            "Over-packing that creates negative shrinkage values.",
            "Differences in packing that create more than a 2:1 difference "
            "across the main areas.",
            "Uneven volumetric shrinkage throughout the part indicates the "
            "likelihood of unacceptable warpage.",
        ],
        "targets": (
            "Average volumetric shrinkage should be approximately 3 times the "
            "expected linear shrinkage — e.g. for PP with 1.5% linear "
            "shrinkage, target roughly 4.5% volumetric."),
    },
    "Frozen layer thickness": {
        "unit": "",
        "description": (
            "A higher value represents a thicker frozen layer and therefore a "
            "thinner polymer melt (flow) layer."),
        "purpose": (
            "Understand flow resistance and whether the part can be packed "
            "adequately."),
        "investigate": [
            "During filling the frozen layer should maintain a constant "
            "thickness.",
            "A higher value indicates areas that are near or have actually "
            "frozen during the filling phase, which may prevent parts from "
            "being packed sufficiently, if at all, in certain areas.",
            "Identifies areas of hesitation within the part.",
        ],
        "targets": (
            "Frozen-layer thickness has very significant effects on flow "
            "resistance: viscosity increases exponentially with decreasing "
            "temperature. A 50% reduction in part thickness reduces fluidity "
            "by a factor of eight; in runners a 50% reduction reduces it by a "
            "factor of 16."),
    },
    "Deflection (all effects)": {
        "unit": "m",
        "description": (
            "The potential shrinkage and warpage in the final moulded part, "
            "combining all contributing effects."),
        "purpose": (
            "Verify dimensional conformance and identify what is driving the "
            "warpage."),
        "investigate": [
            "Are the warpage values within tolerance?",
            "What is the main cause of warpage? Compare the all-effects "
            "result against the individual cooling-effects and "
            "shrinkage-effects plots to isolate the driver.",
        ],
        "targets": (
            "Deflection should fall within the part's dimensional tolerance."),
    },
    "Top temperature, part": {
        "unit": "K",
        "description": (
            "The average temperature of the plastic/mould interface at the top "
            "side of the part element, during the cycle."),
        "purpose": (
            "Expose the cooling imbalance that causes warpage and surface "
            "defects."),
        "investigate": [
            "High localised temperature regions that can cause issues with "
            "sink and voiding.",
            "Too much variation in top temperature can cause warpage issues "
            "due to cooling imbalances.",
        ],
        "targets": (
            "Aim for less than 20 degC variation across the part."),
    },
    "Average temperature, part": {
        "unit": "K",
        "description": (
            "The average of the temperature profile across the part "
            "thickness, calculated at the end of the cooling time."),
        "purpose": (
            "Confirm the part is cool enough to eject and find the hot spots "
            "that limit the cycle."),
        "investigate": [
            "Check that the temperature at the end of the cycle is below the "
            "material's ejection temperature.",
            "What areas are still above the ejection temperature and may be "
            "limiting the cycle?",
        ],
        "targets": (
            "Below the grade's ejection temperature. The reference deck's "
            "worked example uses an ejection temperature of 93.5 degC."),
    },

    # -----------------------------------------------------------------
    # Supporting investigations. The reference deck does not stop at the
    # 12 headline plots — it devotes further slides to specific
    # investigations WITHIN a headline result (air traps and weld lines
    # under Fill time, sinks under Volumetric shrinkage, the deflection
    # cause breakdown under Deflection). These are reported as
    # sub-sections of their parent result, never as standalone results.
    # See SUB_SPECS below for the parent -> investigation mapping.
    # -----------------------------------------------------------------
    "Air traps": {
        "unit": "",
        "description": (
            "Locations where the advancing melt fronts converge and trap air "
            "against the cavity wall."),
        "purpose": (
            "Part of the Fill time review: confirm where the fill pattern "
            "traps air so the tool can be vented there."),
        "investigate": [
            "A significant air trap can cause a short shot if it is not "
            "vented.",
            "Read the air traps together with the fill pattern — the fill "
            "sequence is what creates them.",
        ],
        "targets": (
            "Air traps should fall on a parting line or an ejector pin where "
            "they can vent. Those that do not need a vent, a gate change, or "
            "a fill-pattern change."),
    },
    "Weld lines": {
        "unit": "",
        "description": (
            "Lines where two melt fronts meet, forming a potential structural "
            "and cosmetic weakness."),
        "purpose": (
            "Part of the Fill time review: locate the weld lines the fill "
            "pattern creates and judge their severity."),
        "investigate": [
            "Where the weld line starts, and how far the defect continues.",
            "Whether the weld line falls on a cosmetic surface or in a "
            "load-bearing area.",
        ],
        "targets": (
            "Move weld lines away from cosmetic surfaces and high-stress "
            "regions by relocating the gate or rebalancing the fill."),
    },
    "Pressure at end of fill": {
        "unit": "Pa",
        "description": (
            "The pressure distribution across the part at the instant filling "
            "finishes."),
        "purpose": (
            "Part of the Injection pressure review: identify the end-of-fill "
            "areas that become pressurised, which are the hydrostatic regions "
            "prone to over-packing."),
        "investigate": [
            "End-of-fill areas that become pressurised tend to be "
            "over-packed.",
            "Use the pressure trace at the end of fill to set the start of "
            "the pack pressure decay.",
        ],
        "targets": (
            "Pressure should decay evenly; large retained pressure indicates "
            "an over-packed region."),
    },
    "Deflection, differential cooling": {
        "unit": "m",
        "description": (
            "The component of the total deflection caused by uneven cooling "
            "across the part."),
        "purpose": (
            "Part of the Deflection review: isolates cooling as a warpage "
            "cause so the fix targets the right thing."),
        "investigate": [
            "Compare against the all-effects deflection — if this component "
            "dominates, the cooling layout is the problem.",
            "Cross-check against the part temperature results to find the "
            "imbalance causing it.",
        ],
        "targets": (
            "Should be a small fraction of the total deflection; if it "
            "dominates, rework the cooling circuits."),
    },
    "Deflection, differential shrinkage": {
        "unit": "m",
        "description": (
            "The component of the total deflection caused by uneven "
            "shrinkage across the part."),
        "purpose": (
            "Part of the Deflection review: isolates shrinkage as a warpage "
            "cause."),
        "investigate": [
            "Compare against the all-effects deflection — if this component "
            "dominates, address packing and volumetric shrinkage uniformity.",
        ],
        "targets": (
            "Should be a small fraction of the total deflection; if it "
            "dominates, rebalance the pack profile."),
    },
}

# ---------------------------------------------------------------------------
# The reference deck's engineering STRUCTURE, not just its titles: each
# headline result carries further investigations on its own slides, and those
# belong inside that result's section of the report.
#
#   Fill time              -> Air traps (slide 7), Weld lines (slide 8)
#   Injection pressure     -> Pressure at end of fill / hydrostatic (slide 10)
#   Bulk temperature       -> Time to reach ejection temperature (slide 16)
#   Shear rate             -> Shear rate, maximum (slide 18)
#   Volumetric shrinkage   -> Sink marks, Average volumetric shrinkage (22-23)
#   Frozen layer thickness -> Frozen layer fraction at end of fill (slide 25)
#   Deflection             -> cooling / shrinkage / orientation causes (27)
#
# A sub-result is only reported when its PARENT was produced — it is part of
# that result's review, never a standalone entry. Like the headline results,
# each one is exported only if the study actually produced it.
# ---------------------------------------------------------------------------
SUB_SPECS = {
    # Slide 7 "Fill Time (Air Trap)" and slide 8 "Fill Time (Weld Line)" each
    # show a DIFFERENT Moldflow result from the fill-time contour itself.
    "Fill time": [
        ("Air traps", ["Air traps", "Air traps (3D)"]),
        ("Weld lines", ["Weld lines", "Weld surface formation (3D)",
                        "Weld surface movement (3D)"]),
    ],
    # Slide 10 "Injection Pressure (Hydrostatic)" — the end-of-fill pressure
    # field, showing which areas stay pressurised.
    "Injection pressure": [
        ("Pressure at end of fill", ["Pressure at end of fill"]),
    ],
    # Slide 27 lays out the deflection cause breakdown as three separate
    # plots: Cooling Effects, Shrinkage Effects, Total Deflection. Only those
    # two causes are shown — orientation effects is NOT on that slide.
    "Deflection (all effects)": [
        ("Deflection, differential cooling", ["Deflection, differential cooling"]),
        ("Deflection, differential shrinkage", ["Deflection, differential shrinkage"]),
    ],
}

# ---------------------------------------------------------------------------
# Which ANALYSIS PHASE each result belongs to, so the deck can group the
# results the way the reference report does — "Fill and Pack results",
# "Cooling Analysis Results", "Warp / Deflection Results" — instead of one
# undifferentiated run of slides.
#
# This is presentation only. It does not decide what is exported, what is
# captured or in what order: the slide sequence within a phase is exactly the
# order `captured` already had. A phase with no results produces no divider,
# so a Fill-only study is unaffected.
#
# Supporting investigations are NOT listed here — they inherit their parent's
# phase through `parent_of`, which is what keeps a sub-result in the same
# section as the result it belongs to.
# ---------------------------------------------------------------------------
PHASE_OF = {
    "Fill time": "fill_pack",
    "Injection pressure": "fill_pack",
    "Clamp force": "fill_pack",
    "Bulk temperature": "fill_pack",
    "Shear rate": "fill_pack",
    "Shear stress": "fill_pack",
    "Orientation": "fill_pack",
    "Volumetric shrinkage": "fill_pack",
    "Frozen layer thickness": "fill_pack",
    "Top temperature, part": "cooling",
    "Average temperature, part": "cooling",
    "Deflection (all effects)": "warpage",
}

# A finding names the DATASET it came from, not a Top-12 label, so the phase
# summaries need their own mapping. Substring match, most specific first;
# anything unrecognised belongs to the fill phase, which is the only phase
# every sequence has.
_DATASET_PHASE = [
    ("time to reach ejection temperature", "cooling"),
    ("temperature, part", "cooling"),
    ("circuit", "cooling"),
    ("coolant", "cooling"),
    ("cooling", "cooling"),
    ("deflection", "warpage"),
    ("warp", "warpage"),
    ("mises", "warpage"),
    # NOT "shrinkage": volumetric shrinkage drives warpage but is a PACKING
    # result, and PHASE_OF puts its slide in the fill/pack section. A finding
    # filed under a different phase from the plot it is about sends the reader
    # to the wrong section.
]


def phase_of_dataset(name):
    """Which analysis phase a summary finding belongs to."""
    n = (name or "").strip().lower()
    for fragment, phase in _DATASET_PHASE:
        if fragment in n:
            return phase
    return "fill_pack"


# Display order of the phases, with the divider text for each.
PHASE_SECTIONS = [
    ("fill_pack", "Fill and pack results",
     "How the cavity fills, and the conditions it fills under"),
    ("cooling", "Cooling analysis results",
     "How the part loses its heat, and what limits the cycle"),
    ("warpage", "Warpage results",
     "How the part deforms once it leaves the tool"),
]

# ---------------------------------------------------------------------------
# Why a Top 12 result can be absent, so the closing slide states the real
# reason for THIS study instead of a generic catch-all sentence.
# ---------------------------------------------------------------------------
MISSING_REASON = {
    "Fill time": "needs a Fill phase in the analysis sequence",
    "Injection pressure": "needs a Fill phase in the analysis sequence",
    "Clamp force": "needs a Fill phase in the analysis sequence",
    "Bulk temperature": "needs a Fill phase in the analysis sequence",
    "Shear rate": "needs a Fill phase in the analysis sequence",
    "Shear stress": "needs a Fill phase in the analysis sequence",
    "Orientation": ("needs a fibre-filled grade for the fibre orientation "
                    "tensor, or a Midplane/Dual Domain mesh for molecular "
                    "orientation at skin and core"),
    "Volumetric shrinkage": "needs a Pack phase in the analysis sequence",
    "Frozen layer thickness": "needs a Fill phase in the analysis sequence",
    "Deflection (all effects)": "needs a Warp analysis in the analysis sequence",
    "Top temperature, part": ("needs a Cool analysis, with a cooling circuit "
                              "defined in the model"),
    "Average temperature, part": ("needs a Cool analysis, with a cooling "
                                  "circuit defined in the model"),
}

# ---------------------------------------------------------------------------
# Consolidated "Automation Workflow" dialog.
#
# Replaces the old two-popup sequence (a message box dumping the whole
# diagnostics log, then a separate Yes/No prompt) with ONE dialog: a
# collapsible CAD Diagnostics section showing a per-check summary, a
# "Show Details" toggle for the full diagnostics log, and the Yes/No question.
# Uses the same self-contained mshta/HTA mechanism already used for the
# Process Settings form. Purely a presentation change — the diagnostics logic
# and the downstream workflow are untouched, and it falls back to the original
# two message boxes if the HTA cannot be shown.
# ---------------------------------------------------------------------------
_AUTOMATION_PROMPT_HTA = """<html><head><title>Automation Workflow</title>
<HTA:APPLICATION ID="aw" SCROLL="auto" SYSMENU="yes" BORDER="dialog"
 CAPTION="yes" SHOWINTASKBAR="yes" INNERBORDER="no"/>
<style>
body{font:9pt "Segoe UI";background:#f0f0f0;margin:16px;width:540px}
h3{margin:0 0 8px 0;font-size:13pt}
.sec{border:1px solid #c8c8c8;background:#fff;margin:0 0 4px 0}
.hd{padding:8px 10px;cursor:pointer;font-weight:bold;background:#e8e8e8}
.hd .arw{color:#555;font-weight:normal;float:right}
.bd{padding:10px 12px}
.tot{margin:0 0 8px 0;font-weight:bold}
.ok{color:#2e7d32}
.bad{color:#c62828}
table.sm{border-collapse:collapse;width:100%}
table.sm td{padding:3px 6px;border-bottom:1px solid #eee}
a.dl{display:inline-block;margin-top:8px;color:#0a58ca;cursor:pointer;text-decoration:underline}
pre.dt{display:none;height:220px;overflow-y:scroll;overflow-x:hidden;background:#1e1e1e;color:#dcdcdc;padding:8px;margin-top:8px;font:8pt Consolas;white-space:pre-wrap;word-wrap:break-word}
p.q{margin:14px 2px 4px 2px;font-size:10pt}
.b{margin-top:14px;text-align:right}
button{width:100px;height:30px;margin-left:8px;font:9pt "Segoe UI"}
</style></head><body>
<h3>Automation Workflow</h3>
<div class="sec">
 <div class="hd" onclick="toggleSection()">CAD Diagnostics<span class="arw" id="arw">[ Hide ]</span></div>
 <div class="bd" id="body">
   __SUMMARY__
   <a class="dl" id="dllink" onclick="toggleDetails()">Show Details</a>
   <pre class="dt" id="dt">__DETAILS__</pre>
 </div>
</div>
<p class="q"><b>CAD Diagnostics completed successfully.</b><br>Do you want to start the analysis automation workflow?</p>
<div class="b"><button onclick="doYes()">Yes</button><button onclick="doNo()">No</button></div>
<script language="VBScript">
Sub toggleSection()
  Dim b, a
  Set b = document.getElementById("body")
  Set a = document.getElementById("arw")
  If b.style.display = "none" Then
    b.style.display = "block"
    a.innerText = "[ Hide ]"
  Else
    b.style.display = "none"
    a.innerText = "[ Show ]"
  End If
End Sub
Sub toggleDetails()
  Dim d, l
  Set d = document.getElementById("dt")
  Set l = document.getElementById("dllink")
  If d.style.display = "block" Then
    d.style.display = "none"
    l.innerText = "Show Details"
  Else
    d.style.display = "block"
    l.innerText = "Hide Details"
  End If
End Sub
Sub writeResult(v)
  Dim fso, f
  Set fso = CreateObject("Scripting.FileSystemObject")
  Set f = fso.CreateTextFile("__RES__", True)
  f.WriteLine v
  f.Close
End Sub
Sub doYes()
  writeResult "YES"
  window.close
End Sub
Sub doNo()
  writeResult "NO"
  window.close
End Sub
</script></body></html>"""

# ---------------------------------------------------------------------------
# Presentation capture
#
# Everything below serves ONE goal: every image that reaches the PowerPoint
# deck must be clean, centred, fully visible and free of any collision between
# the model and Synergy's own overlays (the results legend / colour scale, the
# MPa unit block, the study title, the axis triad, the scale bar).
#
# The previous implementation did `GoToStandardView` + `Fit()` and trusted it.
# `Fit()` fits the model to the WHOLE viewport, but Synergy then draws the
# legend and the colour scale ON TOP of that same viewport — so a model that
# `Fit()` considers perfectly framed routinely runs underneath the legend, and
# anything `Fit()` pushes to the very edge gets clipped by the export.
#
# The fix is a measured one rather than a guessed one: `_ViewFramer` renders
# throw-away probe images through the viewer's own exporter, measures where
# the overlays actually are and where the model actually is, and then drives
# Zoom/Pan in a closed loop until the model sits wholly inside the safe area.
# Nothing here is hard-coded to a particular Synergy skin — the reserved bands
# are measured from the running application.
# ---------------------------------------------------------------------------

# Export resolution for the deck images (unchanged from the original export).
CAPTURE_IMAGE_SIZE = (1600, 1200)
# Standard isometric used for unattended capture, so batch decks stay uniform.
CAPTURE_VIEW = "FrontTopRight"
# Redraw settling time after a camera change, before a render is trusted.
CAPTURE_SETTLE_SECONDS = 1.0
# Correction passes allowed per result before the best-so-far view is accepted.
CAPTURE_FRAME_MAX_PASSES = 6
# After GoToStandardView + parallel projection + Fit(), the model fills the
# viewport edge-to-edge and runs under the colour legend on the left. This
# Viewer.Zoom() factor pulls it back to leave a clean margin all round — enough
# to clear the legend and to give Synergy room to draw the "Scale (… mm)" bar
# under the model.
#
# Viewer.Zoom(f) is an ABSOLUTE scale: the model ends up at f x its fitted
# size. It is not a step, and the sign convention noted here previously
# ("negative = zoom out") does not apply — the Viewer class is not in the
# extracted API reference, so this was read off the exported deliverables:
#
#   Zoom(0.10) -> model occupied 10.2% of the frame  (2026-07-29 run, 9 images)
#   Zoom(0.20) -> model occupied 20.6% of the frame  (2026-07-27 run)
#
# i.e. exactly linear in f. At 0.10 the part was a stamp in the middle of a
# white page, which is what "the results are very zoomed" in the deck actually
# was. 0.88 leaves a ~12% margin: model large, legend clear, room for the
# scale bar. _ViewFramer.save_final() measures the exported image and logs the
# real figure, so a wrong value reports itself instead of shipping quietly.
CAPTURE_MARGIN_ZOOM = 0.88
# Model footprint (fraction of the frame) considered a good capture. Outside
# this band save_final logs a warning naming the measured value.
CAPTURE_FILL_OK = (0.35, 0.97)
# Clear breathing room kept on every side, as a fraction of the safe box.
CAPTURE_EDGE_MARGIN = 0.03
# How much smaller than the safe box the model may be before we zoom back in.
# (Some slack is essential: chasing an exact fill oscillates.)
CAPTURE_FILL_SLACK = 0.18
# Centring tolerance, as a fraction of the safe box.
CAPTURE_CENTER_TOL = 0.02
# Per-channel 0-255 difference that counts as real content rather than
# antialiasing noise between two renders of the same scene.
CAPTURE_DIFF_TOL = 14
# Probes are rendered at full size (so the overlays keep their true relative
# size) but ANALYSED downsampled to this width — 1600x1200 is 1.9M pixels and
# the measurements only need shape, not detail.
CAPTURE_ANALYSIS_WIDTH = 400
# No single side may be reserved beyond this fraction — a runaway measurement
# must never squeeze the model into nothing.
CAPTURE_MAX_RESERVE = 0.35
# Maximum blind zoom-out steps when the model is so clipped that _model_bbox
# returns None (the model covers the viewport corners and cannot be measured).
# Each step uses the same 0.15 factor as the measured unclip loop.
CAPTURE_UNCLIP_MAX_BLIND = 12
# Consecutive UNMEASURABLE zoom-out steps (no bounding box, so no progress
# signal) tolerated before concluding the zoom sign is wrong and flipping it.
# Generous on purpose: a correct direction can need several steps to pull the
# model off the viewport corners, and flipping too soon would drive it the
# wrong way.
CAPTURE_UNCLIP_BLIND_FLIP = 6

# Animation export alongside each screenshot.
EXPORT_ANIMATIONS = True
ANIMATION_SPEED = "Medium"
ANIMATION_SIZE = (960, 720)

# Offer the interactive review after an analysis. False restores the previous
# fully unattended capture. When True, the user selects results in Synergy's
# own Results tree and this process captures the ones they reviewed — there is
# no custom selection window.
INTERACTIVE_REVIEW = True

# Ask Moldflow's own AI Assistant panel to write the deck's closing summary
# instead of regenerating it from threshold rules (see assistant_live.py).
#
# It costs ONE request on the signed-in Autodesk account per report and appears
# in the user's chat history, which is why it is a named switch rather than an
# implicit behaviour. Set False to keep the locally computed summary and send
# nothing. Either way the rest of the deck is identical, and a missing panel or
# debug port falls back on its own without this being touched.
ASK_AI_ASSISTANT = True

# Put a "Component details" slide in the deck: the part's envelope, its volume
# and the FEA model the results were computed on. The reference report opens
# with this, and a reader who has not seen the part cannot judge any plot that
# follows it.
#
# It is a named switch because it is the one addition to the deck that costs
# real time: the geometry comes from Project().ExportModel(), which writes the
# WHOLE mesh to a .udm and parses it back. That is seconds on a small study and
# noticeably longer on a multi-million-element one. The elapsed time is logged
# every run; set False if it is not worth it on the studies you actually run.
# Nothing else in the deck depends on it -- the slide, and its contents entry,
# simply do not appear.
INCLUDE_COMPONENT_DETAILS = True

# Pace of the result presentation that follows "Analysis complete!".
# Resolving the Top-12 means calling Viewer.ShowPlot() once per result; back to
# back that flashes the whole set past in a second or two and the user sees
# nothing but the last one. This is a VIEWING pace, not a synchronisation
# delay -- there is no event to wait for, the plot is on screen the moment
# ShowPlot returns; the dwell exists purely so a person can look at it. The
# panel shows which result is up and a "Skip to review" button that ends the
# pacing immediately, so nobody is forced to sit through it.
RESULT_PRESENTATION_DWELL = 2.5   # seconds each result stays on screen
RESULT_PRESENTATION_POLL = 0.25   # how often the Skip button is checked

# How "open the help for this result" is carried out when the user ticks one.
#
#   "url" — open Autodesk's online help for the result directly.
#   "f1"  — press F1 in Synergy and let it decide.
#
# "url" is the default because of what F1 actually does on an install without
# the local help package: Synergy tries it, FAILS, shows "Failed to launch
# help.", and only then falls back to the browser. That box is part of its
# normal F1 path, so nothing on our side can stop it being created -- the
# WinEvent hook closes it within milliseconds, but the user still catches the
# flash. Going straight to the browser skips the failure entirely and lands in
# the same place. Set to "f1" on a site where Synergy's local help is
# installed and its context help is preferred; if that F1 then fails, the
# session switches itself to "url" after the first failure.
RESULT_HELP_MODE = "url"

# Direct topic page for a result -- the SAME page Synergy's own F1 opens.
RESULT_HELP_TOPIC_URL = "https://help.autodesk.com/view/MFIA/2027/ENU/?guid={0}"
# Last-resort search, for a plot with no mapped topic below.
RESULT_HELP_URL = "https://help.autodesk.com/view/MFIA/2027/ENU/?query={0}"

# Moldflow result name (lower-cased) -> Autodesk help topic guid.
#
# Why this table exists: help was being opened with `?query=<our display
# label>`, which lands on the SEARCH RESULTS page, not on the result's help
# topic. Two separate faults compounded there:
#
#   1. A search page is not the topic page. F1 is supposed to answer "what is
#      this result", and it was instead handing the user a list to pick from.
#   2. Our display labels are the Autodesk REVIEW DECK's category names, which
#      are not always Moldflow's result names. "Frozen layer thickness" is the
#      deck's name; Moldflow plots it as "Frozen layer fraction" and the help
#      topic is "Frozen layer fraction result". So the search was being run for
#      a phrase that does not title any page -- which is why the first hit was
#      never the right one.
#
# So the lookup is keyed on the result's REAL Moldflow plot name (available at
# the call site) and falls back to the display label, then to search.
#
# Every guid below was verified against the live MFIA 2027 content on
# 2026-07-30 by fetching
# help.autodesk.com/cloudhelp/2027/ENU/MoldflowInsight-CLC-Results/files/<section>/<guid>.html
# and confirming the page title matches the result. Autodesk keeps these
# slug-form guids stable across releases (the same strings resolve for 2023
# through 2027), so this does not need re-checking per version.
_HELP_FILL = "MoldflowInsight_CLC_Results_Fill_or_flow_results_{0}"
_HELP_COOL = "MoldflowInsight_CLC_Results_Cool_analysis_results_{0}"
_HELP_WARP = "MoldflowInsight_CLC_Results_Warp_analysis_results_{0}"

RESULT_HELP_GUIDS = {
    # Fill / flow
    "fill time": "GUID-7A85D18E-4D00-4439-8A8E-BF94FED55270",
    "pressure": _HELP_FILL.format("Pressure_result_html"),
    "pressure at injection location":
        _HELP_FILL.format("Pressure_at_injection_location_1_html"),
    "pressure at end of fill":
        _HELP_FILL.format("Pressure_at_end_of_fill_result_html"),
    "pressure at v/p switchover":
        _HELP_FILL.format("Pressure_at_velocity_pressure_html"),
    "clamp force": _HELP_FILL.format("Clamp_force_result_html"),
    "clamp force centroid": _HELP_FILL.format("Clamp_force_centroid_result_html"),
    "bulk temperature": _HELP_FILL.format("Bulk_temperature_result_html"),
    "temperature": _HELP_FILL.format("Temperature_result_html"),
    "temperature (3d)": _HELP_FILL.format("Temperature_result_3D_html"),
    "shear rate": _HELP_FILL.format("Shear_rate_result_html"),
    "shear rate (3d)": _HELP_FILL.format("Shear_rate_result_3D_html"),
    "shear rate, bulk": _HELP_FILL.format("Shear_rate_bulk_result_html"),
    "shear rate, maximum": _HELP_FILL.format("Shear_rate_maximum_result_html"),
    "shear stress at wall": _HELP_FILL.format("Shear_stress_at_wall_result_html"),
    "volumetric shrinkage": _HELP_FILL.format("Volumetric_shrinkage_result_html"),
    "average volumetric shrinkage":
        _HELP_FILL.format("Average_volumetric_shrinkage_html"),
    "frozen layer fraction": _HELP_FILL.format("Frozen_layer_fraction_result_html"),
    "orientation at skin": _HELP_FILL.format("Orientation_at_skin_result_html"),
    "orientation at core": _HELP_FILL.format("Orientation_at_core_result_html"),
    "orientation at top skin":
        _HELP_FILL.format("Orientation_at_top_skin_result_html"),
    "orientation at bottom skin":
        _HELP_FILL.format("Orientation_at_bottom_skin_html"),
    "air traps": _HELP_FILL.format("Air_traps_including_air_vents_html"),
    "weld lines": _HELP_FILL.format("Weld_and_meld_lines_result_html"),
    "sink marks": _HELP_FILL.format("Sink_marks_depth_result_html"),
    "velocity": _HELP_FILL.format("Velocity_result_html"),
    # Cool
    "temperature, part (top)": _HELP_COOL.format("Temperature_part_top_result_html"),
    "average temperature, part":
        _HELP_COOL.format("Average_temperature_part_result_html"),
    # Warp. Autodesk documents every deflection variant on ONE topic; there is
    # no separate page per effect, so all of them map to it.
    "deflection": _HELP_WARP.format("Deflection_results_html"),
    "deflection, all effects": _HELP_WARP.format("Deflection_results_html"),
    "deflection, differential cooling": _HELP_WARP.format("Deflection_results_html"),
    "deflection, differential shrinkage": _HELP_WARP.format("Deflection_results_html"),
    # Our display labels whose wording differs from Moldflow's result name, so
    # the label alone still resolves when the plot name is unavailable.
    "frozen layer thickness": _HELP_FILL.format("Frozen_layer_fraction_result_html"),
    "injection pressure": _HELP_FILL.format("Pressure_at_injection_location_1_html"),
    "shear stress": _HELP_FILL.format("Shear_stress_at_wall_result_html"),
    "orientation": _HELP_FILL.format("Orientation_at_skin_result_html"),
    "top temperature, part": _HELP_COOL.format("Temperature_part_top_result_html"),
    "deflection (all effects)": _HELP_WARP.format("Deflection_results_html"),
}


def result_help_url(plot_name="", label=""):
    """The help URL for a result: its own topic page when we know it, and the
    search page only as a last resort.

    Tries the real Moldflow plot name first, then our display label. A trailing
    qualifier Moldflow adds to some plot names ("Frozen layer fraction at end
    of fill", "Volumetric shrinkage (3D)") is stripped on a second pass so a
    variant still reaches its parent topic instead of falling through to
    search."""
    from urllib.parse import quote_plus

    for raw in (plot_name, label):
        key = str(raw or "").strip().lower()
        if not key:
            continue
        # "Deflection, all effects:Deflection" -> the plot part only.
        key = key.split(":")[0].strip()
        for candidate in (key,
                          re.sub(r"\s*\(3d\)$", "", key).strip(),
                          re.sub(r"\s+at end of fill$", "", key).strip(),
                          re.sub(r"\s+at ejection$", "", key).strip()):
            guid = RESULT_HELP_GUIDS.get(candidate)
            if guid:
                return RESULT_HELP_TOPIC_URL.format(guid), True

    return RESULT_HELP_URL.format(quote_plus(str(plot_name or label))), False


def _com_member_names(obj):
    """Real member names of a COM object, from its type library.

    Needed because win32com's DYNAMIC dispatch does not reject unknown
    properties: `opts.ShowScaleBar = True` on a build without that member
    quietly becomes an ordinary Python attribute, and reading it back returns
    the value you just set. Every "did that option take?" check based on
    setattr/getattr is therefore worthless, which is how the exports came to
    log "scale bar preserved" for images that have never had one.

    Falls back to dir() when no type info is available; returns an empty set
    if even that fails, and callers then keep their current behaviour."""
    names = set()

    # 1) pywin32's dynamic dispatch has already resolved the type information
    #    and keeps it in its own maps. This is the one that works: the COM
    #    route below needs GetTypeInfo(0) (the interface index is NOT optional)
    #    and the first version called it bare, which is why the last run logged
    #    "could not read the export options' member list" and every option name
    #    went back to being unverifiable.
    for attr in ("propMap", "propMapGet", "propMapPut", "mapFuncs"):
        try:
            names.update(getattr(obj._olerepr_, attr).keys())
        except Exception:
            pass

    # 2) Straight from the type library.
    if not names:
        try:
            ti = obj._oleobj_.GetTypeInfo(0)
            attr = ti.GetTypeAttr()
            for i in range(attr.cFuncs):
                try:
                    names.add(ti.GetNames(ti.GetFuncDesc(i).memid)[0])
                except Exception:
                    pass
            for i in range(attr.cVars):
                try:
                    names.add(ti.GetNames(ti.GetVarDesc(i).memid)[0])
                except Exception:
                    pass
        except Exception:
            pass

    # 3) Last resort.
    if not names:
        try:
            names = {n for n in dir(obj) if not n.startswith("_")}
        except Exception:
            names = set()
    return {str(n) for n in names}


def _com_object_or_call(raw, probe_attr):
    """Resolve a Synergy member that may be a PROPERTY on one build and a
    METHOD on another (as ActivePlot and ImageExportOptions both turned out to
    be). Given the raw attribute value, return a usable object: use it directly
    if it already answers `probe_attr`, otherwise call it and use the result.
    Returns None if neither works.

    This is why the first FitToScreen attempt silently failed — reading
    `viewer.ImageExportOptions` as a property handed back the METHOD object,
    which SaveImage5 then rejected ('function can not be converted to a COM
    VARIANT'), so it fell back to the auto-fitting legacy export.
    """
    if raw is None:
        return None
    try:
        getattr(raw, probe_attr)
        return raw
    except Exception:
        pass
    try:
        called = raw()
    except Exception:
        return None
    try:
        getattr(called, probe_attr)
        return called
    except Exception:
        return None


class _ViewFramer:
    """Frames the Synergy viewport for a presentation-quality screenshot.

    Usage is one instance per capture session:

        framer = _ViewFramer(viewer, log, 1600, 1200, workdir)
        framer.frame(preserve_orientation=False)   # before every SaveImage

    `frame()` degrades safely at every level. If the probing exporter or PIL
    is unavailable it falls back to exactly the original `GoToStandardView` +
    `Fit()` behaviour, so this can never make a working setup worse — it can
    only decline to improve it, and it says so in the log.
    """

    # Export options this build refused, reported once each (see _set_opt).
    _opt_warned = set()
    # Filled in once per session from the options object's type library.
    _members_logged = False
    _known_opts = set()
    _scale_opt = "ShowScaleBar"     # replaced by whatever this build has
    _viewport_scale_done = False

    def __init__(self, viewer, log, width, height, workdir):
        self.viewer = viewer
        self.log = log
        self.w, self.h = int(width), int(height)
        self.workdir = Path(workdir)
        # Probes are scratch renders, so they go to the temp directory rather
        # than the report's image folder — nothing that is not a deliverable
        # should ever appear next to the exported results.
        import tempfile
        self.probe_path = Path(tempfile.gettempdir()) / "_mf_frame_probe.png"
        self.safe = None            # (x0, y0, x1, y1) in ANALYSIS pixels
        self.available = None       # None = not yet probed, False = unusable
        self._zoom_gain = None      # d(ln model size) / d(Zoom factor)
        self._pan_gain = [None, None]   # d(model px) / d(Pan factor), per axis
        # Zoom factor sign that makes the model SMALLER. The SDK documents
        # negative = out, but _unclip proves it on the live viewer and stores
        # the result here so the phase-2 zoom probe never guesses wrong.
        self._zoom_out_sign = -1.0
        # Reusable image-export options object. THE crucial setting on it is
        # FitToScreen=False: with the default True the exporter re-fits the
        # model to the whole image on every save, which both overlaps the
        # legend/scale bar AND makes any Zoom/Pan we apply invisible (the
        # "no measurable effect" seen in the field). Turning it off is what
        # lets the framing actually take. None until first needed; False if
        # the options API is not available on this build.
        self._opts = None

    # -- plumbing ---------------------------------------------------------

    def _ensure_opts(self):
        """Lazily fetch the viewer's ImageExportOptions. False if the options
        API is unavailable, in which case callers fall back to SaveImage2/4
        (which auto-fit — degraded, but never a crash)."""
        if self._opts is None:
            opts = None
            try:
                # Exposed as a METHOD on this build, so resolve method-or-
                # property. Probe on SizeX, which every options object has.
                opts = _com_object_or_call(
                    self.viewer.ImageExportOptions, "SizeX")
            except Exception as e:
                self.log("  View framing: ImageExportOptions unavailable "
                         "({0}).".format(e))
            if opts is None:
                self.log("  View framing: ImageExportOptions could not be "
                         "resolved; export will auto-fit and may overlap the "
                         "legend.")
                self._opts = False
            else:
                self._opts = opts
                self._resolve_option_names(opts)
        return bool(self._opts)

    def _resolve_option_names(self, opts):
        """Learn what this build's export options object really offers.

        Logged once per session, and used to pick the correct scale-bar member
        instead of assuming a name. The still export sets its overlays
        explicitly and has never produced a scale bar, while the ANIMATION
        export sets none and does -- so the name we were writing to is the
        prime suspect."""
        cls = type(self)
        names = _com_member_names(opts)
        if not names:
            self.log("  View framing: could not read the export options' "
                     "member list; overlay names cannot be verified.")
            return
        if not cls._members_logged:
            cls._members_logged = True
            interesting = sorted(n for n in names
                                 if any(k in n.lower() for k in
                                        ("scale", "show", "fit", "ruler",
                                         # min/max annotation names too: the
                                         # Min label exports without the leader
                                         # line the Max label gets, and this is
                                         # the only way to find out whether the
                                         # build exposes anything that controls
                                         # it beyond the single ShowMinMax flag.
                                         "min", "max", "annot", "marker",
                                         "label", "leader")))
            self.log("  View framing: export options expose {0}".format(
                ", ".join(interesting) or "no Show*/Scale* members"))
        for cand in ("ShowScaleBar", "ShowScale", "ScaleBar", "DisplayScaleBar"):
            if cand in names:
                cls._scale_opt = cand
                break
        else:
            cls._scale_opt = None
            self.log("  View framing: this build's export options have NO "
                     "scale-bar member — the stills cannot carry the bar "
                     "through SaveImage5. (The animation export renders the "
                     "live viewport, which is why the videos have it.)")
        cls._known_opts = names
        self._enable_viewport_scale_bar()

    def _enable_viewport_scale_bar(self):
        """Turn the scale bar on in the LIVE viewport, once per session.

        The animation export sets no overlay options at all and its videos do
        show the bar, which means that path renders the viewport as it stands.
        So whatever the still export can or cannot express, having the bar on
        in the viewport is the precondition. Probed rather than assumed: the
        Viewer class is not in the extracted API reference, so the member list
        is read from the type library and only a member that actually exists is
        touched."""
        cls = type(self)
        if cls._viewport_scale_done:
            return
        cls._viewport_scale_done = True
        try:
            names = _com_member_names(self.viewer)
            hits = sorted(n for n in names if "scale" in n.lower())
            if not hits:
                self.log("  View framing: the Viewer exposes no scale-bar "
                         "member either; the bar cannot be enabled from the "
                         "API on this build.")
                return
            self.log("  View framing: Viewer scale members: {0}".format(
                ", ".join(hits)))
            for n in hits:
                try:
                    member = getattr(self.viewer, n)
                    if callable(member):
                        member(True)
                    else:
                        setattr(self.viewer, n, True)
                    self.log("  View framing: enabled Viewer.{0}.".format(n))
                    return
                except Exception as e:
                    self.log("  View framing: Viewer.{0} would not take True "
                             "({1}).".format(n, e))
        except Exception as e:
            self.log("  View framing: scale-bar probe failed ({0}).".format(e))

    def _set_opt(self, name, value):
        """Set one export option, and record whether it actually took.

        Failures used to be swallowed whole, so an option this build does not
        implement looked exactly like one that worked. That is why "scale bar
        preserved" was logged for images that have no scale bar: nobody had
        ever confirmed ShowScaleBar exists here. Each option is now written,
        read back, and reported ONCE per session."""
        known = type(self)._known_opts
        if known and name not in known:
            if name not in _ViewFramer._opt_warned:
                _ViewFramer._opt_warned.add(name)
                self.log("  View framing: export option {0} does not exist on "
                         "this build; skipping it (setting it would look like "
                         "it worked).".format(name))
            return
        ok, detail = True, ""
        try:
            setattr(self._opts, name, value)
        except Exception as e:
            ok, detail = False, str(e)
        else:
            try:
                back = getattr(self._opts, name)
                back = back() if callable(back) else back
                if isinstance(value, bool) and back is not None and bool(back) != value:
                    ok, detail = False, "read back as {0!r}".format(back)
            except Exception:
                pass          # not readable is not evidence of failure
        if not ok and name not in _ViewFramer._opt_warned:
            _ViewFramer._opt_warned.add(name)
            self.log("  View framing: export option {0}={1!r} not honoured by "
                     "this build ({2}). Anything it controls will be missing "
                     "from the images.".format(name, value, detail or "silently"))

    def _configure_opts(self, filename, show, fit=False):
        """Point the reusable options object at `filename` and switch the
        overlay set to `show` (a set of tokens).

        `fit` controls FitToScreen. The deliverable export uses fit=True so
        Synergy re-fits the model into the EXACT export frame (reserving the
        legend band on the way), which is what makes the model fill the image
        regardless of the live viewport's size/aspect. Probes keep fit=False so
        they measure the raw camera."""
        self._set_opt("FileName", str(filename))
        self._set_opt("SizeX", self.w)
        self._set_opt("SizeY", self.h)
        self._set_opt("FitToScreen", bool(fit))
        self._set_opt("ShowResult", True)
        self._set_opt("ShowLegend", "legend" in show)
        if type(self)._scale_opt:
            self._set_opt(type(self)._scale_opt, "scale" in show)
        self._set_opt("ShowStudyTitle", "title" in show)
        self._set_opt("ShowPlotInfo", "info" in show)
        self._set_opt("ShowRotationAxes", "axes" in show)
        self._set_opt("ShowRotationAngle", "angle" in show)
        self._set_opt("ShowMinMax", "minmax" in show)
        # The "Scale (… mm)" bar under the model IS Moldflow's RULER overlay.
        # The deliverable asked for "scale" and then explicitly switched the
        # ruler off two lines later, which is the whole reason the stills have
        # no scale bar while the animations (which set no overlay options at
        # all and just render the viewport) do. Both names now follow the same
        # token, so whichever one this build implements, the bar is on.
        self._set_opt("ShowRuler", "scale" in show)
        self._set_opt("ShowHistogram", False)

    def save_final(self, path):
        """Export the deliverable screenshot with the framed camera preserved.

        Uses FitToScreen=False so the camera `frame()` set — standard iso,
        parallel projection, fitted and zoomed out for a margin — survives
        into the file. This matters for two reasons the field runs proved:
        FitToScreen=True re-fits the model edge-to-edge (so it overlaps the
        legend) AND it SUPPRESSES the physical "Scale (… mm)" bar, because a
        re-fit invalidates the scale. Exporting the live camera keeps both the
        margin and the scale bar. `frame()` has already matched the viewport to
        this export size, so the framing reproduces faithfully. Falls back to
        the legacy SaveImage2 only if the options API is missing."""
        if self._ensure_opts():
            self._configure_opts(
                path, {"legend", "scale", "title", "info", "axes",
                       "angle", "minmax"}, fit=False)
            try:
                self.viewer.SaveImage5(self._opts)
                self.log("  View framing: exported with FitToScreen OFF "
                         "(framed camera + scale bar preserved).")
                self._check_export(path)
                return True
            except Exception as e:
                self.log("  View framing: SaveImage5 failed ({0}); FALLING "
                         "BACK to auto-fit SaveImage2.".format(e))
        else:
            self.log("  View framing: options export unavailable — using "
                     "auto-fit SaveImage2.")
        try:
            self.viewer.SaveImage2(str(path), self.w, self.h)
            return True
        except Exception as e:
            self.log("  View framing: SaveImage2 failed ({0}).".format(e))
            return False

    def _check_export(self, path):
        """Measure the image that was just written and say how much of it the
        model actually occupies.

        The framing is open-loop -- GoToStandardView, parallel, Fit, Zoom -- so
        nothing downstream notices when a Zoom factor is wrong: the deck is
        built from whatever came out. A run with the model at 10% of the frame
        shipped nine images and a PowerPoint before anyone looked. One
        measurement per capture turns that into a line in the log."""
        Image, _chops = self._pil()
        if Image is None:
            return
        try:
            im = Image.open(str(path)).convert("RGB")
            w, h = im.size
            px = im.load()
            minx, miny, maxx, maxy = w, h, -1, -1
            for y in range(0, h, 4):
                for x in range(int(w * 0.20), w, 4):
                    # Skip the axis triad in the bottom-right corner: it is
                    # saturated too, and including it makes any framing look
                    # fine.
                    if x > w * 0.85 and y > h * 0.78:
                        continue
                    r, g, b = px[x, y]
                    mx, mn = max(r, g, b), min(r, g, b)
                    if mx > 60 and (mx - mn) > 60:
                        if x < minx: minx = x
                        if x > maxx: maxx = x
                        if y < miny: miny = y
                        if y > maxy: maxy = y
            if maxx < 0:
                self.log("  Capture check: no coloured model found in the "
                         "exported image — the plot may not have rendered.")
                return
            fw = (maxx - minx) / float(w)
            fh = (maxy - miny) / float(h)
            fill = max(fw, fh)
            lo, hi = CAPTURE_FILL_OK
            msg = "  Capture check: model fills {0:.0%} x {1:.0%} of the frame".format(fw, fh)
            if fill < lo:
                self.log(msg + " — TOO SMALL. Raise CAPTURE_MARGIN_ZOOM "
                         "(currently {0}); Zoom(f) scales to f x the fitted "
                         "size.".format(CAPTURE_MARGIN_ZOOM))
            elif fill > hi:
                self.log(msg + " — TOO LARGE, the model may be clipped or "
                         "under the legend. Lower CAPTURE_MARGIN_ZOOM "
                         "(currently {0}).".format(CAPTURE_MARGIN_ZOOM))
            else:
                self.log(msg + ".")
        except Exception as e:
            self.log("  Capture check skipped ({0}).".format(e))

    def _pil(self):
        """(Image, ImageChops) or (None, None)."""
        try:
            from PIL import Image, ImageChops
            return Image, ImageChops
        except Exception:
            return None, None

    def _settle(self):
        import time
        time.sleep(CAPTURE_SETTLE_SECONDS)

    def _render(self, overlays):
        """Render a throw-away probe and return it as a downsampled RGB image.

        `overlays=False` renders the MODEL ALONE; `overlays=True` adds the
        edge-anchored chrome (legend, colour scale, plot info, study title,
        axis triad, rotation readout). The difference between the two is what
        tells us where the chrome actually lives.

        The floating min/max labels and the histogram are deliberately left
        out of both renders: they track the model rather than an edge, so
        reserving space for them would shrink the safe area for no reason.
        """
        Image, _ = self._pil()
        if Image is None:
            return None
        try:
            self.probe_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            self.probe_path.unlink()
        except Exception:
            pass
        # Probes MUST render the same way the final image will — i.e. with
        # FitToScreen off — otherwise the framer would measure an auto-fit view
        # that the real (non-fit) export does not reproduce. The overlay probe
        # carries only the EDGE-anchored chrome (legend, scale bar, plot info,
        # study title, axis triad); the floating min/max and histogram track
        # the model, so reserving space for them would shrink the safe area for
        # nothing.
        if self._ensure_opts():
            show = ({"legend", "scale", "title", "info", "axes"}
                    if overlays else set())
            self._configure_opts(self.probe_path, show)
            try:
                self.viewer.SaveImage5(self._opts)
            except Exception as e:
                self.log("  View framing: probe render unavailable ({0}).".format(e))
                return None
        else:
            # No options API: fall back to the legacy overlay export. This
            # auto-fits, so the framer cannot really help, but it will not
            # crash and _ready() will keep the plain Fit() view.
            o = bool(overlays)
            try:
                self.viewer.SaveImage4(str(self.probe_path), self.w, self.h,
                                       True, o, o, o, o, o, o,
                                       False, False, False, False)
            except Exception as e:
                self.log("  View framing: probe render unavailable ({0}).".format(e))
                return None
        if not (self.probe_path.exists() and self.probe_path.stat().st_size > 0):
            return None
        try:
            with Image.open(str(self.probe_path)) as im:
                im = im.convert("RGB")
                aw = min(CAPTURE_ANALYSIS_WIDTH, im.width)
                ah = max(1, int(round(im.height * (float(aw) / im.width))))
                try:
                    box = Image.Resampling.BOX
                except AttributeError:
                    box = Image.BOX
                small = im.resize((aw, ah), box)
            return small
        except Exception as e:
            self.log("  View framing: probe could not be read ({0}).".format(e))
            return None
        finally:
            try:
                self.probe_path.unlink()
            except Exception:
                pass

    def _mask(self, diff, tol=CAPTURE_DIFF_TOL):
        """A 0/255 content mask from a difference image."""
        return diff.convert("L").point(lambda v: 255 if v > tol else 0)

    def _model_bbox(self, im):
        """Pixel bounds of the model in a MODEL-ONLY probe, or None.

        The background is sampled from the four CORNERS, which a centred model
        never reaches even when Fit() pushes it out to the mid-edges — and then
        built into a top-to-bottom gradient so a graduated backdrop is matched
        too. A pixel counts as model when it differs from that background.

        This is the fix for the real failure seen in the field: the previous
        version used the left/right frame EDGES as the background reference,
        but an isometric plate spans the full width and touches those edges, so
        the reference was model-coloured, detection collapsed, and every
        Zoom/Pan probe reported 'no measurable effect' — leaving the plain,
        frame-filling Fit() view. Corners stay background regardless of how the
        model fills the frame, so the measurement now holds up.
        """
        Image, ImageChops = self._pil()
        if im is None or Image is None or ImageChops is None:
            return None
        w, h = im.size
        try:
            px = im.load()

            def avg(a, b):
                return tuple((a[k] + b[k]) // 2 for k in range(3))

            top = avg(px[0, 0], px[w - 1, 0])
            bot = avg(px[0, h - 1], px[w - 1, h - 1])
            # One-pixel-wide vertical gradient top->bottom, stretched to width.
            col = Image.new("RGB", (1, h))
            cp = col.load()
            for y in range(h):
                t = (y / float(h - 1)) if h > 1 else 0.0
                cp[0, y] = tuple(int(round(top[k] * (1.0 - t) + bot[k] * t))
                                 for k in range(3))
            bg = col.resize((w, h))
            mask = self._mask(ImageChops.difference(im, bg))
        except Exception:
            return None
        return mask.getbbox()

    # -- calibration ------------------------------------------------------

    def _calibrate(self):
        """Measure the overlay-free safe rectangle. True if usable."""
        plain = self._render(False)
        chrome = self._render(True)
        _, ImageChops = self._pil()
        if plain is None or chrome is None or ImageChops is None:
            return False
        w, h = plain.size
        try:
            overlay = self._mask(ImageChops.difference(plain, chrome))
        except Exception:
            return False

        def band(crop_box, index, origin):
            """How far the chrome reaches in from one side, in pixels."""
            try:
                b = overlay.crop(crop_box).getbbox()
            except Exception:
                b = None
            if not b:
                return 0
            return max(0, b[index] + origin)

        # Each side is measured only where that side's chrome can live, so one
        # element cannot inflate a band it has nothing to do with. The
        # left/right bands are read from the vertical MIDDLE only: the study
        # title sits top-left but runs a long way to the right, and reading it
        # as a left band would throw away most of the usable width. Anything
        # top- or bottom-anchored is accounted for by res_t / res_b instead.
        cap_w, cap_h = int(w * CAPTURE_MAX_RESERVE), int(h * CAPTURE_MAX_RESERVE)
        lw, rw = int(w * 0.45), int(w * 0.55)
        th, bh = int(h * 0.25), int(h * 0.75)
        res_l = band((0, th, lw, bh), 2, 0)               # bbox x1
        res_r = w - (band((rw, th, w, bh), 0, rw) or w)   # w - bbox x0
        res_l, res_r = min(max(res_l, 0), cap_w), min(max(res_r, 0), cap_w)

        # Top and bottom are then measured in the column that is LEFT OVER
        # after the side bands. A full-height legend reaches into the top and
        # bottom strips too, and counting it there would reserve a quarter of
        # the frame vertically for chrome that is already accounted for.
        ml, mr = min(res_l, w - 1), max(w - res_r, 1)
        res_t = band((ml, 0, mr, th), 3, 0)               # bbox y1
        res_b = h - (band((ml, bh, mr, h), 1, bh) or h)   # h - bbox y0
        res_t, res_b = min(max(res_t, 0), cap_h), min(max(res_b, 0), cap_h)

        mx, my = int(w * CAPTURE_EDGE_MARGIN), int(h * CAPTURE_EDGE_MARGIN)
        x0, y0 = res_l + mx, res_t + my
        x1, y1 = w - res_r - mx, h - res_b - my
        if x1 - x0 < w * 0.30 or y1 - y0 < h * 0.30:
            self.log("  View framing: measured safe area was implausibly small "
                     "— falling back to a plain margin.")
            x0, y0, x1, y1 = mx, my, w - mx, h - my
        self.safe = (x0, y0, x1, y1)
        self.log("  View framing: reserved for overlays L{0} R{1} T{2} B{3} px "
                 "of {4}x{5}; safe area {6}x{7}.".format(
                     res_l, res_r, res_t, res_b, w, h, x1 - x0, y1 - y0))
        return True

    def _ready(self):
        if self.available is None:
            try:
                self.available = self._calibrate()
            except Exception as e:
                self.log("  View framing: calibration failed ({0}); using "
                         "Fit() only.".format(e))
                self.available = False
            if not self.available:
                self.log("  View framing: measurement unavailable — keeping the "
                         "plain Fit() view (images may still collide with the "
                         "legend).")
        return self.available

    # -- closed-loop correction ------------------------------------------

    def _measure(self):
        """(bbox, analysis_size) of the model right now, or (None, None)."""
        im = self._render(False)
        if im is None:
            return None, None
        return self._model_bbox(im), im.size

    def _zoom(self, target_ln):
        """Change the model's on-screen size by `exp(target_ln)`.

        Synergy documents Zoom's factor as "normalized to the screen height"
        but not what that maps to in scale, and the sign convention has bitten
        this project before. So the first correction is a deliberate probe:
        apply a small step, measure what it actually did, and derive the gain
        (sign included) from the result. Every later step is computed from
        that measured gain.
        """
        import math
        if self._zoom_gain == 0.0:
            return
        if self._zoom_gain is None:
            before, _ = self._measure()
            if not before:
                return
            # Probe in the direction we already know: to SHRINK (target_ln<0)
            # go the proven zoom-out way, to GROW go the opposite. This avoids
            # a wrong-way probe that could re-clip the model off the frame.
            step = (self._zoom_out_sign if target_ln < 0
                    else -self._zoom_out_sign) * 0.12
            try:
                self.viewer.Zoom(step)
            except Exception as e:
                self.log("  View framing: Zoom() unavailable ({0}).".format(e))
                self._zoom_gain = 0.0
                return
            self._settle()
            after, _ = self._measure()
            if not after:
                return
            b0 = max(before[2] - before[0], before[3] - before[1], 1)
            a0 = max(after[2] - after[0], after[3] - after[1], 1)
            delta = math.log(float(a0) / float(b0))
            if abs(delta) < 0.01:
                self.log("  View framing: Zoom() had no measurable effect; "
                         "correcting by pan only.")
                self._zoom_gain = 0.0
            else:
                self._zoom_gain = delta / step
            return
        step = target_ln / self._zoom_gain
        step = max(-0.6, min(0.6, step))
        try:
            self.viewer.Zoom(step)
        except Exception:
            self._zoom_gain = 0.0

    def _pan(self, dx_px, dy_px, height_px):
        """Move the model by (dx, dy) analysis pixels.

        Pan factors are normalised to the screen HEIGHT and positive y is
        toward the top of the screen, while image y grows downward — so the
        vertical term is negated. As with zoom, the gain and the sign are
        measured on first use rather than assumed.
        """
        if height_px <= 0:
            return
        want = (float(dx_px) / height_px, -float(dy_px) / height_px)
        for axis in (0, 1):
            if abs(want[axis]) < 1e-4 or self._pan_gain[axis] == 0.0:
                continue
            if self._pan_gain[axis] is None:
                before, _ = self._measure()
                if not before:
                    return
                step = 0.08 if want[axis] > 0 else -0.08
                if not self._call_pan(step if axis == 0 else 0.0,
                                      0.0 if axis == 0 else step):
                    return
                self._settle()
                after, _ = self._measure()
                if not after:
                    return
                moved = (((after[0] + after[2]) - (before[0] + before[2])) / 2.0
                         if axis == 0 else
                         ((after[1] + after[3]) - (before[1] + before[3])) / 2.0)
                if axis == 1:
                    moved = -moved      # back into pan's screen-up convention
                if abs(moved) < 1.0:
                    self.log("  View framing: Pan() had no measurable effect on "
                             "axis {0}.".format(axis))
                    self._pan_gain[axis] = 0.0
                else:
                    self._pan_gain[axis] = moved / (step * height_px)
                continue
            step = want[axis] / self._pan_gain[axis]
            step = max(-0.8, min(0.8, step))
            self._call_pan(step if axis == 0 else 0.0,
                           0.0 if axis == 0 else step)

    def _call_pan(self, x, y):
        try:
            self.viewer.Pan(x, y)
            return True
        except Exception as e:
            self.log("  View framing: Pan() unavailable ({0}).".format(e))
            self._pan_gain = [0.0, 0.0]
            return False

    def _raw_zoom(self, f):
        """Apply a plain, uncalibrated Zoom step (used to escape frame
        clipping, when the model's true size cannot yet be measured)."""
        f = max(-0.6, min(0.6, f))
        try:
            self.viewer.Zoom(f)
            return True
        except Exception as e:
            self.log("  View framing: Zoom() unavailable ({0}).".format(e))
            self._zoom_gain = 0.0
            return False

    def _unclip(self):
        """Phase 1: zoom OUT until the whole model is inside the frame.

        While any part of the model runs off the frame its bounding box is
        pinned to the frame edge, so its true size is unknowable and the
        calibrated zoom/pan cannot work — this is precisely why a wide
        isometric plate defeated the earlier single-phase framer. Here we take
        fixed zoom-out steps until background shows on every side.

        The zoom-out direction is the SDK's (negative = out) but it is
        VERIFIED, not trusted: if the early steps do not shrink the model the
        sign is flipped ONCE and then LATCHED. Latching is essential — while
        the model overflows the frame its bounding box is pinned and its area
        stops changing, so a flip-every-time rule oscillates and never escapes.
        The zoom sign is a single unknown, so it is resolved exactly once.

        When the clipping is so severe that the model covers the viewport
        corners, _model_bbox cannot even detect it (returns None, because
        the corner-sampled background reference is itself model-coloured).
        In that case we zoom out blindly until the model becomes measurable,
        then fall through to the measured loop.
        """
        if self._zoom_gain == 0.0:
            return
        sign = -1.0
        flipped = False         # the sign is corrected at most once, ever

        # A single loop handles both clipped states uniformly, because a
        # wrong-direction step can move the model BETWEEN them (a merely
        # edge-clipped model, when zoomed in further, grows to cover the
        # corners and becomes unmeasurable). Treating them together means one
        # latch resolves the sign no matter which state we are in when the
        # wrong direction reveals itself.
        #
        #   * box is None  -> model covers the corners: unmeasurable, so very
        #                     clipped. Progress = becoming measurable again.
        #   * box touches an edge -> measurably clipped. Progress = shrinking
        #                     bounding-box area.
        #   * box clear of every edge -> fully inside the frame: done.
        #
        # In either clipped state, if progress stalls while the sign is still
        # unproven, flip it once and latch.
        budget = CAPTURE_UNCLIP_MAX_BLIND + CAPTURE_FRAME_MAX_PASSES * 2
        prev_area = None
        none_streak = 0
        stalls = 0
        for _ in range(budget):
            box, size = self._measure()
            if size is None:
                return              # render pipeline broken — nothing to do

            if box is None:
                # Unmeasurable: model covers the corners. With no bounding box
                # there is NO progress signal, so the sign cannot be judged
                # from one step — a correct direction may simply need several
                # steps to separate the model from the corners. Only after a
                # generous run of fruitless steps do we conclude the sign is
                # wrong, flip it and latch. (The measured branch below, which
                # does have a signal, stays decisive at two.)
                if not flipped:
                    none_streak += 1
                    if none_streak >= CAPTURE_UNCLIP_BLIND_FLIP:
                        sign = -sign
                        flipped = True
                        none_streak = 0
                        self._zoom_out_sign = sign
                if not self._raw_zoom(sign * 0.15):
                    return
                self._settle()
                continue

            none_streak = 0
            w, h = size
            touching = (box[0] <= 1 or box[1] <= 1
                        or box[2] >= w - 1 or box[3] >= h - 1)
            if not touching:
                return              # fully inside the frame

            area = (box[2] - box[0]) * (box[3] - box[1])
            if not flipped and prev_area is not None \
                    and area >= prev_area * 0.995:
                stalls += 1
                if stalls >= 2:
                    sign = -sign
                    flipped = True
                    stalls = 0
                    self._zoom_out_sign = sign
            prev_area = area
            if not self._raw_zoom(sign * 0.15):
                return
            self._settle()

    def _converge(self):
        import math
        # Phase 1 — get the model fully inside the frame so it is measurable.
        self._unclip()

        # Phase 2 — precise fit within the overlay-free safe box, then centre.
        x0, y0, x1, y1 = self.safe
        sw, sh = float(x1 - x0), float(y1 - y0)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        status = "framed as closely as measurement allowed"
        for attempt in range(1, CAPTURE_FRAME_MAX_PASSES + 1):
            box, size = self._measure()
            if not box or not size:
                return "model could not be measured; left as fitted"
            bw = max(box[2] - box[0], 1)
            bh = max(box[3] - box[1], 1)
            scale = min(sw / bw, sh / bh)
            dx, dy = cx - (box[0] + box[2]) / 2.0, cy - (box[1] + box[3]) / 2.0
            too_big = scale < 1.0
            too_small = scale > 1.0 / (1.0 - CAPTURE_FILL_SLACK)
            off_centre = (abs(dx) > CAPTURE_CENTER_TOL * sw
                          or abs(dy) > CAPTURE_CENTER_TOL * sh)
            if not too_big and not too_small and not off_centre:
                return "clear of the legend after {0} pass(es)".format(attempt - 1)
            status = ("still {0} after {1} pass(es)".format(
                "overlapping the legend" if too_big else "off-centre", attempt))
            acted = False
            if (too_big or too_small) and self._zoom_gain != 0.0:
                # Aim a little inside the safe box rather than exactly at it,
                # so a small measurement error cannot push the model back out.
                self._zoom(math.log(scale) - CAPTURE_EDGE_MARGIN)
                self._settle()
                acted = True
            if off_centre and self._pan_gain != [0.0, 0.0]:
                self._pan(dx, dy, size[1])
                self._settle()
                acted = True
            if not acted:
                # The only controls that could fix this view are unavailable —
                # further passes would just re-render the same picture.
                return status + "; no further correction available"
        return status

    # -- public -----------------------------------------------------------

    def frame(self, preserve_orientation=False):
        """Put the viewport into a presentation-ready state. Returns a short
        status string for the log.

        `preserve_orientation=True` keeps whatever angle the user rotated to
        during an interactive review — only the framing is normalised. The
        unattended path leaves it False so every batch image shares the same
        standard isometric, exactly as before.
        """
        if not preserve_orientation:
            try:
                self.viewer.GoToStandardView(CAPTURE_VIEW)
            except Exception:
                pass
        # Parallel (orthographic) projection for the capture. Perspective (the
        # Synergy default) enlarges the near corner of the part, so an
        # isometric plate's leading edge overflows and clips the frame even
        # after Fit()/FitToScreen — exactly the right-edge clipping seen in the
        # deck. Orthographic has a well-defined screen bounding box, so Fit()
        # and FitToScreen frame it predictably and the whole model stays in.
        # ViewModes.PARALLEL_PROJECTION == 0.
        try:
            self.viewer.SetViewMode(0)
        except Exception as e:
            self.log("  View framing: SetViewMode(parallel) unavailable "
                     "({0}); keeping perspective.".format(e))
        # Size the LIVE viewport to the export frame BEFORE fitting. Fit()
        # frames to the viewport's aspect; if the viewport aspect differs from
        # the export (the interactive window is rarely 4:3), a FitToScreen=OFF
        # export reproduces the wrong framing — model tiny or off-frame. Making
        # the viewport match the export first is what keeps the still and the
        # animation consistent.
        try:
            self.viewer.SetViewSize(self.w, self.h)
        except Exception:
            pass
        # Fit the model to that viewport.
        try:
            self.viewer.Fit()
        except Exception:
            pass
        self._settle()
        # Zoom OUT for a clean margin. Fit() fills the viewport edge-to-edge, so
        # the model runs under the legend and leaves no room for the scale bar.
        # Stepping back reproduces the framing the user makes by hand: model
        # clear of the legend, with the "Scale (… mm)" bar drawn beneath it.
        # (Viewer.Zoom() works interactively on this build — it was only the
        # framer's probe-based MEASUREMENT of it that failed.)
        try:
            self.viewer.Zoom(CAPTURE_MARGIN_ZOOM)
            self.log("  View framing: zoomed out {0} for margin/scale bar "
                     "clearance.".format(CAPTURE_MARGIN_ZOOM))
        except Exception as e:
            self.log("  View framing: Zoom() for margin unavailable ({0}).".format(e))
        self._settle()
        # The deliverable is exported with FitToScreen=OFF so this framed camera
        # (margin + scale bar) survives into the file. The fragile closed-loop
        # Zoom()/Pan() correction is deliberately not run.
        return "standard isometric, parallel, fitted with margin"

# ---------------------------------------------------------------------------
# Interactive result review (native Synergy Results tree)
#
# There is deliberately NO custom result-selection window any more. The user
# selects results in Synergy’s own Results tree; this process watches the
# viewer’s ActivePlot property to learn which result is on screen, and
# records each distinct result the user visits. A single always-on-top
# message box (shown from a background thread so Synergy stays fully
# interactive) is the only chrome we add — it just carries the Generate
# Report / Cancel decision. When the user clicks Generate Report, every
# result they reviewed is fitted, screenshotted, animated and added to the
# deck through the SAME capture code the unattended path uses.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Mesh Diagnostics (runs between automatic meshing and analysis)
#
# Everything below reads live values from the Synergy DiagnosisManager /
# MeshSummary API — nothing is fabricated.  A diagnostic appears in the
# report ONLY if the API actually returned it; anything the installed API
# version does not expose is listed under "not available" instead.
# To add a new diagnostic later: add a row to _MESH_SUMMARY_FIELDS (or
# _MESH_EXTRA_DIAGNOSTICS) and, if it needs a pass/warn/fail judgement,
# a rule in _mesh_diag_severity().
# ---------------------------------------------------------------------------

# Thresholds used to JUDGE the API values (the values themselves always come
# from the API).  Tune here, in one place.
MESH_DIAG_LIMITS = {
    "max_aspect_ratio_warn": 50.0,   # 3D tet mesh: max AR above this -> warning
    "pct_high_ar_warn": 5.0,         # % tets over Synergy's own AR threshold
    "match_ratio_warn": 85.0,        # Dual Domain match ratio below this (%) -> warning
}

# (MeshSummary COM property, human label, "int"/"float")
_MESH_SUMMARY_FIELDS = [
    ("NodesCount",             "Nodes",                          "int"),
    ("TrianglesCount",         "Triangle elements",              "int"),
    ("TetrasCount",            "Tetrahedral elements",           "int"),
    ("BeamsCount",             "Beam elements",                  "int"),
    ("ConnectivityRegions",    "Connectivity regions",           "int"),
    ("FreeEdgesCount",         "Free edges",                     "int"),
    ("ManifoldEdgesCount",     "Manifold edges",                 "int"),
    ("NonManifoldEdgesCount",  "Non-manifold edges",             "int"),
    ("Unoriented",             "Unoriented elements",            "int"),
    ("IntersectionElements",   "Intersecting elements",          "int"),
    ("OverlapElements",        "Overlapping elements",           "int"),
    ("DuplicatedBeams",        "Duplicate beams",                "int"),
    ("ZeroTriangles",          "Zero-area triangles",            "int"),
    ("ZeroBeams",              "Zero-length beams",              "int"),
    ("MinAspectRatio",         "Minimum aspect ratio",           "float"),
    ("AveAspectRatio",         "Average aspect ratio",           "float"),
    ("MaxAspectRatio",         "Maximum aspect ratio",           "float"),
    ("MaxDihedralAngle",       "Maximum dihedral angle",         "float"),
    ("MaxVolumeRatio",         "Maximum volume ratio",           "float"),
    ("PercentTetsARgtThresh",  "% tets over aspect-ratio limit", "float"),
    ("PercentTetsMDAgtThresh", "% tets over dihedral limit",     "float"),
    ("PercentTetsVRgtThresh",  "% tets over volume-ratio limit", "float"),
    ("MatchRatio",             "Match ratio",                    "float"),
    ("ReciprocalMatchRatio",   "Reciprocal match ratio",         "float"),
    ("MeshVolume",             "Mesh volume",                    "float"),
    ("RunnerVolume",           "Runner volume",                  "float"),
    ("FusionArea",             "Fusion surface area",            "float"),
]

# Extra failed-element diagnostics called directly on the DiagnosisManager
# (only meaningful for tet meshes): (COM method, label, args)
_MESH_EXTRA_DIAGNOSTICS = [
    ("GetInvertedTetras", "Inverted tetras (failed elements)", (False, None)),
    ("GetCollapsedFaces", "Collapsed faces (failed elements)", (False, None)),
]

_MESH_DIAG_LABELS = dict(
    [(n, l) for n, l, _k in _MESH_SUMMARY_FIELDS]
    + [(n, l) for n, l, _a in _MESH_EXTRA_DIAGNOSTICS]
)

def _mesh_diag_severity(name, value, mesh_type):
    """Judge one API-supplied diagnostic value.

    Returns (severity, note) where severity is 'info' / 'warning' / 'error'.
    Extend here when new diagnostics need a judgement; anything without a
    rule is reported as information."""
    limits = MESH_DIAG_LIMITS
    mt = (mesh_type or "").lower()
    if name == "NodesCount" and value < MIN_MESH_NODES:
        return "error", "Below the {0}-node minimum for a usable mesh.".format(
            MIN_MESH_NODES)
    if name in ("IntersectionElements", "OverlapElements", "Unoriented",
                "GetInvertedTetras", "GetCollapsedFaces"):
        if value > 0:
            return "error", "Must be fixed before analysis."
    if name in ("ZeroTriangles", "ZeroBeams", "DuplicatedBeams",
                "NonManifoldEdgesCount"):
        if value > 0:
            return "warning", "Review before running the analysis."
    if name == "ConnectivityRegions" and value > 1:
        return "warning", ("Mesh has {0} disconnected regions; a single part "
                           "is normally 1 (runners/inserts can add more)."
                           .format(int(value)))
    if name == "FreeEdgesCount" and value > 0 and ("3d" in mt or "dual" in mt):
        return "warning", "A closed 3D/Dual Domain surface should have no free edges."
    if name == "MaxAspectRatio" and value > limits["max_aspect_ratio_warn"]:
        return "warning", "Above the recommended limit of {0}.".format(
            limits["max_aspect_ratio_warn"])
    if name == "PercentTetsARgtThresh" and value > limits["pct_high_ar_warn"]:
        return "warning", "More than {0}% of tets exceed the aspect-ratio limit.".format(
            limits["pct_high_ar_warn"])
    if name in ("MatchRatio", "ReciprocalMatchRatio") and "dual" in mt:
        pct = value * 100.0 if value <= 1.0 else value
        if pct < limits["match_ratio_warn"]:
            return "warning", "Below the recommended {0}% match.".format(
                limits["match_ratio_warn"])
    return "info", ""

def collect_mesh_diagnostics(sy, study_doc, log):
    """Read every mesh diagnostic the installed Synergy API exposes.

    Returns a report dict:
      status       'Passed' / 'Warning' / 'Failed'
      items        [{name,label,value,severity,note}, ...] (API values only)
      counts       {'info':n,'warning':n,'error':n}
      total_elements  triangles+tets+beams (from API counts)
      mesh_type    study mesh type string
      unavailable  [labels the API did not expose]
    """
    vals = {}
    unavailable = []

    mesh_type = ""
    try:
        mesh_type = str(study_doc.MeshType() or "")
    except Exception:
        pass

    dm = None
    try:
        dm = get_member(sy, "DiagnosisManager")
        if callable(dm):
            dm = dm()
    except Exception as e:
        log("DiagnosisManager not available: {0}".format(e))
        dm = None

    # --- MeshSummary (bulk of the diagnostics) ---
    summary = None
    if dm is not None:
        # Newer API takes (elementOnly, incBeams, incMatch, recalculate);
        # fall back to the 3-argument form on older installs.
        for args in ((False, True, True, True), (False, True, True)):
            try:
                summary = dm.GetMeshSummary2(*args)
                if summary is not None:
                    break
            except Exception as e:
                last_summary_err = e
        if summary is None:
            log("GetMeshSummary2 not available: {0}".format(
                locals().get("last_summary_err", "returned None")))

    if summary is not None:
        for name, label, kind in _MESH_SUMMARY_FIELDS:
            try:
                v = getattr(summary, name)
                v = v() if callable(v) else v
                if v is None:
                    raise ValueError("no value")
                vals[name] = float(v) if kind == "float" else int(v)
            except Exception:
                unavailable.append(label)
    else:
        unavailable.extend(label for _n, label, _k in _MESH_SUMMARY_FIELDS)

    # --- Failed-element diagnostics (tet meshes only) ---
    if dm is not None and vals.get("TetrasCount"):
        for name, label, args in _MESH_EXTRA_DIAGNOSTICS:
            try:
                fn = getattr(dm, name)
                vals[name] = int(fn(*args))
            except Exception as e:
                unavailable.append(label)
                log("Mesh diagnostic {0} not exposed by this API: {1}".format(
                    name, e))

    # Fallback so the report is never empty: node count via predicate.
    if "NodesCount" not in vals:
        try:
            pred = sy.PredicateManager().CreateLabelPredicate("N1:")
            ents = study_doc.CreateEntityList()
            ents.SelectFromPredicate(pred)
            vals["NodesCount"] = int(ents.Size() or 0)
        except Exception:
            pass

    # --- Judge each available value ---
    items = []
    counts = {"info": 0, "warning": 0, "error": 0}
    order = [n for n, _l, _k in _MESH_SUMMARY_FIELDS] + \
            [n for n, _l, _a in _MESH_EXTRA_DIAGNOSTICS]
    for name in order:
        if name not in vals:
            continue
        value = vals[name]
        severity, note = _mesh_diag_severity(name, value, mesh_type)
        counts[severity] += 1
        items.append({
            "name": name,
            "label": _MESH_DIAG_LABELS.get(name, name),
            "value": value,
            "severity": severity,
            "note": note,
        })

    total_elements = sum(int(vals.get(k) or 0) for k in
                         ("TrianglesCount", "TetrasCount", "BeamsCount"))

    if counts["error"] > 0 or not items:
        status = "Failed"
    elif counts["warning"] > 0:
        status = "Warning"
    else:
        status = "Passed"

    report = {
        "status": status,
        "items": items,
        "counts": counts,
        "total_elements": total_elements,
        "mesh_type": mesh_type,
        "unavailable": unavailable,
    }

    log("Mesh diagnostics: status={0}, {1} value(s) read, "
        "{2} not exposed by the API.".format(status, len(items), len(unavailable)))
    for it in items:
        if it["severity"] != "info":
            log("  [{0}] {1} = {2}  {3}".format(
                it["severity"].upper(), it["label"], it["value"], it["note"]))

    # Persist alongside the CAD diagnostics report (additive; best-effort).
    try:
        out = dict(report, generated=datetime.datetime.now().isoformat())
        (SESSION_DIR / "mesh_diagnostics_report.json").write_text(
            json.dumps(out, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    return report

_MESH_CHOICE_HTA = """<html><head><title>__TITLE__</title>
<HTA:APPLICATION ID="md" SCROLL="auto" SYSMENU="yes" BORDER="dialog"
 CAPTION="yes" SHOWINTASKBAR="yes" INNERBORDER="no"/>
<style>
body{font:9pt "Segoe UI";background:#f0f0f0;margin:16px;width:560px}
h3{margin:0 0 8px 0;font-size:13pt}
.sec{border:1px solid #c8c8c8;background:#fff;margin:0 0 4px 0}
.hd{padding:8px 10px;font-weight:bold;background:#e8e8e8}
.bd{padding:10px 12px;max-height:260px;overflow-y:auto}
.tot{margin:0 0 8px 0;font-weight:bold}
.ok{color:#2e7d32}
.warn{color:#b26a00}
.bad{color:#c62828}
table.sm{border-collapse:collapse;width:100%}
table.sm td{padding:3px 6px;border-bottom:1px solid #eee}
a.dl{display:inline-block;margin-top:8px;color:#0a58ca;cursor:pointer;text-decoration:underline}
pre.dt{display:none;height:180px;overflow-y:scroll;overflow-x:hidden;background:#1e1e1e;color:#dcdcdc;padding:8px;margin-top:8px;font:8pt Consolas;white-space:pre-wrap;word-wrap:break-word}
p.q{margin:14px 2px 4px 2px;font-size:10pt}
.b{margin-top:14px;text-align:right}
button{height:30px;margin-left:8px;padding:0 12px;font:9pt "Segoe UI"}
</style></head><body>
<h3>__TITLE__</h3>
<div class="sec">
 <div class="hd">__HEADING__</div>
 <div class="bd">
   __SUMMARY__
   <a class="dl" id="dllink" onclick="toggleDetails()">Show Details</a>
   <pre class="dt" id="dt">__DETAILS__</pre>
 </div>
</div>
<p class="q">__QUESTION__</p>
<div class="b">__BUTTONS__</div>
<script language="VBScript">
Sub toggleDetails()
  Dim d, l
  Set d = document.getElementById("dt")
  Set l = document.getElementById("dllink")
  If d.style.display = "block" Then
    d.style.display = "none"
    l.innerText = "Show Details"
  Else
    d.style.display = "block"
    l.innerText = "Hide Details"
  End If
End Sub
Sub writeResult(v)
  Dim fso, f
  Set fso = CreateObject("Scripting.FileSystemObject")
  Set f = fso.CreateTextFile("__RES__", True)
  f.WriteLine v
  f.Close
End Sub
__SUBS__
</script></body></html>"""

def _mesh_esc(s):
    return (str(s).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def show_mesh_choice_dialog(title, heading, summary_html, detail_text,
                            question, buttons, default_token, log):
    """Show prompt via ui_bridge rather than raw Win32/mshta dialogs."""
    try:
        import ui_bridge
        options_map = {label: token for token, label in buttons}
        # Prompt user through state
        ans_label = ui_bridge.prompt_user(title, question, options=list(options_map.keys()))
        return options_map.get(ans_label, default_token)
    except Exception:
        return default_token

def _find_synergy_hwnd():
    """Largest visible top-level window owned by synergy.exe, or None.

    Same technique embedded_ui.py uses to dock the panel; duplicated here
    rather than imported because this process must not depend on the panel
    process being importable."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _is_synergy(pid):
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

    best = {"hwnd": None, "area": -1}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or not _is_synergy(pid.value):
                return True
            r = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            area = max(r.right - r.left, 0) * max(r.bottom - r.top, 0)
            if area > best["area"]:
                best.update(hwnd=hwnd, area=area)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    return best["hwnd"]


_HELP_FAILURE_TEXT = "failed to launch help"

# Synergy message boxes this automation is allowed to close by itself.
#
# Synergy.Silence(True) is documented as suppressing message boxes and it
# RETURNS TRUE on this build -- and the "Study : X / Analysis complete" box
# still appears (the reference does warn the call "may be incomplete"). So the
# boxes have to be dismissed as well as silenced.
#
# This list is deliberately tiny and specific. Each entry is a fragment that
# must appear in the dialog's own text, and every one of them is purely
# informational: something the workflow already knows, already logged, and
# already shows in the panel. Nothing that asks a question, reports an error or
# offers a choice is in here -- those must always reach the user.
_AUTO_DISMISS_TEXTS = (
    "analysis complete",
    _HELP_FAILURE_TEXT,
)


def _enum_synergy_dialogs():
    """[(hwnd, text)] for visible standard dialogs belonging to synergy.exe."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _win_text(hwnd):
        try:
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return ""
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value or ""
        except Exception:
            return ""

    def _is_synergy(pid):
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

    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "#32770":          # standard dialog class only
                return True
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or not _is_synergy(pid.value):
                return True
            parts = [_win_text(hwnd)]
            buttons = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def _child(ch, _p):
                txt = _win_text(ch)
                parts.append(txt)
                try:
                    cls_c = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(ch, cls_c, 64)
                    if cls_c.value.lower() == "button" and user32.IsWindowVisible(ch):
                        buttons.append(txt)
                except Exception:
                    pass
                return True

            user32.EnumChildWindows(hwnd, _child, 0)
            out.append((hwnd, " ".join(p for p in parts if p), buttons))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def _close_dialog(hwnd):
    """Click the dialog's default button, falling back to WM_CLOSE."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    BM_CLICK, WM_CLOSE = 0x00F5, 0x0010
    clicked = {"v": False}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _child(ch, _p):
        try:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(ch, cls, 64)
            if cls.value.lower() == "button" and user32.IsWindowEnabled(ch):
                user32.SendMessageW(ch, BM_CLICK, 0, 0)
                clicked["v"] = True
                return False
        except Exception:
            pass
        return True

    try:
        user32.EnumChildWindows(hwnd, _child, 0)
        if not clicked["v"]:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def _dialog_should_be_dismissed(hwnd):
    """(fragment, buttons) if this window is one of the informational boxes we
    are allowed to close, else (None, buttons).

    Shared by all three closers so the text allowlist and the one-button guard
    can never drift apart between them."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    try:
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "#32770":
            return None, []
    except Exception:
        return None, []

    def _text(h):
        try:
            n = user32.GetWindowTextLengthW(h)
            if not n:
                return ""
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            return buf.value or ""
        except Exception:
            return ""

    parts = [_text(hwnd)]
    buttons = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _child(ch, _p):
        t = _text(ch)
        parts.append(t)
        try:
            c = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(ch, c, 64)
            if c.value.lower() == "button":
                buttons.append(t)
        except Exception:
            pass
        return True

    try:
        user32.EnumChildWindows(hwnd, _child, 0)
    except Exception:
        return None, []

    text = " ".join(p for p in parts if p)
    low = text.lower()
    # Must look like Moldflow's own dialog, so a same-worded box from an
    # unrelated application is never touched.
    if not any(k in low for k in ("moldflow", "autodesk", "synergy")):
        return None, buttons
    hit = next((f for f in _AUTO_DISMISS_TEXTS if f in low), None)
    if not hit:
        return None, buttons
    plain = [b.replace("&", "").strip().lower() for b in buttons]
    if len(plain) != 1 or plain[0] not in ("ok", "close"):
        return None, buttons          # a question: never auto-answered
    return hit, buttons


def start_dialog_hook(log, on_dismiss=None):
    """Close the informational boxes the moment Windows creates them.

    Polling always loses this race: "Failed to launch help." is shown by
    Synergy's own help handler BEFORE it falls back to the browser, so with a
    250ms watcher the user still sees it flash. A WinEvent hook fires as the
    dialog is created, which is early enough to hide it before it paints.

    Returns stop(). Falls back silently to the poller if the hook cannot be
    installed -- this is an optimisation on top of the watcher, never the only
    line of defence.
    """
    import ctypes
    import ctypes.wintypes as wt
    import threading
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    EVENT_SYSTEM_DIALOGSTART = 0x0010
    EVENT_OBJECT_SHOW = 0x8002
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002
    OBJID_WINDOW = 0
    WM_QUIT = 0x0012
    SW_HIDE = 0

    proto = ctypes.WINFUNCTYPE(
        None, wt.HANDLE, wt.DWORD, wt.HWND, ctypes.c_long, ctypes.c_long,
        wt.DWORD, wt.DWORD)
    state = {"tid": 0, "hooks": [], "seen": set(), "ready": threading.Event()}

    def _on_event(_hook, _event, hwnd, id_object, _id_child, _tid, _time):
        if id_object != OBJID_WINDOW or not hwnd:
            return
        try:
            hit, _buttons = _dialog_should_be_dismissed(hwnd)
            if not hit:
                return
            # Hide first: closing alone still lets the box paint once.
            user32.ShowWindow(hwnd, SW_HIDE)
            _close_dialog(hwnd)
            if hit not in state["seen"]:
                state["seen"].add(hit)
                log("Intercepted Synergy's '{0}' box as it opened "
                    "(informational; not shown to the user).".format(hit))
            if on_dismiss is not None:
                try:
                    on_dismiss(hit)
                except Exception:
                    pass
        except Exception:
            pass

    callback = proto(_on_event)

    def _run():
        state["tid"] = kernel32.GetCurrentThreadId()
        try:
            for ev in (EVENT_SYSTEM_DIALOGSTART, EVENT_OBJECT_SHOW):
                h = user32.SetWinEventHook(
                    ev, ev, 0, callback, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
                if h:
                    state["hooks"].append(h)
        except Exception as e:
            log("Dialog hook could not be installed ({0}); the polling "
                "watcher still applies.".format(e))
        state["ready"].set()
        if not state["hooks"]:
            return
        msg = wt.MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for h in state["hooks"]:
            try:
                user32.UnhookWinEvent(h)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    state["ready"].wait(2.0)
    if state["hooks"]:
        log("Dialog hook active: Synergy's informational boxes are closed as "
            "they open.")

    def stop():
        try:
            if state["tid"]:
                user32.PostThreadMessageW(state["tid"], WM_QUIT, 0, 0)
        except Exception:
            pass
    return stop


def sweep_dialogs_now(log, wait=2.5, poll=0.08):
    """Close an allowlisted dialog the moment it appears, for `wait` seconds.

    The background watcher polls slowly enough to be free, which means a box
    can be on screen for up to a poll interval -- long enough for the user to
    see "Failed to launch help." flash up before the help browser opens. F1 is
    a known trigger, so the caller sweeps hard right after pressing it instead
    of waiting for the next tick. Same allowlist, same one-button guard.
    """
    import time as _t
    deadline = _t.time() + wait
    closed = False
    while _t.time() < deadline:
        try:
            for hwnd, _text, _buttons in _enum_synergy_dialogs():
                hit, _bt = _dialog_should_be_dismissed(hwnd)
                if not hit:
                    continue
                if _close_dialog(hwnd):
                    closed = True
                    return True
        except Exception:
            pass
        _t.sleep(poll)
    return closed


def start_dialog_watcher(log, poll=0.25):
    """Close Synergy's informational message boxes while the workflow runs.

    Returns a stop() callable. The watcher only ever touches dialogs whose own
    text matches _AUTO_DISMISS_TEXTS -- everything else is left strictly alone,
    including anything it cannot read.
    """
    import threading
    import time as _t

    stop_flag = threading.Event()
    seen = set()

    def _run():
        while not stop_flag.is_set():
            try:
                for hwnd, _text, _buttons in _enum_synergy_dialogs():
                    # The text allowlist AND the one-button guard both live in
                    # _dialog_should_be_dismissed, so the hook, the sweep and
                    # this watcher can never disagree about what is safe to
                    # close. A dialog offering a CHOICE is a question and is
                    # never answered here, however its text happens to read.
                    hit, _bt = _dialog_should_be_dismissed(hwnd)
                    if not hit:
                        continue
                    if _close_dialog(hwnd) and hit not in seen:
                        seen.add(hit)
                        log("Auto-dismissed Synergy's '{0}' message box "
                            "(informational; the workflow reports it in the "
                            "panel).".format(hit))
            except Exception:
                pass
            stop_flag.wait(poll)

    threading.Thread(target=_run, daemon=True).start()

    def stop():
        stop_flag.set()
    return stop


def _send_f1_to_synergy(log):
    """Press F1 in Synergy, reliably, from this background process.

    A bare SetForegroundWindow does NOT work here. Windows only lets the
    process that owns the current foreground window change it, and when the
    user ticks a checkbox the foreground window is the PANEL -- so the call
    fails silently and the synthesised F1 goes to the panel instead of
    Synergy. That is the whole "the first result never opens" symptom: it was
    never about which result, only about who had focus at the time.

    AttachThreadInput ties this thread's input state to Synergy's for the
    moment of the call, which is the documented way to be allowed to set the
    foreground window. The key is then synthesised with SendInput so it goes
    through the normal input queue, and WM_HELP is posted as a belt-and-braces
    fallback (that is the message F1 generates for a dialog/window).
    """
    import ctypes
    import ctypes.wintypes as wt
    import time as _t
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = _find_synergy_hwnd()
    if not hwnd:
        log("  Synergy window not found; F1 not sent.")
        return False

    VK_F1 = 0x70
    attached = False
    try:
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        syn_tid = user32.GetWindowThreadProcessId(hwnd, None)
        for tid in {fg_tid, syn_tid}:
            if tid and tid != cur_tid and user32.AttachThreadInput(cur_tid, tid, True):
                attached = True
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        _t.sleep(0.05)

        # Only synthesise the key if Synergy really is in front now; sending it
        # blind is what typed F1 into the panel.
        front = user32.GetForegroundWindow()
        if front != hwnd:
            root = user32.GetAncestor(front, 2) if front else 0   # GA_ROOT
            if root != hwnd:
                log("  Could not bring Synergy to the front for F1 "
                    "(foreground stayed elsewhere); posting WM_HELP instead.")
                user32.PostMessageW(hwnd, 0x0053, 0, 0)           # WM_HELP
                return True

        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_F1, 0, 0, 0)
        user32.keybd_event(VK_F1, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        log("  Could not send F1 to Synergy: {0}".format(e))
        return False
    finally:
        if attached:
            try:
                cur_tid = kernel32.GetCurrentThreadId()
                for tid in {user32.GetWindowThreadProcessId(hwnd, None)}:
                    if tid and tid != cur_tid:
                        user32.AttachThreadInput(cur_tid, tid, False)
            except Exception:
                pass




def collect_mesh_diagnostics_when_ready(sy, study_doc, log, timeout=180.0, poll=2.0):
    """collect_mesh_diagnostics(), but keep re-reading until the API actually
    has the numbers.

    GetMeshSummary2 can answer before the mesher has published its statistics
    (the call succeeds, the fields come back empty/zero), which is how an
    empty or half-filled diagnostics table used to reach the dialog. Rather
    than sleeping a guessed amount, poll the real conditions: a node count at
    or above the usable minimum AND at least one judged diagnostic value. Only
    time-boxed so a genuinely broken mesh cannot hang the workflow -- the last
    report is returned in that case and judged on its own (empty items already
    means status 'Failed', so the user is asked, never skipped).
    """
    import time
    deadline = time.time() + timeout
    report = None
    while True:
        report = collect_mesh_diagnostics(sy, study_doc, log)
        items = report.get("items") or []
        nodes = 0
        for it in items:
            if it.get("name") == "NodesCount":
                nodes = int(it.get("value") or 0)
                break
        if items and nodes >= MIN_MESH_NODES:
            return report
        if time.time() >= deadline:
            log("Mesh diagnostics did not fully populate within {0}s "
                "({1} value(s), {2} node(s)); reporting what the API returned."
                .format(int(timeout), len(items), nodes))
            return report
        log("Mesh diagnostics not populated yet ({0} value(s), {1} node(s)); "
            "waiting for the mesher to publish its statistics...".format(
                len(items), nodes))
        time.sleep(poll)


def _mesh_diag_plain_text(report):
    """Full diagnostics as plain text -- used for the Show Details pane and as
    the body of the blocking fallback dialog when the panel is unavailable."""
    lines = ["Overall mesh status : {0}".format(report.get("status", "")),
             "Mesh type           : {0}".format(report.get("mesh_type", "")),
             "Total mesh elements : {0}".format(report.get("total_elements", 0)),
             ""]
    for it in report.get("items", []):
        value = it["value"]
        vtxt = ("{0:.3f}".format(value).rstrip("0").rstrip(".")
                if isinstance(value, float) else str(value))
        tag = {"info": "INFO   ", "warning": "WARNING", "error": "ERROR  "}[it["severity"]]
        lines.append("[{0}] {1} = {2}  {3}".format(
            tag, it["label"], vtxt, it["note"]).rstrip())
    if report.get("unavailable"):
        lines.append("")
        lines.append("Not exposed by this Moldflow API version:")
        for label in report["unavailable"]:
            lines.append("  - {0}".format(label))
    return "\n".join(lines)


def _mesh_diag_blocking_fallback(report, question, buttons, log):
    """Last resort when the embedded panel never showed the dialog.

    Shows the complete diagnostics text in a modal, topmost Win32 box that the
    user must dismiss. It is deliberately NOT ui_bridge.prompt_user(): that
    returns the last option instantly when the panel is dead (and instantly
    again for a single-option prompt), which is precisely how the analysis
    used to start with nobody having seen the diagnostics."""
    body = "{0}\n\n{1}\n\n{2}".format(
        question,
        _mesh_diag_plain_text(report),
        "OK = {0}    Cancel = stop the workflow".format(buttons[0][1]))
    try:
        import ctypes
        MB_OKCANCEL = 0x00000001
        MB_ICONINFORMATION = 0x00000040
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        IDOK = 1
        ret = ctypes.windll.user32.MessageBoxW(
            None, body, "Mesh Diagnostics ({0})".format(report.get("status", "")),
            MB_OKCANCEL | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
        chosen = buttons[0][0] if ret == IDOK else "CANCEL"
        log("Mesh diagnostics answered through the fallback dialog: {0}".format(chosen))
        return chosen
    except Exception as exc:
        # No panel and no dialog: stop rather than run an unreviewed mesh.
        log("Mesh diagnostics could not be shown at all ({0}); cancelling.".format(exc))
        return "CANCEL"


def display_mesh_diagnostics(report, log):
    """Render the mesh-diagnostics report in a dedicated dialog and return
    the user's decision token:
      'CONTINUE'  proceed to analysis (Passed, or Warning accepted)
      'REFINE'    apply 50% mesh refinement and re-run diagnostics
      'RETRY'     regenerate the mesh as-is        (Failed only)
      'GATE'      return to gate placement          (Failed only)
      'CANCEL'    stop the workflow
    """
    status = report.get("status", "Failed")
    counts = report.get("counts", {})

    if status == "Passed":
        question = ("Diagnostics passed. Review the values above, then click "
                    "Continue to start the analysis.")
        buttons = [("CONTINUE", "Continue")]
    elif status == "Warning":
        question = ("Diagnostics completed with {0} warning(s). Continue with "
                    "the current mesh, or refine the mesh by 50% and re-check?"
                    .format(counts.get("warning", 0)))
        buttons = [("CONTINUE", "Continue"),
                   ("REFINE", "Refine Mesh (50%)"),
                   ("CANCEL", "Cancel Workflow")]
    else:
        question = ("Diagnostics failed — the analysis is stopped. Choose a "
                    "recovery option. (If you fix the mesh manually in Synergy "
                    "first, choose Retry to re-check it.)")
        buttons = [("REFINE", "Refine Mesh (50%)"),
                   ("RETRY", "Retry Meshing"),
                   ("GATE", "Re-place Gate"),
                   ("CANCEL", "Cancel Workflow")]

    detail_text = _mesh_diag_plain_text(report)

    # The whole report goes to the panel, which draws the full table and only
    # then accepts a click. A None answer means the dialog was never shown --
    # fall back to a modal box carrying the same values rather than defaulting.
    decision = None
    try:
        import ui_bridge
        decision = ui_bridge.request_mesh_diagnostics_report(
            report, question, buttons, detail_text=detail_text)
    except Exception as exc:
        log("Mesh diagnostics prompt failed to reach the panel: {0}".format(exc))

    if decision is None:
        decision = _mesh_diag_blocking_fallback(report, question, buttons, log)

    log("Mesh diagnostics decision: {0} (status was {1}).".format(decision, status))
    return decision


def _display_mesh_diagnostics_html(report, log):
    """Legacy HTML/HTA rendering of the same report. Unused by the workflow
    (the panel path above replaced it) and kept only so the markup that fed
    the old dialog is not lost."""
    status = report.get("status", "Failed")
    counts = report.get("counts", {})

    if status == "Passed":
        banner = ('<div class="tot ok">Mesh diagnostics PASSED '
                  '&mdash; the mesh is ready for analysis.</div>')
    elif status == "Warning":
        banner = ('<div class="tot warn">Mesh diagnostics found {0} '
                  'warning(s) &mdash; review before continuing.</div>'
                  .format(counts.get("warning", 0)))
    else:
        banner = ('<div class="tot bad">Mesh diagnostics FAILED &mdash; {0} '
                  'error(s) found. The analysis will not start.</div>'
                  .format(counts.get("error", 0)))

    rows = ['<tr><td>Overall mesh status</td><td class="{0}"><b>{1}</b></td></tr>'
            .format({"Passed": "ok", "Warning": "warn"}.get(status, "bad"), status),
            "<tr><td>Total mesh elements</td><td>{0}</td></tr>".format(
                report.get("total_elements", 0))]
    detail_lines = ["Overall mesh status : {0}".format(status),
                    "Mesh type           : {0}".format(report.get("mesh_type", "")),
                    "Total mesh elements : {0}".format(report.get("total_elements", 0)),
                    ""]
    for it in report.get("items", []):
        value = it["value"]
        vtxt = ("{0:.3f}".format(value).rstrip("0").rstrip(".")
                if isinstance(value, float) else str(value))
        cls = {"info": "", "warning": "warn", "error": "bad"}[it["severity"]]
        cell = ('<td class="{0}">{1}</td>'.format(cls, _mesh_esc(vtxt))
                if cls else "<td>{0}</td>".format(_mesh_esc(vtxt)))
        rows.append("<tr><td>{0}</td>{1}</tr>".format(_mesh_esc(it["label"]), cell))
        tag = {"info": "INFO   ", "warning": "WARNING", "error": "ERROR  "}[it["severity"]]
        detail_lines.append("[{0}] {1} = {2}  {3}".format(
            tag, it["label"], vtxt, it["note"]).rstrip())
    if report.get("unavailable"):
        detail_lines.append("")
        detail_lines.append("Not exposed by this Moldflow API version:")
        for label in report["unavailable"]:
            detail_lines.append("  - {0}".format(label))

    summary_html = banner + '<table class="sm">' + "".join(rows) + "</table>"

    if status == "Passed":
        question = ("<b>Diagnostics passed.</b><br>"
                    "Click Continue to start the analysis.")
        buttons = [("CONTINUE", "Continue")]
        default = "CONTINUE"
    elif status == "Warning":
        question = ("<b>Diagnostics completed with warnings.</b><br>"
                    "Continue with the current mesh, or refine the mesh by 50% "
                    "and re-check?")
        buttons = [("CONTINUE", "Continue"),
                   ("REFINE", "Refine Mesh (50%)"),
                   ("CANCEL", "Cancel Workflow")]
        default = "CANCEL"
    else:
        question = ("<b>Diagnostics failed &mdash; the analysis is stopped.</b><br>"
                    "Choose a recovery option. (If you fix the mesh manually in "
                    "Synergy first, choose Retry to re-check it.)")
        buttons = [("REFINE", "Refine Mesh (50%)"),
                   ("RETRY", "Retry Meshing"),
                   ("GATE", "Re-place Gate"),
                   ("CANCEL", "Cancel Workflow")]
        default = "CANCEL"

    decision = show_mesh_choice_dialog(
        "Mesh Diagnostics", "Mesh Diagnostics ({0})".format(status),
        summary_html, "\n".join(detail_lines), question, buttons, default, log)
    log("Mesh diagnostics decision: {0} (status was {1}).".format(decision, status))
    return decision

def wait_for_import_dialog_dismissed(log):
    """Block until Synergy's 'File imported successfully' message box is closed.

    The observer launches this diagnostics process as soon as CAD bodies appear
    (right after run_startup.vbs calls ImportFile), which is BEFORE the user has
    clicked OK on the vbs's import-complete box. Without this wait the Automation
    Workflow dialog opens on top of that box (both visible at once). We match the
    box by its window caption and wait for it to disappear — fully self-contained,
    no handshake file and no change to the installed vbs required.
    """
    try:
        import ctypes
        import time as _t
        find_window = ctypes.windll.user32.FindWindowW
        # Captions used by run_startup.vbs for the two import outcomes.
        titles = ("Moldflow Insight - Import Complete", "Import Warning")

        def box_open():
            for t in titles:
                try:
                    if find_window(None, t):
                        return True
                except Exception:
                    pass
            return False

        # The box is shown synchronously right after ImportFile, but give it a
        # short window to appear in case diagnostics got here first.
        #
        # 1.5s, not 8s: run_startup.vbs is silent now and shows neither of
        # these captions, so on every normal run this loop waits out its full
        # window for a box that will never appear -- dead time between the end
        # of diagnostics and the model appearing. A box that IS shown is
        # posted synchronously with the import, so it is already up long
        # before we get here; the shorter window still catches it.
        appear_deadline = _t.time() + 1.5
        while _t.time() < appear_deadline and not box_open():
            _t.sleep(0.3)

        if not box_open():
            return  # never appeared (already dismissed, or not applicable)

        log("Waiting for the 'File imported successfully' dialog to be closed...")
        gone_deadline = _t.time() + 300.0
        while _t.time() < gone_deadline and box_open():
            _t.sleep(0.4)
        log("Import dialog dismissed — showing the Automation Workflow dialog.")
    except Exception as e:
        try:
            log("Import-dialog wait skipped: {0}".format(e))
        except Exception:
            pass

# ===========================================================================
#  Analysis summary
# ---------------------------------------------------------------------------
#  Produces the closing "Analysis Summary" of the deck: per-result statistics
#  plus a flagged overall assessment, in the style of Moldflow's AI Assistant.
#
#  WHY IT IS BUILT RATHER THAN FETCHED. The AI Assistant is a CLOUD service --
#  aiassistant.exe (shipped in both the Insight and Synergy bin folders) talks
#  to https://developer.api.autodesk.com/mfsc/v1 behind Autodesk credentials.
#  It is not part of the Synergy COM API (the API has 39 classes and none of
#  them is an advisor/criteria/summary class) and it caches nothing locally, so
#  there is no supported way to read its output. Everything below is therefore
#  derived independently from the study's own datasets.
#
#  WHAT THE API DOES AND DOES NOT GIVE US:
#    * Enumeration       -- PlotMgr.GetFirstPlot/GetNextPlot. Dynamic, so the
#                           summary covers whatever THIS study produced.
#    * Name / id / type  -- Plot.GetName/GetDataID/GetDataType.
#    * Units             -- NOT from the API. They come from Moldflow's own
#                           data\dat\results.dat catalogue (see
#                           load_results_catalogue).
#    * min / max         -- Plot.GetMinValue/GetMaxValue exist but return the
#                           LEGEND SCALE bounds, which follow GetScaleOption and
#                           can be overwritten by SetMinValue. They are not a
#                           trustworthy statistic and are deliberately not used.
#    * mean / std dev    -- no API at all.
#  So the statistics are computed here from the raw per-node/element arrays
#  returned by PlotMgr.GetScalarData(), which is the same call the report's
#  existing value-range line already relies on.
# ===========================================================================

_RESULTS_CATALOGUE = {"by_name": None, "by_id": None}


def load_results_catalogue(log=None):
    """Moldflow's own result catalogue: {name.lower(): (dsid, unit)} and
    {dsid: (name, unit)}.

    Parsed from data\\dat\\results.dat in the Insight install -- the same file
    the RESULT_GUIDE units were taken from by hand. It declares every dataset
    the solver can produce (1024 definitions on the 2027 install) with its id,
    component count, display name and SI storage unit, which is the only place
    units are available at all: nothing on Plot or PlotMgr reports one.

    Cached for the process. Returns ({}, {}) if the file cannot be read, and
    the caller then falls back to RESULT_GUIDE's hand-maintained units."""
    if _RESULTS_CATALOGUE["by_name"] is not None:
        return _RESULTS_CATALOGUE["by_name"], _RESULTS_CATALOGUE["by_id"]

    by_name, by_id = {}, {}
    try:
        import glob
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 r"C:\Program Files"]
        paths = []
        for r in roots:
            paths += glob.glob(os.path.join(
                r, "Autodesk", "Moldflow Insight*", "data", "dat", "results.dat"))
            paths += glob.glob(os.path.join(
                r, "Autodesk", "Moldflow Synergy*", "data", "dat", "results.dat"))
        if paths:
            text = open(paths[0], "rb").read().decode("latin-1")
            # (TYPE) { <id> <ncomp> ... NAME { "<name>" } DEPT { "<desc>" "<unit>" } }
            pattern = re.compile(
                r'(NDDT|ELDT|HLDT|NMDT|XYDT)\s*\{\s*(\d+)\s+[\d\s]*?'
                r'NAME\s*\{\s*"([^"]+)"\s*\}\s*'
                r'DEPT\s*\{\s*"([^"]*)"\s*"([^"]*)"', re.S)
            for _kind, dsid, name, _desc, unit in pattern.findall(text):
                try:
                    did = int(dsid)
                except ValueError:
                    continue
                key = name.strip().lower()
                by_name.setdefault(key, (did, unit.strip()))
                by_id.setdefault(did, (name.strip(), unit.strip()))
            if log:
                log("Result catalogue: {0} dataset definitions read from {1}.".format(
                    len(by_id), paths[0]))
        elif log:
            log("Result catalogue: results.dat not found; falling back to the "
                "built-in unit table.")
    except Exception as e:
        if log:
            log("Result catalogue could not be read ({0}); falling back to the "
                "built-in unit table.".format(e))

    _RESULTS_CATALOGUE["by_name"], _RESULTS_CATALOGUE["by_id"] = by_name, by_id
    return by_name, by_id


def summarise_values(values):
    """n / min / max / mean / std of a raw dataset array.

    Sentinel and non-finite entries are dropped: Moldflow pads unfilled or
    inapplicable entities with very large magnitudes, and letting those through
    turns a mean into nonsense. Population standard deviation (the spread of
    the result across the part), computed in one pass."""
    nums = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and abs(f) < 1e30:          # f == f rejects NaN
            nums.append(f)
    if not nums:
        return None
    n = len(nums)
    lo, hi = min(nums), max(nums)
    mean = sum(nums) / n
    var = sum((x - mean) ** 2 for x in nums) / n
    return {"n": n, "min": lo, "max": hi, "mean": mean,
            "std": var ** 0.5, "values": nums}


# What a value in each display unit can physically be for an injection-moulding
# result. Used ONLY to catch a double conversion -- deliberately wide, so a
# genuine value is never rejected. A range that had to be tightened to work
# would mean the discriminator is wrong, not that the bound needs tuning.
# (low, high, smallest credible PEAK). The peak matters as much as the bounds:
# scaling an already-converted pressure by 1e-6 lands on 1.06e-06 MPa, which is
# inside any sane range and still not a pressure field -- a filling analysis
# does not peak at a millionth of a MPa. Both tests together catch a double
# conversion that either bound alone would miss.
_PLAUSIBLE_RANGE = {
    "degC": (-50.0, 600.0, None),      # mould coolant to melt; nothing is at -213
    "MPa": (0.0, 500.0, 1e-3),         # injection and cavity pressures
    "kN": (0.0, 100000.0, 1.0),        # clamp force, up to the largest presses
    "mm": (0.0, 20000.0, 1e-3),        # a moulded part is not a kilometre long
}


def convert_stat_unit(stat, unit):
    """Re-express a statistic in the unit an engineer reads, returning
    (converted_stat, display_unit).

    The arrays come back in results.dat's SI STORAGE unit, so a temperature is
    343.15 and a pressure is 8.0e7 -- neither is comparable to the reference
    targets ("below 93.5 degC", "less than 80 MPa") without this.

    Angles are the one special case. results.dat declares 'rad' for weld
    lines, but the values this build returns are plainly already degrees (a
    measured 11.34-164.6 is a sane spread of meeting angles; as radians it
    would be 650-9400 degrees). So the declared unit is trusted only when the
    data is actually consistent with it."""
    if not stat:
        return stat, unit
    out = dict(stat)
    scale, shift, disp = 1.0, 0.0, unit

    if unit == "K":
        shift, disp = -273.15, "degC"
    elif unit == "K(d)":                 # a temperature DIFFERENCE, not a level
        disp = "degC"
    elif unit == "Pa":
        scale, disp = 1e-6, "MPa"
    elif unit == "Pa-s":
        disp = "Pa-s"
    elif unit == "N":
        scale, disp = 1e-3, "kN"
    elif unit == "m":
        scale, disp = 1e3, "mm"
    elif unit == "rad":
        if stat.get("max", 0) > 6.3:     # already degrees -- see docstring
            disp = "deg"
        else:
            scale, disp = 180.0 / 3.141592653589793, "deg"

    # Does the conversion produce a physically possible number? results.dat
    # declares the STORAGE unit, but this build hands several datasets back
    # already in engineering units, and converting those a second time is how
    # the deck came to print a flow front at -73 degC (200 - 273.15), a wall
    # shear stress of 1.06e-06 MPa (1.06 MPa scaled again by 1e-6) and a flow
    # length of 1,085,950 mm (1,086 mm scaled by 1e3).
    #
    # Same principle the radian branch above already uses: trust the declared
    # unit only while the data agrees with it. If converting lands outside what
    # the quantity can physically be, and the RAW values are inside it, the
    # data was already in display units -- so leave it alone.
    if (scale, shift) != (1.0, 0.0):
        lo, hi, floor = _PLAUSIBLE_RANGE.get(disp, (None, None, None))
        if lo is not None:
            raw_lo, raw_hi = stat.get("min", 0.0), stat.get("max", 0.0)
            conv_lo = raw_lo * scale + shift
            conv_hi = raw_hi * scale + shift
            converted_ok = (lo <= conv_lo <= hi) and (lo <= conv_hi <= hi)
            if converted_ok and floor is not None and abs(raw_hi) > 0:
                converted_ok = abs(conv_hi) >= floor
            raw_ok = (lo <= raw_lo <= hi) and (lo <= raw_hi <= hi)
            if raw_ok and floor is not None and abs(raw_hi) > 0:
                raw_ok = abs(raw_hi) >= floor
            if not converted_ok and raw_ok:
                scale, shift = 1.0, 0.0

    for k in ("min", "max", "mean"):
        out[k] = stat[k] * scale + shift
    out["std"] = stat["std"] * scale      # a spread never takes the offset
    out.pop("values", None)
    return out, disp


def format_stat(value, unit):
    """One statistic, formatted so it reads like the reference targets."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(v) >= 1000:
        text = "{0:,.0f}".format(v)
    elif abs(v) >= 1:
        text = "{0:.3g}".format(v)
    elif v == 0:
        text = "0"
    else:
        text = "{0:.4g}".format(v)
    return "{0} {1}".format(text, unit).strip()


# Rule table for the flagged assessment.
#
# Every threshold here is either Moldflow's own (read from the material or
# reported by the solver) or is quoted from the same Autodesk reference deck
# that RESULT_GUIDE's "targets" come from -- see the note on each rule. Nothing
# is invented. A result the study did not produce simply contributes no
# finding, which is what keeps the summary specific to each design.
#
# Each entry: dataset name (lower-case, as Moldflow names it) -> callable
# taking (stat_in_display_units, unit) and returning
# (level, headline, detail) or None. level is "red" / "amber" / "green".

def _rule_air_traps(s, u):
    # Any air trap that cannot vent is a defect; the count is what matters.
    trapped = sum(1 for v in s.get("_raw", []) if v > 0.5)
    if trapped <= 0:
        return ("green", "No air traps detected.", "")
    return ("red", "Air traps detected \u2014 improve venting or gate location.",
            "{0} trapped-air node(s) flagged. Air traps should fall on a "
            "parting line or ejector pin where they can vent.".format(trapped))


def _rule_weld_lines(s, u):
    # The deck treats weld lines as a cosmetic/strength risk to be relocated,
    # not a pass/fail number, so this reports the angles and flags presence.
    if s["n"] <= 0:
        return None
    return ("amber", "Weld lines present \u2014 consider gate relocation.",
            "Meeting angle averages {0} and peaks at {1}. Move weld lines away "
            "from cosmetic surfaces and high-stress regions.".format(
                format_stat(s["mean"], u), format_stat(s["max"], u)))


def _rule_shear_rate(s, u):
    # RESULT_GUIDE: "the reference deck's worked example uses a maximum
    # allowable shear rate of 24,000 1/s". Superseded by the grade's own limit
    # when the material provides one (see material_limits).
    limit = s.get("_limit") or 24000.0
    if s["max"] > limit:
        return ("red", "Excessive shear rate \u2014 review gate and runner dimensions.",
                "Peak {0} exceeds the {1} limit.".format(
                    format_stat(s["max"], u), format_stat(limit, u)))
    return ("green", "Shear rate within limits.",
            "Peak {0} against a {1} limit.".format(
                format_stat(s["max"], u), format_stat(limit, u)))


def _rule_shear_stress(s, u):
    # RESULT_GUIDE: worked example uses 0.24 MPa; ~1% of tensile strength.
    limit = s.get("_limit") or 0.24
    if s["max"] > limit:
        return ("red", "Excessive shear stress \u2014 review gate and runner dimensions.",
                "Peak {0} exceeds the {1} limit.".format(
                    format_stat(s["max"], u), format_stat(limit, u)))
    return ("green", "Shear stress within limits.", "")


def _rule_volumetric_shrinkage(s, u):
    # The deck's concern is VARIATION across the part, which is what drives
    # warpage; the absolute level is a material/pack question.
    spread = s["max"] - s["min"]
    if spread > 4.0:
        return ("amber", "High shrinkage variation \u2014 potential warpage risk.",
                "Varies {0} across the part ({1} to {2}). Rebalance the pack "
                "profile or wall sections.".format(
                    format_stat(spread, u), format_stat(s["min"], u),
                    format_stat(s["max"], u)))
    return ("green", "Shrinkage variation acceptable.",
            "{0} across the part.".format(format_stat(spread, u)))


def _rule_ejection_time(s, u):
    # Spread in time-to-ejection is the standard read on cooling uniformity.
    spread = s["max"] - s["min"]
    if s["mean"] > 0 and spread > s["mean"]:
        return ("amber", "Uneven cooling \u2014 cooling layout should be reviewed.",
                "Time to reach ejection temperature ranges {0} to {1} "
                "(mean {2}).".format(format_stat(s["min"], u),
                                     format_stat(s["max"], u),
                                     format_stat(s["mean"], u)))
    return ("green", "Cooling reasonably uniform.",
            "Ejection time {0} to {1}.".format(
                format_stat(s["min"], u), format_stat(s["max"], u)))


def _rule_injection_pressure(s, u):
    # RESULT_GUIDE: "the cavity should fill at less than 80 MPa, and the cavity
    # and feed system combined should not exceed 100 MPa."
    #
    # A peak of exactly zero is not a pass, it is a dataset that did not
    # report. "Pressure at injection location" is a pressure-vs-time XY trace
    # and can come back as a single 0.0 sample: study 40 did exactly that,
    # while "Pressure" for the same study peaked at 32.7 MPa. Passing that
    # through printed a green "Injection pressure within guide values. Peak
    # 0 MPa." on a customer report -- a clean bill of health derived from no
    # measurement at all. Stay silent instead; the pressure is still tabulated
    # in the statistics, and the deck's process slide sources its own figure
    # from the candidate datasets in _ACHIEVED_FIELDS.
    if not s.get("max"):
        return None
    if s["max"] > 100.0:
        return ("red", "Injection pressure above the 100 MPa guide.",
                "Peak {0}. Review gate size, wall thickness or fill time.".format(
                    format_stat(s["max"], u)))
    if s["max"] > 80.0:
        return ("amber", "Injection pressure above the 80 MPa cavity guide.",
                "Peak {0}.".format(format_stat(s["max"], u)))
    return ("green", "Injection pressure within guide values.",
            "Peak {0}.".format(format_stat(s["max"], u)))


# --------------------------------------------------------------------------- #
# Results the in-product AI Assistant flags that the rules above did not cover.
#
# The rules above all cite an Autodesk source. These cannot, because no
# published limit exists for them, so each one is either SELF-REFERENTIAL
# (it measures variation within this study, which needs no external authority)
# or driven by a number the SOLVER itself sets. Where neither is possible the
# threshold is left as None and the rule stays silent rather than inventing a
# limit -- set it in HEURISTIC_LIMITS to switch the check on for your parts.
# --------------------------------------------------------------------------- #

HEURISTIC_LIMITS = {
    # Moldflow clamps computed viscosity at 1e6 Pa-s. Hitting the clamp is the
    # solver reporting frozen/stagnant material, not a tuning choice of ours.
    "viscosity_ceiling_pas": 1.0e6,
    # Cavity-to-cavity imbalance, as a fraction of the heaviest cavity.
    # Self-referential: a balanced tool fills its cavities to the same weight.
    "cavity_weight_imbalance": 0.10,
    # Coefficient of variation (sd/mean) of pressure at V/P switchover.
    # Self-referential: uniform switchover pressure is the design intent.
    "vp_pressure_cv": 0.50,
    # No published limit for either of these. The AI Assistant flags them with
    # an explicit hedge ("may be too long depending on material/wall
    # thickness"), which is a judgement about a specific part, not a rule.
    # Set a number to enable; left None they are tabulated but never flagged.
    "extension_rate_abs": None,   # 1/s
    "fill_time_s": None,          # s
}


def _rule_viscosity(s, u):
    # Only meaningful against the solver's own ceiling, so only applied when
    # the values really are in Pa-s -- a converted unit would compare a number
    # against a limit expressed in something else.
    ceiling = HEURISTIC_LIMITS.get("viscosity_ceiling_pas")
    if not ceiling or "pa" not in str(u).lower().replace("·", ""):
        return None
    if s["max"] >= ceiling * 0.999:
        return ("red", "Viscosity at the solver ceiling — frozen or stagnant "
                       "regions during fill.",
                "Peak {0} is the solver's {1} clamp, which marks material that "
                "stopped flowing. Check for short shot or cold slug risk.".format(
                    format_stat(s["max"], u), format_stat(ceiling, u)))
    return ("green", "Viscosity within the solver range.", "")


def _rule_cavity_weight(s, u):
    # Imbalance between the lightest and heaviest cavity, relative to the
    # heaviest -- a runner-balance question, answered by this study alone.
    frac = HEURISTIC_LIMITS.get("cavity_weight_imbalance")
    if not frac or s["n"] <= 1 or not s["max"]:
        return None
    spread = s["max"] - s["min"]
    if spread > abs(s["max"]) * frac:
        return ("amber", "Cavity weight imbalance — review runner balance.",
                "Cavities range {0} to {1}, a {2:.0f}% spread. In a multi-cavity "
                "tool this points at an unbalanced runner system.".format(
                    format_stat(s["min"], u), format_stat(s["max"], u),
                    100.0 * spread / abs(s["max"])))
    return ("green", "Cavity weights consistent.", "")


def _rule_vp_switchover_pressure(s, u):
    # Non-uniformity, not magnitude: a wide spread at switchover means parts of
    # the cavity change over under very different pressures.
    cv_limit = HEURISTIC_LIMITS.get("vp_pressure_cv")
    if not cv_limit or not s["mean"]:
        return None
    cv = abs(s["std"] / s["mean"])
    if cv > cv_limit:
        return ("amber", "Non-uniform pressure at V/P switchover.",
                "Averages {0} against a peak of {1} (spread {2}). Uneven "
                "switchover pressure drives inconsistent packing.".format(
                    format_stat(s["mean"], u), format_stat(s["max"], u),
                    format_stat(s["std"], u)))
    return ("green", "Pressure at V/P switchover is uniform.", "")


def _rule_extension_rate(s, u):
    limit = HEURISTIC_LIMITS.get("extension_rate_abs")
    if not limit:
        return None
    peak = max(abs(s["min"]), abs(s["max"]))
    if peak > limit:
        return ("amber", "High extensional flow — check gate and transitions.",
                "Extension rate reaches {0} (range {1} to {2}). Severe "
                "elongational flow affects fibre orientation and "
                "stress.".format(format_stat(peak, u), format_stat(s["min"], u),
                                  format_stat(s["max"], u)))
    return ("green", "Extensional flow within the configured limit.", "")


def _rule_fill_time(s, u):
    limit = HEURISTIC_LIMITS.get("fill_time_s")
    if not limit:
        return None
    if s["max"] > limit:
        return ("amber", "Fill time longer than the configured target.",
                "Fills in {0} against a {1} target. Verify against the grade's "
                "recommended fill time for this wall section.".format(
                    format_stat(s["max"], u), format_stat(limit, u)))
    return ("green", "Fill time within the configured target.",
            "Fills in {0}.".format(format_stat(s["max"], u)))


SUMMARY_RULES = {
    "air traps": _rule_air_traps,
    "air traps, including air vents": _rule_air_traps,
    "weld lines": _rule_weld_lines,
    "shear rate": _rule_shear_rate,
    "shear rate, bulk": _rule_shear_rate,
    "shear rate, maximum": _rule_shear_rate,
    "shear stress at wall": _rule_shear_stress,
    "volumetric shrinkage": _rule_volumetric_shrinkage,
    "average volumetric shrinkage": _rule_volumetric_shrinkage,
    "volumetric shrinkage at ejection": _rule_volumetric_shrinkage,
    "time to reach ejection temperature": _rule_ejection_time,
    "pressure at injection location": _rule_injection_pressure,
    "injection pressure": _rule_injection_pressure,
    "viscosity": _rule_viscosity,
    "cavity weight": _rule_cavity_weight,
    "pressure at v/p switchover": _rule_vp_switchover_pressure,
    "extension rate": _rule_extension_rate,
    "fill time": _rule_fill_time,
}

# Datasets always worth tabulating even when no rule fires, so the summary
# reads like the AI Assistant's statistics block. Anything else the study
# produced is still enumerated -- this only fixes the ORDER of the familiar
# ones, it does not limit the set.
SUMMARY_PREFERRED = [
    "air traps", "weld lines", "fill time", "volumetric shrinkage",
    "average volumetric shrinkage", "pressure at v/p switchover",
    "time to reach ejection temperature", "shear rate", "shear rate, maximum",
    "shear stress at wall", "viscosity", "cavity weight",
    "pressure at injection location", "clamp force", "temperature",
    "bulk temperature", "frozen layer fraction",
]


def material_limits(sy, log):
    """The grade's own limits, so a rule prefers Moldflow's number to a quoted
    one: {"shear rate": x, "shear stress at wall": y}.

    These are real material fields (the solver prints "Maximum shear rate" and
    "Maximum shear stress at wall" from them), read through the same Property
    route the workflow already uses for mould/melt temperatures. Best-effort --
    an unavailable field just leaves the rule on its documented default."""
    limits = {}
    try:
        study_doc = sy.StudyDoc()
        prop_ed = sy.PropertyEditor()
        if study_doc is None or prop_ed is None:
            return limits
        # 21000 == thermoplastic material property set.
        for ptype in (21000,):
            for prop in _iter_props_of_type(prop_ed, ptype):
                for field, key in ((3050, "shear rate"),
                                   (3060, "shear stress at wall")):
                    try:
                        v = prop.FieldValue(field)
                        if v is None:
                            continue
                        f = float(str(v).split()[0])
                        if f > 0:
                            limits.setdefault(key, f)
                    except Exception:
                        continue
    except Exception as e:
        log("Material limits unavailable ({0}); rules use their documented "
            "defaults.".format(e))
    if limits:
        log("Material limits read from the grade: {0}".format(limits))
    return limits


def _arr_values(a):
    """A Synergy DoubleArray/IntegerArray as a Python list."""
    for m in ("ToVBSArray", "to_list", "to_vb_array"):
        try:
            f = getattr(a, m)
            if callable(f):
                r = f()
                if r is not None:
                    return list(r)
        except Exception:
            pass
    return []


def fetch_dataset_values(sy, plot_mgr, did):
    """Every readable number in a dataset, whatever KIND of dataset it is.

    Returns (values, kind_label).

    This exists because asking every dataset for scalar data quietly loses a
    third of the study. PlotMgr exposes five different fetches and a dataset
    answers exactly one of them:

        GetScalarData     node/element data with 1 component
        GetVectorData     3 components -- "Deflection, all effects" is this one,
                          which is why warpage never reached the summary
        GetTensorData     6 components (orientation, stress tensors)
        GetNonmeshData    XY plots (clamp force, ram speed)
        GetHighlightData  flagged entities (air traps, weld/pathline sets)

    The kind comes from GetDataType (LBDT/LYDT/NDDT/ELDT/NMDT/TXDT/HLDT) plus
    GetDataNbComponents, so the right call is made rather than guessed. The old
    scalar-then-nonmesh pair is kept as a last resort for a build that will not
    report its own types.

    A vector is summarised by its MAGNITUDE, which for a deflection result is
    the resultant displacement -- the number Moldflow itself quotes. A tensor
    has no meaningful single magnitude, so it is reported as unreadable rather
    than reduced to something invented.
    """
    def _dbl():
        return sy.CreateDoubleArray()

    def _int():
        return sy.CreateIntegerArray()

    # WHICH time step to read. `aIndpValues` is an INPUT to the fetch: it says
    # which value of the dataset's independent variable you want. Handing it an
    # EMPTY array -- which is what this code did for years -- selects nothing,
    # so every transient result came back with no values while the static ones
    # read fine. That is exactly the split in the logs: Fill time, Flow length
    # and the "at end of fill" results were readable; Density, Pressure,
    # Temperature, Shear rate, Velocity and Viscosity were not.
    #
    # The last value is the one the deck wants: end of filling.
    def _indp():
        arr = _dbl()
        try:
            steps = _dbl()
            plot_mgr.GetIndpValues(did, steps)
            vals = _arr_values(steps)
            if vals:
                arr.AddDouble(float(vals[-1]))
        except Exception:
            pass
        return arr

    dtype = ""
    ncomp = 0
    try:
        dtype = str(call_member(plot_mgr, "GetDataType", did) or "").strip().upper()
    except Exception:
        dtype = ""
    try:
        ncomp = int(call_member(plot_mgr, "GetDataNbComponents", did) or 0)
    except Exception:
        ncomp = 0

    # Kinds that hold no numbers at all. Reported, not attempted.
    if dtype in ("LBDT", "LYDT", "TXDT"):
        return [], dtype.lower() or "non-numeric"

    if dtype == "NMDT":
        try:
            buf = _dbl()
            plot_mgr.GetNonmeshData(did, _indp(), buf)
            return _arr_values(buf), "xy"
        except Exception:
            pass

    if dtype == "HLDT":
        try:
            buf = _dbl()
            plot_mgr.GetHighlightData(did, _indp(), buf)
            vals = _arr_values(buf)
            if vals:
                return vals, "highlight"
        except Exception:
            pass

    if ncomp == 3:
        try:
            va, vb, vc = _dbl(), _dbl(), _dbl()
            plot_mgr.GetVectorData(did, _indp(), _int(), va, vb, vc)
            a, b, c = _arr_values(va), _arr_values(vb), _arr_values(vc)
            n = min(len(a), len(b), len(c))
            if n:
                return [(a[i] ** 2 + b[i] ** 2 + c[i] ** 2) ** 0.5
                        for i in range(n)], "vector magnitude"
        except Exception:
            pass

    if ncomp == 6:
        # Deliberately not reduced: no single number represents a tensor, and a
        # made-up one would be worse than the gap it fills.
        return [], "tensor"

    # Scalar, and the fallback path for a build that reports nothing useful
    # from GetDataType.
    for kind, call in (
            ("scalar", lambda buf: plot_mgr.GetScalarData(did, _indp(), _int(), buf)),
            ("xy", lambda buf: plot_mgr.GetNonmeshData(did, _indp(), buf)),
            ("highlight", lambda buf: plot_mgr.GetHighlightData(did, _indp(), buf))):
        try:
            buf = _dbl()
            call(buf)
            vals = _arr_values(buf)
            if vals:
                return vals, kind
        except Exception:
            continue
    return [], (dtype.lower() or "unknown")


def _label_text(v):
    """A COM return value as display text, or None if it is not text at all.

    The late-bound wrapper hands back whatever a member returns, and str() on
    an IDispatch yields '<Syn <COMObject <unknown>>>' -- which is exactly what
    turned up in the Imported Model row. The API documents GetPartCadNames as
    returning a String; on this build it answers with an object, so the type is
    checked rather than trusted.
    """
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (str, int, float)):
        return None
    s = str(v).strip()
    if not s or s.startswith("<") or "COMObject" in s:
        return None
    return s


def _ask_member(obj, *names):
    """First of `names` this object answers with usable text, or None.

    Per-member rather than one try around the lot: a build missing ONE of these
    must not cost the others, which is how the journey card lost all four
    fields at once.
    """
    if obj is None:
        return None
    for nm in names:
        try:
            v = call_member(obj, nm)
        except Exception:
            continue
        text = _label_text(v)
        if text:
            return text
    return None


def sync_journey_from_study(sy, study_doc, log, include_diagnostics=False):
    """Fill the User Journey card from the study that is actually open.

    Called TWICE on purpose. The card used to be populated once, just before
    Phase 2 -- roughly 240 lines after CAD diagnostics starts -- so for the
    whole of the diagnostics stage Project Name, Location and Imported Model
    sat on "Pending..." even though all three were known the moment the study
    was resolved. Calling it as soon as `study_doc` exists fixes that; calling
    it again later picks up the analysis sequence, which is only chosen in
    Phase 2.

    `include_diagnostics` is False for the early call: writing "No issues
    found" before the diagnostics have run would mark a stage complete that
    has not started.

    The ANALYSIS SEQUENCE is deliberately NOT published here. Only the three
    identity rows are facts at this point; the sequence is the user's choice
    and is not made until Phase 2. Publishing the study's current value early
    showed "Fill" as already chosen before the user had been asked -- and
    ui_bridge already records the real answer when the prompt is answered.
    The confirmed value is republished after Phase 2 verifies it.

    Getters per the API help -- Project has Name and Path; StudyDoc has
    StudyName, DisplayName, AnalysisSequenceDescription and GetPartCadNames().
    The old code asked StudyDoc for Name and Path, neither of which exists, and
    swallowed both failures.
    """
    try:
        import ui_bridge
    except Exception:
        return {}

    project_obj = None
    try:
        project_obj = call_member(sy, "project")
    except Exception:
        project_obj = None

    updates = {}
    for field, value in (
            ("project_name", _ask_member(project_obj, "Name")),
            ("location", _ask_member(project_obj, "Path")),
            # "Imported Model" means the CAD that was imported, which the study
            # can name directly -- better than the .sdy filename.
            ("cad_file", _ask_member(study_doc, "GetPartCadNames")
             or _ask_member(study_doc, "StudyName"))):
        if value:
            updates[field] = value
    if include_diagnostics:
        updates["cad_diagnostics"] = "No issues found"

    if updates:
        try:
            ui_bridge.update_state("params", updates)
        except Exception as e:
            log("User journey: could not publish study details ({0}).".format(e))
    log("User journey: project={0!r} location={1!r} model={2!r} sequence={3!r}"
        .format(updates.get("project_name"), updates.get("location"),
                updates.get("cad_file"), updates.get("analysis_sequence")))
    return updates


# Datasets the solver produces to DRAW something, not to be measured. They are
# still perfectly good plots -- the deck screenshots them like any other -- but
# statistics over them mean nothing: "Grow from" is a fill-order index that is
# 1 everywhere, "Pathlines" and the weld-surface sets are geometry for the
# viewer, and "Polymer fill region" is a region flag. Listing them in Result
# Statistics beside real engineering values invites a reader to treat them as
# measurements, which is worse than leaving them out.
NON_ENGINEERING_DATASETS = {
    "grow from",
    "pathlines",
    "polymer fill region",
    "weld surface formation (3d)",
    "weld surface movement (3d)",
    "weld surface formation",
    "weld surface movement",
}


def collect_component_dimensions(sy, log):
    """The part's own dimensions, for the deck's "Component details" slide.

    Returns {"geometry": [(label, value)], "notes": [str]} or None.

    Everything here is computed by `export_dimensions`, which the plugin has
    always used for its Excel dimension report -- this reuses that module
    rather than reimplementing the geometry, so there is exactly one bounding
    box calculation in the project.

    NEVER raises and never blocks the report: a study that cannot be exported,
    or a Moldflow build whose ExportModel behaves differently, costs this one
    slide and nothing else.
    """
    if not INCLUDE_COMPONENT_DETAILS:
        return None
    import time
    started = time.time()
    try:
        import export_dimensions as xd
    except Exception as e:
        log("Component details: export_dimensions unavailable ({0}).".format(e))
        return None

    try:
        summary = xd.mesh_summary(sy)
        nodes = int(summary.NodesCount()) if summary is not None else 0
        volume = summary.MeshVolume() if summary is not None else None
    except Exception as e:
        log("Component details: mesh summary unreadable ({0}).".format(e))
        return None
    if not nodes:
        log("Component details: study has no mesh; slide omitted.")
        return None

    try:
        res, warning = xd.analyze(sy, nodes)
    except Exception as e:
        log("Component details: model export failed ({0}).".format(e))
        return None
    if not res:
        log("Component details: model export produced no nodes.")
        return None
    if warning:
        log("Component details: {0}".format(warning))

    try:
        units = sy.GetUnits()
    except Exception:
        units = "Metric"
    ulen = xd._length_unit(units)
    bbox = res["bbox"]
    xs, ys, zs = bbox["x"][2], bbox["y"][2], bbox["z"][2]

    rows = [
        ("Length (X)", "{0:,.1f} {1}".format(xs, ulen)),
        ("Width (Y)", "{0:,.1f} {1}".format(ys, ulen)),
        ("Height (Z)", "{0:,.1f} {1}".format(zs, ulen)),
        ("Envelope", "{0:,.0f} x {1:,.0f} x {2:,.0f} {3}".format(
            xs, ys, zs, ulen)),
    ]
    if volume is not None:
        try:
            rows.append(("Part volume", "{0:,.1f} cm3".format(float(volume))))
        except Exception:
            pass

    notes = []
    # Wall thickness: the honest version. GetThicknessDiagnosis is a
    # fusion/midplane measurement; on a 3D tetrahedral mesh it returns ray
    # lengths, and export_dimensions already refuses to report it there. The
    # "nominal thickness" it substitutes is the smallest bounding-box
    # dimension, which for any shaped part is the part's DEPTH, not its wall.
    # Printing that on a customer deck under the heading the reference uses
    # ("Nominal wall Thickness 3.91mm") would be a wrong number stated
    # confidently, so on a 3D mesh the row is omitted and the reason is given.
    if res.get("is_3d"):
        notes.append(
            "Wall thickness is not reported: it is a Dual Domain / midplane "
            "measurement, and on a 3D tetrahedral mesh the thickness "
            "diagnosis does not return a wall thickness.")
    else:
        try:
            th = xd.thickness_stats(
                sy, min(bbox[a][2] for a in ("x", "y", "z")), units)
        except Exception:
            th = None
        if th:
            rows.append(("Wall thickness (avg)",
                         "{0:,.2f} {1}".format(th["avg"], ulen)))
            rows.append(("Wall thickness (min-max)",
                         "{0:,.2f} - {1:,.2f} {2}".format(
                             th["min"], th["max"], ulen)))

    holes = res.get("holes") or []
    if holes:
        rows.append(("Through-holes detected", "{0}".format(len(holes))))

    log("Component details: collected in {0:.1f}s "
        "({1:,.1f} x {2:,.1f} x {3:,.1f} {4}, {5} node(s)).".format(
            time.time() - started, xs, ys, zs, ulen, res["nodes"]))
    return {"geometry": rows, "notes": notes}


def _process_field(prop, tcode):
    """One process-controller field as a list of values, or None.

    Moldflow returns these through FieldValues(), which is absent, empty or
    raises for a TCode this process/sequence does not use -- all three of which
    mean the same thing here: this study does not have that setting.
    """
    try:
        fv = prop.FieldValues(tcode)
        if fv is None:
            return None
        n = int(fv.Size() or 0)
        if not n:
            return None
        return [fv.Val(i) for i in range(n)]
    except Exception:
        return None


# The process controller's own fields, from Moldflow's data\dat\process.dat --
# the WTAB lines that define the Process Settings Wizard pages name exactly
# these TCodes on property type 30011:
#
#   Fill+Pack : 11108 mold surface temp, 11002 melt temp, 10109 filling
#               control, 10310 V/P switch-over, 10704 pack/holding control,
#               11109 cooling time
#   Cool      : 11002, 10104 mold-open time, 11112 injection+packing+cooling
#
# (label, tcode, kind, value_tcode) where kind is "temp" / "time" / "mode".
#
# A "mode" field is a SELECTOR, not a number: its value indexes the option list
# in tcodes.dat. Several of them carry a companion TCode holding the number the
# dialog shows beside the dropdown -- "Cooling time [Specified] of [43.5] s" is
# two fields, not one. The companions are identified from the type-30011 field
# list in process.dat (line 120), where each sits next to the mode it belongs
# to, and from their tcodes.dat units:
#
#   11109 Cooling time                    -> 10102 ("of", s)
#   11112 Injection + packing + cooling    -> 13312 (Time, s)
#   10109 Filling control                 -> 10111 (Nominal injection time, s)
#
# An automatic run still answers "Automatic" and has no companion value, which
# is why the deck pairs this column with the achieved figures.
# (label, tcode, kind, value_tcode, value_label, requires)
#
# `value_label` is the row name to use once the companion NUMBER is what is
# being shown, because "Filling control: 6.4 s" names the dropdown rather than
# the figure beside it.
#
# `requires` is the analysis phase whose wizard page carries the field, or None
# for the ones every page has. THIS MATTERS: the process controller holds every
# field regardless of sequence, so a Fill+Pack study still answers for the Cool
# page's mold-open time and cycle time -- with defaults the wizard never showed
# and the solver never used. Reporting those as "as configured" put two
# parameters on the deck that were not settings of that run at all, and made
# the cycle time look inconsistent with the achieved fill time. Moldflow's own
# per-sequence field lists (process.dat WTAB) are the authority:
#
#   Fill      (line 567): 30011 11108 11002 10109 10310 10704
#   Fill+Pack (line 612): 30011 11108 11002 10109 10310 10704 11109
#   Cool      (line 722): 30011 11002 10104 11112
_PROCESS_FIELDS = [
    ("Mold surface temperature", 11108, "temp", None, None, None),
    ("Melt temperature", 11002, "temp", None, None, None),
    ("Filling control", 10109, "mode", 10111, "Injection time", "fill"),
    ("V/P switch-over", 10310, "mode", None, None, "fill"),
    ("Pack/holding control", 10704, "mode", None, None, "fill"),
    ("Cooling time", 11109, "mode", 10102, "Cooling time", "pack"),
    ("Mold-open time", 10104, "time", None, None, "cool"),
    ("Cycle time (inj + pack + cool)", 11112, "mode", 13312, "Cycle time",
     "cool"),
]

# Option lists, transcribed from tcodes.dat. Index -> label.
_PROCESS_MODES = {
    10109: {1: "Automatic", 2: "Injection time", 3: "Flow rate",
            5: "Relative ram speed profile", 6: "Absolute ram speed profile",
            4: "Legacy ram speed profiles"},
    10310: {0: "Automatic", 1: "By %volume filled", 8: "By ram position",
            2: "By injection pressure", 3: "By hydraulic pressure",
            4: "By clamp force", 5: "By pressure control point",
            6: "By injection time", 7: "By whichever comes first"},
    10704: {5: "Automatic", 4: "%Filling pressure vs time",
            2: "Packing pressure vs time", 1: "Hydraulic pressure vs time",
            3: "%Maximum machine pressure vs time"},
    11109: {1: "Specified", 2: "Automatic"},
    11112: {1: "Specified", 2: "Automatic"},
}

# The pack/hold PROFILE, which is where the reference report's "Hold Time" and
# "Hold Pressure" rows actually live. Which TCode holds it depends on the
# pack/holding control mode (10704), and all four have the same shape in
# tcodes.dat: DATA[0] = "Duration" (s), DATA[1] = the pressure or percentage.
#
#   10704 value -> (profile tcode, row label, unit kind)
_PACK_PROFILE = {
    1: (10706, "Hydraulic pressure", "pressure"),
    2: (10707, "Packing pressure", "pressure"),
    3: (10705, "Max machine pressure", "percent"),
    4: (10702, "Filling pressure", "percent"),
    # 5 is Automatic: the solver derives the profile, so there is none to read.
}


def _pack_profile_rows(prop, mode_value, log):
    """Hold time and hold pressure from the pack/holding profile: [(l, v)].

    The profile is a REPEATING two-column field (duration, pressure). Values
    come back interleaved -- pair 0 is Val(0)/Val(1), pair 1 is Val(2)/Val(3)
    -- which is the same layout the material temperature RANGES use, where
    Val(0)/Val(1) are the two DATA blocks Minimum/Maximum of one row. That
    reading is verified for the ranges and inferred here, so the raw values are
    logged: check them against Synergy's Pack/Holding profile table once.
    """
    try:
        entry = _PACK_PROFILE.get(int(round(float(mode_value))))
    except (TypeError, ValueError):
        return []
    if not entry:
        return []
    tcode, label, kind = entry
    vals = _process_field(prop, tcode)
    if not vals or len(vals) < 2:
        return []
    log("  pack profile {0} ({1}) raw={2}".format(tcode, label, vals[:8]))

    pairs = [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
    try:
        durations = [float(d) for d, _p in pairs]
        pressures = [float(p) for _d, p in pairs]
    except (TypeError, ValueError):
        return []
    total = sum(d for d in durations if d > 0)
    peak = max(pressures) if pressures else 0.0
    if total <= 0 and peak <= 0:
        return []

    rows = []
    if total > 0:
        rows.append(("Hold time", "{0:g} s".format(total)))
    if peak > 0:
        if kind == "percent":
            rows.append((label, "{0:g} %".format(peak)))
        else:
            # tcodes.dat declares these "Pa", but FieldValues hands back the
            # ACTIVE unit system -- the same reason the temperatures arrive in
            # degC despite being declared "K". So the number is expected in
            # MPa. A value in the thousands cannot be MPa (the field's own
            # ceiling is 5.0e+08 Pa = 500 MPa), so it is raw Pa and is scaled.
            if peak > 1000.0:
                peak = peak / 1.0e6
            rows.append((label, "{0:g} MPa".format(peak)))
    return rows


def _sequence_phases(sequence):
    """Which analysis phases a sequence string contains: set of fill/pack/cool.

    Used to show only the process fields Moldflow's own wizard would show for
    this sequence. Unrecognised text is treated as containing everything --
    printing a field that might not apply is a smaller error than silently
    dropping a real setting.
    """
    s = (sequence or "").strip().lower()
    if not s:
        return {"fill", "pack", "cool"}
    phases = set()
    if "fill" in s or "flow" in s:
        phases.add("fill")
    if "pack" in s or "flow" in s:
        # Moldflow's "Flow" sequence is fill+pack.
        phases.add("pack")
    if "cool" in s:
        phases.add("cool")
    return phases or {"fill", "pack", "cool"}


def collect_process_parameters(sy, study_doc, log, resolve=None,
                               sequence=None):
    """The moulding conditions the study was set up with.

    Returns {"configured": [(label, value)]} or None. READ ONLY -- this asks
    the process controller for its values and writes nothing, so the workflow's
    "write-back is edit-only" contract is untouched.

    Most of these fields are mode selectors rather than numbers. An automatic
    run therefore reports "Automatic" for filling control, V/P switch-over,
    pack/holding and cooling time, which is the truth and is why the deck pairs
    this with an "as achieved" column taken from the results.

    `resolve` is the workflow's own `resolve_process_controller()`, passed in
    so this reads the SAME property the study solves with. That matters: a bare
    `GetFirstProperty(30011)` can return a process-controller occurrence the
    study does not use -- root-caused against Autodesk's CustomReport.vbs, and
    the reason that resolver walks the injection property's TCode 20040 first.
    Without it this falls back to the naive lookup and says so, because a
    process table read off the wrong occurrence is worse than none.
    """
    prop = None
    if resolve is not None:
        try:
            _pe, prop = resolve()
        except Exception as e:
            log("Process parameters: controller resolution failed ({0}).".format(e))
            prop = None
    if prop is None:
        try:
            prop = sy.PropertyEditor().GetFirstProperty(30011)
            if prop is not None:
                log("Process parameters: using GetFirstProperty(30011) — this "
                    "may not be the occurrence the study solves with.")
        except Exception as e:
            log("Process parameters: process controller not found ({0}).".format(e))
    if prop is None:
        log("Process parameters: no process controller property on this study.")
        return None

    rows = []
    # The moulding process itself ("Thermoplastics Injection Molding", ...) --
    # the one piece of setup context that is not a numbered TCode.
    try:
        proc = str(study_doc.MoldingProcess() or "").strip()
        if proc:
            # The API returns the raw enum ("THERMOPLASTICS_INJECTION_MOLDING").
            # Shouting an identifier at the reader of a customer deck is not
            # what the rest of this slide does.
            if proc.isupper() or "_" in proc:
                proc = proc.replace("_", " ").title()
            rows.append(("Molding process", proc))
    except Exception:
        pass

    phases = _sequence_phases(sequence)
    log("Process parameters: sequence '{0}' -> phases {1}".format(
        sequence or "(unknown)", sorted(phases)))

    for label, tcode, kind, value_tcode, value_label, requires in _PROCESS_FIELDS:
        # The controller answers for every field whether or not this sequence
        # uses it. Skip the ones Moldflow's wizard would not have shown.
        if requires and requires not in phases:
            continue
        vals = _process_field(prop, tcode)
        if not vals:
            continue
        try:
            raw = vals[0]
            if kind == "mode":
                text = _PROCESS_MODES.get(tcode, {}).get(int(round(float(raw))))
                if not text:
                    continue
                # A mode of anything but Automatic means the engineer gave the
                # solver a number; show the number, which is what the reference
                # report's process table carries.
                if value_tcode is not None and text != "Automatic":
                    companion = _process_field(prop, value_tcode)
                    if companion:
                        try:
                            v = float(companion[0])
                            if v > 0:
                                text = "{0:g} s".format(v)
                                if value_label:
                                    label = value_label
                        except (TypeError, ValueError):
                            pass
            elif kind == "temp":
                # FieldValues returns the ACTIVE unit system, i.e. degC here --
                # the same behaviour the material temperature ranges rely on.
                text = "{0:g} degC".format(float(raw))
            else:
                text = "{0:g} s".format(float(raw))
        except Exception:
            continue
        # Logged raw so the slide's figures can be checked against Synergy's
        # own Process Settings dialog, which is the only way to confirm a
        # companion TCode is the field it is believed to be.
        log("  process field {0} ({1}) raw={2} -> {3}".format(
            tcode, label, vals[:2], text))
        rows.append((label, text))

        # The pack/hold profile hangs off its control mode, so it is read here
        # rather than as a field of its own -- and lands directly beneath it,
        # which is where a reader looks for it.
        if tcode == 10704:
            rows.extend(_pack_profile_rows(prop, raw, log))

    if not rows:
        log("Process parameters: process controller exposed no readable "
            "fields.")
        return None
    log("Process parameters: read {0} field(s) from the process "
        "controller.".format(len(rows)))
    return {"configured": rows}


# What the solver actually did, to sit beside what it was asked to do.
# (display label, [candidate datasets, best first], statistic).
#
# Candidates, not one name, because the XY-plot datasets are not dependable.
# "Pressure at injection location" is a pressure-vs-time trace, and on a
# Fill+Pack run of study 40 it came back as a SINGLE sample of 0.0 -- while
# "Pressure" for the same study peaked at 32.7 MPa. Printing "0 MPa" as the
# peak injection pressure on a customer deck is worse than printing nothing,
# so a degenerate value falls through to the next candidate.
_ACHIEVED_FIELDS = [
    ("Fill time", ["Fill time"], "max"),
    ("Peak injection pressure",
     ["Pressure at injection location", "Pressure",
      "Pressure at end of fill"], "max"),
    ("V/P switch-over pressure", ["Pressure at V/P switchover"], "max"),
    ("Peak clamp force", ["Clamp force"], "max"),
    ("Flow front temperature", ["Temperature at flow front"], "max"),
    ("Time to ejection temperature", ["Time to reach ejection temperature"],
     "max"),
]


def process_achieved_rows(summary_data, log=None):
    """The "as achieved" column of the process slide: [(label, value)].

    Read from the statistics this deck already computes, so it costs nothing
    and cannot disagree with the numbers on the result slides. Returns [] when
    there are no statistics, which drops the column rather than the slide.

    A peak of exactly zero is treated as "this dataset did not report", not as
    a measurement: every quantity here is a pressure, a force, a temperature or
    a duration, and none of them genuinely peaks at zero in a study that ran.
    """
    metrics = {}
    for m in (summary_data or {}).get("metrics") or []:
        name = (m.get("name") or "").strip().lower()
        if name:
            metrics[name] = m

    rows = []
    for label, datasets, stat in _ACHIEVED_FIELDS:
        for dataset in datasets:
            m = metrics.get(dataset.lower())
            if not m:
                continue
            value = m.get(stat)
            try:
                if value is None or float(value) == 0.0:
                    if log and value is not None:
                        log("Process parameters: '{0}' reported {1} of 0 for "
                            "{2}; trying the next dataset.".format(
                                dataset, stat, label))
                    continue
            except (TypeError, ValueError):
                continue
            rows.append((label, format_stat(value, m.get("unit") or "")))
            break
    return rows


def collect_analysis_summary(sy, plot_mgr, log, plots=None, grade_limits=None):
    """Statistics + findings for every dataset this study actually produced.

    Returns {"metrics": [...], "findings": [...], "counts": {...}}.

    Dynamic by construction: the dataset list comes from enumerating the
    study's own plots, so a Fill run and a Cool+Flow+Warp run each summarise
    what they have rather than being measured against a fixed checklist."""
    by_name, by_id = load_results_catalogue(log)
    limits = material_limits(sy, log)
    # The COM route is preferred when it answers, but on this build it returns
    # nothing without raising -- so anything the metadata extractor read from
    # the .sdy fills the gaps. Without this the rules quietly fall back to the
    # reference document's worked-example figures and compare a real result
    # against another grade's limit.
    for _key, _val in (grade_limits or {}).items():
        if _key not in limits and _val:
            limits[_key] = float(_val)
    if limits:
        log("Threshold limits in use: {0}".format(limits))
    else:
        log("No grade limits available; rules use their documented defaults.")

    # --- enumerate ------------------------------------------------------
    found = []
    if plots:
        found = list(plots)
    else:
        try:
            pl = plot_mgr.GetFirstPlot()
            misses = 0
            while pl is not None and misses < 3:
                try:
                    found.append((pl, str(pl.GetName() or "")))
                except Exception:
                    pass
                try:
                    pl = plot_mgr.GetNextPlot(pl)
                    misses = 0
                except Exception:
                    misses += 1
        except Exception as e:
            log("Summary: could not enumerate plots ({0}).".format(e))
    log("Analysis summary: {0} dataset(s) to examine.".format(len(found)))

    metrics, findings = [], []
    seen = set()
    for pl, raw_name in found:
        name = (raw_name or "").split(":")[0].strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        # Viewer geometry and index datasets are plots, not measurements.
        if key in NON_ENGINEERING_DATASETS:
            log("  '{0}': viewer/index dataset, not summarised.".format(name))
            continue

        did = 0
        for getter in ("GetDataID", "get_data_id"):
            try:
                did = int(call_member(pl, getter) or 0)
                break
            except Exception:
                continue
        if did <= 0:
            did = by_name.get(key, (0, ""))[0]
        if did <= 0:
            continue

        # --- raw values ---
        vals, kind = fetch_dataset_values(sy, plot_mgr, did)
        stat = summarise_values(vals)
        if not stat:
            log("  '{0}' (ds {1}, {2}): no readable values.".format(
                name, did, kind))
            continue

        unit = by_id.get(did, ("", ""))[1] or by_name.get(key, (0, ""))[1]
        raw = stat.pop("values", [])
        disp, disp_unit = convert_stat_unit(stat, unit)
        metrics.append({"name": name, "dsid": did, "unit": disp_unit,
                        "n": disp["n"], "min": disp["min"], "max": disp["max"],
                        "mean": disp["mean"], "std": disp["std"]})
        log("  '{0}' (ds {1}): n={2} min={3} max={4} mean={5} sd={6}".format(
            name, did, disp["n"],
            format_stat(disp["min"], disp_unit), format_stat(disp["max"], disp_unit),
            format_stat(disp["mean"], disp_unit), format_stat(disp["std"], disp_unit)))

        rule = SUMMARY_RULES.get(key)
        if rule:
            ctx = dict(disp)
            ctx["_raw"] = raw
            ctx["_limit"] = limits.get(key)
            try:
                verdict = rule(ctx, disp_unit)
            except Exception as e:
                log("  Rule for '{0}' failed: {1}".format(name, e))
                verdict = None
            if verdict:
                level, headline, detail = verdict
                findings.append({"level": level, "headline": headline,
                                 "detail": detail, "result": name,
                                 "_rule": id(rule),
                                 # Peak this finding was judged on, kept only
                                 # to break a same-level tie below.
                                 "_peak": abs(disp.get("max") or 0.0)})

    order = {n: i for i, n in enumerate(SUMMARY_PREFERRED)}
    metrics.sort(key=lambda m: (order.get(m["name"].lower(), 999), m["name"]))
    rank = {"red": 0, "amber": 1, "green": 2}

    # One finding per RULE, not per dataset. A study routinely carries several
    # datasets that answer the same engineering question -- "Shear rate" and
    # "Shear rate, maximum", or "Volumetric shrinkage" and "Average volumetric
    # shrinkage" -- and left alone they produced the same headline twice, or
    # worse, a green from one and an amber from its sibling side by side. The
    # most severe reading wins, because a limit exceeded anywhere in the part
    # is exceeded.
    #
    # The tie-break matters as much as the level. Both shear-rate datasets come
    # back 'red', so level alone left the winner decided by iteration order --
    # and that handed the deck 'Shear rate' (241,073 1/s) while the Assistant,
    # reading the study itself, quoted 'Shear rate, maximum' (274,404 1/s).
    # Study 37 printed both, as two different values for one metric on slides
    # 30 and 32, which reads as the report contradicting itself. At equal level
    # the LARGER peak wins, for the same reason the severest level does: a
    # limit exceeded anywhere in the part is exceeded. (This assumes bigger is
    # worse, which holds for every threshold rule here; a future rule where low
    # values are the problem would need its own tie-break.)
    worst = {}
    for f in findings:
        key = f.pop("_rule")
        cur = worst.get(key)
        if cur is None:
            worst[key] = f
            continue
        better_level = rank.get(f["level"], 3) < rank.get(cur["level"], 3)
        same_level = rank.get(f["level"], 3) == rank.get(cur["level"], 3)
        if better_level or (same_level and f["_peak"] > cur["_peak"]):
            worst[key] = f
    findings = sorted(worst.values(), key=lambda f: rank.get(f["level"], 3))
    for f in findings:
        f.pop("_peak", None)
    counts = {lv: sum(1 for f in findings if f["level"] == lv)
              for lv in ("red", "amber", "green")}
    log("Analysis summary: {0} metric(s), {1} finding(s) "
        "({2} red / {3} amber / {4} green).".format(
            len(metrics), len(findings), counts["red"], counts["amber"],
            counts["green"]))
    return {"metrics": metrics, "findings": findings, "counts": counts}


def enable_plot_minmax(pl, log, label=""):
    """Turn the plot's OWN min/max annotation on before it is captured.

    ImageExportOptions.ShowMinMax governs whether the exporter draws the
    annotation; Plot.SetMinMax() governs whether the PLOT has one. The capture
    path only ever set the first, so the still inherited whatever annotation
    state the plot happened to carry. Setting it explicitly makes the state the
    export renders a known one rather than a leftover.

    Documented API surface here is exactly one boolean (Plot.SetMinMax(aFlag) /
    GetMinMax(); confirmed against the extracted synapi reference): there is NO
    separate control for the marker, the leader line or the label placement, so
    this is the whole of what can be asked for. Best-effort and never fatal."""
    if pl is None:
        return False
    for name in ("SetMinMax", "set_min_max"):
        try:
            call_member(pl, name, True)
            return True
        except Exception:
            continue
    log("  Plot.SetMinMax() not available for '{0}'; the still keeps the "
        "plot's existing min/max annotation state.".format(label or "result"))
    return False


def mp4_frame_size(path):
    """(width, height) of an .mp4's video track, or None.

    Parses the track header ('tkhd') box directly. This matters because
    ANIMATION_SIZE is a REQUEST, not a guarantee: SaveAnimation4's SizeX/SizeY
    are not honoured on every build, and the SaveAnimation3 fallback takes no
    size at all -- both then export the live viewport at whatever shape it
    happens to be. Measured on this install, a run asking for 960x720 (4:3)
    produced 1578x462 files (3.4:1). Sizing the PowerPoint shape from the
    REQUESTED size, or from the 4:3 screenshot beside it, is what made the
    embedded videos look small: a 3.4:1 picture was being fitted into a 4:3
    box, so almost all of the box was empty.

    Pure stdlib on purpose -- no ffprobe/ffmpeg dependency for what is one
    16.16 fixed-point pair in the container header."""
    import struct

    def walk(fh, end, depth=0):
        while fh.tell() < end and depth < 8:
            start = fh.tell()
            head = fh.read(8)
            if len(head) < 8:
                return None
            size, kind = struct.unpack(">I4s", head)
            if size == 1:                      # 64-bit extended size
                ext = fh.read(8)
                if len(ext) < 8:
                    return None
                size = struct.unpack(">Q", ext)[0]
            if size < 8 or start + size > end:
                return None
            if kind in (b"moov", b"trak", b"mdia"):
                found = walk(fh, start + size, depth + 1)
                if found:
                    return found
            elif kind == b"tkhd":
                body = fh.read(size - (fh.tell() - start))
                if len(body) >= 8:
                    w, h = struct.unpack(">II", body[-8:])
                    w, h = w >> 16, h >> 16     # 16.16 fixed point
                    if w > 0 and h > 0:
                        return w, h             # first sized track = the video
            fh.seek(start + size)
        return None

    try:
        with open(str(path), "rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(0)
            return walk(fh, end)
    except Exception:
        return None


def fold_panel(log, reason):
    """Minimise the embedded panel for a long native Synergy operation.

    Returns True when the request was published, which is the token the
    matching unfold_panel() call expects -- a stage that never folded must not
    unfold, or it would pop the panel open over a user who had deliberately
    collapsed it themselves.

    Never raises: the panel is an aid, and no stage of the workflow should
    fail because a window did not move."""
    try:
        import ui_bridge
        return bool(ui_bridge.request_panel_state("minimized", reason))
    except Exception as e:
        log("Could not minimise the panel ({0}): {1}".format(reason, e))
        return False


def unfold_panel(folded, log, reason):
    """Restore the panel after the operation fold_panel() covered."""
    if not folded:
        return
    try:
        import ui_bridge
        ui_bridge.request_panel_state("normal", reason)
    except Exception as e:
        log("Could not restore the panel ({0}): {1}".format(reason, e))


def show_imported_model(sy, log):
    """Put the imported model on screen once CAD Diagnostics has finished.

    Diagnostics leaves the viewport wherever Compute() and the entity
    selections left it (and, on a model with issues, with the offending
    entities still highlighted). This clears that selection and returns the
    viewport to the standard isometric fit, so the moment the checks end the
    user is looking at their part rather than at a diagnostic state.

    Best-effort throughout: the workflow's correctness does not depend on the
    camera, so every step is individually guarded and a build without one of
    these viewer members simply keeps whatever view it already had."""
    try:
        viewer = sy.Viewer()
    except Exception as e:
        log("Could not obtain the Viewer to display the model: {0}".format(e))
        return False
    if viewer is None:
        return False

    # Drop the diagnostic highlight first -- otherwise the "imported model"
    # the user is shown is really the diagnostics' selection set.
    for clear in ("ClearSelection", "clear_selection"):
        try:
            call_member(viewer, clear)
            break
        except Exception:
            continue

    shown = False
    try:
        viewer.GoToStandardView("Isometric")
        shown = True
    except Exception as e:
        log("GoToStandardView('Isometric') unavailable: {0}".format(e))
    try:
        viewer.Fit()
        shown = True
    except Exception as e:
        log("Viewer.Fit() unavailable: {0}".format(e))

    if shown:
        log("CAD Diagnostics complete -- imported model displayed in the viewport.")
    return shown


def build_issue_detail_text(report):
    """Human-readable breakdown of exactly which geometry failed which check,
    for the Model Error Recovery 'Show Details' panel. Falls back to a plain
    explanation when Compute() itself never ran (report holds no real IDs)."""
    if not isinstance(report, dict):
        return ""

    if not report.get("_compute_ok", True):
        return ("CADDiagnostic.Compute() could not resolve any CAD bodies to check on "
                "this model, so no geometry could actually be diagnosed. "
                "See the automation log for the entity-selection attempts that failed.")

    CHECKS = [
        ("edge_edge_intersections", "Edge-edge intersections", "edge"),
        ("face_face_intersections", "Face-face intersections", "face"),
        ("edge_self_intersections", "Edge self-intersections", "edge"),
        ("face_self_intersections", "Face self-intersections", "face"),
        ("non_manifold_bodies", "Non-manifold bodies", "body"),
        ("non_manifold_edges", "Non-manifold edges", "edge"),
        ("toxic_bodies", "Toxic bodies", "body"),
        ("sliver_faces", "Sliver faces", "face"),
    ]

    def fmt_coord(coords, idx):
        if coords and idx < len(coords) and len(coords[idx]) >= 3:
            x, y, z = coords[idx][0], coords[idx][1], coords[idx][2]
            return " at ({0:.2f}, {1:.2f}, {2:.2f})".format(x, y, z)
        return ""

    lines = []
    max_shown = 20
    for key, label, unit in CHECKS:
        item = report.get(key)
        if not isinstance(item, dict):
            continue
        count = int(item.get("count", 0))
        if count <= 0:
            continue

        lines.append("{0} -- {1} issue(s):".format(label, count))
        coords = item.get("coordinates") or []

        if "ids_1" in item and "ids_2" in item:
            pairs = list(zip(item.get("ids_1") or [], item.get("ids_2") or []))
            for i, (a, b) in enumerate(pairs[:max_shown]):
                lines.append("  - {0} {1} <-> {0} {2}{3}".format(
                    unit.capitalize(), a, b, fmt_coord(coords, i)))
        elif "ids" in item:
            ids = item.get("ids") or []
            for i, eid in enumerate(ids[:max_shown]):
                lines.append("  - {0} {1}{2}".format(unit.capitalize(), eid, fmt_coord(coords, i)))

        if count > max_shown:
            lines.append("  ... and {0} more".format(count - max_shown))
        lines.append("")

    return "\n".join(lines).strip()


def cad_body_string(study_doc):
    """The study's CAD bodies as Synergy reports them (e.g. ' BD1'), or ''.

    StudyDoc.GetAllCADBodies(aVisibleOnly) is the authoritative answer to "does
    this study contain any CAD geometry at all". Used to verify that a study
    handed back by the Fusion round trip is not an empty shell."""
    if study_doc is None:
        return ""
    for attr in ("get_all_cad_bodies", "GetAllCadBodies"):
        for visible_only in (True, False):
            try:
                v = call_member(study_doc, attr, visible_only)
                if v is not None and str(v).strip():
                    return str(v)
            except Exception:
                continue
    return ""


def _entlist_size_safe(entlist):
    """Best-effort Size/size read that works whether `entlist` is a raw
    PyIDispatch (no Python attribute access, needs GetIDsOfNames/Invoke) or a
    Syn/moldflow wrapper object (normal attribute access via call_member)."""
    if entlist is None:
        return 0

    # Only a POSITIVE Size is trusted. On this Synergy build Size reports 0
    # even for a populated list (proved by ConvertToString echoing ' BD1 '
    # back on the same object), and `0 is not None` used to short-circuit
    # straight out of here -- making the ConvertToString fallback below
    # unreachable and vetoing a perfectly good body list.
    try:
        val = call_member(entlist, "size")
        if val is not None and int(val) > 0:
            return int(val)
    except Exception:
        pass

    try:
        import pythoncom
        dispid = entlist.GetIDsOfNames(0, "Size")
        flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
        val = int(entlist.Invoke(dispid, 0, flags, True))
        if val > 0:
            return val
    except Exception:
        pass

    # Size is unreliable on these lists. Fall back to the ConvertToString round
    # trip: a non-empty string means the list really does hold entities, so the
    # emptiness gate in run_fusion_repair_roundtrip must not veto it on a bad
    # Size read alone.
    for getter in ("convert_to_string", "ConvertToString"):
        try:
            res = call_member(entlist, getter)
            if res is not None and str(res).strip():
                return len(str(res).split())
        except Exception:
            pass
    try:
        import pythoncom
        dispid = entlist.GetIDsOfNames(0, "ConvertToString")
        flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
        res = entlist.Invoke(dispid, 0, flags, True)
        if res is not None and str(res).strip():
            return len(str(res).split())
    except Exception:
        pass

    return 0


def run_fusion_repair_roundtrip(sy, modeler, bodies, log):
    """Send `bodies` to Fusion for repair via Synergy's built-in CAD round trip
    and wait for the user to finish there.

    This is the scripted equivalent of Synergy's Geometry tab > Modify panel >
    Autodesk Fusion 360 button: Modeler.ModifiedWithInventorFusion() exports
    the body and launches Fusion with it open; the user repairs the geometry
    there (Inspect > Validate, etc.) and clicks "Return to Moldflow", which
    creates a new study in this project containing the repaired geometry.

    Returns the new StudyDoc object on success, or None if the job could not
    be started, the user aborted it in Fusion, or Synergy became unavailable
    while waiting.
    """
    import time

    if modeler is None:
        log("Fusion round trip: Modeler is not available.")
        return None

    body_count = _entlist_size_safe(bodies)
    log("Fusion round trip: CAD body entity list contains {0} item(s).".format(body_count))
    if body_count <= 0:
        log("Fusion round trip: refusing to start -- the CAD body selection is empty. "
            "Sending an empty selection to ModifiedWithInventorFusion does not fail "
            "cleanly (it can destabilize the Synergy connection), so this is a hard "
            "stop instead of a hung retry. The underlying cause is the same CAD body "
            "entity-selection issue already logged above by build_cad_body_entlist "
            "(SelectFromString/StudyDoc.Selection returning 0 bodies) -- that has to "
            "be fixed before Fusion can be launched from here.")
        return None

    def _list_study_names(project):
        """Walk the project's studies via GetFirstStudyName/GetNextStudyName."""
        names = []
        if project is None:
            return names
        try:
            name = call_member(project, "get_first_study_name")
            while name:
                names.append(str(name))
                if len(names) > 500:
                    break
                name = call_member(project, "get_next_study_name", str(name))
        except Exception as e:
            log("Fusion round trip: could not enumerate study names: {0}".format(e))
        return names

    # Snapshot the project's studies BEFORE Fusion runs. Identifying the
    # repaired study afterwards by set-difference is deterministic; matching on
    # the name Synergy reports is not. GetNewEditedCadStudyName returns an
    # internal handle like 'part_study~31' while the project's own item names
    # are 'Part_study' / 'Part_study_0' -- different case, different suffix
    # convention. The old name-ranking fallback compared those case-sensitively,
    # produced zero candidates, and aborted a repair that had actually
    # succeeded (15:49 run, 2026-07-28).
    try:
        _pre_project = call_member(sy, "project")
    except Exception as e:
        _pre_project = None
        log("Fusion round trip: could not get Project for the pre-trip snapshot: {0}".format(e))
    studies_before = _list_study_names(_pre_project)
    log("Fusion round trip: studies before the trip: {0!r}".format(studies_before))

    try:
        job_id = call_member(modeler, "modified_with_inventor_fusion", bodies)
    except Exception as e:
        log("Fusion round trip: ModifiedWithInventorFusion() failed: {0}".format(e))
        return None

    try:
        job_id = int(job_id)
    except Exception:
        job_id = None

    if job_id is None or job_id <= 0:
        log("Fusion round trip: ModifiedWithInventorFusion() returned an invalid job id "
            "({0!r}); Fusion was not launched.".format(job_id))
        return None

    log("Fusion round trip started (job id {0}). Waiting for the user to finish "
        "repairing the model in Fusion and click 'Return to Moldflow'...".format(job_id))
    try:
        import ui_bridge
        ui_bridge.update_state("params", {"cad_diagnostics": "Waiting on Fusion for repair..."})
    except Exception:
        pass

    poll_interval = 2.0
    relog_every_seconds = 3600.0
    waited = 0.0
    # How long Synergy may stay unreachable before we conclude it really is
    # gone rather than just busy with the round trip. Generous on purpose:
    # the user is editing in Fusion during this window.
    unreachable_for = 0.0
    unreachable_give_up_after = 1800.0
    while True:
        time.sleep(poll_interval)
        waited += poll_interval

        # RPC failures here are EXPECTED and must not kill the process.
        #
        # While a Fusion round trip is running, Synergy stops servicing calls
        # from an external COM client: every poll comes back
        # RPC_S_SERVER_UNAVAILABLE. _exit_if_com_dead treats that as a dead
        # session and hard-exits, which is correct everywhere else but wrong
        # here -- it killed the automation at 14:11 even though Fusion had
        # already completed the repair and returned at 14:07. Autodesk's own
        # example polls these same methods, but it runs inside Synergy's macro
        # host where the connection never drops. Tolerate the outage and keep
        # polling; Synergy answers again once the handoff settles.
        com_error = None
        aborted = done = False

        try:
            aborted = bool(call_member(modeler, "is_inventor_fusion_cad_edit_aborted", job_id))
        except Exception as e:
            com_error = e
        if aborted:
            log("Fusion round trip: the CAD edit in Fusion was aborted by the user.")
            return None

        if com_error is None:
            try:
                done = bool(call_member(modeler, "is_inventor_fusion_cad_edit_done", job_id))
            except Exception as e:
                com_error = e

        if com_error is not None:
            unreachable_for += poll_interval
            if unreachable_for >= unreachable_give_up_after:
                log("Fusion round trip: Synergy has been unreachable for {0:.0f}s "
                    "({1}). Giving up on the round trip; the repaired study, if "
                    "Fusion produced one, can still be opened manually from the "
                    "project.".format(unreachable_for, com_error))
                return None
            if unreachable_for % 30 < poll_interval:
                log("Fusion round trip: Synergy not answering yet ({0:.0f}s) -- "
                    "expected while Fusion holds the round trip; still waiting."
                    .format(unreachable_for))
            continue

        if unreachable_for > 0:
            log("Fusion round trip: Synergy is answering again after {0:.0f}s."
                .format(unreachable_for))
            unreachable_for = 0.0

        if done:
            break

        if waited >= relog_every_seconds:
            log("Fusion round trip: still waiting on the user in Fusion "
                "(job id {0})...".format(job_id))
            waited = 0.0

    def _retry_com(label, fn, attempts=30, delay=2.0):
        """Retry a COM call while Synergy finishes settling after the round
        trip. The connection is often still flaky for a few seconds after
        IsInventorFusionCadEditDone first reports True, and losing the new
        study name here would waste the whole repair the user just did."""
        last = None
        for i in range(attempts):
            try:
                return fn(), None
            except Exception as e:
                last = e
                if i == 0 or (i + 1) % 10 == 0:
                    log("Fusion round trip: {0} not ready yet (attempt {1}/{2}): "
                        "{3}".format(label, i + 1, attempts, e))
                time.sleep(delay)
        return None, last

    new_study_name, err = _retry_com(
        "GetNewEditedCadStudyName",
        lambda: call_member(modeler, "get_new_edited_cad_study_name", job_id))
    if err is not None:
        log("Fusion round trip: GetNewEditedCadStudyName() failed: {0}".format(err))
        return None

    if not new_study_name:
        log("Fusion round trip: finished, but no new study name was returned.")
        return None

    log("Fusion round trip complete. Repaired study created: {0!r}".format(new_study_name))

    def _try_open(project, name):
        try:
            return bool(call_member(project, "open_item_by_name", str(name), "Study"))
        except Exception as e:
            log("Fusion round trip: OpenItemByName({0!r}) raised: {1}".format(name, e))
            return False

    project, err = _retry_com("Project", lambda: call_member(sy, "project"))
    if err is not None or project is None:
        log("Fusion round trip: could not get the Project object: {0}".format(err))
        return None

    # The study list does not update the instant GetNewEditedCadStudyName
    # reports a name, so give the project time to register what Fusion handed
    # back before deciding anything. OpenItemByName returning False is not an
    # exception, so the generic _retry_com above never retried it -- that is
    # what stopped the 14:15 run dead after a SUCCESSFUL repair.
    def _resolve_candidates():
        """Every plausible name for the repaired study, best first.

        Order matters: the set-difference against the pre-trip snapshot is the
        only DETERMINISTIC identifier here -- it does not care what naming or
        casing convention Synergy uses. Name matching is a fallback, and it is
        now case-insensitive, which is precisely what the 15:49 run needed:
        'part_study~31' vs project names ['Part_study', 'Part_study_0'] scored
        zero candidates under the old case-sensitive comparison and threw away
        a completed repair."""
        names = _list_study_names(project)
        appeared = [n for n in names if n not in studies_before]

        def _norm(s):
            return str(s).split("~")[0].strip().lower()

        target = _norm(new_study_name)
        ranked = []

        def _add(seq, why):
            for n in seq:
                if n not in ranked:
                    ranked.append(n)
                    reasons[n] = why

        reasons = {}
        _add([n for n in names if str(n) == str(new_study_name)], "exact name")
        _add(appeared, "new since the pre-trip snapshot")
        _add([n for n in names if _norm(n) == target], "name matches ignoring case/suffix")
        _add([n for n in names if target and target in str(n).lower()],
             "name contains the reported stem")
        return names, appeared, ranked, reasons

    opened = False
    names = appeared = ranked = []
    reasons = {}
    for attempt in range(15):
        names, appeared, ranked, reasons = _resolve_candidates()
        if attempt == 0:
            log("Fusion round trip: studies after the trip: {0!r} (new: {1!r})"
                .format(names, appeared))
        for cand in ranked:
            if _try_open(project, cand):
                log("Fusion round trip: opened {0!r} -- matched by {1}.".format(
                    cand, reasons.get(cand, "?")))
                new_study_name = cand
                opened = True
                break
        if opened:
            break
        if attempt == 0 and ranked:
            log("Fusion round trip: candidates {0!r} would not open yet; retrying "
                "while the project registers the repaired study.".format(ranked))
        time.sleep(2.0)

    if not opened:
        log("Fusion round trip: could not open the repaired study for {0!r}. "
            "Studies in project: {1!r}; new since the pre-trip snapshot: {2!r}. "
            "The repair itself SUCCEEDED -- open that study manually from the "
            "project and re-run the automation.".format(
                new_study_name, names, appeared))
        return None

    log("Fusion round trip: opened repaired study {0!r}.".format(new_study_name))

    try:
        new_study_doc = call_member(sy, "study_doc")
    except Exception as e:
        log("Fusion round trip: could not fetch the new active StudyDoc: {0}".format(e))
        return None

    # VERIFY THE RETURNED STUDY ACTUALLY CONTAINS GEOMETRY.
    #
    # This guard was missing and its absence cost days. The round trip regularly
    # hands back an EMPTY study while reporting complete success:
    # IsInventorFusionCadEditDone returns True, GetNewEditedCadStudyName returns
    # a name, OpenItemByName opens it -- and the .sdy on disk is a 4,096 byte
    # stub where a real study in the same project is ~26 MB (part_study~31.sdy
    # vs part_study~30.sdy, 2026-07-28; the round-trip outputs ~3..~17, ~31 and
    # ~34 are ALL exactly 4096 bytes).
    #
    # Worse, diagnostics on such a study does not fail cleanly. It reported
    # "1 edge-edge intersection" with edge ids and coordinates -- stale modeler
    # state belonging to the PREVIOUSLY open study. That confident wrong number
    # sent the investigation chasing a non-existent geometry defect. An error is
    # cheaper than a plausible lie, so refuse to continue.
    #
    # The failure is INTERMITTENT (part_study~32.sdy is a healthy 26.8 MB),
    # which is exactly why it stayed hidden for so long.
    bodies_str = ""
    for attempt in range(10):
        bodies_str = cad_body_string(new_study_doc)
        if bodies_str.strip():
            break
        if attempt == 0:
            log("Fusion round trip: no CAD bodies visible in {0!r} yet; waiting for "
                "geometry to load...".format(new_study_name))
        time.sleep(2.0)
        try:
            sy.Build()
        except Exception:
            pass

    # Independent check: the .sdy on disk. GetAllCADBodies goes through the same
    # modeler that was serving stale data, so it is not a fully independent
    # witness; the file size is. An empty round-trip stub is 4,096 bytes against
    # ~26 MB for a real study in this project -- unambiguous, and it does not
    # depend on any COM state being correct.
    # StudyDoc has StudyName, NOT Path -- 'Attribute Path not found' in the
    # 17:34 log. The directory comes from Project.Path.
    sdy_size = None
    try:
        stem = call_member(new_study_doc, "StudyName")
        proj_dir = call_member(project, "Path")
        if stem and proj_dir:
            # StudyName already carries the .sdy extension on this build --
            # blindly appending it produced 'part_study_0~4.sdy.sdy' (17:46 log).
            stem = str(stem)
            if not stem.lower().endswith(".sdy"):
                stem += ".sdy"
            p = Path(str(proj_dir)) / stem
            if p.is_file():
                sdy_size = p.stat().st_size
                log("Fusion round trip: study file {0} is {1:,} bytes.".format(
                    p.name, sdy_size))
            else:
                log("Fusion round trip: study file not found at {0}".format(p))
    except Exception as e:
        log("Fusion round trip: could not stat the study file: {0}".format(e))

    # WHICH CAD file does the returned study reference? The Synergy tree shows
    # 'Part (Part.stp)' for the round-trip output, i.e. it still names the
    # ORIGINAL imported file. If the returned study references the same source
    # CAD as the original rather than anything Fusion produced, that alone would
    # explain a repair appearing to have no effect.
    try:
        cad_names = call_member(new_study_doc, "GetPartCadNames")
        log("Fusion round trip: {0!r} references CAD part(s): {1!r}".format(
            new_study_name, cad_names))
    except Exception as e:
        log("Fusion round trip: GetPartCadNames unavailable: {0}".format(e))

    if sdy_size is not None and sdy_size < 32768:
        log("Fusion round trip: FAILED -- study file for {0!r} is only {1:,} bytes. "
            "That is an EMPTY STUB, not a study (a real study here is ~26 MB). The "
            "round trip reported success and delivered nothing."
            .format(new_study_name, sdy_size))
        return None

    if not bodies_str.strip():
        log("Fusion round trip: FAILED -- study {0!r} contains NO CAD BODIES. The "
            "round trip reported success but delivered no geometry. NOT continuing: "
            "a diagnostic run on this study would report numbers belonging to the "
            "previously open study, not to this one. The repair done in Fusion was "
            "not lost -- it never arrived. Workaround that is known to work: repair "
            "the part in Fusion, save it to a file, and import that file into "
            "Moldflow directly.".format(new_study_name))
        return None

    log("Fusion round trip: verified {0!r} contains CAD bodies: {1!r}".format(
        new_study_name, bodies_str))

    try:
        import ui_bridge
        # In-progress, not a result: diagnostics are about to run again on the
        # study Fusion handed back.
        ui_bridge.set_step_in_progress(
            "cad_diagnostics", "Repaired in Fusion - re-checking...")
    except Exception:
        pass

    return new_study_doc


def show_automation_prompt(report, total, detail_text, log, sy, modeler, bodies):
    """Show the single consolidated Automation Workflow dialog.

    Returns a tuple (decision, new_study_doc):
      - ("start", None)     -- begin automation on the current study
      - ("recheck", study)  -- the CAD body was repaired via the Fusion round
                               trip; caller should re-run diagnostics on `study`
      - ("stop", None)      -- user declined, or repair could not be completed
    """
    import ctypes

    LABELS = [
        ("edge_edge_intersections", "Edge-edge intersections"),
        ("face_face_intersections", "Face-face intersections"),
        ("edge_self_intersections", "Edge self-intersections"),
        ("face_self_intersections", "Face self-intersections"),
        ("non_manifold_bodies", "Non-manifold bodies"),
        ("non_manifold_edges", "Non-manifold edges"),
        ("toxic_bodies", "Toxic bodies"),
        ("sliver_faces", "Sliver faces"),
    ]

    def esc(s):
        return (str(s).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))

    rows = []
    for key, label in LABELS:
        item = report.get(key) if isinstance(report, dict) else None
        count = int(item.get("count", 0)) if isinstance(item, dict) else 0
        if count > 0:
            cell = '<td class="bad">{0} issue(s)</td>'.format(count)
        else:
            cell = '<td class="ok">No issues</td>'
        rows.append("<tr><td>{0}</td>{1}</tr>".format(esc(label), cell))

    compute_ok = report.get("_compute_ok", True) if isinstance(report, dict) else True

    if not compute_ok:
        banner = ('<div class="tot bad">CAD Diagnostics could not run on this model '
                   "&mdash; the results below are not meaningful. Check the log.</div>")
    elif total > 0:
        banner = ('<div class="tot bad">{0} potential geometry issue(s) found '
                  "&mdash; review before meshing.</div>".format(total))
    else:
        banner = '<div class="tot ok">No geometry issues detected.</div>'

    summary_html = banner + '<table class="sm">' + "".join(rows) + "</table>"

    try:
        import ui_bridge
        if ui_bridge.ui_alive():
            issue_detail_text = build_issue_detail_text(report)
            ans = ui_bridge.request_cad_diagnostics_report(
                report, detail_text=issue_detail_text or detail_text)

            if ans == "Go back to Fusion to fix model":
                log("User selected 'Go back to Fusion' -- starting the Synergy/Fusion "
                    "CAD repair round trip (Modeler.ModifiedWithInventorFusion).")
                new_study = run_fusion_repair_roundtrip(sy, modeler, bodies, log)
                if new_study is not None:
                    return ("recheck", new_study)
                log("Fusion round trip did not complete; automation stopped.")
                return ("stop", None)

            if ans == "Translate Surface":
                log("User selected 'Translate Surface' from UI card. Executing CAD surface translation repair...")
                translate_ok = False
                try:
                    if modeler is not None:
                        for fn in ("TranslateSurface", "translate_surface", "RepairGeometry", "repair_geometry"):
                            try:
                                res = call_member(modeler, fn)
                                if res is not None and bool(res):
                                    translate_ok = True
                                    log("Modeler {0}() completed successfully.".format(fn))
                                    break
                            except Exception:
                                pass
                except Exception as e_ts:
                    log("Translate Surface execution error: {0}".format(e_ts))

                if translate_ok:
                    try:
                        sy.Build()
                    except Exception:
                        pass
                    log("Translate Surface completed. Resuming automation workflow.")
                    return ("start", None)
                else:
                    log("Translate Surface operation was not supported or failed.")
                    return ("stop", None)

            return ("start" if ans == "Yes" else "stop", None)
    except Exception:
        pass

    res_file = SESSION_DIR / "_automation_prompt.txt"
    try:
        res_file.unlink()
    except Exception:
        pass

    hta = (_AUTOMATION_PROMPT_HTA
           .replace("__SUMMARY__", summary_html)
           .replace("__DETAILS__", esc(detail_text))
           .replace("__RES__", str(res_file).replace("\\", "\\\\")))

    hta_path = SESSION_DIR / "_automation_prompt.hta"
    shown = False
    try:
        hta_path.write_text(hta, encoding="utf-8")
        import subprocess
        subprocess.run(["mshta", str(hta_path)], timeout=1800)
        shown = True
    except Exception as e:
        try:
            log("Could not show the Automation Workflow dialog: {0}".format(e))
        except Exception:
            pass
    finally:
        try:
            hta_path.unlink()
        except Exception:
            pass

    if not shown:
        try:
            import ui_bridge
            ans = ui_bridge.prompt_user(
                "CAD Diagnostics Complete",
                "CAD Diagnostics completed.\n\nDo you want to start the analysis automation workflow?",
                options=["Yes", "No"]
            )
            return ("start" if ans == "Yes" else "stop", None)
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                None, str(detail_text), "CAD Diagnostics Complete", 0x00050000)
        except Exception:
            pass
        choice = ctypes.windll.user32.MessageBoxW(
            None,
            "CAD Diagnostics complete.\n\n"
            "Do you want to start the analysis automation workflow?",
            "Automation Workflow",
            0x04 | 0x20 | 0x00040000 | 0x00010000)
        return ("start" if choice == 6 else "stop", None)  # IDYES

    answer = "NO"
    try:
        if res_file.exists():
            answer = res_file.read_text(encoding="utf-8").strip().upper()
    except Exception:
        pass
    finally:
        try:
            res_file.unlink()
        except Exception:
            pass
    return ("start" if answer.startswith("YES") else "stop", None)

def _publish_cad_status(message):
    """Advance the panel's CAD Diagnostics row while this process works.

    The stage is already marked as starting by moldflow_startup/the observer;
    these messages carry that through the attach and the checks so the row is
    never back to "Pending" during the wait for the diagnostics card. Failures
    are swallowed: the panel may not be running at all (manual mode), and no
    status update is worth stopping diagnostics for."""
    try:
        import ui_bridge
        ui_bridge.set_step_in_progress("cad_diagnostics", message)
    except Exception:
        pass


def main():
    # From the very start: if Moldflow closes at ANY point (even while one of
    # our dialogs is blocking), this process and its dialogs die with it.
    _start_synergy_watchdog()

    # Before the COM attach, which is itself one of the slower parts of the
    # gap the user sees between the import finishing and this window opening.
    _publish_cad_status("Starting CAD Diagnostics...")

    log_lines = []

    def log(message):
        text = str(message)
        log_lines.append(text)
        try:
            sy.log(text)
        except Exception:
            pass
        try:
            print(text, flush=True)
        except Exception:
            pass

    def show_dialog(title, message):
        """Show prompt via ui_bridge rather than raw Win32 dialogs."""
        try:
            import ui_bridge
            ui_bridge.prompt_user(title, message, options=["OK"])
            return
        except Exception:
            pass

        try:
            import ctypes
            # MB_TOPMOST | MB_SETFOREGROUND: fallback if ui_bridge unavailable
            ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x00050000)
            return
        except Exception:
            pass

        try:
            sy.log(str(title))
            sy.log(str(message))
        except Exception:
            pass

    # Try to connect to Synergy
    sy = None
    try:
        from synergy_connect import get_synergy
        sy = get_synergy(allow_launch=False)
        log("Connected to Synergy via synergy_connect.")
    except Exception as e:
        log_lines.append(f"synergy_connect failed: {e}")

    if sy is None:
        try:
            # pyrefly: ignore [missing-import]
            from moldflow import Synergy
            sy = Synergy()
            log("Connected to Synergy via native moldflow import.")
        except Exception as e:
            log_lines.append(f"native moldflow import failed: {e}")
            show_dialog("CAD Diagnostics Error", "Could not connect to Moldflow Synergy.\n" + "\n".join(log_lines))
            sys.exit(1)

    def array_to_list(arr):
        for method in ("to_list", "ToVBSArray", "to_vb_array"):
            try:
                func = getattr(arr, method)
                if callable(func):
                    res = func()
                    if res is not None:
                        return list(res)
            except Exception:
                pass
        
        # Fallback to index access
        out = []
        try:
            size = getattr(arr, "size", None)
            if callable(size):
                size = size()
            if size is None:
                size = getattr(arr, "Size", None)
                if callable(size):
                    size = size()
            
            if size is not None:
                for i in range(size):
                    val = None
                    for idx_method in ("Val", "val"):
                        try:
                            getter = getattr(arr, idx_method)
                            if callable(getter):
                                val = getter(i)
                                break
                        except Exception:
                            pass
                    if val is None:
                        try:
                            val = arr[i]
                        except Exception:
                            pass
                    if val is not None:
                        out.append(val)
        except Exception:
            pass
        return out

    def points_from_flat_xyz(values):
        return [
            [values[i], values[i + 1], values[i + 2]]
            for i in range(0, len(values), 3)
            if i + 2 < len(values)
        ]

    def get_active_study_doc():
        for attr in ("study_doc", "active_study_doc"):
            try:
                doc = call_member(sy, attr)
                if doc is not None:
                    return doc
            except Exception:
                pass

        try:
            synergy_obj = call_member(sy, "synergy")
            if synergy_obj is not None:
                for attr in ("StudyDoc", "ActiveStudyDoc"):
                    doc = call_member(synergy_obj, attr)
                    if doc is not None:
                        return doc
        except Exception:
            pass

        return None

    def get_modeler():
        for attr in ("modeler", "Modeler"):
            try:
                obj = call_member(sy, attr)
                if obj is not None:
                    return obj
            except Exception:
                pass

        try:
            synergy_obj = call_member(sy, "synergy")
            if synergy_obj is not None:
                modeler = call_member(synergy_obj, "Modeler")
                if modeler is not None:
                    return modeler
        except Exception:
            pass

        return None

    def _get_raw_dispatch(obj):
        """Unwrap a Syn wrapper (or any object) to its raw IDispatch."""
        raw = getattr(obj, "_raw", lambda: obj)()
        if hasattr(raw, "_oleobj_"):
            return raw._oleobj_
        return raw

    def _raw_invoke(disp, method_name, *args):
        """Call a method on a raw IDispatch by name, returning the raw result."""
        import pythoncom
        dispid = disp.GetIDsOfNames(0, method_name)
        flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
        return disp.Invoke(dispid, 0, flags, True, *args)

    def _raw_size(disp):
        """Get Size/size from a raw IDispatch entity list."""
        for name in ("Size", "size"):
            try:
                return int(_raw_invoke(disp, name))
            except Exception:
                pass
        return 0

    def _raw_select_from_string(disp, s):
        """Call SelectFromString/select_from_string on a raw IDispatch."""
        for name in ("SelectFromString", "select_from_string"):
            try:
                _raw_invoke(disp, name, s)
                return _raw_size(disp)
            except Exception:
                pass
        return 0

    def _raw_convert_to_string(disp):
        """EntList.ConvertToString() -- the round trip that tells us whether a
        SelectFromString actually populated the list. Size has been reading 0
        for every attempt; if ConvertToString echoes the IDs back then the
        selection worked and Size is the unreliable part, which is not
        something reading the code can distinguish."""
        for name in ("ConvertToString", "convert_to_string"):
            try:
                res = _raw_invoke(disp, name)
                if res is not None:
                    return str(res)
            except Exception:
                pass
        return ""

    def _raw_clear(disp):
        """Call Clear/clear on a raw IDispatch entity list."""
        for name in ("Clear", "clear"):
            try:
                _raw_invoke(disp, name)
                return
            except Exception:
                pass

    def create_modeler_entlist(modeler):
        # Strategy: try ALL sources, returning the FIRST one that produces
        # a working entity list.  Log which source succeeded.
        sources = []

        # 1. Modeler.CreateEntityList()  (preferred — same scope as the body strings)
        try:
            raw_mod = _get_raw_dispatch(modeler)
            raw_el = _raw_invoke(raw_mod, "CreateEntityList")
            if raw_el is not None:
                sources.append(("Modeler (raw)", raw_el))
        except Exception as e:
            log("Modeler.CreateEntityList (raw) failed: {0}".format(e))

        # Also try via Syn wrapper
        try:
            res = call_member(modeler, "create_entity_list")
            if res is not None:
                sources.append(("Modeler (wrapper)", res))
        except Exception:
            pass

        # 2. StudyDoc.CreateEntityList()
        try:
            raw_sd = _get_raw_dispatch(study_doc)
            raw_el = _raw_invoke(raw_sd, "CreateEntityList")
            if raw_el is not None:
                sources.append(("StudyDoc (raw)", raw_el))
        except Exception:
            pass

        try:
            res = call_member(study_doc, "create_entity_list")
            if res is not None:
                sources.append(("StudyDoc (wrapper)", res))
        except Exception:
            pass

        # 3. CADDiagnostic.CreateEntityList()
        try:
            raw_cad = _get_raw_dispatch(cad)
            raw_el = _raw_invoke(raw_cad, "CreateEntityList")
            if raw_el is not None:
                sources.append(("CADDiagnostic (raw)", raw_el))
        except Exception:
            pass

        try:
            res = call_member(cad, "create_entity_list")
            if res is not None:
                sources.append(("CADDiagnostic (wrapper)", res))
        except Exception:
            pass

        log("Entity list sources available: {0}".format([s[0] for s in sources]))

        if not sources:
            raise RuntimeError("Could not create EntList from any source.")

        return sources

    def build_cad_body_entlist(study_doc):
        body_string = None
        for attr in ("get_all_cad_bodies", "GetAllCadBodies"):
            try:
                body_string = call_member(study_doc, attr, True)
                if body_string is not None and str(body_string).strip():
                    break
            except Exception:
                pass
            try:
                body_string = call_member(study_doc, attr, False)
                if body_string is not None and str(body_string).strip():
                    break
            except Exception:
                pass

        log("Raw CAD body entity string: {0!r}".format(body_string))

        if body_string is None:
            raise RuntimeError("No CAD body string was returned from the active study.")

        tokens = str(body_string).replace(",", " ").split()
        if not tokens:
            raise RuntimeError(
                "No CAD bodies were found in the active study. "
                "This API checks imported CAD bodies, not mesh-only geometry."
            )

        modeler_body_string = " " + " ".join(tokens)
        compact_body_string = " ".join(tokens)

        log("Modeler CAD body selection string: {0!r}".format(modeler_body_string))

        modeler = get_modeler()
        if modeler is None:
            raise RuntimeError("Could not access Synergy.Modeler().")

        # Build candidate selection strings.
        #
        # The trailing-space forms come first deliberately. Autodesk's own CAD
        # body example is written SelectFromString(" BD1 ") -- leading AND
        # trailing space -- and 61 of the 78 SelectFromString examples across
        # the whole API reference end with a trailing space. GetAllCADBodies
        # here returns ' BD1' with no trailing space, and every candidate tried
        # so far has lacked one. If Synergy's parser needs that trailing
        # delimiter to commit the final token, this is the fix; if not, the
        # ConvertToString logging below will say so definitively.
        raw_bs = str(body_string)
        candidates = [
            raw_bs + " ",
            " " + raw_bs.strip() + " ",
            raw_bs.strip() + " ",
            compact_body_string,
            modeler_body_string,
            raw_bs,
        ]
        for tok in tokens:
            if tok not in candidates:
                candidates.append(tok)
            tok_num = re.sub(r"[^\d]", "", tok)
            if tok_num:
                for fmt in ("CAD Body {0}", "Body {0}", "{0}", "N{0}", "N {0}"):
                    c = fmt.format(tok_num)
                    if c not in candidates:
                        candidates.append(c)

        # Also add the raw body_string with N prefix variations
        for tok in tokens:
            for prefix in ("N", ""):
                c = prefix + tok.strip()
                if c not in candidates:
                    candidates.append(c)

        log("Candidate selection strings to try: {0!r}".format(candidates))

        # Get all entity list sources
        all_sources = create_modeler_entlist(modeler)
        errors = []

        # Try each source with each candidate string using RAW COM dispatch
        for source_name, el_obj in all_sources:
            raw_el = _get_raw_dispatch(el_obj)
            log("Trying entity list source: {0}".format(source_name))

            for candidate in candidates:
                try:
                    _raw_clear(raw_el)
                    size = _raw_select_from_string(raw_el, candidate)
                    conv = _raw_convert_to_string(raw_el)
                    log("  {0} -> SelectFromString({1!r}) size={2} convert={3!r}".format(
                        source_name, candidate, size, conv))

                    # Trust ConvertToString over Size. A populated list that
                    # reports Size 0 is still a usable list; discarding it on
                    # the Size read alone is what made every previous run look
                    # like a total selection failure.
                    if size > 0 or (conv and conv.strip()):
                        log("SUCCESS: {0} selected bodies via {1} with string {2!r} "
                            "(size={3}, convert={4!r})".format(
                                "size>0" if size > 0 else "ConvertToString",
                                source_name, candidate, size, conv))
                        return el_obj, str(body_string)
                except Exception as exc:
                    errors.append("{0} SelectFromString({1!r}): {2}".format(
                        source_name, candidate, exc))

            # Also try Select(entityType, ...) on this source
            for etype in (15, 6, 5, 1):  # 15=CAD body, 6=body, 5=solid, 1=all
                try:
                    _raw_invoke(raw_el, "Select", etype, False)
                    size = _raw_size(raw_el)
                    log("  {0} -> Select({1}, False) = {2} bodies".format(
                        source_name, etype, size))
                    if size > 0:
                        log("SUCCESS: {0} bodies selected via {1} with Select({2})".format(
                            size, source_name, etype))
                        return el_obj, str(body_string)
                except Exception as se:
                    errors.append("{0} Select({1}): {2}".format(source_name, etype, se))

        # Fallback: read StudyDoc.Selection directly. Right after an import,
        # Synergy typically leaves the just-imported CAD body selected, so this
        # needs no ID-string parsing at all -- it just reflects whatever is
        # currently selected in the model.
        try:
            raw_sd = _get_raw_dispatch(study_doc)
            sel = _raw_invoke(raw_sd, "Selection")
            if sel is not None:
                raw_sel = _get_raw_dispatch(sel)
                size = _raw_size(raw_sel)
                log("StudyDoc.Selection -> {0} entities currently selected.".format(size))
                if size > 0:
                    log("SUCCESS: using StudyDoc.Selection as the CAD body entity list.")
                    return sel, str(body_string)
        except Exception as e:
            errors.append("StudyDoc.Selection: {0}".format(e))

        # Last resort: return the first entity list source even if 0 bodies selected
        log("WARNING: Could not select any CAD bodies from any source. "
            "Returning first available entity list for Compute(). "
            "Errors: {0}".format(" | ".join(errors[:10])))
        return all_sources[0][1], str(body_string)

    def compute_cad_diagnostic(cad, bodies, body_string=" BD1"):
        import pythoncom
        raw_cad = _get_raw_dispatch(cad)

        # Strategy 1: Compute() with NO arguments — Moldflow should auto-diagnose all CAD bodies
        try:
            dispid = raw_cad.GetIDsOfNames(0, "Compute")
            flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
            res = raw_cad.Invoke(dispid, 0, flags, True)
            log("CADDiagnostic.Compute() [no args] returned: {0}".format(res))
            if res is not None and bool(res):
                return True
        except Exception as e1:
            log("Compute() no-args raised: {0}".format(e1))

        # Strategy 2: Compute(body_string) — pass the raw body string ' BD1' directly
        try:
            dispid = raw_cad.GetIDsOfNames(0, "Compute")
            flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
            res = raw_cad.Invoke(dispid, 0, flags, True, str(body_string))
            log("CADDiagnostic.Compute(body_string={0!r}) returned: {1}".format(body_string, res))
            if res is not None and bool(res):
                return True
        except Exception as e2:
            log("Compute(body_string) raised: {0}".format(e2))

        # Strategy 3: Try getting entity list from Modeler.GetCadBodyEntityList()
        modeler = get_modeler()
        if modeler is not None:
            raw_mod = _get_raw_dispatch(modeler)
            for method in ("GetCadBodyEntityList", "get_cad_body_entity_list",
                           "GetBodyEntityList", "get_body_entity_list",
                           "CadBodyEntityList", "cad_body_entity_list"):
                try:
                    dispid_m = raw_mod.GetIDsOfNames(0, method)
                    flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
                    el = raw_mod.Invoke(dispid_m, 0, flags, True)
                    if el is not None:
                        log("Modeler.{0}() returned an entity list".format(method))
                        dispid = raw_cad.GetIDsOfNames(0, "Compute")
                        res = raw_cad.Invoke(dispid, 0, flags, True, el)
                        log("CADDiagnostic.Compute(Modeler.{0}()) returned: {1}".format(method, res))
                        if res is not None and bool(res):
                            return True
                except Exception:
                    pass

        # Strategy 4: Compute with the entity list (existing bodies object)
        raw_bodies = _get_raw_dispatch(bodies)
        try:
            dispid = raw_cad.GetIDsOfNames(0, "Compute")
            flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
            res = raw_cad.Invoke(dispid, 0, flags, True, raw_bodies)
            log("CADDiagnostic.Compute(entity_list) returned: {0}".format(res))
            if res is not None and bool(res):
                return True
        except Exception as e4:
            log("Compute(entity_list) raised: {0}".format(e4))

        # Strategy 5: Syn wrapper call
        try:
            res = call_member(cad, "compute", bodies)
            log("Wrapper cad.compute() returned: {0}".format(res))
            if res is not None and bool(res):
                return True
        except Exception as e5:
            log("Wrapper cad.compute(bodies) raised: {0}".format(e5))

        log("WARNING: All Compute() strategies returned False/failed. "
            "Diagnostics may report 0 issues incorrectly.")
        return False

    def _unwrap_arr(arr):
        raw = getattr(arr, "_raw", lambda: arr)()
        if hasattr(raw, "_oleobj_"):
            return raw._oleobj_
        return raw

    def pair_check(name, func_name):
        id_1 = call_member(sy, "create_integer_array")
        id_2 = call_member(sy, "create_integer_array")
        xyz = call_member(sy, "create_double_array")
        
        ok = call_member(cad, func_name, id_1, id_2, xyz)
        if not ok:
            try:
                raw_func = getattr(cad, func_name, None) or getattr(getattr(cad, "cad_diagnostic", lambda: cad)(), func_name, None)
                if callable(raw_func):
                    ok = raw_func(_unwrap_arr(id_1), _unwrap_arr(id_2), _unwrap_arr(xyz))
            except Exception:
                pass

        list_1 = array_to_list(id_1)
        list_2 = array_to_list(id_2)
        coords = points_from_flat_xyz(array_to_list(xyz))
        count = min(len(list_1), len(list_2))

        log("  {0}: {1} issue(s)".format(name, count))

        return {
            "success": bool(ok),
            "count": count,
            "ids_1": list_1,
            "ids_2": list_2,
            "coordinates": coords,
        }

    def _invoke_method_only(obj, method_name, *args):
        """Invoke with DISPATCH_METHOD ONLY (no PROPERTYGET).

        The Syn wrapper invokes everything with METHOD|PROPERTYGET combined,
        which is correct for the no-argument members that dominate this API.
        But for a member taking exactly ONE argument, that combination is
        ambiguous: COM can resolve it as an indexed property get rather than a
        method call, and the call then quietly returns False without raising.
        That is exactly the split observed in the report -- every 2- and
        3-argument diagnostic succeeded, every 1-argument diagnostic returned
        False. Forcing DISPATCH_METHOD removes the ambiguity."""
        import pythoncom
        raw = _get_raw_dispatch(obj)
        dispid = raw.GetIDsOfNames(0, method_name)
        marshalled = [_unwrap_arr(a) if hasattr(a, "_raw") else a for a in args]
        return raw.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *marshalled)

    def id_check(name, func_names):
        if isinstance(func_names, str):
            func_names = [func_names]

        ids = call_member(sy, "create_integer_array")
        ok = False
        attempts = []

        for fn in func_names:
            # 1. Wrapper call (METHOD|PROPERTYGET) -- how it has always been done.
            try:
                ok = call_member(cad, fn, ids)
                attempts.append("{0}(wrapper)={1}".format(fn, ok))
                if ok:
                    break
            except Exception as e:
                attempts.append("{0}(wrapper) raised {1}".format(fn, e))

            # 2. DISPATCH_METHOD only -- the fix for the 1-argument ambiguity.
            for cand in (fn, snake_to_pascal(fn)):
                try:
                    res = _invoke_method_only(cad, cand, ids)
                    attempts.append("{0}(METHOD-only)={1}".format(cand, res))
                    if res:
                        ok = res
                        break
                except Exception as e:
                    attempts.append("{0}(METHOD-only) raised {1}".format(cand, e))
            if ok:
                break

            try:
                raw_func = getattr(cad, fn, None) or getattr(getattr(cad, "cad_diagnostic", lambda: cad)(), fn, None)
                if callable(raw_func):
                    ok = raw_func(_unwrap_arr(ids))
                    attempts.append("{0}(attr)={1}".format(fn, ok))
                    if ok:
                        break
            except Exception as e:
                attempts.append("{0}(attr) raised {1}".format(fn, e))

        id_list = array_to_list(ids)
        count = len(id_list)

        if not ok:
            # Never let a failed call masquerade as a clean result.
            log("  {0}: CHECK FAILED (no variant succeeded) -- result is UNKNOWN, "
                "not 'no issues'. Attempts: {1}".format(name, "; ".join(attempts)))
        else:
            log("  {0}: {1} issue(s)".format(name, count))

        return {
            "success": bool(ok),
            "count": count,
            "ids": id_list,
        }

    def id_point_check(name, func_name):
        ids = call_member(sy, "create_integer_array")
        xyz = call_member(sy, "create_double_array")
        ok = call_member(cad, func_name, ids, xyz)
        if not ok:
            try:
                raw_func = getattr(cad, func_name, None) or getattr(getattr(cad, "cad_diagnostic", lambda: cad)(), func_name, None)
                if callable(raw_func):
                    ok = raw_func(_unwrap_arr(ids), _unwrap_arr(xyz))
            except Exception:
                pass

        id_list = array_to_list(ids)
        coords = points_from_flat_xyz(array_to_list(xyz))
        count = len(id_list)

        log("  {0}: {1} issue(s)".format(name, count))

        return {
            "success": bool(ok),
            "count": count,
            "ids": id_list,
            "coordinates": coords,
        }

    try:
        log("")
        log("=== CAD Diagnostics Macro Started ===")
        log("Time: {0}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        cad = get_member(sy, "cad_diagnostic")
        if callable(cad):
            cad = cad()
            
        study_doc = get_active_study_doc()

        # Populate the User Journey card NOW rather than 240 lines later, so
        # Project Name / Location / Imported Model are filled in while CAD
        # diagnostics runs instead of showing "Pending..." for the whole stage.
        try:
            sync_journey_from_study(sy, study_doc, log)
        except Exception as _e_journey:
            log("User journey: early sync failed ({0}).".format(_e_journey))
        
        # WHICH study are we analysing? Ask the ACTIVE StudyDoc, not the project.
        #
        # The first version of this asked Project for the study name and fell
        # through to GetFirstStudyName, which returns the FIRST study in the
        # project regardless of what is open -- so it cheerfully logged
        # 'Partafterrepair_study' while measuring something else entirely, and
        # made a whole test run uninterpretable. StudyDoc.Name() is the name of
        # the study actually being measured, which is the only one worth
        # logging. GetActiveStudyName/ActiveStudyName do not exist on this build.
        _active = None
        for _attr in ("Name", "DisplayName", "Path"):
            try:
                _v = call_member(study_doc, _attr)
                if _v:
                    _active = str(_v)
                    log("Analysing study: {0!r} (StudyDoc.{1})".format(_active, _attr))
                    break
            except Exception:
                continue
        if not _active:
            log("Analysing study: <StudyDoc did not report a name; the numbers "
                "below cannot be attributed to a specific study>")

        # Rebuild/refresh Synergy model database to register imported CAD bodies
        try:
            sy.Build()
            log("Refreshed Synergy database with Build().")
        except Exception as eb:
            log("Warning: Synergy Build() failed: {0}".format(eb))

        # Moldflow's own docs for CAD body selection (both CADDiagnostic.Compute
        # and the Fusion round trip) require "at least one layer containing a
        # CAD Body visible in the graphics pane". EntList.SelectFromString has
        # been returning 0 bodies for every ID format tried, which is exactly
        # what a hidden CAD-body layer would look like -- so make sure nothing
        # is hidden before any selection attempt.
        try:
            layer_mgr = call_member(sy, "layer_manager")
            if callable(layer_mgr):
                layer_mgr = layer_mgr()
            shown = call_member(layer_mgr, "show_all_layers")
            log("LayerManager.ShowAllLayers() returned: {0}".format(shown))
        except Exception as elm:
            log("Warning: LayerManager.ShowAllLayers() failed: {0}".format(elm))

        if study_doc is None:
            raise RuntimeError("Could not access the active StudyDoc. Make sure a study is open.")

        # --- DUMP PROCESS SETTINGS PROPERTIES FOR TCODE DISCOVERY ---
        try:
            log("--- Dump Process Settings (ID 40000) ---")
            prop_ed = sy.PropertyEditor()
            prop = prop_ed.GetFirstProperty(40000)
            if prop is not None:
                log("Process Settings Property ID: {0}".format(prop.ID()))
                field = prop.GetFirstField()
                while field > 0:
                    try:
                        desc = prop.FieldDescription(field)
                        vals = prop.FieldValues(field)
                        val_str = ""
                        if vals is not None:
                            try:
                                val_str = str(vals.Val(0))
                            except Exception:
                                val_str = "Array/Object"
                        log("  Field {0}: {1} = {2}".format(field, desc, val_str))
                    except Exception as fe:
                        log("  Field {0}: error: {1}".format(field, fe))
                    field = prop.GetNextField(field)
            else:
                log("Process Settings Property (40000) not found.")
        except Exception as pe:
            log("Error inspecting PropertyEditor: {0}".format(pe))
        log("-----------------------------------------")

        # Loops back on itself when the user sends the model to Fusion for
        # repair: once Fusion hands back a new, repaired study, diagnostics
        # re-run on that study rather than blindly trusting the repair.
        #
        # HARD CAP on how many times that may happen. Fusion's Validate with
        # Repair genuinely fixes defects ("Number of surfaces repaired: 3",
        # 2026-07-28) but "Refit Bad Surfaces" and "Gap Healing" REBUILD
        # geometry, and the rebuild leaves an edge-edge intersection that
        # Moldflow then reports. So each trip fixes something invisible to
        # Moldflow and creates something visible to it -- the re-check always
        # finds an issue, always offers Fusion again, and the user rides that
        # loop indefinitely. Repeating a repair that has already been applied
        # cannot converge; after the cap, say so and let the user decide.
        MAX_FUSION_TRIPS = 2
        fusion_trips = 0
        while True:
            bodies, body_str = build_cad_body_entlist(study_doc)

            _publish_cad_status("Checking CAD geometry...")

            log("Running CADDiagnostic.compute(...)")
            compute_ok = compute_cad_diagnostic(cad, bodies, body_str)
            log("CADDiagnostic.compute returned: {0}".format(compute_ok))

            if not compute_ok:
                log("Warning: CADDiagnostic.compute returned False/None; querying individual diagnostic methods...")

            log("")
            log("CAD diagnostic results:")

            report = {
                "edge_edge_intersections": pair_check(
                    "Edge-edge intersections",
                    "get_edge_edge_intersect_diagnostic",
                ),
                "face_face_intersections": pair_check(
                    "Face-face intersections",
                    "get_face_face_intersect_diagnostic",
                ),
                "edge_self_intersections": id_point_check(
                    "Edge self-intersections",
                    "get_edge_self_intersect_diagnostic",
                ),
                "face_self_intersections": id_point_check(
                    "Face self-intersections",
                    "get_face_self_intersect_diagnostic",
                ),
                "non_manifold_bodies": id_check(
                    "Non-manifold bodies",
                    ["get_non_manifold_body_diagnostic", "GetNonManifoldBodyDiagnostic"],
                ),
                "non_manifold_edges": id_check(
                    "Non-manifold edges",
                    ["get_non_manifold_edge_diagnostic", "GetNonManifoldEdgeDiagnostic"],
                ),
                "toxic_bodies": id_check(
                    "Toxic bodies",
                    ["get_body_toxic_diagnostic", "get_toxic_body_diagnostic", "GetBodyToxicDiagnostic", "GetToxicBodyDiagnostic"],
                ),
                "sliver_faces": id_check(
                    "Sliver faces",
                    ["get_sliver_face_diagnostic", "GetSliverFaceDiagnostic"],
                ),
                "_compute_ok": compute_ok,
            }

            total = sum(
                item.get("count", 0)
                for item in report.values()
                if isinstance(item, dict)
            )

            log("")
            log("Total CAD diagnostic issue groups found: {0}".format(total))

            macro_dir = HERE
            output_file = macro_dir / "cad_diagnostics_report.json"
            output_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

            log("Report written to:")
            log(str(output_file))
            log("=== CAD Diagnostics Macro Complete ===")
            log("")

            if CHECK_ONLY:
                # Nothing follows this return, so leave the row on a final
                # value rather than a status that will never advance.
                try:
                    import ui_bridge
                    ui_bridge.update_state("params", {
                        "cad_diagnostics": "{0} issue group(s) found (check-only)".format(total)
                        if total else "No issues found (check-only)"})
                except Exception:
                    pass
                log("CHECK-ONLY MODE: stopping here.")
                log("  No repair prompt, no Fusion round trip, no automation.")
                log("  Edge-edge intersections: {0}".format(
                    report.get("edge_edge_intersections", {}).get("count", "?")))
                log("  Total issue groups: {0}".format(total))
                log("  Run this a second time on the SAME study and compare.")
                return

            # The checks are done but the card is still several steps away --
            # a possible wait on Synergy's own import box, then reframing the
            # model. Keep saying so right up to the moment it opens.
            _publish_cad_status("Preparing CAD Diagnostics results...")

            # Let the user click OK on Synergy's "File imported successfully" box
            # first, so the Automation Workflow dialog never opens on top of it.
            wait_for_import_dialog_dismissed(log)

            # Diagnostics have finished and their report is written, so show
            # the user what was imported before the automation card goes up.
            # This is the point the import flow was missing: the model was
            # only ever framed by run_startup BEFORE diagnostics ran, so by the
            # time the checks ended the viewport was showing the diagnostic
            # state instead of the part.
            show_imported_model(sy, log)

            modeler = get_modeler()

            if fusion_trips >= MAX_FUSION_TRIPS:
                log("Fusion repair has already been applied {0} time(s) and the "
                    "re-check still reports issues. Sending the model round again "
                    "would repeat a repair that has already run -- Fusion's Repair "
                    "fixes defects Moldflow cannot see, while its Refit Bad "
                    "Surfaces / Gap Healing rebuild leaves the edge-edge "
                    "intersection Moldflow does see, so this cannot converge. "
                    "Stopping the repair loop; proceeding to the automation "
                    "decision with the geometry as it stands."
                    .format(fusion_trips))
                try:
                    import ui_bridge
                    ui_bridge.update_state("params", {
                        "cad_diagnostics": "Repaired in Fusion {0}x - repair loop "
                                           "stopped".format(fusion_trips)})
                except Exception:
                    pass

            decision, new_study_doc = show_automation_prompt(
                report, total, "\n".join(log_lines), log, sy, modeler, bodies)

            if decision == "start":
                run_automation_workflow(sy, study_doc, log, show_dialog)
                break

            if decision == "recheck" and new_study_doc is not None:
                fusion_trips += 1
                log("Fusion round trip {0} of at most {1}.".format(
                    fusion_trips, MAX_FUSION_TRIPS))
                study_doc = new_study_doc
                cad = get_member(sy, "cad_diagnostic")
                if callable(cad):
                    cad = cad()
                # Name the study on THIS pass too. The 'Analysing study:' line
                # is printed once at the top of main(), so a re-check used to
                # report numbers with no study attached to them -- which is how
                # a post-repair count got compared against a pre-repair count
                # without anything proving they came from different studies.
                for _attr in ("DisplayName", "StudyName"):
                    try:
                        _v = call_member(study_doc, _attr)
                        if _v:
                            log("Re-running CAD Diagnostics on {0!r} (StudyDoc.{1}) "
                                "-- the study returned from Fusion.".format(str(_v), _attr))
                            break
                    except Exception:
                        continue
                else:
                    log("Re-running CAD Diagnostics on the study returned from Fusion "
                        "(name unavailable)...")
                continue

            log("Automation workflow stopped by user choice or pause.")
            try:
                import ui_bridge
                ui_bridge.update_state("params", {"cad_diagnostics": "Paused - Go back to Fusion"})
            except Exception:
                pass
            break

    except Exception as exc_main:
        error_text = traceback.format_exc()
        log("CAD Diagnostics Macro ERROR:")
        log(error_text)

        # A crash inside meshing or solving skips their unfold, which would
        # strand the panel collapsed exactly when the recovery card needs to be
        # read. Unconditional here: asking for "normal" on a panel that is
        # already open is a no-op.
        unfold_panel(True, log, "workflow error recovery")

        # Trigger Error Recovery card in embedded UI when CAD model entity selection or diagnostic fails
        recovery_choice = None
        try:
            import ui_bridge
            recovery_choice = ui_bridge.request_error_recovery_choice(
                "CAD Diagnostic / Model import error detected.\n"
                "Details: {0}".format(exc_main)
            )
        except Exception as e_rec:
            log("Failed to request UI recovery choice: {0}".format(e_rec))

        if recovery_choice == "translate_surface":
            log("User selected 'Translate Surface' after error. Executing CAD surface translation repair...")
            translate_ok = False
            try:
                modeler = get_modeler()
                if modeler is not None:
                    for fn in ("TranslateSurface", "translate_surface", "RepairGeometry", "repair_geometry"):
                        try:
                            res = call_member(modeler, fn)
                            if res is not None and bool(res):
                                translate_ok = True
                                log("Modeler {0}() completed successfully.".format(fn))
                                break
                        except Exception:
                            pass
            except Exception as e_ts:
                log("Translate Surface execution error: {0}".format(e_ts))

            if translate_ok:
                try:
                    sy.Build()
                except Exception:
                    pass
                log("Translate Surface completed. Resuming workflow...")
                run_automation_workflow(sy, study_doc, log, show_dialog)
            else:
                log("Translate Surface failed.")
                show_dialog(
                    "Translate Surface Failed",
                    "Translate Surface operation could not automatically resolve geometry defects.\n\n"
                    "Please return to Fusion to repair the CAD model geometry."
                )
        elif recovery_choice == "go_back_to_fusion":
            log("User selected 'Go back to Fusion' after error -- starting the Synergy/Fusion "
                "CAD repair round trip (Modeler.ModifiedWithInventorFusion).")
            recovered_study_doc = None
            try:
                recovery_study_doc = get_active_study_doc()
                modeler = get_modeler()
                if recovery_study_doc is not None and modeler is not None:
                    recovery_bodies, _ = build_cad_body_entlist(recovery_study_doc)
                    recovered_study_doc = run_fusion_repair_roundtrip(sy, modeler, recovery_bodies, log)
                else:
                    log("Fusion round trip: no active study/modeler to select the CAD body from.")
            except Exception as e_fr:
                log("Fusion round trip error: {0}".format(e_fr))

            if recovered_study_doc is not None:
                log("Fusion round trip complete. Resuming workflow on the repaired study...")
                run_automation_workflow(sy, recovered_study_doc, log, show_dialog)
            else:
                log("Automation safely paused for manual CAD repair in Fusion.")
                try:
                    import ui_bridge
                    ui_bridge.update_state("params", {"cad_diagnostics": "Paused - Go back to Fusion"})
                except Exception:
                    pass
        else:
            log("Automation safely paused for manual CAD repair in Fusion.")
            try:
                import ui_bridge
                ui_bridge.update_state("params", {"cad_diagnostics": "Paused - Go back to Fusion"})
            except Exception:
                pass

def run_automation_workflow(sy, study_doc, log, show_dialog):
    import time
    import ctypes

    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    IDYES = 6

    def ask_yes_no(title, message):
        try:
            import ui_bridge
            ans = ui_bridge.prompt_user(title, message, options=["Yes", "No"])
            return IDYES if ans == "Yes" else 0
        except Exception:
            pass
        return ctypes.windll.user32.MessageBoxW(
            None, str(message), str(title),
            MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND
        )

    log("")
    log("=======================================")
    log("=== STARTING AUTOMATION WORKFLOW ===")
    log("=======================================")

    # Keep Synergy's own message boxes out of the way for the automated run.
    # They are modal, they land on top of the panel, and the workflow is
    # blocked behind them until a human clicks OK.
    #
    # Two mechanisms, because one is not enough: Silence(True) is the
    # documented control ("Suppresses message boxes. Off by default.") and it
    # returns True here -- but the "Study : X / Analysis complete" box appears
    # regardless, exactly the incompleteness the reference warns about. So a
    # watcher closes that specific box as well.
    #
    # Both are scoped to this function so the restore in its finally can
    # actually see them; the first version set the flag in main() and read it
    # here, which is a NameError that took out the end of a completed run
    # (deck written, then "name '_silenced' is not defined" surfaced as a
    # bogus Model Error Recovery card).
    _silenced = False
    try:
        if bool(sy.Silence(True)):
            _silenced = True
            log("Synergy message boxes silenced for the automated run.")
        else:
            log("Synergy.Silence(True) returned False; relying on the watcher.")
    except Exception as e:
        log("Synergy.Silence not available on this build ({0}).".format(e))
    _stop_dialog_watcher = start_dialog_watcher(log)
    # Synergy's help failing once is proof the local help package is missing,
    # so the results stage stops pressing F1 and opens the browser directly
    # from then on -- the box cannot recur if it is never triggered again.
    _help_broken = {"v": False}

    def _note_dismissed(fragment):
        if fragment == _HELP_FAILURE_TEXT and not _help_broken["v"]:
            _help_broken["v"] = True
            log("Synergy's local help is not installed; result help will open "
                "in the browser instead of pressing F1.")

    _stop_dialog_hook = start_dialog_hook(log, on_dismiss=_note_dismissed)

    # Captured as the workflow runs and reported on the presentation's cover
    # slide. Bound here so the report can never fail on an unset name if an
    # earlier phase took an exception path.
    material_name = ""
    mesh_type_name = ""
    # Per-material recommended process defaults (mold/melt temperature), read
    # dynamically from the assigned material in Phase 2 and used to seed the
    # Process Settings form so it shows the SAME defaults as the native wizard
    # instead of the generic process-controller value. { tcode: degC }.
    material_temp_defaults = {}

    try:
        # Refresh the User Journey card. It was already filled in before CAD
        # diagnostics; this second pass adds the analysis sequence, which is
        # only chosen in Phase 2, and marks diagnostics complete.
        try:
            sync_journey_from_study(sy, study_doc, log, include_diagnostics=True)
        except Exception as e_init_state:
            log("Error syncing initial study state to ui_bridge: {0}".format(
                e_init_state))


        # -----------------------------------------
        # PHASE 2: SEQUENCE & MATERIAL CONFIGURATION
        # -----------------------------------------
        log("Phase 2: Sequence & Material Configuration")
        
        # Verify and set MeshType and MoldingProcess.
        #
        # MeshType used to be forced to "3D" unconditionally, which silently
        # threw away a Dual Domain import: the type was read purely to log it.
        # Dual Domain is now honoured, because it is the only mesh type on
        # which Moldflow's Gate Location analysis can pick the injection
        # location for us (see the auto-gate block before gate placement).
        current_mt = None
        try:
            current_mt = study_doc.MeshType()
            mesh_type_name = str(current_mt or "").strip()
            log("Current Study MeshType: {0}".format(current_mt))
        except Exception as em:
            log("Error getting MeshType: {0}".format(em))

        # "Dual Domain" is only the UI label. The API's MeshType enum is
        # Midplane / **Fusion** / 3D (see moldflow.common.MeshType), and
        # `Fusion` IS Dual Domain -- Moldflow renamed the technology but kept
        # the original token. Matching on "dualdomain" therefore never fires:
        # a live 12:27 run logged `Current Study MeshType: Fusion` and was
        # forced straight to 3D, so the automatic gate offer never appeared.
        # "dualdomain" is kept only as a defensive alias.
        _mt_key = "".join(mesh_type_name.split()).lower()
        is_dual_domain = _mt_key in ("fusion", "dualdomain")

        if is_dual_domain:
            # Report the modern name; "Fusion" in a client deck is wrong
            # terminology for what Moldflow now calls Dual Domain.
            mesh_type_name = "Dual Domain"
            log("Study is Dual Domain (API reports '{0}') — keeping that mesh "
                "type (NOT forcing 3D) so Gate Location can run on "
                "it.".format(current_mt))
        else:
            try:
                study_doc.MeshType = "3D"
                log("Set Study MeshType to '3D' successfully.")
            except Exception as ems:
                log("Error setting MeshType to '3D': {0}".format(ems))

        try:
            current_mp = study_doc.MoldingProcess()
            log("Current Study MoldingProcess: {0}".format(current_mp))
        except Exception as ep:
            log("Error getting MoldingProcess: {0}".format(ep))
        
        # 1. Select Analysis Sequence
        #    There is no native Moldflow dialog API for sequence selection.
        #    We present our own selection dialog using ctypes and then set the
        #    property on StudyDoc via COM.
        import os as _os
        
        ANALYSIS_SEQUENCES = [
            "Fill",
            "Fill + Pack",
            "Fill + Pack + Warp",
            "Cool (FEM)",
            "Cool (FEM) + Fill + Pack",
            "Cool (FEM) + Fill + Pack + Warp",
            "Warp",
        ]
        
        user_input = ""
        vbs_path = SESSION_DIR / "_seq_select.vbs"
        try:
            try:
                import ui_bridge
                ans = ui_bridge.prompt_user(
                    "Analysis Sequence Selection",
                    "Select Analysis Sequence for your study:",
                    options=ANALYSIS_SEQUENCES
                )
                if ans in ANALYSIS_SEQUENCES:
                    idx = ANALYSIS_SEQUENCES.index(ans) + 1
                    user_input = str(idx)
                    log("User selected sequence via embedded UI: {0} (#{1})".format(ans, user_input))
            except Exception as e_ui:
                log("UI bridge sequence selection error/fallback: {0}".format(e_ui))

            if not user_input:
                vbs_script = (
                    'Dim choices\n'
                    'choices = ""'
                )
                for i, seq in enumerate(ANALYSIS_SEQUENCES, 1):
                    vbs_script += '\nchoices = choices & "{0}. {1}" & vbCrLf'.format(i, seq)
                vbs_script += (
                    '\nDim result\n'
                    'result = InputBox("Select Analysis Sequence:" & vbCrLf & vbCrLf & choices & vbCrLf & '
                    '"Enter the number (1-{0}):", "Analysis Sequence Selection", "2")\n'.format(len(ANALYSIS_SEQUENCES))
                )
                vbs_script += 'WScript.Echo result\n'
                
                vbs_path.write_text(vbs_script, encoding="utf-8")
                
                import subprocess
                try:
                    proc = subprocess.run(
                        ["cscript", "//Nologo", str(vbs_path)],
                        capture_output=True, text=True, timeout=120
                    )
                    user_input = proc.stdout.strip()
                    log("User sequence input: '{0}'".format(user_input))
                except Exception as e_vbs:
                    log("VBS sequence error: {0}".format(e_vbs))
            
            if user_input and user_input.isdigit():
                idx = int(user_input) - 1
                if 0 <= idx < len(ANALYSIS_SEQUENCES):
                    selected_seq = ANALYSIS_SEQUENCES[idx]
                    log("User selected sequence: {0}".format(selected_seq))
                    # "Flow" IS Moldflow's token for Fill+Pack -- PROVEN by the
                    # manual Fill+Pack run (2026-07-20), whose results folder is
                    # named "Flow" and contains the pack results (Pressure at V/P
                    # switchover, Sink marks, Volumetric shrinkage, Air traps).
                    # Do NOT reorder this list; the sequence handling is correct.
                    # Tokens transcribed from the "Thermoplastics Injection
                    # Molding (3D)" PROC block of
                    #   ...\Moldflow Synergy 2027\data\dat\process.dat
                    # where each sequence is defined as `{ <id> "<token>" }`.
                    #
                    # MOLDFLOW SEPARATES PHASES WITH '|', NOT '+'.  The old
                    # table used '+' throughout ("Cool+Flow+Warp"), so every
                    # candidate for options 3/5/6/7 was rejected, the property
                    # assignment never landed, and the study silently kept its
                    # previous sequence -- a fresh study stayed on 'Fill' and
                    # the whole run (and its report) came out fill-only.
                    #
                    # Note 'Cool' (2050) and 'Cool (FEM)' (2074) are DIFFERENT
                    # sequences; the old table listed "Cool" first under the
                    # Cool (FEM) entry, which "succeeded" while quietly
                    # selecting the wrong analysis.  FEM tokens come first now.
                    SEQUENCE_MAPPINGS = {
                        "Fill": ["Fill"],
                        "Fill + Pack": ["Flow"],
                        "Fill + Pack + Warp": ["Flow|Warp"],
                        # 2074 is the real Cool (FEM); plain 'Cool' (2050) is a
                        # deliberate second choice for studies that lack it.
                        "Cool (FEM)": ["Cool (FEM)", "Cool"],
                        "Cool (FEM) + Fill + Pack": ["Cool (FEM)|Fill|Pack", "Cool|Flow"],
                        "Cool (FEM) + Fill + Pack + Warp": ["Cool (FEM)|Fill|Pack|Warp", "Cool|Flow|Warp"],
                        # No standalone Warp token exists for a 3D mesh; warp
                        # is only reachable as the tail of a flow sequence.
                        "Warp": ["Flow|Warp"],
                    }

                    candidates = SEQUENCE_MAPPINGS.get(selected_seq, [selected_seq])
                    log("Testing sequence candidates for '{0}': {1}".format(selected_seq, candidates))
                    seq_set = False

                    for cand in candidates:
                        try:
                            # Try both SetAnalysisSequence method and direct assignment
                            try:
                                study_doc.SetAnalysisSequence(cand)
                            except Exception:
                                study_doc.AnalysisSequence = cand

                            verified = str(study_doc.AnalysisSequence() or "").strip()
                            # Description is logged for diagnostics only -- it is
                            # NOT used to accept/reject, since the token vocabulary
                            # ("Flow" == Fill+Pack) is already proven correct.
                            try:
                                desc = str(study_doc.AnalysisSequenceDescription() or "").strip()
                            except Exception:
                                desc = "(unavailable)"
                            log("Tried candidate '{0}' -> AnalysisSequence='{1}', Description='{2}'".format(
                                cand, verified, desc))

                            # With real process.dat tokens the readback returns
                            # the token verbatim, so an exact (case/space
                            # insensitive) comparison is the whole test -- the
                            # old '+'-era alias table ("flow" == "fill+pack")
                            # existed only to paper over wrong tokens.
                            # NOTE: deliberately no `re` here.  This enclosing
                            # function does `import re` further down its body,
                            # which makes `re` a LOCAL of run_automation_workflow
                            # for the whole scope — a nested closure touching it
                            # this early raises "cannot access free variable 're'"
                            # and silently loses an assignment that had actually
                            # succeeded.  str methods have no such hazard.
                            def _norm(s):
                                return "".join(str(s).split()).lower()

                            if _norm(cand) == _norm(verified):
                                log("Verified sequence '{0}' is set (Description: '{1}').".format(
                                    cand, desc))
                                seq_set = True
                                break
                        except Exception as ce:
                            log("Setting sequence to '{0}' failed: {1}".format(cand, ce))

                    if not seq_set:
                        # Do NOT carry on quietly.  This is precisely how a run
                        # selected as Cool (FEM)+Fill+Pack+Warp silently
                        # executed as fill-only and produced a fill-only
                        # report that looked like a stale copy of the
                        # previous analysis.
                        current = ""
                        try:
                            current = str(study_doc.AnalysisSequence() or "").strip()
                        except Exception:
                            pass
                        log("ERROR: could not set the analysis sequence to '{0}'. "
                            "Tried {1}; the study is still on '{2}'.".format(
                                selected_seq, candidates, current or "(unknown)"))
                        keep = ask_yes_no(
                            "Analysis Sequence Not Set",
                            "The analysis sequence could not be changed to:\n"
                            "    {0}\n\n"
                            "The study is still set to '{1}', so the analysis and "
                            "the report would cover '{1}' only.\n\n"
                            "Continue anyway with '{1}'?\n"
                            "(Select 'No' to stop so you can set the sequence "
                            "manually via Analysis Sequence.)".format(
                                selected_seq, current or "unknown")
                        )
                        if keep != IDYES:
                            log("User chose to stop after the sequence could not be set.")
                            show_dialog(
                                "Workflow Stopped",
                                "Set the analysis sequence manually\n"
                                "(Home > Analysis Sequence), then restart the workflow."
                            )
                            return
                        log("User chose to continue with sequence '{0}'.".format(current))
                else:
                    log("Invalid sequence number: {0}. Keeping current sequence.".format(user_input))
            else:
                log("No sequence selected or cancelled. Keeping current sequence.")
        except Exception as e:
            log("Sequence selection error: {0}".format(e))
        finally:
            try:
                vbs_path.unlink()
            except Exception:
                pass
        
        seq_name = ""
        try:
            seq_name = study_doc.AnalysisSequence()
        except Exception as e:
            log("Error getting AnalysisSequence: {0}".format(e))
        log("Active sequence: {0}".format(seq_name))

        # NOW the sequence is a fact: chosen, applied and verified. Publish the
        # readable description ("Fill + Pack + Warp") rather than the internal
        # token ("Flow|Warp"), and only at this point -- anything earlier is
        # showing the user a choice they have not made yet.
        try:
            _seq_shown = (_ask_member(study_doc, "AnalysisSequenceDescription")
                          or _label_text(seq_name))
            if _seq_shown:
                import ui_bridge as _ub_seq
                _ub_seq.update_state("params", {"analysis_sequence": _seq_shown})
        except Exception as _e_seq_pub:
            log("Could not publish the analysis sequence to the panel: "
                "{0}".format(_e_seq_pub))

        # 2. Select Material — ask user if they want to open Synergy Material Selection dialog
        ans = ask_yes_no("Material Selection", "Do you want to select the material?")
        if ans == IDYES:
            log("Opening Material Selection dialog...")
            try:
                import os as _os2
                pid = _os2.getpid()
                mat_sel = sy.MaterialSelector()
                result = mat_sel.SelectViaDialog(0, pid)
                if result:
                    log("Material selected successfully via dialog.")
                else:
                    log("Material selection was cancelled or failed.")
            except Exception as e:
                log("MaterialSelector.SelectViaDialog failed: {0}".format(e))
                show_dialog(
                    "Material Selection",
                    "Could not open the Material Selection dialog automatically.\n\n"
                    "Please select the material manually from the Synergy Home tab."
                )
        else:
            log("User skipped material selection dialog. Continuing with default material/settings.")
                
        # 3. Query assigned material properties
        prop_ed = sy.PropertyEditor()
        process_name = ""
        try:
            process_name = study_doc.MoldingProcess()
        except Exception as e:
            log("Error getting MoldingProcess: {0}".format(e))
        log("Active Molding Process: {0}".format(process_name))
        
        injection_id = 40000
        if any(term in process_name.upper() for term in ["REACTIVE", "THERMOSET", "RTM", "UNDERFILL", "MICROCHIP"]):
            injection_id = 40002
            
        inj_prop = prop_ed.GetFirstProperty(injection_id)
        if inj_prop is None:
            log("Warning: Could not find Injection Location property (ID {0}). Trying fallback 40000.".format(injection_id))
            inj_prop = prop_ed.GetFirstProperty(40000)
            
        has_fiber = False
        if inj_prop is None:
            log("Warning: Injection Location property not found. Skipping fiber check.")
        else:
            # Material ID reference T-Code is 20020
            mat_field = inj_prop.FieldValues(20020)
            if mat_field is None or mat_field.Size() < 2:
                log("Warning: Material field 20020 not found. Skipping fiber check.")
            else:
                mat_id = mat_field.Val(0)
                mat_sub_id = mat_field.Val(1)
                log("Assigned Material ID: {0}, SubID: {1}".format(mat_id, mat_sub_id))
                
                mat_prop = prop_ed.FindProperty(mat_id, mat_sub_id)
                if mat_prop is None:
                    log("Warning: Material property not found in database. Skipping fiber check.")
                else:
                    # Get Manufacturer and Trade Name for logging
                    mfr = ""
                    tn = ""
                    try:
                        mfr_field = mat_prop.FieldValues(1997)
                        if mfr_field is not None:
                            mfr = str(mfr_field.Val(0)).strip()
                        tn_field = mat_prop.FieldValues(1998)
                        if tn_field is not None:
                            tn = str(tn_field.Val(0)).strip()
                    except Exception:
                        pass
                    # Manufacturer/trade-name fields come back empty on some
                    # grades, so fall back to the property's own Name.
                    material_name = " ".join(p for p in (mfr, tn) if p).strip()
                    if not material_name:
                        for attr in ("Name", "Description"):
                            try:
                                v = getattr(mat_prop, attr)
                                v = str(v() if callable(v) else (v or "")).strip()
                            except Exception:
                                continue
                            if v:
                                material_name = v
                                break
                    log("Assigned Material: {0}".format(
                        material_name or "(not identified)"))
                    try:
                        import ui_bridge
                        ui_bridge.update_state("params", {"material": material_name or "Selected from Database"})
                    except Exception as e_ui_m:
                        log("Error updating material in ui_bridge: {0}".format(e_ui_m))

                    # ---------------------------------------------------------
                    # Material recommended processing temperatures.
                    # The native Process Settings Wizard seeds Mold surface
                    # temperature (11108) and Melt temperature (11002) from the
                    # ASSIGNED MATERIAL, not from the generic process controller
                    # (which always holds Moldflow's 60/200 fallback). The
                    # material stores these as recommended RANGES on its own
                    # property: TCode 1808 = mold range, 1800 = melt range
                    # (Minimum, Maximum). FieldValues returns them already in the
                    # active unit system (degC), same as 11108/11002. The wizard
                    # default is the range midpoint. Read + log both ends so the
                    # value is dynamic (never hardcoded) and fully verifiable.
                    for _tc, _lbl in ((1808, "mold surface temperature"),
                                      (1800, "melt temperature")):
                        try:
                            rng = mat_prop.FieldValues(_tc)
                            if rng is not None and int(rng.Size() or 0) >= 1:
                                lo = float(rng.Val(0))
                                hi = float(rng.Val(1)) if int(rng.Size()) >= 2 else lo
                                mid = (lo + hi) / 2.0
                                dst = 11108 if _tc == 1808 else 11002
                                material_temp_defaults[dst] = mid
                                log("Material recommended {0}: {1:.1f} to {2:.1f} degC"
                                    " -> default {3:.1f} degC (min={1:.1f}, max={2:.1f})".format(
                                        _lbl, lo, hi, mid))
                            else:
                                log("Material recommended {0} (TCode {1}) not "
                                    "available on this grade.".format(_lbl, _tc))
                        except Exception as _e:
                            log("Material recommended {0} (TCode {1}) read failed: {2}".format(
                                _lbl, _tc, _e))

                    # Check fiber properties (TCode 3010)
                    filler_field = mat_prop.FieldValues(3010)
                    if filler_field is not None and filler_field.Size() > 0:
                        filler_desc = str(filler_field.Val(0)).lower()
                        log("Material filler description: {0}".format(filler_desc))
                        if any(term in filler_desc for term in ["fiber", "glass", "carbon"]):
                            has_fiber = True
                            
        # 4. Toggle Fiber Flow Parameter.
        #    TCode 36000 (fiber calculation on/off) lives on the ANALYSIS
        #    PARAMETERS property, type 10000 ("PARF" in process.dat; its DEPC
        #    field list holds 36000/36004/36005) — NOT on the injection-location
        #    property (40000). A write to 40000 "succeeds" silently
        #    (SetFieldValues does not validate the TCode against the type) but
        #    the solver never reads it, so the fiber calc stays unset. Write to
        #    every type-10000 occurrence and commit.
        FIBER_PARAM_TYPE = 10000
        fiber_targets = list(_iter_props_of_type(prop_ed, FIBER_PARAM_TYPE))
        if fiber_targets:
            if has_fiber:
                log("Material is FIBER-FILLED. Auto-enabling fiber orientation analysis option.")
                flag = 1
            else:
                log("Material is NON-FIBER. Auto-disabling fiber orientation analysis option.")
                flag = 0
            wrote = 0
            for process_prop in fiber_targets:
                try:
                    new_vals = sy.CreateIntegerArray()
                    new_vals.AddInteger(flag)
                    process_prop.SetFieldValues(36000, new_vals)
                    wrote += 1
                except Exception as pe:
                    log("Error setting parameter 36000 on occurrence {0}: {1}".format(
                        _prop_id(process_prop), pe))
            if wrote:
                try:
                    prop_ed.CommitChanges("Edit")
                    log("Updated Fiber Orientation parameter (TCode 36000) on {0} of "
                        "{1} analysis-parameters (type 10000) occurrence(s).".format(
                            wrote, len(fiber_targets)))
                except Exception as pe:
                    log("CommitChanges for fiber toggle failed: {0}".format(pe))
        else:
            log("Analysis-parameters property (type 10000) not found. Skipping fiber toggle.")
            
        # -----------------------------------------
        # PHASE 3: PROCESS PARAMETERS & MESH
        # -----------------------------------------
        log("")
        log("Phase 3: Process Parameters & Auto-Mesh")

        # -----------------------------------------------------------------
        # Process Settings form (replaces Synergy's own dialog, which CANNOT
        # be opened: PropertyEditor.ShowPropertyPage does not exist and
        # MaterialSelector.SelectViaDialog is the only dialog API in the whole
        # Synergy surface). The field list below is Moldflow's OWN wizard
        # definition, read from process.dat WTAB lines:
        #   Fill      (line 567): 30011 11108 11002 10109 10310 10704
        #   Fill+Pack (line 612): 30011 11108 11002 10109 10310 10704 11109
        # Labels / choices / defaults / units come from tcodes.dat. They are
        # embedded rather than parsed at runtime so a missing or moved data
        # file can never break the workflow.
        # -----------------------------------------------------------------
        PROC_FIELDS = {
            11108: ("Mold surface temperature", "temp", None),
            11002: ("Melt temperature", "temp", None),
            10109: ("Filling control", "enum", [
                ("Automatic", 1), ("Injection time", 2), ("Flow rate", 3),
                ("Relative ram speed profile", 5), ("Absolute ram speed profile", 6)]),
            10310: ("Velocity/pressure switch-over", "enum", [
                ("Automatic", 0), ("By %volume filled", 1), ("By ram position", 8),
                ("By injection pressure", 2), ("By hydraulic pressure", 3),
                ("By clamp force", 4), ("By pressure control point", 5),
                ("By injection time", 6), ("By whichever comes first", 7)]),
            10704: ("Pack/holding control", "enum", [
                ("Automatic", 5), ("%Filling pressure vs time", 4),
                ("Packing pressure vs time", 2), ("Hydraulic pressure vs time", 1),
                ("%Maximum machine pressure vs time", 3)]),
            11109: ("Cooling time", "enum", [("Specified", 1), ("Automatic", 2)]),
        }

        def proc_fields_for_sequence(seq):
            """The TCodes Moldflow's own wizard shows for this sequence."""
            return [11108, 11002, 10109, 10310, 10704, 11109]

        def resolve_process_controller():
            """The process controller the study actually SOLVES with.

            `GetFirstProperty(30011)` can return an occurrence the study does
            not use (root-caused via Autodesk's own CustomReport.vbs). The
            correct chain is: the injection property's TCode 20040 holds
            Val(0)=ProcessID and Val(1)=SubID -> FindProperty(those)."""
            pe = sy.PropertyEditor()
            for inj_type in (40000, 40002):
                try:
                    inj = pe.GetFirstProperty(inj_type)
                    if inj is None:
                        continue
                    ref = inj.FieldValues(20040)
                    if ref is not None and int(ref.Size() or 0) >= 2:
                        pid, sub = ref.Val(0), ref.Val(1)
                        prop = pe.FindProperty(pid, sub)
                        if prop is not None:
                            log("Process controller resolved via TCode 20040: "
                                "ID={0}, SubID={1}".format(pid, sub))
                            return pe, prop
                except Exception as e:
                    log("20040 resolution on type {0} failed: {1}".format(inj_type, e))
            try:
                prop = pe.GetFirstProperty(30011)
                if prop is not None:
                    log("Process controller fell back to GetFirstProperty(30011).")
                    return pe, prop
            except Exception as e:
                log("GetFirstProperty(30011) failed: {0}".format(e))
            return pe, None

        # Temperature TCodes 11108 (Mold surface) and 11002 (Melt) are the ONLY
        # "temp" fields here. Although tcodes.dat labels their unit "K", the raw
        # COM Property.FieldValues / SetFieldValues exchange these in the active
        # unit system (degC), NOT SI Kelvin: a fresh study reads FieldValues ==
        # 60.0 / 200.0 (verified in the run logs), which is exactly what the
        # native Process Settings wizard shows. So the value is ALREADY in degC
        # both ways -- apply NO 273.15 offset. A previous build subtracted 273.15
        # on display and produced the -213.15 / -73.15 bug (60-273.15, 200-273.15).
        # These stay IDENTITY on purpose; do not re-introduce a K<->C conversion.
        def k_to_c(k):
            return float(k)          # FieldValues already returns degC

        def c_to_k(c):
            return float(c)          # SetFieldValues expects the same degC

        def read_proc_values(prop, tcodes):
            """{tcode: float} for fields present and writable on this property."""
            out = {}
            for tc in tcodes:
                try:
                    if not prop.IsFieldWritable(tc):
                        continue
                except Exception:
                    pass
                try:
                    fv = prop.FieldValues(tc)
                    if fv is not None and int(fv.Size() or 0) >= 1:
                        out[tc] = float(fv.Val(0))
                except Exception:
                    continue
            return out

        def show_process_settings_form():
            """Replica of the Process Settings dialog. Returns True if it was
            shown (whatever the user then did), False to fall back."""
            try:
                pe, prop = resolve_process_controller()
                if prop is None:
                    log("Process controller not found — falling back to the manual prompt.")
                    return False

                tcodes = proc_fields_for_sequence(seq_name)
                current = read_proc_values(prop, tcodes)
                if not current:
                    log("No writable process fields found — falling back to the manual prompt.")
                    return False

                # Seed the mold/melt DEFAULT from the assigned material so the
                # form shows the same value the native wizard does. The wizard
                # takes the midpoint of the material's recommended range (TCode
                # 1808 mold / 1800 melt, read in Phase 2); the generic process
                # controller only ever holds Moldflow's 60/200 fallback. This is
                # a MATERIAL property, independent of the analysis sequence, so a
                # different grade correctly yields its own default (e.g. 50/220).
                # Verified against POLYFLAM RIPP 3625: mold 40-80 -> 60, melt
                # 180-220 -> 200, matching the wizard exactly (no regression).
                # DISPLAY ONLY -- the write-back stays edit-only, so leaving the
                # seeded value unchanged still performs zero property writes.
                for tc in (11108, 11002):
                    if tc in current and material_temp_defaults.get(tc) is not None:
                        seeded = float(material_temp_defaults[tc])
                        log("Process form: {0} default = {1:.1f} degC from material "
                            "recommended range (process controller held {2:.1f}).".format(
                                tc, seeded, current[tc]))
                        current[tc] = seeded

                # Check if embedded UI panel is available
                try:
                    import ui_bridge
                    if ui_bridge.ui_alive():
                        fields_list = []
                        for tc in tcodes:
                            if tc not in PROC_FIELDS:
                                continue
                            lbl, knd, opts = PROC_FIELDS[tc]
                            raw = current.get(tc, 1.0 if knd == "enum" else 60.0)
                            fields_list.append({
                                "tcode": tc,
                                "label": lbl,
                                "kind": knd,
                                "value": k_to_c(raw) if knd == "temp" else raw,
                                "options": [n for n, v in opts] if opts else []
                            })

                        m_temp = current.get(11108, 60.0)
                        melt_temp = current.get(11002, 200.0)
                        res = ui_bridge.request_process_settings(fields=fields_list, mold_temp=m_temp, melt_temp=melt_temp, sequence=str(seq_name or ""))
                        if isinstance(res, dict):
                            changes = {}
                            for tc in tcodes:
                                if tc not in PROC_FIELDS:
                                    continue
                                lbl, knd, opts = PROC_FIELDS[tc]
                                if tc in res:
                                    val = res[tc]
                                    if knd == "temp":
                                        new_v = c_to_k(float(val))
                                        if abs(new_v - current.get(tc, new_v)) > 1e-6:
                                            changes[tc] = new_v
                                    elif knd == "enum" and opts:
                                        opt_map = {n: v for n, v in opts}
                                        if val in opt_map:
                                            new_v = float(opt_map[val])
                                            if abs(new_v - current.get(tc, new_v)) > 1e-6:
                                                changes[tc] = new_v
                            if changes:
                                for tc, new in changes.items():
                                    try:
                                        arr = sy.CreateDoubleArray()
                                        arr.AddDouble(float(new))
                                        prop.SetFieldValues(tc, arr)
                                        log("  Set {0} ({1}) = {2} via UI panel".format(PROC_FIELDS[tc][0], tc, new))
                                    except Exception as e_set:
                                        log("  Failed to set {0} via UI: {1}".format(tc, e_set))
                                try:
                                    pe.CommitChanges("Edit")
                                except Exception:
                                    pass

                            # Journey summary. This used to live inside
                            # `if changes:`, so accepting the defaults -- the
                            # normal case -- left the row on "Pending..." for
                            # the rest of the run even though the settings were
                            # reviewed and applied. It also printed the Kelvin
                            # values with a degC suffix; the temperatures the
                            # form works in are Celsius, so convert.
                            try:
                                def _eff_c(tc, fallback_k):
                                    if tc in res:
                                        try:
                                            return float(res[tc])
                                        except (TypeError, ValueError):
                                            pass
                                    return k_to_c(fallback_k)

                                mold_c = _eff_c(11108, m_temp)
                                melt_c = _eff_c(11002, melt_temp)
                                ui_bridge.update_state("params", {
                                    "process_settings": "Mold {0:.0f}°C, Melt {1:.0f}°C{2}".format(
                                        mold_c, melt_c,
                                        "" if changes else " (defaults)")})
                            except Exception as e_js:
                                log("  Could not update the process-settings "
                                    "journey row: {0}".format(e_js))
                            return True
                except Exception as e_ui_proc:
                    log("UI bridge process settings error: {0}".format(e_ui_proc))

                rows, order = [], []
                for tc in tcodes:
                    if tc not in current:
                        continue
                    label, kind, opts = PROC_FIELDS[tc]
                    raw = current[tc]
                    order.append((tc, kind))
                    if kind == "enum":
                        sel = "".join(
                            '<option value="{0}"{1}>{2}</option>'.format(
                                v, " selected" if abs(raw - v) < 1e-9 else "", n)
                            for n, v in opts)
                        rows.append(
                            '<tr><td class="l">{0}</td><td>'
                            '<select id="f{1}">{2}</select></td><td class="u"></td></tr>'.format(
                                label, tc, sel))
                    else:
                        rows.append(
                            '<tr><td class="l">{0}</td><td>'
                            '<input id="f{1}" value="{2:.4g}"></td>'
                            '<td class="u">&deg;C</td></tr>'.format(label, tc, k_to_c(raw)))
                    log("  Current {0} ({1}) = {2}".format(
                        label, tc, k_to_c(raw) if kind == "temp" else raw))

                res_file = SESSION_DIR / "_proc_settings.txt"
                try:
                    res_file.unlink()
                except Exception:
                    pass

                ids = ",".join("{0}:{1}".format(tc, k) for tc, k in order)
                hta = """<html><head><title>Process Settings</title>
<HTA:APPLICATION ID="pf" SCROLL="no" SYSMENU="yes" BORDER="dialog"
 CAPTION="yes" SHOWINTASKBAR="yes" INNERBORDER="no"/>
<style>
body{font:9pt "Segoe UI";background:#f0f0f0;margin:14px}
h3{margin:0 0 2px 0;font-size:11pt}
p.s{margin:0 0 12px 0;color:#555}
table{border-collapse:collapse;width:100%%}
td{padding:4px 6px}
td.l{width:210px}
td.u{width:30px;color:#555}
input,select{width:210px;font:9pt "Segoe UI"}
.b{margin-top:16px;text-align:right}
button{width:86px;height:26px;margin-left:6px;font:9pt "Segoe UI"}
</style></head><body>
<h3>Process Settings</h3>
<p class="s">Sequence: %(seq)s &nbsp;|&nbsp; Leave values unchanged to keep the current defaults.</p>
<table>%(rows)s</table>
<div class="b">
<button onclick="save()">OK</button><button onclick="cancel()">Cancel</button></div>
<script language="VBScript">
Dim ids
ids = "%(ids)s"
Sub save()
  Dim fso,f,parts,i,p,el,out
  Set fso = CreateObject("Scripting.FileSystemObject")
  Set f = fso.CreateTextFile("%(res)s", True)
  parts = Split(ids, ",")
  For i = 0 To UBound(parts)
    p = Split(parts(i), ":")
    Set el = document.getElementById("f" & p(0))
    f.WriteLine p(0) & "=" & p(1) & "=" & el.value
  Next
  f.Close
  window.close
End Sub
Sub cancel()
  window.close
End Sub
</script></body></html>""" % {
                    "seq": str(seq_name or ""),
                    "rows": "".join(rows),
                    "ids": ids,
                    "res": str(res_file).replace("\\", "\\\\"),
                }

                hta_path = SESSION_DIR / "_proc_settings.hta"
                hta_path.write_text(hta, encoding="utf-8")
                import subprocess
                try:
                    subprocess.run(["mshta", str(hta_path)], timeout=1800)
                except Exception as e:
                    log("Could not show the Process Settings form: {0}".format(e))
                    return False
                finally:
                    try:
                        hta_path.unlink()
                    except Exception:
                        pass

                if not res_file.exists():
                    log("Process Settings cancelled — keeping all current values.")
                    return True

                # Write ONLY what actually changed.
                changes = {}
                try:
                    for line in res_file.read_text(encoding="utf-8").splitlines():
                        bits = line.strip().split("=")
                        if len(bits) != 3:
                            continue
                        tc, kind, val = int(bits[0]), bits[1], bits[2].strip()
                        if val == "":
                            continue
                        new = c_to_k(float(val)) if kind == "temp" else float(val)
                        if abs(new - current.get(tc, new)) > 1e-6:
                            changes[tc] = new
                except Exception as e:
                    log("Could not parse the Process Settings form: {0}".format(e))
                    return True
                finally:
                    try:
                        res_file.unlink()
                    except Exception:
                        pass

                if not changes:
                    log("Process Settings: nothing changed — no property writes performed.")
                    return True

                applied = []
                for tc, new in changes.items():
                    try:
                        arr = sy.CreateDoubleArray()
                        arr.AddDouble(float(new))
                        prop.SetFieldValues(tc, arr)
                        applied.append(tc)
                        log("  Set {0} ({1}) = {2}".format(PROC_FIELDS[tc][0], tc, new))
                    except Exception as e:
                        log("  Failed to set {0} ({1}): {2}".format(PROC_FIELDS[tc][0], tc, e))
                try:
                    pe.CommitChanges("Edit")
                except Exception as e:
                    log("CommitChanges for process settings failed: {0}".format(e))

                # Verify against a FRESH property object — a stale COM handle
                # reads 0.0 after commit, and a SetFieldValues "success" on its
                # own proves nothing (the TCode is not validated against the
                # property type).
                try:
                    _pe2, prop2 = resolve_process_controller()
                    if prop2 is not None:
                        back = read_proc_values(prop2, applied)
                        for tc in applied:
                            want, got = changes[tc], back.get(tc)
                            kind = PROC_FIELDS[tc][1]
                            if got is None:
                                log("  VERIFY {0} ({1}): field not readable back.".format(
                                    PROC_FIELDS[tc][0], tc))
                            elif abs(got - want) > 1e-3:
                                log("  VERIFY FAILED {0} ({1}): wrote {2}, reads {3}.".format(
                                    PROC_FIELDS[tc][0], tc, want, got))
                            else:
                                log("  VERIFY OK {0} ({1}) = {2}{3}".format(
                                    PROC_FIELDS[tc][0], tc,
                                    k_to_c(got) if kind == "temp" else got,
                                    " degC" if kind == "temp" else ""))
                except Exception as e:
                    log("Process settings verification failed: {0}".format(e))
                return True
            except Exception as e:
                log("Process Settings form failed: {0}".format(e))
                return False

        choice = ask_yes_no(
            "Process Settings",
            "Do you want to review or modify the Process Settings before continuing?\n\n"
            "Yes  -  Open the Process Settings so you can review or change the values.\n"
            "No   -  Continue using the current Moldflow default settings."
        )
        
        if choice == IDYES:
            log("Opening Process Parameters dialogue...")
            # SAFETY CONTRACT for this block (the analysis has been broken here
            # before): it is PURELY ADDITIVE. Nothing downstream changes, and
            # ONLY fields the user actually edited are ever written — an
            # untouched form performs ZERO property writes, so a run where the
            # user just clicks OK is byte-identical to the previous behaviour.
            # Any failure falls back to the old "adjust it yourself" message.
            opened = show_process_settings_form()
            if not opened:
                show_dialog(
                    "Process Parameters",
                    "Please open Process Settings from the Synergy Study Properties panel.\n\n"
                    "Click OK when you have finished adjusting the parameters."
                )
        else:
            log("Bypassing Process Parameters dialogue (using defaults).")
            
        # -----------------------------------------
        # Helper function to count nodes (defined for gate placement and meshing)
        # -----------------------------------------
        def mesh_node_count():
            """Number of mesh nodes in the study (0 while unmeshed)."""
            try:
                pred = sy.PredicateManager().CreateLabelPredicate("N1:")
                ents = study_doc.CreateEntityList()
                ents.SelectFromPredicate(pred)
                return int(ents.Size() or 0)
            except Exception as ce:
                _exit_if_com_dead(ce, "node count")
                log("Node count query failed: {0}".format(ce))
                return 0

        def resolve_out_path():
            """Best-effort path to the study's solver .out log.

            Defined here rather than in Phase 4 because the Gate Location run
            that picks the injection location needs it too, and that happens
            before the main solve."""
            try:
                sp = study_doc.StudyPath()
                if sp:
                    return Path(sp).with_suffix(".out")
            except Exception:
                pass
            try:
                name = study_doc.StudyName()
                proj_path = sy.Project().Path()
                if proj_path and name:
                    return Path(proj_path) / "{0}.out".format(
                        str(name).replace(".sdy", ""))
            except Exception:
                pass
            return None

        def scan_out_errors(out_path, since_mtime=0.0):
            """Joined ERROR/license lines from a FRESH .out (modified at/after
            since_mtime), or None. The freshness gate avoids reading a stale
            error from a previous run and aborting a healthy solve.

            Hoisted here with resolve_out_path() for the Gate Location run."""
            if not out_path:
                return None
            try:
                p = Path(out_path)
                if not p.exists():
                    return None
                if since_mtime and p.stat().st_mtime < since_mtime - 2:
                    return None
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return None
            low = content.lower()
            if "** error" in low or "license is not available" in low:
                lines = [ln.strip() for ln in content.splitlines()
                         if "ERROR" in ln or "license" in ln.lower()]
                return "\n".join(lines) if lines else None
            return None

        meshed = mesh_node_count() >= MIN_MESH_NODES

        # -----------------------------------------
        # Gate (injection location) placement
        # -----------------------------------------
        # NOTE: there was an `import re` here.  It was redundant (re is
        # imported at module scope) and actively harmful: a mid-function
        # import binds the name as a LOCAL for the WHOLE function, so any
        # closure defined earlier that touched `re` raised "cannot access
        # free variable 're'".  That silently swallowed a successful
        # analysis-sequence assignment.  Do not re-add it.

        def count_injection_locations():
            """Count injection-location boundary conditions.

            There is no StudyDoc.GetInjectionLocationCount() in the Synergy
            API. Gates are NDBC entities carrying a property of type 40000
            (thermoplastic) or 40002 (reactive), so select them with a
            property-type predicate and count the result.
            """
            total = 0
            for tset_id in (40000, 40002):
                try:
                    pred = sy.PredicateManager().CreatePropTypePredicate(tset_id)
                    ents = study_doc.CreateEntityList()
                    ents.SelectFromPredicate(pred)
                    size = ents.Size()
                    if size:
                        total += int(size)
                except Exception as ce:
                    _exit_if_com_dead(ce, "gate count")
                    log("Gate count query (type {0}) failed: {1}".format(tset_id, ce))
            return total

        def gate_prop_candidates():
            """Property to attach to the gate, best first.

            The FIRST candidate is a true VB `Nothing` (null IDispatch), which
            is what Autodesk's own injpts_3d.vbs passes and what makes Synergy
            create/select the right injection property itself. Passing bare
            Python `None` here instead raised "Type mismatch" on argument 4
            and quietly pushed every run onto the explicit-property fallback,
            which produced a boundary condition the 40000 predicate could not
            see. Bare None is kept LAST purely as a belt-and-braces retry."""
            candidates = []
            nothing = com_nothing()
            if nothing is not None:
                candidates.append(nothing)
            try:
                p = sy.PropertyEditor().GetFirstProperty(injection_id)
                if p is not None:
                    candidates.append(p)
            except Exception:
                pass
            try:
                p = sy.PropertyEditor().CreateProperty(injection_id, 1, True)
                if p is not None:
                    candidates.append(p)
            except Exception:
                pass
            candidates.append(None)
            return candidates

        def describe_gate_prop(prop):
            """Short label for the log, so a failed run says which form of the
            property argument was tried."""
            if prop is None:
                return "python-None"
            if type(prop).__name__ == "VARIANT":
                return "VB-Nothing"
            return "explicit-property"

        def active_layer_name():
            """Name of the currently active layer, for the log. Autodesk's
            injpts_3d.vbs creates and activates an 'Injection Locations' layer
            before creating gates, and an entity on an inactive layer is
            excluded from solver input -- so it is worth recording."""
            try:
                lm = sy.LayerManager()
                lyr = lm.GetActivated()
                if lyr is None:
                    return "(none active)"
                return str(lm.GetName(lyr) or "(unnamed)")
            except Exception as ce:
                return "(unavailable: {0})".format(ce)

        def create_gate_on_node(node_label):
            """Attach an injection location to an existing mesh node, so the
            gate is guaranteed to sit on the body."""
            # Normalize to the 'N123' label form Synergy's SelectFromString
            # expects — the coordinate-snap path passes a bare int, which
            # selects nothing on its own.
            digits = str(node_label).lstrip("Nn")
            sel_label = "N{0}".format(digits) if digits.isdigit() else str(node_label)
            bc = sy.BoundaryConditions()
            nodes = bc.CreateEntityList()
            nodes.SelectFromString("{0} ".format(sel_label))
            try:
                if int(nodes.Size() or 0) < 1:
                    log("Node {0} could not be selected.".format(sel_label))
                    return False
            except Exception:
                pass
            nx, ny, nz = gate_normal_for(node_label)
            normal = sy.CreateVector()
            normal.SetXYZ(nx, ny, nz)
            last_error = None
            before = count_injection_locations()

            # A non-None return from CreateNDBC is NOT proof of a gate. The
            # first candidate is None ("let Synergy pick the property"), and
            # that path can produce a boundary condition with NO injection
            # property attached -- it returns an EntList, counts as nothing,
            # and the solver later rejects the study with ERROR 301385. So
            # each attempt is verified by the gate count actually rising, and
            # the next candidate is tried when it does not.
            log("Creating gate on node {0}; active layer: {1}.".format(
                sel_label, active_layer_name()))
            for prop in gate_prop_candidates():
                kind = describe_gate_prop(prop)
                try:
                    ndbc = bc.CreateNDBC(nodes, normal, injection_id, prop)
                except Exception as ce:
                    last_error = ce
                    log("  CreateNDBC({0}) raised: {1}".format(kind, ce))
                    continue
                if ndbc is None:
                    log("  CreateNDBC({0}) returned nothing.".format(kind))
                    continue
                # NOTE: no sy.Build() here. Synergy.Build is a read-only
                # PROPERTY returning the product version string, not a
                # database refresh -- the "Refreshed Synergy database with
                # Build()" calls elsewhere in this file do nothing. The
                # predicate query below goes to Synergy live, so it already
                # sees the new entity.
                if count_injection_locations() > before:
                    log("  CreateNDBC({0}) created the gate.".format(kind))
                    return True
                created = ""
                try:
                    created = str(ndbc.ConvertToString() or "").strip()
                except Exception:
                    pass
                log("  CreateNDBC({0}) returned entity '{1}' but the gate "
                    "count did not rise; trying the next candidate.".format(
                        kind, created or "?"))
            log("CreateNDBC on node {0} failed: {1}".format(
                node_label, last_error or "no candidate produced a gate"))
            return False

        def create_gate_at_xyz(x, y, z):
            """Free-position gate via CreateNDBCAtXYZ — valid PRE-MESH only
            (it creates a new node at that exact location)."""
            bc = sy.BoundaryConditions()
            coord = sy.CreateVector()
            coord.SetXYZ(float(x), float(y), float(z))
            normal = sy.CreateVector()
            normal.SetXYZ(0.0, 0.0, 1.0)
            last_error = None
            for prop in gate_prop_candidates():
                try:
                    ndbc = bc.CreateNDBCAtXYZ(coord, normal, injection_id, prop)
                    if ndbc is not None:
                        return True
                except Exception as ce:
                    last_error = ce
            log("CreateNDBCAtXYZ({0}, {1}, {2}) failed: {3}".format(x, y, z, last_error))
            return False

        _mesh_nodes_cache = {}
        _mesh_node_normals = {}  # int label -> unit OUTWARD surface normal

        def get_surface_nodes_display():
            """label -> (x, y, z) of the mesh SURFACE nodes in display units.
            One bulk UDM export + parse (per-node COM calls are far too slow).
            ExportModel writes meters; scale to the display unit.
            Also fills _mesh_node_normals (outward face normals, direction only,
            so no unit scaling) for correct 3D injection direction."""
            if _mesh_nodes_cache:
                return _mesh_nodes_cache
            udm = SESSION_DIR / "_gate_snap.udm"
            try:
                from mesh_geometry import (parse_udm, boundary_node_labels,
                                           boundary_node_normals,
                                           triangle_node_normals)
                sy.Project().ExportModel(str(udm))
                coords, tets, tris, _bbox = parse_udm(str(udm))
                units = str(sy.GetUnits())
                f = 1000.0 / 25.4 if units.lower().startswith("eng") else 1000.0
                labels = boundary_node_labels(tets) if tets else set(coords)
                for lbl in labels:
                    if lbl in coords:
                        x, y, z = coords[lbl]
                        _mesh_nodes_cache[lbl] = (x * f, y * f, z * f)
                if tets:
                    _mesh_node_normals.update(boundary_node_normals(coords, tets))
                elif tris:
                    # Dual Domain / midplane: no tets, so the tet-based
                    # construction yields nothing and every gate used the +Z
                    # fallback -- the cause of the injection cone sitting at an
                    # angle to the surface instead of standing on it.
                    _mesh_node_normals.update(triangle_node_normals(coords, tris))
                log("Loaded {0} surface node coordinates ({1} with normals) "
                    "for gate placement.".format(
                        len(_mesh_nodes_cache), len(_mesh_node_normals)))
            except Exception as ce:
                log("Mesh node export/parse failed: {0}".format(ce))
            finally:
                try:
                    udm.unlink()
                except Exception:
                    pass
            return _mesh_nodes_cache

        def gate_normal_for(node_label):
            """Unit OUTWARD surface normal for a node label (int or 'N123').
            The 3D solver silently drops an injection location whose direction
            is parallel to the surface, so the direction must point out of the
            part. Falls back to +Z only when no mesh normal is available
            (e.g. Dual Domain / midplane, where the normal is not used)."""
            if not _mesh_node_normals:
                get_surface_nodes_display()  # populates both caches from one export
            try:
                nid = int(str(node_label).lstrip("Nn"))
            except (TypeError, ValueError):
                nid = None
            n = _mesh_node_normals.get(nid) if nid is not None else None
            if n is None:
                log("No outward normal for node {0}; using +Z fallback.".format(node_label))
                return (0.0, 0.0, 1.0)
            log("Gate node {0} outward normal: ({1:.3f}, {2:.3f}, {3:.3f})".format(
                node_label, n[0], n[1], n[2]))
            return n

        def nearest_surface_node(x, y, z):
            """(label, (nx, ny, nz), distance) of the surface node closest to
            the given display-unit point, or None if node data is missing."""
            nodes = get_surface_nodes_display()
            if not nodes:
                return None
            best_lbl = None
            best_xyz = None
            best_d2 = None
            for lbl, (nx, ny, nz) in nodes.items():
                d2 = (nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2
                if best_d2 is None or d2 < best_d2:
                    best_lbl, best_xyz, best_d2 = lbl, (nx, ny, nz), d2
            return best_lbl, best_xyz, best_d2 ** 0.5

        def selected_node_labels():
            """Mesh-node labels (e.g. N123) currently selected in Synergy.
            NBC/other labels also start with N, so match N + digits only."""
            try:
                sel = study_doc.Selection()
                if sel is None:
                    return []
                text = str(sel.ConvertToString() or "")
            except Exception as ce:
                _exit_if_com_dead(ce, "selection poll")
                # Synergy can reject COM calls while the user is mid-click;
                # just report nothing selected and poll again.
                return []
            return [t for t in text.split() if re.match(r"^N\d+$", t)]

        def prompt_gate_coordinates(unit_name):
            """Ask for X,Y,Z gate coordinates via an InputBox.
            Returns (x, y, z) floats, or None if cancelled/invalid."""
            import subprocess as sp
            vbs_gate = SESSION_DIR / "_gate_coord.vbs"
            vbs_gate.write_text(
                'Dim r\n'
                'r = InputBox("Enter the gate position as X,Y,Z in {0}." '
                '& vbCrLf & "The gate will snap to the nearest point ON the part." '
                '& vbCrLf & vbCrLf & "Example: 10, 25, 0", '
                '"Gate Location", "0, 0, 0")\n'
                'WScript.Echo r\n'.format(unit_name),
                encoding="utf-8",
            )
            try:
                proc = sp.run(
                    ["cscript", "//Nologo", str(vbs_gate)],
                    capture_output=True, text=True, timeout=300
                )
                raw = proc.stdout.strip()
                log("Gate coordinate input: '{0}'".format(raw))
                parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
                if len(parts) != 3:
                    return None
                return tuple(float(p) for p in parts)
            except Exception as ce:
                log("Gate coordinate input failed: {0}".format(ce))
                return None
            finally:
                try:
                    vbs_gate.unlink()
                except Exception:
                    pass

        def wait_for_manual_gate():
            """Native-tool path: instruct, then poll until a gate appears."""
            show_dialog(
                "Gate Location Setup",
                "1. In Synergy, open the Home tab.\n"
                "2. Click 'Boundary Conditions' > 'Injection Locations'.\n"
                "3. Click on the model where the gate should be placed\n"
                "    (a cone marker appears at the gate).\n\n"
                "Click OK now — the script waits in the background and\n"
                "continues automatically as soon as it detects the gate."
            )
            log("Waiting for the user to place an injection location...")
            start = time.time()
            while time.time() - start < 600:
                n = count_injection_locations()
                if n > 0:
                    log("Gate location detected ({0} gate(s)).".format(n))
                    return n
                time.sleep(3)
            log("No gate detected within 10 minutes; asking again.")
            return 0

        unit_name = "inches" if str(sy.GetUnits()).lower().startswith("eng") else "mm"
        MB_YESNOCANCEL = 0x03
        IDCANCEL = 2

        gate_count = count_injection_locations()
        log("Existing injection location count: {0}".format(gate_count))

        # ------------------------------------------------------------------
        # Dual Domain: offer to let MOLDFLOW choose the gate
        # ------------------------------------------------------------------
        # Moldflow's own "Gate Location" analysis sequence searches every
        # candidate node on a Dual Domain mesh and reports the best one(s).
        # When the user takes that offer we do NOT ask them to place a gate
        # here -- the mesh does not exist yet, and Gate Location needs one.
        # Instead the placement is DEFERRED to just after mesh diagnostics
        # pass, where run_auto_gate_location() runs the analysis, reads the
        # recommended node out of the solver log and places the gate on it.
        auto_gate_pending = False
        if is_dual_domain and gate_count < 1:
            try:
                import ui_bridge
                ans = ui_bridge.prompt_user(
                    "Gate Location — Automatic or Manual?",
                    "This is a Dual Domain study, so Moldflow can work out the "
                    "best gate location itself.\n"
                    "• Let Moldflow Decide: runs the Gate Location analysis "
                    "after meshing and places the gate on the node it "
                    "recommends. No picking required.\n"
                    "• Place It Myself: choose the gate position now, as usual.",
                    options=["Let Moldflow Decide", "Place It Myself"]
                )
                auto_gate_pending = (ans == "Let Moldflow Decide")
            except Exception as e_ag:
                log("Auto-gate prompt unavailable ({0}); falling back to the "
                    "yes/no dialog.".format(e_ag))
                auto_gate_pending = ask_yes_no(
                    "Gate Location — Automatic or Manual?",
                    "This is a Dual Domain study, so Moldflow can work out the\n"
                    "best gate location itself.\n\n"
                    "YES  -  Let Moldflow decide: the Gate Location analysis\n"
                    "            runs after meshing and the gate is placed on\n"
                    "            the node it recommends.\n\n"
                    "NO   -  Place the gate yourself, as usual."
                ) == IDYES
            log("Dual Domain automatic gate location: {0}".format(
                "ENABLED — deferring gate placement until after meshing"
                if auto_gate_pending else "declined; placing the gate manually"))

        # The gate location is otherwise the one unavoidable manual step. The
        # guidance that used to be a separate OK-only popup is now folded into
        # the method dialogs below, so choosing a method takes the user straight
        # to the action instead of through an extra box.
        while gate_count < 1 and not auto_gate_pending:
            if meshed:
                try:
                    import ui_bridge
                    ans = ui_bridge.prompt_user(
                        "Select the Gate Location",
                        "Choose how you want to place the gate location on the model:\n"
                        "• Pick on Model: Click a point on the part to place automatically.\n"
                        "• Enter Coordinates: Type X,Y,Z coordinates.\n"
                        "• Synergy Tool: Place manually using Synergy's tool.",
                        options=["Pick on Model", "Enter Coordinates", "Synergy Tool", "Cancel"]
                    )
                    if ans == "Pick on Model":
                        choice = IDYES
                    elif ans == "Enter Coordinates":
                        choice = IDNO
                    elif ans == "Synergy Tool":
                        choice = IDCANCEL
                    else:
                        choice = IDCANCEL
                except Exception:
                    choice = ctypes.windll.user32.MessageBoxW(
                        None,
                        "Please select the gate location on the model.\n"
                        "Choose how you want to place it:\n\n"
                        "YES   -   Pick on the model: click a point on the part and the\n"
                        "               gate is placed there automatically.\n\n"
                        "NO    -   Type X,Y,Z coordinates; the gate snaps to the\n"
                        "               nearest point on the part surface.\n\n"
                        "CANCEL - Place it yourself with Synergy's own tool\n"
                        "               (Home > Boundary Conditions > Injection Locations).\n\n"
                        "Once a gate is set, the automation continues automatically with\n"
                        "meshing, mesh diagnostics, analysis and report generation.",
                        "Select the Gate Location",
                        MB_YESNOCANCEL | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND,
                    )
                if choice == IDYES:
                    # --- Pick a point (node) on the model ---
                    baseline = set(selected_node_labels())
                    show_dialog(
                        "Pick the Gate Location",
                        "Click OK, then click the point on the model where the\n"
                        "gate should go.\n\n"
                        "The script watches your selection and places the gate\n"
                        "there automatically — no need to open any tool. (A click\n"
                        "that selects a face or element works too; it resolves to\n"
                        "the nearest node.)"
                    )
                    log("Waiting for the user to select a mesh node...")
                    picked = None
                    pick_start = time.time()
                    while time.time() - pick_start < 600:
                        labels = selected_node_labels()
                        fresh = [l for l in labels if l not in baseline]
                        if fresh:
                            picked = fresh[0]
                            break
                        if labels and not baseline:
                            picked = labels[0]
                            break
                        time.sleep(2)
                    if picked is None:
                        log("No node selected within 10 minutes; asking again.")
                        continue
                    log("User selected node {0}.".format(picked))
                    if create_gate_on_node(picked):
                        gate_count = max(count_injection_locations(), 1)
                        log("Gate placed on node {0}.".format(picked))
                        show_dialog(
                            "Gate Location Setup",
                            "Gate placed on the selected point (node {0}).".format(picked)
                        )
                    else:
                        show_dialog(
                            "Gate Location Setup",
                            "Could not attach the gate to node {0}.\n"
                            "Please try again.".format(picked)
                        )
                elif choice == IDCANCEL:
                    gate_count = wait_for_manual_gate()
                else:
                    # --- Typed coordinates, snapped onto the part surface ---
                    coords_in = prompt_gate_coordinates(unit_name)
                    if coords_in is None:
                        log("Coordinate entry cancelled or invalid; asking again.")
                        show_dialog(
                            "Gate Location Setup",
                            "No valid coordinates were entered.\n\n"
                            "Please enter three numbers separated by commas,\n"
                            "e.g. 10, 25, 0."
                        )
                        continue
                    snap = nearest_surface_node(*coords_in)
                    if snap is None:
                        log("No mesh node data available for snapping.")
                        show_dialog(
                            "Gate Location Setup",
                            "Could not read the mesh to snap your point onto the\n"
                            "part. Please place the gate manually instead."
                        )
                        gate_count = wait_for_manual_gate()
                        continue
                    lbl, (nx, ny, nz), dist = snap
                    log("Typed point ({0}, {1}, {2}) {3} snapped to node {4} at "
                        "({5:.2f}, {6:.2f}, {7:.2f}), {8:.2f} {3} away.".format(
                            coords_in[0], coords_in[1], coords_in[2], unit_name,
                            lbl, nx, ny, nz, dist))
                    if create_gate_on_node(lbl):
                        gate_count = max(count_injection_locations(), 1)
                        show_dialog(
                            "Gate Location Setup",
                            "Gate placed ON the part surface at\n"
                            "({0:.2f}, {1:.2f}, {2:.2f}) {3}\n"
                            "— the nearest surface point, {4:.2f} {3} from the\n"
                            "coordinates you typed.".format(nx, ny, nz, unit_name, dist)
                        )
                    else:
                        show_dialog(
                            "Gate Location Setup",
                            "Could not create the gate at the snapped point.\n"
                            "Please try again or place it manually."
                        )
            else:
                # Mesh unavailable: native tool, or exact-XYZ creation which
                # is only valid pre-mesh.
                choice = ask_yes_no(
                    "Select the Gate Location",
                    "Please select the gate location on the model.\n"
                    "(The model is not meshed yet, so choose one of these.)\n\n"
                    "YES  -  Place it yourself in Synergy:\n"
                    "            Home tab > Boundary Conditions > Injection Locations,\n"
                    "            then click the point on the model.\n\n"
                    "NO   -  Type exact X,Y,Z coordinates ON the part surface.\n\n"
                    "Once a gate is set, the automation continues automatically with\n"
                    "meshing, mesh diagnostics, analysis and report generation."
                )
                if choice == IDYES:
                    gate_count = wait_for_manual_gate()
                else:
                    coords_in = prompt_gate_coordinates(unit_name)
                    if coords_in is None:
                        log("Coordinate entry cancelled or invalid; asking again.")
                        continue
                    if create_gate_at_xyz(*coords_in):
                        log("Injection location created at ({0}, {1}, {2}).".format(*coords_in))
                        gate_count = max(count_injection_locations(), 1)
                    else:
                        show_dialog(
                            "Gate Location Setup",
                            "Could not create the injection location at the given\n"
                            "coordinates. Make sure the point lies on the part\n"
                            "surface, then try again."
                        )

        if auto_gate_pending:
            log("Gate placement deferred to the Gate Location analysis.")
        else:
            log("Gate placement confirmed ({0} gate(s)).".format(gate_count))

        # ----------------------------------------
        # Auto-Mesh AFTER the gate is placed
        # -----------------------------------------
        log("Auto-meshing model...")
        node_count = mesh_node_count()
        mesh_error = None
        mstatus = "Existing mesh"
        ok_mesh = node_count >= MIN_MESH_NODES

        def confirm_node_count(attempts=4, pause=3.0):
            """Re-read the node count when a 'Completed' mesh reports an
            implausible one, and say so in the log if the server is dead.

            Two different things produce a low count here: the node table
            lagging the status by a poll or two (recoverable — retrying
            picks up the real figure), and the COM server dropping out
            mid-marshal (not recoverable — every subsequent call fails).
            Probing Synergy directly tells them apart, so a genuinely slow
            mesh is not misreported as a crash."""
            best = 0
            for i in range(attempts):
                try:
                    # Cheap round-trip: if the server is gone this raises
                    # rather than quietly returning a bogus figure.
                    sy.Build()
                except Exception as exc:
                    if _com_is_dead(exc):
                        log("  Synergy connection is dead while confirming "
                            "the node count ({0}).".format(exc))
                        return best
                nc = mesh_node_count()
                best = max(best, nc)
                if nc >= MIN_MESH_NODES:
                    return nc
                log("  Node count {0} below the {1}-node floor "
                    "(attempt {2}/{3}); re-probing...".format(
                        nc, MIN_MESH_NODES, i + 1, attempts))
                if i < attempts - 1:
                    time.sleep(pause)
            return best

        def wait_for_mesh(timeout=1800.0, poll=3.0, require_status=False):
            """Poll until the mesh job finishes. MeshNow / Generate QUEUE
            the mesh in the Analysis Manager and return immediately, so the
            nodes appear only after the mesher completes — checking the node
            count once (or for a short fixed window) races the mesher and
            falsely reports 'no nodes'. Success = MeshStatus 'Completed' or
            nodes present; failure = a terminal failure status or a lost
            connection; else timeout. Returns (ok, node_count, status).

            require_status=True is used when REMESHING an already-meshed
            model (refinement / re-placed gate): the OLD nodes are still
            present while the new job runs, so the node-count shortcut would
            return instantly; instead wait for the mesher status to complete
            (with a short grace period in case the job finished before the
            first poll)."""
            start = time.time()
            dead = 0
            last = None
            seen_active = False
            while time.time() - start < timeout:
                st = ""
                try:
                    st = str(study_doc.MeshStatus() or "")
                    dead = 0
                except Exception as exc:
                    if _com_is_dead(exc):
                        dead += 1
                        if dead >= 3:
                            return False, mesh_node_count(), "Synergy connection lost"
                if st and st != last:
                    log("Mesh status: {0}".format(st))
                    last = st
                low = st.lower()
                if low and "complet" not in low and not any(
                        k in low for k in ("fail", "cancel", "abort", "error")):
                    seen_active = True
                nc = mesh_node_count()
                if not require_status and nc >= MIN_MESH_NODES:
                    return True, nc, st or "Completed"
                if "complet" in low:
                    if require_status and not seen_active and time.time() - start < 15.0:
                        # Remesh: the status may still read 'Completed' from
                        # the PREVIOUS mesh; give the new job a moment to start.
                        time.sleep(poll)
                        continue
                    # The mesher says it finished but the node count is not
                    # plausible for a real mesh.  Do NOT treat the status
                    # alone as success: that is the path that previously
                    # returned nodes=1 as 'Completed' and let the workflow
                    # drive a solve against a half-dead COM server.  The
                    # count can also legitimately lag the status by a poll
                    # or two, so re-probe before deciding.
                    confirmed = confirm_node_count()
                    if confirmed >= MIN_MESH_NODES:
                        return True, confirmed, st or "Completed"
                    return False, confirmed, (
                        "mesher reported '{0}' but only {1} node(s) — "
                        "Synergy connection suspect".format(st or "Completed", confirmed)
                    )
                if any(k in low for k in ("fail", "cancel", "abort", "error")):
                    return False, nc, st
                time.sleep(poll)
            return False, mesh_node_count(), last or "timeout"

        def generate_mesh(force=False, set_default_options=True):
            """Trigger automatic meshing and wait for the result.

            The meshing calls are the ORIGINAL inline logic, wrapped so the
            diagnostics/recovery stages can re-run them.  force=True remeshes
            even when a mesh already exists (refinement / re-placed gate);
            set_default_options=False preserves options a refinement has just
            written.  Updates ok_mesh / node_count / mstatus / mesh_error."""
            nonlocal ok_mesh, node_count, mstatus, mesh_error
            mesh_error = None
            if not force:
                nc = mesh_node_count()
                if nc >= MIN_MESH_NODES:
                    ok_mesh, node_count, mstatus = True, nc, "Existing mesh"
                    log("Model is already meshed ({0} nodes).".format(nc))
                    return True
            ok_mesh = False
            node_count = 0
            mstatus = "unknown"

            # Meshing is the one long stage the user actually wants to WATCH:
            # Synergy's own mesher log and progress run in the window the panel
            # is docked over, and the panel has nothing to show while it runs.
            # Fold it to its header for the duration and open it again below --
            # this process and the panel both keep running throughout, so the
            # mesh-diagnostics card that follows is unaffected either way.
            _panel_folded = fold_panel(log, "mesh generation running")

            try:
                mesh_gen = sy.MeshGenerator()
                log("MeshGenerator created. Setting options...")
                if set_default_options:
                    try:
                        mesh_gen.UseAutoSize = True
                        mesh_gen.MeshComponentType = 0
                        mesh_gen.SourceGeomType = "Auto-Detect"
                        mesh_gen.Mesher3D = "AdvancingFront"
                        mesh_gen.SaveOptions()
                        log("MeshGenerator options saved: UseAutoSize=True, MeshComponentType=0, "
                            "SourceGeomType='Auto-Detect', Mesher3D='AdvancingFront'")
                    except Exception as eopts:
                        log("Warning: Failed to set MeshGenerator options: {0}".format(eopts))

                try:
                    sy.Build()
                    log("Build() completed before meshing.")
                except Exception:
                    pass

                # 1) Queue the mesh with MeshNow, then wait for it to finish.
                log("Calling study_doc.MeshNow(False) (queues the mesh job)...")
                try:
                    result = study_doc.MeshNow(False)
                    log("study_doc.MeshNow(False) returned: {0}".format(result))
                except Exception as me:
                    mesh_error = str(me)
                    log("MeshNow raised: {0}".format(me))

                log("Waiting for the mesher to finish (3D meshing may take several minutes)...")
                ok_mesh, node_count, mstatus = wait_for_mesh(require_status=force)
                log("Mesh wait finished: nodes={0}, status='{1}'.".format(node_count, mstatus))

                # 2) If MeshNow produced nothing, fall back to Generate() and wait again.
                if not ok_mesh and node_count < MIN_MESH_NODES:
                    log("MeshNow produced no nodes; trying MeshGenerator.Generate()...")
                    try:
                        result = mesh_gen.Generate()
                        log("MeshGenerator.Generate() returned: {0}".format(result))
                    except Exception as ge:
                        mesh_error = str(ge)
                        log("MeshGenerator.Generate() raised: {0}".format(ge))
                    ok_mesh, node_count, mstatus = wait_for_mesh(require_status=force)
                    log("Mesh wait (after Generate) finished: nodes={0}, status='{1}'.".format(
                        node_count, mstatus))
            except Exception as me:
                mesh_error = str(me)
                log("Error triggering mesh: {0}".format(me))
            finally:
                # Unconditional: a mesh that fails or throws is exactly when the
                # user needs the panel back -- either to read the mesh
                # diagnostics card or to answer the failure-recovery one.
                unfold_panel(_panel_folded, log, "mesh generation finished")

            if node_count >= MIN_MESH_NODES:
                ok_mesh = True
                log("Meshing finished: {0} nodes.".format(node_count))
            else:
                ok_mesh = False
                log("WARNING: usable mesh not detected after meshing — {0} node(s), "
                    "status '{1}'.".format(node_count, mstatus))
                if mesh_error:
                    log("Mesh error was: {0}".format(mesh_error))
            return ok_mesh

        def apply_mesh_refinement(factor=0.5):
            """Apply a 50% finer mesh via the API (or its closest supported
            equivalent) and mark the model for a full remesh.  Prefers halving
            MeshGenerator.EdgeLength; falls back to scaling CadAutoSizeScale
            when EdgeLength is not exposed.  Returns True when new options
            were written (the caller then regenerates the mesh)."""
            def read_val(obj, name):
                v = getattr(obj, name)
                return v() if callable(v) else v
            try:
                mesh_gen = sy.MeshGenerator()
            except Exception as e:
                log("MeshGenerator unavailable for refinement: {0}".format(e))
                return False
            applied = False
            try:
                cur = float(read_val(mesh_gen, "EdgeLength") or 0)
                if cur > 0:
                    mesh_gen.UseAutoSize = False
                    mesh_gen.EdgeLength = cur * factor
                    log("Mesh refinement: EdgeLength {0:.4f} -> {1:.4f} "
                        "(UseAutoSize off).".format(cur, cur * factor))
                    applied = True
            except Exception as e:
                log("EdgeLength-based refinement not available: {0}".format(e))
            if not applied:
                # Closest supported equivalent: scale the CAD auto-size down.
                try:
                    scale = float(read_val(mesh_gen, "CadAutoSizeScale") or 0)
                    if scale > 0:
                        mesh_gen.UseAutoSize = True
                        mesh_gen.CadAutoSizeScale = scale * factor
                        log("Mesh refinement: CadAutoSizeScale {0:.4f} -> {1:.4f}.".format(
                            scale, scale * factor))
                        applied = True
                except Exception as e:
                    log("CadAutoSizeScale-based refinement not available: {0}".format(e))
            if applied:
                try:
                    mesh_gen.RemeshAll = True  # replace the existing mesh
                except Exception:
                    pass
                try:
                    mesh_gen.SaveOptions()
                except Exception as e:
                    log("SaveOptions after refinement failed: {0}".format(e))
            return applied

        def refinement_unavailable_dialog():
            show_dialog(
                "Mesh Refinement",
                "This Moldflow API version does not expose the mesh size\n"
                "options needed for automatic 50% refinement.\n\n"
                "Please adjust the mesh size manually (Mesh > Mesh Settings),\n"
                "regenerate the mesh, then choose Retry to re-check it.")

        def return_to_gate_placement():
            """Recovery: let the user delete and re-place the injection
            location with Synergy's native tool, then continue."""
            show_dialog(
                "Gate Placement",
                "To change the gate location:\n\n"
                "1. In Synergy, select the injection-location cone and press Delete.\n"
                "2. Place a new one: Home > Boundary Conditions >\n"
                "    Injection Locations, then click the point on the model.\n\n"
                "Click OK here when the new gate is in place."
            )
            n = count_injection_locations()
            if n < 1:
                n = wait_for_manual_gate()
            log("Gate placement after recovery: {0} gate(s).".format(n))

        def handle_mesh_failure():
            """Meshing produced no usable mesh: show the exact Moldflow error
            and loop over the recovery options until a usable mesh exists or
            the user cancels.  Returns True when a usable mesh exists."""
            while not ok_mesh:
                detail_lines = [
                    "Automatic meshing did not produce a usable mesh.",
                    "",
                    "Nodes found        : {0}".format(node_count),
                    "Last mesh status   : {0}".format(mstatus),
                ]
                if mesh_error:
                    detail_lines.append("Moldflow API error : {0}".format(mesh_error))
                err_row = ""
                if mesh_error:
                    err_row = ('<tr><td>Moldflow error</td><td class="bad">{0}'
                               "</td></tr>".format(_mesh_esc(mesh_error)))
                summary_html = (
                    '<div class="tot bad">Meshing failed &mdash; the workflow '
                    "is stopped.</div>"
                    '<table class="sm">'
                    '<tr><td>Nodes found</td><td class="bad">{0}</td></tr>'
                    '<tr><td>Last mesh status</td><td class="bad">{1}</td></tr>'
                    "{2}</table>".format(node_count, _mesh_esc(mstatus), err_row))
                choice = show_mesh_choice_dialog(
                    "Meshing Failed", "Mesh Generation Error",
                    summary_html, "\n".join(detail_lines),
                    "<b>Choose how to recover.</b><br>(If you mesh manually "
                    "from the Mesh tab first, choose Retry Meshing &mdash; an "
                    "existing mesh is detected and reused.)",
                    [("RETRY", "Retry Meshing"),
                     ("REFINE", "50% Mesh Refinement"),
                     ("GATE", "Return to Gate Placement"),
                     ("CANCEL", "Cancel Workflow")],
                    "CANCEL", log)
                log("Mesh-failure recovery choice: {0}".format(choice))
                if choice == "RETRY":
                    generate_mesh()
                elif choice == "REFINE":
                    if apply_mesh_refinement():
                        generate_mesh(force=True, set_default_options=False)
                    else:
                        refinement_unavailable_dialog()
                elif choice == "GATE":
                    return_to_gate_placement()
                    generate_mesh()
                else:
                    return False
            return True

        def stop_after_mesh_stage(reason):
            log(reason)
            show_dialog(
                "Workflow Stopped",
                "The workflow was stopped before analysis.\n\n"
                "You can mesh or repair the model manually from the Mesh tab,\n"
                "then restart the automation workflow.")

        if ok_mesh:
            log("Model is already meshed ({0} nodes).".format(node_count))
            try:
                import ui_bridge
                ui_bridge.update_state("params", {"mesh_status": f"3D Mesh Ready ({node_count} nodes)"})
            except Exception:
                pass
        else:
            show_dialog(
                "Meshing",
                "The model will now be meshed automatically.\n\n"
                "Click OK to begin.\n\n"
                "NOTE: 3D meshing may take a few minutes.\n"
                "Please wait for the next dialog to appear."
            )
            generate_mesh()
            if not ok_mesh and not handle_mesh_failure():
                stop_after_mesh_stage("User chose to stop workflow after meshing issue.")
                return
        meshed = node_count >= MIN_MESH_NODES

        # -----------------------------------------
        # MESH DIAGNOSTICS (validation gate between meshing and analysis)
        # -----------------------------------------
        log("")
        log("Mesh Diagnostics: validating the mesh before analysis...")
        MAX_AUTO_REFINEMENTS = 3
        refinements_done = 0
        while True:
            diag_report = collect_mesh_diagnostics_when_ready(sy, study_doc, log)
            decision = display_mesh_diagnostics(diag_report, log)

            if decision == "CONTINUE":
                # Passed, or a Warning the user accepted.
                break

            if decision in ("RETRY", "GATE", "REFINE"):
                if decision == "GATE":
                    return_to_gate_placement()
                if decision == "REFINE":
                    if refinements_done >= MAX_AUTO_REFINEMENTS:
                        show_dialog(
                            "Mesh Refinement",
                            "The automatic refinement limit ({0}) was reached.\n"
                            "Please adjust the mesh manually, then choose Retry."
                            .format(MAX_AUTO_REFINEMENTS))
                        continue
                    if not apply_mesh_refinement():
                        refinement_unavailable_dialog()
                        continue
                    refinements_done += 1
                # RETRY re-checks an existing (possibly manually fixed) mesh
                # without destroying it; GATE/REFINE force a full remesh.
                generate_mesh(force=(decision != "RETRY"),
                              set_default_options=False)
                if not ok_mesh and not handle_mesh_failure():
                    stop_after_mesh_stage(
                        "User cancelled during mesh-diagnostics recovery.")
                    return
                continue

            # CANCEL (or the dialog was closed without a choice)
            stop_after_mesh_stage(
                "User cancelled the workflow at the Mesh Diagnostics stage.")
            return

        node_count = mesh_node_count()
        meshed = node_count >= MIN_MESH_NODES

        log("Study is ready to run ({0} nodes).".format(node_count))
        try:
            import ui_bridge
            ui_bridge.update_state("params", {
                "mesh_status": "Meshed & checked ({0} nodes)".format(node_count)})
        except Exception:
            pass

        # -----------------------------------------
        # PHASE 3b: AUTOMATIC GATE LOCATION (Dual Domain)
        # -----------------------------------------
        # Runs HERE, after mesh diagnostics have passed, because the
        # recommendation is only valid for the mesh it was computed on: doing
        # it earlier would let a diagnostics REFINE remesh the part underneath
        # the answer.  The gate goes onto THIS study, on THIS mesh, so the node
        # label the solver reports still identifies the same physical point.
        # The solver log is archived as soon as it is read, because the main
        # solve overwrites the .out and Moldflow deletes the per-job scratch
        # files -- the recommendation would otherwise leave no trace.

        # Tokens for the Gate Location sequence, from the PROC block of
        #   ...\Moldflow Synergy 2027\data\dat\process.dat
        # where sequence 2058 is `Gate3D`.  The display name in the Analysis
        # Sequence dialog is "Gate Location", but that string is NOT the token
        # -- passing it is how the '+' era rejected every candidate and left
        # studies silently on 'Fill'.  Dual Domain uses the plain `Gate` form,
        # so it is tried first here; `Gate3D` covers a 3D mesh.  Verified by
        # readback either way; an unverified assignment is never trusted.
        GATE_LOCATION_TOKENS = ["Gate", "Gate3D"]
        GATE_LOC_TIMEOUT = 2700.0   # 45 min; a real run takes ~1-2 min.
        GATE_LOC_SETTLE = 90.0      # grace after 'Completed' before giving up.

        def set_sequence_token(token):
            """Set the analysis sequence and confirm the readback. True on
            success only -- never assume the assignment landed."""
            try:
                try:
                    study_doc.SetAnalysisSequence(token)
                except Exception:
                    study_doc.AnalysisSequence = token
                verified = str(study_doc.AnalysisSequence() or "").strip()
            except Exception as ce:
                log("Setting sequence to '{0}' failed: {1}".format(token, ce))
                return False
            ok = ("".join(token.split()).lower()
                  == "".join(verified.split()).lower())
            log("Sequence candidate '{0}' -> readback '{1}' ({2}).".format(
                token, verified, "accepted" if ok else "rejected"))
            return ok

        def read_gate_location_logs(out_path, since_mtime):
            """Text of every FRESH solver log for this study.

            The recommendation can land either in the study-level <stem>.out
            or in a per-job <stem>~N.out, so both are read and concatenated.
            The freshness gate matters as much here as in scan_out_errors: a
            previous run's log would otherwise hand back a node chosen for a
            different mesh."""
            if not out_path:
                return ""
            chunks = []
            try:
                base = Path(out_path)
                # Job logs are named <study>~<job>.out -- but the STUDY name
                # can itself carry a ~N suffix (a copy: 'unoteam_study~49.sdy'
                # solves into 'unoteam_study~251.out'). Globbing on the raw
                # stem would look for 'unoteam_study~49~*.out' and find
                # nothing, which is how a finished run looked like a hang.
                stem = re.sub(r"~\d+$", "", base.stem)
                candidates = [base] + sorted(
                    base.parent.glob("{0}~*.out".format(stem)))
                for p in candidates:
                    try:
                        if not p.exists():
                            continue
                        if p.stat().st_mtime < since_mtime - 2:
                            continue
                        chunks.append(p.read_text(encoding="utf-8",
                                                  errors="ignore"))
                    except Exception:
                        continue
            except Exception:
                return ""
            return "\n".join(chunks)

        def wait_for_gate_location(out_path, since_mtime):
            """Poll the solver logs until they name the recommended node(s).

            AnalyzeNow2 only QUEUES the job, so the log is the completion
            signal: the recommendation is the last thing Gate Location writes.
            Returns a list of node labels, empty on failure/timeout."""
            start = time.time()
            last_status = None
            completed_at = None
            # Same stale-status guard as wait_for_results: AnalysisStatus(0)
            # reports the PREVIOUS job until this one registers, so a leftover
            # 'Cancelled' must not abort a gate search that has not begun.
            seen_active = False
            TERMINAL = ("complet", "fail", "cancel", "abort", "error")
            while time.time() - start < GATE_LOC_TIMEOUT:
                text = read_gate_location_logs(out_path, since_mtime)

                nodes = parse_recommended_gate_nodes(text)
                if nodes:
                    log("Gate Location recommends node(s): {0}".format(nodes))
                    return nodes

                err = scan_out_errors(out_path, since_mtime)
                if err:
                    log("Gate Location analysis reported an error:\n{0}".format(err))
                    return []

                try:
                    st = str(study_doc.AnalysisStatus(0) or "")
                except Exception:
                    st = ""
                if st and st != last_status:
                    log("Gate Location status: {0}".format(st))
                    last_status = st
                low = st.lower()
                if st and not any(k in low for k in TERMINAL):
                    seen_active = True
                if seen_active and any(k in low
                                       for k in ("fail", "cancel", "abort",
                                                 "error")):
                    log("Gate Location analysis ended as '{0}'.".format(st))
                    return []

                # Completion bail. Without it a recommendation this code cannot
                # read means sitting out the FULL timeout while Synergy has
                # plainly finished -- which is exactly what a live 12:37 run
                # did, hanging the panel on "Finding the best gate location..."
                # long after the analysis was done. 'Transferring Results' is
                # NOT done, so it does not start the clock.
                if "complet" in low and "transferring" not in low:
                    if completed_at is None:
                        completed_at = time.time()
                        log("Gate Location reported '{0}'; allowing {1:.0f}s "
                            "for the log to be written.".format(
                                st, GATE_LOC_SETTLE))
                    elif time.time() - completed_at > GATE_LOC_SETTLE:
                        log("Gate Location finished but no recommended node "
                            "appeared in the solver log within {0:.0f}s. The "
                            "log may be in a form this build cannot "
                            "read.".format(GATE_LOC_SETTLE))
                        return []
                else:
                    completed_at = None

                time.sleep(5.0)
            log("Gate Location analysis timed out after {0:.0f} minutes.".format(
                GATE_LOC_TIMEOUT / 60.0))
            return []

        def archive_gate_location_log(out_path, since_mtime, nodes):
            """Keep the recommendation.  The main solve overwrites the .out,
            and Moldflow DELETES the per-job ~N.out/.err scratch files shortly
            after a run, so this has to happen now or not at all."""
            try:
                text = read_gate_location_logs(out_path, since_mtime)
                if not text:
                    return
                dest = SESSION_DIR / "gate_location.out"
                dest.write_text(text, encoding="utf-8")
                log("Archived the Gate Location log to {0} "
                    "(recommended node(s): {1}).".format(dest, nodes))
            except Exception as ce:
                log("Could not archive the Gate Location log: {0}".format(ce))

        def choose_recommended_node(nodes):
            """Which recommendation to gate on. Moldflow usually returns one;
            when it returns several the choice is the user's, because they
            are alternatives, not a ranked list to take blindly from."""
            if len(nodes) == 1:
                return nodes[0]
            try:
                import ui_bridge
                labels = ["Node {0}".format(n) for n in nodes]
                ans = ui_bridge.prompt_user(
                    "Moldflow Recommended {0} Gate Locations".format(len(nodes)),
                    "The Gate Location analysis found several suitable gate "
                    "positions. Choose the one to use for this study:",
                    options=labels
                )
                if ans in labels:
                    picked = nodes[labels.index(ans)]
                    log("User chose recommended node {0}.".format(picked))
                    return picked
            except Exception as ce:
                log("Could not ask which recommendation to use ({0}); "
                    "taking the first.".format(ce))
            return nodes[0]

        def remove_injection_locations():
            """Delete every injection location in the study.

            Gate Location aborts outright with ERROR 1204100 "Predefined gates
            detected" if ANY gate exists, and one can appear on its own during
            import/meshing even though the user placed none.  Deleting the
            entities orphans their 40000/40002 property occurrences, so the
            unused ones are purged too."""
            removed = 0
            for tset_id in (40000, 40002):
                try:
                    pred = sy.PredicateManager().CreatePropTypePredicate(tset_id)
                    ents = study_doc.CreateEntityList()
                    ents.SelectFromPredicate(pred)
                    if int(ents.Size() or 0) < 1:
                        continue
                    removed += int(ents.Size() or 0)
                    sy.MeshEditor().Delete(ents)
                except Exception as ce:
                    _exit_if_com_dead(ce, "gate removal")
                    log("Could not remove gates of type {0}: {1}".format(tset_id, ce))
            if removed:
                try:
                    sy.PropertyEditor().RemoveUnusedProperties()
                except Exception:
                    pass
                try:
                    sy.Build()
                except Exception:
                    pass
                log("Removed {0} pre-existing injection location(s) so Gate "
                    "Location can run.".format(removed))
            return removed

        def run_auto_gate_location():
            """Run Gate Location, place the gate on the recommended node.
            True when a gate now exists. Restores the user's sequence."""
            user_sequence = ""
            try:
                user_sequence = str(study_doc.AnalysisSequence() or "").strip()
            except Exception:
                pass
            log("Auto gate location: current sequence '{0}' will be restored "
                "afterwards.".format(user_sequence or "(unknown)"))

            # ERROR 1204100: a single stray gate makes the whole analysis abort.
            remove_injection_locations()

            # Switching the sequence with results present raises Moldflow's
            # native "This modification will invalidate the results"
            # Delete/Create Copy/Cancel modal, which this script cannot
            # dismiss -- it would simply hang. Clearing results first, and
            # solving with prompts off below, keeps that dialog off screen.
            try:
                study_doc.DeleteResults(0)
            except Exception as de:
                log("DeleteResults(0) before Gate Location: {0}".format(de))

            token_set = False
            for token in GATE_LOCATION_TOKENS:
                if set_sequence_token(token):
                    token_set = True
                    break
            if not token_set:
                log("ERROR: no Gate Location sequence token was accepted "
                    "(tried {0}).".format(GATE_LOCATION_TOKENS))
                return False

            out_path = resolve_out_path()
            solve_start = time.time()
            try:
                study_doc.Save()
            except Exception as se:
                log("Warning: could not save before Gate Location: {0}".format(se))

            try:
                import ui_bridge
                ui_bridge.set_step_in_progress(
                    "solver_status",
                    "Finding the best gate location...")
            except Exception:
                pass

            # AnalyzeNow2's third argument suppresses the solver's prompts.
            # The 2-arg AnalyzeNow shows them, and a native modal behind the
            # panel is an unrecoverable hang for an unattended run.
            try:
                log("Calling study_doc.AnalyzeNow2(False, True, False) for "
                    "Gate Location...")
                res = study_doc.AnalyzeNow2(False, True, False)
                log("AnalyzeNow2 returned: {0}".format(res))
            except Exception as ae:
                log("Gate Location AnalyzeNow2 exception: {0}".format(ae))

            nodes = wait_for_gate_location(out_path, solve_start)
            archive_gate_location_log(out_path, solve_start, nodes)

            # Gate Location has now written results of its own, and switching
            # the sequence with results present is exactly what raises the
            # blocking "This modification will invalidate the results" modal.
            # The recommendation is already parsed and archived, so the
            # results themselves are of no further use -- clear them first.
            try:
                study_doc.DeleteResults(0)
            except Exception as de:
                log("DeleteResults(0) after Gate Location: {0}".format(de))

            # Restore the user's sequence BEFORE returning on either path, so a
            # failed gate search cannot leave the study set to Gate Location
            # and quietly turn the real run into another gate search.
            if user_sequence and not set_sequence_token(user_sequence):
                log("WARNING: could not restore the sequence to "
                    "'{0}'.".format(user_sequence))

            if not nodes:
                # The solver's own reason lives in <stem>~N.err as numeric
                # IDMSG codes, not as text in the .out -- and Moldflow deletes
                # those scratch files soon after the run, so decode NOW.
                # ERROR 1204100 ("Predefined gates detected") is the one to
                # expect here if a gate slipped past remove_injection_locations.
                try:
                    if out_path:
                        # decode_solver_err strips the study's own ~N suffix.
                        decoded = decode_solver_err(
                            Path(out_path).parent, Path(out_path).stem,
                            solve_start)
                        if decoded:
                            sev, txt = decoded
                            log("Gate Location {0}: {1}".format(sev, txt))
                except Exception as ce:
                    log("Could not decode the Gate Location .err: {0}".format(ce))
                return False

            node = choose_recommended_node(nodes)

            # Sanity check, and a useful line in the log: where is that node?
            # The mesh has not been touched since the search, so this should
            # always resolve; if it does not, the node label is not trustworthy
            # and manual placement is the honest fallback.
            try:
                node_table = get_surface_nodes_display()
                coords = node_table.get(int(node))
                if coords:
                    log("Recommended node {0} sits at ({1:.2f}, {2:.2f}, "
                        "{3:.2f}) {4}.".format(node, coords[0], coords[1],
                                               coords[2], unit_name))
                elif node_table:
                    # A populated table that does not contain the node means
                    # the mesh is not the one the search ran on. Placing a gate
                    # on that label would put it somewhere arbitrary.
                    log("WARNING: node {0} is not in the current mesh's node "
                        "table ({1} nodes) — refusing to place a gate on "
                        "it.".format(node, len(node_table)))
                    return False
                else:
                    # No table to check against (the model export failed); let
                    # create_gate_on_node be the judge rather than giving up.
                    log("Node table unavailable; placing the gate on node {0} "
                        "without the coordinate cross-check.".format(node))
            except Exception as ce:
                log("Could not resolve node {0} to coordinates: {1}".format(node, ce))

            if not create_gate_on_node(node):
                log("Could not attach a gate to recommended node {0}.".format(node))
                return False

            placed = count_injection_locations()
            log("Gate placed automatically on node {0} ({1} gate(s)).".format(
                node, placed))
            return placed >= 1

        if auto_gate_pending and count_injection_locations() < 1:
            if run_auto_gate_location():
                gate_count = count_injection_locations()
                show_dialog(
                    "Gate Location Set Automatically",
                    "Moldflow's Gate Location analysis chose the gate position\n"
                    "and it has been placed on the model.\n\n"
                    "The workflow continues with your selected analysis."
                )
            else:
                log("Automatic gate location failed; asking for a manual gate.")
                show_dialog(
                    "Automatic Gate Location Unavailable",
                    "Moldflow could not work out a gate location automatically.\n\n"
                    "Please place the gate yourself: Home > Boundary Conditions >\n"
                    "Injection Locations, then click the point on the model."
                )
                gate_count = wait_for_manual_gate()
            if gate_count < 1:
                stop_after_mesh_stage(
                    "No injection location was set; the analysis cannot run.")
                return

        # -----------------------------------------
        # PHASE 4: RUN SOLVER & RESULTS
        # -----------------------------------------
        log("")
        log("Phase 4: Run Solver & Results Presentation")
        
        # Check if results already exist in the study doc
        has_results = False
        plot_mgr = sy.PlotManager()
        # The poll dataset must be one produced by the LAST phase of the
        # sequence, otherwise the wait finishes early and results are read
        # mid-solve. Fill time (1610) appears when the FILL phase ends — for a
        # Fill+Pack run the packing phase is still going at that point, which
        # is why the automated Results tree held only the 16 fill-phase plots
        # while the manual (fully finished) tree held ~32 (proven 2026-07-20).
        # Every id below is verified against
        #   ...\Moldflow Synergy 2027\data\dat\results.dat
        # (blocks are `(NDDT|ELDT|HLDT) { <id> <ncomp> NAME { "<name>" } }`).
        # Two of these were previously WRONG and cost a full run each:
        #   1800 is "Load, core shift", NOT warp deflection — a Cool|Flow|Warp
        #        run polled it, the solver never produced it, and the wait sat
        #        for the full 4h timeout while Synergy showed "Analysis
        #        complete": no results shown, no images, no report.
        #   1510 does not exist in results.dat at all.
        # ALWAYS resolve a poll id against results.dat before trusting it.
        _seq_u = seq_name.upper()
        poll_ds_id = 1610  # Fill time — correct only for a fill-only run
        if "WARP" in _seq_u:
            poll_ds_id = 6250   # "Deflection, all effects" (NDDT, 3-component vector)
        elif "COOL" in _seq_u and "FILL" not in _seq_u and "FLOW" not in _seq_u:
            poll_ds_id = 5600   # "Temperature, part" — written by the cool phase
        elif "PACK" in _seq_u or "FLOW" in _seq_u:
            # "Flow" IS Moldflow's token for Fill+Pack. Volumetric shrinkage
            # (1620) is written at the end of the PACKING phase.
            poll_ds_id = 1620
        log("Polling on terminal dataset {0} for sequence '{1}'.".format(poll_ds_id, seq_name))

        # resolve_out_path() and scan_out_errors() are defined once, up with
        # mesh_node_count(), because the Dual Domain Gate Location run reads
        # the .out log before Phase 4 exists.

        def _status_is_failure(study):
            """True if the primary analysis slot reports a terminal failure."""
            try:
                st = str(study.AnalysisStatus(0) or "").lower()
            except Exception:
                return False
            return any(k in st for k in ("fail", "cancel", "abort", "error"))

        # -----------------------------------------------------------------
        # Live job card -- our own Job Manager, inside the panel.
        #
        # Synergy's "Job Manager" is a separate product (Simulation Compute
        # Manager) shown in its own Chromium window, and synapi exposes nothing
        # about it. Its queue service does expose the job over REST on
        # localhost, though, so compute_jobs.py reads it and the progress is
        # rendered as a normal panel card. See compute_jobs.py for the API.
        #
        # Every call below is a no-op until start_job_watch() succeeds, and it
        # only succeeds when the queue service actually answers -- on a machine
        # without it the solve behaves exactly as it did before.
        job_watch = {"active": False, "job_id": None, "start": 0.0,
                     "study": "", "sequence": "", "next": 0.0, "last": None,
                     "viewer": None, "can_open": False, "exclude": set(), "cancel_requested": False,
                     "percent": 0}

        # Label of the card's button. It is both what the panel draws and what
        # comes back as the answer, so it is defined once.
        OPEN_VIEWER_OPTION = "Open Job Manager"

        # The queue is polled far less often than the result dataset: job
        # progress moves in percent, not in milliseconds, and this read sits in
        # the same loop that is watching for results.
        JOB_CARD_INTERVAL = 1.5

        # While the job id is still unknown every pass re-reads the whole job
        # history. The first 10 seconds use a tight ~1.0s cadence so fast solves
        # are caught in INPROGRESS before transitioning to COMPLETED.
        JOB_SEARCH_FAST_WINDOW = 10.0
        JOB_SEARCH_FAST_INTERVAL = 1.0
        JOB_SEARCH_INTERVAL = 4.0

        # How finely the solve-wait slices its sleep so the card can refresh
        # between the (slower, COM-bound) dataset polls. Keeps the panel within
        # a couple of seconds of Autodesk's own Job Manager window.
        JOB_CARD_SLICE = 1.0

        # Finding the job means reading the whole job history; polling a known
        # job is a single small record. If the job never turns up (an unusable
        # study name, a queue that took the work under another name) the search
        # backs off after this long rather than re-reading ~500KB every few
        # seconds for the rest of a multi-hour solve.
        JOB_SEARCH_PATIENCE = 300.0
        JOB_SEARCH_BACKOFF = 30.0

        def start_job_watch(study_name, solve_start, sequence):
            """Begin tracking this run's queue job. True when the card is on."""
            if not str(study_name or "").strip():
                # The job is found by study name; without one there is nothing
                # to match on and the card could only ever say "waiting".
                log("Job Manager: study has no name to match a queue job on.")
                return False
            try:
                import compute_jobs
                if not compute_jobs.available():
                    log("Job Manager: local Compute queue not reachable; "
                        "falling back to a folded panel for the solve.")
                    return False
            except Exception as e:
                log("Job Manager: compute_jobs unavailable ({0}).".format(e))
                return False
            # Everything the queue already holds for this study -- crucially
            # the Gate Location job, which is also a "study"-type job on the
            # same name and lands inside find_job's 60s slack. Without this the
            # card latched onto it and showed "Canceled 100%, phases: gate"
            # for the whole of a healthy Fill.
            try:
                seen_before = compute_jobs.job_ids(study_name)
            except Exception:
                seen_before = set()
            if seen_before:
                log("Job Manager: ignoring {0} earlier job(s) for this "
                    "study.".format(len(seen_before)))

            job_watch.update({"active": True, "job_id": None,
                              "start": float(solve_start), "study": study_name,
                              "sequence": sequence, "next": 0.0, "last": None,
                              "viewer": None, "exclude": seen_before,
                              "can_open": bool(compute_jobs.viewer_exe()), "cancel_requested": False,
                              "percent": 0})
            log("Job Manager: watching the queue for '{0}' (viewer {1}).".format(
                study_name, "available" if job_watch["can_open"] else "not installed"))
            return True

        def open_job_viewer():
            """Open Autodesk's Job Viewer window for the user, once.

            Electron opens a new window per launch, so a second press while the
            first is still up would stack duplicates. The handle we already own
            answers that precisely -- poll() is None only while OUR viewer is
            still running -- without going near the process table."""
            proc = job_watch.get("viewer")
            try:
                if proc is not None and proc.poll() is None:
                    log("Job Manager: viewer is already open.")
                    return
            except Exception:
                pass
            try:
                import compute_jobs
                proc = compute_jobs.open_viewer()
            except Exception as e:
                log("Job Manager: could not open the viewer ({0}).".format(e))
                return
            job_watch["viewer"] = proc
            log("Job Manager: opened the Job Viewer window."
                if proc else "Job Manager: viewer executable not found.")

        def refresh_job_card(force=False):
            """Publish/refresh the job card. Throttled; never raises."""
            if not job_watch["active"]:
                return
            try:
                import compute_jobs
                import ui_bridge
            except Exception:
                job_watch["active"] = False
                return

            # Button presses are handled on EVERY pass, ahead of the refresh
            # throttle. The throttle stretches to JOB_SEARCH_BACKOFF once the
            # job search gives up, and making someone wait half a minute for a
            # window to open after pressing the button would read as broken.
            # Consuming the answer here also clears it, so the refresh below is
            # not skipped by publish_live_card protecting an unread click.
            try:
                if ui_bridge.take_live_card_answer("job_manager") == OPEN_VIEWER_OPTION:
                    open_job_viewer()
            except Exception:
                pass

            now = time.time()
            if not force and now < job_watch["next"]:
                return
            searching_for = now - job_watch["start"]
            if job_watch["job_id"]:
                # Known id: a single ~1.4KB record, so it can be read often and
                # the card stays in step with Autodesk's own Job Manager.
                # Keep fast cadence during early window until INPROGRESS/terminal is observed
                # so rapid solves do not bypass the analysis-start notification.
                if (job_watch.get("last") not in ("INPROGRESS", "COMPLETED", "FAILED", "CANCELED", "TIMEDOUT")
                        and searching_for < JOB_SEARCH_FAST_WINDOW):
                    job_watch["next"] = now + JOB_SEARCH_FAST_INTERVAL
                else:
                    job_watch["next"] = now + JOB_CARD_INTERVAL
            elif searching_for < JOB_SEARCH_FAST_WINDOW:
                # Early search window: poll every ~1.0s to catch fast solves
                # before they transition from INPROGRESS to COMPLETED.
                job_watch["next"] = now + JOB_SEARCH_FAST_INTERVAL
            elif searching_for < JOB_SEARCH_PATIENCE:
                # Still searching: each pass re-reads the WHOLE job history 
                # (~500KB), so keep that on the slower cadence.
                job_watch["next"] = now + JOB_SEARCH_INTERVAL
            else:
                job_watch["next"] = now + JOB_SEARCH_BACKOFF

            job = None
            try:
                if job_watch["job_id"]:
                    # Known id -> poll the single job (~1.4KB) rather than the
                    # whole history (~500KB) every few seconds.
                    job = compute_jobs.get_job(job_watch["job_id"])
                else:
                    # AnalyzeNow hands the job to the queue a moment after it
                    # returns, so the first few lookups legitimately find
                    # nothing and search by name + start time until it lands.
                    job = compute_jobs.find_job(
                        job_watch["study"], job_watch["start"],
                        exclude_ids=job_watch.get("exclude"))
                    if job:
                        job_watch["job_id"] = str(job.get("jobID") or "")
                        job_watch["scm_job_id"] = str(job.get("jobID") or "")
                        log("Job Manager: tracking queue job {0}.".format(
                            job_watch["job_id"]))
            except Exception:
                job = None

            fields = {"sequence": job_watch["sequence"],
                      "elapsed": int(max(0.0, now - job_watch["start"])),
                      "name": job_watch["study"],
                      "message": ""}
            summary = None
            try:
                summary = compute_jobs.summarize(job)
            except Exception:
                summary = None
            if summary:
                # wait_for_results() (a sibling function, not nested in here)
                # needs the latest percent for the final CANCELED report it
                # sends on mobile cancellation, so it is kept on job_watch --
                # this `summary` local is not visible outside refresh_job_card.
                job_watch["percent"] = summary["percent"]
                fields.update({
                    "name": summary["name"] or job_watch["study"],
                    "status": summary["status"],
                    "percent": summary["percent"],
                    "cloud": summary["cloud"],
                    "worker": summary["worker"],
                })
                try:
                    fields["phases"] = compute_jobs.phases(job)
                except Exception:
                    fields["phases"] = []
                # Mirror status changes into the run log, so the text log tells
                # the same story as the card after the fact.
                status_changed = summary["status"] != job_watch["last"]
                if status_changed:
                    job_watch["last"] = summary["status"]
                    log("Job Manager: job is {0} ({1}%).".format(
                        summary["status"], summary["percent"]))

                # Mobile push -- same tick, same already-computed summary; a
                # no-op until mobile_report_config.json turns it on. Only
                # worth resolving the human error text on an actual failure
                # status change, not every poll tick.
                error_text = None
                if status_changed and summary["status"] in (
                        "FAILED", "CANCELED", "TIMEDOUT"):
                    try:
                        out_path = resolve_out_path()
                        if out_path:
                            sev, txt = decode_solver_err(
                                out_path.parent, out_path.stem,
                                job_watch["start"]) or (None, None)
                            error_text = txt or scan_out_errors(
                                out_path, job_watch["start"])
                    except Exception:
                        error_text = None
                try:
                    import mobile_reporter

                    # Reuse the same SCM record that was already fetched for
                    # the live Job Manager card. Do NOT create another SCM
                    # polling loop here.
                    if isinstance(job, dict):
                        scm_payload = job.get("payload") or {}
                        scm_job_id = str(job.get("jobID") or job_watch["job_id"] or "")
                        scm_type = str(scm_payload.get("type") or "")
                        scm_user = str(scm_payload.get("user") or "").strip()
                        if not scm_user:
                            scm_user = (
                                summary.get("scm_user")
                                or os.environ.get("USERNAME")
                                or os.environ.get("USER")
                                or ""
                            )
                        parent_raw = job.get("parent")
                        parent_job_id = (
                            str(parent_raw).strip()
                            if parent_raw is not None and str(parent_raw).strip()
                            else None
                        )
                    else:
                        scm_payload = {}
                        scm_job_id = str(job_watch["job_id"] or "")
                        scm_type = ""
                        scm_user = (
                            summary.get("scm_user")
                            or os.environ.get("USERNAME")
                            or os.environ.get("USER")
                            or ""
                        )
                        parent_job_id = None

                    cancel_sig = mobile_reporter.report_status({
                        "job_id": job_watch["job_id"],
                        "name": summary["name"] or job_watch["study"],
                        "type": job_watch["sequence"],
                        "status": summary["status"],
                        "percent": summary["percent"],
                        "started": job_watch["start"],
                        "finished": summary["finished"],
                        "error_message": error_text,

                        # Simulation Compute Manager metadata.
                        "scm_job_id": scm_job_id or None,
                        "scm_type": scm_type or None,
                        "compute_source": (
                            "CLOUD" if summary["cloud"] else "LOCAL"
                        ),
                        "scm_user": scm_user or None,
                        "worker": summary["worker"] or None,
                        "parent_job_id": parent_job_id,
                    }, log=log)
                    if cancel_sig:
                        job_watch["cancel_requested"] = True
                except Exception as exc:
                    try:
                        log(
                            "Mobile notify: could not prepare SCM metadata ({0})."
                            .format(exc)
                        )
                    except Exception:
                        pass
            else:
                fields["message"] = "Waiting for the job to appear in the queue..."

            # No viewer installed means no button -- offering to open something
            # that is not there is worse than not offering it.
            options = [OPEN_VIEWER_OPTION] if job_watch["can_open"] else []
            try:
                ui_bridge.publish_live_card(
                    "job_manager", "Analysis Running", fields, options)
            except Exception:
                pass

        def stop_job_watch():
            """Take the card down once the solve is over."""
            if not job_watch["active"]:
                return
            job_watch["active"] = False
            try:
                import ui_bridge
                ui_bridge.clear_live_card("job_manager")
            except Exception:
                pass

        def wait_for_results(ds_id, out_path, solve_start, timeout=14400.0, poll=5.0):
            """Poll until the result dataset exists (success), the solver writes
            an explicit error / reports a failure status (failure), Synergy dies,
            or the timeout elapses. Returns (ok, detail).

            AnalyzeNow queues the job in the Analysis Manager and can return
            before results are registered, so the dataset — not the immediate
            return value — is the authoritative success signal."""
            start = time.time()
            dead = 0
            # Set once the solver first reports a settled 'Completed'.  If the
            # run has finished and our dataset STILL has not appeared, waiting
            # longer is pointless -- the id is wrong for this sequence, or the
            # phase that writes it never ran.  Without this the poll burns the
            # entire 4h timeout in silence (exactly what a Cool|Flow|Warp run
            # did against the bogus id 1800), so a wrong id costs a whole run
            # instead of reporting itself in seconds.
            completed_since = None
            grace = 90.0   # datasets can lag a 'Completed' status by a little

            # STALE-STATUS GUARD. AnalysisStatus(0) still reports the PREVIOUS
            # job until this one starts, and Phase 3b now runs a Gate Location
            # analysis in this same study first -- whose slot ends up
            # 'Cancelled' once its results are cleared. Without this latch the
            # very first poll read that stale 'Cancelled', declared failure,
            # and abandoned a Fill that was in fact running normally (a live
            # 14:34 run: solver log showed the fill progressing to 8.8s and a
            # 3.7MB .of1 on disk, yet the workflow reported failure).
            # Mirrors the seen_active latch wait_for_mesh already uses for the
            # same reason. A genuine early failure still reports, just after
            # STALE_STATUS_GRACE rather than instantly.
            STALE_STATUS_GRACE = 60.0
            seen_active = False
            TERMINAL = ("complet", "fail", "cancel", "abort", "error")

            while time.time() - start < timeout:
                try:
                    if plot_mgr.FindDatasetByID(ds_id):
                        return True, "dataset {0} available".format(ds_id)
                    dead = 0
                except Exception as exc:
                    if _com_is_dead(exc):
                        dead += 1
                        if dead >= 3:
                            return False, "Synergy connection lost during solve"
                    # transient COM hiccup — keep polling
                err = scan_out_errors(out_path, solve_start)
                if err:
                    return False, "solver reported an error"

                try:
                    st = str(study_doc.AnalysisStatus(0) or "").strip().lower()
                except Exception:
                    st = ""
                # An active (non-terminal) status proves THIS job has started,
                # so anything terminal we see from here on is genuinely ours.
                if st and not any(k in st for k in TERMINAL):
                    if not seen_active:
                        log("  Solver job is active (status '{0}').".format(st))
                    seen_active = True

                if _status_is_failure(study_doc):
                    if seen_active or time.time() - start >= STALE_STATUS_GRACE:
                        return False, "analysis reported a failure status"
                    # Otherwise: almost certainly the previous job's status,
                    # read before this one had a chance to register.

                # Has the solver finished while our dataset stayed absent?
                # Gated on seen_active for the same reason as the failure check
                # above: a 'Completed' left over from the Gate Location job
                # would otherwise start this clock before our solve even began
                # and bail with a bogus "dataset was never produced".
                if seen_active and "complet" in st and "transferring" not in st:
                    if completed_since is None:
                        completed_since = time.time()
                        log("  Solver reports '{0}' but dataset {1} is not present "
                            "yet; allowing {2:.0f}s for it to register.".format(
                                st, ds_id, grace))
                    elif time.time() - completed_since >= grace:
                        return False, (
                            "analysis finished but result dataset {0} was never "
                            "produced — the poll dataset is wrong for sequence "
                            "'{1}', or that phase did not run".format(ds_id, seq_name)
                        )
                else:
                    completed_since = None

                # Refresh the card SEVERAL times per dataset poll rather than
                # once. The dataset/status checks above are COM round-trips and
                # stay on the slow `poll` cadence, but the job card only reads
                # the local REST queue -- polling it once per 5s loop, behind a
                # 4s throttle, left the panel up to ~9s behind the Job Manager
                # window (observed: our card 65% while Autodesk's showed 69%).
                # Slicing the sleep also makes the "Open Job Manager" button
                # respond promptly, since presses are handled on every pass.
                slept = 0.0
                while slept < poll:
                    refresh_job_card()
                    if job_watch.get("job_id"):
                        try:
                            import mobile_reporter
                            if mobile_reporter.check_cancel(job_watch["job_id"], log=log):
                                job_watch["cancel_requested"] = True
                        except Exception:
                            pass
                    if job_watch.get("cancel_requested"):
                        log("Mobile cancellation detected — initiating SCM cancellation.")
                        scm_jid = job_watch.get("scm_job_id") or job_watch.get("job_id")
                        scm_cancelled = False
                        if scm_jid:
                            try:
                                import compute_jobs
                                scm_cancelled = compute_jobs.cancel_job(str(scm_jid))
                            except Exception as exc:
                                log("Job Manager: exception during SCM cancel_job: {0}".format(exc))
                                scm_cancelled = False

                        if not scm_cancelled:
                            log("Job Manager: SCM cancellation failed or unverified for {0}; continuing wait.".format(scm_jid))
                        else:
                            log("Job Manager: SCM job {0} cancellation confirmed.".format(scm_jid))
                            try:
                                import mobile_reporter
                                mobile_reporter.report_status({
                                    "job_id": job_watch.get("job_id"),
                                    "name": job_watch.get("study"),
                                    "type": job_watch.get("sequence"),
                                    "status": "CANCELED",
                                    "percent": job_watch.get("percent", 0),
                                    "started": job_watch.get("start"),
                                    "finished": True,
                                    "error_message": "Cancelled remotely via mobile app",
                                    "scm_job_id": str(scm_jid) if scm_jid else None,
                                }, log=log)
                            except Exception:
                                pass
                            return False, "Cancelled remotely via mobile app"
                    step = min(JOB_CARD_SLICE, poll - slept)
                    time.sleep(step)
                    slept += step
            return False, "timed out after {0}s".format(int(timeout))

        def wait_for_analysis_settled(timeout=900.0, poll=5.0, quiet_for=20.0):
            """After the terminal dataset appears, give the solver time to finish
            registering the REST of its datasets before we read the results tree.

            Moldflow writes results progressively and keeps the job in
            'Transferring Results' after the last solver phase; enumerating too
            early yields a partial tree. Waits for the status to reach a settled
            'Completed' and hold for `quiet_for` seconds. Never fatal — this is
            a best-effort refinement, so any error just returns."""
            start = time.time()
            settled_since = None
            last = None
            while time.time() - start < timeout:
                try:
                    st = str(study_doc.AnalysisStatus(0) or "").strip()
                except Exception as exc:
                    if _com_is_dead(exc):
                        return
                    time.sleep(poll)
                    continue
                if st != last:
                    log("  Post-solve status: {0}".format(st or "(empty)"))
                    last = st
                low = st.lower()
                if any(k in low for k in ("fail", "cancel", "abort", "error")):
                    return
                done = "complet" in low and "transferring" not in low
                if done:
                    if settled_since is None:
                        settled_since = time.time()
                    elif time.time() - settled_since >= quiet_for:
                        log("Analysis fully settled ('{0}'); reading results.".format(st))
                        return
                else:
                    settled_since = None
                refresh_job_card()
                time.sleep(poll)
            log("Settle wait timed out; reading results anyway.")

        try:
            if plot_mgr.FindDatasetByID(poll_ds_id):
                has_results = True
        except Exception:
            pass

        if has_results:
            log("Existing analysis results detected. Skipping solver run.")
        else:
            run_choice = ask_yes_no(
                "Run Analysis",
                "The model is meshed and ready.\n\nDo you want to run the analysis now?"
            )
            
            if run_choice == IDYES:
                # 1. Save the study document first so the solver gets the latest changes
                try:
                    study_doc.Save()
                    log("Study saved successfully before analysis.")
                except Exception as se:
                    log("Warning: Failed to save study: {0}".format(se))

                log("Starting solver...")

                # The "Analysis Solver" journey row sat on "Pending..." for the
                # whole solve and only ever changed at the end. Marking it
                # underway here also gives the panel's status line something
                # true to say while the job card is up -- that line is
                # suppressed for the job card, which asks for nothing.
                try:
                    import ui_bridge
                    ui_bridge.set_step_in_progress(
                        "solver_status",
                        "Running the analysis ({0})...".format(seq_name or "solving"))
                except Exception:
                    pass

                out_file_path = resolve_out_path()
                solve_start = time.time()

                # The solve used to fold the panel away for its whole duration
                # -- often hours -- because Synergy's Analysis Manager held the
                # only progress and the panel had nothing to add. The Compute
                # queue publishes that same progress over REST, so when it
                # answers the panel STAYS UP and shows a live job card instead
                # of leaving the user staring at a blind wait. When it does not
                # answer, fold exactly as before.
                try:
                    study_file_name = str(study_doc.StudyName() or "")
                except Exception:
                    study_file_name = ""
                _watching = start_job_watch(study_file_name, solve_start, seq_name)
                _solve_folded = False if _watching else fold_panel(
                    log, "analysis running")

                # AnalyzeNow queues the analysis in the Analysis Manager; it may
                # return before results are written, so its return value is not
                # a completion signal — we poll the result dataset below.
                try:
                    log("Calling study_doc.AnalyzeNow(False, True)...")
                    res = study_doc.AnalyzeNow(False, True)
                    log("study_doc.AnalyzeNow(False, True) returned: {0}".format(res))
                except Exception as ae:
                    log("AnalyzeNow exception: {0}".format(ae))

                # Put the card up straight away rather than waiting out the
                # first poll interval: the user has just been told the solver
                # is starting, and an empty panel in that gap reads as a hang.
                refresh_job_card(force=True)

                log("Waiting for analysis to finish (result dataset {0})...".format(poll_ds_id))
                ok, detail = wait_for_results(poll_ds_id, out_file_path, solve_start)
                if ok:
                    wait_for_analysis_settled()

                # The solve is over either way. Take the job card down and bring
                # the panel back BEFORE the branch below, so the results-review
                # prompt and the failure dialog both land on a visible panel
                # rather than behind a folded header or a stale progress card.
                stop_job_watch()
                unfold_panel(_solve_folded, log, "analysis finished")

                if ok:
                    log("Analysis completed successfully. Dataset {0} found.".format(poll_ds_id))
                    # Journey summary: this row sat on "Pending..." for the whole
                    # run and was still pending on the Workflow Complete card.
                    try:
                        import ui_bridge
                        ui_bridge.update_state("params", {
                            "solver_status": "Analysis complete ({0})".format(
                                seq_name or "solved")})
                    except Exception:
                        pass
                else:
                    log("Analysis did not produce results: {0}".format(detail))

                    # Land the journey row on a final value. It was marked
                    # underway when the solver started, and this branch ends the
                    # run -- left as-is it would read "Running the analysis..."
                    # on a workflow that had already stopped.
                    try:
                        import ui_bridge
                        ui_bridge.update_state("params", {
                            "solver_status": "Analysis failed"})
                    except Exception:
                        pass

                    # Prefer the solver's OWN reason, decoded from this run's
                    # <study>~*.err codes via cmmesage.dat. The .out is numeric
                    # so scan_out_errors usually finds nothing on these jobs;
                    # the .err is where "No mold block (3D) mesh found" lives.
                    reason = None
                    try:
                        if out_file_path:
                            decoded = decode_solver_err(
                                Path(out_file_path).parent,
                                Path(out_file_path).stem,
                                solve_start)
                            if decoded:
                                sev, txt = decoded
                                reason = txt
                                log("Solver {0} decoded from .err: {1}".format(
                                    sev, txt.replace("\n", " | ")))
                    except Exception as de:
                        log("Could not decode solver .err messages: {0}".format(de))

                    if not reason:
                        # Fall back to any text ERROR line in the .out.
                        out_error = scan_out_errors(out_file_path, solve_start)
                        if out_error:
                            log("Solver error detected in .out file!")
                            log("Details: " + out_error.replace("\n", " | "))
                            reason = out_error

                    if "cancel" in str(detail).lower():
                        show_dialog(
                            "Analysis Cancelled",
                            "The analysis was cancelled remotely via the mobile app.\n\n"
                            "You can re-run the workflow when ready."
                        )
                    elif reason:
                        show_dialog(
                            "Analysis Failed",
                            "The analysis failed:\n\n{0}\n\n"
                            "Fix the cause above, then re-run the workflow.".format(reason)
                        )
                    else:
                        show_dialog(
                            "Analysis Failed",
                            "The analysis did not produce results.\n\n"
                            "({0})\n\n"
                            "Check the analysis log in Synergy for details.".format(detail)
                        )
                    return
            else:
                log("User chose not to run solver. Automation finished.")
                show_dialog("Workflow Stopped", "Automation finished. You can run the solver manually when ready.")
                return
            
        # -----------------------------------------
        # Present the client-required result plots
        # -----------------------------------------
        review_choice = ask_yes_no(
            "Review Results",
            "Analysis complete!\n\nDo you want to display the required result plots?"
        )

        if review_choice == IDYES:
            log("Displaying client-required result plots...")
            viewer = sy.Viewer()

            # StudyDoc.AnalysisSequence() returns Moldflow's internal TOKEN
            # ("Flow"), not the sequence the user chose ("Fill + Pack"). The
            # readable name lives in AnalysisSequenceDescription() — read it
            # from the study instead of mapping the token ourselves, so the
            # report always states the real sequence. (`seq_name` keeps the
            # token: the terminal-dataset choice above is keyed off it.)
            seq_display = ""
            for getter in ("AnalysisSequenceDescription", "AnalysisSequence"):
                try:
                    v = str(getattr(study_doc, getter)() or "").strip()
                except Exception:
                    continue
                if v:
                    seq_display = v
                    break
            if not seq_display:
                seq_display = str(seq_name or "(unknown)")
            log("Report analysis sequence: '{0}' (internal token '{1}').".format(
                seq_display, seq_name))

            # The master engineering result list is the "Top 12 Results to View
            # from a Cool + Flow + Warp Simulation" from Autodesk's review deck
            # (see RESULT_GUIDE at module scope for each result's meaning,
            # investigation points and targets).
            #
            # Each entry is (display label, [candidate result names]). A
            # result's actual name in Synergy depends on the mesh type and
            # sequence, so candidates are ordered most-specific-first and the
            # first one that resolves is used. Names are the authoritative
            # strings from Moldflow's data\dat\results.dat, cross-checked
            # against the plot names a real 3D Fill+Pack run produces.
            #
            # Availability is NOT assumed. Only the subset of the Top 12 that
            # this sequence actually produced is exported — the display loop
            # below gates every result on a dataset that exists AND holds data,
            # and reports the rest as missing rather than inventing them.
            # (Deflection needs Warp; the two part-temperature results need
            # Cool; Orientation needs a filled grade or a Midplane/DD mesh.)

            # Orientation is #7 in the reference deck for any material. For a
            # fibre-filled grade it is the fibre orientation tensor; for an
            # unfilled grade it is the molecular orientation at skin/core.
            # Try the applicable pair first, then fall back to the other.
            if has_fiber:
                orientation_candidates = [
                    "Fiber orientation tensor (3D)", "Fiber orientation tensor",
                    "Average fiber orientation (3D)", "Average fiber orientation",
                    "Orientation at skin", "Orientation at core"]
            else:
                orientation_candidates = [
                    "Orientation at skin", "Orientation at core",
                    "Fiber orientation tensor (3D)", "Fiber orientation tensor",
                    "Average fiber orientation (3D)", "Average fiber orientation"]

            result_specs = [
                # 1
                ("Fill time",
                 ["Fill time"]),
                # 2 — the deck's "Injection Pressure" is the pressure required
                # at the nozzle, i.e. the injection-location trace, not the V/P
                # switchover snapshot the previous report used.
                ("Injection pressure",
                 ["Pressure at injection location", "Pressure at end of fill",
                  "Pressure"]),
                # 3
                ("Clamp force",
                 ["Clamp force"]),
                # 4 — velocity-weighted temperature averaged through the
                # thickness. On a 3D mesh this is plotted as "Temperature".
                ("Bulk temperature",
                 ["Bulk temperature", "Temperature (3D)", "Temperature"]),
                # 5
                ("Shear rate",
                 ["Shear rate, bulk", "Shear rate", "Shear rate, maximum"]),
                # 6
                ("Shear stress",
                 ["Shear stress at wall"]),
                # 7
                ("Orientation", orientation_candidates),
                # 8
                ("Volumetric shrinkage",
                 ["Volumetric shrinkage", "Volumetric shrinkage (3D)",
                  "Average volumetric shrinkage", "Volumetric shrinkage at ejection"]),
                # 9 — Moldflow plots frozen layer THICKNESS as a fraction.
                ("Frozen layer thickness",
                 ["Frozen layer fraction", "Frozen layer fraction at end of fill"]),
                # 10 — Warp sequence only.
                ("Deflection (all effects)",
                 ["Deflection, all effects", "Deflection, all effects:Deflection",
                  "Deflection"]),
                # 11 — Cool sequence only.
                ("Top temperature, part",
                 ["Temperature, part (top)", "Temperature (top), parting plane"]),
                # 12 — Cool sequence only.
                ("Average temperature, part",
                 ["Average temperature, part", "Temperature, part (averaged)"]),
            ]

            # NOTE: the reference deck is the ONLY authority for what counts as
            # a reportable engineering result. Legacy results the earlier
            # implementation exported — Sink marks, Pressure at V/P switchover,
            # Air traps, Weld lines, Cooling circuit flow rate — are NOT
            # headline results in the deck and are deliberately NOT reported.
            # Moldflow still generates them (they stay visible in the Synergy
            # Results tree), they simply do not enter the document. Do not
            # re-add them because they exist in the study: existing != reportable.

            # DEFINITIVE (proven by log 2026-07-20): deleting non-required plots
            # succeeds at the API level (`Results tree now contains (4)`) but
            # Moldflow silently REGENERATES its default plots in the UI a moment
            # later — the tree cannot be curated by any means (Antigravity hit the
            # same wall; its "only 12 appear" was the momentary pre-regeneration
            # state). So we do NOT delete anything. We only DISPLAY the required
            # results that actually exist (matching Moldflow's own plots by exact
            # name, so no duplicates and no grey phantoms), and report the set.
            def plot_name(pl):
                try:
                    return str(pl.GetName() or "")
                except Exception:
                    return ""

            def enumerate_plots():
                out = []
                try:
                    pl = plot_mgr.GetFirstPlot()
                except Exception as ee:
                    log("GetFirstPlot failed: {0}".format(ee))
                    return out
                misses = 0
                while pl is not None and misses < 3:
                    out.append((pl, plot_name(pl)))
                    try:
                        pl = plot_mgr.GetNextPlot(pl)
                        misses = 0
                    except Exception:
                        misses += 1
                return out

            def matches(name, candidate):
                n = name.strip().lower()
                c = candidate.strip().lower()
                return n == c or n.startswith(c + ":")

            existing = enumerate_plots()
            log("Instantiated plots ({0}): {1}".format(
                len(existing), ", ".join(nm for _p, nm in existing) or "(none)"))

            def dataset_exists(did, candidate=""):
                """True if THIS STUDY actually contains dataset `did`.

                `FindDatasetByID` is a genuine runtime existence check and was
                correct all along — it is `FindDatasetIdsByName` that resolves a
                name against the results.dat DEFINITIONS and happily returns an
                id (1760, 1620, 5030, 6250, ...) for a result this run never
                produced. Removing this gate on 2026-07-20 is what let six
                phantom results be "displayed" while the Results tree showed
                none of them."""
                try:
                    if plot_mgr.FindDatasetByID(did):
                        return True
                except Exception:
                    return False
                log("  Dataset {0} ('{1}') is not in this study — skipping.".format(did, candidate))
                return False

            def dataset_has_data(did, candidate=""):
                """True only if dataset `did` actually CONTAINS data.

                A dataset can be DEFINED but EMPTY: resolving a name to an id
                proves the result is *known to Moldflow*, not that this run
                produced it. Proven twice in this project — gate dataset 2050
                (2026-07-18) resolved by name yet GetScalarData returned no
                nodes, and on 2026-07-20 a Fill+Pack run resolved 'Circuit flow
                rate'->5030 and 'Deflection, all effects'->6250 even though
                Fill+Pack has no Cool or Warp phase and the manual results tree
                showed neither.

                Probe every accessor, not just the scalar one: Deflection is a
                VECTOR result and circuit/XY results can be NON-MESH, so a
                scalar-only test would wrongly discard genuine results.
                """
                def arr_size(a):
                    try:
                        return int(a.Size())
                    except Exception:
                        try:
                            return int(a.Size)
                        except Exception:
                            return 0

                # Each probe needs its own live entity array so we can measure it.
                for kind in ("scalar", "vector", "tensor"):
                    ents = sy.CreateIntegerArray()
                    indp = sy.CreateDoubleArray()
                    try:
                        if kind == "scalar":
                            plot_mgr.GetScalarData(did, indp, ents, sy.CreateDoubleArray())
                        elif kind == "vector":
                            plot_mgr.GetVectorData(did, indp, ents, sy.CreateDoubleArray(),
                                                   sy.CreateDoubleArray(), sy.CreateDoubleArray())
                        else:
                            plot_mgr.GetTensorData(did, indp, ents,
                                                   sy.CreateDoubleArray(), sy.CreateDoubleArray(),
                                                   sy.CreateDoubleArray(), sy.CreateDoubleArray(),
                                                   sy.CreateDoubleArray(), sy.CreateDoubleArray())
                        if arr_size(ents) > 0:
                            return True
                    except Exception:
                        pass

                # HIGHLIGHT data (results.dat type HLDT). Air traps and Weld
                # lines each exist BOTH as a nodal dataset and as an HLDT one
                # (Air traps = 1622 NDDT / 1740 HLDT; Weld lines is HLDT only),
                # and HLDT is unreachable via the scalar/vector/tensor
                # accessors — without this probe a genuine Air traps / Weld
                # lines result resolving to its HLDT id would be discarded.
                # The docs don't state whether aHlData is an integer or double
                # array, so try both rather than guess.
                for _mk in (sy.CreateIntegerArray, sy.CreateDoubleArray):
                    try:
                        hl = _mk()
                        plot_mgr.GetHighlightData(did, sy.CreateDoubleArray(), hl)
                        if arr_size(hl) > 0:
                            return True
                    except Exception:
                        pass

                # Non-mesh (XY-plot style) data, e.g. clamp force / circuit results.
                try:
                    nm = sy.CreateDoubleArray()
                    plot_mgr.GetNonmeshData(did, sy.CreateDoubleArray(), nm)
                    if arr_size(nm) > 0:
                        return True
                except Exception:
                    pass

                # Last resort: an XY plot genuinely backed by data.
                try:
                    if plot_mgr.DataHasXYPlotByDsID(did):
                        return True
                except Exception:
                    pass

                log("  Dataset {0} ('{1}') is DEFINED but EMPTY — this run did not "
                    "produce it; skipping.".format(did, candidate))
                return False

            def dataset_id_for(candidate):
                """Real RUNTIME dataset id if this result EXISTS in the results
                files, else 0. GetFirstPlot only lists the ~16 plots Moldflow
                pre-instantiated, but many required results (Pressure at V/P
                switchover, Sink marks, Volumetric shrinkage, Air traps, ...)
                exist as datasets that are not yet plots — this finds those.

                DEFINITIVE (2026-07-20): the old implementation gated on
                `FindDatasetByID(FindDatasetIdByName(name))` and rejected EVERY
                real dataset — the manual Fill+Pack tree clearly contains
                'Pressure at V/P switchover', 'Sink marks estimate',
                'Volumetric shrinkage' and 'Air traps', yet all 6 non-plotted
                results were reported "no dataset". Cause: FindDatasetIdByName
                returns the DEFINITION id from results.dat, while FindDatasetByID
                expects the RUNTIME id — the same definition-vs-runtime id
                mismatch already proven for the gate dataset 2050 on 2026-07-18.

                `FindDatasetIdsByName(name, intArray)` is the correct API: it
                returns a COUNT and fills the array with the actual RUNTIME ids.
                """
                # Primary: runtime ids straight from FindDatasetIdsByName. A name
                # can map to SEVERAL ids (2 were seen for Volumetric shrinkage /
                # Sink marks / Air traps), so try each and take the first that
                # actually holds data rather than assuming index 0.
                try:
                    ids = sy.CreateIntegerArray()
                    count = int(plot_mgr.FindDatasetIdsByName(candidate, ids) or 0)
                    if count > 0:
                        log("  '{0}': FindDatasetIdsByName -> {1} runtime id(s).".format(
                            candidate, count))
                        for i in range(count):
                            try:
                                rid = int(ids.Val(i))
                            except Exception:
                                continue
                            if rid > 0 and dataset_exists(rid, candidate) \
                                    and dataset_has_data(rid, candidate):
                                log("  '{0}': using dataset {1} (has data).".format(candidate, rid))
                                return rid
                except Exception as fe:
                    log("  FindDatasetIdsByName('{0}') failed: {1}".format(candidate, fe))

                # Fallback: single-id lookup. Do NOT use the old
                # FindDatasetByID re-check (definition-id vs runtime-id — it
                # rejected everything); gate on real data instead.
                try:
                    did = int(plot_mgr.FindDatasetIdByName(candidate) or 0)
                except Exception:
                    did = 0
                if did > 0 and dataset_exists(did, candidate) \
                        and dataset_has_data(did, candidate):
                    log("  '{0}': using dataset {1} via FindDatasetIdByName (has data).".format(
                        candidate, did))
                    return did
                return 0

            def _arr_values(arr):
                """A COM data array as a Python list. ToVBSArray marshals the
                whole array in ONE call — indexing Val(i) over 20k+ nodes would
                mean 20k COM round-trips."""
                for m in ("ToVBSArray", "to_list", "to_vb_array"):
                    try:
                        f = getattr(arr, m)
                        if callable(f):
                            r = f()
                            if r is not None:
                                return list(r)
                    except Exception:
                        pass
                return []

            def dataset_range(did, label):
                """Best-effort 'min ... max unit' summary of a dataset, in
                engineering units, so the report can be checked against the
                reference targets. Values come back in the SI storage unit
                declared in results.dat (K, Pa, N, m), which is why they are
                converted here — a raw 343.15 for a temperature would be
                meaningless next to a 'below 93.5 degC' target.

                Purely additive and never fatal: any failure returns None and
                the report simply omits the values line, exactly as before."""
                info = RESULT_GUIDE.get(label) or {}
                unit = info.get("unit", "")
                vals = []
                try:
                    buf = sy.CreateDoubleArray()
                    plot_mgr.GetScalarData(did, sy.CreateDoubleArray(),
                                           sy.CreateIntegerArray(), buf)
                    vals = _arr_values(buf)
                except Exception:
                    vals = []
                if not vals:
                    try:
                        buf = sy.CreateDoubleArray()
                        plot_mgr.GetNonmeshData(did, sy.CreateDoubleArray(), buf)
                        vals = _arr_values(buf)
                    except Exception:
                        vals = []
                nums = []
                for v in vals:
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    if f == f and abs(f) < 1e30:   # drop NaN / sentinel fills
                        nums.append(f)
                if not nums:
                    return None
                lo, hi = min(nums), max(nums)

                # Convert through convert_stat_unit, NOT with the arithmetic
                # inline here. This function used to scale by 1e-6 / 1e-3 / 1e3
                # unconditionally, which is only right while the data really is
                # in results.dat's SI storage unit -- and several datasets come
                # back from this build ALREADY in engineering units. Converting
                # those a second time is what printed
                #     "Pressure at end of fill -- Value range: 0.00 to 0.00 MPa"
                # on study 37, where the data was 12.34 to 31.87 MPa and got
                # scaled to 1.2e-05. convert_stat_unit has carried the
                # plausibility guard for exactly this since the flow-front and
                # shear-stress double conversions; the fix is to stop having a
                # second, unguarded copy of the conversion rather than to add
                # another special case here.
                stat = {"min": lo, "max": hi, "mean": (lo + hi) / 2.0,
                        "std": 0.0}
                try:
                    conv, disp = convert_stat_unit(stat, unit)
                    lo, hi, unit = conv["min"], conv["max"], disp
                except Exception:
                    pass                    # keep the raw values and unit

                def _n(v):
                    # Thousands-separated for large magnitudes so the value
                    # reads like the reference targets ("24,000 1/s") instead
                    # of drifting into scientific notation.
                    return ("{0:,.0f}".format(v) if abs(v) >= 1000
                            else "{0:.4g}".format(v))
                if unit:
                    return "{0} to {1} {2}".format(_n(lo), _n(hi), unit)
                return "{0} to {1}".format(_n(lo), _n(hi))

            def plot_dataset_id(pl):
                """Dataset id behind an already-instantiated plot, else 0."""
                for m in ("GetDataID", "GetDataId"):
                    try:
                        f = getattr(pl, m)
                        v = f() if callable(f) else f
                        if v:
                            return int(v)
                    except Exception:
                        pass
                return 0

            # Display every required result that EXISTS: prefer a plot Moldflow
            # already made (no duplicate); otherwise create it from its dataset.
            # Only skip results whose dataset is genuinely absent (wrong sequence)
            # — so no grey phantoms, but nothing real is missed.
            shown = []
            missing = []
            # (label, plot object, real plot name) for every result actually
            # displayed — drives the exported report so the document and the
            # summary dialog can never disagree.
            shown_plots = []
            # label -> extracted 'min to max unit' string, when readable.
            result_values = {}
            # sub-result label -> the headline result it belongs under, so the
            # report can nest it inside that result's section.
            parent_of = {}

            def show_result(label, candidates):
                """Resolve ONE result and display it; True if this study
                actually produced it. The resolution logic is unchanged — it
                is a function now so the reference deck's supporting
                investigations can reuse it underneath their parent result."""
                # 1) An already-instantiated plot?
                for c in candidates:
                    for pl, nm in existing:
                        if matches(nm, c):
                            try:
                                viewer.ShowPlot(pl)
                            except Exception:
                                pass
                            shown.append(label)
                            shown_plots.append((label, pl, nm))
                            rng = dataset_range(plot_dataset_id(pl), label)
                            if rng:
                                result_values[label] = rng
                            log("Displayed '{0}' (existing plot '{1}'){2}.".format(
                                label, nm, " [{0}]".format(rng) if rng else ""))
                            return True
                # 2) Not instantiated — create it from its dataset if it exists.
                for c in candidates:
                    did = dataset_id_for(c)
                    if did <= 0:
                        continue
                    # Exact id first (no fuzzy-name duplicates), then by name.
                    plot = None
                    for is_xy in (0, 1):
                        try:
                            plot = plot_mgr.CreatePlotByDsID(did, is_xy)
                            if plot is not None:
                                break
                        except Exception as pe:
                            log("  CreatePlotByDsID({0}, {1}) failed: {2}".format(did, is_xy, pe))
                    if plot is None:
                        for is_xy in (False, True):
                            try:
                                plot = plot_mgr.CreatePlotByName(c, is_xy)
                                if plot is not None:
                                    break
                            except Exception as pe:
                                log("  CreatePlotByName('{0}', {1}) failed: {2}".format(c, is_xy, pe))
                    if plot is not None:
                        try:
                            viewer.ShowPlot(plot)
                        except Exception:
                            pass
                        shown.append(label)
                        shown_plots.append((label, plot, c))
                        rng = dataset_range(did, label)
                        if rng:
                            result_values[label] = rng
                        log("Displayed '{0}' (created from dataset {1}, '{2}'){3}.".format(
                            label, did, c, " [{0}]".format(rng) if rng else ""))
                        return True
                return False

            # --- paced presentation ------------------------------------------
            # show_result() puts a plot on screen and returns immediately; the
            # loop below then dwells on it so the user actually sees it, and
            # tells the panel which result is up. Nothing is captured here.
            presentation = {"skipped": False, "index": 0,
                            "total": len(result_specs), "shown": []}

            def presentation_dwell(label):
                """Hold the current plot on screen for RESULT_PRESENTATION_DWELL
                seconds, updating the panel card and honouring 'Skip to
                review'. Returns immediately once the user has skipped."""
                presentation["index"] += 1
                presentation["shown"].append(label)
                # Supporting investigations are shown under their parent and
                # are not in result_specs, so the total grows as they appear
                # rather than the counter running past it ("13 of 12").
                presentation["total"] = max(presentation["total"],
                                            presentation["index"])
                if presentation["skipped"]:
                    return
                try:
                    import ui_bridge
                except Exception:
                    return
                have_card = ui_bridge.publish_live_card(
                    "result_presentation", "Presenting Results",
                    {"index": presentation["index"],
                     "total": presentation["total"],
                     "label": label,
                     "shown": presentation["shown"]},
                    ["Skip to review"])
                deadline = time.time() + RESULT_PRESENTATION_DWELL
                while time.time() < deadline:
                    if have_card and ui_bridge.poll_live_card("result_presentation"):
                        presentation["skipped"] = True
                        log("User skipped the result presentation at '{0}'.".format(label))
                        ui_bridge.clear_live_card("result_presentation")
                        return
                    time.sleep(RESULT_PRESENTATION_POLL)

            for label, candidates in result_specs:
                if not show_result(label, candidates):
                    missing.append(label)
                    log("Result '{0}' not present in this study (no dataset).".format(label))
                    continue
                presentation_dwell(label)
                # The deck's further investigations for this headline result.
                # They are only meaningful as part of its review, so they are
                # attempted only when the parent itself was produced, and one
                # that this study did not produce is simply skipped (it is not
                # a missing headline result).
                for sub_label, sub_cands in SUB_SPECS.get(label, []):
                    if show_result(sub_label, sub_cands):
                        parent_of[sub_label] = label
                        presentation_dwell(sub_label)
                    else:
                        log("  Supporting investigation '{0}' not produced by "
                            "this study; skipping.".format(sub_label))

            try:
                import ui_bridge
                ui_bridge.clear_live_card("result_presentation")
            except Exception:
                pass

            # -------------------------------------------------------------
            # Export one Synergy image per displayed result and assemble them
            # into a Word report. Uses the NATIVE Viewer.SaveImage2 export
            # (true off-screen render at the requested resolution) rather than
            # a desktop screen-grab, so the images are clean, correctly sized
            # and unaffected by window occlusion or the user's screen.
            # -------------------------------------------------------------
            def study_label():
                for getter in ("StudyName", "StudyPath"):
                    try:
                        v = str(getattr(study_doc, getter)() or "").strip()
                        if v:
                            return Path(v).stem if getter == "StudyPath" else v
                    except Exception:
                        pass
                return "Moldflow study"

            def prepare_capture_dir(out_dir, width, height, resize_now=True):
                """Shared set-up for both capture paths: the image folder and
                the export viewport. Returns a _ViewFramer, or None if the
                folder could not be created.

                `resize_now=False` is for the interactive path, which resizes
                only around each capture — the user spends most of the review
                driving this viewport by hand and should not have it stretched
                to the export resolution the whole time."""
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    log("Could not create image folder {0}: {1}".format(out_dir, e))
                    return None
                # Bigger viewport = higher-fidelity export. Set BEFORE any
                # fitting: Fit() frames to the viewport's ASPECT RATIO, so
                # fitting first and resizing after is itself a source of the
                # cropped, off-centre exports this rework is fixing.
                if resize_now:
                    try:
                        viewer.SetViewSize(width, height)
                    except Exception:
                        pass
                return _ViewFramer(viewer, log, width, height, out_dir)

            def current_view_size():
                """(x, y) of the live viewport, or None."""
                try:
                    return int(viewer.ViewSizeX), int(viewer.ViewSizeY)
                except Exception:
                    return None

            def plot_frame_count(pl, nm):
                """Frames in a plot, or None when it cannot be determined.

                A single frame means there is nothing to animate — a clamp
                force XY trace, a weld-line map. `None` is deliberately NOT
                treated as 1: failing to read the count is no reason to drop a
                genuine animation, so the export is attempted anyway."""
                for m in ("GetNumberOfFrames", "GetNumberFrames"):
                    try:
                        f = getattr(pl, m)
                        v = f() if callable(f) else f
                        if v is not None:
                            return int(v)
                    except Exception:
                        pass
                # Viewer fallback. This one reports through a BYREF out
                # parameter rather than a return value, so it needs a real
                # VARIANT — win32com cannot synthesise one from a plain int.
                try:
                    import pythoncom
                    from win32com.client import VARIANT
                    out = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                    viewer.GetNumberFramesByName(nm, out)
                    return int(out.value)
                except Exception:
                    return None

            def export_result_animation(lbl, pl, nm, out_dir, idx):
                """Export the animation for the CURRENTLY DISPLAYED result.

                Called with the view already framed, so the video shares the
                screenshot's framing. Returns the path or None — a result with
                a single frame, or a Synergy build without the animation
                exporter, simply contributes no video and is not an error."""
                if not EXPORT_ANIMATIONS:
                    return None
                frames = plot_frame_count(pl, nm)
                if frames is not None and frames <= 1:
                    log("  '{0}' has {1} frame(s) — no animation to export.".format(
                        lbl, frames))
                    return None
                if frames is None:
                    log("  Frame count for '{0}' is unreadable — attempting the "
                        "animation export anyway.".format(lbl))
                safe = re.sub(r"[^A-Za-z0-9]+", "_", lbl).strip("_") or "result"
                vid = out_dir / "{0:02d}_{1}.mp4".format(idx, safe)
                try:
                    vid.unlink()
                except Exception:
                    pass
                aw, ah = ANIMATION_SIZE

                def written():
                    try:
                        return vid.exists() and vid.stat().st_size > 0
                    except Exception:
                        return False

                # Preferred: the options-based exporter, which lets us pin the
                # resolution and suppress Synergy's own progress prompts (a
                # modal prompt here would stall the whole review). Like
                # ImageExportOptions, AnimationExportOptions is a METHOD on this
                # build, so resolve method-or-property before using it.
                try:
                    opts = _com_object_or_call(
                        viewer.AnimationExportOptions, "SizeX")
                    if opts is None:
                        raise RuntimeError("AnimationExportOptions unresolved")
                    opts.FileName = str(vid)
                    opts.AnimationSpeed = ANIMATION_SPEED
                    opts.ShowPrompts = False
                    opts.SizeX, opts.SizeY = aw, ah
                    viewer.SaveAnimation4(opts)
                except Exception as e:
                    log("  Animation (options API) unavailable for '{0}': {1}".format(lbl, e))
                if not written():
                    try:
                        viewer.SaveAnimation3(str(vid), ANIMATION_SPEED, False)
                    except Exception as e:
                        log("  SaveAnimation3 failed for '{0}': {1}".format(lbl, e))
                if not written():
                    log("  No animation was written for '{0}'.".format(lbl))
                    return None
                log("  Exported animation for '{0}' -> {1}".format(lbl, vid.name))
                return vid

            def capture_displayed_result(lbl, pl, nm, out_dir, framer, idx,
                                         width, height, preserve_orientation,
                                         manage_view_size=False):
                """Frame, screenshot and animate ONE result, in that order.

                This is the single capture routine — the unattended export and
                the interactive review both go through it, so a deck built
                either way is identical in every respect except the viewing
                angle the user chose.

                `manage_view_size=True` sizes the viewport to the export
                resolution just for this capture and puts it back afterwards."""
                restore = None
                if manage_view_size:
                    restore = current_view_size()
                    try:
                        viewer.SetViewSize(width, height)
                    except Exception:
                        restore = None
                # Ask the plot itself for its min/max annotation before the
                # exporter is told to draw one, so both halves agree.
                enable_plot_minmax(pl, log, lbl)
                safe = re.sub(r"[^A-Za-z0-9]+", "_", lbl).strip("_") or "result"
                img = out_dir / "{0:02d}_{1}.png".format(idx, safe)
                vid = None
                try:
                    try:
                        if framer is not None:
                            status = framer.frame(preserve_orientation)
                            log("  '{0}': view {1}.".format(lbl, status))
                            # Export through the framer so FitToScreen stays OFF
                            # and the framed camera survives into the file. A
                            # plain SaveImage2 here would re-fit and undo the
                            # framing, re-introducing the legend overlap.
                            framer.save_final(img)
                        else:
                            viewer.SaveImage2(str(img), width, height)
                    except Exception as e:
                        log("  Image export failed for '{0}': {1}".format(lbl, e))
                        return None, None
                    if img.exists() and img.stat().st_size > 0:
                        log("  Exported image for '{0}' -> {1}".format(lbl, img.name))
                    else:
                        log("  Image for '{0}' was not written.".format(lbl))
                        img = None
                    vid = export_result_animation(lbl, pl, nm, out_dir, idx)
                    return img, vid
                finally:
                    if restore:
                        try:
                            viewer.SetViewSize(restore[0], restore[1])
                        except Exception:
                            pass

            def export_result_images(items, out_dir, width=CAPTURE_IMAGE_SIZE[0],
                                     height=CAPTURE_IMAGE_SIZE[1]):
                """Unattended capture of every displayed result, in order.

                Behaviour is unchanged from the original export apart from the
                two fixes the framing work brought: the view is now VERIFIED
                clear of the legend before each shot, and an animation is
                exported alongside each multi-frame result.

                Returns [(label, plot_name, image_or_None, video_or_None)]."""
                framer = prepare_capture_dir(out_dir, width, height)
                if framer is None:
                    return [(lbl, nm, None, None) for lbl, _pl, nm in items]

                captured = []
                for idx, (lbl, pl, nm) in enumerate(items, 1):
                    try:
                        viewer.ShowPlot(pl)
                    except Exception:
                        pass
                    img, vid = capture_displayed_result(
                        lbl, pl, nm, out_dir, framer, idx, width, height, False)
                    captured.append((lbl, nm, img, vid))
                return captured

            def interactive_result_review(available, out_dir,
                                          width=CAPTURE_IMAGE_SIZE[0],
                                          height=CAPTURE_IMAGE_SIZE[1]):
                """Let the user choose which results go in the report, then
                capture exactly those.

                Two ways in, one list. The panel shows a Review Results card
                holding every result this study produced, in Results-tree
                order, each with a tick; ticking one asks this loop to display
                it in Synergy and open its F1 help. Meanwhile this watches
                ``Viewer.ActivePlot``, so a result the user opens directly in
                Synergy's own tree is recorded and ticked here too. The stage
                ends when the user clicks Export & Report.

                Without the panel it degrades to the original behaviour: an
                always-on-top message box on a background thread (so Synergy
                and this loop stay live) carrying OK / Cancel, with everything
                the user opened captured.

                On Export & Report every ticked result is captured through the
                SAME capture_displayed_result path as the unattended export —
                framed clear of the legend, screenshotted and animated — so the
                deck is identical in kind to the automatic one, just limited to
                what the user chose.

                Returns [(label, plot_name, image_or_None, video_or_None)], or
                None if the review could not be started so the caller can fall
                back to the unattended path. An empty list means the user
                cancelled or reviewed nothing.
                """
                import threading
                import ctypes

                framer = prepare_capture_dir(out_dir, width, height,
                                             resize_now=False)
                if framer is None:
                    return None

                # Resolved Top-12 by plot name, so a reviewed result that is one
                # of them carries its proper report label (and, later, its guide
                # text and value range). Anything else the user opens is still
                # captured, under its own Synergy plot name.
                by_name = {}
                for lbl, pl, nm in available:
                    if nm:
                        by_name[nm.strip().lower()] = lbl

                # Stable plot references straight from the Results tree
                # enumeration. The plot returned by ActivePlot() is only used to
                # learn WHICH result is on screen; for the actual capture we
                # re-show the enumerated plot of the same name, because those are
                # the same references the resolve loop displays reliably.
                plot_by_name = {}
                for _pl, _nm in existing:
                    if _nm:
                        plot_by_name.setdefault(_nm.strip().lower(), _pl)

                def label_for(nm):
                    key = (nm or "").strip().lower()
                    if key in by_name:
                        return by_name[key]
                    for lbl, cands in result_specs:
                        for c in cands:
                            if matches(nm, c):
                                return lbl
                    for _parent, subs in SUB_SPECS.items():
                        for sub_lbl, cands in subs:
                            for c in cands:
                                if matches(nm, c):
                                    return sub_lbl
                    return nm or "Result"

                # Viewer.ActivePlot is exposed as a METHOD in this API
                # (`$Viewer.ActivePlot()`), but some builds also expose it as a
                # property. Under late-bound COM, reading it as a property when
                # it is a method hands back the method object itself, not the
                # plot — which is exactly why an earlier version silently
                # captured nothing. So resolve it defensively: whatever comes
                # back, use it only if it actually answers GetName(); otherwise
                # call it and use that. `active_plot_diag` records the outcome
                # once so a future failure is never silent again.
                active_plot_diag = {"logged": False}

                def _has_name(obj):
                    try:
                        obj.GetName()
                        return True
                    except Exception:
                        return False

                def active_plot():
                    try:
                        raw = viewer.ActivePlot
                    except Exception as e:
                        if not active_plot_diag["logged"]:
                            active_plot_diag["logged"] = True
                            log("  Could not read Viewer.ActivePlot: {0}".format(e))
                        return None
                    if raw is None:
                        return None
                    if _has_name(raw):
                        return raw
                    try:
                        called = raw()
                    except Exception as e:
                        if not active_plot_diag["logged"]:
                            active_plot_diag["logged"] = True
                            log("  Viewer.ActivePlot did not resolve to a plot: "
                                "{0}".format(e))
                        return None
                    return called if (called is not None and _has_name(called)) else None

                def plot_nm(pl):
                    try:
                        return str(pl.GetName() or "")
                    except Exception:
                        return ""

                # Clear the viewport so the user's FIRST tree selection reads as
                # a change. Without this, whatever the resolve loop left on
                # screen would be indistinguishable from a deliberate pick, and
                # re-selecting it (already active) would raise no change at all.
                start = active_plot()
                if start is not None:
                    try:
                        viewer.HidePlot(start)
                    except Exception:
                        pass

                visited = []        # ordered [(label, plot, name)], first seen
                seen = set()
                signal = {"done": False, "result": 1}   # 1 = OK, 2 = Cancel
                # Labels the user left ticked on the Export & Report card. None
                # means "the card never answered" -> capture everything visited,
                # which is the behaviour this stage had before the card existed.
                selected_labels = {"value": None}
                # Highest focus_request the card has issued and this loop has
                # already serviced, so one tick opens F1 exactly once.
                focus_seen = {"seq": 0}
                panel_misses = {"n": 0}
                # "url" opens Autodesk's help for the result in the browser;
                # "f1" presses F1 in Synergy. A failed F1 switches the rest of
                # the session to "url" so the failure box cannot recur.
                help_state = {"enabled": True, "mode": RESULT_HELP_MODE}

                # Preferred path: the panel's Review Results card. It lists every
                # result as the user opens it, each with a tick they can clear,
                # and carries the Export & Report button. It is a LIVE card --
                # published here, polled by the watch loop below -- because this
                # loop has to keep reading ActivePlot while it is on screen.
                try:
                    import ui_bridge as _bridge
                except Exception as _e:
                    _bridge = None
                    log("  ui_bridge unavailable ({0}); using the message-box "
                        "review prompt.".format(_e))

                # The card lists what this study PRODUCED, in the order the
                # Results tree shows it, so ticking on the card and clicking in
                # the tree are two ways into the same list. `available` is the
                # resolved Top-12 (plus supporting investigations); anything
                # else the user opens is appended by the panel.
                available_labels = [lbl for lbl, _pl, _nm in available]
                # One-line "what is this for" per result, straight from
                # RESULT_GUIDE, shown when the user ticks it.
                review_guide = {}
                for _lbl in available_labels:
                    _info = RESULT_GUIDE.get(_lbl) or {}
                    _txt = _info.get("purpose") or _info.get("description") or ""
                    if _txt:
                        review_guide[_lbl] = _txt

                def review_card_available():
                    if _bridge is None:
                        return False
                    return _bridge.publish_live_card(
                        "result_review", "Review Results",
                        {"available": available_labels,
                         "visited": [lbl for lbl, _p, _n in visited],
                         "guide": review_guide,
                         "message": "Tick the results you want, then click "
                                    "Export & Report."},
                        ["Export & Report", "Cancel"])

                def open_result_help(label):
                    """Show `label` in Synergy and open its F1 help.

                    There is no help/advisor call anywhere in the Synergy API
                    (checked the full member list in the extracted CHM), so F1
                    has to be a real keystroke to Synergy's own window: focus
                    it, then send the key. Best-effort by nature -- Synergy
                    decides what its context help shows -- which is why the
                    card also carries the plugin's own guide text, and why a
                    failure here only logs."""
                    pl = None
                    nm = ""
                    for _lbl, _pl, _nm in available:
                        if _lbl == label:
                            pl, nm = _pl, _nm
                            break
                    if pl is None:
                        pl = plot_by_name.get((label or "").strip().lower())
                    if pl is not None:
                        try:
                            viewer.ShowPlot(pl)
                            log("  Ticked '{0}' — displayed in Synergy.".format(label))
                        except Exception as e:
                            log("  Could not display '{0}': {1}".format(label, e))
                    if not help_state["enabled"]:
                        return

                    # A failure seen anywhere in this session (the hook reports
                    # it) demotes F1 mode to the browser for good.
                    if _help_broken["v"] and help_state["mode"] != "url":
                        help_state["mode"] = "url"
                        log("  Switching result help to the browser after "
                            "Synergy failed to launch its own help.")

                    if help_state["mode"] == "url":
                        # Straight to the browser. Synergy's F1 on this install
                        # goes: try local help -> fail -> show "Failed to launch
                        # help." -> open the browser anyway. The box is created
                        # by Synergy itself, so the hook can only close it after
                        # the fact and a flash always remains. Skipping the
                        # broken step removes the box entirely and lands on the
                        # same help page.
                        try:
                            import webbrowser
                            # The result's OWN topic page, resolved from the
                            # Moldflow plot name. Previously this formatted the
                            # display label into a `?query=` search URL, so F1
                            # landed on the search-results list rather than on
                            # the result's help page.
                            url, direct = result_help_url(nm, label)
                            webbrowser.open(url, new=0, autoraise=True)
                            if direct:
                                log("  Help topic opened for '{0}' (plot '{1}'): "
                                    "{2}".format(label, nm or "?", url))
                            else:
                                log("  No mapped help topic for '{0}' (plot "
                                    "'{1}') — falling back to search: {2}".format(
                                        label, nm or "?", url))
                        except Exception as e:
                            log("  Could not open help for '{0}': {1}".format(label, e))
                        return

                    if _send_f1_to_synergy(log):
                        log("  F1 help requested for '{0}'.".format(label))
                        # Belt and braces while in F1 mode: the hook normally
                        # gets the failure box first, and this sweep covers the
                        # case where the hook could not be installed. On its own
                        # thread -- when help DOES launch there is nothing to
                        # find and an inline sweep would stall the card.
                        threading.Thread(target=sweep_dialogs_now, args=(log,),
                                         daemon=True).start()

                # Drop any focus request left behind by a previous run before
                # the card goes up.
                if _bridge is not None:
                    _bridge.clear_result_focus()
                use_card = review_card_available()

                def wait_for_click():
                    # Background thread. MessageBoxW pumps its own messages and
                    # touches no COM, so it is safe off the main thread and
                    # leaves Synergy fully interactive while it waits.
                    try:
                        import ui_bridge
                        ans = ui_bridge.prompt_user(
                            "Generate Report",
                            "Interactive result review:\n\n"
                            "Select each result you want in the report from the Synergy Results tree. "
                            "Rotate, zoom, pan or inspect each result as you go.\n\n"
                            "Click OK when you have opened every result you want added to PowerPoint.\n"
                            "Click Cancel to stop without building the report.",
                            options=["OK", "Cancel"]
                        )
                        signal["result"] = 1 if ans == "OK" else 2
                    except Exception:
                        try:
                            signal["result"] = ctypes.windll.user32.MessageBoxW(
                                None,
                                "Interactive result review\n\n"
                                "Select each result you want in the report from the "
                                "Synergy Results tree, one at a time. Rotate, zoom, "
                                "pan or press F1 to inspect each result as you go - "
                                "the automation waits for you.\n\n"
                                "Click OK when you have opened every result you "
                                "want. Each result you viewed will then be fitted, "
                                "captured and added to the PowerPoint (the one on "
                                "screen last is included too).\n\n"
                                "Click Cancel to stop without building the report.",
                                "Generate Report",
                                0x00050001)   # OKCANCEL | TOPMOST | SETFOREGROUND
                        except Exception:
                            signal["result"] = 1
                    signal["done"] = True

                if not use_card:
                    # No panel: fall back to the message-box prompt on its own
                    # thread, exactly as before.
                    threading.Thread(target=wait_for_click, daemon=True).start()
                log("Interactive review started - select results in the Synergy "
                    "Results tree, then click Export & Report.")

                last_key = None
                while not signal["done"]:
                    pl = active_plot()
                    nm = plot_nm(pl) if pl is not None else ""
                    key = nm.strip().lower()
                    if key and key != last_key:
                        last_key = key
                        if key not in seen:
                            seen.add(key)
                            lbl = label_for(nm)
                            visited.append((lbl, pl, nm))
                            log("  Reviewing '{0}' (plot '{1}').".format(lbl, nm))
                            # Card shows the new result immediately, so the user
                            # can see exactly what will be exported.
                            if use_card:
                                review_card_available()
                    if use_card and not _bridge.ui_alive():
                        # The panel went away mid-review. Without this the loop
                        # would spin forever waiting for a click on a card that
                        # no longer exists, so hand over to the message-box
                        # prompt and keep what the user reviewed so far.
                        panel_misses["n"] += 1
                        if panel_misses["n"] >= 5:
                            log("Review card lost (panel gone) - switching to the "
                                "message-box prompt.")
                            use_card = False
                            threading.Thread(target=wait_for_click,
                                             daemon=True).start()
                    elif use_card:
                        panel_misses["n"] = 0

                    if use_card:
                        # A tick on the card asks for that result to be shown
                        # and its F1 help opened. Serviced here because only
                        # this process has the COM connection.
                        try:
                            _req = _bridge.poll_result_focus(focus_seen["seq"])
                            if _req:
                                _seq, _lbl = _req
                                focus_seen["seq"] = _seq
                                if _lbl:
                                    open_result_help(_lbl)
                                    # Ticking counts as reviewing it, so the
                                    # result joins the capture set even if the
                                    # user never opens it from the tree.
                                    _key, _name = None, ""
                                    for _l, _p, _n in available:
                                        if _l == _lbl:
                                            _name = _n or ""
                                            _key = _name.strip().lower()
                                            break
                                    if _key and _key not in seen:
                                        seen.add(_key)
                                        visited.append(
                                            (_lbl, plot_by_name.get(_key), _name))
                                        last_key = _key
                                        review_card_available()
                        except Exception as _fe:
                            log("  Focus request handling failed: {0}".format(_fe))

                        ans = _bridge.poll_live_card("result_review")
                        if ans is not None:
                            action = (ans.get("action") if isinstance(ans, dict)
                                      else str(ans))
                            if isinstance(ans, dict):
                                selected_labels["value"] = ans.get("selected") or []
                            signal["result"] = 2 if action == "CANCEL" else 1
                            signal["done"] = True
                            _bridge.clear_live_card("result_review")
                            break
                    time.sleep(0.3)

                if use_card:
                    _bridge.clear_live_card("result_review")

                if signal["result"] == 2:
                    log("Interactive review cancelled by the user - no report.")
                    return []

                # Honour the ticks: only the results the user checked are
                # captured. Opening a result in Synergy's tree ticks it, so
                # browsing alone is still enough to build a report.
                if selected_labels["value"] is not None:
                    keep = set(selected_labels["value"])
                    dropped = [lbl for lbl, _p, _n in visited if lbl not in keep]
                    visited = [item for item in visited if item[0] in keep]
                    if dropped:
                        log("  Unticked on the review card, not exported: {0}".format(
                            ", ".join(dropped)))

                if not visited:
                    log("Interactive review finished but no result was selected; "
                        "nothing to capture.")
                    return []

                # Batch-capture the reviewed set, exactly like the unattended
                # export: one enlarged viewport, one framed screenshot and one
                # animation per result, in the order the user visited them.
                log("Capturing {0} reviewed result(s) for the report...".format(
                    len(visited)))
                try:
                    viewer.SetViewSize(width, height)
                except Exception:
                    pass
                captured = []
                for idx, (lbl, pl, nm) in enumerate(visited, 1):
                    # Keep the user informed while this runs: each result costs
                    # a framed screenshot plus an animation export, so a silent
                    # panel here reads as a hang.
                    if _bridge is not None:
                        _bridge.publish_live_card(
                            "result_presentation", "Exporting Results",
                            {"index": idx, "total": len(visited), "label": lbl,
                             "shown": [l for l, _p, _n in visited[:idx - 1]]},
                            [])
                    # Prefer the enumerated tree reference of this name; fall
                    # back to the plot ActivePlot handed us during the review.
                    pl = plot_by_name.get(nm.strip().lower(), pl)
                    try:
                        viewer.ShowPlot(pl)
                    except Exception:
                        pass
                    # Value range for the report, if this result is not one the
                    # resolve loop already measured.
                    if lbl not in result_values:
                        try:
                            rng = dataset_range(plot_dataset_id(pl), lbl)
                            if rng:
                                result_values[lbl] = rng
                        except Exception:
                            pass
                    img, vid = capture_displayed_result(
                        lbl, pl, nm, out_dir, framer, idx, width, height, False)
                    captured.append((lbl, nm, img, vid))
                if _bridge is not None:
                    _bridge.clear_live_card("result_presentation")
                log("Interactive review finished - {0} result(s) captured.".format(
                    len(captured)))
                return captured

            def build_pptx_report(captured, missing_labels, deck_path,
                                  summary_data=None, ai_meta=None,
                                  study_name=None, assistant=None,
                                  component=None, process=None,
                                  mesh_report=None):
                """Assemble the exported Synergy images — and the animations
                exported beside them — into a .pptx review deck. Returns the
                path, or None.

                `captured` is [(label, plot_name, image_or_None,
                video_or_None)].

                This is the report's PRESENTATION LAYER only — result
                selection, filtering, screenshots, values and the engineering
                narrative are all produced upstream and simply rendered here.

                python-pptx is OPTIONAL: a missing package or any failure in
                this function must never break the workflow, which has already
                completed by the time it runs."""
                try:
                    from pptx import Presentation
                    from pptx.util import Inches, Pt, Emu
                    from pptx.dml.color import RGBColor
                    from pptx.enum.shapes import MSO_SHAPE
                    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
                except ImportError:
                    log("python-pptx not installed — skipping the PowerPoint "
                        "report. Install it with: "
                        ".venv\\Scripts\\python.exe -m pip install python-pptx")
                    return None

                # Neutral, technical chrome. Moldflow plots are full-spectrum
                # rainbow, so the deck itself stays achromatic and lets the
                # screenshots carry the colour.
                INK = RGBColor(0x1F, 0x29, 0x37)      # headings, cover ground
                BODY = RGBColor(0x37, 0x41, 0x51)     # body copy
                MUTED = RGBColor(0x6B, 0x72, 0x80)    # captions
                ACCENT = RGBColor(0x0F, 0x76, 0x6E)   # field labels only
                PAPER = RGBColor(0xFF, 0xFF, 0xFF)
                PANEL = RGBColor(0xF4, 0xF5, 0xF7)    # text-column tint
                FONT = "Calibri"

                SW, SH = Inches(13.333), Inches(7.5)   # 16:9

                def txbox(slide, x, y, w, h):
                    box = slide.shapes.add_textbox(x, y, w, h)
                    tf = box.text_frame
                    tf.word_wrap = True
                    tf.margin_left = tf.margin_right = 0
                    tf.margin_top = tf.margin_bottom = 0
                    return tf

                def para(tf, first, text="", size=11, bold=False, color=BODY,
                         space_before=0, space_after=4, bullet=False,
                         italic=False):
                    p = tf.paragraphs[0] if first else tf.add_paragraph()
                    p.space_before = Pt(space_before)
                    p.space_after = Pt(space_after)
                    r = p.add_run()
                    r.text = ("•  " + text) if bullet else text
                    f = r.font
                    f.name, f.size, f.bold, f.italic = FONT, Pt(size), bold, italic
                    f.color.rgb = color
                    return p

                def label_value(tf, first, label, value, size=11):
                    """'Label: value' with the label in the accent colour."""
                    p = tf.paragraphs[0] if first else tf.add_paragraph()
                    p.space_before = Pt(6)
                    p.space_after = Pt(2)
                    r = p.add_run()
                    r.text = label
                    r.font.name, r.font.size, r.font.bold = FONT, Pt(size), True
                    r.font.color.rgb = ACCENT
                    r2 = p.add_run()
                    r2.text = value
                    r2.font.name, r2.font.size = FONT, Pt(size)
                    r2.font.color.rgb = BODY
                    return p

                def fit_image(path, max_w, max_h):
                    """Image size in EMU that fills the box without distortion."""
                    ratio = 4.0 / 3.0            # SaveImage2 exports 1600x1200
                    try:
                        from PIL import Image
                        with Image.open(str(path)) as im:
                            if im.height:
                                ratio = float(im.width) / float(im.height)
                    except Exception:
                        pass
                    w, h = max_w, int(max_w / ratio)
                    if h > max_h:
                        h, w = max_h, int(max_h * ratio)
                    return int(w), int(h)

                logo_path = None
                for _lp in [Path(HERE) / "logo.png", Path(HERE) / "logo.png.png", Path(HERE) / "logo.jpg"] + list(Path(HERE).glob("*logo*")):
                    if _lp.is_file() and _lp.suffix.lower() in ('.png', '.jpg', '.jpeg'):
                        logo_path = _lp
                        break

                def add_footer_logo(slide):
                    if not logo_path or not logo_path.is_file():
                        return
                    try:
                        max_w, max_h = Inches(1.8), Inches(0.42)
                        lw, lh = fit_image(logo_path, max_w, max_h)
                        lx = int(SW - Inches(0.55) - lw)
                        ly = int(SH - Inches(0.12) - lh)
                        slide.shapes.add_picture(str(logo_path), lx, ly, width=lw, height=lh)
                    except Exception as le:
                        log("  Could not place logo on slide: {0}".format(le))

                try:
                    prs = Presentation()
                    prs.slide_width, prs.slide_height = SW, SH
                    blank = prs.slide_layouts[6]

                    head_done = [l for l, _n, _i, _v in captured if l not in parent_of]
                    subs_done = [l for l, _n, _i, _v in captured if l in parent_of]
                    # parent -> its supporting investigations that made the deck
                    children = {}
                    for l in subs_done:
                        children.setdefault(parent_of[l], []).append(l)

                    # ---- results, grouped into the reference report's phases ----
                    # A supporting investigation takes its parent's phase, so it
                    # stays in the same section as the result it belongs to.
                    # Order WITHIN a phase is exactly the order `captured`
                    # already had; only the phases themselves are ordered, and
                    # only when there is more than one of them. A single-phase
                    # run (a Fill study, say) therefore produces precisely the
                    # deck it produced before this grouping existed.
                    def _phase_of(label):
                        return PHASE_OF.get(parent_of.get(label, label),
                                            "fill_pack")

                    phase_buckets = {}
                    for _entry in captured:
                        phase_buckets.setdefault(
                            _phase_of(_entry[0]), []).append(_entry)
                    phase_order = [(k, t, sub) for k, t, sub in PHASE_SECTIONS
                                   if phase_buckets.get(k)]
                    # Defensive: a phase key that is not in PHASE_SECTIONS would
                    # otherwise drop its results off the deck entirely.
                    for _k in phase_buckets:
                        if not any(_k == k for k, _t, _s in phase_order):
                            phase_order.append(
                                (_k, "Engineering results",
                                 "The Top 12 results this study produced"))
                    multi_phase = len(phase_order) > 1

                    # The presentation layer for the slides that carry no
                    # measured data of their own -- cover, contents, section
                    # dividers, study setup, points to highlight. Optional by
                    # design: if it cannot be imported the deck falls back to
                    # the plain cover below and every other slide is unchanged.
                    try:
                        import report_style as style
                    except Exception as se:
                        style = None
                        log("  Deck styling unavailable ({0}); using the plain "
                            "cover.".format(se))

                    # ---------------- cover ----------------
                    s = prs.slides.add_slide(blank)
                    exported = "{0} of the Top 12{1}".format(
                        len(head_done),
                        ", plus {0} supporting investigation{1}".format(
                            len(subs_done),
                            "" if len(subs_done) == 1 else "s")
                        if subs_done else "")
                    footnote = ("Result categories, interpretation and targets "
                                "per Autodesk, “Top 12 Results to View from a "
                                "Cool + Flow + Warp Simulation”.")
                    if style is not None:
                        style.cover(
                            s, study_label(),
                            "Engineering review of the Top 12 results",
                            [("Analysis sequence", seq_display),
                             ("Material", material_name or "(not identified)"),
                             ("Mesh type", mesh_type_name or "(not identified)"),
                             ("Generated",
                              datetime.datetime.now().strftime("%d %b %Y, %H:%M")),
                             ("Results exported", exported)],
                            footnote=footnote,
                            prepared_by="Generated automatically from the open "
                                        "Moldflow study")
                        # No footer logo on the dark slides: the wordmark's
                        # strapline is near-black and vanishes into the ground.
                        # (The old cover called it anyway, then drew the dark
                        # panel over the top, so nothing is lost here.)
                    else:
                        add_footer_logo(s)
                        bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, SH)
                        bg.fill.solid()
                        bg.fill.fore_color.rgb = INK
                        bg.line.fill.background()
                        bg.shadow.inherit = False
                        bg.text_frame.text = ""

                        tf = txbox(s, Inches(0.9), Inches(1.5),
                                   Inches(11.5), Inches(1.6))
                        para(tf, True, "Moldflow Analysis Results", size=40,
                             bold=True, color=PAPER, space_after=6)
                        para(tf, False,
                             "Engineering review of the Top 12 results",
                             size=16, color=RGBColor(0x9C, 0xA3, 0xAF))

                        tf = txbox(s, Inches(0.9), Inches(3.5),
                                   Inches(11.5), Inches(3.0))
                        rows = [
                            ("Study:  ", study_label()),
                            ("Analysis sequence:  ", seq_display),
                            ("Material:  ", material_name or "(not identified)"),
                            ("Mesh type:  ", mesh_type_name or "(not identified)"),
                            ("Generated:  ",
                             datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
                            ("Engineering results exported:  ", exported),
                        ]
                        for i, (k, v) in enumerate(rows):
                            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                            p.space_after = Pt(9)
                            r = p.add_run()
                            r.text = k
                            r.font.name, r.font.size, r.font.bold = FONT, Pt(13), True
                            r.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
                            r2 = p.add_run()
                            r2.text = str(v)
                            r2.font.name, r2.font.size = FONT, Pt(13)
                            r2.font.color.rgb = PAPER

                        tf = txbox(s, Inches(0.9), Inches(6.7), Inches(9.5),
                                   Inches(0.5))
                        para(tf, True, footnote, size=10,
                             color=RGBColor(0x6B, 0x72, 0x80), italic=True)

                    # ------------- contents, and the study's inputs -------------
                    # Inserted slides only: nothing that follows moves relative
                    # to anything else, so the result/animation sequence below
                    # is exactly the sequence this deck has always had.
                    ai_notes = (assistant or {}).get("notes") or {}
                    # Section numbers are handed out as sections are actually
                    # built, so a skipped one does not leave a gap in the
                    # sequence -- and the contents list is assembled from the
                    # same facts, so it can never advertise a slide the deck
                    # does not contain.
                    section_no = [0]

                    def next_section():
                        section_no[0] += 1
                        return section_no[0]

                    if style is not None:
                        # What the study-setup card would say. Computed first
                        # because the contents list has to know whether that
                        # section is going to exist.
                        setup_rows, process_rows = [], []
                        try:
                            import ai_assistant as _aia
                            _m = _aia.summary(ai_meta) if ai_meta else {}
                        except Exception:
                            _m = {}
                        # The grade is known from the session even when the
                        # metadata extractor returns nothing, so the card is
                        # not left saying only "3D".
                        if _m.get("material_name"):
                            setup_rows.append(("Material", _m["material_name"]))
                        elif material_name:
                            setup_rows.append(("Material", material_name))
                        for label, key in (("Family", "material_family"),
                                           ("Fillers", "material_fillers")):
                            if _m.get(key):
                                setup_rows.append((label, _m[key]))
                        if mesh_type_name:
                            setup_rows.append(("Mesh type", mesh_type_name))
                        if seq_display:
                            setup_rows.append(("Analysis sequence", seq_display))
                        for label, key, unit in (
                                ("Melt temperature", "melt_temp", " °C"),
                                ("Mold surface temperature", "mold_temp", " °C"),
                                ("Machine", "machine_name", ""),
                                ("Max clamp force", "max_clamp_force", " tonne"),
                                ("Max injection pressure",
                                 "max_injection_pressure", " MPa")):
                            v = _m.get(key)
                            if v is None or v == "":
                                continue
                            if unit and isinstance(v, (int, float)):
                                v = "{0:g}{1}".format(v, unit)
                            process_rows.append((label, v))
                        # Only worth a section of its own if it actually says
                        # something. With the extractor unavailable this can be
                        # down to "3D" and "Fill", which the cover already
                        # carries -- and a two-line slide behind its own divider
                        # reads as a mistake, not as brevity.
                        have_setup = (len(setup_rows) + len(process_rows)) >= 4

                        # The FEA model the results were computed on, beside
                        # the part itself. These come from the mesh diagnostics
                        # the workflow already ran, so they cost nothing.
                        mesh_rows = []
                        if mesh_report:
                            _mvals = {it.get("name"): it.get("value")
                                      for it in (mesh_report.get("items") or [])}
                            if mesh_type_name:
                                mesh_rows.append(("Mesh type", mesh_type_name))
                            for _lbl, _key in (("Nodes", "NodesCount"),
                                               ("Tetrahedral elements",
                                                "TetrasCount"),
                                               ("Triangle elements",
                                                "TrianglesCount"),
                                               ("Beam elements", "BeamsCount")):
                                _v = _mvals.get(_key)
                                if _v:
                                    mesh_rows.append(
                                        (_lbl, "{0:,}".format(int(_v))))
                            _tot = mesh_report.get("total_elements")
                            if _tot:
                                mesh_rows.append(
                                    ("Total elements", "{0:,}".format(int(_tot))))

                        geometry_rows = (component or {}).get("geometry") or []
                        have_component = bool(geometry_rows or mesh_rows)

                        # "As configured" needs the process controller; "as
                        # achieved" is free, but a column on its own is not a
                        # slide -- without the settings there is nothing to
                        # compare it against, and the numbers are already on
                        # the result slides.
                        proc_conf_rows = (process or {}).get("configured") or []
                        proc_achieved_rows = process_achieved_rows(
                            summary_data, log)
                        have_process = bool(proc_conf_rows)

                        # The component, the setup card and the process
                        # conditions are all "what this study was", so they sit
                        # behind ONE divider rather than three.
                        have_inputs = have_setup or have_component or have_process

                        contents = []
                        if have_inputs:
                            _what = [w for w, ok in (
                                ("the part", have_component),
                                ("material and mesh", have_setup),
                                ("process conditions", have_process)) if ok]
                            contents.append(
                                ("Component and setup",
                                 ", ".join(_what).capitalize()))
                        if multi_phase:
                            for _pk, _ptitle, _psub in phase_order:
                                contents.append(
                                    (_ptitle,
                                     "{0} result{1} in this phase".format(
                                         len(phase_buckets[_pk]),
                                         "" if len(phase_buckets[_pk]) == 1
                                         else "s")))
                        else:
                            contents.append(
                                ("Engineering results",
                                 "{0} result{1}, each with its plot{2}".format(
                                     len(head_done),
                                     "" if len(head_done) == 1 else "s",
                                     " and animation" if any(
                                         v for _l, _n, _i, v in captured) else "")))
                        if subs_done:
                            contents.append(
                                ("Supporting investigations",
                                 ", ".join(sorted(set(subs_done))[:4])))
                        if missing_labels:
                            contents.append(
                                ("Results not produced",
                                 "What this sequence could not supply, and why"))
                        contents.append(
                            ("Analysis summary",
                             "Problematic results, key concerns and statistics"))
                        if (assistant or {}).get("summary", {}) is not None and \
                                ((assistant or {}).get("summary") or {}).get(
                                    "recommendations"):
                            contents.append(
                                ("Points to highlight",
                                 "Recommended actions before the next iteration"))
                        s = prs.slides.add_slide(blank)
                        add_footer_logo(s)
                        style.agenda(s, contents)

                        if have_inputs:
                            s = prs.slides.add_slide(blank)
                            style.divider(
                                s, next_section(), "Component and setup",
                                "The part, and the inputs this analysis was "
                                "run with")

                        # The part first: a reader who has not seen the
                        # component cannot judge a single plot that follows it.
                        if have_component:
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            style.component_details(
                                s, geometry_rows, mesh_rows,
                                notes=(component or {}).get("notes") or [])

                        if have_setup:
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            style.study_setup(
                                s, setup_rows, process_rows,
                                notes=["Read from the study file itself, so "
                                       "these are the conditions the solver "
                                       "actually used."])

                        if have_process:
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            style.process_parameters(
                                s, proc_conf_rows, proc_achieved_rows,
                                notes=["Settings read from the study's process "
                                       "controller; achieved values measured "
                                       "from this study's own results.",
                                       "\"Automatic\" means the solver chose "
                                       "the value rather than being given one."])

                        # With more than one phase each gets its own divider
                        # below, so this single heading would be a section that
                        # immediately contains another section.
                        if not multi_phase:
                            s = prs.slides.add_slide(blank)
                            style.divider(
                                s, next_section(), "Engineering results",
                                "The Top 12 results this study produced",
                                ["Each result is shown as exported from Synergy, "
                                 "with what it is for and the target to judge it "
                                 "against",
                                 "Animations follow the result they belong to"])

                    # ---- targets: THIS grade's limits, not a worked example ----
                    # RESULT_GUIDE quotes Autodesk's "Top 12 Results" reference,
                    # and some of its targets carry the figures from that
                    # document's worked example -- 24,000 1/s for shear rate,
                    # for instance. Printed beside a measurement from a
                    # different grade those are worse than no target at all:
                    # this study's POLYFLAM PP is rated to 100,000 1/s, so the
                    # reference figure made a compliant reading look like a 10x
                    # breach. Where the material supplies its own published
                    # limit, that is what the slide states, and the worked
                    # example is dropped rather than left to be misread.
                    mat_limits = {}
                    try:
                        import ai_assistant as _aia2
                        _ms = _aia2.summary(ai_meta) if ai_meta else {}
                        for _lbl, _key, _unit in (
                                ("Shear rate", "max_shear_rate", "1/s"),
                                ("Shear stress", "max_shear_stress", "MPa")):
                            _v = _ms.get(_key)
                            if _v:
                                mat_limits[_lbl] = ("{0:,.4g} {1}".format(
                                    float(_v), _unit), _ms.get("material_name"))
                    except Exception:
                        mat_limits = {}

                    def _target_for(label, info, limits):
                        """The target line for a result slide."""
                        text = (info or {}).get("targets") or ""
                        hit = limits.get(label)
                        if not hit:
                            return text
                        value, grade = hit
                        # Drop the reference document's worked example: the
                        # grade's own number supersedes it and printing both
                        # invites the reader to use the wrong one.
                        text = re.sub(
                            r"\s*The reference deck's worked example[^.]*\.", "",
                            text).strip()
                        own = "Not to exceed {0}, the published maximum for {1}.".format(
                            value, grade or "this grade")
                        return (own + " " + text).strip() if text else own

                    def phase_summary_slide(slide, phase_title, findings):
                        """Close a phase section with what its results said.

                        The reference report ends each phase this way ("Fill &
                        pack summary", "Cooling summary", "Warpage summary").
                        This is ADDITIVE: the deck's consolidated Analysis
                        Summary further on is untouched and still carries every
                        finding, including these.
                        """
                        LEVEL_DOT = {"red": "\U0001F534", "amber": "\U0001F7E0",
                                     "green": "\U0001F7E2"}
                        LEVEL_INK = {"red": RGBColor(0xB9, 0x1C, 0x1C),
                                     "amber": RGBColor(0xB4, 0x53, 0x09),
                                     "green": RGBColor(0x15, 0x6F, 0x3B)}
                        order = {"red": 0, "amber": 1, "green": 2}
                        findings = sorted(
                            findings,
                            key=lambda f: order.get(f.get("level"), 3))[:6]

                        tf = txbox(slide, Inches(0.55), Inches(0.34),
                                   Inches(12.2), Inches(0.9))
                        para(tf, True, "{0} — summary".format(phase_title),
                             size=28, bold=True, color=INK, space_after=2)
                        _n_red = sum(1 for f in findings
                                     if f.get("level") == "red")
                        para(tf, False,
                             "What this phase's results say — {0} of {1} "
                             "finding(s) need action.".format(
                                 _n_red, len(findings)),
                             size=10, color=MUTED, italic=True, space_before=2)

                        top = Inches(1.55)
                        avail = SH - top - Inches(0.55)
                        per = min(Inches(1.22),
                                  max(Inches(0.92),
                                      int(avail / max(1, len(findings)))))
                        band_h = min(Inches(1.05),
                                     max(Inches(0.8), per - Inches(0.12)))
                        for i, f in enumerate(findings):
                            y = top + i * per
                            band = slide.shapes.add_shape(
                                MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.55), y,
                                Inches(12.2), band_h)
                            band.fill.solid()
                            band.fill.fore_color.rgb = PANEL
                            band.line.fill.background()
                            band.shadow.inherit = False
                            band.adjustments[0] = 0.08
                            band.text_frame.text = ""
                            tf = txbox(slide, Inches(0.85), y + Inches(0.12),
                                       Inches(11.6), band_h - Inches(0.2))
                            p = tf.paragraphs[0]
                            p.space_after = Pt(1)
                            r = p.add_run()
                            r.text = "{0}  {1}".format(
                                LEVEL_DOT.get(f.get("level"), ""),
                                f.get("headline", "")).strip()
                            r.font.name, r.font.size, r.font.bold = \
                                FONT, Pt(13), True
                            r.font.color.rgb = LEVEL_INK.get(f.get("level"), INK)
                            if f.get("detail"):
                                para(tf, False, f["detail"], size=9.5,
                                     color=BODY, space_before=0)

                    # ---------------- one slide per result ----------------
                    # The render sequence is `captured` with the phase dividers
                    # (and each phase's closing summary) interleaved, rather
                    # than a loop nested inside a loop: the body below is the
                    # one that has always produced the result and animation
                    # slides, and it is reached identically either way. With a
                    # single phase nothing is interleaved and this is literally
                    # `captured`.
                    _DIVIDER, _PHASE_SUM = "__phase_divider__", "__phase_summary__"
                    if multi_phase:
                        render_seq = []
                        for _pk, _ptitle, _psub in phase_order:
                            render_seq.append((_DIVIDER, _ptitle, _psub, None))
                            render_seq.extend(phase_buckets[_pk])
                            render_seq.append((_PHASE_SUM, _pk, _ptitle, None))
                    else:
                        render_seq = list(captured)

                    for lbl, nm, img, vid in render_seq:
                        if lbl == _DIVIDER:
                            if style is not None:
                                s = prs.slides.add_slide(blank)
                                style.divider(s, next_section(), nm, img)
                            continue
                        if lbl == _PHASE_SUM:
                            _pf = [f for f in
                                   ((summary_data or {}).get("findings") or [])
                                   if phase_of_dataset(f.get("result")) == nm]
                            if _pf:
                                s = prs.slides.add_slide(blank)
                                add_footer_logo(s)
                                phase_summary_slide(s, img, _pf)
                            continue
                        s = prs.slides.add_slide(blank)
                        add_footer_logo(s)
                        parent = parent_of.get(lbl)

                        # Title block
                        tf = txbox(s, Inches(0.55), Inches(0.34),
                                   Inches(12.2), Inches(0.95))
                        if parent:
                            para(tf, True,
                                 "{0}  —  supporting investigation".format(
                                     parent.upper()),
                                 size=10, bold=True, color=ACCENT, space_after=2)
                            para(tf, False, lbl, size=26, bold=True, color=INK,
                                 space_after=0)
                        else:
                            para(tf, True, lbl, size=28, bold=True, color=INK,
                                 space_after=0)
                        if nm and nm != lbl:
                            para(tf, False, "Moldflow plot: {0}".format(nm),
                                 size=9.5, color=MUTED, italic=True,
                                 space_before=2)

                        # Screenshot (left)
                        IMG_X, IMG_Y = Inches(0.55), Inches(1.5)
                        IMG_W, IMG_H = Inches(6.95), Inches(5.4)
                        if img is not None:
                            w, h = fit_image(img, IMG_W, IMG_H)
                            s.shapes.add_picture(
                                str(img),
                                IMG_X + int((IMG_W - w) / 2),
                                IMG_Y + int((IMG_H - h) / 2),
                                width=w, height=h)
                        else:
                            ph = s.shapes.add_shape(
                                MSO_SHAPE.RECTANGLE, IMG_X, IMG_Y, IMG_W, IMG_H)
                            ph.fill.solid()
                            ph.fill.fore_color.rgb = PANEL
                            ph.line.fill.background()
                            ph.shadow.inherit = False
                            ptf = ph.text_frame
                            ptf.vertical_anchor = MSO_ANCHOR.MIDDLE
                            p = ptf.paragraphs[0]
                            p.alignment = PP_ALIGN.CENTER
                            r = p.add_run()
                            r.text = "Image could not be exported for this result."
                            r.font.name, r.font.size = FONT, Pt(12)
                            r.font.color.rgb = MUTED

                        # Interpretation column (right), on a soft panel
                        COL_X, COL_Y = Inches(7.75), Inches(1.5)
                        COL_W, COL_H = Inches(5.05), Inches(5.3)
                        panel = s.shapes.add_shape(
                            MSO_SHAPE.ROUNDED_RECTANGLE, COL_X, COL_Y, COL_W, COL_H)
                        panel.fill.solid()
                        panel.fill.fore_color.rgb = PANEL
                        panel.line.fill.background()
                        panel.shadow.inherit = False
                        panel.adjustments[0] = 0.02
                        panel.text_frame.text = ""

                        info = RESULT_GUIDE.get(lbl) or {}
                        rng = result_values.get(lbl)
                        kids = children.get(lbl) or []

                        # What the Assistant said about THIS study's figure for
                        # this result, when it answered. It leads the column:
                        # the reader wants the finding before the reference
                        # material that explains how to read it.
                        note = ai_notes.get(lbl) if style is not None else None

                        # Adaptive sizing: long entries step down a point so the
                        # column never overflows its panel.
                        bulk = len(info.get("description", "")) \
                            + len(info.get("purpose", "")) \
                            + len(info.get("targets", "")) \
                            + len(note or "") \
                            + sum(len(b) for b in info.get("investigate", []))
                        base = 11 if bulk < 700 else (10 if bulk < 1000 else 9)

                        tf = txbox(s, COL_X + Inches(0.28), COL_Y + Inches(0.24),
                                   COL_W - Inches(0.56), COL_H - Inches(0.48))
                        first = True
                        if note:
                            style.study_note(tf, first, note, size=base)
                            first = False
                        if rng:
                            label_value(tf, first, "Value range in this study:  ",
                                        rng, size=base)
                            first = False
                        if info.get("description"):
                            label_value(tf, first, "Description:  ",
                                        info["description"], size=base)
                            first = False
                        if info.get("purpose"):
                            label_value(tf, first, "Engineering purpose:  ",
                                        info["purpose"], size=base)
                            first = False
                        if info.get("investigate"):
                            p = tf.paragraphs[0] if first else tf.add_paragraph()
                            p.space_before, p.space_after = Pt(8), Pt(3)
                            r = p.add_run()
                            r.text = "Investigate"
                            r.font.name, r.font.size, r.font.bold = FONT, Pt(base), True
                            r.font.color.rgb = ACCENT
                            first = False
                            for point in info["investigate"]:
                                para(tf, False, point, size=base - 0.5,
                                     bullet=True, space_after=3)
                        target_text = _target_for(lbl, info, mat_limits)
                        if target_text:
                            label_value(tf, first, "Recommended target:  ",
                                        target_text, size=base)
                            first = False
                        if kids:
                            label_value(
                                tf, first, "Related plots in this deck:  ",
                                ", ".join(kids), size=base - 0.5)
                            first = False

                        # ------------- animation, on its own slide -------------
                        # Kept off the result slide so the layout above is
                        # untouched, and so the video gets the whole stage. The
                        # screenshot doubles as the poster frame, which means
                        # the slide still reads correctly in a PDF export or
                        # anywhere the video cannot play.
                        if vid is None:
                            continue
                        s = prs.slides.add_slide(blank)
                        add_footer_logo(s)
                        tf = txbox(s, Inches(0.55), Inches(0.34),
                                   Inches(12.2), Inches(0.95))
                        para(tf, True, "{0}  —  animation".format(lbl),
                             size=26, bold=True, color=INK, space_after=0)
                        para(tf, False,
                             "Click to play the filling sequence for this result.",
                             size=9.5, color=MUTED, italic=True, space_before=2)
                        # The animation slide carries nothing but the title, so
                        # the video gets the whole area beneath it rather than
                        # the screenshot's 5.4in band -- that unused strip at
                        # the bottom was free size being thrown away.
                        VID_X, VID_Y = Inches(0.4), Inches(1.42)
                        VID_W = int(SW - Inches(0.8))
                        VID_H = int(SH - VID_Y - Inches(0.6))

                        # Size from the FILE, not from the request or from the
                        # screenshot. See mp4_frame_size(): this build exports
                        # 1578x462 for a 960x720 request, so both of the other
                        # sources say 4:3 for a 3.4:1 video -- which is exactly
                        # why the embedded videos came out small. Falling back
                        # to the poster's ratio and then to 4:3 keeps a build
                        # that DOES honour the request working as before.
                        dims = mp4_frame_size(vid)
                        if dims:
                            ratio = float(dims[0]) / float(dims[1])
                            log("  '{0}' animation is {1}x{2} ({3:.2f}:1).".format(
                                lbl, dims[0], dims[1], ratio))
                        elif img is not None:
                            iw, ih = fit_image(img, Inches(4), Inches(4))
                            ratio = float(iw) / float(ih)
                            log("  Could not read the animation's size for '{0}'; "
                                "using the screenshot's aspect.".format(lbl))
                        else:
                            ratio = 4.0 / 3.0

                        # Fill the stage along whichever axis binds first, so
                        # the video is as large as it can be while staying
                        # wholly inside the slide and clear of the title.
                        vw, vh = VID_W, int(VID_W / ratio)
                        if vh > VID_H:
                            vh, vw = VID_H, int(VID_H * ratio)

                        # A poster of a different shape gets stretched to the
                        # shape's box by PowerPoint, so the still is letterboxed
                        # onto a canvas of the VIDEO's aspect first. Without
                        # this the static preview (and any PDF export) shows a
                        # distorted screenshot.
                        poster = str(img) if img is not None else None
                        if poster and dims:
                            try:
                                from PIL import Image
                                with Image.open(poster) as pim:
                                    pim = pim.convert("RGB")
                                    if abs(pim.width / float(pim.height)
                                           - ratio) > 0.02:
                                        cw = max(pim.width,
                                                 int(pim.height * ratio))
                                        ch = max(pim.height,
                                                 int(cw / ratio))
                                        canvas = Image.new(
                                            "RGB", (cw, ch), (255, 255, 255))
                                        canvas.paste(pim,
                                                     ((cw - pim.width) // 2,
                                                      (ch - pim.height) // 2))
                                        # Temp dir, not the images folder: the
                                        # poster is scratch for the embed, and
                                        # nothing that is not a deliverable
                                        # should appear next to the exported
                                        # results (same rule as the framing
                                        # probes). python-pptx copies the bytes
                                        # into the .pptx, so it is not needed
                                        # after the save.
                                        import tempfile
                                        shaped = Path(tempfile.gettempdir()) / (
                                            Path(str(vid)).stem + "_poster.png")
                                        canvas.save(str(shaped))
                                        poster = str(shaped)
                            except Exception as pe:
                                log("  Could not reshape the poster frame for "
                                    "'{0}': {1}".format(lbl, pe))

                        try:
                            s.shapes.add_movie(
                                str(vid),
                                VID_X + int((VID_W - vw) / 2),
                                VID_Y + int((VID_H - vh) / 2),
                                vw, vh,
                                poster_frame_image=poster,
                                mime_type="video/mp4")
                        except Exception as ve:
                            log("  Could not embed the animation for '{0}': "
                                "{1}".format(lbl, ve))

                    # ---------------- results not produced ----------------
                    if missing_labels:
                        s = prs.slides.add_slide(blank)
                        add_footer_logo(s)
                        tf = txbox(s, Inches(0.55), Inches(0.34),
                                   Inches(12.2), Inches(0.8))
                        para(tf, True, "Results not produced by this analysis",
                             size=28, bold=True, color=INK)
                        # Narrower measure than the title: a full-width line of
                        # 12pt body text is far too long to read comfortably.
                        tf = txbox(s, Inches(0.55), Inches(1.45),
                                   Inches(10.4), Inches(0.9))
                        para(tf, True,
                             "The '{0}' sequence produced {1} of the Top 12 "
                             "engineering results. The {2} below {3} not "
                             "available from this study — each is listed with "
                             "what it would require.".format(
                                 seq_display, len(head_done),
                                 "result" if len(missing_labels) == 1
                                 else "{0} results".format(len(missing_labels)),
                                 "is" if len(missing_labels) == 1 else "are"),
                             size=12, color=BODY)
                        # Per-result reason, derived from the result itself
                        # rather than one blanket sentence that could contradict
                        # the sequence actually run.
                        tf = txbox(s, Inches(0.85), Inches(2.6),
                                   Inches(11.6), Inches(4.2))
                        for i, lbl in enumerate(missing_labels):
                            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                            p.space_before, p.space_after = Pt(0), Pt(9)
                            r = p.add_run()
                            r.text = "•  " + lbl
                            r.font.name, r.font.size, r.font.bold = FONT, Pt(13), True
                            r.font.color.rgb = INK
                            reason = MISSING_REASON.get(lbl)
                            if reason:
                                r2 = p.add_run()
                                r2.text = "  —  " + reason
                                r2.font.name, r2.font.size = FONT, Pt(12)
                                r2.font.color.rgb = MUTED

                    # ---------------- analysis summary (last) ----------------
                    # Deliberately the closing section: the reader has been
                    # through the plots by this point, and this is the "so what"
                    # -- the measured statistics for every result THIS study
                    # produced, then the flagged assessment.
                    summary = summary_data or {}
                    s_metrics = summary.get("metrics") or []
                    s_findings = summary.get("findings") or []

                    # ---- AI Assistant summary ----
                    # The problematic results as a table, then the key concerns
                    # ranked by severity. When `assistant` carries an answer
                    # captured from Moldflow's own AI Assistant panel, that is
                    # what these slides show; otherwise the same shape is
                    # regenerated from this study's statistics. Either way the
                    # layout below is identical -- see ai_report_summary.py.
                    # Rendered FIRST so the reader meets the conclusions before
                    # the per-finding detail.
                    ai_sum = None
                    try:
                        import ai_report_summary
                        ai_sum = ai_report_summary.build_summary(
                            summary, meta=ai_meta, study_name=study_name,
                            assistant=(assistant or {}).get("summary"))
                    except Exception as e_ai:
                        log("  AI-style summary unavailable: {0}".format(e_ai))

                    # Section divider, so the closing assessment is announced
                    # rather than arriving straight after the last plot.
                    if style is not None and (ai_sum or s_metrics or s_findings):
                        s = prs.slides.add_slide(blank)
                        style.divider(
                            s, next_section(), "Analysis summary",
                            "What the results say, and what to do about it",
                            ["Problematic results, worst first",
                             "The measured statistics behind each finding",
                             "Recommended actions before the next iteration"])

                    # Does the closing section speak for the Assistant or for
                    # our own statistics? Every caption below turns on this, so
                    # the deck never attributes a number to the wrong source.
                    from_assistant = bool(
                        ai_sum and ai_sum.get("source") == "assistant")

                    if ai_sum and ai_sum.get("problems"):
                        LEVEL_TINT = {"red": RGBColor(0xFE, 0xE2, 0xE2),
                                      "amber": RGBColor(0xFE, 0xF3, 0xC7),
                                      "green": RGBColor(0xE6, 0xF4, 0xEA)}
                        LVL_INK = {"red": RGBColor(0xB9, 0x1C, 0x1C),
                                   "amber": RGBColor(0xB4, 0x53, 0x09),
                                   "green": RGBColor(0x15, 0x6F, 0x3B)}
                        MARK = {"red": "\U0001F534", "amber": "\U0001F7E0",
                                "green": "\U0001F7E2"}

                        # Eight data rows per slide: a header plus eight 0.62in
                        # rows from 1.75in ends at 7.33in on a 7.5in slide.
                        ROWS_PER_PAGE = 8
                        prob = ai_sum["problems"]
                        pages = [prob[i:i + ROWS_PER_PAGE]
                                 for i in range(0, len(prob), ROWS_PER_PAGE)]
                        for pno, chunk in enumerate(pages):
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            tf = txbox(s, Inches(0.55), Inches(0.34),
                                       Inches(12.2), Inches(1.0))
                            para(tf, True, "Problematic Results" + (
                                "  ({0}/{1})".format(pno + 1, len(pages))
                                if len(pages) > 1 else ""),
                                size=28, bold=True, color=INK, space_after=2)
                            para(tf, False, ai_sum.get("headline", ""),
                                 size=11, color=BODY, space_before=2,
                                 space_after=1)
                            if ai_sum.get("context_line"):
                                para(tf, False, ai_sum["context_line"],
                                     size=9, color=MUTED, italic=True,
                                     space_before=1)

                            tbl_h = Inches(0.42) + Inches(0.62) * len(chunk)
                            gf = s.shapes.add_table(
                                len(chunk) + 1, 4, Inches(0.55), Inches(1.62),
                                Inches(12.2), tbl_h)
                            table = gf.table
                            for idx, w in enumerate(
                                    (Inches(2.9), Inches(6.0), Inches(2.3), Inches(1.0))):
                                table.columns[idx].width = w

                            def _cell(r, c, text, bold=False, size=10,
                                      color=BODY, fill=None):
                                cell = table.cell(r, c)
                                cell.text = ""
                                cell.margin_left = cell.margin_right = Inches(0.08)
                                cell.margin_top = cell.margin_bottom = Inches(0.03)
                                if fill is not None:
                                    cell.fill.solid()
                                    cell.fill.fore_color.rgb = fill
                                p = cell.text_frame.paragraphs[0]
                                r_ = p.add_run()
                                r_.text = str(text)
                                r_.font.name, r_.font.size = FONT, Pt(size)
                                r_.font.bold = bold
                                r_.font.color.rgb = color
                                return cell

                            for c, head in enumerate(("Result", "Issue", "Value", "Unit")):
                                _cell(0, c, head, bold=True, size=10,
                                      color=PAPER, fill=INK)
                            for i, row in enumerate(chunk, start=1):
                                tint = LEVEL_TINT.get(row["level"], PANEL)
                                ink = LVL_INK.get(row["level"], BODY)
                                _cell(i, 0, "{0}  {1}".format(
                                    MARK.get(row["level"], ""), row["result"]),
                                    bold=True, color=ink, fill=tint)
                                _cell(i, 1, row["issue"], fill=tint)
                                _cell(i, 2, row["value"] or "—", fill=tint)
                                _cell(i, 3, row["unit"], color=MUTED, fill=tint)

                        # ---- summary of key concerns ----
                        concerns = ai_sum.get("concerns") or []
                        advice = (ai_sum.get("context") or {}).get("autodesk_advice") or []
                        if concerns:
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            tf = txbox(s, Inches(0.55), Inches(0.34),
                                       Inches(12.2), Inches(0.9))
                            para(tf, True, "Summary of Key Concerns",
                                 size=28, bold=True, color=INK, space_after=2)
                            c = ai_sum.get("counts") or {}
                            para(tf, False,
                                 "{0} requiring action, {1} for review.".format(
                                     c.get("red", 0), c.get("amber", 0)),
                                 size=10, color=MUTED, italic=True,
                                 space_before=2)

                            tf = txbox(s, Inches(0.55), Inches(1.5),
                                       Inches(12.2), Inches(5.2))
                            # A short list was stacking at the top and leaving
                            # the lower half of the slide blank. Give the lines
                            # room in proportion to how few there are, capped so
                            # they still read as one list.
                            shown = concerns[:12]
                            csize = 14 if len(shown) <= 6 else 13
                            cgap = max(8, min(24, int(150 / max(1, len(shown)))))
                            for i, con in enumerate(shown):
                                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                                p.space_before, p.space_after = Pt(0), Pt(cgap)
                                r_ = p.add_run()
                                r_.text = "{0}  {1}: ".format(
                                    con["mark"], con["result"])
                                r_.font.name, r_.font.size = FONT, Pt(csize)
                                r_.font.bold = True
                                r_.font.color.rgb = LVL_INK.get(con["level"], INK)
                                r2 = p.add_run()
                                r2.text = con["text"]
                                r2.font.name, r2.font.size = FONT, Pt(csize)
                                r2.font.color.rgb = BODY

                            # Autodesk's own advice engine, verbatim and
                            # attributed -- it is the one part of this deck we
                            # did not author, so it is never reworded.
                            if advice:
                                tf = txbox(s, Inches(0.55), Inches(6.5),
                                           Inches(12.2), Inches(0.8))
                                para(tf, True, "Autodesk result advice",
                                     size=9, bold=True, color=ACCENT,
                                     space_after=2)
                                for a in advice[:2]:
                                    para(tf, False, a, size=9, color=MUTED,
                                         italic=True)

                    # With a live answer the statistics table shows the values
                    # the Assistant read from the study. The Analysis Summary
                    # bands are NOT touched: they carry this project's own
                    # threshold assessment, which stands on its own and would
                    # otherwise be replaced by a second printing of the
                    # recommendations that already have their own slide.
                    if from_assistant:
                        s_metrics = ai_sum.get("stat_rows") or []

                    if s_metrics or s_findings:
                        LEVEL_DOT = {"red": "\U0001F534", "amber": "\U0001F7E0",
                                     "green": "\U0001F7E2"}
                        LEVEL_INK = {"red": RGBColor(0xB9, 0x1C, 0x1C),
                                     "amber": RGBColor(0xB4, 0x53, 0x09),
                                     "green": RGBColor(0x15, 0x6F, 0x3B)}

                        # ---- overall assessment ----
                        # Paginated at 6 bands per slide: six 0.92in bands from
                        # 1.5in reach 7.02in on a 7.5in slide, so a seventh
                        # would run off. Overflowing onto another slide rather
                        # than truncating matters — a dropped band is a finding
                        # the reader never learns about.
                        FIND_PER_PAGE = 6
                        f_pages = [s_findings[i:i + FIND_PER_PAGE]
                                   for i in range(0, len(s_findings), FIND_PER_PAGE)]
                        for fno, fchunk in enumerate(f_pages):
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            tf = txbox(s, Inches(0.55), Inches(0.34),
                                       Inches(12.2), Inches(0.9))
                            para(tf, True,
                                 "Analysis Summary" + (
                                     "  ({0}/{1})".format(fno + 1, len(f_pages))
                                     if len(f_pages) > 1 else ""),
                                 size=28, bold=True, color=INK, space_after=2)
                            c = summary.get("counts") or {}
                            para(tf, False,
                                 "Automatically assessed from this study's own "
                                 "results — {0} action(s), {1} caution(s), "
                                 "{2} within guide values.".format(
                                     c.get("red", 0), c.get("amber", 0),
                                     c.get("green", 0)),
                                 size=10, color=MUTED, italic=True, space_before=2)

                            # Bands are spread to fill the slide when there are
                            # fewer than a full page of them, rather than
                            # stacking at the top and leaving the lower half
                            # blank. Pitch is capped so three findings do not
                            # drift apart into three unrelated boxes.
                            top = Inches(1.5)
                            avail = SH - top - Inches(0.55)
                            per = min(Inches(1.22),
                                      max(Inches(0.92),
                                          int(avail / max(1, len(fchunk)))))
                            band_h = min(Inches(1.05),
                                         max(Inches(0.8), per - Inches(0.12)))
                            for i, f in enumerate(fchunk):
                                y = top + i * per
                                band = s.shapes.add_shape(
                                    MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.55), y,
                                    Inches(12.2), band_h)
                                band.fill.solid()
                                band.fill.fore_color.rgb = PANEL
                                band.line.fill.background()
                                band.shadow.inherit = False
                                band.adjustments[0] = 0.08
                                band.text_frame.text = ""

                                tf = txbox(s, Inches(0.85), y + Inches(0.12),
                                           Inches(11.6), band_h - Inches(0.2))
                                p = tf.paragraphs[0]
                                p.space_after = Pt(1)
                                r = p.add_run()
                                # A recommendation carries no severity, so it
                                # has no dot; strip rather than leave the line
                                # indented by a marker that is not there.
                                r.text = "{0}  {1}".format(
                                    LEVEL_DOT.get(f["level"], ""),
                                    f["headline"]).strip()
                                r.font.name, r.font.size, r.font.bold = FONT, Pt(13), True
                                r.font.color.rgb = LEVEL_INK.get(f["level"], INK)
                                if f.get("detail"):
                                    para(tf, False, f["detail"], size=9.5,
                                         color=BODY, space_before=0)

                        # ---- statistics table ----
                        # Chunked so a study with many results simply gets a
                        # second page instead of a table running off the slide.
                        PER_PAGE = 12
                        pages = [s_metrics[i:i + PER_PAGE]
                                 for i in range(0, len(s_metrics), PER_PAGE)] or []
                        for pno, chunk in enumerate(pages):
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            tf = txbox(s, Inches(0.55), Inches(0.34),
                                       Inches(12.2), Inches(0.9))
                            para(tf, True,
                                 "Result Statistics" + (
                                     "  ({0}/{1})".format(pno + 1, len(pages))
                                     if len(pages) > 1 else ""),
                                 size=28, bold=True, color=INK, space_after=2)
                            para(tf, False,
                                 "Read from this study by Moldflow's AI "
                                 "Assistant." if from_assistant else
                                 "Computed across every node/element of each "
                                 "result in this study.",
                                 size=10, color=MUTED, italic=True, space_before=2)

                            # Same table, same place, same widths in total: the
                            # Assistant returns one figure per result, so its
                            # table is Result | Value | Unit rather than the
                            # four statistics we compute ourselves.
                            heads = (("Result", "Value", "Unit")
                                     if from_assistant else
                                     ("Result", "Minimum", "Maximum",
                                      "Average", "Std. deviation"))
                            widths = ((Inches(7.0), Inches(3.2), Inches(2.0))
                                      if from_assistant else
                                      (Inches(4.3), Inches(1.9), Inches(1.9),
                                       Inches(1.9), Inches(2.2)))
                            rows, cols = len(chunk) + 1, len(heads)
                            tbl = s.shapes.add_table(
                                rows, cols, Inches(0.55), Inches(1.45),
                                Inches(12.2), Inches(0.36) * rows).table
                            for w, cw in enumerate(widths):
                                tbl.columns[w].width = cw
                            for ci, head in enumerate(heads):
                                cell = tbl.cell(0, ci)
                                cell.text = head
                                pr = cell.text_frame.paragraphs[0]
                                pr.runs[0].font.name = FONT
                                pr.runs[0].font.size = Pt(11)
                                pr.runs[0].font.bold = True
                                pr.runs[0].font.color.rgb = PAPER
                                cell.fill.solid()
                                cell.fill.fore_color.rgb = INK
                            for ri, m in enumerate(chunk, start=1):
                                if from_assistant:
                                    cells = (m["name"], m["value"] or "—",
                                             m["unit"] or "—")
                                else:
                                    u = m["unit"]
                                    cells = (m["name"],
                                             format_stat(m["min"], u),
                                             format_stat(m["max"], u),
                                             format_stat(m["mean"], u),
                                             format_stat(m["std"], u))
                                for ci, text in enumerate(cells):
                                    cell = tbl.cell(ri, ci)
                                    cell.text = str(text)
                                    pr = cell.text_frame.paragraphs[0]
                                    pr.runs[0].font.name = FONT
                                    pr.runs[0].font.size = Pt(10)
                                    pr.runs[0].font.color.rgb = BODY
                                    cell.fill.solid()
                                    cell.fill.fore_color.rgb = (
                                        PAPER if ri % 2 else PANEL)

                        # ---- points to highlight ----
                        # The Assistant's recommendations, grouped by what a
                        # reader would act on. Only when it answered: the local
                        # threshold rules produce findings, not actions, and a
                        # slide of invented advice would be worse than none.
                        recs = (ai_sum or {}).get("recommendations") or []
                        if style is not None and recs:
                            try:
                                groups = ai_report_summary.group_recommendations(
                                    recs)
                            except Exception:
                                groups = [("Recommended actions", recs)]
                            s = prs.slides.add_slide(blank)
                            add_footer_logo(s)
                            style.highlights(
                                s, groups,
                                footnote="Generated by Moldflow's AI Assistant "
                                         "from this study's own results. Review "
                                         "against the plots in this deck before "
                                         "acting on them.")

                        # Provenance: this is a derived assessment, and the deck
                        # should say so rather than let it read as a Moldflow
                        # verdict.
                        s = prs.slides.add_slide(blank)
                        add_footer_logo(s)
                        tf = txbox(s, Inches(0.55), Inches(0.34),
                                   Inches(12.2), Inches(0.8))
                        para(tf, True, "How this summary was produced",
                             size=22, bold=True, color=INK)
                        tf = txbox(s, Inches(0.55), Inches(1.4),
                                   Inches(11.6), Inches(5.0))
                        assistant_provenance = [
                            "The study was open in Synergy with its analysis "
                            "results loaded, and the question was put to "
                            "Moldflow's own AI Assistant panel automatically.",
                            "The results, observations and recommendations on "
                            "the preceding slides are the Assistant's answer, "
                            "laid out but not reworded.",
                            "Values are the figures the Assistant read from "
                            "this study; they are single figures per result "
                            "rather than the per-node statistics this deck "
                            "computes when the Assistant is unavailable.",
                            "The Assistant is asked once per report. If it "
                            "cannot be reached, this section falls back to an "
                            "assessment computed from the study's own raw "
                            "solver data.",
                            "AI-generated content — review it against the "
                            "plots in this deck before acting on it.",
                        ]
                        for i, line in enumerate(assistant_provenance
                                                 if from_assistant else [
                            "Results were discovered from this study itself, so "
                            "the summary covers what this analysis actually "
                            "produced rather than a fixed checklist.",
                            "Minimum, maximum, average and standard deviation are "
                            "computed across every node or element of each "
                            "result, from the raw solver data.",
                            "Values are converted from Moldflow's storage units "
                            "to engineering units using the result catalogue "
                            "shipped with Moldflow Insight.",
                            "Flagged findings apply the targets in Autodesk's "
                            "“Top 12 Results” reference, using the "
                            "grade's own limits where the material supplies them.",
                            "This is an automated first pass to direct attention. "
                            "It does not replace engineering review of the plots "
                            "in this deck.",
                        ]):
                            para(tf, i == 0, line, size=12, color=BODY,
                                 bullet=True, space_after=10)

                    prs.save(str(deck_path))
                    return deck_path
                except Exception as e:
                    log("Report generation failed: {0}".format(e))
                    return None

            report_path = None
            captured = []
            if shown_plots:
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                clean_name = re.sub(r"[^A-Za-z0-9]+", "_", study_label()).strip("_") or "filename"
                report_dir = Path(HERE) / "reports" / "{0}_{1}".format(clean_name, stamp)
                image_dir = report_dir / "images"

                # Interactive review first: the user selects results in the
                # native Synergy Results tree and inspects each one freely; the
                # ones they reviewed are captured when they click Generate
                # Report. If the review cannot be started it returns None and we
                # fall back to the original unattended export so the deck is
                # still produced. (`shown_plots` is passed only to attach the
                # proper report labels/values to results that are in the Top 12
                # — selection itself comes entirely from the user's tree.)
                if INTERACTIVE_REVIEW:
                    captured = interactive_result_review(shown_plots, image_dir)
                    if captured is None:
                        log("Falling back to unattended capture.")
                if not INTERACTIVE_REVIEW or captured is None:
                    log("Exporting {0} result image(s) for the report...".format(
                        len(shown_plots)))
                    captured = export_result_images(shown_plots, image_dir)

                if captured:
                    # Build the deck. This call had been replaced by a log line
                    # reading "PowerPoint presentation deck export skipped per
                    # configuration" -- but there is no such configuration, and
                    # build_pptx_report() below was left fully implemented and
                    # simply never called. So every run captured its images and
                    # animations and then produced no .pptx at all.
                    deck_path = report_dir / "{0}_review.pptx".format(clean_name)
                    try:
                        report_dir.mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                    # Study context for the summary slides -- material grade,
                    # machine limits and Autodesk's own result advice -- read
                    # from the .sdy with the 2027 metadata extractor. Offline
                    # and ~0.1s: it never touches the COM channel, so it cannot
                    # disturb the session the way another automation call
                    # would. Optional by design; the deck simply omits the
                    # context line if Moldflow 2027 is not installed.
                    ai_meta = None
                    try:
                        import ai_assistant
                        # StudyDoc has StudyName, NOT Path or StudyPath --
                        # 'Attribute Path not found', the same finding the
                        # Fusion round-trip check records above. Asking for the
                        # wrong two names is why this quietly returned nothing
                        # on every run and the study-setup slide came out empty.
                        # The directory comes from Project.Path, and StudyName
                        # already carries the .sdy extension on this build.
                        sdy = None
                        try:
                            stem = call_member(study_doc, "StudyName")
                            proj_dir = call_member(call_member(sy, "project"),
                                                   "Path")
                            if stem and proj_dir:
                                stem = str(stem)
                                if not stem.lower().endswith(".sdy"):
                                    stem += ".sdy"
                                p = Path(str(proj_dir)) / stem
                                if p.is_file():
                                    sdy = str(p)
                                else:
                                    log("AI Assistant metadata: no study file at "
                                        "{0}".format(p))
                        except Exception as pe:
                            log("AI Assistant metadata: could not resolve the "
                                "study file ({0}).".format(pe))
                        if sdy:
                            ai_meta = ai_assistant.extract(sdy)
                            log("AI Assistant metadata: {0} from {1}".format(
                                "extracted" if ai_meta else
                                "none returned (study unreadable to the extractor)",
                                Path(sdy).name))
                    except Exception as ae:
                        log("AI Assistant metadata unavailable: {0}".format(ae))


                    # Statistics for the closing summary. Computed over the
                    # study's OWN datasets (not just the captured ones), so the
                    # summary reflects the whole analysis even when the user
                    # reviewed a subset. Never fatal: a failure here costs the
                    # summary slides, not the deck.
                    summary_data = None
                    try:
                        log("Collecting analysis statistics for the summary...")
                        # The grade's published limits, from the metadata
                        # extractor. material_limits() reads them over COM and
                        # on this build comes back empty without raising, which
                        # left every threshold rule on the reference document's
                        # worked-example figure -- the run that flagged
                        # "247,142 1/s exceeds the 24,000 1/s limit" when this
                        # PP is actually rated to 100,000. Same numbers, a
                        # source that works.
                        _grade_limits = {}
                        try:
                            _msum = ai_assistant.summary(ai_meta) if ai_meta else {}
                            for _k, _key in (("max_shear_rate", "shear rate"),
                                             ("max_shear_stress",
                                              "shear stress at wall")):
                                _v = _msum.get(_k)
                                if _v:
                                    _grade_limits[_key] = float(_v)
                        except Exception:
                            _grade_limits = {}
                        summary_data = collect_analysis_summary(
                            sy, plot_mgr, log, grade_limits=_grade_limits)
                    except Exception as se:
                        log("Analysis summary could not be produced: {0}".format(se))

                    # A machine-readable copy beside the deck, so the numbers can
                    # be diffed between design iterations without reopening the
                    # PowerPoint.
                    if summary_data:
                        try:
                            (report_dir / "analysis_summary.json").write_text(
                                json.dumps(summary_data, indent=2), encoding="utf-8")
                        except Exception as je:
                            log("Could not write analysis_summary.json: {0}".format(je))

                    # The closing summary, asked of Moldflow's OWN AI Assistant
                    # panel rather than regenerated from threshold rules. The
                    # panel reads the study Autodesk's way and catches what a
                    # fixed rule cannot -- on the study this was built against
                    # it flagged a shear rate of 281,178 1/s against the
                    # grade's 100,000 limit, which our own rules never raised.
                    #
                    # Costs one request on the signed-in Autodesk account and
                    # needs the WebView2 debug port (enable_assistant_port.bat,
                    # once). Whenever it is not available `assistant` is None
                    # and every summary slide falls back to exactly what this
                    # deck produced before -- nothing else in the report
                    # changes either way.
                    assistant = None
                    try:
                        import assistant_live
                        assistant = assistant_live.fetch_report_data(
                            labels=[l for l, _n, _i, _v in captured],
                            allow_send=ASK_AI_ASSISTANT, log=log)
                    except Exception as le:
                        log("AI Assistant summary unavailable: {0}".format(le))
                    # Keep the raw answers beside the deck so any sentence on a
                    # slide can be traced back to what the panel actually wrote.
                    for name, text in (
                            ("ai_assistant_answer.md",
                             ((assistant or {}).get("summary") or {}).get("answer")),
                            ("ai_assistant_notes.md",
                             (assistant or {}).get("notes_answer"))):
                        if not text:
                            continue
                        try:
                            (report_dir / name).write_text(text, encoding="utf-8")
                        except Exception as we:
                            log("Could not write {0}: {1}".format(name, we))

                    # Markdown sidecar of the assistant-style summary, beside
                    # the JSON. Same content as the deck's summary slides, in
                    # the form that pastes straight into a ticket or email.
                    try:
                        import ai_report_summary
                        _md = ai_report_summary.to_markdown(
                            ai_report_summary.build_summary(
                                summary_data or {}, meta=ai_meta,
                                study_name=study_label(),
                                assistant=(assistant or {}).get("summary")))
                        (report_dir / "ai_summary.md").write_text(_md, encoding="utf-8")
                    except Exception as me:
                        log("Could not write ai_summary.md: {0}".format(me))

                    # The part itself, for the deck's opening "Component
                    # details" slide. Reuses export_dimensions, so there is one
                    # bounding-box calculation in the project rather than two.
                    # Costs a full model export -- see INCLUDE_COMPONENT_DETAILS.
                    component_data = None
                    try:
                        component_data = collect_component_dimensions(sy, log)
                    except Exception as ce:
                        log("Component details unavailable: {0}".format(ce))

                    # The moulding conditions the study was set up with. Read
                    # only: nothing is written to the process controller, so
                    # the workflow's edit-only write contract is untouched.
                    process_data = None
                    try:
                        # Phase 3's resolver, so the deck reads the process
                        # controller the study actually solves with. Unbound if
                        # this run never reached Phase 3, in which case the
                        # collector falls back and logs that it did.
                        try:
                            _resolve_proc = resolve_process_controller
                        except NameError:
                            _resolve_proc = None
                        process_data = collect_process_parameters(
                            sy, study_doc, log, resolve=_resolve_proc,
                            sequence=seq_display or seq_name)
                    except Exception as pe2:
                        log("Process parameters unavailable: {0}".format(pe2))

                    # Mesh counts for the FEA column of the component slide.
                    # Already collected at the diagnostics gate; a run that
                    # skipped that gate simply has no mesh column.
                    try:
                        mesh_data = diag_report
                    except NameError:
                        mesh_data = None

                    log("Building the PowerPoint review deck from {0} "
                        "captured result(s)...".format(len(captured)))
                    report_path = build_pptx_report(captured, missing, deck_path,
                                                    summary_data, ai_meta,
                                                    study_label(), assistant,
                                                    component=component_data,
                                                    process=process_data,
                                                    mesh_report=mesh_data)
                    if report_path:
                        log("Presentation written to: {0}".format(report_path))
                    else:
                        log("The review deck was NOT written — see the reason "
                            "logged above.")
                else:
                    log("No results were reviewed — skipping the report.")
                try:
                    import ui_bridge
                    if captured and report_path:
                        _ex = "{0} result(s) + PowerPoint deck".format(len(captured))
                    elif captured:
                        _ex = "{0} result(s) exported, deck failed".format(len(captured))
                    else:
                        _ex = "No results exported"
                    ui_bridge.update_state("params", {"export_status": _ex})
                except Exception:
                    pass
            else:
                log("No results were displayed — skipping the report.")

            # Summary dialog. Only the Top 12 are reportable, so the count is
            # always out of 12 — it shows how much of the master engineering
            # list this sequence was actually able to produce.
            msg = "Displayed {0} of the {1} Top 12 engineering results.\n\n".format(
                len(shown), len(result_specs))
            if shown:
                msg += "Shown:\n" + "\n".join("  • " + s for s in shown) + "\n\n"
            if missing:
                msg += ("Not available in this study:\n"
                        + "\n".join("  • " + m for m in missing)
                        + "\n\nThese require the matching analysis sequence "
                          "(e.g. Pack for shrinkage/sink marks, Cool for the part "
                          "temperature and cooling flow rate, Warp for deflection). "
                          "Re-run with that sequence to produce them.")
            # The review is selective, so the deck can legitimately hold fewer
            # results than were displayed. Say which ones made it rather than
            # letting the user infer it from the deck.
            if captured and len(captured) != len(shown):
                msg += ("Reviewed and added to the report:\n"
                        + "\n".join("  • " + l for l, _n, _i, _v in captured)
                        + "\n\n")
            if report_path:
                msg += "\n\nPowerPoint review deck saved to:\n{0}".format(report_path)
            elif shown_plots and captured:
                msg += ("\n\nThe PowerPoint review deck could not be generated "
                        "— see diagnostics_log.txt for the reason.")
            show_dialog("Workflow Complete", msg)

    except Exception as ex:
        log("Workflow Error: {0}".format(ex))
        show_dialog("Workflow Error", "An error occurred during the automation workflow:\n\n{0}".format(ex))
    finally:
        # Hand Synergy back exactly as we found it. Without this an aborted run
        # would leave the application permanently unable to warn the user about
        # anything, and a watcher thread still clicking its dialogs.
        for _stopper in (_stop_dialog_watcher, _stop_dialog_hook):
            try:
                _stopper()
            except Exception:
                pass
        if _silenced:
            try:
                sy.Silence(False)
                log("Synergy message boxes restored.")
            except Exception as e:
                log("Could not restore Synergy message boxes: {0}".format(e))

if __name__ == "__main__":
    main()
