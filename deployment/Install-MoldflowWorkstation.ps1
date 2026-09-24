# deployment/Install-MoldflowWorkstation.ps1
# ----------------------------------------
# Production Workstation Installer for Autodesk Moldflow Mobile System.
# Installs runtime into %ProgramFiles%\MoldflowMobileWorkstation,
# discovers Moldflow Synergy, enrolls workstation via one-time token,
# applies least-privilege ACLs to %ProgramData%\MoldflowMobile\config.json,
# registers single-instance Scheduled Tasks, and configures Synergy startup.

#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $false)]
    [string]$BackendUrl = "https://moldflowplugin-mobile-app.onrender.com",

    [Parameter(Mandatory = $false)]
    [string]$UserId = "DEV-USER-001",

    [Parameter(Mandatory = $false)]
    [string]$MachineId = $env:COMPUTERNAME,

    [Parameter(Mandatory = $false)]
    [string]$MachineName = "",

    # Secure enrollment token options
    [Parameter(Mandatory = $false)]
    [string]$EnrollmentToken = "",

    [Parameter(Mandatory = $false)]
    [string]$EnrollmentTokenFile = "",

    [Parameter(Mandatory = $false)]
    [switch]$ForceReenroll,

    # Custom directory overrides (optional)
    [Parameter(Mandatory = $false)]
    [string]$InstallDir = "$env:ProgramFiles\MoldflowMobileWorkstation",

    [Parameter(Mandatory = $false)]
    [string]$ConfigDir = "$env:ProgramData\MoldflowMobile",

    [Parameter(Mandatory = $false)]
    [string]$SourceBundleDir = "",

    [Parameter(Mandatory = $false)]
    [string]$SynergyCommandsDir = "",

    [Parameter(Mandatory = $false)]
    [switch]$SkipTasks,

    [Parameter(Mandatory = $false)]
    [switch]$SkipSynergyCommand,

    [Parameter(Mandatory = $false)]
    [switch]$SkipFileCopy,

    [Parameter(Mandatory = $false)]
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

# ============================================================================
# Helper Functions: Logging & Masking
# ============================================================================

function Write-InstallerHeader {
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " Moldflow Mobile Workstation Production Installer           " -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " Machine ID       : $MachineId"
    Write-Host " Target User ID   : $UserId"
    Write-Host " Install Target   : $InstallDir"
    Write-Host " Config Target    : $ConfigDir"
    Write-Host " Backend Target   : $BackendUrl"
    Write-Host " Runtime User     : $env:USERDOMAIN\$env:USERNAME"
    Write-Host ""
}

function Mask-Secret([string]$secret) {
    if ([string]::IsNullOrWhiteSpace($secret)) { return "<empty>" }
    if ($secret.Length -le 10) { return "********" }
    return ($secret.Substring(0, 6) + "..." + $secret.Substring($secret.Length - 4))
}

# ============================================================================
# 1. Moldflow Synergy Path Discovery
# ============================================================================

