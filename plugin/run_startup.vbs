 '%RunPerInstance
'@
'@ DESCRIPTION
'@ Startup workflow for Moldflow Insight 2027.
'@
'@ This script is intentionally silent: it does NOT show any MsgBox,
'@ InputBox, or native dialog of its own. Its only jobs are to (1) launch the
'@ background observer, and (2) run moldflow_startup.py, which does the real
'@ work -- connecting to Synergy, checking whether a study is already open,
'@ and (if not) driving the "Create New Project" / "Import Model" workflow
'@ through the embedded, docked panel defined in embedded_ui.py (see that
'@ file and ui_bridge.py for how prompts are shown and answered). If the
'@ embedded panel can't be created for any reason, moldflow_startup.py logs
'@ why and skips the automated prompts silently -- it never falls back to a
'@ VBScript dialog.
'@
'@ AUTO-RUN SETUP:
'@   Copy this file to the Moldflow commands directory:
'@     C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands\
'@   Then in Moldflow: Tools > Application Options > Startup command:
'@     run_startup
'@
'@ SYNTAX
'@ run_startup
'@@
Option Explicit

' IMPORTANT: this must point at the folder that actually CONTAINS the
' plugin's .py/.venv files (moldflow_observer.py, moldflow_startup.py,
' embedded_ui.py, ui_bridge.py, .venv\...). Earlier versions of this
' constant pointed one level too high (the outer "MoldflowSynergyPlugin"
' folder, which holds nothing but this repo checkout) -- that silently
' broke the observer launch (wrong script path, wrong .venv, fell back to
' the bare "python" on PATH) and the _manual_session.flag handshake with
' moldflow_observer.py (which has always looked for the flag next to
' itself, i.e. in THIS folder, not the outer one). Verified against disk
' 2026-07-27: only this path actually contains moldflow_observer.py.
Const PLUGIN_DIR = "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin"

' ============================================================================
'  PER-SYNERGY-WINDOW SESSION IDENTITY
'
'  Synergy's %RunPerInstance means this script runs once per Synergy window,
'  and each run owns its own process tree. Everything those trees hand state
'  through -- ui_state.json, the heartbeat, the generated HTA dialogs, the
'  _manual_session.flag handshake -- must therefore be per-window, or two
'  open Synergy windows fight over one set of files: the second window's
'  launcher sees the first window's heartbeat and never starts its own panel,
'  and clearing a "stale" flag here wipes the OTHER window's live one.
'
'  The key mirrors session_context.py exactly: SAInstance (the per-window
'  {GUID} moniker Synergy exports) reduced to alphanumerics, prefixed "sa".
'  It is also pinned into the process environment as MFPLUGIN_SESSION, which
'  every child inherits -- so the Python side adopts this exact key rather
'  than re-deriving it, and the two can never disagree.
' ============================================================================
Dim gSessionKey
gSessionKey = ""

Function SessionKey()
    If gSessionKey <> "" Then
        SessionKey = gSessionKey
        Exit Function
    End If

    Dim shell, sa, clean, i, ch
    Set shell = CreateObject("WScript.Shell")
    sa = shell.ExpandEnvironmentStrings("%SAInstance%")
    If sa = "%SAInstance%" Then sa = ""

    clean = ""
    For i = 1 To Len(sa)
        ch = Mid(sa, i, 1)
        If (ch >= "0" And ch <= "9") Or (LCase(ch) >= "a" And LCase(ch) <= "z") Then
            clean = clean & ch
        End If
        If Len(clean) >= 40 Then Exit For
    Next

    If clean <> "" Then
        gSessionKey = "sa" & clean
    Else
        ' No moniker (Synergy did not export one under this configuration).
        ' Mint a key unique to THIS script run. It is cached for the life of
        ' the script and pinned into the environment below, so every child
        ' inherits the same one -- which is all the key has to guarantee.
        gSessionKey = "vbs" & Replace(Replace(Replace( _
            CStr(Year(Now)) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & _
            Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2) & _
            Right("0" & Second(Now), 2), ":", ""), "/", ""), " ", "") _
            & CStr(Int(Rnd() * 100000))
    End If

    ' Pin it so children adopt this key instead of deriving their own.
    On Error Resume Next
    shell.Environment("PROCESS")("MFPLUGIN_SESSION") = gSessionKey
    On Error GoTo 0

    SessionKey = gSessionKey
End Function

Function SessionDir()
    On Error Resume Next
    Dim fso, root, target
    Set fso = CreateObject("Scripting.FileSystemObject")
    root = PLUGIN_DIR & "\sessions"
    If Not fso.FolderExists(root) Then fso.CreateFolder root
    target = root & "\" & SessionKey()
    If Not fso.FolderExists(target) Then fso.CreateFolder target
    If Err.Number <> 0 Then
        ' Could not create it (read-only install): degrade to the plugin
        ' folder rather than failing Synergy's startup command.
        Err.Clear
        target = PLUGIN_DIR
    End If
    On Error GoTo 0
    SessionDir = target
