# deployment/build_msi.ps1
# ------------------------
# Builds the production MSI installer for Moldflow Mobile Workstation Runtime.
# Uses WiX Toolset v7.0.0 with automated payload harvesting and staging hygiene enforcement.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$StagingDir = "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging",

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = "",

    [Parameter(Mandatory = $false)]
    [string]$Version = "1.0.0.0"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "deployment\output"
}

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$MsiPath = Join-Path $OutputDir "MoldflowMobileWorkstation.msi"
$WxsPath = Join-Path $ScriptDir "msi\MoldflowMobileWorkstation.wxs"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile Workstation MSI Build Pipeline             " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Repo Root     : $RepoRoot"
Write-Host " Staging Dir   : $StagingDir"
Write-Host " WXS Source    : $WxsPath"
Write-Host " Target MSI    : $MsiPath"
Write-Host " Version       : $Version"
Write-Host ""

# ----------------------------------------------------------------------------
# Step 1: Ensure Staging Directory is clean and fresh
# ----------------------------------------------------------------------------
Write-Host "[1/4] Refreshing clean workstation staging payload..." -ForegroundColor Yellow
$bundleExtractor = Join-Path $ScriptDir "build_workstation_bundle.ps1"
if (Test-Path $bundleExtractor) {
    & $bundleExtractor -OutputDir $StagingDir
    Write-Host "  [OK] Staging payload refreshed." -ForegroundColor Green
} else {
    Write-Warning "Bundle extractor script not found; using existing staging directory."
}

# ----------------------------------------------------------------------------
# Step 2: Verify zero bytecode and logs in staging
# ----------------------------------------------------------------------------
Write-Host "`n[2/4] Verifying payload hygiene..." -ForegroundColor Yellow
$pycCount = (Get-ChildItem -Path $StagingDir -Recurse -Filter "*.pyc" -File -ErrorAction SilentlyContinue).Count
$logCount = (Get-ChildItem -Path $StagingDir -Recurse -Filter "*.log" -File -ErrorAction SilentlyContinue).Count
$cacheCount = (Get-ChildItem -Path $StagingDir -Recurse -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue).Count

if ($pycCount -gt 0 -or $logCount -gt 0 -or $cacheCount -gt 0) {
    throw "Staging payload hygiene check failed: $pycCount .pyc, $logCount .log, $cacheCount __pycache__ files found."
}
Write-Host "  [OK] Payload hygiene verified (0 .pyc, 0 .log, 0 __pycache__)." -ForegroundColor Green

# ----------------------------------------------------------------------------
# Step 3: Compile and link MSI using WiX v7
# ----------------------------------------------------------------------------
Write-Host "`n[3/4] Compiling and linking MSI via WiX Toolset..." -ForegroundColor Yellow
$wixCmd = Get-Command wix -ErrorAction SilentlyContinue
if (-not $wixCmd) {
    throw "WiX Toolset (wix CLI) was not found in PATH. Install via 'dotnet tool install --global wix'."
}

# Check if target MSI is locked or existing
if (Test-Path $MsiPath) {
    Remove-Item -Path $MsiPath -Force -ErrorAction SilentlyContinue
}

$buildArgs = @(
    "build",
    "-arch", "x64",
    "`"$WxsPath`"",
    "-d", "StagingDir=`"$StagingDir`"",
    "-ext", "WixToolset.UI.wixext",
    "-o", "`"$MsiPath`""
)

Write-Host "  Executing: wix $($buildArgs -join ' ')" -ForegroundColor Gray
$proc = Start-Process -FilePath "wix" -ArgumentList $buildArgs -NoNewWindow -PassThru -Wait

if ($proc.ExitCode -ne 0) {
    throw "WiX build failed with exit code $($proc.ExitCode)."
}

if (-not (Test-Path $MsiPath)) {
    throw "Target MSI was not created at expected path: $MsiPath"
}
Write-Host "  [OK] MSI compiled successfully!" -ForegroundColor Green

# ----------------------------------------------------------------------------
# Step 4: Verification and Checksum
# ----------------------------------------------------------------------------
Write-Host "`n[4/4] Verifying MSI Artifact..." -ForegroundColor Yellow
$fileItem = Get-Item $MsiPath
$fileSizeMb = [math]::Round($fileItem.Length / 1MB, 2)
$hash = (Get-FileHash -Path $MsiPath -Algorithm SHA256).Hash

Write-Host "============================================================" -ForegroundColor Green
Write-Host " Production MSI Built Successfully!                         " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Output File   : $MsiPath"
Write-Host " Size          : $fileSizeMb MB ($($fileItem.Length) bytes)"
Write-Host " SHA-256 Hash  : $hash"
Write-Host "============================================================" -ForegroundColor Green

return [PSCustomObject]@{
    Status = "SUCCESS"
    Path = $MsiPath
    Size = $fileItem.Length
    SHA256 = $hash
}
