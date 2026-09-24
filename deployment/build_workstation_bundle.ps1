# deployment/build_workstation_bundle.ps1
# ----------------------------------------
# Stage 8B Phase 1: Automated Workstation Runtime Extraction Tool.
# Builds a self-contained, production-isolated workstation runtime bundle
# completely independent of the developer Git repository and dev paths.

[CmdletBinding()]
param(
    [string]$OutputDir = "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging",
    [switch]$Clean = $true
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile Workstation Runtime Bundle Extractor       " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Source Repository : $RepoRoot"
Write-Host " Target Output Dir : $OutputDir"
Write-Host ""

if ($Clean -and (Test-Path $OutputDir)) {
    Write-Host "Cleaning existing output directory..." -ForegroundColor Yellow
    Remove-Item -Path $OutputDir -Recurse -Force
}

# 1. Create target directory structure
$directories = @(
    "agents\post_analyze",
    "monitor",
    "lib\scm",
    "lib\mobile",
    "lib\jobs",
    "plugin",
    "config"
)

foreach ($dir in $directories) {
    $targetPath = Join-Path $OutputDir $dir
    if (-not (Test-Path $targetPath)) {
        New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
    }
}

# 2. Define exact runtime files manifest
$runtimeFiles = @(
    # Agents (Post-Analyze)
    "agents\post_analyze\__init__.py",
    "agents\post_analyze\agent.py",
    "agents\post_analyze\job_detector.py",
    "agents\post_analyze\inspection_engine.py",
    "agents\post_analyze\config.json",

    # Monitor
    "monitor\standalone_job_monitor.py",

    # Core Libraries
    "lib\__init__.py",
    "lib\scm\__init__.py",
    "lib\scm\client.py",
    "lib\mobile\__init__.py",
    "lib\mobile\reporter.py",
    "lib\mobile\enrollment.py",
    "lib\jobs\__init__.py",
    "lib\jobs\models.py",

    # Synergy Plugin
    "plugin\run_startup.vbs",
    "plugin\moldflow_observer.py",
    "plugin\moldflow_startup.py",
    "plugin\embedded_ui.py",
    "plugin\ui_bridge.py",
    "plugin\ui_launcher.py",
    "plugin\assistant_panel.py",
    "plugin\assistant_live.py",
    "plugin\cad_diagnostics.py",
    "plugin\mesh_geometry.py",
    "plugin\synergy_connect.py",
    "plugin\session_context.py",
    "plugin\export_dimensions.py",
    "plugin\report_style.py",
    "plugin\ai_assistant.py",
    "plugin\ai_report_summary.py",
    "plugin\compute_jobs.py",
    "plugin\mobile_reporter.py",
    "plugin\standalone_job_monitor.py",
    "plugin\mobile_report_config.json.example"
)

Write-Host "Copying runtime files..." -ForegroundColor Yellow
$copiedCount = 0

foreach ($relPath in $runtimeFiles) {
    $src = Join-Path $RepoRoot $relPath
    $dest = Join-Path $OutputDir $relPath
    if (Test-Path $src) {
        $destParent = Split-Path -Parent $dest
        if (-not (Test-Path $destParent)) {
            New-Item -ItemType Directory -Path $destParent -Force | Out-Null
        }
        Copy-Item -Path $src -Destination $dest -Force
        $copiedCount++
    } else {
        Write-Warning "Required runtime file not found in repo: $src"
    }
}

# 3. Copy Directory Assets
$assetDirs = @(
    "plugin\assets",
    "plugin\ui_design"
)

foreach ($relDir in $assetDirs) {
    $srcDir = Join-Path $RepoRoot $relDir
    $destDir = Join-Path $OutputDir $relDir
    if (Test-Path $srcDir) {
        Copy-Item -Path $srcDir -Destination (Split-Path -Parent $destDir) -Recurse -Force
        Write-Host "  [DIR] Copied $relDir"
    }
}

# Copy config template into config/
$exampleConfigSrc = Join-Path $RepoRoot "plugin\mobile_report_config.json.example"
if (Test-Path $exampleConfigSrc) {
    Copy-Item -Path $exampleConfigSrc -Destination (Join-Path $OutputDir "config\config.json.example") -Force
}

# Copy installer automation scripts into installer/
$installerDir = Join-Path $OutputDir "installer"
if (-not (Test-Path $installerDir)) {
    New-Item -ItemType Directory -Path $installerDir -Force | Out-Null
}
Copy-Item -Path (Join-Path $RepoRoot "deployment\Install-MoldflowWorkstation.ps1") -Destination (Join-Path $installerDir "Install-MoldflowWorkstation.ps1") -Force
Copy-Item -Path (Join-Path $RepoRoot "deployment\Uninstall-MoldflowWorkstation.ps1") -Destination (Join-Path $installerDir "Uninstall-MoldflowWorkstation.ps1") -Force
Write-Host "  [DIR] Copied installer engine scripts"


# 4. Enforce Production Hygiene (purge any bytecode, caches, or logs)
Write-Host "Enforcing bundle cleanliness (purging bytecode, caches, and logs)..." -ForegroundColor Yellow
Get-ChildItem -Path $OutputDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem -Path $OutputDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Extension -in @(".pyc", ".log") -or $_.Name -like "*.pyc" -or $_.Name -like "*.log"
} | Remove-Item -Force

