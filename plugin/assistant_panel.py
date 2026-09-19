r"""
assistant_panel.py
------------------
Opens Moldflow's AI Assistant panel — and closes it again — so the report can be
produced without the user having to open anything.

WHY THIS IS A SEPARATE FILE
---------------------------
`assistant_live.py` talks to the panel's *page* over the DevTools protocol and
knows nothing about Synergy's window. This file is the opposite: it drives
Synergy's own UI through **UI Automation** (the same accessibility API a screen
reader uses) and knows nothing about CDP. Keeping them apart means a change to
Autodesk's ribbon lands here and a change to the chat markup lands in the POC.

WHY UI AUTOMATION AND NOT A WINDOW MESSAGE
-------------------------------------------
Synergy's ribbon is owner-drawn: its buttons are not child HWNDs, so the
`EnumChildWindows` + `BM_CLICK` approach used elsewhere in this plugin for
standard dialogs finds nothing to click. UI Automation sees the ribbon the way
an assistive tool does, which is the only supported way in.

WHAT IT CANNOT DO
-----------------
Make the panel invisible. The Assistant is a WebView2 view; it has to be
realised and laid out before its page exists at all, and a hidden panel has no
page to read. So the panel does appear on screen for as long as the question is
being answered, and is put back the way it was found afterwards. "In the
background" here means *the user does nothing* — not that nothing is drawn.

Every function is best-effort and returns a bool. Nothing here raises into a
report: a panel that will not open costs the live summary, not the deck.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import time

# Names Autodesk has used, or plausibly would, for the ribbon control. Tried as
# exact matches first because UI Automation filters those natively (fast); the
# substring sweep below is the fallback for anything not on this list.
PANEL_NAMES = (
    # What this build actually calls it: an untranslated resource id, found by
    # the sweep below on 2026-08-04 and promoted here so the fast path gets it.
    #
    # "Support Assistant" is not a different feature despite the name. The page
    # the POC attaches to is served from `ase-cdn.autodesk.com/adp/ad-csi-panel-
    # web/...` and titles itself "Autodesk Assistant" -- the ADP support panel
    # IS the assistant. The control lives in Autodesk's InfoCenter strip
    # alongside Search and the sign-in button, not on the Moldflow ribbon.
    "ID_IC_SupportAssistantButton",
    "AI Assistant",
    "Autodesk AI Assistant",
    "Autodesk Assistant",
    "Assistant",
    "AI assistant",
    "Moldflow AI Assistant",
)

_NAME_RE = re.compile(r"\bai\b.*assistant|assistant", re.I)

# Control types worth sweeping when no exact name matched. Ribbon items surface
# as buttons or checkboxes; a docked panel's launcher is sometimes a tab item.
_SWEEP_TYPES = ("Button", "CheckBox", "MenuItem", "TabItem", "ListItem",
                "SplitButton", "Custom")


# --------------------------------------------------------------------------- #
#  Persisted dock state -- the reliable way to have the panel already open
# --------------------------------------------------------------------------- #
#
# Synergy is an MFC application and saves its docking layout to the registry on
# exit. The Assistant is one of those docked bars:
#
#   HKCU\Software\Autodesk\Moldflow Synergy\<ver>\<profile>\WorkState_v1_1\
#       DockState\Bar-N
#           BarID     = 700
#           ClassName = CAutodeskAssistantDialog
#           WindowName= Autodesk Assistant
#           Visible   = False      <-- present ONLY when the bar is hidden
#
# MFC writes `Visible` only for a bar that is hidden, so DELETING that value is
# what marks the bar visible for the next launch. Do it while Synergy is closed
# -- it rewrites the whole DockState on exit and would overwrite anything we
# set during a session.
#
# This matters because it is the one mechanism that needs no UI at all: with
# the bar persisted visible, the Assistant is up the moment Synergy starts, the
# plugin sees the channel ready and never has to find or press a button.
_DOCKSTATE_ROOT = r"Software\Autodesk\Moldflow Synergy"
_ASSISTANT_CLASS = "cautodeskassistantdialog"
_ASSISTANT_BAR_ID = 700


def _iter_dockstate_bars():
    """Yield (key_path, values) for every persisted dock bar, all versions and
    profiles. Synergy keeps one WorkState per named configuration, so the bar
    can appear more than once and all of them matter."""
    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DOCKSTATE_ROOT)
    except OSError:
        return
    versions = []
    try:
        i = 0
        while True:
            versions.append(winreg.EnumKey(root, i))
            i += 1
    except OSError:
        pass
    for ver in versions:
        base = "{0}\\{1}".format(_DOCKSTATE_ROOT, ver)
        try:
            vkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
        except OSError:
            continue
        profiles = []
        try:
            i = 0
            while True:
                profiles.append(winreg.EnumKey(vkey, i))
                i += 1
        except OSError:
            pass
        for prof in profiles:
            dock = "{0}\\{1}\\WorkState_v1_1\\DockState".format(base, prof)
            try:
                dkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, dock)
            except OSError:
                continue
            bars = []
            try:
                i = 0
                while True:
                    bars.append(winreg.EnumKey(dkey, i))
                    i += 1
            except OSError:
                pass
            for bar in bars:
                path = "{0}\\{1}".format(dock, bar)
                try:
                    bkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
                except OSError:
                    continue
                values = {}
                try:
                    i = 0
                    while True:
                        n, v, _t = winreg.EnumValue(bkey, i)
                        values[n] = v
                        i += 1
                except OSError:
                    pass
                yield path, values


def _is_assistant_bar(values):
    cls = str(values.get("ClassName", "")).strip().lower()
    if cls == _ASSISTANT_CLASS:
        return True
    try:
        return int(values.get("BarID", 0)) == _ASSISTANT_BAR_ID and not cls
    except (TypeError, ValueError):
        return False


def persisted_panel_state():
    """[(registry path, visible)] for every persisted Assistant bar.

    `visible` mirrors MFC's own convention: the bar is visible unless a
    `Visible` value says otherwise.
    """
    out = []
    try:
        for path, values in _iter_dockstate_bars():
            if not _is_assistant_bar(values):
                continue
            raw = values.get("Visible")
            hidden = str(raw).strip().lower() in ("false", "0") if raw is not None else False
            out.append((path, not hidden))
    except Exception:
        pass
    return out


def ensure_persisted_visible(log=print, synergy_must_be_closed=True):
    """Mark the Assistant bar visible for the NEXT Synergy launch.

    Returns (changed, message). Refuses while Synergy is running, because it
    rewrites the whole dock state on exit and would undo this immediately --
    call it after the process has gone (run_startup.vbs already watches for
    that) or from the one-time setup script.
    """
    if synergy_must_be_closed and _synergy_pids():
        return False, "Synergy is still running; the dock state is rewritten on exit."

    bars = persisted_panel_state()
    if not bars:
        return False, ("no persisted Autodesk Assistant bar found -- open the "
                       "panel once and close Synergy, which writes it")

    import winreg
    changed = 0
    for path, visible in bars:
        if visible:
            continue
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                 winreg.KEY_SET_VALUE)
            # Deleting is what marks it visible; MFC writes this value only for
            # a HIDDEN bar, so setting it to True would not be understood.
            winreg.DeleteValue(key, "Visible")
            changed += 1
            log("  Assistant panel marked visible for the next Synergy launch "
                "({0}).".format(path.rsplit("\\", 3)[-1]))
        except OSError as e:
            log("  Could not update {0}: {1}".format(path, e))
    if changed:
        return True, "{0} bar(s) updated".format(changed)
    return False, "already set to open at startup"


def _uia():
    """The UI Automation root object, or None if it is unavailable.

    Imported lazily: `comtypes` generates a wrapper module for
    UIAutomationCore.dll on first use, and there is no reason to pay that in a
    session that never asks for the Assistant.
    """
    from comtypes.client import CreateObject, GetModule
    GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    return CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA


# Without this, every tasklist call POPS A CONSOLE WINDOW when the caller has
# no console of its own. The detached pythonw helper that polls at Synergy
# startup is exactly that caller, and it put seven empty black windows on the
# desktop before this was added (2026-08-05).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _synergy_pids():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq synergy.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW).stdout
    except Exception:
        return set()
    return {int(m) for m in re.findall(r'"synergy\.exe","(\d+)"', out)}


def synergy_is_running():
    """True while any synergy.exe process is alive.

    Public because the dock state can only be written once they have ALL gone
    -- Synergy rewrites the whole thing on exit. Callers that want to wait out
    a shutdown poll this rather than parsing ensure_persisted_visible()'s
    refusal message.
    """
    return bool(_synergy_pids())


def find_window(log=None):
    """The Synergy main window as a UI Automation element, or None.

    Matched by owning process rather than by title: the title carries the study
    name and changes with every open document.
    """
    try:
        uia, UIA = _uia()
    except Exception as e:
        if log:
            log("  UI Automation unavailable: {0}".format(e))
        return None, None, None
    pids = _synergy_pids()
    if not pids:
        return None, None, None
    try:
        root = uia.GetRootElement()
        kids = root.FindAll(UIA.TreeScope_Children, uia.CreateTrueCondition())
    except Exception as e:
        if log:
            log("  Could not enumerate top-level windows: {0}".format(e))
        return None, None, None

    # Every top-level window this process owns, biggest first. The main frame is
    # normally the only one that matters, but returning just the biggest meant a
    # control living on another of Synergy's frames could never be found -- so
    # the list is kept and searched in order.
    owned = []
    best, best_area = None, -1
    for i in range(kids.Length):
        try:
            el = kids.GetElement(i)
            if el.CurrentProcessId not in pids:
                continue
            r = el.CurrentBoundingRectangle
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            owned.append((area, el))
            # The main frame is the biggest window the process owns; splash and
            # tooltip windows are also top-level and would otherwise win.
            if area > best_area:
                best, best_area = el, area
        except Exception:
            continue
    _CACHE["windows"] = [el for _a, el in sorted(owned, key=lambda t: -t[0])]
    return best, uia, UIA


# Every top-level window the Synergy process owns, filled in by find_window()
# and read by find_control() so the search is not limited to the main frame.
_CACHE = {}


def _invoke(el, UIA, log=None):
    """Press an element, whatever kind of control it turns out to be.

    Four mechanisms in order of how faithfully they model a click. Toggle is
    tried before Invoke because a panel launcher is usually a toggle, and its
    Invoke (if it has one at all) can be a no-op.
    """
    attempts = (
        ("Toggle", UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern, "Toggle"),
        ("Invoke", UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern, "Invoke"),
        ("SelectionItem", UIA.UIA_SelectionItemPatternId,
         UIA.IUIAutomationSelectionItemPattern, "Select"),
        ("LegacyIAccessible", UIA.UIA_LegacyIAccessiblePatternId,
         UIA.IUIAutomationLegacyIAccessiblePattern, "DoDefaultAction"),
    )
    for label, pattern_id, interface, method in attempts:
        try:
            pat = el.GetCurrentPattern(pattern_id)
            if not pat:
                continue
            getattr(pat.QueryInterface(interface), method)()
            if log:
                log("  Panel control pressed via {0}.".format(label))
            return True
        except Exception:
            continue
    if log:
        log("  Found the panel control but no way to press it.")
    return False


def _find_control_once(win, uia, UIA, log=None):
    """One pass over one window. See find_control for the retry wrapper."""
    # Exact names first: UI Automation evaluates a property condition natively,
    # so this costs one cross-process call instead of walking the whole ribbon.
    # Both Name and AutomationId are tried -- on this build the InfoCenter
    # controls carry the resource id in each, but not every control does.
    for prop in (UIA.UIA_NamePropertyId, UIA.UIA_AutomationIdPropertyId):
        for name in PANEL_NAMES:
            try:
                cond = uia.CreatePropertyCondition(prop, name)
                el = win.FindFirst(UIA.TreeScope_Descendants, cond)
                if el:
                    if log:
                        log("  Assistant control found by {0}: {1!r}".format(
                            "name" if prop == UIA.UIA_NamePropertyId
                            else "automation id", name))
                    return el
            except Exception:
                continue

    # Nothing matched, so sweep the controls that could plausibly be it and read
    # their names ourselves. Bounded to one FindAll per control type rather than
    # a full-tree walk, which on a ribbon this size is thousands of elements.
    for type_name in _SWEEP_TYPES:
        try:
            type_id = getattr(UIA, "UIA_{0}ControlTypeId".format(type_name), None)
            if type_id is None:
                continue
            cond = uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, type_id)
            found = win.FindAll(UIA.TreeScope_Descendants, cond)
        except Exception:
            continue
        for i in range(found.Length):
            try:
                el = found.GetElement(i)
                hay = (el.CurrentName or "") + " " + (el.CurrentAutomationId or "")
                if _NAME_RE.search(hay):
                    if log:
                        log("  Assistant control found by sweep: {0!r} "
                            "({1})".format(hay.strip()[:40], type_name))
                    return el
            except Exception:
                continue
    return None


def find_control(win, uia, UIA, log=None, timeout=25.0):
    """The control that shows the Assistant panel, or None.

    Retries, because the strip it lives in is not populated the instant the
    window exists. The Assistant button belongs to Autodesk's **InfoCenter** --
    the same strip as Search, Subscription, Communication Center and sign-in --
    and that strip is web-backed and fills in asynchronously after startup. A
    single look taken while the workflow is still in CAD diagnostics can miss a
    control that is there thirty seconds later.

    This is worth the wait only once per report, and the wait is skipped
    entirely on the common path where the very first look succeeds.
    """
    deadline = time.time() + max(0.0, timeout)
    attempt = 0
    windows = _CACHE.get("windows") or [win]
    while True:
        attempt += 1
        el = None
        for w in windows:
            el = _find_control_once(w, uia, UIA,
                                    log=log if attempt == 1 else None)
            if el is not None:
                break
        if el is not None:
            if attempt > 1 and log:
                log("  Assistant control appeared after {0:.0f}s.".format(
                    timeout - (deadline - time.time())))
            return el
        if time.time() >= deadline:
            break
        if attempt == 1 and log:
            log("  Assistant control not in the UI yet; waiting up to "
                "{0:.0f}s for InfoCenter to finish loading...".format(timeout))
        time.sleep(3.0)
    if log:
        # Observed 2026-08-04: the control is an Autodesk **InfoCenter** item
        # (`ID_IC_SupportAssistantButton`, same family as Search, Subscription,
        # Favorites and the sign-in button), and InfoCenter shows it only some
        # of the time. It was in the tree during one session and completely
        # absent from the next, while the sign-in button sat there at full
        # size -- so the likeliest gate is simply not being signed in. The
        # Assistant needs that account anyway: every prompt is billed to it.
        log("  No AI Assistant control in Synergy's UI right now.")
        log("  It is an Autodesk InfoCenter button and only appears once "
            "you are signed in to your Autodesk account in Synergy — sign in "
            "(top right), or open the AI Assistant panel yourself and re-run.")
    return None


def open_panel(ready, timeout=60.0, log=print):
    """Open the Assistant panel and wait until `ready()` says it is reachable.

    `ready` is a zero-argument callable returning True once the panel's page can
    be read — passed in rather than imported so this module stays free of any
    DevTools knowledge.

    Returns (opened, control): `opened` is True only if WE opened it, so the
    caller knows whether it is ours to close again.
    """
    if ready():
        return False, None                 # already open; leave it alone

    win, uia, UIA = find_window(log=log)
    if win is None:
        log("  Synergy's main window not found — cannot open the Assistant.")
        return False, None

    control = find_control(win, uia, UIA, log=log)
    if control is None:
        return False, None

    log("  Opening the AI Assistant panel...")
    if not _invoke(control, UIA, log=log):
        return False, None

    # The panel has to create its WebView2 environment, load the CDN shell and
    # then the inner app before there is anything to read. First open in a
    # session is the slow one; later opens are near-instant.
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ready():
            log("  Assistant panel ready after {0:.0f}s.".format(time.time() - t0))
            return True, control
        time.sleep(2.0)

    log("  Assistant panel did not become readable within {0:.0f}s.".format(timeout))
    return True, control                   # we did open it; still ours to close


def close_panel(control, ready=None, log=print):
    """Put the panel back.

    Pressing the control a second time closes it when it is a toggle, which is
    what a panel launcher normally is. When it is a plain button, pressing it
    again may simply re-focus the panel instead — so if `ready` is given, the
    result is checked and the outcome reported rather than assumed. Leaving a
    panel open is an annoyance; a stray click hunting for an X would be worse.
    """
    if control is None:
        return False
    try:
        UIA = _uia()[1]
    except Exception:
        return False
    log("  Closing the AI Assistant panel again.")
    if not _invoke(control, UIA, log=None):
        log("  The panel could not be closed automatically — it is still open.")
        return False
    if ready is not None:
        time.sleep(1.5)
        if ready():
            log("  The Assistant panel is still open (its ribbon control is "
                "not a toggle). Close it by hand if it is in the way.")
            return False
    return True


# Title the Assistant DOCK PANE itself carries -- not the InfoCenter launcher
# (that is PANEL_NAMES). Verified 2026-08-05 by enumerating Synergy's windows:
# the pane is a dialog-class window (#32770) whose caption is exactly this, and
# the WebView2 lives in a Chrome_WidgetWin_1 child of it.
PANEL_WINDOW_NAMES = ("Autodesk Assistant",)


def find_panel_window(log=None):
    """The Assistant dock pane as a UI Automation element, or None.

    Deliberately separate from find_control(): that looks for the InfoCenter
    BUTTON that toggles the panel, which is frequently absent from the tree
    (it only materialises once the panel has been opened in this session, and
    it depends on Autodesk sign-in state). The pane itself is present whenever
    the panel is on screen, which is exactly the case we need to close.
    """
    win, uia, UIA = find_window(log=log)
    if uia is None:
        return None, None, None
    for candidate in (_CACHE.get("windows") or ([win] if win is not None else [])):
        for name in PANEL_WINDOW_NAMES:
            try:
                cond = uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
                el = candidate.FindFirst(UIA.TreeScope_Descendants, cond)
                if el:
                    if log:
                        log("  Found the Assistant panel window "
                            "({0}).".format(name))
                    return el, uia, UIA
            except Exception:
                continue
    if log:
        log("  The Assistant panel window is not present (already closed?).")
    return None, uia, UIA


def _find_pane_hwnd(visible_only=True):
    """Win32 handle of the Assistant dock pane, or 0.

    `visible_only` keeps this honest once the pane has been hidden: the window
    still EXISTS after SW_HIDE (that is the point -- its WebView2 stays alive),
    so a finder that ignored visibility would keep reporting a panel that is
    not on screen, and callers would re-hide it and log that they had.

    Win32 rather than UI Automation on purpose. UIA exposes only the pane's WEB
    CONTENT (RootWebArea / 'Autodesk Assistant MFE') -- the MFC dock chrome and
    its close button are owner-drawn and appear nowhere in the UIA tree, so
    there is no accessible button to press. The window itself is plainly
    visible to Win32.
    """
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    EWP = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    pids = _synergy_pids()
    found = {"hwnd": 0}

    def _cls(h):
        b = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, b, 256)
        return b.value

    def _txt(h):
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        return b.value

    def _walk(parent):
        def cb(h, _l):
            try:
                if not found["hwnd"] and _cls(h) == "#32770" \
                        and _txt(h) in PANEL_WINDOW_NAMES \
                        and (u.IsWindowVisible(h) or not visible_only):
                    found["hwnd"] = h
                    return False
                _walk(h)
            except Exception:
                pass
            return True
        u.EnumChildWindows(parent, EWP(cb), 0)

    def _top(h, _l):
        try:
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value in pids and u.IsWindowVisible(h):
                _walk(h)
        except Exception:
            pass
        return not found["hwnd"]

    u.EnumWindows(EWP(_top), 0)
    return found["hwnd"]


def close_panel_window(log=print, ready=None, settle=1.5):
    """Take the Assistant panel off screen for the rest of the session.

    Hides the dock pane window rather than closing it. Three mechanisms were
    tried live on 2026-08-05 and only this one works:

      * the pane's own X -- not reachable, it is owner-drawn and absent from
        the UIA tree (the pane's only UIA descendants are the web content);
      * WM_CLOSE to the pane -- ignored by an MFC dock pane;
      * WM_COMMAND with the bar id (700) to the main frame -- no effect.

    Hiding does NOT spare us the dock-state problem, though it was tempting to
    assume so: tested 2026-08-05, Synergy still persists the bar as
    Visible=False on exit, exactly as if the user had closed it. So
    moldflow_observer.ensure_assistant_panel_for_next_launch() remains
    LOAD-BEARING -- without it the next session starts with no panel, no
    WebView2 and no channel, and the report quietly loses its Recommendations.

    Returns True when the pane is off screen afterwards. Never raises: if it
    cannot be hidden the panel simply stays where it is, which is how this
    worked before and still produces a correct report.

    KNOWN RISK: because MFC's own visibility state is bypassed, a later layout
    recalculation inside Synergy could put the pane back. Not observed, but not
    ruled out -- if a user reports the panel reappearing mid-session, this is
    why.
    """
    import ctypes

    hwnd = _find_pane_hwnd()
    if not hwnd:
        log("  The AI Assistant panel is not on screen; nothing to hide.")
        return False

    u = ctypes.windll.user32
    try:
        u.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception as e:
        log("  Could not hide the AI Assistant panel: {0}".format(e))
        return False

    time.sleep(settle)
    if u.IsWindowVisible(hwnd):
        log("  The AI Assistant panel is still on screen; leaving it as is.")
        return False
    log("  AI Assistant panel taken off screen for this session.")

    if ready is not None:
        # The invariant this whole design rests on. If it ever fails, hiding is
        # NOT safe on that build -- say so loudly rather than silently shipping
        # degraded reports.
        try:
            if ready():
                log("  AI Assistant channel still ready with the panel hidden.")
            else:
                log("  WARNING: hiding the panel ALSO dropped the AI Assistant "
                    "channel on this build. Restoring it.")
                u.ShowWindow(hwnd, 5)  # SW_SHOW -- undo rather than lose the summary
                return False
        except Exception:
            pass
    return True


def initialise_and_hide(ready, timeout=180.0, poll=3.0, log=print):
    """Wait for the Assistant channel to come up, then close the panel.

    This is the startup step that makes the Assistant invisible for the rest of
    the session. The panel must be SHOWN once for Synergy to create its
    WebView2 -- hiding the dock bar instead produces no channel at all (tested
    2026-08-05) -- so the panel opening briefly at startup is unavoidable. What
    is avoidable is leaving it there for the whole workflow.

    Returns (channel_ready, panel_closed). Waiting is capped because a session
    where the panel never initialises (signed out, no debug port) must not keep
    a background process alive indefinitely.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if ready():
                break
        except Exception as e:
            log("  Assistant readiness probe failed: {0}".format(e))
            return False, False
        time.sleep(poll)
    else:
        log("  AI Assistant channel did not come up within {0:.0f}s; leaving "
            "the panel alone.".format(timeout))
        return False, False

    log("  AI Assistant initialised; closing its panel for the rest of the "
        "session.")
    return True, close_panel_window(log=log, ready=ready)


if __name__ == "__main__":
    import sys

    # Called by run_startup.vbs the moment synergy.exe exits, when the docking
    # layout has just been written and can still be corrected for next time.
    if "--ensure-visible" in sys.argv:
        for _p, _v in persisted_panel_state():
            print("persisted: visible={0}  {1}".format(_v, _p))
        _changed, _msg = ensure_persisted_visible(log=print)
        print("result: {0}".format(_msg))
        sys.exit(0)

    import assistant_live

    def _ready():
        return assistant_live.port_status() == "ready"

    # Startup step, launched detached by moldflow_startup.py: wait for the
    # panel that the persisted dock state opened to finish initialising, then
    # close it so the user gets their workspace back for the whole workflow.
    if "--init-and-hide" in sys.argv:
        _ready_ok, _closed = initialise_and_hide(_ready, log=print)
        print("channel ready: {0}  panel closed: {1}".format(_ready_ok, _closed))
        sys.exit(0)

    print("port status:", assistant_live.port_status())
    win, uia, UIA = find_window(log=print)
    print("synergy window:", "found" if win is not None else "NOT FOUND")
    if win is not None:
        el = find_control(win, uia, UIA, log=print)
        print("assistant control:", el.CurrentName if el else "NOT FOUND")
        if "--open" in sys.argv and el is not None:
            opened, ctl = open_panel(_ready, log=print)
            print("opened:", opened, " ready:", _ready())
            if opened and "--keep" not in sys.argv:
                close_panel(ctl)
