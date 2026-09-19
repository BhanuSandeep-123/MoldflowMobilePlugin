"""
session_context.py
------------------
Per-Synergy-instance identity for the plugin's runtime files.

WHY THIS EXISTS
===============
Synergy's ``%RunPerInstance`` startup command means every Synergy window gets
its OWN process tree: run_startup.vbs -> moldflow_observer.py /
moldflow_startup.py -> ui_launcher -> embedded_ui.py -> cad_diagnostics.py.
The COM side was already correctly isolated -- each tree binds its own Synergy
object through the ``SAInstance`` moniker its window exported, so automation
calls never cross windows.

What was NOT isolated was every file those trees talk through. ui_state.json,
ui_heartbeat.txt, ui_panel_command.json, ui_focus_request.json, the
_manual_session.flag handshake and the generated HTA/VBS dialog templates
(_automation_prompt.hta, _proc_settings.hta, _seq_select.vbs, ...) all lived at
ONE fixed path next to this file. With two Synergy windows open:

  * the second window's launcher saw the FIRST window's heartbeat, concluded a
    panel was already alive and never started its own -- so one window had no
    UI at all;
  * both workflows published prompts into the same ui_state.json, so an answer
    given in one window resolved the other window's prompt;
  * both wrote the same _proc_settings.hta / _automation_prompt.hta path, so
    whichever workflow wrote last replaced the template the other was about to
    show, and both mshta processes wrote their answers to the same result file;
  * declining automation in one window wrote _manual_session.flag, which stood
    the OTHER window's observer down.

This module gives every process in a Synergy window's tree the same session
key, and a private directory to put those files in. Nothing here is shared
between windows, so the isolation that already existed at the COM layer now
holds all the way up through the UI.

WHAT MAKES A GOOD KEY
=====================
It has to resolve to the SAME value in every process of one window's tree, and
to DIFFERENT values across windows. In preference order:

  1. ``MFPLUGIN_SESSION`` -- once any process in the tree resolves a key it
     exports it here, so children inherit the exact answer rather than
     re-deriving it. This also keeps a detached grandchild correct even if its
     parent chain is broken (Popen'd processes whose parent has exited).
  2. ``SAInstance`` -- the ``{GUID}`` moniker Synergy sets for the script it
     launched. Per-window by construction and inherited by every child.
  3. The owning ``synergy.exe`` PID, found by walking up the parent-process
     chain. Covers processes started outside the macro host (manual runs,
     diagnostics scripts) where SAInstance was never set.
  4. This process's own PID -- last resort. Isolated but not shared, so a
     stray process gets its own sandbox instead of trampling a real session.

Everything is resolved once and cached: the key must not change underneath a
running process even if Synergy exits mid-workflow.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Parent of all per-session directories. One level of nesting keeps the plugin
# folder readable and makes stale-session cleanup a single directory walk.
SESSIONS_ROOT = HERE / "sessions"

# Environment variable used to pin an already-resolved key for child processes.
SESSION_ENV_VAR = "MFPLUGIN_SESSION"

_CACHE: dict = {}


# --------------------------------------------------------------------------- #
#  Process-ancestry walk (pure Win32, no extra dependency)
# --------------------------------------------------------------------------- #

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


def _snapshot():
    """{pid: (parent_pid, lowercase_exe_name)} for every process we can see,
    taken fresh each call."""
    table = {}
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap and snap != -1:
                try:
                    entry = _PROCESSENTRY32()
                    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
                    ok = kernel32.Process32First(snap, ctypes.byref(entry))
                    while ok:
                        try:
                            name = entry.szExeFile.decode("mbcs", "replace").lower()
                        except Exception:
                            name = ""
                        table[int(entry.th32ProcessID)] = (
                            int(entry.th32ParentProcessID), name)
                        ok = kernel32.Process32Next(snap, ctypes.byref(entry))
                finally:
                    kernel32.CloseHandle(snap)
        except Exception:
            table = {}

    return table


def _process_table():
    """Cached process snapshot backing the ancestor walk.

    Cached on purpose: one snapshot answers the whole walk, so a process that
    exits mid-walk cannot make us follow a PID that has since been recycled
    onto a different program -- and the session key must not change under a
    running process. Use synergy_process_count() when you need live data."""
    if "table" not in _CACHE:
        _CACHE["table"] = _snapshot()
    return _CACHE["table"]


def synergy_process_count() -> int:
    """How many synergy.exe are running right now. Best-effort; 0 if it
    cannot be determined. Takes a fresh snapshot rather than reusing the
    cached one, which is pinned to this process's startup."""
    try:
        return sum(1 for _pid, (_ppid, name) in _snapshot().items()
                   if name == "synergy.exe")
    except Exception:
        return 0


