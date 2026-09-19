'%RunPerInstance
'@
'@ DESCRIPTION
'@ Runs CAD Diagnostics on the CURRENTLY OPEN study and stops.
'@
'@ No repair prompt, no Fusion round trip, no automation workflow. Nothing is
'@ modified, so the same study can be measured twice and the numbers compared.
'@
'@ SYNTAX
'@ run_diagnostics
'@@

' ---------------------------------------------------------------------------
' The '%RunPerInstance directive on line 1 is the ONLY reason this wrapper
' exists, and it must stay on line 1.
'
' It tells Synergy to attach the macro to THIS instance and to set the
' SAInstance environment variable that synergy_connect.py binds to. Without it
' Synergy warns "This macro/script should be edited to call a specific instance
' of Synergy, otherwise it will call the first instance that was executed on
' this machine" -- and then hands over a stale COM object. That failure is not
' obvious: Compute() returns False, the wrong study gets reported, and the run
' still writes a report full of zeros that looks like a clean model.
'
' Synergy only parses this directive in its own macro files, so a .py assigned
' directly to a macro button can never carry it.
' ---------------------------------------------------------------------------

Option Explicit

' Absolute, because once this file is copied into Synergy's commands folder it
' can no longer find the plugin from its own path. Note the doubled folder
' name: the .py files and .venv live in the INNER MoldflowSynergyPlugin folder.
Const PLUGIN_DIR = "c:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin"

Dim shell, fso, pyExe, pyScript, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

pyExe = PLUGIN_DIR & "\.venv\Scripts\python.exe"
If Not fso.FileExists(pyExe) Then
  pyExe = "python"
End If

pyScript = PLUGIN_DIR & "\run_diagnostics.py"

If Not fso.FileExists(pyScript) Then
  MsgBox "Cannot find:" & vbCrLf & pyScript & vbCrLf & vbCrLf & _
         "Check the PLUGIN_DIR constant inside run_diagnostics.vbs.", _
         vbCritical, "Moldflow CAD Diagnostics"
  WScript.Quit 1
End If

' cmd /k keeps the console open so the numbers can be read. The child process
' inherits SAInstance from this macro, which is what binds it to this Synergy.
cmd = "%comspec% /k """"" & pyExe & """ """ & pyScript & """"""
shell.Run cmd, 1, False
