# deployment/register_post_analyze_task.ps1
# Registers the Moldflow Post-Analyze Agent Scheduled Task.

param(
    [string]$TaskName = "Moldflow Post-Analyze Agent"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$XmlPath = Join-Path $ScriptDir "scheduled_task_post_analyze.xml"

if (-not (Test-Path $XmlPath)) {
    Write-Error "Scheduled task XML not found: $XmlPath"
}

Write-Host "Registering scheduled task '$TaskName' from $XmlPath..."
schtasks /Create /XML "$XmlPath" /TN "$TaskName" /F

Write-Host "Starting scheduled task '$TaskName'..."
Start-ScheduledTask -TaskName "$TaskName"

Start-Sleep -Seconds 2
$TaskState = (Get-ScheduledTask -TaskName "$TaskName").State
Write-Host "Task '$TaskName' state: $TaskState"
