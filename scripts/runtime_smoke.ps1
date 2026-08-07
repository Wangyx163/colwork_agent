param(
    [string]$BaseUrl = "http://127.0.0.1:8767"
)

$ErrorActionPreference = "Stop"

function Invoke-ApiPost {
    param(
        [string]$Path,
        [hashtable]$Body,
        [string]$Token = ""
    )
    $headers = @{}
    if ($Token) {
        $headers.Authorization = "Bearer $Token"
    }
    Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUrl$Path" `
        -Headers $headers `
        -ContentType "application/json; charset=utf-8" `
        -Body ($Body | ConvertTo-Json -Depth 20)
}

function Get-State {
    param(
        [string]$Surface,
        [string]$Token
    )
    Invoke-RestMethod `
        -Method Get `
        -Uri "$BaseUrl/api/state?surface=$Surface" `
        -Headers @{ Authorization = "Bearer $Token" }
}

function New-MessageId {
    "runtime-smoke-$([guid]::NewGuid().ToString('N'))"
}

function Wait-TaskProcessing {
    param(
        [string]$TaskId,
        [string]$CoordinatorToken
    )
    for ($attempt = 0; $attempt -lt 240; $attempt++) {
        $state = Get-State -Surface "manage" -Token $CoordinatorToken
        $task = $state.tasks | Where-Object action_item_id -eq $TaskId
        if ($task.latest_version.processing_status -in @("READY", "FAILED")) {
            return $task.latest_version
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Task result processing did not finish"
}

function Wait-Final {
    param(
        [string]$CoordinatorToken,
        [string]$NotFinalId = ""
    )
    for ($attempt = 0; $attempt -lt 240; $attempt++) {
        $state = Get-State -Surface "manage" -Token $CoordinatorToken
        if (
            $state.final -and
            $state.final.final_deliverable_id -ne $NotFinalId -and
            $state.pending_approvals.Count -eq 1
        ) {
            return $state
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Final organization did not finish"
}

$actors = (Invoke-RestMethod -Uri "$BaseUrl/api/session/actors").actors
$coordinator = $actors | Where-Object { $_.roles -contains "COORDINATOR" } | Select-Object -First 1
$participant = $actors | Where-Object { $_.roles -contains "PARTICIPANT" } | Select-Object -First 1
if (-not $coordinator -or -not $participant) {
    throw "Smoke scenario requires one coordinator and one participant"
}
$coordinatorToken = (
    Invoke-ApiPost -Path "/api/session" -Body @{ actor_id = $coordinator.actor_id }
).token
$participantToken = (
    Invoke-ApiPost -Path "/api/session" -Body @{ actor_id = $participant.actor_id }
).token

$state = Get-State -Surface "manage" -Token $coordinatorToken
$kept = $state.tasks | Select-Object -First 1
$ignored = $state.tasks | Select-Object -Skip 1
$teamRequired = (Get-Date).AddDays(2).ToString("o")

# A participant cannot use a coordinator route, and the denied request is
# expected to be observable in SIG-AUTH rather than silently disappearing.
$forbiddenStatus = 0
try {
    Invoke-ApiPost `
        -Path "/api/action-items/$($kept.action_item_id)/dispatch" `
        -Token $participantToken `
        -Body @{
            actor_id = $coordinator.actor_id
            owner_actor_id = $participant.actor_id
            collaborator_actor_ids = @()
            assignment_message = "Unauthorized dispatch attempt"
            message_id = New-MessageId
        } | Out-Null
} catch {
    if (-not $_.Exception.Response) {
        throw
    }
    $forbiddenStatus = [int]$_.Exception.Response.StatusCode
}
if ($forbiddenStatus -ne 403) {
    throw "Participant coordinator-route denial did not return HTTP 403"
}

Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/revise" `
    -Token $coordinatorToken `
    -Body @{
        title = $kept.title
        deliverable = $kept.proposal_metadata.deliverable
        acceptance_criteria = "The body contains traceable conclusions"
        work_requirements = $kept.proposal_metadata.deliverable
        management_review_policy = "Coordinator checks task-result alignment"
        priority = "P0"
        team_required_by_sim_time = $teamRequired
        message_id = New-MessageId
    } | Out-Null
Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/dispatch" `
    -Token $coordinatorToken `
    -Body @{
        owner_actor_id = $participant.actor_id
        collaborator_actor_ids = @()
        assignment_message = "Run the isolated runtime smoke delivery"
        message_id = New-MessageId
    } | Out-Null
foreach ($task in $ignored) {
    Invoke-ApiPost `
        -Path "/api/action-items/$($task.action_item_id)/ignore" `
        -Token $coordinatorToken `
        -Body @{ reason = "Runtime smoke keeps one task"; message_id = New-MessageId } |
        Out-Null
}

Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/assignment-response" `
    -Token $participantToken `
    -Body @{
        actor_id = $coordinator.actor_id
        decision = "ACCEPT"
        response_message = "Accepted by the authenticated participant"
        message_id = New-MessageId
    } |
    Out-Null
$participantProjection = Get-State -Surface "tasks" -Token $participantToken
$participantTask = $participantProjection.tasks |
    Where-Object action_item_id -eq $kept.action_item_id |
    Select-Object -First 1
if (
    $participantTask.proposal_metadata.PSObject.Properties.Name -contains
    "management_review_policy"
) {
    throw "Participant projection exposed the management review policy"
}
if ($participantTask.owner_actor_id -ne $participant.actor_id) {
    throw "Body actor_id spoofing changed the authenticated assignee"
}
Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/signal" `
    -Token $participantToken `
    -Body @{ signal_type = "ON_TRACK"; note = "Work started"; message_id = New-MessageId } |
    Out-Null
$assistance = Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/assistance" `
    -Token $participantToken `
    -Body @{
        target_actor_id = $coordinator.actor_id
        category = "DECISION"
        summary = "Please confirm the smoke delivery structure"
        message_id = New-MessageId
    }
Invoke-ApiPost `
    -Path "/api/assistance/$($assistance.assistance_request_id)/acknowledge" `
    -Token $coordinatorToken `
    -Body @{ message_id = New-MessageId } | Out-Null