# 5. Generate Manifest with SHA-256 Hashes
Write-Host "Generating bundle manifest..." -ForegroundColor Yellow
$manifestPath = Join-Path $OutputDir "MANIFEST.txt"
$allExtracted = Get-ChildItem -Path $OutputDir -Recurse -File | Where-Object {
    $_.FullName -ne $manifestPath -and
    $_.Extension -notin @(".pyc", ".log") -and
    $_.DirectoryName -notlike "*__pycache__*"
}

$manifestLines = [System.Collections.Generic.List[string]]::new()
$manifestLines.Add("MOLDFLOW MOBILE WORKSTATION RUNTIME BUNDLE MANIFEST")
$manifestLines.Add("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$manifestLines.Add("Target Runtime: MoldflowMobileWorkstation")
$manifestLines.Add("Total Files: $($allExtracted.Count)")
$totalSize = ($allExtracted | Measure-Object -Property Length -Sum).Sum
$manifestLines.Add("Total Bundle Size: $([math]::Round($totalSize / 1MB, 2)) MB ($totalSize bytes)")
$manifestLines.Add("")
$manifestLines.Add("EXCLUDED SUBSYSTEMS (Zero developer or backend code):")
$manifestLines.Add("  - backend/          (FastAPI, PostgreSQL, DB migrations)")
$manifestLines.Add("  - mobile/           (.NET MAUI mobile application)")
$manifestLines.Add("  - mobile-tests/     (C# integration tests)")
$manifestLines.Add("  - tests/            (Python pytest test suite)")
$manifestLines.Add("  - docs/             (Developer documentation)")
$manifestLines.Add("  - license_monitor/  (Independent network license subsystem)")
$manifestLines.Add("  - .git/             (Git metadata and commit history)")
$manifestLines.Add("")
$manifestLines.Add("FILE INVENTORY & SHA-256 CHECKSUMS:")
$manifestLines.Add("--------------------------------------------------------------------------------")

foreach ($item in ($allExtracted | Sort-Object FullName)) {
    $hash = (Get-FileHash -Path $item.FullName -Algorithm SHA256).Hash
    $subPath = $item.FullName.Substring($OutputDir.Length).TrimStart('\', '/')
    $manifestLines.Add("$hash  $subPath ($($item.Length) bytes)")
}

$manifestLines | Set-Content -Path $manifestPath -Encoding utf8

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Workstation Runtime Extraction Complete!" -ForegroundColor Green
Write-Host " Target Directory : $OutputDir"
Write-Host " Total Files      : $($allExtracted.Count)"
Write-Host " Total Size       : $([math]::Round($totalSize / 1MB, 2)) MB"
Write-Host " Manifest File    : $manifestPath"
Write-Host "============================================================" -ForegroundColor Green
