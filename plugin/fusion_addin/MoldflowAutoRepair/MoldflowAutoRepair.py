"""
MoldflowAutoRepair.py
----------------------
Best-effort Fusion add-in: when a model is opened via Moldflow Synergy's
built-in "Modify with Autodesk Fusion 360" round trip (Modeler's
ModifiedWithInventorFusion / Geometry tab > Modify panel > Autodesk Fusion 360
button), try to automatically run Inspect > Validate with repair enabled,
then click "Return to Moldflow" so the Moldflow-side automation can resume
without the user having to do it by hand.

IMPORTANT -- UNSUPPORTED / UNVERIFIED:
There is no documented Fusion API for the Validate command's results, for
triggering an automatic repair, or for the "Return to Moldflow" command. Both
are driven here through Application.executeTextCommand() with internal
command IDs that are NOT part of Autodesk's public API and can change or
break without notice:

  - "Commands.Start FusionSurfaceValidateCommand" + SetString/SetBool params
    is a pattern reported to work by other Fusion API users (see the
    Autodesk Community thread "Inspect/Validate Bodies Tool"), but whether it
    auto-commits without a final user click has not been confirmed here.
  - There is no known internal ID for "Return to Moldflow" anywhere in public
    documentation or community references. This add-in discovers it at
    runtime by scanning ui.commandDefinitions for anything mentioning
    "moldflow", rather than hard-coding a guessed ID.

Because of that, this add-in is deliberately conservative: if it cannot find
a Moldflow-related command registered in Fusion (the signal that this really
is a Moldflow round-trip session), it does nothing at all. That keeps it
inert during any normal, non-Moldflow use of Fusion. If Fusion's Validate
step does not auto-commit, the add-in will have started the command and
selected the bodies, but the user may still need to click OK once in Fusion
-- that is a safe degraded outcome, not a crash.

Everything this add-in does is appended to MoldflowAutoRepair_log.txt next to
this file so behavior can be diagnosed and the detection/command IDs refined
after testing against a real Moldflow + Fusion round trip.
"""

import traceback
import datetime
import time
import os

import adsk.core
import adsk.fusion

app = None
ui = None
handlers = []

_LOG_FILE = os.path.join(os.path.dirname(__file__), "MoldflowAutoRepair_log.txt")
_processed_docs = set()
_dumped_commands_once = False
_dumped_env_once = False

# Discovered live by this add-in's own command scan (see MoldflowAutoRepair_log.txt,
# 2026-07-28): the Moldflow connector registers these in Fusion when a round-trip
# session is active. This is no longer a guess -- it is the observed id.
RETURN_TO_MOLDFLOW_CMD_ID = "ReturnToExternalAppCmdDefMoldflow"

# Never fire "Return to Moldflow" unless Fusion is genuinely idle -- no command
# dialog still open. Returning while the Validate dialog is up does nothing
# (the call lands on a blocked UI and is dropped), and worse, if the user is
# repairing by hand it yanks the model back mid-edit. Moldflow then waits
# forever on IsInventorFusionCadEditDone. Confirm, then return.
AUTO_RETURN_ONLY_WHEN_IDLE = True


def _log(message):
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("{0} - {1}\n".format(ts, message))
    except Exception:
        pass


def _find_moldflow_commands():
    """Scan every registered command definition for anything that looks like
    it belongs to the Moldflow-Fusion connector. This is the only way to both
    detect a round-trip session and locate the 'Return to Moldflow' command,
    since neither is documented anywhere."""
    candidates = []
    try:
        all_defs = ui.commandDefinitions
        for i in range(all_defs.count):
            cmd_def = all_defs.item(i)
            try:
                cid = cmd_def.id or ""
                cname = cmd_def.name or ""
            except Exception:
                continue
            haystack = (cid + " " + cname).lower()
            if "moldflow" in haystack:
                candidates.append(cmd_def)
    except Exception as e:
        _log("Command definition enumeration failed: {0}".format(e))
        return []

    if candidates:
        _log("Found {0} Moldflow-related command definition(s): {1}".format(
            len(candidates), [(c.id, c.name) for c in candidates]))
    return candidates


