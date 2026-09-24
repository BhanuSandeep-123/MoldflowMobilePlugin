@echo off
setlocal EnableDelayedExpansion

:: Setup.cmd - Double-clickable elevated launcher for Moldflow Mobile Workstation Installer
title Moldflow Mobile Workstation Setup

:: Check for Administrative privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Requesting Administrator privileges (UAC elevation)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    exit /b %errorLevel%
)

:: Running elevated
cd /d "%~dp0"
echo ============================================================
echo  Starting Moldflow Mobile Workstation Production Installer
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-MoldflowWorkstation.ps1" %*
set SCRIPT_EXIT=%errorlevel%

echo.
if %SCRIPT_EXIT% equ 0 (
    echo [SUCCESS] Setup completed successfully.
) else (
    echo [ERROR] Setup encountered an error (Code: %SCRIPT_EXIT%).
)

:: If run from double-click (explorer), pause so window doesn't immediately vanish
echo %CMDCMDLINE% | find /i "%~0" >nul
if %errorlevel% equ 0 (
    echo.
    echo Press any key to exit...
    pause >nul
)

exit /b %SCRIPT_EXIT%
