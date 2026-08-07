param(
    [string]$Transcript = $env:COLWORK_MEETING_TRANSCRIPT,
    [string]$Extraction = "var\extractions\acisc-2026-03-02.json",
    [string]$Organization = $env:COLWORK_MEETING_ORGANIZATION,
    [string]$Coordinator = $env:COLWORK_MEETING_COORDINATOR,
    [string[]]$Participants = @(),
    [ValidateSet("bailian", "local", "disabled")]
    [string]$ResultProcessing = "local",
    [ValidateSet("postgres", "sqlite")]
    [string]$DatabaseMode = "postgres",
    [string]$Database = "var\meeting.sqlite3",
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$localEnvPath = Join-Path $projectRoot ".env.local"
if (-not $PSBoundParameters.ContainsKey("ResultProcessing") -and
    (Test-Path -LiteralPath $localEnvPath)) {
    $resultProcessingPolicy = Get-Content -LiteralPath $localEnvPath |
        Where-Object { $_ -match '^COLWORK_RESULT_PROCESSING_MODE=' } |
        Select-Object -First 1
    if ($resultProcessingPolicy) {
        $configuredResultProcessing = $resultProcessingPolicy.Substring(
            'COLWORK_RESULT_PROCESSING_MODE='.Length
        ).Trim().ToLowerInvariant()
        if ($configuredResultProcessing -in @('bailian', 'local', 'disabled')) {
            $ResultProcessing = $configuredResultProcessing
        }
    }
}
if (-not $Transcript) {
    throw "Transcript is required; set COLWORK_MEETING_TRANSCRIPT or pass -Transcript."
}
if (-not $Organization) {
    $Organization = "ACISC"
}
if (-not $Coordinator) {
    $Coordinator = "Coordinator"
}
if ($Participants.Count -eq 0 -and $env:COLWORK_MEETING_PARTICIPANTS) {
    $Participants = @(
        $env:COLWORK_MEETING_PARTICIPANTS.Split(',') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
}
if ($Participants.Count -eq 0) {
    throw "At least one participant is required; pass -Participants or set COLWORK_MEETING_PARTICIPANTS."
}
$transcriptPath = (Resolve-Path -LiteralPath $Transcript).Path
$extractionPath = (Resolve-Path -LiteralPath (Join-Path $projectRoot $Extraction)).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $projectRoot "src"

Set-Location -LiteralPath $projectRoot
$arguments = @(
    "-m", "collab_agent", "serve-meeting",
    "--extraction", $extractionPath,
    "--transcript", $transcriptPath,
    "--organization", $Organization,
    "--coordinator", $Coordinator,
    "--result-processing", $ResultProcessing,
    "--host", "127.0.0.1",
    "--port", $Port
)
if ($DatabaseMode -eq "postgres") {
    $arguments += "--postgres"
} else {
    $databasePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Database))
    $arguments += @("--db", $databasePath)
}
foreach ($participant in $Participants) {
    $arguments += @("--participant", $participant)
}
& $python @arguments
exit $LASTEXITCODE
