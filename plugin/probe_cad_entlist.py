"""
probe_cad_entlist.py
--------------------
READ-ONLY diagnostic probe. Answers one question definitively:

    Can we build a NON-EMPTY EntList of CAD bodies through the Synergy COM API?

Everything else we want (CADDiagnostic.Compute, and Modeler's Fusion round
trip) depends on that one thing, and it has failed in every run so far --
SelectFromString never raises, but the resulting list always reports Size 0.

This probe does NOT call ModifiedWithInventorFusion and does NOT modify the
study. It is safe to run on an open study at any time.

The key test is ConvertToString(): if SelectFromString actually populated the
list, ConvertToString() must echo the IDs back. That single check separates
the two competing explanations, which no amount of code reading can:

    (A) selection genuinely fails  -> ConvertToString() == ""
    (B) selection works, Size is   -> ConvertToString() == " BD1" while
        being read incorrectly        Size still reads 0

Run it the same way as the other macros (as a Synergy macro, or with the
plugin's venv while Synergy is open). Results go to probe_cad_entlist_report.txt
next to this file, and to stdout.
"""

from pathlib import Path
import datetime
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

REPORT = HERE / "probe_cad_entlist_report.txt"
_lines = []


def log(msg):
    text = str(msg)
    _lines.append(text)
    try:
        print(text, flush=True)
    except Exception:
        pass


def flush_report():
    try:
        REPORT.write_text("\n".join(_lines), encoding="utf-8")
        print("\nReport written to: {0}".format(REPORT), flush=True)
    except Exception as e:
        print("Could not write report: {0}".format(e), flush=True)


def raw_of(obj):
    """Unwrap to a raw PyIDispatch."""
    raw = getattr(obj, "_raw", lambda: obj)()
    if hasattr(raw, "_oleobj_"):
        return raw._oleobj_
    return raw


def invoke(disp, name, *args):
    import pythoncom
    dispid = disp.GetIDsOfNames(0, name)
    flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
    return disp.Invoke(dispid, 0, flags, True, *args)


def read_size_every_way(raw_el):
    """Read Size through several mechanisms. If these disagree, the bug is in
    how we read Size, not in the selection itself."""
    results = {}

    import pythoncom
    for name in ("Size", "size"):
        # a) combined METHOD|PROPERTYGET (what the plugin currently uses)
        try:
            results["{0} (METHOD|PROPERTYGET)".format(name)] = invoke(raw_el, name)
        except Exception as e:
            results["{0} (METHOD|PROPERTYGET)".format(name)] = "ERR: {0}".format(e)
        # b) PROPERTYGET only -- Size is documented as a property, not a method
        try:
            dispid = raw_el.GetIDsOfNames(0, name)
            results["{0} (PROPERTYGET only)".format(name)] = raw_el.Invoke(
                dispid, 0, pythoncom.DISPATCH_PROPERTYGET, True)
        except Exception as e:
            results["{0} (PROPERTYGET only)".format(name)] = "ERR: {0}".format(e)
    return results


def convert_to_string(raw_el):
    for name in ("ConvertToString", "convert_to_string"):
        try:
            return invoke(raw_el, name)
        except Exception as e:
            last = e
    return "ERR: {0}".format(last)


