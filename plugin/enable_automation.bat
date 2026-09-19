@echo off
:: Switch Moldflow automation back ON.
:: Deletes automation_disabled.flag. Takes effect the next time Synergy starts
:: (the startup script launches the observer again).
:: No admin rights needed.
del "%~dp0automation_disabled.flag" 2>nul
echo.
echo Automation is now ENABLED.
echo It becomes active the next time you start Moldflow Synergy.
echo.
pause