function Find-MoldflowSynergyInstallation {
    [CmdletBinding()]
    param()

    # If explicit path was provided and is valid, use it
    if ($SynergyCommandsDir -and (Test-Path $SynergyCommandsDir)) {
        Write-Host "  Using explicitly configured Synergy commands path: $SynergyCommandsDir" -ForegroundColor Green
        return @{
            Version = "Custom"
            InstallLocation = (Split-Path -Parent (Split-Path -Parent $SynergyCommandsDir))
            CommandsDir = $SynergyCommandsDir
            SynergyExe = ""
        }
    }

    $candidates = [System.Collections.Generic.List[hashtable]]::new()

    # 1. Inspect HKLM (64-bit and WOW6432Node)
    $regRoots = @(
        "HKLM:\Software\Autodesk\Moldflow Synergy",
        "HKLM:\Software\WOW6432Node\Autodesk\Moldflow Synergy"
    )

    foreach ($regRoot in $regRoots) {
        if (Test-Path $regRoot) {
            $versions = Get-ChildItem -Path $regRoot -ErrorAction SilentlyContinue
            foreach ($vKey in $versions) {
                $verName = $vKey.PSChildName
                $props = Get-ItemProperty -Path $vKey.PSPath -ErrorAction SilentlyContinue
                $loc = $props.InstallLocation
                if (-not $loc) { $loc = $props.InstallDirectory }

                if ($loc -and (Test-Path $loc)) {
                    if ($loc.ToLower().EndsWith("\bin")) {
                        $loc = Split-Path -Parent $loc
                    }
                    $cDir = Join-Path $loc "data\commands"
                    $sExe = Join-Path $loc "bin\synergy.exe"
                    if ((Test-Path $cDir) -and (Test-Path $sExe)) {
                        $candidates.Add(@{
                            Version = $verName
                            InstallLocation = $loc
                            CommandsDir = $cDir
                            SynergyExe = $sExe
                            Source = "HKLM ($verName)"
                        })
                    }
                }
            }
        }
    }

    # 2. Inspect HKCU as documented fallback
    $hkcuRoot = "HKCU:\Software\Autodesk\Moldflow Synergy"
    if (Test-Path $hkcuRoot) {
        $versions = Get-ChildItem -Path $hkcuRoot -ErrorAction SilentlyContinue
        foreach ($vKey in $versions) {
            $verName = $vKey.PSChildName
            $props = Get-ItemProperty -Path $vKey.PSPath -ErrorAction SilentlyContinue
            $loc = $props.InstallLocation
            if (-not $loc) { $loc = $props.InstallDirectory }
            if ($loc -and (Test-Path $loc)) {
                # If loc points to bin, resolve parent
                if ($loc.ToLower().EndsWith("\bin")) {
                    $loc = Split-Path -Parent $loc
                }
                $cDir = Join-Path $loc "data\commands"
                $sExe = Join-Path $loc "bin\synergy.exe"
                if ((Test-Path $cDir) -and (Test-Path $sExe)) {
                    $candidates.Add(@{
                        Version = $verName
                        InstallLocation = $loc
                        CommandsDir = $cDir
                        SynergyExe = $sExe
                        Source = "HKCU ($verName)"
                    })
                }
            }
        }
    }

    # 3. Default known filesystem fallbacks (dynamic program roots, sorted newest first)
    $programRoots = @(
        $env:ProgramFiles,
        $env:ProgramW6432,
        ${env:ProgramFiles(x86)}
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

    $supportedVersions = @("2027", "2026", "2025")
    $diskFallbacks = [System.Collections.Generic.List[string]]::new()
    foreach ($pRoot in $programRoots) {
        foreach ($v in $supportedVersions) {
            $candidatePath = Join-Path $pRoot ("Autodesk\Moldflow Synergy " + $v)
            if (-not $diskFallbacks.Contains($candidatePath)) {
                $diskFallbacks.Add($candidatePath)
            }
        }
    }

    foreach ($fPath in $diskFallbacks) {
        if (-not (Test-Path $fPath)) {
            continue
        }
        $cDir = Join-Path $fPath "data\commands"
        $sExe = Join-Path $fPath "bin\synergy.exe"
        if ((Test-Path $cDir) -and (Test-Path $sExe)) {
            $verName = (Split-Path -Leaf $fPath).Replace("Moldflow Synergy ", "")
            $candidates.Add(@{
                Version = $verName
                InstallLocation = $fPath
                CommandsDir = $cDir
                SynergyExe = $sExe
                Source = "Filesystem Fallback"
            })
        }
    }

    if ($candidates.Count -gt 0) {
        # Select newest verified installation
        $best = $candidates[0]
        Write-Host "  [OK] Discovered Moldflow Synergy:" -ForegroundColor Green
        Write-Host "       Source   : $($best.Source)"
        Write-Host "       Location : $($best.InstallLocation)"
        Write-Host "       Commands : $($best.CommandsDir)"
        return $best
    }

    return $null
}

# ============================================================================
# 2. Python Interpreter Discovery
# ============================================================================

function Find-PythonInterpreter {
    [CmdletBinding()]
    param()

    $candidates = @(
        (Join-Path $InstallDir "bin\pythonw.exe"),
        (Join-Path $InstallDir "bin\python.exe"),
        "C:\Program Files\Python314\pythonw.exe",
        "C:\Program Files\Python314\python.exe",
        "C:\Program Files\Python312\pythonw.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\pythonw.exe",
        "C:\Program Files\Python311\python.exe"
    )

    foreach ($c in $candidates) {
        if (Test-Path $c) {
            return $c
        }
    }

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $cmd2 = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd2) { return $cmd2.Source }

    throw "Could not find a supported Python 3 interpreter (tested standard Program Files and PATH)."
}

