/**
 * Electron main entry point for the offline Robotics Team Hub desktop app.
 *
 * Responsibilities:
 * 1. Find the bundled Python backend executable.
 * 2. Spawn it on a local port.
 * 3. Wait for the backend health endpoint to respond.
 * 4. Open the Chromium window pointing at the all-in-one HTML frontend.
 * 5. Kill the backend when the Electron window closes.
 */

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, execSync } = require("child_process");
const http = require("http");
const os = require("os");

// This port must match RH_PORT/default in desktop/backend/main.py.
const BACKEND_PORT = 54114;
const BACKEND_HOST = `http://127.0.0.1:${BACKEND_PORT}`;

let mainWindow = null;
let backendProcess = null;
let backendRealPid = null;

function logToFile(line) {
  try {
    const logDir = path.dirname(getDataDir());
    fs.mkdirSync(logDir, { recursive: true });
    const logFile = path.join(logDir, "electron.log");
    fs.appendFileSync(logFile, `${new Date().toISOString()} - ${line}\n`);
  } catch (e) {
    // If we cannot write logs, console is the only fallback.
    console.warn("Could not write electron log:", e.message);
  }
}

function getDataDir() {
  // Development fallback.
  if (!app.isPackaged) {
    return path.join(__dirname, "..", "backend", "data");
  }

  // Production: keep data next to the app so it works from a USB stick or folder.
  // For an electron-builder portable .exe, the app is extracted to a temp folder,
  // but PORTABLE_EXECUTABLE_DIR points to the original .exe location (e.g., the USB).
  const candidates = [
    process.env.PORTABLE_EXECUTABLE_DIR && path.join(process.env.PORTABLE_EXECUTABLE_DIR, "data"),
    path.join(path.dirname(app.getPath("exe")), "data"),
    path.join(app.getPath("userData"), "data"),
  ].filter(Boolean);

  for (const dir of candidates) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      const testFile = path.join(dir, ".write-test");
      fs.writeFileSync(testFile, "ok");
      fs.unlinkSync(testFile);
      return dir;
    } catch (e) {
      // Try the next candidate.
    }
  }

  // Final fallback should never be reached because userData is writable.
  return path.join(app.getPath("userData"), "data");
}

function getBackendExePath() {
  if (app.isPackaged) {
    // electron-builder extraResources copies backend.exe to resources/backend/
    return path.join(process.resourcesPath, "backend", "backend.exe");
  }
  // Development path after running desktop/scripts/build-python.ps1.
  return path.join(__dirname, "..", "backend", "dist", "backend.exe");
}

function getFrontendIndexPath() {
  // Use the all-in-one HTML frontend by default.
  const allInOne = path.join(__dirname, "index.html");
  if (fs.existsSync(allInOne)) {
    return allInOne;
  }
  // Fallback for older React builds still present in resources/build.
  const packaged = path.join(process.resourcesPath, "build", "index.html");
  if (fs.existsSync(packaged)) {
    return packaged;
  }
  const dev = path.join(__dirname, "..", "..", "frontend", "build", "index.html");
  if (fs.existsSync(dev)) {
    return dev;
  }
  return allInOne;
}

function findBackendPidByPort(port) {
  if (process.platform !== "win32") {
    return null;
  }
  try {
    const output = execSync(`netstat -ano -p tcp | findstr :${port}`, {
      encoding: "utf8",
      windowsHide: true,
      timeout: 5000,
    });
    const lines = output.split(/\r?\n/).filter(Boolean);
    for (const line of lines) {
      const match = line.match(/LISTENING\s+(\d+)/i);
      if (match) {
        return parseInt(match[1], 10);
      }
    }
  } catch (e) {
    // netstat/findstr may fail if the port is not open yet.
  }
  return null;
}

function waitForBackend(url, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      const req = http.get(`${url}/api/`, { timeout: 2000 }, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          retry();
        }
      });
      req.on("error", retry);
      req.on("timeout", () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) {
        reject(new Error("Backend did not start in time"));
        return;
      }
      setTimeout(check, 400);
    };
    check();
  });
}

