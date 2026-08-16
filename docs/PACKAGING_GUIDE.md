# Robotics Team Hub — Offline Desktop Packaging Guide

This guide explains how to build and distribute the Robotics Team Hub as a **single offline Windows desktop app** that runs from a folder or USB stick with **no installation, no admin rights, and no internet**.

## What you will have at the end

- A single `RoboticsHub.exe` file (double-click to run).
- An optional `win-unpacked/` folder version that is easier to copy onto USB drives.
- A local backend stored as `backend.exe` that Electron starts and stops automatically.
- All team data kept on the local machine or USB stick (SQLite + files).

---

## Requirements (only on the machine that builds the app)

You only need these tools **once** on the build computer. The students' school laptops do **not** need anything installed.

1. **Windows 10 or Windows 11**
2. **Node.js 18+ LTS** (with `npm`)
3. **Python 3.11+** (with `pip`)
4. **Yarn** (for the React frontend — `npm` will also work)
5. A copy of this repository

> If your build machine does not have Python available as `python`, use the full path to your Python executable in the commands below.

---

## Step 1 — Build the React frontend

The frontend must be a static website that Electron can load from the local filesystem.

Open PowerShell in the **repository root** and run:

```powershell
cd frontend
yarn install
yarn build
```

When it finishes, a `frontend\build` folder appears with `index.html` and all static files.

---

## Step 2 — Build the Python backend executable

The backend is packaged with **PyInstaller** into one `backend.exe` file.

### 2a. Create a Python virtual environment

```powershell
cd desktop\backend
python -m venv .venv
```

### 2b. Install dependencies

```powershell
.venv\Scripts\pip.exe install --upgrade pip
.venv\Scripts\pip.exe install -r requirements.txt
```

### 2c. Run PyInstaller

This is the **exact command** used to create `backend.exe`:

```powershell
.venv\Scripts\pyinstaller.exe `
    --onefile `
    --name backend `
    --clean `
    --noconsole `
    --hidden-import=sqlite3 `
    --hidden-import=uvicorn `
    --hidden-import=uvicorn.logging `
    --hidden-import=uvicorn.loops `
    --hidden-import=uvicorn.loops.asyncio `
    --hidden-import=uvicorn.protocols `
    --hidden-import=uvicorn.protocols.http `
    --hidden-import=uvicorn.protocols.http.auto `
    --hidden-import=uvicorn.lifespan `
    --hidden-import=uvicorn.lifespan.on `
    main.py
```

When it finishes, the file is at:

```text
desktop\backend\dist\backend.exe
```

> **What this command does:**
> - `--onefile` — bundles everything into a single `.exe`.
> - `--noconsole` — hides the black terminal window so students only see the app window.
> - `--hidden-import` — tells PyInstaller to include modules the backend needs at runtime.

---

## Step 3 — Package Electron

### 3a. Install Electron dependencies

```powershell
cd desktop\electron
npm install
```

### 3b. Build the desktop app

```powershell
npx electron-builder --win portable --win dir
```

This produces two outputs in `desktop\dist`:

```text
desktop\dist\RoboticsHub.exe              ← single portable .exe
desktop\dist\win-unpacked\RoboticsHub.exe  ← folder-based app (best for USB)
```

---

## Step 4 — Final folder layout

After all builds are complete, the `desktop\dist` folder looks like this:

```text
desktop\dist\
├── RoboticsHub.exe                    (single portable executable, ~90 MB)
└── win-unpacked\                     (copy this whole folder to a USB stick)
    ├── RoboticsHub.exe
    ├── locales\
    ├── resources\
    │   ├── app.asar
    │   ├── backend\backend.exe        (PyInstaller backend)
    │   └── build\                    (React frontend files)
    │       ├── index.html
    │       └── static\
    ├── *.dll                         (Electron / Chromium libraries)
    └── ...
```

### How the files talk to each other

1. `RoboticsHub.exe` starts `resources\backend\backend.exe`.
2. `backend.exe` opens a web server on `http://localhost:54114`.
3. `RoboticsHub.exe` loads `resources\build\index.html` in a Chromium window.
4. The React UI calls `http://localhost:54114/api/...` to load and save data.
5. When the user closes the window, `RoboticsHub.exe` kills the backend process.

