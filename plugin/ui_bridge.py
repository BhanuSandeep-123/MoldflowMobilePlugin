import json
import time
import os
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

import sys
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import session_context

# Every file below is PER SYNERGY WINDOW, not per install. Synergy's
# %RunPerInstance startup command gives each window its own process tree, and
# these are the channels those trees talk through -- state, liveness, prompt
# hand-off, panel commands. Sharing one path between two windows is what made
# the second window's launcher see the first window's heartbeat and never
# start a panel, and made an answer given in one window resolve the other
# window's prompt. See session_context.py.
STATE_FILE = session_context.session_path("ui_state.json")

# Out-of-band diagnostics log. Nothing in this module ever pops a dialog
# silently and unexpectedly to the user -- when something can't be shown to
# them, it goes here instead, so it's always possible to find out why a step
# was skipped.
LOG_FILE = HERE / "ui_bridge_log.txt"

# embedded_ui.py (the docked, chrome-less tkinter panel) touches this field in
# ui_state.json roughly every 300ms while it is alive and watching for
# prompts. prompt_user() uses "is this fresh" as its liveness signal instead
# of a network probe -- there is no server/browser in this design anymore.
UI_HEARTBEAT_KEY = "_ui_heartbeat"
UI_HEARTBEAT_MAX_AGE = 15.0  # seconds

# The heartbeat lives in its OWN file, not in ui_state.json.
#
# It used to be a key in the state, which meant the panel rewrote the entire
# state file every 300ms. Every write is read-modify-write on one JSON file, so
# a heartbeat that loaded the state just before the workflow published a prompt
# put the pre-prompt copy back a moment later -- and the prompt vanished before
# anyone saw it ("prompt disappeared before it was answered"). Rare, silent, and
# able to hit ANY prompt in the workflow. Splitting the highest-frequency writer
# out of the shared file removes the collision instead of narrowing it.
HEARTBEAT_FILE = session_context.session_path("ui_heartbeat.txt")

# Last beat successfully read, per process. Used only when a read fails
# outright, so a momentary file-sharing collision cannot be mistaken for the
# panel having died. It never extends the panel's life: max_age still applies.
_LAST_GOOD_BEAT = {"t": None}

# Hard ceiling on how long a single prompt waits for the embedded panel before
# giving up on it. Guards against the panel being alive-but-stuck (e.g. its
# own window lost focus permanently) so the workflow never hangs forever.
PROMPT_MAX_WAIT_SECONDS = 600.0

DEFAULT_STATE = {
    "current_step": "project-setup",
    "step_status": "Running",
    "overall_progress": 0,
    "elapsed_time": "00:00:00",
    "remaining_time": "N/A",
    "activity_log": [],
    "params": {
        "project_name": None,
        "location": None,
        "cad_file": None,
        "cad_diagnostics": None,
        "analysis_sequence": None,
        "material": None,
        "process_settings": None,
        "mesh_status": None,
        "solver_status": None,
        "export_status": None,
        "phase": "Phase 1: Project Setup"
    },
    "diagnostics": {},
    "mesh_progress": {
        "percentage": 0,
        "task": ""
    },
    "solver_progress": {
        "percentage": 0,
        "task": "",
        "phase": ""
    },
    "pending_prompt": None,
    "_ui_heartbeat": None
}

def load_state():
    if not STATE_FILE.exists():
        save_state(DEFAULT_STATE)
        return DEFAULT_STATE
    # A torn read (another process mid-write) must not look like "no state":
    # the caller would carry on with DEFAULT_STATE and save that back, wiping
    # the run. Retry briefly, and only then give up.
    last = None
    for attempt in range(4):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            last = exc
            time.sleep(0.05)
    log_silent("load_state: unreadable state file ({0}); using defaults.".format(last))
    return DEFAULT_STATE

def save_state(state):
    """Atomic write: a reader either sees the previous state or the new one,
    never half of either."""
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(str(tmp), str(STATE_FILE))
    except Exception:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass


def touch_heartbeat():
    """Called by the panel every tick. Writes ONLY the heartbeat file, so the
    panel no longer rewrites ui_state.json just to say 'I am alive'.

    Atomic for the same reason save_state is: a reader that catches a plain
    write half-done gets an empty string, and an unparseable beat used to read
    as "no panel" -- which is how a running panel could be declared dead for
    one unlucky poll, sending a prompt to its fallback."""
    try:
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        os.replace(str(tmp), str(HEARTBEAT_FILE))
    except Exception:
        try:
            HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

