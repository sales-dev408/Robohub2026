# Robotics Team Hub — Desktop Offline Build

This folder turns the web-based **Robotics Team Hub** into a fully offline, portable Windows desktop app:

- **Electron** wraps the React frontend.
- **PyInstaller** packages the FastAPI backend into a single `backend.exe`.
- The backend stores everything locally with **SQLite** and the filesystem — no MongoDB, no cloud, no internet.

## What lives here

```text
desktop/
├── backend/
│   ├── main.py              # offline FastAPI backend (replaces cloud backend)
│   ├── requirements.txt     # Python dependencies
│   └── dist/backend.exe     # PyInstaller output (built, not committed)
├── electron/
│   ├── main.js              # Electron entry point (spawns backend, loads UI)
│   ├── preload.js           # exposes safe metadata to the frontend
│   ├── package.json         # Electron dependencies & electron-builder config
│   └── index.html           # fallback page if no React build is present
├── scripts/
│   ├── build-python.ps1     # builds backend.exe
│   ├── build-electron.ps1   # packages the Electron app
│   └── build-all.ps1        # full pipeline: frontend → backend → electron
└── dist/                    # final deliverables (built, not committed)
    ├── RoboticsHub.exe          # single portable .exe
    └── win-unpacked/RoboticsHub.exe  # folder-based USB-friendly app
```

## Quick test (developer mode)

1. Build the frontend:
   ```powershell
   cd ..\frontend
   yarn install
   yarn build
   ```

2. Build the backend executable:
   ```powershell
   cd ..\desktop
   .\scripts\build-python.ps1
   ```

3. Run Electron directly:
   ```powershell
   cd electron
   npm install
   npm start
   ```

Electron will automatically launch `backend\dist\backend.exe` on `http://localhost:54114`, wait for it to be healthy, and then open the React UI.

## Full packaging

See [`docs/PACKAGING_GUIDE.md`](../docs/PACKAGING_GUIDE.md) for the complete, beginner-friendly instructions that the team can follow on a school laptop.

## Default owner account

The backend seeds one owner account on first start:

- **Email:** `owner@robohub.local`
- **Password:** `Robotics2026!`

You can change it by setting environment variables **before the first run** (see the packaging guide).

## Backend port & data directory

- Default port: `54114` (override with `RH_PORT`).
- Data directory: the app tries to keep data next to the executable so it works from a USB stick. If that folder is not writable it falls back to `%LOCALAPPDATA%\RoboticsHub\data`.
- Logs are written to the data directory as `electron.log`.
