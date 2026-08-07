[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    & python -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Python virtual environment creation failed"
    }
}

& $venvPython -m pip install -e "$projectRoot[postgres]"
if ($LASTEXITCODE -ne 0) {
    throw "Project dependency installation failed"
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup_postgres.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL setup failed"
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "verify_postgres.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL verification failed"
}
