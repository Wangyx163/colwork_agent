$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $projectRoot "src"

Set-Location -LiteralPath $projectRoot

$taskOutput = & $python scripts\probe_task_result.py --synthetic
if ($LASTEXITCODE -ne 0) {
    throw "Bailian task-result contract smoke failed"
}
$task = (($taskOutput -join "`n") | ConvertFrom-Json)

$finalOutput = & $python scripts\probe_final_result.py
if ($LASTEXITCODE -ne 0) {
    throw "Bailian final-organization contract smoke failed"
}
$final = (($finalOutput -join "`n") | ConvertFrom-Json)

if ($task.processing.input_hash -ne $task.invocation.input_hash) {
    throw "Task-result input_hash does not match its invocation manifest"
}
if ($final.invocation.input_hash.Length -ne 64) {
    throw "Final-organization invocation input_hash is invalid"
}

[ordered]@{
    passed = $true
    task_result = [ordered]@{
        model = $task.processing.model
        prompt_version = $task.processing.prompt_version
        repair_count = $task.processing.repair_count
        normalization_count = @($task.processing.normalization_actions).Count
        alignment = $task.result.task_alignment.status
        advice = $task.result.acceptance_advice.decision
        invocation_purpose = $task.invocation.purpose
        binary_forwarded = $false
    }
    final_organization = [ordered]@{
        model = $final.model
        prompt_version = $final.prompt_version
        repair_count = $final.repair_count
        normalization_count = @($final.normalization_actions).Count
        section_version_id = $final.section_version_id
        section_result_id = $final.section_result_id
        invocation_purpose = $final.invocation.purpose
    }
} | ConvertTo-Json -Depth 10
