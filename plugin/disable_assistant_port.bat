@echo off
REM  Undo enable_assistant_port.bat: remove the WebView2 debug-port policy for
REM  synergy.exe. Takes effect for WebView2 views created afterwards, so restart
REM  Synergy. The deck then falls back to the locally computed summary.
REM
REM  Also clears the older WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS environment
REM  variable, which an earlier version of this plugin used to set.

setlocal
set KEY=HKCU\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments

reg delete "%KEY%" /v "synergy.exe" /f >nul 2>&1
if errorlevel 1 (
    echo   Policy: nothing to remove.
) else (
    echo   [ok] Policy removed for synergy.exe.
)

reg query "HKCU\Environment" /v WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS >nul 2>&1
if not errorlevel 1 (
    reg delete "HKCU\Environment" /v WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS /f >nul 2>&1
    echo   [ok] Environment variable removed as well.
)

echo   Restart Synergy for this to take effect.
pause