def main():
    log("=" * 70)
    log("CAD EntList probe -- {0}".format(datetime.datetime.now()))
    log("=" * 70)

    from synergy_connect import get_synergy
    sy = get_synergy(allow_launch=False)
    log("Connected to Synergy.")

    study_doc = sy.StudyDoc()
    if study_doc is None:
        log("FATAL: no active study. Open a study with an imported CAD model first.")
        return
    try:
        log("Active study: {0}".format(sy.StudyDoc().StudyName()))
    except Exception:
        pass

    try:
        sy.Build()
    except Exception:
        pass

    # ---- 1. What does Synergy say the CAD bodies are? --------------------
    log("")
    log("--- 1. GetAllCADBodies ---")
    for vis in (True, False):
        try:
            s = study_doc.GetAllCADBodies(vis)
            log("GetAllCADBodies({0}) = {1!r}   (len={2})".format(
                vis, s, len(s) if s is not None else None))
        except Exception as e:
            log("GetAllCADBodies({0}) raised: {1}".format(vis, e))

    try:
        body_string = study_doc.GetAllCADBodies(True)
    except Exception:
        body_string = None
    if not body_string:
        log("No CAD body string -- nothing further to test.")
        return

    # ---- 2. Candidate strings, INCLUDING trailing-space forms -------------
    # Every CAD-body example in Autodesk's own reference is written as
    # " BD1 " (leading AND trailing space). GetAllCADBodies here returns
    # " BD1" with no trailing space. Synergy's parser may need the trailing
    # delimiter to commit the final token -- that is what these test.
    raw = str(body_string)
    candidates = [
        (raw,                    "raw GetAllCADBodies result"),
        (raw + " ",              "raw + TRAILING space  <-- matches the docs"),
        (" " + raw.strip() + " ", "' BD1 ' exact doc form"),
        (raw.strip(),            "stripped"),
        (raw.strip() + " ",      "stripped + trailing space"),
    ]

    # ---- 3. Try every EntList source x every candidate --------------------
    log("")
    log("--- 2. SelectFromString across sources x candidates ---")
    log("KEY: 'convert' is the ConvertToString() round trip. If convert shows")
    log("     the body IDs but size is 0, the selection WORKED and our Size")
    log("     read is the bug. If convert is empty, selection truly failed.")

    sources = []
    for label, owner in (("CADDiagnostic", sy.CADDiagnostic()),
                         ("Modeler", sy.Modeler()),
                         ("StudyDoc", study_doc)):
        try:
            el = invoke(raw_of(owner), "CreateEntityList")
            if el is not None:
                sources.append((label, el))
        except Exception as e:
            log("{0}.CreateEntityList() failed: {1}".format(label, e))

    log("Sources available: {0}".format([s[0] for s in sources]))

    winner = None
    for label, el in sources:
        raw_el = raw_of(el)
        log("")
        log("### source: {0}".format(label))
        for cand, why in candidates:
            try:
                invoke(raw_el, "Clear")
            except Exception:
                pass
            try:
                invoke(raw_el, "SelectFromString", cand)
            except Exception as e:
                log("  {0!r:28} [{1}] -> SelectFromString RAISED: {2}".format(cand, why, e))
                continue

            conv = convert_to_string(raw_el)
            sizes = read_size_every_way(raw_el)
            size_summary = ", ".join("{0}={1}".format(k, v) for k, v in sizes.items())
            log("  {0!r:28} [{1}]".format(cand, why))
            log("        convert={0!r}".format(conv))
            log("        {0}".format(size_summary))

            nonempty = isinstance(conv, str) and conv.strip() != ""
            if nonempty and winner is None:
                winner = (label, cand, why)

    # ---- 4. Verdict -------------------------------------------------------
    log("")
    log("=" * 70)
    if winner:
        log("RESULT: A WORKING COMBINATION WAS FOUND.")
        log("  source    : {0}.CreateEntityList()".format(winner[0]))
        log("  string    : {0!r}".format(winner[1]))
        log("  why       : {0}".format(winner[2]))
        log("This combination should be used in build_cad_body_entlist().")
    else:
        log("RESULT: NO combination produced a non-empty EntList.")
        log("Every ConvertToString() came back empty, so SelectFromString is")
        log("genuinely not selecting these CAD bodies -- this is NOT a Size")
        log("read bug. That points at the Synergy install/model itself rather")
        log("than at the calling code, and the next step is to confirm whether")
        log("the equivalent UI action works: in Synergy, Geometry tab > Modify")
        log("panel > Autodesk Fusion 360, select the body, click Apply.")
        log("  - If the UI button ALSO fails  -> environment/licensing issue.")
        log("  - If the UI button WORKS       -> the API path differs from the")
        log("    UI path and we escalate to Autodesk with this probe output.")
    log("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("PROBE ERROR:\n" + traceback.format_exc())
    finally:
        flush_report()
