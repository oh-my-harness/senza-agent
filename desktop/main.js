'use strict';

/**
 * senza-agent Desktop — Electron main process.
 *
 * Spawns `senza-agent --web` as a child process, waits for the HTTP
 * server to be ready, then loads the dashboard in a BrowserWindow.
 *
 * Architecture:
 *   ┌──────────────────────────────────────────────┐
 *   │ Electron (this file)                         │
 *   │  ┌─ BrowserWindow → http://localhost:PORT    │
 *   │  │                                            │
 *   │  └─ child process: senza-agent --web PORT    │
 *   └──────────────────────────────────────────────┘
 *
 * The Python webserver (aiohttp) owns all business logic:
 *   - Task execution & event streaming (WS /ws/task)
 *   - Render panels (/ws)
 *   - Terminal, files, browser automation, apps
 *
 * Electron is just a thin shell: window + lifecycle.
 */

const { app, BrowserWindow, shell, nativeImage, Menu } = require('electron');
const path = require('path');
const http = require('http');
const net  = require('net');
const fs   = require('fs');
const { spawn } = require('child_process');

app.setAppUserModelId('com.senzaagent.desktop');

// ── Paths ──────────────────────────────────────────────────────────────────
//
// When packaged with electron-builder, the app structure is:
//
//   <InstallDir>/
//   ├── SenzaAgent.exe                 ← Electron binary
//   ├── resources/
//   │   ├── app.asar                    ← main.js, preload.js, icon.png
//   │   ├── senza_agent/               ← Python backend source
//   │   ├── SKILLS/                     ← Skill definitions
//   │   ├── AGENTS.md
//   │   ├── pyproject.toml
//   │   ├── requirements.txt
//   │   └── setup_python.ps1
//   └── python_venv/                    ← Created by installer / first-run
//       └── Scripts/python.exe
//
// APP_ROOT = the directory containing senza_agent/ (the Python source root).
//   - Packaged: <InstallDir>/resources/
//   - Dev:      repo root (parent of desktop/)
//
// INSTALL_DIR = the top-level installation directory (for venv lookup).
//   - Packaged: parent of resources/
//   - Dev:      same as APP_ROOT

const EXE_DIR     = app.isPackaged ? path.dirname(app.getPath('exe')) : path.resolve(__dirname, '..');
const APP_ROOT    = app.isPackaged ? path.join(EXE_DIR, 'resources') : path.resolve(__dirname, '..');
const INSTALL_DIR = app.isPackaged ? EXE_DIR : APP_ROOT;
const DOT_ENV_DIR = app.isPackaged
  ? (process.platform === 'win32' ? EXE_DIR : app.getPath('userData'))
  : APP_ROOT;

// ── .env loading (same logic as cli.py load_dotenv_if_present) ─────────────

function loadDotenv() {
  const envFile = path.join(DOT_ENV_DIR, '.env');
  let raw;
  try { raw = fs.readFileSync(envFile, 'utf8'); } catch { return; }
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    if (!key || key in process.env) continue;
    let val = line.slice(eq + 1).trim();
    if (val.length >= 2 && val[0] === val[val.length - 1] && (val[0] === '"' || val[0] === "'")) {
      val = val.slice(1, -1);
    }
    process.env[key] = val;
  }
}

loadDotenv();

// ── Python command ─────────────────────────────────────────────────────────

