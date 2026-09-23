@echo off
setlocal

:: Canonical Moldflow Mobile System plugin Python
set PYTHON_EXE=%~dp0..\..\plugin\.venv\Scripts\python.exe

:: Dynamic fallbacks for other installations
if not exist "%PYTHON_EXE%" (
  set PYTHON_EXE=%USERPROFILE%\Documents\MoldflowMobileSystem\plugin\.venv\Scripts\python.exe
)
if not exist "%PYTHON_EXE%" (
  set PYTHON_EXE=C:\Program Files\Python314\python.exe
)

if not exist "%PYTHON_EXE%" (
  echo Moldflow plugin Python was not found.
  pause
  exit /b 1
)

cd /d %~dp0
echo ======================================================================
echo Launching Moldflow Standalone Post-Analyze Agent
echo Canonical location: MoldflowMobileSystem\agents\post_analyze\
echo Python: %PYTHON_EXE%
echo Working Directory: %~dp0
echo ======================================================================
echo.

%PYTHON_EXE% %~dp0agent.py
set RC=%ERRORLEVEL%
echo.
echo Standalone agent exited with code %RC%.
pause
exit /b %RC%
