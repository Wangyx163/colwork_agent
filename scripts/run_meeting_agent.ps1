param(
    [string]$Transcript = $env:COLWORK_MEETING_TRANSCRIPT,
    [string]$Extraction = "var\extractions\acisc-2026-03-02.json",
    [string]$Organization = $env:COLWORK_MEETING_ORGANIZATION,
    [string]$Coordinator = $env:COLWORK_MEETING_COORDINATOR,
    [string[]]$Participants = @(),
    [ValidateSet("bailian", "local")]
    [string]$ResultProcessing = "local",
    [ValidateSet("postgres", "sqlite")]
    [string]$DatabaseMode = "postgres",
    [string]$Database = "var\meeting.sqlite3",
    [switch]$Once,
    [switch]$UntilIdle,
    [switch]$AllowContributionAnalysis,
    [int]$MaxSteps = 100,
    [double]$PollSeconds = 2
)

$ErrorActionPreference = "Stop"
if ($Once -and $UntilIdle) {
    throw "Choose only one of -Once or -UntilIdle."
}
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
        if ($configuredResultProcessing -in @('bailian', 'local')) {
            $ResultProcessing = $configuredResultProcessing
        }
    }
}
if (-not $AllowContributionAnalysis) {
    if (Test-Path -LiteralPath $localEnvPath) {
        $contributionPolicy = Get-Content -LiteralPath $localEnvPath |
            Where-Object { $_ -match '^COLWORK_ALLOW_CONTRIBUTION_ANALYSIS=' } |
            Select-Object -First 1
        if ($contributionPolicy) {
            $AllowContributionAnalysis = (
                $contributionPolicy.Substring(
                    'COLWORK_ALLOW_CONTRIBUTION_ANALYSIS='.Length
                ).Trim() -match '^(1|true|yes)$'
            )
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
$workerArguments = @(
    "-m", "collab_agent", "agent-meeting",
    "--extraction", $extractionPath,
    "--transcript", $transcriptPath,
    "--organization", $Organization,
    "--coordinator", $Coordinator,
    "--result-processing", $ResultProcessing,
    "--poll-seconds", $PollSeconds,
    "--max-steps", $MaxSteps
)
if ($DatabaseMode -eq "postgres") {
    $workerArguments += "--postgres"
} else {
    $databasePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Database))
    $workerArguments += @("--db", $databasePath)
}
if ($Once) {
    $workerArguments += "--once"
} elseif ($UntilIdle) {
    $workerArguments += "--until-idle"
}
if ($AllowContributionAnalysis) {
    $workerArguments += "--allow-contribution-analysis"
}
foreach ($participant in $Participants) {
    $workerArguments += @("--participant", $participant)
}

& $python @workerArguments
exit $LASTEXITCODE