function startBackend() {
  const exe = getBackendExePath();
  if (!fs.existsSync(exe)) {
    const msg = `Backend executable not found at: ${exe}`;
    logToFile(msg);
    throw new Error(msg);
  }

  const dataDir = getDataDir();
  fs.mkdirSync(dataDir, { recursive: true });

  const env = {
    ...process.env,
    RH_PORT: String(BACKEND_PORT),
    RH_DATA_DIR: dataDir,
    OWNER_EMAIL: process.env.OWNER_EMAIL || "",
    OWNER_PASSWORD: process.env.OWNER_PASSWORD || "",
    OWNER_NAME: process.env.OWNER_NAME || "",
  };

  logToFile(`Spawning backend: ${exe} with data dir ${dataDir}`);

  backendProcess = spawn(exe, [], {
    env,
    detached: false,
    windowsHide: true, // Hide the black terminal window on school laptops.
  });

  backendProcess.stdout.on("data", (data) => {
    logToFile(`[backend] ${data.toString().trim()}`);
  });

  backendProcess.stderr.on("data", (data) => {
    logToFile(`[backend stderr] ${data.toString().trim()}`);
  });

  backendProcess.on("exit", (code) => {
    logToFile(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

function stopBackend() {
  const pids = new Set();
  if (backendProcess && !backendProcess.killed && backendProcess.pid) {
    pids.add(backendProcess.pid);
  }
  if (backendRealPid) {
    pids.add(backendRealPid);
  }
  // The real server process may have a different PID than the one spawn() returned
  // (PyInstaller onefile spawns a child), so also look it up by the listening port.
  const portPid = findBackendPidByPort(BACKEND_PORT);
  if (portPid) {
    pids.add(portPid);
  }

  logToFile(`Stopping backend processes: ${[...pids].join(", ")}`);
  for (const pid of pids) {
    try {
      if (process.platform === "win32") {
        execSync(`taskkill /F /T /PID ${pid}`, { timeout: 5000, windowsHide: true });
      } else {
        process.kill(pid, "SIGTERM");
      }
    } catch (e) {
      // Process may already be gone; that's fine.
      logToFile(`Stop pid ${pid} result: ${e.message}`);
    }
  }

  // Fallback: force-kill any remaining backend.exe processes owned by this app.
  try {
    if (process.platform === "win32") {
      execSync("taskkill /F /IM backend.exe", { timeout: 5000, windowsHide: true });
    } else {
      execSync("pkill -f backend", { timeout: 5000 });
    }
    logToFile("Fallback backend kill attempted");
  } catch (e) {
    // taskkill returns 128 when no matching process exists; that's fine.
  }

  try {
    if (backendProcess) {
      backendProcess.kill();
    }
  } catch (e) {
    // Already gone.
  }
  backendProcess = null;
  backendRealPid = null;
}

function createWindow() {
  const indexPath = getFrontendIndexPath();

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    title: "Robotics Team Hub",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The frontend is loaded from file:// and talks to http://localhost.
      // For maximum compatibility across school laptops, webSecurity is disabled
      // here and CORS is handled by the backend.
      webSecurity: false,
    },
  });

  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadFile(indexPath).catch((err) => {
    logToFile(`Failed to load UI: ${err.message}`);
  });

  mainWindow.webContents.on("console-message", (event) => {
    const { level, message, lineNumber, sourceId } = event;
    logToFile(`[console:${level}] ${message} (${sourceId}:${lineNumber})`);
  });



  // Optional: open DevTools with F12 or Ctrl+Shift+I.
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (
      input.type === "keyDown" &&
      (input.key === "F12" || (input.control && input.shift && input.key.toLowerCase() === "i"))
    ) {
      mainWindow.webContents.openDevTools();
      event.preventDefault();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    startBackend();
    await waitForBackend(BACKEND_HOST);
    backendRealPid = findBackendPidByPort(BACKEND_PORT);
    if (backendRealPid) {
      logToFile(`Backend server pid: ${backendRealPid}`);
    }
    createWindow();
  } catch (err) {
    logToFile(`Startup error: ${err.message}`);
    // Even if the backend fails to respond, show the window so the user sees an error.
    createWindow();
  }
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") {
    app.quit();
    // Force-exit in case a lingering backend child process keeps the event loop alive.
    setTimeout(() => app.exit(0), 800);
  }
});

app.on("before-quit", () => {
  stopBackend();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});
