<#
.SYNOPSIS
    One-click build script for SenzaAgent Windows installer (NSIS).

.DESCRIPTION
    Clones (or updates) the senza-agent repo, installs Node.js dependencies,
    and builds the NSIS .exe installer via electron-builder.

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
#.EXAMPLE
#    # Build without China mirror (if you're outside China or have VPN)
#    .\build-windows.ps1 -NoMirror
#>
param(
    [string]$RepoDir = (Join-Path $PWD "senza-agent"),
    [switch]$SkipClone,
    [switch]$NoMirror
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
        $cloneOk = $false
        git clone --quiet $repoUrl $RepoDir 2>&1 | Out-Null
        if (Test-Path (Join-Path $RepoDir ".git")) {
            $cloneOk = $true
        } elseif (-not $NoMirror) {
            Write-Warn "Direct clone failed, trying mirror ..."
            $mirrorUrl = "https://ghfast.top/$repoUrl"
            git clone --quiet $mirrorUrl $RepoDir 2>&1 | Out-Null
            if (Test-Path (Join-Path $RepoDir ".git")) {
                $cloneOk = $true
            }
        }
        if (-not $cloneOk) {
            Write-Err "Clone failed. Check your network / Git installation."
            exit 1
        }
        Write-OK "Cloned to $RepoDir"
    }
}

# ── 2b. Configure npm mirror (China) ────────────────────────────────
if (-not $NoMirror) {
    Write-Step "Configuring npm mirror (npmmirror.com) ..."
    npm config set registry https://registry.npmmirror.com 2>$null
    # Electron + NSIS download mirrors (electron-builder downloads these
    # at build time, often slow or blocked from China)
    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
    Write-OK "npm registry -> npmmirror.com"
    Write-OK "Electron / NSIS mirror -> npmmirror.com"
}


# ── 3. Install npm dependencies ──────────────────────────────────────
$DesktopDir = (Resolve-Path (Join-Path $RepoDir "desktop")).Path
Write-Step "Installing npm dependencies (this may take a few minutes) ..."

# Start-Process runs npm as a separate process — output streams directly
# to the console in real time, and PowerShell never wraps stderr into
# NativeCommandError (the PS 5.1 trap that breaks under Stop mode).

Push-Location $DesktopDir
try {
    $npmArgs = if (Test-Path "package-lock.json") { "ci" } else { "install" }
    if (-not (Test-Path "package-lock.json")) {
        Write-Warn "package-lock.json not found, using npm install"
    }
    $proc = Start-Process -FilePath "cmd" -ArgumentList "/c npm $npmArgs" `
        -WorkingDirectory $DesktopDir -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Err "npm install failed (exit $($proc.ExitCode))."
        Write-Host "    Try: cd $DesktopDir; npm install"
        exit 1
    }
    Write-OK "Dependencies installed"
} finally {
    Pop-Location
}

# ── 4. Build installer ──────────────────────────────────────────────
Write-Step "Building NSIS installer (electron-builder) ..."

Push-Location $DesktopDir
try {
    $proc = Start-Process -FilePath "cmd" -ArgumentList "/c npm run build:win" `
        -WorkingDirectory $DesktopDir -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Err "Build failed (exit $($proc.ExitCode))."
        exit 1
    }
    Write-OK "Build completed"
} finally {
    Pop-Location
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