function findPythonCmd() {
  // 1. Explicit override
  if (process.env.PYTHON_CMD) return process.env.PYTHON_CMD;
  // 2. Installer-created venv (packaged app on Windows): <InstallDir>/python_venv/
  const installerVenv = process.platform === 'win32'
    ? path.join(INSTALL_DIR, 'python_venv', 'Scripts', 'python.exe')
    : path.join(INSTALL_DIR, 'python_venv', 'bin', 'python');
  if (fs.existsSync(installerVenv)) return installerVenv;
  // 3. Bundled Python (packaged app)
  const bundled = process.platform === 'win32'
    ? path.join(__dirname, 'vendor', 'python', 'python.exe')
    : path.join(__dirname, 'vendor', 'python', 'bin', 'python3');
  if (fs.existsSync(bundled)) return bundled;
  // 4. Local venv (repo root .venv — dev mode)
  const repoVenv = process.platform === 'win32'
    ? path.join(APP_ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(APP_ROOT, '.venv', 'bin', 'python');
  if (fs.existsSync(repoVenv)) return repoVenv;
  // 5. Sibling Senza repo venv (dev setup: senza-agent lives next to Senza)
  const senzaVenv = process.platform === 'win32'
    ? path.join(APP_ROOT, '..', 'Senza', '.venv', 'Scripts', 'python.exe')
    : path.join(APP_ROOT, '..', 'Senza', '.venv', 'bin', 'python');
  if (fs.existsSync(senzaVenv)) return senzaVenv;
  // 6. System python3 / python
  const sysBin = process.platform === 'win32' ? 'python' : 'python3';
  return sysBin;
}

// ── First-run venv setup ─────────────────────────────────────────────────

let _loadingWin = null;

function setLoadingMessage(msg) {
  if (_loadingWin && !_loadingWin.isDestroyed()) {
    _loadingWin.webContents.executeJavaScript(
      `document.getElementById('status').textContent = ${JSON.stringify(msg)};`
    ).catch(() => {});
  }
}

function ensurePythonVenv() {
  if (!app.isPackaged || process.platform !== 'win32') return Promise.resolve();

  const venvPython = path.join(INSTALL_DIR, 'python_venv', 'Scripts', 'python.exe');
  if (fs.existsSync(venvPython)) return Promise.resolve();

  const setupScript = path.join(APP_ROOT, 'setup_python.ps1');
  if (!fs.existsSync(setupScript)) return Promise.resolve();

  console.log('[desktop] No Python venv found — running setup_python.ps1...');
  setLoadingMessage('Creating Python virtual environment...');

  return new Promise((resolve) => {
    const proc = spawn('powershell.exe', [
      '-ExecutionPolicy', 'Bypass',
      '-NoProfile',
      '-File', setupScript,
      '-InstallDir', INSTALL_DIR,
    ], { stdio: ['ignore', 'pipe', 'pipe'] });

    proc.stdout.on('data', (data) => {
      const text = data.toString().trim();
      if (!text) return;
      console.log('[setup]', text);
      // Forward meaningful lines to the loading window
      if (text.includes('Creating'))       setLoadingMessage('Creating Python virtual environment...');
      else if (text.includes('Upgrading'))  setLoadingMessage('Upgrading pip...');
      else if (text.includes('Installing dependencies')) setLoadingMessage('Installing Python packages...');
      else if (text.includes('senza-agent')) setLoadingMessage('Installing senza-agent...');
      else if (text.includes('complete'))   setLoadingMessage('Finalizing setup...');
    });
    proc.stderr.on('data', (data) => {
      const text = data.toString().trim();
      if (text) console.error('[setup]', text);
    });
    proc.on('exit', (code) => {
      console.log(`[setup] setup_python.ps1 exited (code=${code})`);
      resolve();
    });
    proc.on('error', (err) => {
      console.error('[setup] Failed to run setup_python.ps1:', err.message);
      resolve();
    });
  });
}

// ── Port selection ─────────────────────────────────────────────────────────

function findFreePort(startPort) {
  return new Promise(resolve => {
    const sock = net.connect(startPort, '127.0.0.1');
    sock.once('connect', () => { sock.destroy(); findFreePort(startPort + 1).then(resolve); });
    sock.once('error',   () => { sock.destroy(); resolve(startPort); });
  });
}

// ── State ──────────────────────────────────────────────────────────────────

let mainWindow = null;
let agentProc = null;
let agentPort = 8090;

// ── Spawn agent ────────────────────────────────────────────────────────────

async function startAgent() {
  agentPort = await findFreePort(8090);

  // Ensure the Python venv exists (first run after MSI install, or if the
  // NSIS post-install step failed).  No-op if venv already present or dev mode.
  await ensurePythonVenv();

  const pyCmd  = findPythonCmd();
  const args   = ['-m', 'senza_agent.cli', '--no-inspect', '--web', String(agentPort)];

  // PYTHONPATH: point at the directory containing senza_agent/ so the package
  // is importable.  In packaged mode this is APP_ROOT (resources/).
  // SENZA_AGENT_DIR: tells the Python backend where AGENTS.md, SKILLS/, etc.
  // live (same as APP_ROOT in packaged mode).
  const env = { ...process.env };
  if (!env.PYTHONPATH) {
    env.PYTHONPATH = APP_ROOT;
  } else {
    env.PYTHONPATH = APP_ROOT + path.delimiter + env.PYTHONPATH;
  }
  env.SENZA_AGENT_DIR = APP_ROOT;
  env.PYTHONUTF8       = '1';
  env.PYTHONIOENCODING = 'utf-8';

  agentProc = spawn(pyCmd, args, { env, cwd: APP_ROOT, stdio: ['ignore', 'pipe', 'pipe'] });

  agentProc.stdout.on('data', (data) => {
    const text = data.toString().trim();
    if (text) console.log('[agent]', text);
  });
  agentProc.stderr.on('data', (data) => {
    const text = data.toString().trim();
    if (text) console.error('[agent]', text);
  });
  agentProc.on('exit', (code, signal) => {
    console.log(`[agent] exited (code=${code}, signal=${signal})`);
    agentProc = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.close();
    }
  });
}

