@echo off
:: Check for admin privileges
net session >nul 2>&1
if %errorLevel% == 0 (
    goto :admin
) else (
    goto :elevate
)

:elevate
echo Requesting administrator privileges...
powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:admin
echo Running installation with admin privileges...
:: NOTE: run_startup.vbs actually lives in the INNER MoldflowSynergyPlugin
:: folder alongside the plugin's .py files/.venv, not the outer checkout
:: folder this .bat file itself sits in one level above.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Copy-Item -Path 'C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\plugin\run_startup.vbs' -Destination 'C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands\run_startup.vbs' -Force"
echo.
echo Success! Updated startup script copied to Moldflow commands folder.
pause
exit
