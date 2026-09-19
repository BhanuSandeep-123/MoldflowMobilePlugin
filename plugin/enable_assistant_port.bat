@echo off
REM ---------------------------------------------------------------------------
REM  enable_assistant_port.bat  --  run ONCE
REM ---------------------------------------------------------------------------
REM  The review deck's engineering commentary is written by Moldflow's own AI
REM  Assistant panel. That panel is a WebView2 (Edge) view, and it can only be
REM  read if WebView2 was given a DevTools port.
REM
REM  This writes an app-scoped WebView2 policy for synergy.exe:
REM
REM    HKCU\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
REM        "synergy.exe" = "--remote-debugging-port=0"
REM
REM  WHY THE REGISTRY AND NOT WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
REM    * the environment variable applies to EVERY WebView2 app this user runs;
REM      this applies to synergy.exe alone
REM    * WebView2 reads it when the view is CREATED, not when synergy.exe
REM      starts, so a Synergy that has not yet opened the Assistant panel picks
REM      it up without being restarted
REM
REM  Port 0 = "pick a free one". Synergy hosts TWO WebView2 environments (the
REM  Simulation Hub tab and the Assistant panel); a fixed port makes them race
REM  for it and only one wins, unpredictably.
REM
REM  This opens a local DevTools port on the Assistant's WebView2 for the life
REM  of each Synergy session. Any process on this machine can connect to it.
REM  Fine on your own workstation; not for a shared or untrusted machine.
REM  Undo with:  disable_assistant_port.bat
REM ---------------------------------------------------------------------------

setlocal
set KEY=HKCU\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments

echo.
echo   This lets the plugin read Moldflow's AI Assistant panel, so the review
echo   deck can carry its engineering commentary and recommendations.
echo.
echo   It sets, for your user account and for synergy.exe only:
echo       %KEY%
echo           "synergy.exe" = "--remote-debugging-port=0"
echo.

REM  The environment variable OUTRANKS this policy in WebView2's precedence
REM  order, so a stale one left over from earlier experimenting would silently
REM  win and nothing would change. If it is a fixed port it is also the active
REM  fault: Synergy's two WebView2 views race for that single port and the
REM  Assistant usually loses. Remove it and let the app-scoped policy apply.
reg query "HKCU\Environment" /v WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS >nul 2>&1
if not errorlevel 1 (
    echo   It will ALSO remove this user environment variable, which currently
    echo   overrides the policy above and applies to every WebView2 app you run:
    echo.
    reg query "HKCU\Environment" /v WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
    echo.
)
echo   Run disable_assistant_port.bat to undo it.
echo.
set /p ANSWER=  Continue? [y/N]
if /i not "%ANSWER%"=="y" (
    echo   Cancelled. Nothing was changed.
    goto :eof
)

REM  Preferred: the app-scoped policy, which touches synergy.exe alone. On most
REM  machines HKCU\Software\Policies is ACL-protected and a standard user gets
REM  "Access is denied" -- writing policy is an administrator's job by design.
REM  So this is an attempt, not an assumption, and the environment variable is
REM  the fallback when it fails.
set SCOPED=0
reg add "%KEY%" /v "synergy.exe" /t REG_SZ /d "--remote-debugging-port=0" /f >nul 2>&1
if not errorlevel 1 set SCOPED=1

if "%SCOPED%"=="1" (
    reg delete "HKCU\Environment" /v WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS /f >nul 2>&1
    echo.
    echo   [ok] App-scoped policy written for synergy.exe; the overriding
    echo        environment variable has been removed.
) else (
    REM  The variable must be SET, not merely cleared: with the policy
    REM  unavailable it is the only remaining mechanism. Port 0, never a fixed
    REM  port -- Synergy's two WebView2 views would race for a fixed one.
    setx WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS "--remote-debugging-port=0" >nul
    if errorlevel 1 (
        echo.
        echo   [x] Could not write the policy OR the environment variable.
        goto :eof
    )
    echo.
    echo   [ok] The app-scoped policy needs administrator rights on this
    echo        machine, so the environment variable was used instead:
    echo            WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=0
    echo        This applies to every WebView2 app you run, not just Synergy.
    echo        To scope it to Synergy alone, re-run this file as administrator.
)
echo.
echo   NOW CLOSE SYNERGY COMPLETELY AND START IT AGAIN. A WebView2 view keeps
echo   the arguments it was created with, and removing the environment variable
echo   only affects processes started afterwards -- so the running Synergy is
echo   still using the old setting.
echo.
echo   To check, with Synergy running:
echo       .venv\Scripts\python.exe assistant_live.py
echo   It should print:  port status: ready
echo   ("panel_closed" is fine too -- the plugin opens the panel itself.)
echo.
pause
