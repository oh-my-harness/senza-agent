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

const { app, BrowserWindow, shell, nativeImage, Menu, dialog, ipcMain } = require('electron');
const { autoUpdater, CancellationToken } = require('electron-updater');
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
      backgroundThrottling: false,
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

const APP_DESCRIPTION =
  'senza-agent — 开箱即用的通用 AI Agent\n' +
  '基于 Senza SDK 构建：终端 CLI、Web 面板、桌面应用三端一体，\n' +
  '内置文件读写、命令执行、联网搜索、技能系统等工具。\n' +
  '项目主页: https://github.com/oh-my-harness/senza-agent';

function showAboutDialog() {
  const win = BrowserWindow.getFocusedWindow() || mainWindow;
  dialog.showMessageBox(win, {
    type: 'info',
    title: 'About senza-agent',
    message: `senza-agent v${app.getVersion()}`,
    detail: APP_DESCRIPTION,
    buttons: ['检查更新', '项目主页', '确定'],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  }).then(({ response }) => {
    if (response === 0) {
      checkForUpdatesInteractive(win);
    } else if (response === 1) {
      shell.openExternal('https://github.com/oh-my-harness/senza-agent');
    }
  });
}

function setupMenu() {
  const template = [
    {
      label: 'senza-agent',
      submenu: [
        { label: 'About senza-agent', click: () => showAboutDialog() },
        { label: 'Check for Updates…', click: () => checkForUpdatesInteractive(BrowserWindow.getFocusedWindow() || mainWindow) },
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
  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
    `<!doctype html><html><head><meta charset="utf-8"></head>` +
    `<div style="font-family:system-ui;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;">` +
    `<div style="font-size:18px;font-weight:600;">SenzaAgent</div>` +
    `<div id="status" style="font-size:14px;color:#8b949e;">${message}</div>` +
    `<div style="width:200px;height:4px;background:#21262d;border-radius:2px;overflow:hidden;">` +
    `<div style="width:40%;height:100%;background:#58a6ff;border-radius:2px;animation:pulse 1.5s ease-in-out infinite;"></div></div>` +
    `<style>@keyframes pulse{0%,100%{opacity:0.3}50%{opacity:1}}</style>` +
    `</div>`));
  return win;
}

// ── Native folder picker (settings → 工作目录) ──────────────────────────────

ipcMain.handle('desktop:pick-folder', async (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(win, {
    title: '选择工作目录',
    properties: ['openDirectory', 'createDirectory'],
  });
  return result.canceled || result.filePaths.length === 0 ? null : result.filePaths[0];
});

// ── Update download progress window ─────────────────────────────────────────
//
// Small always-on-top window shown while the update package downloads.
// Mirrors the startup loading window style; carries a determinate bar driven
// by electron-updater's "download-progress" events plus a Cancel button that
// aborts the transfer via CancellationToken.

let _updateProgressWin = null;

function _execInProgressWin(js) {
  if (_updateProgressWin && !_updateProgressWin.isDestroyed()) {
    _updateProgressWin.webContents.executeJavaScript(js).catch(() => {});
  }
}

function showUpdateProgressWindow(version) {
  _updateProgressWin = new BrowserWindow({
    width: 420, height: 190, frame: false, resizable: false,
    title: 'senza-agent 更新', backgroundColor: '#0d1117',
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  // charset=utf-8 in the MIME type AND <meta charset> in the document: without one,
  // Chromium decodes the data: URL body with the OS locale's legacy encoding
  // (GB18030 on zh-CN Windows), producing mojibake for the Chinese UI text.
  _updateProgressWin.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
    `<!doctype html><html><head><meta charset="utf-8"></head>` +
    `<div style="font-family:system-ui;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;">` +
    `<div style="font-size:16px;font-weight:600;">正在下载 v${version}</div>` +
    `<div style="width:300px;height:6px;background:#21262d;border-radius:3px;overflow:hidden;">` +
    `<div id="bar" style="width:0%;height:100%;background:#58a6ff;border-radius:3px;transition:width 0.2s ease;"></div></div>` +
    `<div id="pct" style="font-size:13px;color:#8b949e;">0%（连接中…）</div>` +
    `<button id="cancel" style="margin-top:4px;padding:5px 18px;font-size:13px;color:#c9d1d9;background:#21262d;border:1px solid #30363d;border-radius:6px;cursor:pointer;">取消</button>` +
    `</div>`));
  return _updateProgressWin;
}

function setUpdateProgress(info) {
  const pct = Math.max(0, Math.min(100, Math.round(info.percent || 0)));
  const mb = (n) => (n / 1024 / 1024).toFixed(1);
  _execInProgressWin(
    `document.getElementById('bar').style.width = '${pct}%';` +
    `document.getElementById('pct').textContent = ${JSON.stringify(
      `${pct}%（${mb(info.transferred)}/${mb(info.total)} MB，${mb(info.bytesPerSecond)} MB/s）`
    )};`
  );
}

function setUpdateProgressDone() {
  _execInProgressWin(
    `document.getElementById('bar').style.width = '100%';` +
    `document.getElementById('pct').textContent = '下载完成，正在准备安装…';` +
    `document.getElementById('cancel').style.display = 'none';`
  );
}

function closeUpdateProgressWindow() {
  if (_updateProgressWin && !_updateProgressWin.isDestroyed()) _updateProgressWin.close();
  _updateProgressWin = null;
}

// ── Update check (manual, from the app menu) ───────────────────────────────
//
// No background polling and no automatic download: updates are only checked
// when the user clicks "Check for Updates" in the senza-agent menu. If a
// newer release exists the user is asked whether to download + install it
// right away; the download itself installs on restart (or app quit).
// Dev / non-packaged runs are skipped entirely.

let _updateBusy = false;

async function checkForUpdatesInteractive(win) {
  if (!app.isPackaged) {
    dialog.showMessageBox(win, { type: 'info', message: '开发模式下不检查更新。' });
    return;
  }
  if (_updateBusy) return;
  _updateBusy = true;
  try {
    // Disable auto-download while probing: we only want version metadata.
    autoUpdater.autoDownload = false;
    autoUpdater.logger = console;
    const res = await autoUpdater.checkForUpdates();
    const ui = res && res.updateInfo ? res.updateInfo : null;
    const latest = ui && ui.version ? ui.version : '';
    const hasUpdate = !!latest && latest !== app.getVersion();
    const r = await dialog.showMessageBox(win, {
      type: 'info',
      message: hasUpdate ? `发现新版本 v${latest}` : '当前已是最新版本',
      detail: hasUpdate
        ? `当前版本 v${app.getVersion()}，最新版本 v${latest}。是否下载并安装？`
        : `当前版本 v${app.getVersion()} 已是最新。`,
      buttons: hasUpdate ? ['下载并安装', '以后再说'] : ['确定'],
      defaultId: 0,
      cancelId: hasUpdate ? 1 : 0,
    });
    if (hasUpdate && r.response === 0) {
      autoUpdater.autoDownload = true;
      const token = new CancellationToken();
      showUpdateProgressWindow(latest);
      // Wait for the data: URL document to finish parsing before running any
      // executeJavaScript against it — earlier calls could hit an unloaded
      // window and be silently swallowed by _execInProgressWin's catch.
      await new Promise((resolve) => {
        if (_updateProgressWin.webContents.isLoading()) {
          _updateProgressWin.webContents.once('did-finish-load', resolve);
        } else {
          resolve();
        }
      });

      // Cancel button inside the progress window aborts the transfer.
      _execInProgressWin(
        `document.getElementById('cancel').onclick = () => require('electron').ipcRenderer.send('desktop:cancel-update');`
      );
      ipcMain.once('desktop:cancel-update', () => {
        token.cancel();
      });

      const onProgress = (info) => setUpdateProgress(info);
      autoUpdater.on('download-progress', onProgress);
      try {
        await autoUpdater.downloadUpdate(token);
      } catch (dlErr) {
        // A user cancel rejects with "cancelled"; anything else is real.
        if (!token.cancelled) throw dlErr;
      } finally {
        autoUpdater.removeListener('download-progress', onProgress);
        closeUpdateProgressWindow();
      }
      if (token.cancelled) return;

      setUpdateProgressDone();
      const d = await dialog.showMessageBox(win, {
        type: 'info',
        message: `v${latest} 已下载完成`,
        detail: '重启应用后自动安装。现在重启吗？',
        buttons: ['立即重启安装', '下次启动时安装'],
        defaultId: 0,
        cancelId: 1,
      });
      if (d.response === 0) {
        // quitAndInstall terminates the app (before-quit SIGTERMs the Python
        // backend), then hands over to the NSIS installer.
        setTimeout(() => autoUpdater.quitAndInstall(false, true), 150);
      }
    }
  } catch (err) {
    dialog.showMessageBox(win, {
      type: 'warning',
      message: '检查更新失败',
      detail: (err && err.message) || String(err),
    });
  } finally {
    _updateBusy = false;
  }
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
    mainWindow.loadURL('data:text/html;charset=utf-8,<meta charset="utf-8"><h2 style="font-family:sans-serif;color:#e6edf3;background:#0d1117;height:100vh;margin:0;display:flex;align-items:center;justify-content:flex-start;padding:20px;">' + err.message.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</h2>');
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
