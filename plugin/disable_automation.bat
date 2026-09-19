@echo off
:: Switch Moldflow automation OFF (manual mode).
:: Creates automation_disabled.flag - run_startup.vbs and the observer see it
:: and stand down. Works immediately: a running observer exits on its next
:: 3-second cycle, and the next Synergy start shows no prompts at all.
:: No admin rights needed. Re-enable with enable_automation.bat.
echo manual mode > "%~dp0automation_disabled.flag"
echo.
echo Automation is now DISABLED (manual mode).
echo Synergy will start and run with no prompts and no background observer.
echo Run enable_automation.bat to turn the automation back on.
echo.
pause
