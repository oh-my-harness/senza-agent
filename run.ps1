<#
.SYNOPSIS
    Run senza-agent from source on Windows — no build, no Node.js, no NSIS installer.

.DESCRIPTION
    Creates a lightweight venv, pip-installs dependencies (senza-sdk + aiohttp),
    then launches the senza-agent CLI / web dashboard directly from source.

    Prerequisites: Python 3.9+ (python.exe on PATH or detected via py launcher).
    No Git, Node.js, or Electron needed.

.PARAMETER Web
    Launch the web dashboard instead of the interactive CLI.
    You can optionally specify a port:  -Web 8090

.PARAMETER Port
    Port for the web dashboard (default 8090).  Ignored unless -Web is given.

.PARAMETER Task
    Run a single task non-interactively, then exit.
    Example:  -Task "list files in the current directory"

.PARAMETER NoVenv
    Skip venv creation — use the system Python directly.  Not recommended.

.PARAMETER NoMirror
    Don't use the China pip mirror (pypi.tuna.tsinghua.edu.cn).

.PARAMETER Python
    Path to a specific python.exe.  Overrides auto-detection.

.EXAMPLE
    .\run.ps1                  # interactive CLI
    .\run.ps1 -Web             # web dashboard on port 8090
    .\run.ps1 -Web 9000        # web dashboard on port 9000
    .\run.ps1 -Task "hello"    # single task, then exit

.NOTES
    The script auto-detects the repo root (the directory containing this file
    and pyproject.toml).  Run it from anywhere — it will find the source.
#>
param(
    [switch]$Web,
    [int]$Port = 8090,
    [string]$Task = "",
    [switch]$NoVenv,
    [switch]$NoMirror,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# ── Helpers ──────────────────────────────────────────────────────────

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Err([string]$msg)  { Write-Host "    ERR $msg" -ForegroundColor Red }
function Write-Warn([string]$msg) { Write-Host "    !   $msg" -ForegroundColor Yellow }

# ── 1. Locate repo root ──────────────────────────────────────────────

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = $PWD.Path }
$ScriptDir = (Resolve-Path $ScriptDir).Path

if (-not (Test-Path (Join-Path $ScriptDir "pyproject.toml"))) {
    Write-Err "pyproject.toml not found next to this script."
    Write-Host "    Put run.ps1 in the repo root (next to pyproject.toml)."
    exit 1
}
$RepoRoot = $ScriptDir
Write-OK "Repo root: $RepoRoot"

# ── 2. Find Python ───────────────────────────────────────────────────

$PyExe = ""

if ($Python) {
    if (Test-Path $Python) {
        $PyExe = (Resolve-Path $Python).Path
    } else {
        $cmd = Get-Command $Python -ErrorAction SilentlyContinue
        if ($cmd) { $PyExe = $cmd.Source }
    }
    if (-not $PyExe) {
        Write-Err "Python not found at: $Python"
        exit 1
    }
} else {
    # Try plain 'python' first, then 'py -3', then 'python3'.
    foreach ($candidate in @("python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $PyExe = $cmd.Source; break }
    }
    if (-not $PyExe) {
        try {
            $pyOut = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $pyOut) { $PyExe = $pyOut.Trim() }
        } catch { }
    }
}

if (-not $PyExe) {
    Write-Err "Python not found. Install Python 3.9+ from https://python.org"
    Write-Host "    Or pass -Python C:\path\to\python.exe"
    exit 1
}

$PyVer = & $PyExe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1
Write-OK "Python $PyVer at $PyExe"

# Check version >= 3.9
$verOK = & $PyExe -c "import sys; print(1 if sys.version_info >= (3,9) else 0)" 2>&1
if ($verOK -ne "1") {
    Write-Err "Python 3.9+ required, got $PyVer"
    exit 1
}

# ── 3. Create / reuse venv ───────────────────────────────────────────

$VenvDir = Join-Path $RepoRoot ".venv"
$PyInVenv = Join-Path $VenvDir "Scripts\python.exe"

if ($NoVenv) {
    Write-Warn "-NoVenv: using system Python directly (dependencies installed globally)."
    $RunPy = $PyExe
} else {
    if (-not (Test-Path $PyInVenv)) {
        Write-Step "Creating virtual environment (.venv) ..."
        & $PyExe -m venv $VenvDir
        if (-not (Test-Path $PyInVenv)) {
            Write-Err "venv creation failed."
            exit 1
        }
        Write-OK "venv created at $VenvDir"
    } else {
        Write-OK "venv already exists at $VenvDir"
    }
    $RunPy = $PyInVenv
}

# ── 4. Install dependencies ──────────────────────────────────────────

# Always check / install — pip is fast if everything is already satisfied.
Write-Step "Checking dependencies ..."

$PipArgs = @("install", "-q")
if (-not $NoMirror) {
    $PipArgs += "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"
}
# Install the package itself in editable mode + its declared dependencies.
$PipArgs += "-e", $RepoRoot

& $RunPy -m pip @PipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Warn "pip install -e failed, trying plain install ..."
    $PipArgs2 = @("install", "-q")
    if (-not $NoMirror) {
        $PipArgs2 += "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"
    }
    $PipArgs2 += "senza-sdk>=1.2.3", "aiohttp"
    & $RunPy -m pip @PipArgs2
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Dependency installation failed."
        Write-Host "    Try manually:  $RunPy -m pip install senza-sdk aiohttp"
        exit 1
    }
}
Write-OK "Dependencies ready"

# ── 5. Launch senza-agent ────────────────────────────────────────────

Write-Step "Launching senza-agent ..."

$CliArgs = @("-m", "senza_agent.cli")

if ($Task) {
    $CliArgs += "--nostop", $Task
    Write-OK "Mode: single task"
} elseif ($Web) {
    $CliArgs += "--web", $Port
    Write-OK "Mode: web dashboard (http://localhost:$Port)"
} else {
    $CliArgs += "--nostop"
    Write-OK "Mode: interactive CLI"
}

Write-Host ""
& $RunPy @CliArgs
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-OK "senza-agent exited normally."
} else {
    Write-Warn "senza-agent exited with code $exitCode"
}
exit $exitCode
