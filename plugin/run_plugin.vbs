'%RunPerInstance
'@
'@ DESCRIPTION
'@ Runs the Python design-dimensions export against the CURRENT active Synergy
'@ study, and opens the resulting Excel file (design_dimensions.xlsx).
'@ The study must be MESHED. Make your part study the active tab before running.
'@
'@ The console window is kept open (cmd /k) so you can read the output.
'@
'@ SYNTAX
'@ run_plugin
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

pyScript = scriptDir & "\export_dimensions.py"

cmd = "%comspec% /k """"" & pyExe & """ """ & pyScript & """"""
shell.Run cmd, 1, False