// ── Wait for server ────────────────────────────────────────────────────────

function waitForServer(port, maxRetries, intervalMs) {
  return new Promise((resolve, reject) => {
    let retries = maxRetries;
    const attempt = () => {
      const req = http.get(`http://127.0.0.1:${port}/api/status`, (res) => {
        res.resume();
        resolve();
      });
      req.setTimeout(1000, () => req.destroy());
      req.on('error', () => {
        if (--retries <= 0) {
          reject(new Error(`Server not ready on port ${port} after ${maxRetries} retries`));
          return;
        }
        setTimeout(attempt, intervalMs);
      });
    };
    attempt();
  });
}

// ── Window ─────────────────────────────────────────────────────────────────

function createWindow() {
  const iconPath = path.join(__dirname, 'icon.png');
  const appIcon  = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : undefined;

  mainWindow = new BrowserWindow({
    width:           1200,
    height:          800,
    minWidth:        600,
    minHeight:       400,
    title:           'senza-agent',
    icon:            appIcon || undefined,
    backgroundColor: '#0d1117',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, 'preload.js'),
    },
  });

  if (appIcon && !appIcon.isEmpty()) {
    mainWindow.setIcon(appIcon);
  }

  // External links open in system browser.
  mainWindow.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    shell.openExternal(targetUrl);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (e, targetUrl) => {
    try {
      const { hostname, protocol } = new URL(targetUrl);
      if (protocol !== 'file:' && hostname !== '127.0.0.1' && hostname !== 'localhost') {
        e.preventDefault();
        shell.openExternal(targetUrl);
      }
    } catch { e.preventDefault(); }
  });

  mainWindow.on('closed', () => { mainWindow = null; });

  // Load the dashboard.
  mainWindow.loadURL(`http://127.0.0.1:${agentPort}`);
}

// ── Menu ───────────────────────────────────────────────────────────────────

function setupMenu() {
  const template = [
    {
      label: 'senza-agent',
      submenu: [
        { role: 'about', label: 'About senza-agent' },
        { type: 'separator' },
        { role: 'reload', label: 'Reload' },
        { role: 'toggleDevTools', label: 'Toggle Developer Tools' },
        { type: 'separator' },
        { role: 'quit', label: 'Quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' }, { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── App lifecycle ──────────────────────────────────────────────────────────

function showLoadingWindow(message) {
  const win = new BrowserWindow({
    width: 480, height: 220, frame: false, resizable: false,
    title: 'senza-agent', backgroundColor: '#0d1117',
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  win.loadURL('data:text/html,' + encodeURIComponent(
    `<div style="font-family:system-ui;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;">` +
    `<div style="font-size:18px;font-weight:600;">SenzaAgent</div>` +
    `<div id="status" style="font-size:14px;color:#8b949e;">${message}</div>` +
    `<div style="width:200px;height:4px;background:#21262d;border-radius:2px;overflow:hidden;">` +
    `<div style="width:40%;height:100%;background:#58a6ff;border-radius:2px;animation:pulse 1.5s ease-in-out infinite;"></div></div>` +
    `<style>@keyframes pulse{0%,100%{opacity:0.3}50%{opacity:1}}</style>` +
    `</div>`));
  return win;
}

app.whenReady().then(async () => {
  setupMenu();

  // Show a loading window during first-run setup (venv creation can take 30s+).
  const venvPython = process.platform === 'win32'
    ? path.join(INSTALL_DIR, 'python_venv', 'Scripts', 'python.exe')
    : '';
  const needsSetup = app.isPackaged && process.platform === 'win32' && !fs.existsSync(venvPython);
  if (needsSetup) {
    _loadingWin = showLoadingWindow('Setting up Python environment...');
  }

  try {
    await startAgent();
    // Wait up to 120s for the Python webserver (first-run pip install is slow).
    await waitForServer(agentPort, 240, 500);
  } catch (err) {
    console.error('[desktop] Failed to start agent:', err.message);
    if (_loadingWin) _loadingWin.close();
    mainWindow = new BrowserWindow({ width: 500, height: 300, title: 'senza-agent — Error' });
    mainWindow.loadURL('data:text/html,<h2 style="font-family:sans-serif;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;align-items:center;justify-content:flex-start;padding:20px;">' + err.message + '</h2>');
    return;
  }

  if (_loadingWin) _loadingWin.close();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (agentProc) {
    agentProc.kill('SIGTERM');
    agentProc = null;
  }
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (agentProc) {
    agentProc.kill('SIGTERM');
    agentProc = null;
  }
});

process.on('exit', () => {
  if (agentProc) agentProc.kill('SIGKILL');
});