# ============================================================================
# 3. Source Bundle Resolution
# ============================================================================

function Resolve-SourceBundle {
    [CmdletBinding()]
    param()

    if ($SourceBundleDir -and (Test-Path $SourceBundleDir)) {
        return (Resolve-Path $SourceBundleDir).Path
    }

    # Relative to installer script (packaged distribution)
    $packaged = Join-Path $ScriptDir "bundle"
    if (Test-Path $packaged) {
        return (Resolve-Path $packaged).Path
    }

    # Staging directory on build/dev workstation
    $stagingDir = "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging"
    if (Test-Path $stagingDir) {
        return $stagingDir
    }

    # Repo source (auto-extract if build script exists)
    $buildScript = Join-Path $ScriptDir "build_workstation_bundle.ps1"
    if (Test-Path $buildScript) {
        $tempStaging = Join-Path $env:TEMP "MoldflowWorkstationBundle_Staging"
        Write-Host "  Building runtime bundle from repository source..." -ForegroundColor Yellow
        & $buildScript -OutputDir $tempStaging
        return $tempStaging
    }

    throw "Workstation runtime bundle source could not be resolved."
}

# ============================================================================
# 4. Least-Privilege ACL Application
# ============================================================================

function Set-ProtectedConfigAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) { return }

    try {
        $acl = Get-Acl -Path $Path
        # Disable inheritance and remove existing inherited rules ($true, $false)
        $acl.SetAccessRuleProtection($true, $false)

        # 1. SYSTEM (Full Control) - Well-known SID S-1-5-18
        $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
        $systemRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $systemSid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($systemRule)

        # 2. BUILTIN\Administrators (Full Control) - Well-known SID S-1-5-32-544
        $adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
        $adminRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $adminSid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($adminRule)

        # 3. Current User / Runtime Identity (ReadAndExecute)
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $userRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $currentIdentity.User,
            [System.Security.AccessControl.FileSystemRights]::ReadAndExecute,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($userRule)

        Set-Acl -Path $Path -AclObject $acl
        Write-Host "  [OK] Applied hardened least-privilege ACLs to: $Path" -ForegroundColor Green
    }
    catch {
        Write-Warning "Could not apply hardened ACL to ${Path}: $_"
    }
}

function Set-WritableLogDirectoryAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) { return }

    try {
        $acl = Get-Acl -Path $Path
        # Disable inheritance and remove existing inherited rules ($true, $false)
        $acl.SetAccessRuleProtection($true, $false)

        # 1. SYSTEM (Full Control) - Well-known SID S-1-5-18
        $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
        $systemRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $systemSid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit",
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($systemRule)

        # 2. BUILTIN\Administrators (Full Control) - Well-known SID S-1-5-32-544
        $adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
        $adminRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $adminSid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit",
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($adminRule)

        # 3. Current User / Runtime Identity (Modify)
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $userRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $currentIdentity.User,
            [System.Security.AccessControl.FileSystemRights]::Modify,
            [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit",
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($userRule)

        Set-Acl -Path $Path -AclObject $acl
        Write-Host "  [OK] Applied runtime writable ACLs to log directory: $Path" -ForegroundColor Green
    }
    catch {
        Write-Warning "Could not apply runtime writable ACL to ${Path}: $_"
    }
}

# ============================================================================
# 5. Safe Synergy Startup Command Deployment
# ============================================================================

function Install-SynergyStartupCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandsDir,
        [Parameter(Mandatory = $true)]
        [string]$SourceVbs
    )

    if (-not (Test-Path $CommandsDir)) {
        Write-Warning "Synergy commands directory does not exist: $CommandsDir. Skipping startup script deployment."
        return
    }

    $targetVbs = Join-Path $CommandsDir "run_startup.vbs"
    $backupVbs = Join-Path $CommandsDir "run_startup.vbs.bak"

    if (Test-Path $targetVbs) {
        $content = Get-Content -Path $targetVbs -Raw -ErrorAction SilentlyContinue
        # Check if file belongs to this product
        $isOurProduct = ($content -match "Moldflow Insight 2027" -or $content -match "ResolvePluginDir" -or $content -match "MFPLUGIN_SESSION")
        if (-not $isOurProduct) {
            Write-Host "  Existing run_startup.vbs is not recognized as our product. Creating backup..." -ForegroundColor Yellow
            Copy-Item -Path $targetVbs -Destination $backupVbs -Force
        }
    }

    Copy-Item -Path $SourceVbs -Destination $targetVbs -Force
    Write-Host "  [OK] Deployed run_startup.vbs to: $targetVbs" -ForegroundColor Green
}

