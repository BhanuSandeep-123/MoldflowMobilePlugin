"""
synergy_connect.py
------------------
Connection helper for the Autodesk Moldflow Synergy 2027 automation (COM) API.

WHAT WE LEARNED (the hard way) about Synergy 2027's COM server:

  * ProgID ``synergy.Synergy`` / CLSID {ECB5F86C-9418-11D6-8099-001083FF030C},
    an out-of-process LocalServer (synergy.exe).
  * It does NOT publish itself in the Running Object Table under the plain
    ProgID, so ``GetActiveObject("synergy.Synergy")`` fails even while Synergy
    is open.
  * When Synergy launches a script (a macro / custom command) it sets the
    ``SAInstance`` environment variable to a per-session moniker of the form
    ``{GUID}``. You bind it and read its ``GetSASynergy`` **property** to get
    the Synergy object. (This moniker is only valid while that exact Synergy
    session is alive.)
  * CRUCIAL: the Synergy objects expose NO type information. Under win32com's
    dynamic dispatch that means every *no-argument* method (StudyDoc, GetUnits,
    CreateIntegerArray, ...) is otherwise treated as a *property*, and
    ``GetSASynergy`` (a property) is otherwise treated as a *method* -> the
    infamous "Member not found". The clean fix is to invoke EVERY member with
    the combined flag ``DISPATCH_METHOD | DISPATCH_PROPERTYGET``, which works
    for properties and methods alike. That is what the ``Syn`` wrapper does.

USAGE
    from synergy_connect import get_synergy
    syn = get_synergy()          # a Syn-wrapped Synergy object
    print(syn.Build())           # <-- call EVERYTHING with parentheses
    print(syn.GetUnits())
    sd = syn.StudyDoc()          # None if no study is open

Requires pywin32 (win32com / pythoncom).
"""

from __future__ import annotations

import atexit
import gc
import os
import weakref

import pythoncom
import win32com.client as win32

PROGID = "synergy.Synergy"

# ---------------------------------------------------------------------------
# Deterministic COM teardown (root cause of the post-workflow Synergy crash).
#
# Every Syn wrapper holds an out-of-process IDispatch proxy into synergy.exe.
# The workflow keeps many of these alive inside closure/frame REFERENCE CYCLES
# (log/show_dialog/etc. capture sy, study_doc, plot_mgr, viewer, ...), so they
# are NOT freed when the functions return -- they die during interpreter
# finalization, in arbitrary order, partly after COM apartment teardown.
# synergy.exe then has its automation stubs abandoned instead of Released; the
# next incoming COM call (the observer's 3-second poll) hits that stale state
# and the server faults: the observer log shows RPC_E_SERVERFAULT
# (-2147417851, "The server threw an exception") three polls in a row, then
# the Synergy 2027 Error Report dialog -- seconds after every workflow run
# ended, success or failure alike.
#
# The fix: track every wrapper in a weakref registry and, from an atexit
# handler (atexit runs BEFORE the interpreter tears down garbage cycles),
# explicitly drop every live proxy in reverse-creation order while COM is
# still initialized, then CoUninitialize. Synergy receives orderly Release
# calls and a clean apartment shutdown, so its state stays valid after the
# automation process exits.
# ---------------------------------------------------------------------------
_ALL_SYN = []          # weakrefs to every Syn ever created, in creation order
_shutdown_registered = False


def release_com_objects():
    """Explicitly drop every live Synergy COM proxy (children first)."""
    for ref in reversed(_ALL_SYN):
        obj = ref()
        if obj is None:
            continue
        try:
            object.__setattr__(obj, "_d", None)   # drops CDispatch -> Release
            object.__setattr__(obj, "_ids", {})
        except Exception:
            pass
    del _ALL_SYN[:]
    # Collect the closure/frame cycles NOW, while COM is still initialized,
    # so any proxies they held indirectly are also released cleanly.
    try:
        gc.collect()
    except Exception:
        pass


def _com_shutdown():
    release_com_objects()
    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass

# Invoke every member permissively so we don't care whether the server
# implemented a given name as a method or a property.
_FLAGS = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET


def _is_dispatch(x) -> bool:
    return type(x).__name__ == "PyIDispatch"


