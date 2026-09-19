<#
.SYNOPSIS
    Automated PostgreSQL database backup script for Moldflow Mobile.

.DESCRIPTION
    Creates a timestamped, compressed PostgreSQL dump archive using pg_dump.
    Automatically rotates and removes backups older than the retention threshold.

.PARAMETER BackupDir
    Directory where backup archives are stored.

.PARAMETER RetentionDays
    Number of days to keep historical backups before pruning (default: 14).
#>

[CmdletBinding()]
param(
    [string]$BackupDir = "C:\MF\MoldflowSynergyPlugin\backups",
    [int]$RetentionDays = 14,
    [string]$PgDumpPath = "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
    [string]$DbHost = "127.0.0.1",
    [int]$DbPort = 5432,
    [string]$DbUser = "moldflow_app",
    [string]$DbName = "moldflow_mobile",
    [string]$DbPassword = "Moldflow123"
)

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
$backupFileName = "moldflow_mobile_backup_$timestamp.dump"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile - Automated Database Backup               " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Ensure backup directory exists
if (-not (Test-Path -Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Write-Host "[1/3] Created backup directory: $BackupDir" -ForegroundColor Green
} else {
    Write-Host "[1/3] Backup directory: $BackupDir" -ForegroundColor Green
}

$backupFilePath = Join-Path -Path $BackupDir -ChildPath $backupFileName

# 2. Check pg_dump.exe
if (-not (Test-Path -Path $PgDumpPath)) {
    Write-Error "pg_dump.exe not found at $PgDumpPath"
    exit 1
}

# 3. Execute pg_dump
Write-Host "[2/3] Exporting database '$DbName' to $backupFileName..." -ForegroundColor Yellow

$env:PGPASSWORD = $DbPassword

$dumpArgs = @(
    "-h", $DbHost,
    "-p", $DbPort.ToString(),
    "-U", $DbUser,
    "-F", "c",
    "-b",
    "-v",
    "-f", $backupFilePath,
    $DbName
)

try {
    $proc = Start-Process -FilePath $PgDumpPath -ArgumentList $dumpArgs -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -eq 0) {
        $fileInfo = Get-Item -Path $backupFilePath
        $sizeKb = [math]::Round($fileInfo.Length / 1KB, 2)
        Write-Host "      [OK] Backup completed successfully! ($sizeKb KB)" -ForegroundColor Green
    } else {
        Write-Error "pg_dump failed with exit code $($proc.ExitCode)"
        exit $proc.ExitCode
    }
}
finally {
    Remove-Item env:PGPASSWORD -ErrorAction SilentlyContinue
}

# 4. Prune old backups
Write-Host "[3/3] Pruning backups older than $RetentionDays days..." -ForegroundColor Yellow
$cutoffDate = (Get-Date).AddDays(-$RetentionDays)
$oldBackups = Get-ChildItem -Path $BackupDir -Filter "moldflow_mobile_backup_*.dump" | Where-Object { $_.LastWriteTime -lt $cutoffDate }

if ($oldBackups) {
    foreach ($old in $oldBackups) {
        Remove-Item -Path $old.FullName -Force
        Write-Host "      Removed old backup: $($old.Name)" -ForegroundColor Gray
    }
} else {
    Write-Host "      No outdated backups to prune." -ForegroundColor Gray
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Backup Finished at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