def _dump_environment_once():
    """Record WHICH Fusion environment the round trip actually lands in, and
    what commands are reachable there.

    This was the blind spot. The Moldflow round trip does not open the Design
    workspace -- it opens Simulation > Simplify (browser shows 'Simulation
    Models', ribbon shows SIMPLIFY SOLID / SIMPLIFY SURFACE / FINISH SIMPLIFY).
    Inspect there offers only Measure / Section Analysis / Interference /
    Display Component Colors / Transparent Surfaces Toggle -- there is NO
    Validate. FusionSurfaceValidateCommand belongs to Design > Surface >
    Inspect > Validate, so Commands.Start for it was a no-op the whole time:
    no dialog, nothing to commit, nothing to keypress.

    Dump the real workspace + panel + command inventory so the repair step can
    be built from what exists here instead of what exists in Design."""
    global _dumped_env_once
    if _dumped_env_once:
        return
    _dumped_env_once = True

    out = os.path.join(os.path.dirname(__file__), "fusion_environment_dump.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            try:
                ws = ui.activeWorkspace
                f.write("ACTIVE WORKSPACE: id={0!r} name={1!r}\n".format(ws.id, ws.name))
            except Exception as e:
                f.write("ACTIVE WORKSPACE: unavailable ({0})\n".format(e))

            try:
                env = app.activeProduct
                f.write("ACTIVE PRODUCT  : {0!r}\n".format(
                    env.objectType if env else None))
            except Exception as e:
                f.write("ACTIVE PRODUCT  : unavailable ({0})\n".format(e))

            f.write("\n=== PANELS IN ACTIVE WORKSPACE ===\n")
            try:
                for p in ui.activeWorkspace.toolbarPanels:
                    f.write("\nPANEL {0!r} ({1!r})\n".format(p.id, p.name))
                    try:
                        for c in p.controls:
                            try:
                                d = c.commandDefinition
                                f.write("    {0}\t{1}\n".format(d.id, d.name))
                            except Exception:
                                f.write("    <control {0}>\n".format(
                                    getattr(c, "id", "?")))
                    except Exception as e:
                        f.write("    <controls unavailable: {0}>\n".format(e))
            except Exception as e:
                f.write("<panels unavailable: {0}>\n".format(e))

            # Ask Fusion itself which text commands exist rather than guessing.
            # Its own error told us how: "There is no command Commands.Execute.
            # Use ? to get help on available commands". Commands.Execute was a
            # guess and did not exist; this is the authoritative list, straight
            # from the running instance, and should contain whatever actually
            # commits an open command dialog.
            f.write("\n=== TEXT COMMAND HELP (from Fusion itself) ===\n")
            for probe in ("Commands.?", "?", "NuCommands.?"):
                f.write("\n--- executeTextCommand({0!r}) ---\n".format(probe))
                try:
                    f.write(str(app.executeTextCommand(probe)) + "\n")
                except Exception as e:
                    f.write("<rejected: {0}>\n".format(e))

            # Which repair-ish commands are actually REACHABLE (enabled) right
            # now. A command can be registered globally yet unusable in the
            # current environment, so isVisible/isEnabled matters as much as
            # existence.
            f.write("\n=== REPAIR-CANDIDATE COMMANDS: REGISTERED vs USABLE ===\n")
            for cid in ("FusionSurfaceValidateCommand", "FusionSurfaceStitchCommand",
                        "FusionSurfacePatchCommand", "FusionSurfaceUnStitchCommand",
                        "ParaMeshRepairCommand", "TSplineRepairBodyCmd",
                        RETURN_TO_MOLDFLOW_CMD_ID):
                try:
                    d = ui.commandDefinitions.itemById(cid)
                    if d is None:
                        f.write("{0}\tNOT REGISTERED\n".format(cid))
                        continue
                    cd = d.controlDefinition
                    f.write("{0}\tregistered  enabled={1}  visible={2}\n".format(
                        cid,
                        getattr(cd, "isEnabled", "?"),
                        getattr(cd, "isVisible", "?")))
                except Exception as e:
                    f.write("{0}\t<error: {1}>\n".format(cid, e))

            f.write("\n=== ALL COMMAND DEFINITIONS MATCHING REPAIR KEYWORDS ===\n")
            keywords = ("valid", "repair", "heal", "stitch", "patch", "simplify",
                        "surface", "moldflow", "return", "finish", "remove",
                        "delete face", "defeature")
            try:
                defs = ui.commandDefinitions
                for i in range(defs.count):
                    d = defs.item(i)
                    try:
                        hay = ((d.id or "") + " " + (d.name or "")).lower()
                    except Exception:
                        continue
                    if any(k in hay for k in keywords):
                        f.write("{0}\t{1}\n".format(d.id, d.name))
            except Exception as e:
                f.write("<command definitions unavailable: {0}>\n".format(e))
        _log("Environment dump written to {0}".format(out))
    except Exception as e:
        _log("Could not write environment dump: {0}".format(e))


def _dump_all_commands_once():
    """One-time diagnostic dump of every command id/name Fusion has
    registered. If _find_moldflow_commands() ever comes back empty during an
    actual round trip, this file is where to look for the real command id so
    the 'moldflow' substring check above can be corrected."""
    global _dumped_commands_once
    if _dumped_commands_once:
        return
    _dumped_commands_once = True
    try:
        dump_path = os.path.join(os.path.dirname(__file__), "all_command_definitions.txt")
        all_defs = ui.commandDefinitions
        with open(dump_path, "w", encoding="utf-8") as f:
            for i in range(all_defs.count):
                cmd_def = all_defs.item(i)
                try:
                    f.write("{0}\t{1}\n".format(cmd_def.id, cmd_def.name))
                except Exception:
                    pass
        _log("Dumped {0} command definitions to {1} for inspection.".format(
            all_defs.count, dump_path))
    except Exception as e:
        _log("Could not dump command definitions: {0}".format(e))


def _pick_return_command(candidates):
    # Prefer the id observed live in this environment.
    try:
        exact = ui.commandDefinitions.itemById(RETURN_TO_MOLDFLOW_CMD_ID)
        if exact is not None:
            return exact
    except Exception:
        pass
    for c in candidates:
        try:
            haystack = (c.id + " " + c.name).lower()
        except Exception:
            continue
        if "return" in haystack:
            return c
    return candidates[0] if candidates else None


def _active_workspace():
    try:
        ws = ui.activeWorkspace
        return (ws.id or ""), (ws.name or "")
    except Exception:
        return "", ""


def _wait_for_workspace_to_settle(stable_for=4.0, timeout=60.0, poll=0.5):
    """Wait until Fusion stops switching workspaces, and report the transition.

    THE bug this fixes: documentActivated fires while Fusion is still in its
    default Design workspace, and only afterwards does it switch to
    Simulation > Simplify for the Moldflow round trip. Acting on that early
    sample meant opening Design's Validate command, which the subsequent
    workspace switch then orphaned -- the dialog stayed 'active' forever,
    nothing appeared on screen, and Moldflow waited indefinitely.

    Returns the settled (id, name)."""
    last = _active_workspace()
    _log("Workspace at document-activated time: id={0!r} name={1!r} "
         "(may still be transitioning)".format(*last))

    unchanged = 0.0
    waited = 0.0
    while waited < timeout:
        time.sleep(poll)
        waited += poll
        try:
            adsk.doEvents()
        except Exception:
            pass
        cur = _active_workspace()
        if cur != last:
            _log("Workspace changed: {0!r} -> {1!r} after {2:.1f}s".format(
                last[1], cur[1], waited))
            last = cur
            unchanged = 0.0
            continue
        unchanged += poll
        if unchanged >= stable_for:
            break

    _log("Workspace settled: id={0!r} name={1!r} (after {2:.1f}s)".format(
        last[0], last[1], waited))
    return last


def _active_command():
    """Which command Fusion currently has open, or '' if idle.

    This is the diagnostic that was missing: 'Validate commit accepted' only
    meant Fusion accepted the TEXT COMMAND, not that the dialog closed. Reading
    activeCommand tells us what is actually on screen."""
    try:
        cmd = ui.activeCommand
        return str(cmd) if cmd else ""
    except Exception as e:
        _log("Could not read activeCommand: {0}".format(e))
        return "<unknown>"


def _wait_until_idle(timeout_s=20.0, poll=0.5):
    """Wait for Fusion to have no command dialog open. Returns True if idle."""
    waited = 0.0
    while waited < timeout_s:
        cur = _active_command()
        # Fusion reports a select/idle pseudo-command when nothing is running.
        if cur in ("", "SelectCommand", "<unknown>"):
            return True
        try:
            adsk.doEvents()
        except Exception:
            pass
        time.sleep(poll)
        waited += poll
    return False


def _get_active_selections(max_tries=40, delay_ms=250):
    """UserInterface.activeSelections is None until Fusion's UI has finished
    settling on the newly activated document. documentActivated fires before
    that, so the first read after a Moldflow round trip reliably came back
    None ('NoneType' object has no attribute 'clear' in the log). Poll for it
    instead of assuming it is ready."""
    for attempt in range(max_tries):
        try:
            sels = ui.activeSelections
            if sels is not None:
                if attempt:
                    _log("activeSelections became available after {0} retries.".format(attempt))
                return sels
        except Exception as e:
            _log("activeSelections read raised (attempt {0}): {1}".format(attempt, e))
        try:
            adsk.doEvents()
        except Exception:
            pass
        time.sleep(delay_ms / 1000.0)
    _log("activeSelections never became available after {0} tries.".format(max_tries))
    return None


def _collect_all_bodies(design):
    bodies = []
    try:
        root = design.rootComponent
        for body in root.bRepBodies:
            bodies.append(body)
        for occ in root.allOccurrences:
            comp = occ.component
            for body in comp.bRepBodies:
                bodies.append(body)
    except Exception as e:
        _log("Body collection failed: {0}".format(e))
    return bodies


def _run_validate_and_repair(design):
    bodies = _collect_all_bodies(design)
    if not bodies:
        _log("No bodies found in the active design; nothing to validate.")
        return False

    sels = _get_active_selections()
    if sels is None:
        _log("Cannot select bodies because activeSelections is unavailable; "
             "skipping the automatic Validate step.")
        return False

    try:
        sels.clear()
        added = 0
        for b in bodies:
            try:
                sels.add(b)
                added += 1
            except Exception as e:
                _log("Could not select a body: {0}".format(e))
        _log("Selected {0}/{1} body(ies) for validation.".format(added, len(bodies)))
        if added == 0:
            return False
    except Exception as e:
        _log("Selection setup failed: {0}".format(e))
        return False

    try:
        # Let Fusion finish switching into the Simulation > Simplify
        # environment before touching anything. Acting on the transient Design
        # state is what orphaned the Validate dialog on every previous run.
        ws_id, ws_name = _wait_for_workspace_to_settle()

        # Dump AFTER settling so the inventory reflects the environment we are
        # actually going to operate in, not the one we passed through.
        _dump_environment_once()

        if ui.commandDefinitions.itemById("FusionSurfaceValidateCommand") is None:
            _log("FusionSurfaceValidateCommand is not available in the settled "
                 "workspace ({0!r}). Not firing it -- that is the no-op that made "
                 "earlier runs look like they had started a repair. See "
                 "fusion_environment_dump.txt for the tools that exist here."
                 .format(ws_name))
            return False

        _log("activeCommand before Validate = {0!r}".format(_active_command()))
        _log("Starting FusionSurfaceValidateCommand on {0} body(ies)...".format(len(bodies)))
        app.executeTextCommand(u'Commands.Start FusionSurfaceValidateCommand')
        try:
            adsk.doEvents()
        except Exception:
            pass
        time.sleep(1.0)
        _log("activeCommand after Start = {0!r} (if this is empty, the Validate "
             "command never actually opened)".format(_active_command()))
        app.executeTextCommand(u'Commands.SetString checkingLevelType standardType')
        app.executeTextCommand(u'Commands.SetBool enableRepair 1')
    except Exception as e:
        _log("Validate/repair command failed: {0}".format(e))
        return False

    # Commands.Start only OPENS the dialog and the SetX calls only fill it in --
    # nothing presses OK. The dialog then sits there waiting, the subsequent
    # Return-to-Moldflow call fires into a busy UI and is ignored, and the user
    # has to finish by hand. That is exactly what the 14:16 run showed: the
    # add-in "completed" instantly, then 6.5 minutes of manual work followed.
    #
    # There is no documented commit command, so try the plausible ones in order
    # and log which (if any) is accepted. Return is Qt's Key_Return (16777220);
    # Fusion's UI is Qt-based, and the documented syntax is
    # Commands.KeyPressDown <KeyCode> <IsRepeat> <Modifiers>.
    try:
        adsk.doEvents()
    except Exception:
        pass
    time.sleep(1.0)

    # A text command being "accepted" proves nothing -- the Return keypress was
    # accepted and the dialog stayed open. The only real test is whether
    # activeCommand stops being FusionSurfaceValidateCommand, so check that
    # after every candidate and keep going until one actually closes it.
    committed = False
    for label, cmds in (
        ("NuCommands.CommitCmd", [u'NuCommands.CommitCmd']),
        ("Commands.Commit", [u'Commands.Commit']),
        ("Commands.Ok", [u'Commands.Ok']),
        ("Commands.Finish", [u'Commands.Finish']),
        ("Qt Return keypress", [u'Commands.KeyPressDown 16777220 0 0',
                                u'Commands.KeyReleaseUp 16777220 0 0']),
        ("Qt Enter keypress", [u'Commands.KeyPressDown 16777221 0 0',
                               u'Commands.KeyReleaseUp 16777221 0 0']),
        ("ASCII CR keypress", [u'Commands.KeyPressDown 13 0 0',
                               u'Commands.KeyReleaseUp 13 0 0']),
    ):
        res = None
        try:
            for c in cmds:
                res = app.executeTextCommand(c)
            accepted = True
        except Exception as e:
            accepted = False
            _log("Validate commit via {0}: rejected by Fusion ({1}).".format(label, e))

        if not accepted:
            continue

        try:
            adsk.doEvents()
        except Exception:
            pass
        time.sleep(1.0)

        now = _active_command()
        if now != "FusionSurfaceValidateCommand":
            _log("Validate commit via {0}: WORKED -- dialog closed "
                 "(activeCommand now {1!r}, result={2!r}).".format(label, now, res))
            committed = True
            break
        _log("Validate commit via {0}: accepted but dialog STILL OPEN "
             "(result={1!r}).".format(label, res))

    try:
        adsk.doEvents()
    except Exception:
        pass
    time.sleep(1.5)

    still_open = _active_command()
    _log("After commit attempts, activeCommand = {0!r}".format(still_open))

    if not committed:
        _log("Could not commit the Validate dialog automatically -- it is probably "
             "still open in Fusion. Click OK there, then Return to Moldflow.")
    return True


def _maybe_process_document(doc):
    """Decide whether this document is a Moldflow round trip and, if so, run
    the Validate/repair/return sequence.

    Every exit path logs. An earlier version returned silently on both the
    null-document and already-seen checks, so three events fired in a row and
    produced no output whatsoever -- indistinguishable from the handlers never
    being called. Silence is not a diagnosis."""
    try:
        if doc is None:
            _log("Event carried a null document; falling back to app.activeDocument.")
            try:
                doc = app.activeDocument
            except Exception as e:
                _log("  app.activeDocument also unavailable: {0}".format(e))
                return
            if doc is None:
                _log("  app.activeDocument is None too; nothing to process.")
                return

        try:
            key = doc.name
        except Exception:
            key = str(doc)

        # Classify by CONTENT, not by name. The previous name-based skip list
        # added "Untitled" to _processed_docs at start-up and then silently
        # swallowed the real round-trip document when it arrived under that
        # same name before Fusion had renamed it.
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            _log("Document {0!r}: active product is not a Design; skipping.".format(key))
            return

        bodies = _collect_all_bodies(design)
        _log("Document {0!r}: {1} body(ies) present.".format(key, len(bodies)))
        if not bodies:
            _log("  No bodies -- treating as Fusion's empty start-up document, not a "
                 "Moldflow round trip. Add-in stays loaded and waiting.")
            return

        sig = "{0}|{1}".format(key, len(bodies))
        if sig in _processed_docs:
            _log("  Already handled this document ({0}); not repeating.".format(sig))
            return
        _processed_docs.add(sig)

        moldflow_cmds = _find_moldflow_commands()
        if not moldflow_cmds:
            _log("  No Moldflow-related command registered -- this does not look like "
                 "a Moldflow round-trip session. Doing nothing for this document.")
            _dump_all_commands_once()
            return

        if not _run_validate_and_repair(design):
            _log("Validate/repair step did not run; leaving the model open for the "
                 "user to finish manually in Fusion.")
            return

        return_cmd = _pick_return_command(moldflow_cmds)
        if return_cmd is None:
            _log("Could not identify a 'Return to Moldflow' command among the "
                 "Moldflow-related commands found; leaving Fusion open for the user "
                 "to click Return to Moldflow manually.")
            return

        # Do NOT return while a command dialog is still open. Executing the
        # return against a blocked UI is silently dropped -- that is what left
        # Moldflow polling IsInventorFusionCadEditDone forever at 14:33 -- and
        # if the user is repairing by hand it rips the model away mid-edit.
        if AUTO_RETURN_ONLY_WHEN_IDLE and not _wait_until_idle(timeout_s=20.0):
            _log("Fusion still has a command open (activeCommand={0!r}) after the "
                 "Validate step. NOT auto-returning -- that call would be dropped "
                 "and would also interrupt you mid-repair. Finish the repair in "
                 "Fusion and click Return to Moldflow yourself; Moldflow is still "
                 "waiting and will resume the automation when you do."
                 .format(_active_command()))
            return

        try:
            _log("Fusion is idle (activeCommand={0!r}); executing '{1}' ({2}) to "
                 "return to Moldflow...".format(
                     _active_command(), return_cmd.name, return_cmd.id))
            return_cmd.execute()
            _log("Return-to-Moldflow executed.")
        except Exception as e:
            _log("Failed to execute the Return-to-Moldflow command: {0}".format(e))
    except Exception:
        _log("Unhandled error in _maybe_process_document:\n" + traceback.format_exc())


class _DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        try:
            _log("[event] documentActivated fired.")
            _maybe_process_document(args.document)
        except Exception:
            _log("Unhandled error in DocumentActivatedHandler:\n" + traceback.format_exc())


class _DocumentOpenedHandler(adsk.core.DocumentEventHandler):
    """Second trigger. The Moldflow round trip opens the exported study as a
    document; depending on how Fusion was launched, documentActivated does not
    always fire for it (a whole test cycle logged no document event at all).
    _processed_docs keeps the two handlers from double-running the same doc."""
    def notify(self, args):
        try:
            _log("[event] documentOpened fired.")
            _maybe_process_document(args.document)
        except Exception:
            _log("Unhandled error in DocumentOpenedHandler:\n" + traceback.format_exc())


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        on_doc_activated = _DocumentActivatedHandler()
        app.documentActivated.add(on_doc_activated)
        handlers.append(("documentActivated", on_doc_activated))

        on_doc_opened = _DocumentOpenedHandler()
        app.documentOpened.add(on_doc_opened)
        handlers.append(("documentOpened", on_doc_opened))

        _log("MoldflowAutoRepair add-in started (documentActivated + "
             "documentOpened hooked).")

        # Cover the case where Moldflow already opened the document before
        # this add-in finished loading (runOnStartup races the round trip).
        try:
            active = app.activeDocument
            _log("Active document at startup: {0!r}".format(
                active.name if active is not None else None))
            if active is not None:
                _maybe_process_document(active)
        except Exception as e:
            _log("Could not inspect the active document at startup: {0}".format(e))
    except Exception:
        _log("Unhandled error in run():\n" + traceback.format_exc())
        if ui:
            ui.messageBox("MoldflowAutoRepair failed to start:\n{0}".format(traceback.format_exc()))


def stop(context):
    try:
        for event_name, h in handlers:
            try:
                getattr(app, event_name).remove(h)
            except Exception:
                pass
        handlers.clear()
        _log("MoldflowAutoRepair add-in stopped.")
    except Exception:
        pass
