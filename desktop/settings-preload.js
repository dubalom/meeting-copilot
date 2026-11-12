'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('settingsBridge', {
  get: () => ipcRenderer.invoke('settings:get'),
  save: (url) => ipcRenderer.invoke('settings:save', url),
});