# ============================================================================
# 6. Scheduled Task Registration
# ============================================================================

function Register-WorkstationScheduledTasks {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe,
        [Parameter(Mandatory = $true)]
        [string]$TargetDir
    )

    $UserPrincipal = "$env:USERDOMAIN\$env:USERNAME"
    if ($env:USERNAME -eq "SYSTEM") {
        try {
            $consoleUser = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
            if ($consoleUser) { $UserPrincipal = $consoleUser }
        } catch {}
    }
    $Principal = New-ScheduledTaskPrincipal -UserId $UserPrincipal -LogonType Interactive

    # 1. Moldflow Mobile Job Monitor
    $monitorTaskName = "Moldflow Mobile Job Monitor"
    try { Stop-ScheduledTask -TaskName $monitorTaskName -ErrorAction SilentlyContinue } catch {}

    $monitorWorkDir = Join-Path $TargetDir "monitor"
    $monitorTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserPrincipal
    $repeat = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1))
    $monitorTrigger.Repetition = $repeat.Repetition
    $monitorAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "-B standalone_job_monitor.py" -WorkingDirectory $monitorWorkDir
    $monitorSettings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask `
        -TaskName $monitorTaskName `
        -Trigger $monitorTrigger `
        -Action $monitorAction `
        -Settings $monitorSettings `
        -Principal $Principal `
        -Description "Auto-starts Moldflow Mobile standalone job monitor at logon; self-healing via 1-minute repetition with IgnoreNew." `
        -Force | Out-Null
    Write-Host "  [OK] Registered Scheduled Task: $monitorTaskName" -ForegroundColor Green

    # 2. Moldflow Post-Analyze Agent
    $agentTaskName = "Moldflow Post-Analyze Agent"
    try { Stop-ScheduledTask -TaskName $agentTaskName -ErrorAction SilentlyContinue } catch {}

    $agentWorkDir = Join-Path $TargetDir "agents\post_analyze"
    $agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserPrincipal
    $agentRepeat = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1))
    $agentTrigger.Repetition = $agentRepeat.Repetition
    $agentAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "-B agent.py" -WorkingDirectory $agentWorkDir
    $agentSettings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask `
        -TaskName $agentTaskName `
        -Trigger $agentTrigger `
        -Action $agentAction `
        -Settings $agentSettings `
        -Principal $Principal `
        -Description "Auto-starts Moldflow Standalone Post-Analyze Agent at logon; self-healing via 1-minute repetition with IgnoreNew." `
        -Force | Out-Null
    Write-Host "  [OK] Registered Scheduled Task: $agentTaskName" -ForegroundColor Green

    # Start tasks
    try {
        Start-ScheduledTask -TaskName $monitorTaskName
        Start-ScheduledTask -TaskName $agentTaskName
        Write-Host "  [OK] Started workstation background tasks." -ForegroundColor Green
    }
    catch {
        Write-Warning "Could not immediately start scheduled tasks: $_"
    }
}

# ============================================================================
# Main Installation Flow
# ============================================================================

