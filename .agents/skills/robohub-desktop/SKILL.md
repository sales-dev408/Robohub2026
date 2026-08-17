---
name: Testing the RoboticsHub offline desktop app
description: How to build, launch, and end-to-end test the all-in-one HTML frontend + Electron + PyInstaller offline desktop app on Windows.
---

## Overview

The repo contains a FastAPI Python backend and an Electron wrapper in `desktop/`. The frontend is a single self-contained HTML file at `desktop/electron/index.html` that talks to the local backend over HTTP.

The full deliverable is built in two stages:

1. `cd desktop\backend && .venv\Scripts\python -m pip install -r requirements.txt` (if needed)
2. `.\desktop\scripts\build-python.ps1` — produces `desktop/backend/dist/backend.exe`
3. `.\desktop\scripts\build-electron.ps1` — produces `desktop/dist/RoboticsHub.exe` and `desktop/dist/win-unpacked/RoboticsHub.exe`

The unpacked folder is the easiest target for UI tests and USB sharing.

## One-time environment requirements

- Node.js + npm (Electron builder uses `npm`).
- Python 3.12 with `venv` support.
- Windows PowerShell or pwsh, with execution policy allowing scripts (`Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force`).

## Known quirks

- The PyInstaller `--onefile` build emits `api-ms-win-crt-*` warnings on minimal Windows images, but the resulting `backend.exe` still starts.
- `backend.exe` can take 10–20 seconds to extract and bind to `http://127.0.0.1:54114`. Electron waits up to 30 s; shell tests should poll similarly.
- The app seeds a default owner account `owner@robohub.local` / `Robotics2026!` on first run.
- The all-in-one HTML lives at `desktop/electron/index.html`. In Electron it is loaded from the app resources; in a browser it can be opened as a `file://` URL after pointing the Backend URL box to the running backend (`http://127.0.0.1:54114/api` for local, or a hosted Render URL for web).

## Reaching the permission dialog in a UI test

1. Launch `desktop/dist/win-unpacked/RoboticsHub.exe` (or open `desktop/electron/index.html` in a browser while `backend.exe` is running).
2. Wait for `GET http://127.0.0.1:54114/api/` to return `{"message": "Robotics Team Hub offline API"}`.
3. Log in as owner (`owner@robohub.local` / `Robotics2026!`).
4. Click the `Team` tab.
5. Create or select a member and click `Permissions`.

## Automation / API verification

- The 8 permission keys are defined in `desktop/electron/index.html` and enforced by `desktop/backend/main.py`.
- A quick smoke test script is at `desktop/backend/test_frontend_api.py` and checks login, dashboard, channels, messages, user creation, and permission updates.

## Cleanup check

After closing the window, verify no lingering processes:

```powershell
Get-Process backend, RoboticsHub -ErrorAction SilentlyContinue
```

## Devin Secrets Needed

None. The offline build is self-contained; the owner account and SQLite DB are created locally. No cloud credentials or API keys are used for this flow.