End Function

Sub LogMsg(msg)
    On Error Resume Next
    Dim fso, f
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set f = fso.OpenTextFile(PLUGIN_DIR & "\startup_vbs_log.txt", 8, True)
    ' Tagged per window -- both windows append to this one log.
    f.WriteLine Now & " [" & SessionKey() & "] - " & msg
    f.Close
    On Error GoTo 0
End Sub

Sub KeepAlive(Synergy)
    ' Keep this script RESIDENT (required by the %RunPerInstance startup
    ' mechanism) WITHOUT ever calling into Synergy again.
    '
    ' The old loop polled Synergy.Build every 2 seconds — and kept doing so
    ' WHILE Synergy was shutting down (proven 2026-07-23: window closed at
    ' 17:41:54, this loop still polling until 17:42:47). An automation call
    ' landing mid-teardown throws an exception inside synergy.exe and
    ' produces the "Moldflow Synergy 2027 Error Report" dialog a few seconds
    ' after closing Moldflow. So: watch the synergy.exe PROCESS via WMI
    ' instead — zero COM traffic into Synergy, ever.
    LogMsg "Entering KeepAlive loop (process-watch, no COM polling)..."
    Dim wmi, procs, alive, wmiOk
    wmiOk = False
    On Error Resume Next
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    If Err.Number = 0 And Not (wmi Is Nothing) Then
        wmiOk = True
    End If
    Err.Clear
    On Error GoTo 0

    If Not wmiOk Then
        ' Extremely unlikely; fall back to the old Build poll so the script
        ' still ends when Synergy goes away.
        LogMsg "KeepAlive: WMI unavailable — falling back to COM polling."
        Dim errNum2
        alive = True
        Do While alive
            WScript.Sleep 2000
            On Error Resume Next
            Dim testBuild
            testBuild = Synergy.Build
            errNum2 = Err.Number
            Err.Clear
            On Error GoTo 0
            If errNum2 <> 0 Then alive = False
        Loop
        LogMsg "Exiting KeepAlive loop (COM fallback)."
        Exit Sub
    End If

    alive = True
    Do While alive
        WScript.Sleep 3000
        On Error Resume Next
        Set procs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='synergy.exe'")
        If Err.Number <> 0 Then
            Err.Clear
        ElseIf procs.Count = 0 Then
            alive = False
        End If
        On Error GoTo 0
    Loop
    ' Drop our Synergy reference only now, when the process is already gone —
    ' the release is a no-op client-side cleanup at this point.
    Set Synergy = Nothing
    LogMsg "KeepAlive: synergy.exe has exited. Ending script."

    ' Synergy has just written its docking layout, so this is the ONE moment we
    ' can make the AI Assistant panel come up on the next launch. The panel is
    ' a persisted MFC dock bar (CAutodeskAssistantDialog, BarID 700); if the
    ' user closed it, Synergy has recorded Visible=False and the next session
    ' starts without it -- and without it the plugin has no launcher button to
    ' press, so the report falls back to the local summary.
    ' Doing this any earlier is pointless: the state is rewritten on exit.
    EnsureAssistantPanelVisible
End Sub