def update_state(key, value):
    state = load_state()
    if isinstance(state.get(key), dict) and isinstance(value, dict):
        for k, v in value.items():
            if v is not None or k not in state[key]:
                state[key][k] = v
    else:
        state[key] = value
    save_state(state)

def log_activity(message, status="🟢"):
    state = load_state()
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    state["activity_log"].append({
        "time": timestamp,
        "status": status,
        "text": message
    })
    save_state(state)


# A journey step that has STARTED but not finished.
#
# Until this existed a step's param was either empty (the panel drew
# "Pending...") or a finished value (green tick) -- there was no way to say
# "this stage is underway". That is why the seconds between the model import
# finishing and the CAD Diagnostics card appearing looked to the user like the
# workflow had stopped: the observer's 3s poll, the diagnostics process start,
# the COM attach and the CAD checks all happen with the row still reading
# "Pending", and then a window appears out of nowhere.
#
# Any params value carrying this prefix is rendered by the panel as an amber
# "in progress" row (see embedded_ui._update_journey_summary) and drives the
# header status line while no prompt is on screen. Writing the step's final
# value clears it -- there is nothing separate to reset, so a crash between
# the two cannot strand the panel showing a stale "Waiting..." line for a step
# that already finished.
IN_PROGRESS_PREFIX = "⏳ "


def set_step_in_progress(param_key, message, log=True):
    """Mark one journey step as underway, e.g.

        set_step_in_progress("cad_diagnostics", "Preparing CAD Diagnostics...")

    param_key is a key of state["params"] (the same keys the journey card
    reads). Call it at the moment the work starts, not when its window opens.
    """
    try:
        state = load_state()
        params = state.setdefault("params", {})
        params[param_key] = IN_PROGRESS_PREFIX + str(message)
        state["step_status"] = "Running"
        save_state(state)
    except Exception as exc:
        log_silent("set_step_in_progress({0!r}) failed: {1}".format(param_key, exc))
        return
    if log:
        log_activity(str(message), status="🟡")


def is_in_progress(value):
    """True if a params value was published by set_step_in_progress()."""
    return isinstance(value, str) and value.startswith(IN_PROGRESS_PREFIX)


def in_progress_text(value):
    """The message inside an in-progress value, without its marker."""
    return str(value)[len(IN_PROGRESS_PREFIX):].strip() if is_in_progress(value) else ""


