# deployment/register_license_monitor_task.ps1
# Registers and starts the Moldflow Network License Monitor Scheduled Task.

param(
    [string]$TaskName = "Moldflow Network License Monitor"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$XmlPath = Join-Path $ScriptDir "scheduled_task_license_monitor.xml"

if (-not (Test-Path $XmlPath)) {
    Write-Error "Scheduled task XML not found: $XmlPath"
}

Write-Host "Stopping any running instances of scheduled task '$TaskName'..."
try {
    Stop-ScheduledTask -TaskName "$TaskName" -ErrorAction SilentlyContinue
} catch {}

Write-Host "Stopping any existing monitor processes..."
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*license_monitor.monitor*" } | ForEach-Object {
    Write-Host "Stopping obsolete monitor process (PID: $($_.ProcessId))..."
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "Registering scheduled task '$TaskName' from $XmlPath..."
schtasks /Create /XML "$XmlPath" /TN "$TaskName" /F

Write-Host "Starting scheduled task '$TaskName'..."
Start-ScheduledTask -TaskName "$TaskName"

Start-Sleep -Seconds 3
$task = Get-ScheduledTask -TaskName "$TaskName"
$TaskState = $task.State
$Hidden = $task.Settings.Hidden
Write-Host "Task '$TaskName' state: $TaskState | Hidden: $Hidden"
