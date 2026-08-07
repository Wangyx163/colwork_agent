[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env.local"
$psql = Join-Path $projectRoot "var\postgresql-18\pgsql\bin\psql.exe"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw ".env.local is missing; run scripts/setup_postgres.ps1 first"
}
if (-not (Test-Path -LiteralPath $psql)) {
    throw "Portable PostgreSQL 18 command-line tools are missing"
}

$databaseUrlLine = Get-Content -LiteralPath $envFile -Encoding utf8 |
    Where-Object { $_ -like "DATABASE_URL=*" } |
    Select-Object -First 1
if (-not $databaseUrlLine) {
    throw "DATABASE_URL is missing from .env.local"
}
$databaseUrl = $databaseUrlLine.Substring("DATABASE_URL=".Length)

& $psql -X -v ON_ERROR_STOP=1 $databaseUrl -c "SELECT current_database(), current_user, current_setting('server_version') AS server_version, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser"
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL verification failed"
}
& $psql -X -v ON_ERROR_STOP=1 $databaseUrl -c "SELECT count(*) AS public_tables FROM information_schema.tables WHERE table_schema = 'public'"
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL schema verification failed"
}
& $psql -X -v ON_ERROR_STOP=1 $databaseUrl -c "SELECT count(*) AS append_only_triggers FROM information_schema.triggers WHERE event_object_table = 'audit_events' AND action_timing = 'BEFORE' AND event_manipulation IN ('UPDATE','DELETE')"
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL audit trigger verification failed"
}
& $psql -X -v ON_ERROR_STOP=1 $databaseUrl -c "SELECT status AS episode_status, (SELECT count(*) FROM audit_events) AS audit_events, (SELECT count(*) FROM mock_im_messages) AS visible_messages FROM episodes WHERE episode_id = 'episode_p0'"
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL P0 state verification failed"
}
