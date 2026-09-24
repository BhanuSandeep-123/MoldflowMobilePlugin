# deployment/Uninstall-MoldflowWorkstation.ps1
# ------------------------------------------
# Clean Uninstaller for Autodesk Moldflow Mobile Workstation.
# Stops & unregisters Scheduled Tasks, removes Synergy startup integration,
# removes runtime files, and optionally removes ProgramData configuration.

#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $false)]
    [string]$InstallDir = "$env:ProgramFiles\MoldflowMobileWorkstation",

    [Parameter(Mandatory = $false)]
    [string]$ConfigDir = "$env:ProgramData\MoldflowMobile",

    [Parameter(Mandatory = $false)]
    [string]$SynergyCommandsDir = "",

    [Parameter(Mandatory = $false)]
    [switch]$RemoveConfig,

    [Parameter(Mandatory = $false)]
    [switch]$SkipTasks,

    [Parameter(Mandatory = $false)]
    [switch]$Quiet,

    [Parameter(Mandatory = $false)]
    [switch]$SkipFileDelete,

    [Parameter(Mandatory = $false)]
    [switch]$Silent
)

$ErrorActionPreference = "Stop"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile Workstation Uninstaller                    " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Target Runtime : $InstallDir"
Write-Host " Config Dir     : $ConfigDir (Remove: $RemoveConfig)"
Write-Host ""

# ----------------------------------------------------------------------------
# 1. Stop and Unregister Scheduled Tasks
# ----------------------------------------------------------------------------
Write-Host "[1/4] Removing Scheduled Tasks..." -ForegroundColor Yellow
if (-not $SkipTasks) {
    $tasks = @(
        "Moldflow Mobile Job Monitor",
        "Moldflow Post-Analyze Agent"
    )

    foreach ($t in $tasks) {
        try {
            $taskObj = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            if ($taskObj) {
                Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
                Write-Host "  [OK] Unregistered Scheduled Task: $t" -ForegroundColor Green
            } else {
                Write-Host "  [INFO] Task not found: $t" -ForegroundColor Gray
            }
        }
        catch {
            Write-Warning "Could not unregister task ${t}: $_"
        }
    }
} else {
    Write-Host "  [SKIPPED] Scheduled task removal skipped by parameter." -ForegroundColor Gray
}

# ----------------------------------------------------------------------------
# 2. Remove Synergy Startup Integration
# ----------------------------------------------------------------------------
Write-Host "`n[2/4] Removing Synergy Startup Command..." -ForegroundColor Yellow
$commandsDirs = [System.Collections.Generic.List[string]]::new()

if ($SynergyCommandsDir -and (Test-Path $SynergyCommandsDir)) {
    $commandsDirs.Add($SynergyCommandsDir)
} else {
    # Check HKLM
    $regRoots = @(
        "HKLM:\Software\Autodesk\Moldflow Synergy",
        "HKLM:\Software\WOW6432Node\Autodesk\Moldflow Synergy"
    )
    foreach ($regRoot in $regRoots) {
        if (Test-Path $regRoot) {
            $versions = Get-ChildItem -Path $regRoot -ErrorAction SilentlyContinue
            foreach ($vKey in $versions) {
                $props = Get-ItemProperty -Path $vKey.PSPath -ErrorAction SilentlyContinue
                $loc = $props.InstallLocation
                if (-not $loc) { $loc = $props.InstallDirectory }
                if ($loc) {
                    $cDir = Join-Path $loc "data\commands"
                    if (Test-Path $cDir) { $commandsDirs.Add($cDir) }
                }
            }
        }
    }

    # Filesystem fallbacks (dynamic program roots)
    $programRoots = @(
        $env:ProgramFiles,
        $env:ProgramW6432,
        ${env:ProgramFiles(x86)}
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

    $supportedVersions = @("2027", "2026", "2025")
    foreach ($pRoot in $programRoots) {
        foreach ($v in $supportedVersions) {
            $candidateDir = Join-Path $pRoot ("Autodesk\Moldflow Synergy " + $v + "\data\commands")
            if ((Test-Path $candidateDir) -and (-not $commandsDirs.Contains($candidateDir))) {
                $commandsDirs.Add($candidateDir)
            }
        }
    }
}

foreach ($cDir in $commandsDirs) {
    $vbsPath = Join-Path $cDir "run_startup.vbs"
    $bakPath = Join-Path $cDir "run_startup.vbs.bak"

    if (Test-Path $vbsPath) {
        $content = Get-Content -Path $vbsPath -Raw -ErrorAction SilentlyContinue
        # Only remove if it is confirmed to belong to our product
        $isOurProduct = ($content -match "Moldflow Insight 2027" -or $content -match "ResolvePluginDir" -or $content -match "MFPLUGIN_SESSION")
        if ($isOurProduct) {
            Remove-Item -Path $vbsPath -Force -ErrorAction SilentlyContinue
            Write-Host "  [OK] Removed product run_startup.vbs from: $cDir" -ForegroundColor Green

            # If backup exists, restore it
            if (Test-Path $bakPath) {
                Move-Item -Path $bakPath -Destination $vbsPath -Force -ErrorAction SilentlyContinue
                Write-Host "  [OK] Restored original run_startup.vbs from backup in: $cDir" -ForegroundColor Green
            }
        } else {
            Write-Host "  [SKIP] Preserving run_startup.vbs in $cDir (does not belong to this product)." -ForegroundColor Yellow
        }
    }
}

# ----------------------------------------------------------------------------
# 3. Remove Runtime Directory
# ----------------------------------------------------------------------------
Write-Host "`n[3/4] Removing Workstation Runtime Files..." -ForegroundColor Yellow
if (-not $SkipFileDelete) {
    if (Test-Path $InstallDir) {
        try {
            Remove-Item -Path $InstallDir -Recurse -Force
            Write-Host "  [OK] Removed runtime directory: $InstallDir" -ForegroundColor Green
        }
        catch {
            Write-Warning "Could not completely remove ${InstallDir}: $_"
        }
    } else {
        Write-Host "  [INFO] Runtime directory already removed." -ForegroundColor Gray
    }
} else {
    Write-Host "  [SKIPPED] Runtime directory removal managed by MSI package." -ForegroundColor Gray
}

# ----------------------------------------------------------------------------
# 4. Handle Configuration Clean-up
# ----------------------------------------------------------------------------
Write-Host "`n[4/4] Evaluating Configuration Clean-up..." -ForegroundColor Yellow
if ($RemoveConfig -and (Test-Path $ConfigDir)) {
    try {
        Remove-Item -Path $ConfigDir -Recurse -Force
        Write-Host "  [OK] Removed configuration directory: $ConfigDir" -ForegroundColor Green
    }
    catch {
        Write-Warning "Could not remove configuration directory ${ConfigDir}: $_"
    }
} elseif (Test-Path $ConfigDir) {
    Write-Host "  [PRESERVED] Workstation configuration preserved at: $ConfigDir" -ForegroundColor Cyan
    Write-Host "              Use -RemoveConfig to delete saved credentials."
}

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Moldflow Mobile Workstation Uninstallation Complete!       " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
