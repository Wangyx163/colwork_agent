[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot "var"
$archivePath = Join-Path $runtimeRoot "downloads\postgresql-18.4-2-windows-x64-binaries.zip"
$archiveUrl = "https://get.enterprisedb.com/postgresql/postgresql-18.4-2-windows-x64-binaries.zip"
$archiveSha256 = "02E239529ED7833D169F98D915D3FEFFE0813264B08B3AE353E78E8B9C97E1A6"
$postgresRoot = Join-Path $runtimeRoot "postgresql-18\pgsql"
$postgresBin = Join-Path $postgresRoot "bin"
$psql = Join-Path $postgresBin "psql.exe"
$createdb = Join-Path $postgresBin "createdb.exe"
$initdb = Join-Path $postgresBin "initdb.exe"
$pgCtl = Join-Path $postgresBin "pg_ctl.exe"
$pgIsReady = Join-Path $postgresBin "pg_isready.exe"
$dataDirectory = Join-Path $runtimeRoot "postgres-data"
$logFile = Join-Path $runtimeRoot "postgres.log"
$envFile = Join-Path $projectRoot ".env.local"
$schemaFile = Join-Path $projectRoot "db\postgres_schema.sql"
$passwordFile = Join-Path $runtimeRoot ".postgres-init-pw"
$appRole = "colwork_app"
$databaseName = "colwork_agent"
$port = 55432

function New-LocalPassword {
    $bytes = [byte[]]::new(24)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return "Clw-A9!" + [BitConverter]::ToString($bytes).Replace("-", "")
}

function Invoke-Psql {
    param(
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $previousPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = $Password
        & $psql -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -p $port -U $User -d $Database @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "psql failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:PGPASSWORD = $previousPassword
    }
}

if (-not (Test-Path -LiteralPath $psql)) {
    $downloadDirectory = Split-Path -Parent $archivePath
    New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
    if (-not (Test-Path -LiteralPath $archivePath)) {
        Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $archivePath
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
    if ($actualHash -ne $archiveSha256) {
        throw "PostgreSQL archive hash mismatch; refusing to extract"
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath (Join-Path $runtimeRoot "postgresql-18") -Force
}

if (-not (Test-Path -LiteralPath $psql)) {
    throw "Portable PostgreSQL binaries are missing at $postgresBin"
}

$appPassword = $null
if (Test-Path -LiteralPath $envFile) {
    $databaseUrlLine = Get-Content -LiteralPath $envFile -Encoding utf8 |
        Where-Object { $_ -like "DATABASE_URL=*" } |
        Select-Object -First 1
    if ($databaseUrlLine -match '^DATABASE_URL=postgresql://colwork_app:([^@]+)@127\.0\.0\.1:55432/colwork_agent$') {
        $appPassword = [Uri]::UnescapeDataString($Matches[1])
    }
}

$initializedByThisRun = $false
$superPassword = $null
if (-not (Test-Path -LiteralPath (Join-Path $dataDirectory "PG_VERSION"))) {
    if (Test-Path -LiteralPath $dataDirectory) {
        $existingItems = @(Get-ChildItem -LiteralPath $dataDirectory -Force)
        if ($existingItems.Count -gt 0) {
            throw "PostgreSQL data directory exists but is not initialized: $dataDirectory"
        }
    }
    $superPassword = New-LocalPassword
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    [IO.File]::WriteAllText($passwordFile, "$superPassword`n", [Text.UTF8Encoding]::new($false))
    try {
        & $initdb -D $dataDirectory -U postgres --encoding=UTF8 --locale=C --auth-local=scram-sha-256 --auth-host=scram-sha-256 --pwfile=$passwordFile
        if ($LASTEXITCODE -ne 0) {
            throw "initdb failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        if (Test-Path -LiteralPath $passwordFile) {
            Remove-Item -LiteralPath $passwordFile -Force
        }
    }
    Add-Content -LiteralPath (Join-Path $dataDirectory "postgresql.conf") -Encoding utf8 -Value @"

# colwork_agent local portable instance
listen_addresses = '127.0.0.1'
port = $port
max_connections = 30
"@
    $initializedByThisRun = $true
}
elseif (-not $appPassword) {
    throw "The project cluster exists, but .env.local has no usable credential. Refusing to overwrite it."
}

& $pgIsReady -h 127.0.0.1 -p $port -d postgres *> $null
if ($LASTEXITCODE -ne 0) {
    & $pgCtl -D $dataDirectory -l $logFile -w start
    if ($LASTEXITCODE -ne 0) {
        throw "pg_ctl failed to start the local cluster"
    }
}

if ($initializedByThisRun) {
    $appPassword = New-LocalPassword
    $escapedPassword = $appPassword.Replace("'", "''")
    Invoke-Psql -Database "postgres" -User "postgres" -Password $superPassword -Arguments @(
        "-c", "CREATE ROLE $appRole LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$escapedPassword'"
    )
    $previousPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = $superPassword
        & $createdb -h 127.0.0.1 -p $port -U postgres -O $appRole $databaseName
        if ($LASTEXITCODE -ne 0) {
            throw "createdb failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:PGPASSWORD = $previousPassword
    }
    $databaseUrl = "postgresql://${appRole}:$([Uri]::EscapeDataString($appPassword))@127.0.0.1:$port/$databaseName"
    [IO.File]::WriteAllText($envFile, "DATABASE_URL=$databaseUrl`n", [Text.UTF8Encoding]::new($false))
}

$tableExists = Invoke-Psql -Database $databaseName -User $appRole -Password $appPassword -Arguments @(
    "-tAc", "SELECT to_regclass('public.organizations') IS NOT NULL"
)
if (($tableExists | Out-String).Trim() -ne "t") {
    Invoke-Psql -Database $databaseName -User $appRole -Password $appPassword -Arguments @(
        "-f", $schemaFile
    )
}

$version = Invoke-Psql -Database $databaseName -User $appRole -Password $appPassword -Arguments @(
    "-tAc", "SHOW server_version"
)
$tableCount = Invoke-Psql -Database $databaseName -User $appRole -Password $appPassword -Arguments @(
    "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
)
$isSuperuser = Invoke-Psql -Database $databaseName -User $appRole -Password $appPassword -Arguments @(
    "-tAc", "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
)

[pscustomobject]@{
    PostgreSQL = ($version | Out-String).Trim()
    Mode = "portable"
    Status = "Running"
    Host = "127.0.0.1"
    Port = $port
    Database = $databaseName
    Role = $appRole
    RoleIsSuperuser = (($isSuperuser | Out-String).Trim() -eq "t")
    PublicTables = [int](($tableCount | Out-String).Trim())
    EnvironmentFile = $envFile
} | ConvertTo-Json