Sub EnsureAssistantPanelVisible()
    Dim fso, shell, pyExe, cmd
    On Error Resume Next
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set shell = CreateObject("WScript.Shell")
    pyExe = ResolvePythonExe(fso)
    cmd = """" & pyExe & """ """ & PLUGIN_DIR & "\assistant_panel.py"" --ensure-visible"
    shell.Run cmd, 0, True
    If Err.Number <> 0 Then
        LogMsg "Assistant panel persistence: " & Err.Description
        Err.Clear
    Else
        LogMsg "Assistant panel persistence: checked for the next launch."
    End If
    On Error GoTo 0
End Sub

Function ResolvePythonExe(fso)
    Dim exe
    exe = PLUGIN_DIR & "\.venv\Scripts\python.exe"
    If fso.FileExists(exe) Then
        ResolvePythonExe = exe
        Exit Function
    End If
    ' Safe fallback: system Python 3.14 (never bare "python" alias)
    If fso.FileExists("C:\Program Files\Python314\python.exe") Then
        ResolvePythonExe = "C:\Program Files\Python314\python.exe"
        Exit Function
    End If
    ResolvePythonExe = exe
End Function

Sub Main()
    LogMsg "--- Main Started ---"

    ' ============================================================================
    '  MANUAL-MODE SWITCH
    '  If automation_disabled.flag exists in the plugin folder, do NOTHING:
    '  no observer, no prompts — Synergy runs fully manually.
    '  Toggle it with disable_automation.bat / enable_automation.bat.
    '  NEVER disable the automation by commenting this script out — the
    '  '%RunPerInstance and '@..'@@ header must stay intact or Synergy
    '  fails to load the startup command and will not open.
    ' ============================================================================
    ' IMPORTANT: Synergy's %RunPerInstance startup mechanism needs this script
    ' to CONNECT and STAY RESIDENT — that is why every path below ends in
    ' KeepAlive. Exiting Main immediately prevents Synergy from opening
    ' (proven 2026-07-23), so manual mode must still connect + KeepAlive and
    ' only skip the observer and the startup workflow.
    Dim fsoFlag
    Set fsoFlag = CreateObject("Scripting.FileSystemObject")
    If fsoFlag.FileExists(PLUGIN_DIR & "\automation_disabled.flag") Then
        LogMsg "automation_disabled.flag present - MANUAL MODE: no observer, no startup workflow."
        Dim SynergyManual
        Set SynergyManual = GetSynergy()
        If SynergyManual Is Nothing Then
            LogMsg "MANUAL MODE: could not connect to Synergy; exiting."
            Exit Sub
        End If
        KeepAlive SynergyManual
        Exit Sub
    End If

    ' A fresh Synergy session starts with the automation armed: remove any
    ' manual-SESSION flag left over from a previous session. (moldflow_startup.py
    ' writes this flag when the user chooses to skip automation for a session;
    ' it tells the already-running observer to stand down for that session only.)
    '
    ' Scoped to THIS window's session directory. Clearing the flag at the old
    ' shared path meant opening a second Synergy window re-armed the first
    ' window's automation after its user had explicitly opted out.
    On Error Resume Next
    Dim manualFlag
    manualFlag = SessionDir() & "\_manual_session.flag"
    If fsoFlag.FileExists(manualFlag) Then
        fsoFlag.DeleteFile manualFlag
        LogMsg "Cleared stale _manual_session.flag from a previous session."
    End If
    On Error GoTo 0

    ' ============================================================================
    '  LAUNCH PYTHON OBSERVER (PARALLEL PROCESS)
    ' ============================================================================
    On Error Resume Next
    Dim wshShell, pyExe, observerScriptPath, observerCmd
    Set wshShell = CreateObject("WScript.Shell")
    pyExe = ResolvePythonExe(fsoFlag)

    observerScriptPath = PLUGIN_DIR & "\moldflow_observer.py"
    observerCmd = """" & pyExe & """ """ & observerScriptPath & """"
    LogMsg "Launching background observer: " & observerCmd
    wshShell.Run observerCmd, 0, False
    If Err.Number <> 0 Then
        LogMsg "ERROR launching observer: " & Err.Description
        Err.Clear
    End If
    On Error GoTo 0

    ' ============================================================================
    '  LAUNCH STANDALONE JOB MONITOR (PARALLEL, INDEPENDENT PROCESS)
    '
    '  standalone_job_monitor.py keeps mobile job reporting alive after Synergy
    '  closes (see that file's own docstring) -- unlike the observer above, it
    '  has zero COM/Synergy dependency by design, so it must NOT watch for
    '  Synergy closing; it is launched once, detached, and simply left running.
    '
    '  %RunPerInstance means this whole script re-runs for EVERY Synergy window
    '  opened, so a WMI check for an already-running instance (same technique
    '  KeepAlive below already uses to watch synergy.exe) guards against
    '  launching a second monitor process per extra window. If WMI itself is
    '  unavailable, launch anyway rather than silently never starting the
    '  monitor -- a duplicate instance is harmless (report_job_status's
    '  terminal-state lock and notification dedup already make repeated/
    '  duplicate reports safe), while a monitor that never starts is not.
    ' ============================================================================
    On Error Resume Next
    Dim monitorScriptPath, monitorCmd, wmiMon, monitorProcs, monitorAlreadyRunning, wmiMonOk
    monitorAlreadyRunning = False
    wmiMonOk = False

    Set wmiMon = GetObject("winmgmts:\\.\root\cimv2")
    If Err.Number = 0 And Not (wmiMon Is Nothing) Then
        wmiMonOk = True
    End If
    Err.Clear

    If wmiMonOk Then
        Set monitorProcs = wmiMon.ExecQuery( _
            "SELECT ProcessId FROM Win32_Process WHERE CommandLine LIKE '%standalone_job_monitor.py%'")
        If Err.Number = 0 And Not (monitorProcs Is Nothing) Then
            If monitorProcs.Count > 0 Then
                monitorAlreadyRunning = True
            End If
        End If
        Err.Clear
    Else
        LogMsg "Standalone monitor check: WMI unavailable -- launching anyway (duplicate reports are harmless)."
    End If

    If monitorAlreadyRunning Then
        LogMsg "Standalone job monitor already running -- not launching another instance."
    Else
        monitorScriptPath = PLUGIN_DIR & "\standalone_job_monitor.py"
        monitorCmd = """" & pyExe & """ """ & monitorScriptPath & """"
        LogMsg "Launching standalone job monitor: " & monitorCmd
        wshShell.Run monitorCmd, 0, False
        If Err.Number <> 0 Then
            LogMsg "ERROR launching standalone job monitor: " & Err.Description
            Err.Clear
        End If
    End If
    On Error GoTo 0

    ' ============================================================================
    '  CONNECT TO SYNERGY (needed so KeepAlive has a reference; the real
    '  workflow decisioning happens in moldflow_startup.py below, which binds
    '  its own Synergy object via the same SAInstance environment variable —
    '  inherited automatically since it's spawned from this process).
    ' ============================================================================
    Dim Synergy
    Set Synergy = GetSynergy()

    If Synergy Is Nothing Then
        LogMsg "ERROR: Synergy object is Nothing. Exiting Main."
        Exit Sub
    End If

    ' ============================================================================
    '  RUN THE STARTUP WORKFLOW — SILENTLY.
    '
    '  moldflow_startup.py does everything that used to live here as native
    '  VBScript dialogs (Yes/No prompt, project name InputBox, folder
    '  BrowseForFolder, native import dialog): it checks whether a study is
    '  already open, brings up the embedded/docked panel (embedded_ui.py),
    '  and shows the Create-New-Project / Import-Model workflow INSIDE it.
    '  No MsgBox, no InputBox, nothing pops up from this script. If the
    '  embedded panel itself can't be created, moldflow_startup.py logs the
    '  reason to startup_log.txt / embedded_ui_log.txt and returns quietly —
    '  it does not fall back to any kind of dialog.
    '
    '  Run hidden and WAIT (last arg True) so KeepAlive only takes over once
    '  the workflow (and any embedded-panel interaction) has finished.
    ' ============================================================================
    On Error Resume Next
    Dim startupScript, startupCmd
    startupScript = PLUGIN_DIR & "\moldflow_startup.py"
    startupCmd = """" & pyExe & """ """ & startupScript & """"
    LogMsg "Running startup workflow (silent): " & startupCmd
    wshShell.Run startupCmd, 0, True
    If Err.Number <> 0 Then
        LogMsg "ERROR running startup workflow: " & Err.Description
        Err.Clear
    End If
    On Error GoTo 0

    LogMsg "Startup workflow finished. Entering KeepAlive..."
    KeepAlive Synergy
