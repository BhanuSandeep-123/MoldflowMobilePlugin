# setup.ps1 - one-time environment setup for the Moldflow Synergy Python plugin.
# Creates a .venv using Moldflow's bundled 64-bit Python 3.14 and installs comtypes.
#
# Usage (from this folder):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Moldflow's bundled interpreter is guaranteed 64-bit and matches the COM server.
$basePython = "C:\Program Files\Autodesk\Moldflow Insight 2027\bin\python.exe"
if (-not (Test-Path $basePython)) {
    # Fall back to any python on PATH (must be 64-bit).
    $basePython = "python"
}

Write-Host "Creating virtual environment (.venv)..."
& $basePython -m venv "$here\.venv"

$venvPy = "$here\.venv\Scripts\python.exe"
Write-Host "Upgrading pip and installing dependencies..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r "$here\requirements.txt" --quiet

$synergyWheel = "C:\Program Files\Autodesk\Moldflow Synergy 2027\runtime\python\wheelhouse\moldflow-27.0.0-py3-none-any.whl"
if (Test-Path $synergyWheel) {
    Write-Host "Installing Moldflow Synergy python library..."
    & $venvPy -m pip install $synergyWheel --quiet
} else {
    Write-Host "WARNING: Moldflow Synergy python library wheel not found at: $synergyWheel"
}

Write-Host ""
Write-Host "Done. Test the connection with:"
Write-Host "  .\.venv\Scripts\python.exe synergy_connect.py   (Synergy must be running)"
Write-Host "Run the sample plugin with:"
Write-Host "  .\.venv\Scripts\python.exe plugin_example.py"
