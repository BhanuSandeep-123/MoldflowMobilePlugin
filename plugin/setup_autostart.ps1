# setup_autostart.ps1
# Copies run_startup.vbs to Moldflow's commands directory so it can be
# registered as a startup command that auto-runs when Moldflow opens.
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceVBS = Join-Path $scriptDir "run_startup.vbs"
$moldflowCmdDir = "C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands"

Write-Host "=== Moldflow Startup Auto-Run Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check source file exists
if (-not (Test-Path $sourceVBS)) {
    Write-Host "ERROR: run_startup.vbs not found at:" -ForegroundColor Red
    Write-Host "  $sourceVBS"
    exit 1
}

# Check Moldflow commands directory exists
if (-not (Test-Path $moldflowCmdDir)) {
    Write-Host "ERROR: Moldflow commands directory not found at:" -ForegroundColor Red
    Write-Host "  $moldflowCmdDir"
    Write-Host ""
    Write-Host "Please check your Moldflow Synergy 2027 installation path."
    exit 1
}

$destVBS = Join-Path $moldflowCmdDir "run_startup.vbs"

# Copy the file
try {
    Copy-Item -Path $sourceVBS -Destination $destVBS -Force
    Write-Host "SUCCESS: Copied run_startup.vbs to:" -ForegroundColor Green
    Write-Host "  $destVBS"
} catch {
    Write-Host "ERROR: Could not copy file. Run this script as Administrator:" -ForegroundColor Red
    Write-Host "  Right-click PowerShell > Run as Administrator"
    Write-Host "  Then run: powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1"
    exit 1
}

Write-Host ""
Write-Host "=== Next Steps ===" -ForegroundColor Yellow
Write-Host ""
Write-Host "To make it auto-run on Moldflow startup:"
Write-Host "  1. Open Moldflow Insight 2027"
Write-Host "  2. Go to: Tools > Application Options"
Write-Host "  3. In the 'Startup command' field, type: run_startup"
Write-Host "  4. Click OK"
Write-Host ""
Write-Host "Now every time Moldflow opens, it will automatically"
Write-Host "prompt you to create a new project."
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