def log_silent(message):
    """Write a diagnostic line to ui_bridge_log.txt. Used for everything that
    would otherwise have to be a popup -- e.g. 'embedded panel unavailable,
    startup prompt skipped'. Never raises, never shows anything to the user."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # The log file stays shared (one place to look), so every line is
        # tagged with the Synergy window it came from -- otherwise two windows
        # running in parallel interleave into an unreadable transcript.
        key = session_context.session_key()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{key}] - {message}\n")
    except Exception:
        pass


def ui_alive(max_age=UI_HEARTBEAT_MAX_AGE):
    """True if the embedded panel (embedded_ui.py) is running and has
    updated its heartbeat recently. This is the only thing that tells
    prompt_user whether anyone can actually answer a prompt right now."""
    # Freshest of the two beats wins. A STALE heartbeat file (left by a panel
    # that died, or by an earlier session) must not mask a live legacy beat --
    # returning on the file alone would report "no panel" with one running.
    beats = []
    try:
        if HEARTBEAT_FILE.exists():
            hb = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
            if hb:
                beats.append(float(hb))
                _LAST_GOOD_BEAT["t"] = float(hb)
            else:
                # Empty content, but the file is being kept: its mtime is
                # still evidence the panel is writing.
                beats.append(HEARTBEAT_FILE.stat().st_mtime)
    except Exception:
        # A failed READ is not evidence of death. On Windows a reader can hit
        # the instant the panel swaps the file in and get a sharing violation;
        # treating that as "no panel" would send a prompt to its fallback with
        # the panel alive and well. Trust the last beat we did read -- it still
        # expires normally through max_age below.
        pass
    if not beats and _LAST_GOOD_BEAT["t"]:
        beats.append(_LAST_GOOD_BEAT["t"])
    try:
        # Legacy in-state heartbeat, for a panel from before the split.
        hb = load_state().get(UI_HEARTBEAT_KEY)
        if hb:
            beats.append(float(hb))
    except Exception:
        pass
    if not beats:
        return False
    return (time.time() - max(beats)) <= max_age


def _native_prompt(title, message, options):
    """Blocking native Win32 dialog. Only ever used for prompts that were
    explicitly allowed to fall back to one (allow_native_fallback=True) --
    never during the silent startup sequence."""
    if not options:
        options = ["Yes", "No"]
    if len(options) != 2:
        return options[-1]
    try:
        import ctypes
        MB_YESNO = 0x00000004
        MB_ICONQUESTION = 0x00000020
        MB_TOPMOST = 0x00040000
        IDYES = 6
        ret = ctypes.windll.user32.MessageBoxW(
            0, str(message), str(title), MB_YESNO | MB_ICONQUESTION | MB_TOPMOST
        )
        return options[0] if ret == IDYES else options[1]
    except Exception:
        return options[-1]


def prompt_user(title, message, options=None, allow_native_fallback=True):
    """Ask the user a question through the embedded (docked, chrome-less)
    panel and return their answer.

    allow_native_fallback controls what happens when the panel can't answer
    (not running, or stops responding mid-wait):
      - True  (default; used by the deep workflow, e.g. mesh diagnostics)
              falls back to a native Win32 Yes/No dialog so the user is never
              stranded mid-analysis.
      - False (used by the silent startup sequence) never shows a dialog at
              all: the attempt is logged to ui_bridge_log.txt and the safe
              default (last option, e.g. "No") is returned so the workflow
              can move on quietly.
    """
    if options is None:
        options = ["Yes", "No"]

    if not ui_alive():
        log_silent(
            f"prompt_user: embedded panel not available for '{title}' "
            f"({'native fallback' if allow_native_fallback else 'no fallback allowed, using default'})."
        )
        if allow_native_fallback:
            return _native_prompt(title, message, options)
        return options[-1]

    state = load_state()
    state["pending_prompt"] = {
        "title": title,
        "message": message,
        "options": options,
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)

    log_activity(f"Prompt: {message} (Waiting for input...)", status="🟡")

    # Wait for the panel to write back the answer. Bail out if it goes away
    # mid-wait or we exceed the ceiling -- either way we never hang forever.
    deadline = time.time() + PROMPT_MAX_WAIT_SECONDS
    misses = 0
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            # Cancelled or cleared
            return options[-1]
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            if "Sequence" in str(title):
                current.setdefault("params", {})["analysis_sequence"] = ans
            save_state(current)
            log_activity(f"User selected: {ans}", status="🟢")
            return ans

        if time.time() > deadline:
            break
        if not ui_alive():
            misses += 1
            if misses >= 5:  # ~1.5s of missed heartbeats -> panel is gone
                break
        else:
            misses = 0

    # Panel became unavailable — clear the stale prompt and fall back per
    # the caller's policy (never silently hang, never surprise-popup during
    # startup).
    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass

    log_silent(f"prompt_user: embedded panel lost while waiting on '{title}'.")
    if allow_native_fallback:
        return _native_prompt(title, message, options)
    return options[-1]


def request_project_form(default_name="MyProject", default_dir=None,
                          timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask the embedded panel to show the native 'Create New Project' form
    (name + directory + Browse) and return {"name":..., "dir":...}, or None
    if the panel is unavailable or the user cancelled.

    Never falls back to a dialog -- this is only used during the silent
    startup sequence. If the panel can't show the form, the caller is
    expected to log and skip project creation for this session."""
    if not ui_alive():
        log_silent("request_project_form: embedded panel not available; skipping.")
        return None

    state = load_state()
    state["pending_prompt"] = {
        "kind": "project_form",
        "title": "Create New Project",
        "message": "Enter a project name and choose where to create it.",
        "default_name": default_name,
        "default_dir": default_dir or "",
        # Create only -- the panel no longer offers Skip, because every later
        # phase needs a project to live in. The "Skip" handling below is kept
        # as a defensive path for a state file left over from an older build.
        "options": ["Create"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)
    log_activity("Prompt: Create New Project (waiting for input...)", status="🟡")

    deadline = time.time() + timeout
    misses = 0
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return None
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            if ans == "Skip" or not isinstance(ans, dict):
                log_activity("Project creation skipped by user.", status="🟡")
                save_state(current)
                return None
            current.setdefault("params", {})["project_name"] = ans.get("name", "MyProject")
            current.setdefault("params", {})["location"] = ans.get("dir", "")
            save_state(current)
            log_activity(f"Project form submitted: {ans}", status="🟢")
            return ans

        if time.time() > deadline or not ui_alive():
            break
        misses += 1

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    log_silent("request_project_form: embedded panel lost or timed out; skipping.")
    return None


def request_file_path(title="Import Model", filters=None,
                       timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask the embedded panel to show a native 'open file' picker (a standard
    OS chooser, not an interrupting dialog) and return the chosen path, or
    None if unavailable/cancelled."""
    if not ui_alive():
        log_silent("request_file_path: embedded panel not available; skipping.")
        return None

    state = load_state()
    state["pending_prompt"] = {
        "kind": "file_picker",
        "title": title,
        "message": "Choose a CAD/mesh file to import.",
        "filters": filters or [],
        "options": ["Browse", "Skip"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)
    log_activity("Prompt: Import Model file picker (waiting for input...)", status="🟡")

    deadline = time.time() + timeout
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return None
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            if not ans or ans == "Skip":
                log_activity("Import file selection skipped by user.", status="🟡")
                save_state(current)
                return None
            current.setdefault("params", {})["cad_file"] = os.path.basename(ans)
            save_state(current)
            log_activity(f"File selected: {ans}", status="🟢")
            return ans

        if time.time() > deadline or not ui_alive():
            break

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    log_silent("request_file_path: embedded panel lost or timed out; skipping.")
    return None


def request_cad_diagnostics_report(report, detail_text="", timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask embedded panel to show CAD diagnostics summary table card and return user answer ('Yes' or 'No')."""
    if not ui_alive():
        log_silent("request_cad_diagnostics_report: embedded panel not available.")
        return "Yes"

    state = load_state()
    state["pending_prompt"] = {
        "kind": "cad_diagnostics_report",
        "title": "CAD Diagnostics Summary",
        "message": "Review CAD geometry diagnostics report below:",
        "report": report,
        "detail_text": detail_text,
        "options": ["Yes", "No"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)

    deadline = time.time() + timeout
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return "Yes"
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            save_state(current)
            return ans

        if time.time() > deadline or not ui_alive():
            break

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    return "Yes"


def request_mesh_diagnostics_report(report, question, buttons, detail_text="",
                                    timeout=PROMPT_MAX_WAIT_SECONDS,
                                    panel_wait=60.0):
    """Show the FULL mesh-diagnostics dialog in the panel and block until the
    user picks one of `buttons`.

    `report` is the dict from collect_mesh_diagnostics() -- the whole thing
    (status, items, counts, mesh_type, total_elements, unavailable), so the
    panel can draw the complete diagnostics table (aspect ratio, dihedral
    angle, volume ratio, ...). `buttons` is [(token, label), ...].

    Returns the chosen TOKEN, or None if the panel never showed the dialog.
    None means "nobody has seen these diagnostics" -- the caller must not
    treat it as a Continue; the mesh gate is a user decision, never a default.

    Three-stage wait, all on real conditions (no fixed sleeps):
      1. panel alive          -- up to `panel_wait` seconds for the heartbeat,
                                 instead of giving up on the first miss.
      2. dialog rendered      -- the panel sets pending_prompt["rendered"]
                                 with the row count once the table is built
                                 and laid out. Until that ack arrives the
                                 dialog is NOT considered shown.
      3. user answered        -- pending_prompt["answer"].
    Stage 2 is why this exists: the old path published a one-line question and
    returned as soon as any answer appeared, so a defaulted/auto-answered
    prompt was indistinguishable from a reviewed one.
    """
    options_map = {label: token for token, label in buttons}
    labels = [label for _t, label in buttons]

    # Stage 1 -- wait for the panel rather than bailing out immediately.
    panel_deadline = time.time() + panel_wait
    while not ui_alive():
        if time.time() > panel_deadline:
            log_silent("request_mesh_diagnostics_report: embedded panel never "
                       "appeared; mesh diagnostics NOT shown.")
            return None
        time.sleep(0.3)

    state = load_state()
    state["pending_prompt"] = {
        "kind": "mesh_diagnostics_report",
        "title": "Mesh Diagnostics",
        "message": question,
        "report": report,
        "detail_text": detail_text,
        "options": labels,
        "rendered": None,
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)
    log_activity("Mesh Diagnostics ({0}): waiting for your review...".format(
        report.get("status", "?")), status="🟡")

    deadline = time.time() + timeout
    misses = 0
    rendered = False
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt or prompt.get("kind") != "mesh_diagnostics_report":
            log_silent("request_mesh_diagnostics_report: prompt disappeared "
                       "before it was answered.")
            return None

        # Stage 2 -- the panel confirms the table is built and populated.
        if not rendered and prompt.get("rendered"):
            rendered = True
            log_silent("request_mesh_diagnostics_report: panel rendered the "
                       "dialog ({0} diagnostic row(s)).".format(
                           prompt.get("rendered")))

        # Stage 3 -- the answer. Ignore any answer that arrives before the
        # render ack: it cannot have come from a user reading the table.
        if prompt.get("answer") is not None:
            if not rendered:
                log_silent("request_mesh_diagnostics_report: ignoring an "
                           "answer that arrived before the render ack.")
                # Re-read before clearing: the panel may have written its
                # render ack in between, and dropping that would leave the
                # dialog open with nobody willing to accept its answer.
                current = load_state()
                fresh = current.get("pending_prompt")
                if fresh and fresh.get("kind") == "mesh_diagnostics_report":
                    fresh["answer"] = None
                    current["pending_prompt"] = fresh
                    save_state(current)
            else:
                ans = prompt["answer"]
                current["pending_prompt"] = None
                current["step_status"] = "Running"
                save_state(current)
                log_activity("Mesh Diagnostics: user selected '{0}'.".format(ans))
                return options_map.get(ans, None)

        if time.time() > deadline:
            log_silent("request_mesh_diagnostics_report: timed out after "
                       "{0}s without a decision.".format(int(timeout)))
            break
        if not ui_alive():
            misses += 1
            if misses >= 5:
                log_silent("request_mesh_diagnostics_report: embedded panel "
                           "disappeared while the dialog was open.")
                break
        else:
            misses = 0

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
#  Live (non-blocking) cards
#
#  Everything above blocks the workflow until the user answers. The results
#  stage cannot work that way: it has to keep driving Synergy (showing the next
#  plot, watching which result the user opened) WHILE a card is on screen. So
#  these three publish a prompt, let the caller carry on, and let it poll for
#  the answer whenever it reaches a safe point.
# --------------------------------------------------------------------------- #

def publish_live_card(kind, title, fields, options):
    """Create or update a non-blocking card. Returns False if the panel is not
    running (the caller then just carries on without a card -- a card is an
    aid to the user, never a gate)."""
    if not ui_alive():
        return False
    try:
        state = load_state()
        prompt = state.get("pending_prompt") or {}
        if prompt.get("kind") != kind:
            prompt = {"kind": kind, "answer": None}
        elif prompt.get("answer") is not None:
            # The user clicked while we were preparing this update. Leave the
            # answer alone -- refreshing the card over it would swallow the
            # click (and with it the Export & Report decision).
            return True
        prompt.update(fields)
        prompt["kind"] = kind
        prompt["title"] = title
        prompt["options"] = options
        prompt.setdefault("answer", None)
        # Writes here are atomic but read-modify-write is not, and the panel
        # records clicks on its own schedule. The answer check at the top of
        # this function can therefore go stale while the fields are assembled,
        # and a plain write would drop a click made in that gap. Re-reading
        # immediately before the write narrows that window to almost nothing.
        state = load_state()
        latest = state.get("pending_prompt") or {}
        if latest.get("kind") == kind and latest.get("answer") is not None:
            prompt["answer"] = latest["answer"]
        state["pending_prompt"] = prompt
        save_state(state)
        return True
    except Exception:
        return False


FOCUS_FILE = session_context.session_path("ui_focus_request.json")


def request_result_focus(label):
    """Panel -> workflow: 'show me this result and open its help'.

    Its own file, for the same reason the heartbeat has one. This started life
    as a field inside pending_prompt, where it collided with the workflow's own
    refresh of the review card: the refresh reads the prompt, the panel writes
    the request, the refresh writes its copy back, request gone. The visible
    symptom was ticking the first result doing nothing while the second worked.

    The sequence number is what makes re-ticking the SAME result count again.
    """
    try:
        seq = 0
        if FOCUS_FILE.exists():
            try:
                seq = int(json.loads(FOCUS_FILE.read_text(encoding="utf-8"))
                          .get("seq") or 0)
            except Exception:
                seq = 0
        payload = json.dumps({"seq": seq + 1, "label": str(label)})
        tmp = FOCUS_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(FOCUS_FILE))
        return seq + 1
    except Exception as exc:
        log_silent("request_result_focus failed for '{0}': {1}".format(label, exc))
        return 0


PANEL_COMMAND_FILE = session_context.session_path("ui_panel_command.json")


def request_panel_state(state, reason=""):
    """Workflow -> panel: fold the panel down to its header ('minimized') or
    open it again ('normal').

    Used by long native Synergy operations -- meshing above all -- where the
    panel has nothing to show but is sitting over the part of Synergy the user
    wants to watch. The panel process keeps running throughout: this only
    drives the same collapse the header's '—' button does, so a prompt raised
    while folded is still waiting when it opens again.

    Its own file rather than a field in ui_state.json, for the reason spelled
    out on FOCUS_FILE: the workflow rewrites whole-state snapshots constantly,
    and a command living inside one is liable to be written back out of a
    stale copy. The sequence number makes a repeat of the SAME request count.

    Never raises and never blocks -- a missing or unresponsive panel just means
    the window stays where it is."""
    want = "minimized" if str(state).lower().startswith("min") else "normal"
    try:
        seq = 0
        if PANEL_COMMAND_FILE.exists():
            try:
                seq = int(json.loads(PANEL_COMMAND_FILE.read_text(encoding="utf-8"))
                          .get("seq") or 0)
            except Exception:
                seq = 0
        payload = json.dumps({"seq": seq + 1, "state": want, "reason": str(reason)})
        tmp = PANEL_COMMAND_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(PANEL_COMMAND_FILE))
        log_silent("Panel state requested: {0}{1}".format(
            want, " ({0})".format(reason) if reason else ""))
        return seq + 1
    except Exception as exc:
        log_silent("request_panel_state({0}) failed: {1}".format(want, exc))
        return 0


def poll_panel_command(after_seq=0):
    """Panel side: (seq, state) for a request newer than `after_seq`, else
    None."""
    try:
        if not PANEL_COMMAND_FILE.exists():
            return None
        data = json.loads(PANEL_COMMAND_FILE.read_text(encoding="utf-8"))
        seq = int(data.get("seq") or 0)
        if seq > int(after_seq or 0) and data.get("state"):
            return seq, str(data["state"])
    except Exception:
        pass
    return None


def clear_panel_command():
    """Drop a command left over from an earlier run, so a fresh session never
    starts by replaying the last one."""
    try:
        if PANEL_COMMAND_FILE.exists():
            PANEL_COMMAND_FILE.unlink()
    except Exception:
        pass


def poll_result_focus(after_seq=0):
    """Workflow side: (seq, label) for a request newer than `after_seq`, else
    None."""
    try:
        if not FOCUS_FILE.exists():
            return None
        data = json.loads(FOCUS_FILE.read_text(encoding="utf-8"))
        seq = int(data.get("seq") or 0)
        if seq > int(after_seq or 0) and data.get("label"):
            return seq, str(data["label"])
    except Exception:
        pass
    return None


def clear_result_focus():
    """Drop any request left over from an earlier run, so the review stage
    never opens help for a result the user has not touched this time."""
    try:
        if FOCUS_FILE.exists():
            FOCUS_FILE.unlink()
    except Exception:
        pass


def poll_live_card(kind):
    """Answer for a live card, or None while the user has not clicked yet.
    Returns None too if the card is gone (panel restarted, prompt replaced)."""
    try:
        prompt = load_state().get("pending_prompt")
        if not prompt or prompt.get("kind") != kind:
            return None
        return prompt.get("answer")
    except Exception:
        return None


def take_live_card_answer(kind):
    """Answer for a live card, CONSUMED: read and reset to None in one write.

    poll_live_card() only reads, which is right for a card whose button ends
    the stage (the answer is read once and the card comes down). A card that
    stays up with a repeatable button needs the answer cleared, or
    publish_live_card() sees a non-None answer on the next refresh, returns
    early to avoid swallowing the click, and the card freezes on the values it
    had at the moment of the first press.

    Read-modify-write in one go so the reset cannot land on top of a second
    click made while we were working."""
    try:
        state = load_state()
        prompt = state.get("pending_prompt")
        if not prompt or prompt.get("kind") != kind:
            return None
        answer = prompt.get("answer")
        if answer is None:
            return None
        prompt["answer"] = None
        state["pending_prompt"] = prompt
        save_state(state)
        return answer
    except Exception:
        return None


def clear_live_card(kind=None):
    """Take the card down. `kind` guards against clearing somebody else's
    prompt if the workflow moved on in between."""
    try:
        state = load_state()
        prompt = state.get("pending_prompt")
        if prompt and (kind is None or prompt.get("kind") == kind):
            state["pending_prompt"] = None
            state["step_status"] = "Running"
            save_state(state)
    except Exception:
        pass


def request_error_recovery_choice(error_details="Model error detected", timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask embedded panel to show Error Recovery prompt with two options:
    'Go back to Fusion' or 'Translate Surface'.
    Returns 'go_back_to_fusion', 'translate_surface', or None if panel unavailable/timed out.
    """
    if not ui_alive():
        log_silent("request_error_recovery_choice: embedded panel not available.")
        return None

    state = load_state()
    state["pending_prompt"] = {
        "kind": "error_recovery",
        "title": "Model Error Recovery",
        "message": str(error_details),
        "options": ["Go back to Fusion to fix model", "Translate Surface"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)

    deadline = time.time() + timeout
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return None
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            save_state(current)
            if "Fusion" in str(ans):
                return "go_back_to_fusion"
            elif "Translate" in str(ans):
                return "translate_surface"
            return str(ans)

        if time.time() > deadline or not ui_alive():
            break

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    return None



def request_material_selection(default_material=None, common_materials=None, timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask embedded panel to show material selector card and return selected material name."""
    if not ui_alive():
        log_silent("request_material_selection: embedded panel not available.")
        return default_material or "POLYFLAM RIPP 3625 CS1"

    if common_materials is None:
        common_materials = [
            "POLYFLAM RIPP 3625 CS1: A Schulman GMBH",
            "Generic PP: Polymer Database",
            "Generic ABS: Polymer Database",
            "Generic PA66: Polymer Database"
        ]

    state = load_state()
    state["pending_prompt"] = {
        "kind": "material_selector",
        "title": "Select Material",
        "message": "Select thermoplastic material for analysis:",
        "default_material": default_material or common_materials[0],
        "common_materials": common_materials,
        "options": ["Select", "Skip"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)

    deadline = time.time() + timeout
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return default_material
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            save_state(current)
            return ans

        if time.time() > deadline or not ui_alive():
            break

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    return default_material


def request_process_settings(fields=None, mold_temp=60.0, melt_temp=200.0, sequence="Fill", timeout=PROMPT_MAX_WAIT_SECONDS):
    """Ask embedded panel to show process settings card and return dict of parameters."""
    if not ui_alive():
        log_silent("request_process_settings: embedded panel not available.")
        return {"mold_temp": mold_temp, "melt_temp": melt_temp}

    state = load_state()
    state["pending_prompt"] = {
        "kind": "process_settings",
        "title": "Process Settings",
        "message": f"Sequence: {sequence} | Review and adjust process defaults:",
        "fields": fields or [],
        "mold_temp": mold_temp,
        "melt_temp": melt_temp,
        "options": ["OK", "Skip"],
        "answer": None
    }
    state["step_status"] = "Waiting"
    save_state(state)

    deadline = time.time() + timeout
    while True:
        time.sleep(0.3)
        current = load_state()
        prompt = current.get("pending_prompt")
        if not prompt:
            return {"mold_temp": mold_temp, "melt_temp": melt_temp}
        if prompt.get("answer") is not None:
            ans = prompt["answer"]
            current["pending_prompt"] = None
            current["step_status"] = "Running"
            save_state(current)
            if isinstance(ans, dict):
                return ans
            return {"mold_temp": mold_temp, "melt_temp": melt_temp}

        if time.time() > deadline or not ui_alive():
            break

    try:
        current = load_state()
        current["pending_prompt"] = None
        current["step_status"] = "Running"
        save_state(current)
    except Exception:
        pass
    return {"mold_temp": mold_temp, "melt_temp": melt_temp}

