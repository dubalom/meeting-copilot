'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('overlayBridge', {
  onEvent: (cb) => ipcRenderer.on('event', (_e, ev) => cb(ev)),
  onStyle: (cb) => ipcRenderer.on('style', (_e, s) => cb(s)),
});