class Syn:
    """
    Uniform late-bound wrapper for Synergy COM objects (which carry no type
    info). Call every member with parentheses -- properties and methods alike.
    COM objects returned by a call are auto-wrapped in ``Syn``; a COM null is
    returned as Python ``None``; scalars/arrays are returned as-is.
    """

    __slots__ = ("_d", "_ids", "__weakref__")

    def __init__(self, disp):
        object.__setattr__(self, "_d", disp)
        object.__setattr__(self, "_ids", {})  # cache name -> dispid (saves a COM
                                              # round-trip on repeated access)
        # Track for the atexit teardown (see release_com_objects above).
        # Weakrefs only -- this never extends a wrapper's lifetime.
        _ALL_SYN.append(weakref.ref(self))
        if len(_ALL_SYN) % 1000 == 0:   # keep the long-running observer lean
            _ALL_SYN[:] = [r for r in _ALL_SYN if r() is not None]

    def _raw(self):
        """The underlying win32com CDispatch (for advanced/manual calls)."""
        return object.__getattribute__(self, "_d")

    def __setattr__(self, name, value):
        if name in ("_d", "_ids"):
            object.__setattr__(self, name, value)
            return
        d = object.__getattribute__(self, "_d")
        unwrapped = value._raw() if isinstance(value, Syn) else value
        
        # Try to resolve dispid
        ids = object.__getattribute__(self, "_ids")
        dispid = ids.get(name)
        if dispid is None:
            try:
                dispid = d._oleobj_.GetIDsOfNames(0, name)
                ids[name] = dispid
            except pythoncom.com_error:
                # If not a COM property, try standard setattr fallback
                try:
                    setattr(d, name, unwrapped)
                    return
                except Exception as e:
                    raise AttributeError("Cannot set attribute '{0}' on {1}: {2}".format(name, d, e))
        
        try:
            d._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, True, unwrapped)
        except Exception as e:
            # Log the COM failure for debugging
            try:
                print("COM DISPATCH_PROPERTYPUT failed for '{0}' with value '{1}': {2}".format(name, unwrapped, e), flush=True)
            except Exception:
                pass
            # Fallback to standard setattr or raise
            try:
                setattr(d, name, unwrapped)
            except Exception:
                raise AttributeError("Cannot set COM property '{0}' on {1}: {2}".format(name, d, e))

    def _set(self, name, *args):
        """Perform a DISPATCH_PROPERTYPUT call for named parameterized property."""
        d = object.__getattribute__(self, "_d")
        marshalled = []
        for a in args:
            if isinstance(a, Syn):
                marshalled.append(a._raw()._oleobj_)
            else:
                marshalled.append(a)
        
        # Get dispid of the property
        ids = object.__getattribute__(self, "_ids")
        dispid = ids.get(name)
        if dispid is None:
            try:
                dispid = d._oleobj_.GetIDsOfNames(0, name)
                ids[name] = dispid
            except pythoncom.com_error:
                raise AttributeError(name)
                
        d._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, True, *marshalled)

    def __getattr__(self, name):
        d = object.__getattribute__(self, "_d")
        ids = object.__getattribute__(self, "_ids")
        dispid = ids.get(name)
        if dispid is None:
            try:
                dispid = d._oleobj_.GetIDsOfNames(0, name)
            except pythoncom.com_error:
                raise AttributeError(name)
            ids[name] = dispid

        def call(*args):
            marshalled = []
            for a in args:
                if isinstance(a, Syn):
                    marshalled.append(a._raw()._oleobj_)  # pass the raw IDispatch
                else:
                    marshalled.append(a)
            res = d._oleobj_.Invoke(dispid, 0, _FLAGS, True, *marshalled)
            if _is_dispatch(res):
                return Syn(win32.Dispatch(res))
            return res

        return call

    def __repr__(self):
        return f"<Syn {object.__getattribute__(self, '_d')!r}>"


def _from_sainstance():
    """Bind the session that launched us (SAInstance is only set for macros)."""
    sa = os.environ.get("SAInstance")
    if not sa:
        return None
    getter = Syn(win32.Dispatch(win32.GetObject(sa)))  # bind {GUID} moniker
    syn = getter.GetSASynergy()                         # property -> Synergy obj
    return syn


def attach():
    """Attach to a running Synergy (via SAInstance, then ROT), else None.

    SAInstance is the only binding that identifies WHICH Synergy window we
    belong to. The ROT fallback below asks for "a" running Synergy and takes
    whatever the Running Object Table hands back -- fine with one window open,
    ambiguous with two. It is kept because it is the only thing that works for
    processes started outside Synergy's macro host, but when it is used with
    more than one Synergy alive the choice is arbitrary, so say so in the log
    rather than letting a cross-window attach look like normal operation."""
    try:
        syn = _from_sainstance()
        if syn is not None:
            return syn
    except Exception:
        pass

    try:
        syn = Syn(win32.Dispatch(win32.GetActiveObject(PROGID)))
    except pythoncom.com_error:
        return None

    try:
        if _synergy_process_count() > 1:
            _warn(
                "attach(): SAInstance was not set, so this process fell back to "
                "the Running Object Table with more than one synergy.exe "
                "running -- it may have attached to a DIFFERENT Synergy window "
                "than the one that started it. Launch via run_startup.vbs (or "
                "any Synergy macro) so SAInstance identifies the window."
            )
    except Exception:
        pass
    return syn


def _synergy_process_count() -> int:
    """How many synergy.exe are running. Best-effort; 0 if it can't be told."""
    try:
        import session_context
        return session_context.synergy_process_count()
    except Exception:
        return 0


def _warn(message: str) -> None:
    """Diagnostic only -- never a dialog, never fatal."""
    try:
        import ui_bridge
        ui_bridge.log_silent(message)
    except Exception:
        pass


def launch():
    """Start a brand-new Synergy process and return its (wrapped) object."""
    return Syn(win32.DispatchEx(PROGID))


def get_synergy(allow_launch: bool = True) -> Syn:
    """Return a live, Syn-wrapped Synergy object (see module docstring)."""
    global _shutdown_registered
    pythoncom.CoInitialize()
    if not _shutdown_registered:
        # atexit is LIFO and runs before interpreter finalization tears down
        # garbage cycles, so this releases every proxy while COM is still up.
        atexit.register(_com_shutdown)
        _shutdown_registered = True

    syn = attach()
    if syn is not None:
        return syn

    if not allow_launch:
        raise RuntimeError(
            "No Moldflow Synergy session available.\n"
            " - Run this as a Synergy macro (run_plugin.vbs) so SAInstance is set, or\n"
            " - start Synergy first, or\n"
            " - call get_synergy(allow_launch=True) to launch a new instance."
        )
    return launch()



# --------------------------------------------------------------------------- #
# High-level helpers for the startup workflow (used by moldflow_startup.py)
# --------------------------------------------------------------------------- #

def has_active_study(syn: Syn) -> bool:
    """Return True if a study document is currently open/active."""
    try:
        sd = syn.StudyDoc()
        return sd is not None
    except Exception:
        return False


if __name__ == "__main__":
    s = get_synergy(allow_launch=False)
    print("Connected to Synergy")
    print("  Build :", s.Build())
    print("  Units :", s.GetUnits())

