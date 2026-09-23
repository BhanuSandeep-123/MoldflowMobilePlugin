# deployment/register_job_monitor_task.ps1
# Registers and starts the Moldflow Mobile Job Monitor Scheduled Task dynamically.

[CmdletBinding()]
param(
    [string]$TaskName = "Moldflow Mobile Job Monitor",
    [string]$PythonExe = "",
    [string]$WorkingDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

# 1. Resolve Working Directory (default: <repo>\monitor)
if (-not $WorkingDir) {
    $WorkingDir = Join-Path $RepoRoot "monitor"
}
if (-not (Test-Path $WorkingDir)) {
    Write-Error "Working directory does not exist: $WorkingDir"
}

# 2. Resolve Python interpreter
if (-not $PythonExe) {
    $candidateVenv = Join-Path $RepoRoot "plugin\.venv\Scripts\pythonw.exe"
    $candidateSys = "C:\Program Files\Python314\pythonw.exe"
    if (Test-Path $candidateSys) {
        $PythonExe = $candidateSys
    } elseif (Test-Path $candidateVenv) {
        $PythonExe = $candidateVenv
    } else {
        $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $PythonExe = $cmd.Source
        } else {
            Write-Error "Could not find a valid pythonw.exe interpreter."
        }
    }
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Registering Moldflow Mobile Job Monitor Scheduled Task     " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Task Name     : $TaskName"
Write-Host " Python Binary : $PythonExe"
Write-Host " Working Dir   : $WorkingDir"
Write-Host " User Principal: $env:USERDOMAIN\$env:USERNAME"

# 3. Stop running instance if already active
try {
    Stop-ScheduledTask -TaskName "$TaskName" -ErrorAction SilentlyContinue
} catch {}

# 4. Register Scheduled Task using dynamic current user principal
$UserPrincipal = "$env:USERDOMAIN\$env:USERNAME"
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserPrincipal
$Repeat = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1))
$Trigger.Repetition = $Repeat.Repetition

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "standalone_job_monitor.py" -WorkingDirectory $WorkingDir
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal -UserId $UserPrincipal -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Auto-starts standalone_job_monitor.py at logon; self-heals via 1-minute repetition + IgnoreNew (relaunches only if not already running)." `
    -Force | Out-Null

Write-Host "Starting scheduled task '$TaskName'..."
Start-ScheduledTask -TaskName "$TaskName"

Start-Sleep -Seconds 2
$TaskState = (Get-ScheduledTask -TaskName "$TaskName").State
Write-Host "Task '$TaskName' state: $TaskState" -ForegroundColor Green
