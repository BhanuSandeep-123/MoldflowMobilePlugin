<#
.SYNOPSIS
    Installs and configures the Autodesk Moldflow Synergy Mobile Monitoring Plugin
    for the current workstation.

.DESCRIPTION
    Standardizes workstation setup by:
    1. Verifying connectivity to the Moldflow Mobile FastAPI backend.
    2. Deploying/updating plugin Python scripts into the local Synergy plugin folder.
    3. Generating the workstation-specific mobile_report_config.json configuration.

.PARAMETER BackendUrl
    The URL of the Moldflow Mobile backend server (e.g. http://127.0.0.1:8001, http://192.168.1.100:8001, or https://moldflow.mycompany.com)

.PARAMETER ApiKey
    The X-Api-Key assigned to this workstation / engineer.

.PARAMETER UserId
    The user ID of the engineer (e.g. DEV-USER-001).

.PARAMETER MachineId
    The machine identifier (defaults to local computer name).

.PARAMETER TargetDir
    The target Moldflow Synergy plugin directory.
#>

[CmdletBinding()]
param(
    [string]$BackendUrl = "http://127.0.0.1:8001",
    [string]$ApiKey = "dev-moldflow-key-change-me",
    [string]$UserId = "DEV-USER-001",
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$TargetDir = "$env:USERPROFILE\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin"
)

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Autodesk Moldflow Synergy Plugin - Workstation Installer  " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[1/4] Target Configuration:" -ForegroundColor Yellow
Write-Host "      Backend URL : $BackendUrl"
Write-Host "      User ID     : $UserId"
Write-Host "      Machine ID  : $MachineId"
Write-Host "      Plugin Path : $TargetDir"
Write-Host ""

# -----------------------------------------------------------------------------
# 1. Verify Backend Connectivity
# -----------------------------------------------------------------------------
Write-Host "[2/4] Testing backend connectivity..." -ForegroundColor Yellow
$healthUrl = "$($BackendUrl.TrimEnd('/'))/health"

try {
    $healthResp = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 5 -ErrorAction Stop
    Write-Host "      [OK] Connected to Backend successfully!" -ForegroundColor Green
    Write-Host "      [OK] Service: $($healthResp.service)" -ForegroundColor Green
    Write-Host "      [OK] Database: $($healthResp.database)" -ForegroundColor Green
    if ($healthResp.pool) {
        Write-Host "      [OK] Connection Pool: min=$($healthResp.pool.min_size), max=$($healthResp.pool.max_size)" -ForegroundColor Green
    }
}
catch {
    Write-Warning ("Could not reach backend health endpoint at " + $healthUrl + ": " + $_)
    Write-Warning "Proceeding with file installation, but please ensure the backend is started."
}

# -----------------------------------------------------------------------------
# 2. Ensure Target Directory Exists
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "[3/4] Ensuring plugin directory structure..." -ForegroundColor Yellow
if (-not (Test-Path -Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    Write-Host "      Created directory: $TargetDir" -ForegroundColor Green
} else {
    Write-Host "      Found directory: $TargetDir" -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 3. Write Workstation Configuration (mobile_report_config.json)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "[4/4] Writing workstation configuration..." -ForegroundColor Yellow

$configData = @{
    enabled     = $true
    backend_url = $BackendUrl.TrimEnd('/')
    api_key     = $ApiKey
    user_id     = $UserId
    machine_id  = $MachineId
}

$configJson = $configData | ConvertTo-Json -Depth 4
$configPath = Join-Path -Path $TargetDir -ChildPath "mobile_report_config.json"

Set-Content -Path $configPath -Value $configJson -Encoding utf8
Write-Host "      [OK] Saved configuration to: $configPath" -ForegroundColor Green

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Workstation Installation & Configuration Completed!       " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
