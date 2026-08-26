<#
.SYNOPSIS
    One-click build script for SenzaAgent Windows installer (NSIS + MSI).

.DESCRIPTION
    Clones (or updates) the senza-agent repo, installs Node.js dependencies,
    and builds the NSIS .exe and MSI .msi installers via electron-builder.

    Run this on a Windows machine with Node.js 18+ and Git installed.

.PARAMETER RepoDir
    Where to clone/update the repo. Default: .\senza-agent

.PARAMETER SkipClone
    Skip git clone/pull — use the repo already at RepoDir (e.g. you cloned it yourself).

.EXAMPLE
    # Default: clone and build
    .\build-windows.ps1

.EXAMPLE
    # Build from an existing repo copy
    .\build-windows.ps1 -SkipClone -RepoDir D:\code\senza-agent
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Join-Path $PWD "senza-agent"),
    [switch]$SkipClone
)

$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/oh-my-harness/senza-agent.git"

function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Write-OK([string]$msg) {
    Write-Host "    OK  $msg" -ForegroundColor Green
}

function Write-Err([string]$msg) {
    Write-Host "    ERR $msg" -ForegroundColor Red
}

function Write-Warn([string]$msg) {
    Write-Host "    !   $msg" -ForegroundColor Yellow
}

# ── 1. Check prerequisites ───────────────────────────────────────────
Write-Step "Checking prerequisites ..."

# Node.js
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
    Write-Err "Node.js not found. Install Node.js 18+ from https://nodejs.org"
    exit 1
}
$nodeVer = & node --version 2>&1
Write-OK "Node.js $nodeVer at $nodeExe"

# npm
$npmExe = (Get-Command npm -ErrorAction SilentlyContinue).Source
if (-not $npmExe) {
    Write-Err "npm not found. Install Node.js 18+ from https://nodejs.org"
    exit 1
}
Write-OK "npm found"

# Git (only needed if not SkipClone)
if (-not $SkipClone) {
    $gitExe = (Get-Command git -ErrorAction SilentlyContinue).Source
    if (-not $gitExe) {
        Write-Err "git not found. Install from https://git-scm.com/download/win"
        exit 1
    }
    Write-OK "git found"
}

# ── 2. Clone or update repo ──────────────────────────────────────────
if ($SkipClone) {
    if (-not (Test-Path (Join-Path $RepoDir "desktop\package.json"))) {
        Write-Err "Repo not found at $RepoDir (no desktop\package.json)."
        Write-Host "    Remove -SkipClone to clone automatically."
        exit 1
    }
    Write-OK "Using existing repo at $RepoDir"
} else {
    Write-Step "Cloning / updating senza-agent ..."
    if (Test-Path (Join-Path $RepoDir ".git")) {
        Write-OK "Repo exists — pulling latest."
        Push-Location $RepoDir
        git pull --quiet origin main 2>$null
        Pop-Location
    } else {
        git clone --quiet $repoUrl $RepoDir 2>&1 | Out-Null
        if (-not (Test-Path (Join-Path $RepoDir ".git"))) {
            Write-Err "Clone failed. Check your network / Git installation."
            exit 1
        }
        Write-OK "Cloned to $RepoDir"
    }
}

# ── 3. Install npm dependencies ──────────────────────────────────────
$DesktopDir = Join-Path $RepoDir "desktop"
Write-Step "Installing npm dependencies (this may take a few minutes) ..."

# Temporarily relax error preference: npm/electron-builder write progress
# and warnings to stderr, which PowerShell treats as fatal under Stop mode.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

Push-Location $DesktopDir
try {
    if (Test-Path "package-lock.json") {
        & cmd /c "npm ci 2>&1" | ForEach-Object {
            if ($_ -match "added|changed|removed|npm warn|npm notice") {
                Write-Host "    $_" -ForegroundColor DarkGray
            }
        }
    } else {
        Write-Warn "package-lock.json not found, using npm install"
        & cmd /c "npm install 2>&1" | ForEach-Object {
            if ($_ -match "added|changed|removed|npm warn|npm notice") {
                Write-Host "    $_" -ForegroundColor DarkGray
            }
        }
    }
    if ($LASTEXITCODE -ne 0) {
        $ErrorActionPreference = $prevEAP
        Write-Err "npm install failed (exit $LASTEXITCODE)."
        Write-Host "    Try: cd $DesktopDir; npm install"
        exit 1
    }
    Write-OK "Dependencies installed"
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
}

# ── 4. Build installers ──────────────────────────────────────────────
Write-Step "Building NSIS + MSI installers (electron-builder) ..."

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

Push-Location $DesktopDir
try {
    & cmd /c "npm run build:win 2>&1" | ForEach-Object {
        Write-Host "    $_" -ForegroundColor DarkGray
    }
    if ($LASTEXITCODE -ne 0) {
        $ErrorActionPreference = $prevEAP
        Write-Err "Build failed (exit $LASTEXITCODE)."
        exit 1
    }
    Write-OK "Build completed"
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
}

# ── 5. Show results ──────────────────────────────────────────────────
$DistDir = Join-Path $DesktopDir "dist"
Write-Step "Build output"

if (Test-Path $DistDir) {
    $files = Get-ChildItem $DistDir -File | Sort-Object Length -Descending
    foreach ($f in $files) {
        $sizeMB = [math]::Round($f.Length / 1MB, 1)
        $tag = switch ($f.Extension) {
            ".exe" { "NSIS installer" }
            ".msi" { "MSI installer" }
            ".zip" { "portable" }
            default { "" }
        }
        Write-Host ("    {0,-45} {1,8} MB  {2}" -f $f.Name, $sizeMB, $tag) -ForegroundColor White
    }
} else {
    Write-Err "Output directory not found: $DistDir"
    exit 1
}

# ── 6. Done ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Output directory:" -ForegroundColor Yellow
Write-Host "  $DistDir" -ForegroundColor White
Write-Host ""
Write-Host "Distribute the .exe file to users." -ForegroundColor Yellow
Write-Host "Users need Python 3.9+ pre-installed." -ForegroundColor Yellow
Write-Host ""
