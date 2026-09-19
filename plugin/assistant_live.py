r"""
assistant_live.py
-----------------
Bridge between the production plugin and the standalone **AIAssistantPOC**, so
the review deck's closing summary carries the answer Moldflow 2027's own AI
Assistant gives for the open study instead of one we regenerate ourselves.

Why this file exists at all
---------------------------
`ai_report_summary.py` was written when the panel's prose looked unreachable:
`aiassistant.exe` is only an offline metadata extractor and Autodesk publishes
no API for the chat. The POC disproved that -- the panel is a WebView2 view, and
with the Chrome DevTools port open it can be driven and read. So the summary no
longer has to be inferred from threshold rules; it can be *asked for*.

The POC stays where it is, unchanged and independently testable. This module
only:

  * finds it (sibling folder by default, MOLDFLOW_AIASSISTANT_POC to override),
  * puts it on sys.path for the duration of the call,
  * asks the question from `report/prompts.py`, and
  * hands back `report.parse.parse_summary()`'s dict.

Nothing in the plugin imports the POC directly, so when Autodesk reshuffles the
panel only the POC moves -- which is the whole point of keeping it separate.

TWO THINGS THE CALLER MUST KNOW
-------------------------------
1. **It sends a real request.** One prompt per report, on the signed-in Autodesk
   account, and it appears in the user's chat history. `fetch_summary()` will not
   send unless `allow_send=True` is passed explicitly.
2. **It needs the DevTools port**, which only exists if Synergy was started with
   ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=0``. The
   plugin attaches to an already-running Synergy and so cannot turn it on
   itself; run `enable_assistant_port.bat` once and restart Synergy. When the
   port is absent every function here returns None and the deck falls back to
   the local summary -- never an exception, never a broken report.

The user does NOT have to open the Assistant panel. If it is closed,
`assistant_panel.open_panel()` opens it through Synergy's own UI, waits for the
chat app to finish loading, and closes it again when the answer is in -- a panel
the user already had open is left alone. What cannot be avoided is the panel
being *drawn*: the Assistant is a WebView2 view and a view that was never laid
out has no page to read. So this is unattended, not invisible.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Search order for the POC checkout. The sibling layout
# (Documents/MoldflowSynergyPlugin/{MoldflowSynergyPlugin,AIAssistantPOC}) is the
# one on this machine; the others cover a nested copy or a moved checkout.
_POC_CANDIDATES = [
    HERE.parent / "AIAssistantPOC",
    HERE / "AIAssistantPOC",
    HERE.parent.parent / "AIAssistantPOC",
]

# Modules the POC owns. Cleared from sys.modules after a call so the plugin's
# own namespace is not left holding a package called `assistant` or `report`.
_POC_PACKAGES = ("assistant", "cdp", "report", "send_prompt")


def find_poc():
    """Absolute path to the AIAssistantPOC checkout, or None."""
    override = os.environ.get("MOLDFLOW_AIASSISTANT_POC")
    if override:
        p = Path(override)
        if (p / "report" / "prompts.py").is_file():
            return p
    for p in _POC_CANDIDATES:
        try:
            if (p / "report" / "prompts.py").is_file():
                return p.resolve()
        except OSError:
            pass
    return None


@contextlib.contextmanager
def _poc_on_path():
    """Import the POC's packages, then put sys.path and sys.modules back.

    Scoped rather than permanent: `assistant`, `cdp` and `report` are generic
    names, and leaving them importable inside a long-lived Synergy session is
    how a future module ends up shadowed by something it never asked for.
    """
    poc = find_poc()
    if poc is None:
        raise ImportError(
            "AIAssistantPOC not found next to the plugin. Set "
            "MOLDFLOW_AIASSISTANT_POC to its folder.")
    before = set(sys.modules)
    sys.path.insert(0, str(poc))
    try:
        yield poc
    finally:
        try:
            sys.path.remove(str(poc))
        except ValueError:
            pass
        for name in list(sys.modules):
            root = name.split(".", 1)[0]
            if name not in before and root in _POC_PACKAGES:
                sys.modules.pop(name, None)


# How the debug port gets switched on. WebView2 takes additional browser
# arguments from three places, in this order of precedence:
#
#   1. the WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS environment variable
#   2. HKLM\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
#   3. HKCU\...\AdditionalBrowserArguments        (only if HKLM has no match)
#
# with the value NAME being the app's own executable name, or "*" for every
# WebView2 app. The registry route is what `enable_assistant_port.bat` uses and
# is better than the environment variable in two ways: it applies to synergy.exe
# ALONE rather than to every WebView2 app the user runs, and it is read when the
# WebView2 environment is CREATED rather than when the host process starts --
# so a Synergy that has not yet opened the Assistant panel picks it up without
# being restarted.
#
# Docs: WebView2 browser flags / CreateCoreWebView2EnvironmentWithOptions.
PORT_ARG = "--remote-debugging-port=0"
POLICY_KEY = r"Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
POLICY_VALUES = ("synergy.exe", "*")


_PORT_RE = re.compile(r"--remote-debugging-port=(\d+)")


def _port_setting():
    """The FIRST additional-browser-arguments setting WebView2 would honour,
    as (value, where), searched in WebView2's own order of precedence."""
    env = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if env:
        return env, "the WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS environment variable"
    try:
        import winreg
    except ImportError:
        return "", ""
    for hive, label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                        (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            with winreg.OpenKey(hive, POLICY_KEY) as key:
                for name in POLICY_VALUES:
                    try:
                        value, _kind = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if value:
                        return str(value), "{0} policy, value {1!r}".format(
                            label, name)
        except OSError:
            continue
    return "", ""


def port_configured():
    """(status, detail) — is Synergy's WebView2 debug port set up correctly?

        'ok'         a port is configured, and it is port 0 (auto-assign)
        'fixed_port' a port is configured, but a FIXED one — see below
        'off'        nothing is configured

    Why a fixed port is a fault and not merely a preference: Synergy runs TWO
    WebView2 environments (the Simulation Hub tab and the Assistant panel) and
    both honour the same setting. With `--remote-debugging-port=9222` both try
    to bind 9222, the first one to start wins, and which one that is varies run
    to run. When the hub wins — which is what happened here — the Assistant has
    no port at all and looks, from the outside, exactly like a machine where
    nothing was ever configured. Port 0 gives each environment its own free
    port and the ambiguity disappears.

    Note this says the SETTING is in place, not that the port is open: a
    WebView2 environment keeps the arguments it was created with, so a change
    reaches a session that has not yet opened the panel and no other.
    """
    value, where = _port_setting()
    if not value:
        return "off", ""
    m = _PORT_RE.search(value)
    if not m:
        return "off", where
    if m.group(1) == "0":
        return "ok", where
    return "fixed_port", "{0} (currently {1!r})".format(where, value)


def port_status(log=None):
    """What state the Assistant channel is in right now, without sending
    anything. Returns one of:

        'ready'             -- endpoint up and the Assistant page is there
        'panel_closed'      -- endpoint up, but no Assistant page target
        'no_assistant_page' -- Synergy's debug port works, but no Assistant
                               WebView2 was ever created this session
        'no_port'           -- Synergy is up without the debug port
        'no_synergy'        -- Synergy is not running
        'no_poc'            -- the POC checkout could not be found

    Read-only: process/port listing plus one localhost probe.
    """
    try:
        with _poc_on_path():
            from cdp import browser  # noqa: WPS433 (POC package, scoped import)
            endpoint = browser.find_assistant_endpoint()
            if endpoint:
                return "ready" if browser.find_page(endpoint) else "panel_closed"
            # No Assistant page anywhere. Two causes with OPPOSITE fixes, and
            # this used to report both as 'no_port' -- which then told the user
            # to fix an environment variable that was already correct. If any
            # WebView2 owned by synergy.exe has a debug port, the variable did
            # its job and the missing piece is the Assistant panel itself.
            if browser.synergy_webview_endpoints():
                return "no_assistant_page"
            running = browser.synergy_running()
            return "no_port" if running else "no_synergy"
    except ImportError:
        return "no_poc"
    except Exception as e:            # discovery must never raise into a report
        if log:
            log("  Assistant port probe failed: {0}".format(e))
        return "no_port"


# Plain-English meaning of every port_status() value, for the diagnostic line
# fetch_report_data writes before it decides anything. Deliberately separate
# from PORT_HELP below: that one is phrased to complete the sentence "AI
# Assistant unavailable -- ...", so it has no entry for a healthy channel and
# its wording is advice rather than observation.
#
# ASCII only. The log file is written in a codepage that mangles em dashes
# (they show up as replacement characters in diagnostics_*.log), and a
# diagnostic nobody can read is worse than no diagnostic.
_STATE_MEANING = {
    # NOT "panel open": the channel stays live after the panel is closed or
    # hidden, which is the whole basis of the initialise-once-then-hide design.
    # Saying "open" here made the log identical for a visible panel and a
    # hidden one, so it could not confirm the hide had worked.
    "ready": "channel live and readable (the panel need not be visible)",
    "panel_closed": ("debug port is up but there is no Assistant page - the "
                     "panel is not open in this Synergy session"),
    "no_assistant_page": ("Synergy's WebView2 debug port IS working, but no "
                          "Assistant panel was created this session - check "
                          "that you are signed in to Autodesk in Synergy and "
                          "that the panel opens at startup. Do NOT touch the "
                          "environment variable; it is not the problem here"),
    "no_port": ("no WebView2 owned by synergy.exe has a debug port - the "
                "environment variable did not reach synergy.exe at launch"),
    "no_synergy": "no Synergy process found",
    "no_poc": "the AIAssistantPOC folder is not beside the plugin",
}


PORT_HELP = {
    "no_poc": "the AIAssistantPOC folder is not beside the plugin",
    "no_synergy": "Synergy is not running",
    "no_port": ("Synergy's WebView2 has no debug port — run "
                "enable_assistant_port.bat once (and restart Synergy if the "
                "Assistant panel was already opened this session)"),
    "no_assistant_page": ("the debug port is working but Synergy never created "
                          "an Assistant panel this session — sign in to your "
                          "Autodesk account in Synergy (top right) and open the "
                          "AI Assistant panel once; it then stays reachable for "
                          "the rest of the session even after you close it"),
    "panel_closed": "the AI Assistant panel is not open in Synergy",
}


def _wait_for_composer(page, timeout=60.0, log=print):
    """Block until the panel's prompt box exists, or give up.

    A panel that has just been opened is reachable over CDP well before its
    inner React app has rendered a composer, so attaching is not the same as
    being ready to type. Locating the box is read-only — this is exactly what
    the POC's dry run does — so polling it costs nothing and changes nothing.
    """
    from assistant.prompt import ComposerNotFound, composer
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            with composer(page):
                return True
        except ComposerNotFound as e:
            last = e
        except Exception as e:
            last = e
        time.sleep(2.0)
    log("  Assistant prompt box never appeared ({0}).".format(last))
    return False


def _ask(page, prompt, timeout, interval, log):
    """One question, one finished answer. Returns the cleaned text or None.

    Split out because a report asks more than one thing and each question costs
    a request: keeping the panel open across them is the difference between two
    exchanges and two open/close cycles.
    """
    from assistant.prompt import ComposerNotFound
    from send_prompt import send_and_capture
    try:
        result = send_and_capture(page, prompt, timeout=timeout,
                                  interval=interval,
                                  log=lambda m: log("    " + str(m).strip()))
    except ComposerNotFound as e:
        log("  AI Assistant prompt box not found: {0}".format(e))
        return None
    answer = (result.get("answer") or "").strip()
    if not answer:
        log("  AI Assistant returned no answer ({0}{1}).".format(
            result.get("status"),
            ": " + result["reason"] if result.get("reason") else ""))
        return None
    log("  Answered in {0:.1f}s ({1} chars).".format(
        result.get("elapsed", 0.0), len(answer)))
    return answer


def fetch_report_data(labels=None, allow_send=False, timeout=300.0,
                      interval=2.0, open_panel=True, log=print):
    """Everything the deck wants from the Assistant, in one panel session.

    Returns {"summary": <parse_summary dict or None>,
             "notes":   {result label: one-sentence comment}}

    `labels` are the results the deck actually captured; when given, a second
    question asks for a sentence on each so the result slides can say what this
    study's figure means instead of only what the result type is. Two requests
    per report, no more -- the panel is opened once and closed once around both.
    """
    empty = {"summary": None, "notes": {}}
    if not allow_send:
        log("  AI Assistant: not asked (sending is disabled for this run).")
        return empty

    state = port_status(log=log)
    # Record the RESOLVED state, not just the configured setting. The line
    # below this used to be the only evidence in the log, and it reports what
    # the port setting SAYS, not what Synergy actually got -- so a degraded
    # report looked identical whether the debug port never reached synergy.exe
    # ('no_port') or the port was fine and the panel simply was not open
    # ('panel_closed'). Those have opposite fixes. (2026-08-05: two runs fell
    # back to the local summary and the log could not tell them apart.)
    #
    # Round two of the same bug, same day: 'no_port' ITSELF covered two causes,
    # because the endpoint scan counts WebView2 apps machine-wide and Synergy
    # having none looked the same as Synergy having one without an Assistant
    # page. Study 36's log blamed the environment variable while that variable
    # was provably set and working. 'no_assistant_page' now separates them.
    log("  Assistant channel state: {0} - {1}.".format(
        state, _STATE_MEANING.get(state, "unrecognised state")))

    # A missing panel is the one failure we can fix ourselves, whether that
    # shows up as 'panel_closed' or as 'no_assistant_page' (port fine, panel
    # never created -- typically signed out of Autodesk). A missing debug port
    # is not fixable here: it is read once at WebView2 startup, so no amount of
    # clicking helps a Synergy that was started without it.
    opened, panel_control = False, None
    if state in ("panel_closed", "no_assistant_page", "no_port") and open_panel:
        # Check the SETTING before touching the user's window. Opening the
        # panel when the port cannot appear costs a minute of waiting, pops a
        # panel in the user's face for nothing, and — worse — creates the
        # WebView2 environment without the flag, which is what then forces a
        # Synergy restart even after the setting is corrected.
        configured, where = port_configured()
        if configured == "fixed_port":
            log("  AI Assistant not available: the WebView2 debug port is set "
                "to a FIXED port in {0}.".format(where))
            log("  Synergy runs two WebView2 views (Simulation Hub and the "
                "Assistant) and both try to bind that one port; whichever "
                "starts first wins, so the Assistant is often left with none.")
            log("  Fix it once with:  enable_assistant_port.bat  "
                "(switches to port 0, one free port each), then restart Synergy.")
            return empty
        if configured != "ok":
            log("  AI Assistant not available: Synergy's WebView2 has no debug "
                "port configured, so the panel cannot be read.")
            log("  Fix it once with:  enable_assistant_port.bat")
            log("  (writes an app-scoped policy for synergy.exe only; the "
                "deck falls back to the local summary until then.)")
            return empty
        log("  Debug port configured via {0}.".format(where))
        found_control = True
        try:
            import assistant_panel
            opened, panel_control = assistant_panel.open_panel(
                lambda: port_status() == "ready", log=log)
            found_control = panel_control is not None
        except Exception as pe:
            log("  Could not open the Assistant panel: {0}".format(pe))
        state = port_status(log=log)
        # Same again after the open attempt: this is the line that says whether
        # opening the panel actually changed anything.
        log("  Assistant channel state after open attempt: {0} - {1}.".format(
            state, _STATE_MEANING.get(state, "unrecognised state")))
        if state != "ready" and not found_control:
            # Do NOT fall through to the generic port advice below: the port is
            # configured and this run proved it. The reason is that the panel's
            # launcher is not in the UI, and telling the user to reconfigure a
            # port that is already correct sends them the wrong way entirely.
            log("  AI Assistant unavailable — its button is not in Synergy's "
                "UI, so the panel cannot be opened automatically. Open the AI "
                "Assistant panel once in this Synergy session and it stays "
                "reachable from then on, even after you close it.")
            return empty
        if state != "ready" and opened:
            # The setting is right but the port is not there, which means this
            # WebView2 environment was created before the setting was written.
            # Arguments are fixed at creation, so only a restart clears it.
            log("  The panel opened but exposed no debug port. Its WebView2 was "
                "created earlier in this Synergy session, before the port was "
                "configured — close Synergy completely and start it again.")

    if state != "ready":
        log("  AI Assistant unavailable — {0}.".format(
            PORT_HELP.get(state, state)))
        if opened:
            _restore_panel(panel_control, log)
        return empty

    out = dict(empty)
    try:
        with _poc_on_path():
            from cdp.page import AssistantPage, PageNotFound
            from report import parse, prompts

            try:
                page = AssistantPage.attach()
            except PageNotFound as e:
                log("  AI Assistant unavailable — {0}".format(e))
                return out

            # Reachable is not the same as ready when the panel was opened a
            # moment ago: the shell answers CDP before the chat app has drawn
            # its prompt box.
            if not _wait_for_composer(page, log=log):
                return out

            log("  Asking the Moldflow AI Assistant to summarise this study...")
            answer = _ask(page, prompts.SUMMARY_PROMPT, timeout, interval, log)
            if answer:
                data = parse.parse_summary(answer)
                data["answer"] = answer
                data["prompt"] = prompts.SUMMARY_PROMPT
                out["summary"] = data
                log("  Summary: {0} result(s), {1} observation(s), "
                    "{2} recommendation(s){3}.".format(
                        len(data["results"]), len(data["observations"]),
                        len(data["recommendations"]),
                        "" if data["ok"] else " (format not recognised)"))

            labels = [l for l in (labels or []) if str(l).strip()]
            if labels:
                log("  Asking for a comment on each of the {0} exported "
                    "result(s)...".format(len(labels)))
                answer = _ask(page, prompts.notes_prompt(labels), timeout,
                              interval, log)
                if answer:
                    out["notes"] = parse.parse_notes(answer, labels)
                    out["notes_answer"] = answer
                    log("  Commentary returned for {0} of {1} result(s).".format(
                        len(out["notes"]), len(labels)))
            return out
    except Exception as e:
        # A report must never fail because an unsupported panel changed shape.
        log("  AI Assistant unavailable: {0}".format(e))
        return out
    finally:
        # Leave Synergy as we found it. Only ours to close: a panel the user
        # already had open stays open.
        if opened:
            _restore_panel(panel_control, log)


def fetch_summary(allow_send=False, timeout=300.0, interval=2.0,
                  open_panel=True, log=print):
    """Just the summary — `fetch_report_data()` without the per-result
    commentary. Kept because the summary alone is what a caller usually wants,
    and it is what the deck fell back on before the notes existed.

    `open_panel` (default True) lets this open Synergy's Assistant panel itself
    when the user has not, and close it again afterwards — the whole exchange
    then needs no interaction at all. See assistant_panel.py for what that can
    and cannot do.

    The return value is exactly `report.parse.parse_summary()`'s dict —
    study / material / analysis / results / observations / recommendations /
    sources / raw — plus:

        answer  the cleaned answer text as the panel wrote it
        prompt  the question that was asked
        status  'complete' or 'timeout'
        elapsed seconds spent waiting for the response

    None means "no answer" for ANY reason: POC missing, port closed, panel
    closed, composer already had the user's own text in it, or the response
    never settled. Every one of those is normal and none is fatal — the caller
    keeps the locally computed summary.
    """
    out = fetch_report_data(labels=None, allow_send=allow_send, timeout=timeout,
                            interval=interval, open_panel=open_panel, log=log)
    return out.get("summary")


def _restore_panel(control, log):
    try:
        import assistant_panel
        assistant_panel.close_panel(
            control, ready=lambda: port_status() == "ready", log=log)
    except Exception as e:
        log("  Could not close the Assistant panel again: {0}".format(e))


if __name__ == "__main__":
    print("POC        :", find_poc() or "(not found)")
    _cfg, _where = port_configured()
    print("port config:", {
        "ok": "OK (port 0, auto-assign) via " + _where,
        "fixed_port": "BROKEN -- a FIXED port in " + _where,
        "off": "OFF -- nothing configured",
    }[_cfg])
    if _cfg == "fixed_port":
        print("             Synergy's two WebView2 views race for that one "
              "port and the\n             Assistant usually loses. Run "
              "enable_assistant_port.bat to switch\n             to port 0, "
              "then restart Synergy.")
    elif _cfg == "off":
        print("             Run enable_assistant_port.bat once.")
    _state = port_status()
    print("port status:", _state)
    if _state == "ready":
        print("             the channel can be read now (the panel does not "
              "have to be visible).")
    elif _state == "panel_closed":
        print("             fine -- the plugin opens the panel itself.")
    elif _state == "no_assistant_page":
        print("             the debug port IS reaching Synergy, so leave the "
              "environment\n             variable alone. Synergy just never "
              "created an Assistant panel:\n             check you are signed "
              "in to Autodesk (top right) and open the\n             AI "
              "Assistant panel once.")
    elif _state == "no_port" and _cfg == "ok":
        print("             configured, but this Synergy session's WebView2 was "
              "created before\n             that. Close Synergy completely and "
              "start it again.")
    if "--yes" in sys.argv:
        import json
        d = fetch_summary(allow_send=True)
        print(json.dumps(d, indent=2, ensure_ascii=False) if d else "(no answer)")
    else:
        print("\nAdd --yes to send a real prompt and print the parsed answer.")