End Sub


' ============================================================================
'  HELPER: Get Synergy COM object
' ============================================================================
Function GetSynergy()
    Dim SynergyGetter, shell, sa, i, errNum, errDesc
    Set shell = CreateObject("WScript.Shell")
    sa = shell.ExpandEnvironmentStrings("%SAInstance%")
    LogMsg "GetSynergy: SAInstance environment string = " & sa

    If sa <> "%SAInstance%" And sa <> "" Then
        ' Try to attach to the moniker with a short retry loop
        For i = 1 To 10
            LogMsg "GetSynergy: Attempting GetObject(" & sa & "), try " & i
            On Error Resume Next
            Set SynergyGetter = GetObject(sa)
            errNum = Err.Number
            errDesc = Err.Description
            On Error GoTo 0

            If errNum = 0 And Not (SynergyGetter Is Nothing) Then
                LogMsg "GetSynergy: GetObject succeeded on try " & i
                On Error Resume Next
                Set GetSynergy = SynergyGetter.GetSASynergy
                errNum = Err.Number
                errDesc = Err.Description
                On Error GoTo 0

                If errNum = 0 And Not (GetSynergy Is Nothing) Then
                    LogMsg "GetSynergy: GetSASynergy succeeded."
                    Exit Function
                Else
                    LogMsg "GetSynergy: GetSASynergy failed with error: " & errDesc
                End If
            Else
                LogMsg "GetSynergy: GetObject failed with error: " & errDesc
            End If

            ' Sleep for 500ms
            WScript.Sleep 500
        Next
    Else
        LogMsg "GetSynergy: SAInstance env var not set or invalid"
    End If

    ' Fallback to active object or create new one if not running
    LogMsg "GetSynergy: Falling back to CreateObject(synergy.Synergy)"
    On Error Resume Next
    Set GetSynergy = CreateObject("synergy.Synergy")
    errNum = Err.Number
    errDesc = Err.Description
    On Error GoTo 0

    If errNum <> 0 Then
        LogMsg "GetSynergy: CreateObject failed: " & errDesc
    Else
        LogMsg "GetSynergy: CreateObject succeeded."
    End If
End Function

' Run the main workflow
Main()
