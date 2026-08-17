# Full offline build pipeline.
# Run from the repository root in PowerShell:
#   .\desktop\scripts\build-all.ps1
#
# This builds the all-in-one HTML frontend, packages the Python backend,
# and wraps both in an Electron portable .exe.

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path "$PSScriptRoot\..\.."

Set-Location $RepoRoot

# 1. Build the Python backend as a single .exe.
. "$PSScriptRoot\build-python.ps1"

# 2. Package the Electron wrapper (which loads the all-in-one index.html).
. "$PSScriptRoot\build-electron.ps1"

Write-Host "Done. Distribution is in desktop/dist/"
