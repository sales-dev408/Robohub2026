# Build the Python backend as a single portable executable.
# Run from the repository root in PowerShell:
#   .\desktop\scripts\build-python.ps1

$ErrorActionPreference = "Continue"
$BackendDir = "$PSScriptRoot\..\backend"
$VenvDir = "$BackendDir\.venv"

Set-Location $BackendDir

# Create a local virtual environment.
if (!(Test-Path $VenvDir)) {
    python -m venv $VenvDir
}

& "$VenvDir\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& "$VenvDir\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# PyInstaller command (one-file, no console).
# --clean prevents stale builds.
# --noconsole hides the black terminal window when end users double-click.
& "$VenvDir\Scripts\python.exe" -m PyInstaller `
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
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host "Backend executable built at: $BackendDir\dist\backend.exe"
