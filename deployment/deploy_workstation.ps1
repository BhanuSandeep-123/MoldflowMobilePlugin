# deployment/deploy_workstation.ps1
# Complete automated workstation installer for Moldflow Mobile Job Monitoring.
# Supports any Windows workstation without machine-specific hardcoding.

[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$BackendUrl = "https://moldflowplugin-mobile-app.onrender.com",
    [Parameter(Mandatory=$true)]
    [string]$ApiKey,
    [string]$UserId = "DEV-USER-001",
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$SynergyCommandsDir = "C:\Program Files\Autodesk\Moldflow Synergy 2027\data\commands",
    [switch]$SkipTasks,
    [switch]$SkipSynergyCommand
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile Workstation Automated Deployment           " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Workstation Machine ID : $MachineId"
Write-Host " Workstation User ID    : $UserId"
Write-Host " Current Windows User   : $env:USERDOMAIN\$env:USERNAME"
Write-Host " Backend Target URL     : $BackendUrl"
Write-Host " Repository Root        : $RepoRoot"
Write-Host ""

# -----------------------------------------------------------------------------
# 1. Test Backend Connectivity
# -----------------------------------------------------------------------------
Write-Host "[1/5] Testing Backend Health..." -ForegroundColor Yellow
$healthUrl = "$($BackendUrl.TrimEnd('/'))/health"
try {
    $resp = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 10
    Write-Host "      [OK] Backend is reachable! Status: $($resp.status) ($($resp.service))" -ForegroundColor Green
} catch {
    Write-Warning "Could not reach backend at ${healthUrl}: $_"
    Write-Warning "Continuing deployment; ensure backend is accessible before starting analyses."
}

# -----------------------------------------------------------------------------
# 2. Generate Machine-Specific mobile_report_config.json
# -----------------------------------------------------------------------------
Write-Host "`n[2/5] Configuring Workstation Identity..." -ForegroundColor Yellow
$configPath = Join-Path $RepoRoot "plugin\mobile_report_config.json"
$configObject = [ordered]@{
    enabled     = $true
    backend_url = $BackendUrl.TrimEnd('/')
    api_key     = $ApiKey
    user_id     = $UserId
    machine_id  = $MachineId
}
$configJson = $configObject | ConvertTo-Json -Depth 4
Set-Content -Path $configPath -Value $configJson -Encoding utf8
Write-Host "      [OK] Generated config at: $configPath" -ForegroundColor Green
Write-Host "           machine_id = $MachineId"
Write-Host "           user_id    = $UserId"

# -----------------------------------------------------------------------------
# 3. Deploy Synergy Startup Integration
# -----------------------------------------------------------------------------
Write-Host "`n[3/5] Deploying Synergy Startup Command..." -ForegroundColor Yellow
if (-not $SkipSynergyCommand) {
    if (Test-Path $SynergyCommandsDir) {
        $sourceVbs = Join-Path $RepoRoot "plugin\run_startup.vbs"
        $destVbs = Join-Path $SynergyCommandsDir "run_startup.vbs"
        try {
            Copy-Item -Path $sourceVbs -Destination $destVbs -Force
            Write-Host "      [OK] Copied run_startup.vbs to: $destVbs" -ForegroundColor Green
        } catch {
            Write-Warning "Could not copy run_startup.vbs (administrator rights may be required): $_"
        }
    } else {
        Write-Host "      [INFO] Synergy commands directory not found ($SynergyCommandsDir). Skipping copy." -ForegroundColor Gray
    }
} else {
    Write-Host "      [SKIPPED] Synergy command deployment skipped by parameter." -ForegroundColor Gray
}

# -----------------------------------------------------------------------------
# 4. Register Post-Analyze Agent Scheduled Task
# -----------------------------------------------------------------------------
Write-Host "`n[4/5] Registering Post-Analyze Agent..." -ForegroundColor Yellow
if (-not $SkipTasks) {
    $postAnalyzeScript = Join-Path $ScriptDir "register_post_analyze_task.ps1"
    & $postAnalyzeScript
} else {
    Write-Host "      [SKIPPED] Scheduled task registration skipped by parameter." -ForegroundColor Gray
}

# -----------------------------------------------------------------------------
# 5. Register Mobile Job Monitor Scheduled Task
# -----------------------------------------------------------------------------
Write-Host "`n[5/5] Registering Mobile Job Monitor..." -ForegroundColor Yellow
if (-not $SkipTasks) {
    $jobMonitorScript = Join-Path $ScriptDir "register_job_monitor_task.ps1"
    & $jobMonitorScript
} else {
    Write-Host "      [SKIPPED] Job monitor task registration skipped by parameter." -ForegroundColor Gray
}

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Workstation Deployment Complete for $MachineId!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
