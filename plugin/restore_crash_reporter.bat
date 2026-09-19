@echo off
:: Restores the Autodesk crash-report dialog for Moldflow Synergy 2027.
:: (Reverses the senddmp.exe rename done on 2026-07-23 to hide the harmless
::  shutdown-crash dialog from OT602asu.dll during client demos.)
net session >nul 2>&1
if not %errorLevel% == 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
if exist "C:\Program Files\Autodesk\Moldflow Synergy 2027\bin\senddmp.exe.disabled" (
    ren "C:\Program Files\Autodesk\Moldflow Synergy 2027\bin\senddmp.exe.disabled" "senddmp.exe"
    echo Moldflow senddmp.exe RESTORED.
) else (
    echo Moldflow senddmp.exe.disabled not found - nothing to restore.
)
if exist "C:\Program Files\Autodesk\Autodesk CER\dialog\cer_dialog.exe.disabled" (
    del "C:\Program Files\Autodesk\Autodesk CER\dialog\cer_dialog.exe" 2>nul
    ren "C:\Program Files\Autodesk\Autodesk CER\dialog\cer_dialog.exe.disabled" "cer_dialog.exe"
    echo Shared Autodesk CER dialog RESTORED - silent stub removed.
) else (
    echo cer_dialog.exe.disabled not found - nothing to restore.
)
pause
