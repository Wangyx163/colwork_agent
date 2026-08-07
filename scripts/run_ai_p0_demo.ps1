param(
    [string]$Report = "var\ai-p0-report.json",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $projectRoot "src"
Set-Location -LiteralPath $projectRoot

if (-not $SkipTests) {
    & $python -m unittest tests.test_agent_worker tests.test_context_budget -v
    if ($LASTEXITCODE -ne 0) {
        throw "AI-P0 focused tests failed"
    }
}

& $python -m collab_agent eval-ai-p0 --fresh --report $Report
if ($LASTEXITCODE -ne 0) {
    throw "AI-P0 Harness failed"
}

$reportPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Report))
Write-Host "AI-P0 passed. Report: $reportPath"
