[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pgCtl = Join-Path $projectRoot "var\postgresql-18\pgsql\bin\pg_ctl.exe"
$dataDirectory = Join-Path $projectRoot "var\postgres-data"

if (-not (Test-Path -LiteralPath $pgCtl)) {
    throw "Portable PostgreSQL binaries are missing"
}
if (-not (Test-Path -LiteralPath (Join-Path $dataDirectory "PG_VERSION"))) {
    throw "Portable PostgreSQL cluster is not initialized"
}

& $pgCtl -D $dataDirectory status *> $null
if ($LASTEXITCODE -eq 0) {
    & $pgCtl -D $dataDirectory -m fast -w stop
}
