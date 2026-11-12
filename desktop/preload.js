// Bridge for the web app (main window). Exposes only three narrow channels.
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('copilotDesktop', {
  isDesktop: true,
  platform: process.platform,
  overlay: (ev) => ipcRenderer.send('overlay-event', ev),
  state: (s) => ipcRenderer.send('web-state', s),
  onCommand: (cb) => ipcRenderer.on('command', (_e, cmd, arg) => cb(cmd, arg)),
  // macOS: meeting audio straight from the system (ScreenCaptureKit), no BlackHole needed
  systemAudio: process.platform === 'darwin' ? {
    start: () => ipcRenderer.invoke('sysaudio:start'),
    stop: () => ipcRenderer.invoke('sysaudio:stop'),
    onData: (cb) => ipcRenderer.on('sysaudio:data', (_e, data) => {
      const u8 = data instanceof Uint8Array ? data : new Uint8Array(data);
      cb(u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength));
    }),
    onError: (cb) => ipcRenderer.on('sysaudio:error', (_e, msg) => cb(msg)),
  } : null,
});
