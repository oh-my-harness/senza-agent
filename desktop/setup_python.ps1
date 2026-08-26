<#
.SYNOPSIS
    Sets up a Python virtual environment for senza-agent on Windows.

.DESCRIPTION
    Called by the NSIS installer (or manually) after files are extracted.
    Creates a venv at <InstallDir>\python_venv and pip-installs the project
    dependencies from requirements.txt.

    If the venv already exists (upgrade install), it is reused.

.PARAMETER InstallDir
    The installation directory. Defaults to the script's parent directory.
#>
param(
    [string]$InstallDir = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = "Stop"

Write-Host "=== senza-agent Python setup ===" -ForegroundColor Cyan
Write-Host "InstallDir: $InstallDir"

$RequirementsFile = Join-Path $InstallDir "resources\requirements.txt"
$VenvDir          = Join-Path $InstallDir "python_venv"
$VenvPython       = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip          = Join-Path $VenvDir "Scripts\pip.exe"

# ── 1. Locate system Python ──────────────────────────────────────────────
# Prefer py launcher (ships with official Python installer), then python.
$PythonExe = $null

$pyCmd = Get-Command "py" -ErrorAction SilentlyContinue
if ($pyCmd) {
    # Use py to find the latest 3.x installed.
    $pyVersion = & py --list 2>$null | Select-Object -First 1
    if ($pyVersion) {
        # py --list outputs " -V:3.12 *" etc. Extract the version tag.
        if ($pyVersion -match 'V:(\d+\.\d+)') {
            $PythonExe = "py -$($Matches[1])"
        }
    }
    if (-not $PythonExe) {
        $PythonExe = "py"
    }
} else {
    $pythonCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $PythonExe = "python"
    }
}

if (-not $PythonExe) {
    Write-Host @"
[ERROR] Python is not installed or not on PATH.
Please install Python 3.9+ from https://www.python.org/downloads/
and re-run this script, or re-launch SenzaAgent after installing Python.
"@ -ForegroundColor Red
    exit 1
}

Write-Host "Using Python: $PythonExe"

# Get version: handle "py -3.x" (two tokens) vs "python" (one token)
if ($PythonExe -match '^py -(\S+)') {
    $versionOutput = & py "-$($Matches[1])" --version 2>&1
} else {
    $versionOutput = & $PythonExe --version 2>&1
}
Write-Host "Python version: $versionOutput"

# ── 2. Create venv if it doesn't exist ───────────────────────────────────
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment at $VenvDir ..." -ForegroundColor Yellow
    if ($PythonExe -match '^py -(\S+)') {
        & py "-$($Matches[1])" -m venv $VenvDir
    } else {
        & $PythonExe -m venv $VenvDir
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create virtual environment." -ForegroundColor Red
        exit 1
    }
    Write-Host "Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "Virtual environment already exists, reusing." -ForegroundColor DarkGray
}

# ── 3. Upgrade pip ───────────────────────────────────────────────────────
Write-Host "Upgrading pip..." -ForegroundColor Yellow
& $VenvPython -m pip install --quiet --upgrade pip

# ── 4. Install dependencies ──────────────────────────────────────────────
if (Test-Path $RequirementsFile) {
    Write-Host "Installing dependencies from $RequirementsFile ..." -ForegroundColor Yellow
    & $VenvPip install -r $RequirementsFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARNING] Some dependencies may not have installed correctly." -ForegroundColor Yellow
        Write-Host "You can re-run: $VenvPip install -r $RequirementsFile" -ForegroundColor DarkGray
    } else {
        Write-Host "Dependencies installed." -ForegroundColor Green
    }
} else {
    Write-Host "[WARNING] requirements.txt not found at $RequirementsFile" -ForegroundColor Yellow
}

# ── 5. Install senza-agent itself (editable pointing at resources\senza_agent) ──
$ProjectRoot = Join-Path $InstallDir "resources"
$PyProject   = Join-Path $ProjectRoot "pyproject.toml"
if (Test-Path $PyProject) {
    Write-Host "Installing senza-agent package..." -ForegroundColor Yellow
    & $VenvPip install -e $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARNING] senza-agent package install failed. App may not work." -ForegroundColor Yellow
    } else {
        Write-Host "senza-agent installed." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "=== Python setup complete ===" -ForegroundColor Cyan
Write-Host "Venv: $VenvDir"
Write-Host "Python: $VenvPython"