def synergy_pid():
    """PID of the synergy.exe that owns this process tree, or None.

    Walks up from this process. The depth cap stops a corrupted table (a cycle,
    or a PID that is its own parent) from spinning forever."""
    if "synergy_pid" in _CACHE:
        return _CACHE["synergy_pid"]

    found = None
    table = _process_table()
    pid = os.getpid()
    seen = set()
    for _ in range(32):
        if pid in seen or pid not in table:
            break
        seen.add(pid)
        parent, name = table[pid]
        if name == "synergy.exe":
            found = pid
            break
        pid = parent

    _CACHE["synergy_pid"] = found
    return found


# --------------------------------------------------------------------------- #
#  Session key + directory
# --------------------------------------------------------------------------- #

def _sanitize(text: str) -> str:
    """Reduce a moniker to something safe for a directory name. SAInstance is
    a ``{GUID}``; the braces and dashes go, the hex stays."""
    return re.sub(r"[^A-Za-z0-9]", "", str(text))[:40]


def session_key() -> str:
    """The identity of THIS Synergy window's process tree. Stable for the life
    of the process, identical across every process in the tree.

    Resolving also pins the answer into the environment, so anything this
    process spawns inherits the same key rather than re-deriving it from an
    ancestor chain that may no longer reach synergy.exe."""
    if "key" in _CACHE:
        return _CACHE["key"]

    pinned = os.environ.get(SESSION_ENV_VAR, "").strip()
    if pinned:
        key = _sanitize(pinned)
    else:
        sa = os.environ.get("SAInstance", "").strip()
        if sa and sa != "%SAInstance%":
            key = "sa" + _sanitize(sa)
        else:
            spid = synergy_pid()
            key = "syn{0}".format(spid) if spid else "pid{0}".format(os.getpid())

    if not key:
        key = "pid{0}".format(os.getpid())

    _CACHE["key"] = key
    # Pin it for our children.
    try:
        os.environ[SESSION_ENV_VAR] = key
    except Exception:
        pass
    return key


def session_dir() -> Path:
    """This session's private directory, created on first use.

    Falls back to the plugin folder itself if the directory cannot be created
    (read-only install, permissions). That restores the old shared-path
    behaviour rather than crashing -- degraded, but still a working single
    window."""
    if "dir" in _CACHE:
        return _CACHE["dir"]

    target = SESSIONS_ROOT / session_key()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        target = HERE

    _CACHE["dir"] = target
    return target


def session_path(name: str) -> Path:
    """Path to a per-session runtime file. Use this for ANYTHING two Synergy
    windows would otherwise fight over: state, heartbeats, prompt hand-offs,
    generated HTA/VBS templates and their result files."""
    return session_dir() / name


def prune_stale_sessions(max_age_hours: float = 48.0) -> int:
    """Delete session directories nothing has touched in a long while, and
    return how many went. Called once per bring-up so the sessions/ folder
    does not grow without bound.

    Deliberately conservative. A session directory belonging to a LIVE window
    that simply has not written for a while must never be removed, so the age
    is measured against the newest file in the directory (the heartbeat ticks
    every 300ms while a panel is up) and the threshold is days, not minutes.
    The current session is skipped outright."""
    removed = 0
    try:
        if not SESSIONS_ROOT.is_dir():
            return 0
        current = session_dir().resolve()
        cutoff = max_age_hours * 3600.0
        import shutil
        import time
        now = time.time()
        for child in SESSIONS_ROOT.iterdir():
            try:
                if not child.is_dir() or child.resolve() == current:
                    continue
                newest = child.stat().st_mtime
                for f in child.rglob("*"):
                    try:
                        newest = max(newest, f.stat().st_mtime)
                    except Exception:
                        pass
                if (now - newest) > cutoff:
                    shutil.rmtree(str(child), ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def describe() -> str:
    """One-line identity for log lines, so a shared log file (the diagnostics
    logs are still global and append-only) can be read back per window."""
    spid = synergy_pid()
    return "session={0} synergy_pid={1} pid={2}".format(
        session_key(), spid if spid else "?", os.getpid())


if __name__ == "__main__":
    sys.stdout.write(describe() + "\n")
    sys.stdout.write(str(session_dir()) + "\n")
