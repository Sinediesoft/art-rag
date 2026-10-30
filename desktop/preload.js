// 只給啟動畫面（file://splash.html）用：後端的網頁介面不拿到任何 IPC
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('artrag', {
    onLog: (cb) => ipcRenderer.on('log', (_e, line) => cb(line)),
    onState: (cb) => ipcRenderer.on('state', (_e, state) => cb(state)),
    action: (name) => ipcRenderer.send('action', name),
    ask: (question) => ipcRenderer.invoke('ask', question),
  });
}
