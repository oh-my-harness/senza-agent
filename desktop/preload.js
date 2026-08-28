'use strict';

/**
 * Preload bridge — exposes a minimal, audited API surface to the dashboard.
 * The dashboard communicates with the Python backend via HTTP/WebSocket;
 * the only IPC is the native folder picker used by the settings panel.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('senzaDesktop', {
  platform: process.platform,
  version: process.env.APP_VERSION || '0.1.0',
  // Resolves to the picked absolute path, or null if the user cancelled.
  pickFolder: () => ipcRenderer.invoke('desktop:pick-folder'),
  // Resolves when the downloaded update has been handed to the installer.
  installUpdate: () => ipcRenderer.invoke('desktop:install-update'),
  // onUpdateDownloaded(cb) — cb receives { version } when an update finished
  // downloading in the background.
  onUpdateDownloaded: (cb) => ipcRenderer.on('desktop:update-downloaded', (_e, info) => cb(info)),
});

// ── Native folder picker (desktop app only) ────────────────────────────────
// The dashboard calls window.senzaDesktop.pickFolder() to open a native
// directory chooser. Resolves to the picked absolute path, or null if the
// user cancelled. In a plain browser (dev tab) this API simply doesn't
// exist, and the panel hides the browse button.
