# Build the Python backend as a single portable executable.
# Run from the repository root in PowerShell:
#   .\desktop\scripts\build-python.ps1

$ErrorActionPreference = "Stop"
$BackendDir = "$PSScriptRoot\..\backend"
$VenvDir = "$BackendDir\.venv"

Set-Location $BackendDir

# Create a local virtual environment.
if (!(Test-Path $VenvDir)) {
    python -m venv $VenvDir
}

& "$VenvDir\Scripts\pip.exe" install --upgrade pip
& "$VenvDir\Scripts\pip.exe" install -r requirements.txt

# PyInstaller command (one-file, no console).
# --clean prevents stale builds.
# --noconsole hides the black terminal window when end users double-click.
& "$VenvDir\Scripts\pyinstaller.exe" `
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

Write-Host "Backend executable built at: $BackendDir\dist\backend.exe"
