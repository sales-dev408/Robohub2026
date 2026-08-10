const { contextBridge } = require("electron");

// Expose a minimal, safe API to the renderer so the React app can detect
// that it is running inside Electron and switch to HashRouter / enable
// desktop-only features such as the per-member permissions manager.
contextBridge.exposeInMainWorld("electronAPI", {
  isElectron: true,
  backendPort: 54114,
});
