'%RunPerInstance
'@
'@ DESCRIPTION
'@ Runs the READ-ONLY CAD EntList diagnostic probe against the CURRENT active
'@ Synergy study. Answers one question: can we build a non-empty EntList of
'@ CAD bodies through the COM API?
'@
'@ This MUST be run from inside Synergy (not from a terminal): Synergy only
'@ exposes its automation object to scripts it launches itself, via the
'@ SAInstance environment variable. A standalone run always fails with
'@ "No Moldflow Synergy session available".
'@
'@ Open the study containing the imported CAD model first, then run this.
'@ Results are written to probe_cad_entlist_report.txt next to this script,
'@ and the console window is kept open (cmd /k) so you can read them.
'@
'@ SYNTAX
'@ run_probe
'@@
Option Explicit
Dim shell, fso, scriptDir, pyExe, pyScript, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

pyExe = scriptDir & "\.venv\Scripts\python.exe"
If Not fso.FileExists(pyExe) Then
  pyExe = "python"
End If

pyScript = scriptDir & "\probe_cad_entlist.py"

cmd = "%comspec% /k """"" & pyExe & """ """ & pyScript & """"""
shell.Run cmd, 1, False
