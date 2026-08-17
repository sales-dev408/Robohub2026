# Full offline build pipeline.
# Run from the repository root in PowerShell:
#   .\desktop\scripts\build-all.ps1

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path "$PSScriptRoot\..\.."

Set-Location $RepoRoot

# 1. Build the React frontend (uses .env files already present).
Set-Location "$RepoRoot\frontend"
if (!(Test-Path node_modules)) {
    yarn install
    if ($LASTEXITCODE -ne 0) { throw "yarn install failed" }
}
yarn build
if ($LASTEXITCODE -ne 0) { throw "yarn build failed" }

# 2. Build the Python backend as a single .exe.
Set-Location $RepoRoot
. "$PSScriptRoot\build-python.ps1"

# 3. Package Electron.
. "$PSScriptRoot\build-electron.ps1"

Write-Host "Done. Distribution is in desktop/dist/"
