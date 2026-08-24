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

const APP_ROOT    = app.isPackaged
  ? path.dirname(app.getPath('exe'))
  : path.resolve(__dirname, '..');
const DOT_ENV_DIR = app.isPackaged
  ? (process.platform === 'win32' ? APP_ROOT : app.getPath('userData'))
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
  // 2. Bundled Python (packaged app)
  const bundled = process.platform === 'win32'
    ? path.join(__dirname, 'vendor', 'python', 'python.exe')
    : path.join(__dirname, 'vendor', 'python', 'bin', 'python3');
  if (fs.existsSync(bundled)) return bundled;
  // 3. Local venv (repo root .venv)
  const repoVenv = process.platform === 'win32'
    ? path.join(APP_ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(APP_ROOT, '.venv', 'bin', 'python');
  if (fs.existsSync(repoVenv)) return repoVenv;
  // 4. Sibling Senza repo venv (dev setup: senza-agent lives next to Senza)
  const senzaVenv = process.platform === 'win32'
    ? path.join(APP_ROOT, '..', 'Senza', '.venv', 'Scripts', 'python.exe')
    : path.join(APP_ROOT, '..', 'Senza', '.venv', 'bin', 'python');
  if (fs.existsSync(senzaVenv)) return senzaVenv;
  // 5. System python3 / python
  const sysBin = process.platform === 'win32' ? 'python' : 'python3';
  return sysBin;
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

  const pyCmd  = findPythonCmd();
  const args   = ['-m', 'senza_agent.cli', '--no-inspect', '--web', String(agentPort)];

  // PYTHONPATH: point at senza-agent dir so `senza_agent` is importable.
  const env = { ...process.env };
  if (!env.PYTHONPATH) {
    env.PYTHONPATH = APP_ROOT;
  } else {
    env.PYTHONPATH = APP_ROOT + path.delimiter + env.PYTHONPATH;
  }
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

app.whenReady().then(async () => {
  setupMenu();

  try {
    await startAgent();
    // Wait up to 30s for the Python webserver to be ready.
    await waitForServer(agentPort, 60, 500);
  } catch (err) {
    console.error('[desktop] Failed to start agent:', err.message);
    // Show an error window.
    mainWindow = new BrowserWindow({ width: 500, height: 300, title: 'senza-agent — Error' });
    mainWindow.loadURL('data:text/html,<h2 style="font-family:sans-serif;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;align-items:center;justify-content:flex-start;padding:20px;">' + err.message + '</h2>');
    return;
  }

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
