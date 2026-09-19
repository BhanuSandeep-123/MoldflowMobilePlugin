"""
ai_assistant.py
---------------
Wrapper around Moldflow 2027's `aiassistant.exe` -- the "AI Assistant" meta
data extraction tool that ships in the Synergy bin directory.

It reads a study file (.sdy) straight from disk and writes a JSON summary of
the model, material, machine, result advice and results. No COM, no running
Synergy instance, no session key: it is a plain offline reader, which makes it
much cheaper (~0.1s) and much safer than asking Synergy for the same numbers
over automation while it is meshing or solving.

    from ai_assistant import extract, summary
    data = extract(r"C:\\...\\part_study.sdy")   # dict, or None
    print(summary(data)["melt_temp_c"])

BEHAVIOURS OF THE EXE THAT THIS MODULE EXISTS TO ABSORB
-------------------------------------------------------
1. It exits 0 even when it extracts NOTHING. A study it cannot read produces
   no .json at all and still returns success, so the exit code is worthless as
   a check -- the output file's existence is the real signal. (Verified with a
   4 KB Fusion round-trip stub: exit 0, no JSON, no error text.)
2. The JSON is written with a UTF-8 BOM, so json.load(open(p)) raises
   "Expecting value: line 1 column 1". It must be read as utf-8-sig.
3. -output is a PREFIX, not a filename: it appends .json (the data) and .out
   (an internal numeric message log that is of no use here).
4. Temperatures come back in KELVIN by default. -units_option METRIC returns
   the degC values the workflow actually shows the user.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import glob
import shutil

# Default install location. Kept as a search list rather than one constant so a
# 2028 install, or a machine where Moldflow sits on another drive, still
# resolves without a code change.
_EXE_GLOBS = [
    r"C:\Program Files\Autodesk\Moldflow Synergy *\bin\aiassistant.exe",
    r"C:\Program Files\Autodesk\Moldflow Insight *\bin\aiassistant.exe",
    r"D:\Program Files\Autodesk\Moldflow Synergy *\bin\aiassistant.exe",
]

# Sections the exe understands. Passing none of them means -all.
SECTIONS = ("model", "material", "machine", "advice", "results")

UNITS_SI = "SI"            # Kelvin, Pascal, m^3  (the exe's default)
UNITS_METRIC = "METRIC"    # degC, MPa            (what the panel shows)
UNITS_ENGLISH = "ENGLISH"  # degF, psi


def find_exe():
    """Absolute path to aiassistant.exe, or None if Moldflow 2027+ is not
    installed here. Highest version wins when several are present."""
    override = os.environ.get("MOLDFLOW_AIASSISTANT")
    if override and os.path.isfile(override):
        return override
    hits = []
    for pattern in _EXE_GLOBS:
        hits.extend(glob.glob(pattern))
    if not hits:
        found = shutil.which("aiassistant")
        return found
    return sorted(hits)[-1]


def extract(model_file, sections=None, units=UNITS_METRIC, timeout=120):
    """Run the extractor over `model_file` and return the parsed metadata dict
    (the contents of the "Moldflow Meta Data" object), or None.

    None means "no metadata came back" -- the tool is not installed, the study
    could not be read, or it is one of the empty round-trip stubs. It is never
    an exception: this is a nice-to-have data source and no caller should have
    to guard it.

    sections: any of SECTIONS, or None for everything.
    units:    UNITS_METRIC (degC) by default, NOT the exe's Kelvin default.
    """
    exe = find_exe()
    if not exe:
        return None
    model_file = os.path.abspath(str(model_file))
    if not os.path.isfile(model_file):
        return None

    # Its own scratch directory: -output is a prefix and the exe drops a .out
    # log beside the .json, neither of which belongs in the user's project.
    workdir = tempfile.mkdtemp(prefix="mf_ai_")
    prefix = os.path.join(workdir, "meta")
    cmd = [exe]
    for name in (sections or ()):
        if name not in SECTIONS:
            raise ValueError("unknown section {0!r}; expected any of {1}".format(name, SECTIONS))
        cmd.append("-" + name)
    if not sections:
        cmd.append("-all")
    if units:
        cmd += ["-units_option", units]
    cmd += ["-output", prefix, model_file]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Deliberately NOT checking returncode: it is 0 whether or not any
        # metadata was produced. The file is the only honest signal.
        out_json = prefix + ".json"
        if not os.path.isfile(out_json):
            return None
        with open(out_json, "r", encoding="utf-8-sig") as fh:
            return json.load(fh).get("Moldflow Meta Data")
    except Exception:
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _val(node):
    """Unwrap a {"Units": ..., "Value": ...} node to its number."""
    if isinstance(node, dict):
        return node.get("Value")
    return node


def summary(data):
    """Flatten the parts of the metadata the workflow actually cares about.

    Every key may be None -- an unmeshed study reports a part volume of 0 and
    carries no Results section at all, so absence is normal, not an error.
    """
    if not isinstance(data, dict):
        return {}
    material = (data.get("Material") or {}).get("1") or {}
    machine = (data.get("Machine") or {}).get("1") or {}
    model = data.get("Model") or {}
    advice = data.get("Advice") or {}

    warnings = [
        "{0}: {1}".format(name, (body or {}).get("Reason", ""))
        for name, body in advice.items()
        if isinstance(body, dict) and body.get("Advice")
    ]

    return {
        "study_name": model.get("Name"),
        "mesh_type": data.get("Mesh type"),
        "analysis_sequence": data.get("Analysis sequence"),
        "part_volume": _val(model.get("Part volume")),
        "material_name": material.get("Manufacturer Name"),
        "material_family": material.get("Family abbreviation"),
        "material_fillers": material.get("Fillers"),
        # With units=UNITS_METRIC these are degC -- the same numbers the
        # Process Settings card seeds itself with from the material.
        "melt_temp": _val(material.get("Melt temperature")),
        "mold_temp": _val(material.get("Mold surface temperature")),
        "melt_temp_range": material.get("Melt temperature range"),
        "mold_temp_range": material.get("Mold temperature range"),
        "max_shear_stress": _val(material.get("Maximum shear stress")),
        "max_shear_rate": _val(material.get("Maximum shear rate")),
        "machine_name": machine.get("Manufacturer Name"),
        "max_clamp_force": _val(machine.get("Maximum machine clamp force")),
        "max_injection_pressure": _val(machine.get("Maximum machine injection pressure")),
        "advice_warnings": warnings,
        "has_results": "Results" in data,
    }


def is_readable_study(model_file):
    """True if the extractor gets anything at all out of this .sdy.

    Cheap integrity check for the Fusion round-trip: a returned study that is
    really a 4 KB stub yields no metadata, while a genuine ~26 MB study yields
    a full model/material block. Costs about a tenth of a second and touches
    neither COM nor the running Synergy session."""
    return extract(model_file, sections=("model",), units=UNITS_SI) is not None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python ai_assistant.py <study.sdy>")
        print("exe:", find_exe())
        sys.exit(2)
    meta = extract(sys.argv[1])
    if meta is None:
        print("No metadata extracted from", sys.argv[1])
        sys.exit(1)
    print(json.dumps(summary(meta), indent=2, ensure_ascii=False))
