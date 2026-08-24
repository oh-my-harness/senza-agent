'use strict';

/**
 * Minimal preload — no IPC needed.
 * The dashboard communicates with the Python backend via HTTP/WebSocket.
 */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('senzaDesktop', {
  platform: process.platform,
  version: process.env.APP_VERSION || '0.1.0',
});