---

## Step 5 — Distribute to students

### Option A: USB stick (recommended for school use)

1. Copy the entire `win-unpacked` folder to the USB drive.
2. Students plug in the USB drive.
3. Students open `win-unpacked\RoboticsHub.exe`.
4. The app runs entirely from the USB drive. Data is stored in a `data` folder next to `RoboticsHub.exe`.

### Option B: One file to share

1. Give students `RoboticsHub.exe`.
2. They double-click it.
3. The app starts and stores data next to the executable when possible. If the executable is in a protected folder, data falls back to `%LOCALAPPDATA%\RoboticsHub\data`.

> **Tip:** For school laptops with strict policies, the `win-unpacked` folder method is usually more reliable because nothing needs to be extracted to a temporary directory.

---

## Step 6 — First run and owner setup

The first time the app starts, it creates the owner account.

### Default owner login

- **Email:** `owner@robohub.local`
- **Password:** `Robotics2026!`

### How to set a custom owner account

Before the very first run, create a batch file named `Start Robotics Hub.bat` in the same folder as `RoboticsHub.exe`:

```bat
@echo off
set OWNER_EMAIL=youremail@school.edu
set OWNER_PASSWORD=YourSecurePassword123!
set OWNER_NAME=Mr. Smith
start "" "RoboticsHub.exe"
```

Students run `Start Robotics Hub.bat` instead of the `.exe`. The owner account is created with the values you set.

---

## Step 7 — Managing member permissions

1. Sign in as the owner.
2. Go to **Team** in the left sidebar.
3. Add members by clicking **Add Member**.
4. For each member, click the **Permissions** button.
5. Toggle permissions such as:
   - Send chat & direct messages
   - Upload files
   - View Members-Only channel
   - Create & edit calendar events
   - Manage any task
   - Delete any chat message / file
   - Approve / add / remove members
6. Click **Save Permissions**.

The owner always has full permissions and cannot be changed.

---

## How to rebuild after making code changes

Run the full pipeline script from the repository root:

```powershell
.\desktop\scripts\build-all.ps1
```

This script does the following:

1. Builds the React frontend (`frontend\build`).
2. Runs the PyInstaller command in `desktop\backend`.
3. Runs `electron-builder` in `desktop\electron`.
4. Outputs to `desktop\dist`.

---

## Troubleshooting

| Problem | Likely cause | Fix |
| --- | --- | --- |
| "Backend executable not found" | `desktop\backend\dist\backend.exe` missing | Run the PyInstaller command in Step 2. |
| "Backend did not start in time" | Port `54114` already in use | Close other copies of the app, or restart the laptop. |
| App window opens but stays blank | Frontend build missing | Run `yarn build` in `frontend`. |
| Antivirus blocks `backend.exe` | PyInstaller executables are sometimes flagged | Add an exception for `backend.exe` in Windows Defender. |
| Data is lost after restart | Running the single `.exe` from a temp folder | Use the `win-unpacked` folder on a USB stick, or set `RH_DATA_DIR` to a known folder. |

### Where to find logs

- Electron / backend logs: `data\electron.log` next to the app, or `%LOCALAPPDATA%\RoboticsHub\data\electron.log`.
- Backend database: `data\robohub.db`.
- Uploaded files: `data\files\`.

---

## Summary checklist for packaging

- [ ] `frontend\build` exists after `yarn build`.
- [ ] `desktop\backend\dist\backend.exe` exists after PyInstaller.
- [ ] `desktop\dist\RoboticsHub.exe` and `desktop\dist\win-unpacked\RoboticsHub.exe` exist after `electron-builder`.
- [ ] Test double-clicking `win-unpacked\RoboticsHub.exe`.
- [ ] Sign in with owner credentials.
- [ ] Add a test member, open **Permissions**, and save.
- [ ] Close the window and confirm no `backend.exe` remains in Task Manager.
- [ ] Copy the `win-unpacked` folder to a USB stick and test on a school laptop.

That is the complete offline, portable app packaging process.
