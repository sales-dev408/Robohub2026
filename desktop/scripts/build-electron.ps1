# Build the Electron wrapper (one portable .exe and an unpacked folder).
# Assumes:
#   - frontend/build already exists (run npm/yarn build in frontend/)
#   - desktop/backend/dist/backend.exe already exists (run build-python.ps1)
# Run from the repository root in PowerShell:
#   .\desktop\scripts\build-electron.ps1

$ErrorActionPreference = "Stop"
$ElectronDir = "$PSScriptRoot\..\electron"
$DistDir = "$ElectronDir\..\dist"

Set-Location $ElectronDir

if (!(Test-Path node_modules)) {
    npm install
}

# Build both the single portable .exe and the unpacked folder.
npx electron-builder --win portable --win dir

Write-Host "Electron artifacts in: $DistDir"
Write-Host "  - Single portable executable: $DistDir\RoboticsHub.exe"
Write-Host "  - Unpacked folder (USB-friendly): $DistDir\win-unpacked\RoboticsHub.exe"