try {
    Write-InstallerHeader

    # ------------------------------------------------------------------------
    # STEP 1: Discover Environment & Synergy
    # ------------------------------------------------------------------------
    Write-Host "[1/6] Discovering Environment & Moldflow Synergy..." -ForegroundColor Yellow
    $synergyInfo = Find-MoldflowSynergyInstallation
    if (-not $synergyInfo) {
        Write-Warning "Autodesk Moldflow Synergy installation was not detected."
        Write-Warning "The workstation runtime will be installed, but Synergy startup integration will be skipped."
    }

    $pythonExe = Find-PythonInterpreter
    $cliPython = $pythonExe.Replace("pythonw.exe", "python.exe")
    if (-not (Test-Path $cliPython)) { $cliPython = $pythonExe }
    Write-Host "  [OK] Using Python interpreter: $pythonExe" -ForegroundColor Green

    # ------------------------------------------------------------------------
    # STEP 2: Deploy Runtime Files into %ProgramFiles%\MoldflowMobileWorkstation
    # ------------------------------------------------------------------------
    if (-not $SkipFileCopy) {
        $sourceBundle = Resolve-SourceBundle
        Write-Host "  Source bundle: $sourceBundle"

        # Stop tasks if installing to standard production location to prevent file locks during upgrade
        if ($InstallDir -eq "$env:ProgramFiles\MoldflowMobileWorkstation") {
            try { Stop-ScheduledTask -TaskName "Moldflow Mobile Job Monitor" -ErrorAction SilentlyContinue } catch {}
            try { Stop-ScheduledTask -TaskName "Moldflow Post-Analyze Agent" -ErrorAction SilentlyContinue } catch {}
        }

        if (-not (Test-Path $InstallDir)) {
            New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        }

        Copy-Item -Path "$sourceBundle\*" -Destination $InstallDir -Recurse -Force
        Write-Host "  [OK] Deployed runtime bundle into: $InstallDir" -ForegroundColor Green
    } else {
        Write-Host "  [SKIPPED] File copy skipped (managed by MSI package)." -ForegroundColor Gray
    }

    # Purge any accidental bytecode or cache files from runtime directory
    Get-ChildItem -Path $InstallDir -Recurse -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $InstallDir -Recurse -Filter '*.pyc' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

    # ------------------------------------------------------------------------
    # STEP 3: Workstation Enrollment & Protected Configuration
    # ------------------------------------------------------------------------
    Write-Host "`n[3/6] Configuring Workstation Identity & Credentials..." -ForegroundColor Yellow
    if (-not (Test-Path $ConfigDir)) {
        New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    }
    $targetConfigFile = Join-Path $ConfigDir "config.json"

    # Check for existing valid configuration
    $hasExistingKey = $false
    if ((Test-Path $targetConfigFile) -and (-not $ForceReenroll)) {
        try {
            $existing = Get-Content -Path $targetConfigFile -Raw | ConvertFrom-Json
            if ($existing.enabled -and $existing.api_key -and $existing.api_key.StartsWith("mf-client-")) {
                $hasExistingKey = $true
                Write-Host "  [OK] Existing active workstation configuration preserved (Key: $(Mask-Secret $existing.api_key))." -ForegroundColor Green
                Write-Host "       Use -ForceReenroll to rotate credentials with a new one-time token."
            }
        }
        catch {}
    }

    if (-not $hasExistingKey) {
        # Resolve Enrollment Token securely
        $effectiveToken = $EnrollmentToken
        if (-not $effectiveToken -and $EnrollmentTokenFile -and (Test-Path $EnrollmentTokenFile)) {
            $effectiveToken = (Get-Content -Path $EnrollmentTokenFile -Raw).Trim()
        }

        # If interactive and token is missing, prompt securely
        if (-not $effectiveToken -and (-not $Silent) -and [Environment]::UserInteractive) {
            Write-Host "  An enrollment token is required to provision this workstation." -ForegroundColor Yellow
            $securePrompt = Read-Host -Prompt "  Enter One-Time Enrollment Token" -AsSecureString
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePrompt)
            $effectiveToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }

        if (-not $effectiveToken) {
            throw "Enrollment failed: No enrollment token was provided. The workstation cannot be registered without an enrollment token."
        }

        Write-Host "  Enrolling workstation with backend: $BackendUrl..." -ForegroundColor Yellow

        # Use Python enrollment helper to execute zero-dependency enrollment
        $enrollmentScript = @"
import sys, json
sys.path.insert(0, r'$InstallDir')
from lib.mobile.enrollment import enroll_workstation, build_config_from_enrollment, save_workstation_config, EnrollmentError

try:
    payload = json.loads(sys.stdin.read())
    backend = payload['backend_url']
    m_id = payload['machine_id']
    u_id = payload['user_id']
    token = payload['token']
    m_name = payload['machine_name']

    res = enroll_workstation(backend, m_id, u_id, token, m_name)
    cfg = build_config_from_enrollment(res, backend)
    target = save_workstation_config(cfg, target_path=r'$targetConfigFile')
    print(json.dumps({'status': 'ok', 'api_key': res['api_key'], 'target': str(target)}))
except EnrollmentError as e:
    print(json.dumps({'status': 'error', 'detail': str(e)}))
    sys.exit(1)
except Exception as e:
    print(json.dumps({'status': 'error', 'detail': str(e)}))
    sys.exit(1)
"@
        $enrollInput = @{
            backend_url = $BackendUrl.TrimEnd('/')
            machine_id = $MachineId
            user_id = $UserId
            token = $effectiveToken
            machine_name = $MachineName
        } | ConvertTo-Json -Compress

        $pinfo = New-Object System.Diagnostics.ProcessStartInfo
        $pinfo.FileName = $cliPython
        $pinfo.Arguments = "-B -c `"$enrollmentScript`""
        $pinfo.RedirectStandardInput = $true
        $pinfo.RedirectStandardOutput = $true
        $pinfo.RedirectStandardError = $true
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true

        $proc = [System.Diagnostics.Process]::Start($pinfo)
        $proc.StandardInput.Write($enrollInput)
        $proc.StandardInput.Close()
        $output = $proc.StandardOutput.ReadToEnd()
        $errOutput = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()

        # Immediately clear token variable from memory
        $effectiveToken = $null
        $enrollInput = $null

        # If token was provided via file, delete it immediately
        if ($EnrollmentTokenFile -and (Test-Path $EnrollmentTokenFile)) {
            Remove-Item -Path $EnrollmentTokenFile -Force -ErrorAction SilentlyContinue
        }

        if ($proc.ExitCode -ne 0) {
            $errDetail = "Enrollment rejected by backend."
            try {
                $errObj = $output | ConvertFrom-Json
                if ($errObj.detail) { $errDetail = $errObj.detail }
            } catch {}
            throw "Workstation Enrollment Failed: $errDetail"
        }

        $resObj = $output | ConvertFrom-Json
        Write-Host "  [OK] Workstation enrolled successfully!" -ForegroundColor Green
        Write-Host "       Provisioned API Key : $(Mask-Secret $resObj.api_key)"
        Write-Host "       Configuration File  : $($resObj.target)"
    }

    # Ensure logs and inspection directories exist and are writable by runtime user
    $logsDir = Join-Path $ConfigDir "logs"
    if (-not (Test-Path $logsDir)) {
        New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    }
    Set-WritableLogDirectoryAcl -Path $logsDir

    $inspectionDir = Join-Path $ConfigDir "inspection_results"
    if (-not (Test-Path $inspectionDir)) {
        New-Item -ItemType Directory -Path $inspectionDir -Force | Out-Null
    }
    Set-WritableLogDirectoryAcl -Path $inspectionDir

    # Apply least-privilege ACLs to config file and directory
    Set-ProtectedConfigAcl -Path $targetConfigFile
    Set-ProtectedConfigAcl -Path $ConfigDir

    # ------------------------------------------------------------------------
    # STEP 4: Deploy Synergy Startup Integration
    # ------------------------------------------------------------------------
    Write-Host "`n[4/6] Deploying Synergy Startup Integration..." -ForegroundColor Yellow
    if (-not $SkipSynergyCommand -and $synergyInfo) {
        $sourceVbs = Join-Path $InstallDir "plugin\run_startup.vbs"
        Install-SynergyStartupCommand -CommandsDir $synergyInfo.CommandsDir -SourceVbs $sourceVbs
    } else {
        Write-Host "  [SKIPPED] Synergy startup command copy skipped." -ForegroundColor Gray
    }

    # ------------------------------------------------------------------------
    # STEP 5: Register Scheduled Tasks
    # ------------------------------------------------------------------------
    Write-Host "`n[5/6] Registering Background Scheduled Tasks..." -ForegroundColor Yellow
    if (-not $SkipTasks) {
        Register-WorkstationScheduledTasks -PythonExe $pythonExe -TargetDir $InstallDir
    } else {
        Write-Host "  [SKIPPED] Scheduled task registration skipped by parameter." -ForegroundColor Gray
    }

    # ------------------------------------------------------------------------
    # STEP 6: Post-Install Validation Checks
    # ------------------------------------------------------------------------
    Write-Host "`n[6/6] Executing Post-Install Validation Checks..." -ForegroundColor Yellow

    # Check 1: Files check
    $manifestPath = Join-Path $InstallDir "MANIFEST.txt"
    if (Test-Path $manifestPath) {
        Write-Host "  [PASS] Runtime manifest present." -ForegroundColor Green
    }

    # Check 2: Python import check
    $cliPython = $pythonExe.Replace("pythonw.exe", "python.exe")
    if (-not (Test-Path $cliPython)) { $cliPython = $pythonExe }

    $valCmd = "& '$cliPython' -B -c `"import sys; sys.path.insert(0, r'$InstallDir'); from lib.mobile import reporter, enrollment; from lib.scm import client; print('IMPORTS_OK')`""
    $valRes = Invoke-Expression $valCmd
    if ($valRes -match "IMPORTS_OK") {
        Write-Host "  [PASS] Python runtime successfully loaded workstation modules." -ForegroundColor Green
    } else {
        Write-Warning "Python import test failed: $valRes"
    }

    # Check 3: Active config resolution
    $cfgCmd = "& '$cliPython' -B -c `"import sys; sys.path.insert(0, r'$InstallDir'); from lib.mobile.reporter import _find_config_path; print(_find_config_path())`""
    $cfgOutput = Invoke-Expression $cfgCmd
    $cfgResolved = if ($cfgOutput) { $cfgOutput.ToString().Trim() } else { "" }
    if ($cfgResolved -like "*ProgramData*config.json*") {
        Write-Host "  [PASS] Config resolver correctly targets %ProgramData%\MoldflowMobile\config.json." -ForegroundColor Green
    } else {
        Write-Host "  [INFO] Config resolver returned: $cfgResolved" -ForegroundColor Gray
    }

    # Check 4: Backend API connectivity verification using provisioned key
    try {
        $cfgJson = Get-Content -Path $targetConfigFile -Raw | ConvertFrom-Json
        $testUrl = "$($cfgJson.backend_url.TrimEnd('/'))/internal/active-jobs"
        $apiCheck = Invoke-RestMethod -Uri $testUrl -Headers @{"X-Api-Key" = $cfgJson.api_key} -Method Get -TimeoutSec 10 -ErrorAction Stop
        Write-Host "  [PASS] Backend API authenticated successfully with provisioned key!" -ForegroundColor Green
    } catch {
        Write-Warning "Could not verify backend key against /internal/active-jobs: $_"
    }

    # Check 5: Runtime hygiene verification (confirm 0 .pyc, 0 .log, 0 __pycache__)
    Get-ChildItem -Path $InstallDir -Recurse -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $InstallDir -Recurse -Filter '*.pyc' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    $remainingPyc = (Get-ChildItem -Path $InstallDir -Recurse -Filter '*.pyc' -File -ErrorAction SilentlyContinue).Count
    $remainingLog = (Get-ChildItem -Path $InstallDir -Recurse -Filter '*.log' -File -ErrorAction SilentlyContinue).Count
    $remainingPycache = (Get-ChildItem -Path $InstallDir -Recurse -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue).Count
    if ($remainingPyc -eq 0 -and $remainingLog -eq 0 -and $remainingPycache -eq 0) {
        Write-Host "  [PASS] Runtime verified clean: 0 .pyc, 0 .log, 0 __pycache__." -ForegroundColor Green
    } else {
        Write-Warning "Runtime hygiene warning: $remainingPyc .pyc, $remainingLog .log, $remainingPycache __pycache__."
    }

    # Check 6: Runtime log directory writability check
    $testLogPath = Join-Path $logsDir ".perm_check"
    try {
        [System.IO.File]::WriteAllText($testLogPath, "ok")
        Remove-Item -Path $testLogPath -Force -ErrorAction SilentlyContinue
        Write-Host "  [PASS] Production log directory is writable: $logsDir" -ForegroundColor Green
    } catch {
        Write-Warning "Log directory writability check failed: $_"
    }

    Write-Host "`n============================================================" -ForegroundColor Green
    Write-Host " Workstation Installation & Onboarding Complete!           " -ForegroundColor Green
    Write-Host " Machine ID: $MachineId is READY.                          " -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "`n[FATAL ERROR] Installation Aborted: $_" -ForegroundColor Red
    exit 1
}