Invoke-ApiPost `
    -Path "/api/assistance/$($assistance.assistance_request_id)/resolve" `
    -Token $coordinatorToken `
    -Body @{ resolution_summary = "Structure confirmed"; message_id = New-MessageId } |
    Out-Null

$firstVersion = Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/submit" `
    -Token $participantToken `
    -Body @{
        message_id = New-MessageId
        delivery = @{
            summary = "Runtime result version one"
            content = "Isolated smoke-test content, not a business conclusion."
            links = @()
            files = @()
        }
    }
$processedFirst = Wait-TaskProcessing `
    -TaskId $kept.action_item_id `
    -CoordinatorToken $coordinatorToken
Invoke-ApiPost `
    -Path "/api/artifact-versions/$($firstVersion.version_id)/review" `
    -Token $coordinatorToken `
    -Body @{
        approve = $true
        comment = "First task result accepted"
        completion_report = "Accepted result version one"
        message_id = New-MessageId
    } | Out-Null
$firstFinalState = Wait-Final -CoordinatorToken $coordinatorToken
$firstFinalId = $firstFinalState.final.final_deliverable_id
$participantBeforeRelease = Get-State -Surface "tasks" -Token $participantToken
if ($participantBeforeRelease.final) {
    throw "Participant saw an unreleased final"
}
$firstApproval = $firstFinalState.pending_approvals[0]
Invoke-ApiPost `
    -Path "/api/approvals/$($firstApproval.approval_id)" `
    -Token $coordinatorToken `
    -Body @{ approve = $false; comment = "Submit a revised task result before release" } | Out-Null

$secondVersion = Invoke-ApiPost `
    -Path "/api/action-items/$($kept.action_item_id)/submit" `
    -Token $participantToken `
    -Body @{
        message_id = New-MessageId
        delivery = @{
            summary = "Runtime result version two"
            content = "Revised isolated smoke-test content."
            links = @()
            files = @()
        }
    }
$processedSecond = Wait-TaskProcessing `
    -TaskId $kept.action_item_id `
    -CoordinatorToken $coordinatorToken
Invoke-ApiPost `
    -Path "/api/artifact-versions/$($secondVersion.version_id)/review" `
    -Token $coordinatorToken `
    -Body @{
        approve = $true
        comment = "Revised task result accepted"
        completion_report = "Accepted result version two"
        message_id = New-MessageId
    } | Out-Null
$replacementState = Wait-Final `
    -CoordinatorToken $coordinatorToken `
    -NotFinalId $firstFinalId
$replacementFinalId = $replacementState.final.final_deliverable_id
$replacementApproval = $replacementState.pending_approvals[0]
Invoke-ApiPost `
    -Path "/api/approvals/$($replacementApproval.approval_id)" `
    -Token $coordinatorToken `
    -Body @{ approve = $true; comment = "" } | Out-Null

$participantAfterRelease = Get-State -Surface "tasks" -Token $participantToken
if (
    $participantAfterRelease.episode.status -ne "ARCHIVED" -or
    $participantAfterRelease.final.status -ne "RELEASED"
) {
    throw "Released final was not archived or visible to the participant"
}
$diagnostics = Get-State -Surface "diagnostics" -Token $coordinatorToken
$authorizationRejections = $diagnostics.report.node_signals.'SIG-AUTH-001'.authorization_rejections
if ($authorizationRejections -lt 1) {
    throw "Denied HTTP request was not present in SIG-AUTH"
}
$projectionApplications = $diagnostics.report.node_signals.'SIG-AUTH-001'.field_projection_applications
if ($projectionApplications -lt 1) {
    throw "Participant field projection was not present in SIG-AUTH"
}

[ordered]@{
    passed = $true
    action_item_id = $kept.action_item_id
    first_version_status = $processedFirst.processing_status
    second_version_status = $processedSecond.processing_status
    first_final_id = $firstFinalId
    replacement_final_id = $replacementFinalId
    episode_status = $participantAfterRelease.episode.status
    final_status = $participantAfterRelease.final.status
    authorization_rejections = $authorizationRejections
    field_projection_applications = $projectionApplications
    body_actor_id_spoofing_ignored = $true
    participant_projection_hides_management_policy = $true
} | ConvertTo-Json -Depth 10
