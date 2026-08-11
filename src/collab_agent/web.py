from __future__ import annotations

import json
import os
import re
import socket
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import (
    AuthorizationService,
    Principal,
    PrincipalError,
    VirtualSessionPrincipalProvider,
)
from . import compound_store
from .attachments import MAX_ATTACHMENT_COUNT, MAX_TOTAL_ATTACHMENT_BYTES
from .metrics import build_report
from .memory_lexicon import (
    canonical_topic,
    memory_lexicon_payload,
    projected_value,
)
from .models import ASSIGNMENT_RETURN_REASONS, OTHER_RETURN_REASON, parse_time


# Base64 inflates the attachment ceiling by ~4/3; the rest covers JSON framing
# and the text fields that travel with a delivery.
MAX_REQUEST_BYTES = MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3 + 1024 * 1024


class RequestTooLarge(Exception):
    """The request body exceeds the ceiling and was never read into memory."""


class SingleInstanceHTTPServer(HTTPServer):
    """Bind a workbench port exclusively, including on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()
from .service import NOTIFICATION_EFFECT_TYPES, CoordinationService
from .user_messages import user_message


# The server-rendered workbench used to live here. Every surface it drew
# is now a React page in web/, built into src/collab_agent/static/console
# and served by static_assets.py; this module keeps the API those pages
# call. Deleted rather than left dormant, because a second page nothing
# serves is the kind of thing a reader edits by mistake.


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _agent_trace(service: CoordinationService) -> list[dict[str, Any]]:
    event_types = (
        "TaskResultProcessingStarted",
        "TaskResultProcessingSucceeded",
        "TaskResultProcessingFailed",
        "TaskResultProcessingRecovered",
        "FinalOrganizationQueued",
        "FinalOrganizationStarted",
        "FinalOrganizationSucceeded",
        "FinalOrganizationRetryScheduled",
        "QuestionBallotDraftPrepared",
        "CollaborationHintContextBuilt",
        "AgentStepCompleted",
        "ProcessRecovered",
    )
    placeholders = ", ".join("?" for _ in event_types)
    rows = service.db.all(
        "SELECT sequence_no, aggregate_id, event_type, payload, sim_time "
        "FROM audit_events WHERE run_id = ? "
        f"AND event_type IN ({placeholders}) ORDER BY sequence_no",
        (service.run_id, *event_types),
    )
    trace: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode_json(row["payload"])
        invocation = payload.get("invocation") or {}
        if not invocation and payload.get("generation"):
            generation = payload["generation"]
            invocation = {
                "purpose": "QUESTION_BALLOT_DRAFT",
                "output_status": "SUCCEEDED",
                "model": generation.get("model"),
                "context": generation.get("context_manifest") or {},
                "usage": generation.get("usage") or {},
            }
        context = invocation.get("context") or {}
        usage = invocation.get("usage") or {}
        trace.append(
            {
                "sequence_no": int(row["sequence_no"]),
                "aggregate_id": row["aggregate_id"],
                "event_type": row["event_type"],
                "sim_time": row["sim_time"],
                "purpose": invocation.get("purpose"),
                "output_status": invocation.get("output_status"),
                "model": invocation.get("model"),
                "estimated_input_tokens": context.get(
                    "estimated_input_tokens"
                ),
                "token_budget": context.get("token_budget"),
                "included_refs": context.get("included_refs") or [],
                "omitted_refs": context.get("omitted_refs") or [],
                "truncation_strategy": context.get("truncation_strategy") or [],
                "total_tokens": usage.get("total_tokens"),
                "step_kind": payload.get("step_kind"),
            }
        )
    return trace


def _task_activity(
    service: CoordinationService,
    action: dict[str, Any],
    *,
    audit_sequences: dict[str, int],
) -> list[dict[str, Any]]:
    db = service.db
    action_id = action["action_item_id"]
    actor_names = {
        row["actor_id"]: row["display_name"]
        for row in db.all("SELECT actor_id, display_name FROM actors")
    }
    activity: list[dict[str, Any]] = []

    assignments = db.all(
        "SELECT * FROM action_item_assignments WHERE action_item_id = ? "
        "ORDER BY definition_version, assigned_sim_time, actor_id",
        (action_id,),
    )
    for assignment in assignments:
        role_label = (
            "主负责人"
            if assignment["assignment_role"] == "OWNER"
            else "协作者"
        )
        activity.append(
            {
                "kind": "ASSIGNMENT",
                "title": f"派发任务 v{assignment['definition_version']}",
                "detail": (
                    f"派发为{role_label}"
                    + (
                        f"；留言：{assignment['assignment_message']}"
                        if assignment["assignment_message"]
                        else ""
                    )
                ),
                "status": "PENDING",
                "actor": actor_names.get(
                    assignment["actor_id"], assignment["actor_id"]
                ),
                "actor_id": assignment["actor_id"],
                "sim_time": assignment["assigned_sim_time"],
                "sequence_no": audit_sequences.get(action_id, 0),
            }
        )
        response_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ActionItemAssignmentAccepted',"
            "'ActionItemAssignmentReturned') ORDER BY sequence_no",
            (service.run_id, assignment["assignment_id"]),
        )
        for event in response_events:
            payload = _decode_json(event["payload"])
            returned = event["event_type"] == "ActionItemAssignmentReturned"
            activity.append(
                {
                    "kind": "ASSIGNMENT",
                    "title": "退回任务定义" if returned else "接受任务派发",
                    "detail": payload.get("reason")
                    or payload.get("message")
                    or ("需要负责人重改" if returned else f"以{role_label}身份接受"),
                    "status": "RETURNED" if returned else "ACCEPTED",
                    "actor": actor_names.get(
                        assignment["actor_id"], assignment["actor_id"]
                    ),
                    "actor_id": assignment["actor_id"],
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                }
            )

    revisions = db.all(
        "SELECT * FROM commitment_revisions WHERE action_item_id = ? "
        "ORDER BY revision_no",
        (action_id,),
    )
    for revision in revisions:
        activity.append(
            {
                "kind": "COMMITMENT",
                "title": (
                    "确认任务承诺"
                    if int(revision["revision_no"]) == 1
                    else "更新任务承诺"
                ),
                "detail": f'承诺截止：{revision["promised_deadline_sim_time"]}',
                "status": revision["status"],
                # The same instant the prose carries, kept apart from it: the
                # schedule bar draws a superseded promise where it used to sit,
                # and digging a date back out of a sentence would break the
                # first time the sentence is reworded.
                "promised_deadline_sim_time": revision[
                    "promised_deadline_sim_time"
                ],
                "revision_no": int(revision["revision_no"]),
                "actor": actor_names.get(
                    revision["owner_actor_id"], revision["owner_actor_id"]
                ),
                "actor_id": revision["owner_actor_id"],
                "sim_time": revision["created_sim_time"],
                "sequence_no": audit_sequences.get(
                    revision["commitment_revision_id"], 0
                ),
            }
        )

    versions = db.all(
        "SELECT * FROM artifact_versions WHERE action_item_id = ? "
        "ORDER BY received_sequence",
        (action_id,),
    )
    for version in versions:
        payload = _decode_json(version["payload"])
        errors = _decode_json(version["validation_errors"])
        missing = errors.get("missing_fields", [])
        is_collaborator_submission = bool(
            version["submitted_by_actor_id"]
            and version["submitted_by_actor_id"] != action.get("owner_actor_id")
        )
        if version["validation_status"] == "FAILED":
            status = "VALIDATION_FAILED"
            outcome = "校验未通过：" + "、".join(missing)
        elif version["review_status"] == "PENDING":
            status = "PENDING_ACCEPTANCE"
            outcome = "格式校验通过，等待负责人验收"
        elif version["review_status"] == "REJECTED":
            status = "REJECTED"
            outcome = f'负责人退回：{version["review_comment"] or "未填写原因"}'
        elif version["review_status"] == "ACCEPTED":
            status = "ACCEPTED"
            outcome = f'负责人验收通过：{version["review_comment"] or "无补充意见"}'
        elif is_collaborator_submission:
            status = "CONTRIBUTION_RECEIVED"
            outcome = "协作贡献已接收，等待任务负责人处理"
        else:
            status = version["validation_status"]
            outcome = "格式校验通过"
        summary = payload.get("summary") or payload.get("content") or "未填写摘要"
        receipt_event = db.one(
            "SELECT sequence_no, sim_time FROM audit_events WHERE run_id = ? "
            "AND aggregate_id = ? AND event_type = 'ArtifactVersionReceived' "
            "ORDER BY sequence_no LIMIT 1",
            (service.run_id, version["version_id"]),
        )
        activity.append(
            {
                "kind": "DELIVERY",
                "title": (
                    f'协作者提交贡献版本 {version["received_sequence"]}'
                    if is_collaborator_submission
                    else f'提交交付版本 {version["received_sequence"]}'
                ),
                "detail": f'{summary}；{outcome}',
                "status": status,
                "actor": actor_names.get(
                    version["submitted_by_actor_id"]
                    or action.get("owner_actor_id"),
                    version["submitted_by_actor_id"]
                    or action.get("owner_actor_id"),
                ),
                "actor_id": version["submitted_by_actor_id"]
                or action.get("owner_actor_id"),
                "sim_time": (
                    receipt_event["sim_time"]
                    if receipt_event
                    else version["received_sim_time"]
                ),
                "sequence_no": (
                    receipt_event["sequence_no"] if receipt_event else 0
                ),
                "version_id": version["version_id"],
            }
        )

        decision_titles = {
            "ArtifactContributionIncluded": "负责人已纳入协作资料",
            "ArtifactContributionRevisionRequested": "负责人要求补充协作成果",
            "ArtifactContributionPromotedToFinalCandidate": "负责人将贡献送入任务验收",
            "ArtifactContributionReclassified": "协作成果已恢复为贡献版本",
        }
        decision_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ArtifactContributionIncluded',"
            "'ArtifactContributionRevisionRequested',"
            "'ArtifactContributionPromotedToFinalCandidate',"
            "'ArtifactContributionReclassified') ORDER BY sequence_no",
            (service.run_id, version["version_id"]),
        )
        for event in decision_events:
            decision_payload = _decode_json(event["payload"])
            actor_id = decision_payload.get("decided_by")
            activity.append(
                {
                    "kind": "CONTRIBUTION",
                    "title": decision_titles[event["event_type"]],
                    "detail": decision_payload.get("comment")
                    or decision_payload.get("reason")
                    or "协作成果状态已更新",
                    "status": decision_payload.get("contribution_status")
                    or "AWAITING_OWNER",
                    "actor": actor_names.get(actor_id, actor_id or "SYSTEM"),
                    "actor_id": actor_id,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                    "version_id": version["version_id"],
                }
            )

        review_titles = {
            "ArtifactVersionAcceptedByCoordinator": "任务成果验收通过",
            "ArtifactVersionReturnedForRevision": "任务成果被退回",
        }
        review_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ArtifactVersionAcceptedByCoordinator',"
            "'ArtifactVersionReturnedForRevision') ORDER BY sequence_no",
            (service.run_id, version["version_id"]),
        )
        for event in review_events:
            review_payload = _decode_json(event["payload"])
            reviewer = review_payload.get("reviewed_by")
            activity.append(
                {
                    "kind": "REVIEW",
                    "title": review_titles[event["event_type"]],
                    "detail": review_payload.get("comment") or "未补充意见",
                    "status": (
                        "ACCEPTED"
                        if event["event_type"]
                        == "ArtifactVersionAcceptedByCoordinator"
                        else "REJECTED"
                    ),
                    "actor": actor_names.get(reviewer, reviewer),
                    "actor_id": reviewer,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                    "version_id": version["version_id"],
                }
            )

    interventions = db.all(
        "SELECT * FROM interventions WHERE action_item_id = ? "
        "ORDER BY created_sim_time, intervention_id",
        (action_id,),
    )
    for intervention in interventions:
        activity.append(
            {
                "kind": "INTERVENTION",
                "title": f'{intervention["level"]} 协调触达',
                "detail": f'原因：{intervention["reason_code"]}',
                "status": intervention["status"],
                "actor": actor_names.get(
                    intervention["target_actor_id"],
                    intervention["target_actor_id"],
                ),
                "actor_id": intervention["target_actor_id"],
                "sim_time": intervention["created_sim_time"],
                "sequence_no": audit_sequences.get(
                    intervention["intervention_id"], 0
                ),
            }
        )

    progress_events = db.all(
        "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
        "AND event_type = 'ActionItemProgressUpdated' ORDER BY sequence_no",
        (service.run_id, action_id),
    )
    for event in progress_events:
        payload = _decode_json(event["payload"])
        detail = payload.get("progress_summary", "未填写进展")
        if payload.get("blocked"):
            detail += f'；阻塞：{payload.get("blocker_reason", "未填写")}'
        if payload.get("next_step"):
            detail += f'；下一步：{payload["next_step"]}'
        activity.append(
            {
                "kind": "PROGRESS",
                "title": "更新任务进展",
                "detail": detail,
                "status": "BLOCKED" if payload.get("blocked") else "UPDATED",
                "actor": actor_names.get(
                    payload.get("updated_by"), payload.get("updated_by")
                ),
                "actor_id": payload.get("updated_by"),
                "sim_time": event["sim_time"],
                "sequence_no": event["sequence_no"],
            }
        )

    signal_events = db.all(
        "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
        "AND event_type = 'ProgressSignalRecorded' ORDER BY sequence_no",
        (service.run_id, action_id),
    )
    signal_titles = {
        "ON_TRACK": "状态更新：按计划",
        "AT_RISK": "状态更新：有风险",
        "BLOCKED": "状态更新：被阻塞",
        "WAITING_INPUT": "状态更新：等待输入",
        "READY_TO_SUBMIT": "状态更新：准备提交",
    }
    for event in signal_events:
        payload = _decode_json(event["payload"])
        signal_type = payload.get("signal_type")
        if signal_type not in signal_titles:
            continue
        activity.append(
            {
                "kind": "STATUS",
                "title": signal_titles[signal_type],
                "detail": payload.get("note") or "未补充说明",
                "status": signal_type,
                "actor": actor_names.get(
                    payload.get("actor_id"), payload.get("actor_id")
                ),
                "actor_id": payload.get("actor_id"),
                "sim_time": event["sim_time"],
                "sequence_no": event["sequence_no"],
            }
        )

    assistance_rows = db.all(
        "SELECT * FROM assistance_requests WHERE action_item_id = ? "
        "ORDER BY created_sim_time, assistance_request_id",
        (action_id,),
    )
    assistance_titles = {
        "AssistanceRequested": "邀请协作者",
        "AssistanceAcknowledged": "协作者确认接手",
        "AssistanceResolved": "协作已完成",
        "AssistanceCancelled": "协作已取消",
    }
    for request in assistance_rows:
        events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('AssistanceRequested','AssistanceAcknowledged',"
            "'AssistanceResolved','AssistanceCancelled') ORDER BY sequence_no",
            (service.run_id, request["assistance_request_id"]),
        )
        for event in events:
            payload = _decode_json(event["payload"])
            event_type = event["event_type"]
            if event_type == "AssistanceRequested":
                actor_id = payload.get("requester_actor_id")
                detail = (
                    f'邀请 {actor_names.get(payload.get("target_actor_id"), payload.get("target_actor_id"))}'
                    f' 协作：{payload.get("summary") or request["summary"]}'
                )
                status = "OPEN"
            else:
                actor_id = payload.get("actor_id")
                detail = payload.get("resolution_summary") or (
                    "已进入同一任务协作空间"
                    if event_type == "AssistanceAcknowledged"
                    else "协作状态已更新"
                )
                status = payload.get("status") or request["status"]
            activity.append(
                {
                    "kind": "COLLABORATION",
                    "title": assistance_titles[event_type],
                    "detail": detail,
                    "status": status,
                    "actor": actor_names.get(actor_id, actor_id),
                    "actor_id": actor_id,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                }
            )

    return sorted(
        activity,
        key=lambda item: (item["sim_time"], int(item["sequence_no"])),
        reverse=True,
    )


def workbench_state(
    service: CoordinationService,
    *,
    result_processing_mode: str = "local",
    principal: Principal | None = None,
) -> dict[str, Any]:
    db = service.db
    episode = db.one("SELECT * FROM episodes WHERE episode_id = ?", (service.episode_id,))
    timeline = service.audit_events()
    audit_sequences: dict[str, int] = {}
    for event in timeline:
        audit_sequences[event["aggregate_id"]] = max(
            audit_sequences.get(event["aggregate_id"], 0),
            int(event["sequence_no"]),
        )
    tasks: list[dict[str, Any]] = []
    for row in service.action_items():
        assignment_rows = db.all(
            "SELECT aa.*, actor.display_name FROM action_item_assignments aa "
            "JOIN actors actor ON actor.actor_id = aa.actor_id "
            "WHERE aa.action_item_id = ? "
            "ORDER BY aa.definition_version DESC, "
            "CASE aa.assignment_role WHEN 'OWNER' THEN 0 ELSE 1 END, "
            "actor.display_name, aa.actor_id",
            (row["action_item_id"],),
        )
        row["assignments"] = [dict(item) for item in assignment_rows]
        row["current_assignments"] = [
            dict(item)
            for item in assignment_rows
            if int(item["definition_version"])
            == int(row.get("definition_version") or 1)
        ]
        row["collaboration_hints"] = []
        if row["status"] in {"TRACKING", "PENDING_ACCEPTANCE"}:
            for assignment in row["current_assignments"]:
                if assignment["response_status"] != "ACCEPTED":
                    continue
                memory_rows = db.all(
                    "SELECT topic, value FROM collaboration_memories "
                    "WHERE actor_id = ? AND status = 'CONFIRMED' "
                    "ORDER BY topic, confirmed_sim_time, memory_id",
                    (assignment["actor_id"],),
                )
                for memory in memory_rows:
                    value = _decode_json(memory["value"]) or {}
                    topic = canonical_topic(memory["topic"])
                    shown = projected_value(topic, value)
                    # Nothing safe to show: an entry from before the lexicon,
                    # carrying a sentence but no code. Dropped rather than
                    # degraded, because the fallback was the first-person
                    # sentence -- which describes the person to a colleague
                    # instead of telling the colleague what to do.
                    if shown is None:
                        continue
                    row["collaboration_hints"].append(
                        {
                            "actor_id": assignment["actor_id"],
                            "display_name": assignment["display_name"],
                            "topic": topic,
                            "code": shown["code"],
                            # What the colleague reads. The first-person
                            # statement stays out of this projection.
                            "collaborator_hint": shown["collaborator_hint"],
                        }
                    )
        active_commitment = db.one(
            "SELECT * FROM commitment_revisions WHERE "
            "commitment_revision_id = ? AND status = 'ACTIVE'",
            (row["active_commitment_revision_id"],),
        ) if row["active_commitment_revision_id"] else None
        latest_intervention = db.one(
            "SELECT level, action_type, created_sim_time, status FROM interventions "
            "WHERE action_item_id = ? ORDER BY created_sim_time DESC LIMIT 1",
            (row["action_item_id"],),
        )
        latest_progress = db.one(
            "SELECT payload, sim_time FROM audit_events WHERE run_id = ? "
            "AND aggregate_id = ? AND event_type = 'ActionItemProgressUpdated' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (service.run_id, row["action_item_id"]),
        )
        all_versions = db.all(
            "SELECT * FROM artifact_versions WHERE action_item_id = ? "
            "ORDER BY received_sequence DESC",
            (row["action_item_id"],),
        )
        latest_version = all_versions[0] if all_versions else None
        current_version = (
            db.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?",
                (row["current_valid_version_id"],),
            )
            if row["current_valid_version_id"]
            else None
        )
        assistance_rows = db.all(
            "SELECT ar.*, requester.display_name AS requester_display_name, "
            "target.display_name AS target_display_name "
            "FROM assistance_requests ar "
            "JOIN actors requester ON requester.actor_id = ar.requester_actor_id "
            "JOIN actors target ON target.actor_id = ar.target_actor_id "
            "WHERE ar.action_item_id = ? "
            "ORDER BY ar.created_sim_time DESC, ar.assistance_request_id DESC",
            (row["action_item_id"],),
        )
        row["assistance_requests"] = [dict(item) for item in assistance_rows]
        row["active_assistance"] = next(
            (
                dict(item)
                for item in assistance_rows
                if item["status"] in ("OPEN", "ACKNOWLEDGED")
            ),
            None,
        )
        metadata = service.proposal_metadata(row)
        collaborator_sources: dict[str, set[str]] = {}
        historical_collaborator_sources: dict[str, set[str]] = {}
        if assignment_rows:
            if row["status"] in {
                "TRACKING",
                "PENDING_ACCEPTANCE",
                "ACCEPTED",
                "AGGREGATED",
                "ARCHIVED",
            }:
                for assignment in row["current_assignments"]:
                    if (
                        assignment["assignment_role"] == "COLLABORATOR"
                        and assignment["response_status"] == "ACCEPTED"
                    ):
                        collaborator_sources.setdefault(
                            assignment["actor_id"], set()
                        ).add("DISPATCH_ACCEPTED")
                        historical_collaborator_sources.setdefault(
                            assignment["actor_id"], set()
                        ).add("DISPATCH_ACCEPTED")
        else:
            # Backward-compatible read for meetings created before ADR-035.
            for actor_id in metadata.get("collaborator_actor_ids", []):
                collaborator_sources.setdefault(actor_id, set()).add(
                    "MEETING_RECORDED"
                )
                historical_collaborator_sources.setdefault(actor_id, set()).add(
                    "MEETING_RECORDED"
                )
        for request in assistance_rows:
            historical_collaborator_sources.setdefault(
                request["target_actor_id"], set()
            ).add("ASSISTANCE_HISTORY")
            if request["status"] in ("OPEN", "ACKNOWLEDGED"):
                collaborator_sources.setdefault(
                    request["target_actor_id"], set()
                ).add("ACTIVE_REQUEST")
        for version in all_versions:
            submitter = version["submitted_by_actor_id"]
            if submitter and submitter != row.get("owner_actor_id"):
                historical_collaborator_sources.setdefault(
                    submitter, set()
                ).add("CONTRIBUTION_SUBMITTED")
        collaborator_sources.pop(row.get("owner_actor_id"), None)
        historical_collaborator_sources.pop(row.get("owner_actor_id"), None)
        row["active_collaborator_actor_ids"] = sorted(collaborator_sources)
        row["historical_collaborator_actor_ids"] = sorted(
            historical_collaborator_sources
        )
        row["collaborators"] = [
            {
                "actor_id": actor_id,
                "display_name": next(
                    (
                        participant["target_display_name"]
                        for participant in assistance_rows
                        if participant["target_actor_id"] == actor_id
                    ),
                    None,
                )
                or db.one(
                    "SELECT display_name FROM actors WHERE actor_id = ?",
                    (actor_id,),
                )["display_name"],
                "sources": sorted(sources),
            }
            for actor_id, sources in sorted(collaborator_sources.items())
        ]
        row["historical_collaborators"] = [
            {
                "actor_id": actor_id,
                "display_name": (
                    db.one(
                        "SELECT display_name FROM actors WHERE actor_id = ?",
                        (actor_id,),
                    )["display_name"]
                ),
                "sources": sorted(sources),
            }
            for actor_id, sources in sorted(
                historical_collaborator_sources.items()
            )
        ]
        contribution_versions: list[dict[str, Any]] = []
        for version in all_versions:
            submitter = version["submitted_by_actor_id"]
            if not submitter or submitter == row.get("owner_actor_id"):
                continue
            item = dict(version)
            item["payload"] = _decode_json(version["payload"])
            item["validation_errors"] = _decode_json(version["validation_errors"])
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                item[field] = _decode_json(version[field])
            submitter_row = db.one(
                "SELECT display_name FROM actors WHERE actor_id = ?",
                (submitter,),
            )
            item["submitted_by_display_name"] = (
                submitter_row["display_name"] if submitter_row else submitter
            )
            decision_event = db.one(
                "SELECT event_type, payload, sequence_no FROM audit_events "
                "WHERE run_id = ? AND aggregate_id = ? AND event_type IN "
                "('ArtifactContributionIncluded',"
                "'ArtifactContributionRevisionRequested',"
                "'ArtifactContributionPromotedToFinalCandidate') "
                "ORDER BY sequence_no DESC LIMIT 1",
                (service.run_id, version["version_id"]),
            )
            if version["validation_status"] == "FAILED":
                contribution_status = "VALIDATION_FAILED"
            elif version["review_status"] != "NOT_REQUIRED":
                contribution_status = "PROMOTED"
            elif decision_event:
                contribution_status = {
                    "ArtifactContributionIncluded": "INCLUDED",
                    "ArtifactContributionRevisionRequested": "REVISION_REQUESTED",
                    "ArtifactContributionPromotedToFinalCandidate": "PROMOTED",
                }[decision_event["event_type"]]
            else:
                contribution_status = "AWAITING_OWNER"
            item["contribution_status"] = contribution_status
            item["decision"] = (
                _decode_json(decision_event["payload"]) if decision_event else None
            )
            item["can_request_revision"] = bool(
                submitter in collaborator_sources
            )
            contribution_versions.append(item)
        row["contribution_versions"] = contribution_versions
        row["last_owner_signal"] = (
            {
                "signal_type": row.get("last_owner_signal_type"),
                "signal_at": row.get("last_owner_signal_at"),
                "valid_until": row.get("last_owner_signal_valid_until"),
            }
            if row.get("last_owner_signal_at")
            else None
        )
        row["last_intervention"] = (
            dict(latest_intervention) if latest_intervention else None
        )
        row["active_commitment"] = (
            dict(active_commitment) if active_commitment else None
        )
        row["promised_by_sim_time"] = (
            active_commitment["promised_deadline_sim_time"]
            if active_commitment
            else None
        )
        team_required_by = row.get("team_required_by_sim_time")
        promised_by = row["promised_by_sim_time"]
        complete = row["status"] in {
            "ACCEPTED",
            "AGGREGATED",
            "ARCHIVED",
            "REJECTED",
        }
        if promised_by and team_required_by and parse_time(promised_by) > parse_time(team_required_by):
            row["schedule_status"] = "CONFLICT"
            row["schedule_risk_reason"] = "个人承诺晚于团队需要时间"
        elif promised_by and not complete and parse_time(service.now()) > parse_time(promised_by):
            row["schedule_status"] = "OVERDUE"
            row["schedule_risk_reason"] = "已超过个人承诺时间"
        else:
            row["schedule_status"] = "ON_TIME"
            row["schedule_risk_reason"] = ""
        if latest_progress:
            row["latest_progress"] = {
                "payload": _decode_json(latest_progress["payload"]),
                "sim_time": latest_progress["sim_time"],
            }
        else:
            row["latest_progress"] = None
        if latest_version:
            row["latest_version"] = dict(latest_version)
            row["latest_version"]["payload"] = _decode_json(latest_version["payload"])
            row["latest_version"]["validation_errors"] = _decode_json(
                latest_version["validation_errors"]
            )
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                row["latest_version"][field] = _decode_json(latest_version[field])
            row["latest_version"]["is_contribution"] = bool(
                latest_version["submitted_by_actor_id"]
                and latest_version["submitted_by_actor_id"]
                != row.get("owner_actor_id")
                and latest_version["review_status"] == "NOT_REQUIRED"
            )
            latest_submitter = latest_version["submitted_by_actor_id"]
            latest_submitter_row = (
                db.one(
                    "SELECT display_name FROM actors WHERE actor_id = ?",
                    (latest_submitter,),
                )
                if latest_submitter
                else None
            )
            row["latest_version"]["submitted_by_display_name"] = (
                latest_submitter_row["display_name"]
                if latest_submitter_row
                else row.get("owner_display_name")
            )
        else:
            row["latest_version"] = None
        if current_version:
            row["current_version"] = dict(current_version)
            row["current_version"]["payload"] = _decode_json(
                current_version["payload"]
            )
            row["current_version"]["validation_errors"] = _decode_json(
                current_version["validation_errors"]
            )
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                row["current_version"][field] = _decode_json(current_version[field])
        else:
            row["current_version"] = None
        accepted_result = (
            db.one(
                "SELECT * FROM accepted_task_results WHERE action_item_id = ? "
                "AND accepted_version_id = ?",
                (row["action_item_id"], row["current_valid_version_id"]),
            )
            if row["current_valid_version_id"]
            else None
        )
        row["accepted_task_result"] = dict(accepted_result) if accepted_result else None
        if row["accepted_task_result"]:
            for field in (
                "completed_content_refs",
                "normalized_result",
                "source_manifest",
                "processing_metadata",
                "collaboration_report",
            ):
                row["accepted_task_result"][field] = _decode_json(
                    accepted_result[field]
                )
        row["proposal_metadata"] = metadata
        row["collaboration_progress"] = service.collaboration_progress(
            row["action_item_id"]
        )
        row["collaboration_inputs"] = (
            service.collaboration_input_context(row["action_item_id"])
            if row["collaboration_progress"]
            else None
        )
        owner = None
        if row["owner_actor_id"]:
            owner = db.one(
                "SELECT display_name FROM actors WHERE actor_id = ?",
                (row["owner_actor_id"],),
            )
        row["owner_display_name"] = owner["display_name"] if owner else None
        assigned_owner = next(
            (
                assignment
                for assignment in row["current_assignments"]
                if assignment["assignment_role"] == "OWNER"
            ),
            None,
        )
        row["assigned_owner_display_name"] = (
            assigned_owner["display_name"] if assigned_owner else None
        )
        row["activity"] = _task_activity(
            service, row, audit_sequences=audit_sequences
        )
        tasks.append(row)
    approvals = [
        {**dict(row), "requested_action": _decode_json(row["requested_action"])}
        for row in db.all(
            "SELECT * FROM approvals WHERE episode_id = ? "
            "AND approval_type = 'FINAL_RELEASE' AND status = 'PENDING' "
            "ORDER BY requested_sim_time",
            (service.episode_id,),
        )
    ]
    final = None
    lineage: list[dict[str, Any]] = []
    if episode["current_final_deliverable_id"]:
        final_row = db.one(
            "SELECT * FROM final_deliverables WHERE final_deliverable_id = ?",
            (episode["current_final_deliverable_id"],),
        )
        final = dict(final_row) if final_row else None
        if final:
            final["payload"] = _decode_json(final["payload"])
            final["release_review"] = None
            release_approvals = db.all(
                "SELECT * FROM approvals WHERE episode_id = ? "
                "AND approval_type = 'FINAL_RELEASE' "
                "ORDER BY requested_sim_time DESC",
                (service.episode_id,),
            )
            for release_approval in release_approvals:
                requested = _decode_json(release_approval["requested_action"])
                if requested.get("final_deliverable_id") != final[
                    "final_deliverable_id"
                ]:
                    continue
                decision_event = db.one(
                    "SELECT payload FROM audit_events WHERE run_id = ? "
                    "AND aggregate_id = ? AND event_type IN "
                    "('ApprovalApproved','ApprovalRejected') "
                    "ORDER BY sequence_no DESC LIMIT 1",
                    (service.run_id, release_approval["approval_id"]),
                )
                decision_payload = (
                    _decode_json(decision_event["payload"])
                    if decision_event
                    else {}
                )
                final["release_review"] = {
                    "approval_id": release_approval["approval_id"],
                    "status": release_approval["status"],
                    "comment": decision_payload.get("comment", ""),
                    "decided_sim_time": release_approval["decided_sim_time"],
                }
                break
        lineage = [
            dict(row)
            for row in db.all(
                "SELECT * FROM final_field_lineage WHERE final_deliverable_id = ? "
                "ORDER BY field_path",
                (episode["current_final_deliverable_id"],),
            )
        ]
    processing_job_row = db.one(
        "SELECT * FROM outbox_entries WHERE episode_id = ? "
        "AND effect_type = 'FINAL_ORGANIZATION' "
        "ORDER BY created_sim_time DESC, outbox_id DESC LIMIT 1",
        (service.episode_id,),
    )
    processing_job = dict(processing_job_row) if processing_job_row else None
    if processing_job:
        processing_job["payload"] = _decode_json(processing_job["payload"])
        processing_event = db.one(
            "SELECT event_type, payload, sim_time FROM audit_events "
            "WHERE run_id = ? AND aggregate_id = ? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (service.run_id, processing_job["outbox_id"]),
        )
        processing_job["latest_event"] = (
            {
                "event_type": processing_event["event_type"],
                "payload": _decode_json(processing_event["payload"]),
                "sim_time": processing_event["sim_time"],
            }
            if processing_event
            else None
        )
    participant_rows = db.all(
        "SELECT ep.actor_id, ep.role, a.display_name FROM episode_participants ep "
        "JOIN actors a ON a.actor_id = ep.actor_id "
        "WHERE ep.episode_id = ? "
        "AND ep.role IN ('COORDINATOR','AGGREGATOR','PARTICIPANT','ACTION_OWNER') "
        "AND a.status = 'ACTIVE' ORDER BY a.display_name, ep.actor_id",
        (service.episode_id,),
    )
    participants_by_id: dict[str, dict[str, Any]] = {}
    for participant in participant_rows:
        item = participants_by_id.setdefault(
            participant["actor_id"],
            {
                "actor_id": participant["actor_id"],
                "display_name": participant["display_name"],
                "roles": [],
            },
        )
        item["roles"].append(participant["role"])
    if principal is None:
        memory_rows = db.all(
            "SELECT * FROM collaboration_memories ORDER BY created_sim_time, memory_id"
        )
    elif principal.is_participant:
        memory_rows = db.all(
            "SELECT * FROM collaboration_memories WHERE actor_id = ? "
            "ORDER BY created_sim_time, memory_id",
            (principal.actor_id,),
        )
    else:
        memory_rows = []
    memories = []
    for memory in memory_rows:
        item = dict(memory)
        item["value"] = _decode_json(memory["value"])
        item["evidence_refs"] = _decode_json(memory["evidence_refs"])
        memories.append(item)
    progress_tasks = [task for task in tasks if task["status"] != "REJECTED"]
    meeting_progress = {
        "total": len(progress_tasks),
        "pending_assignment": sum(
            task["status"] == "PENDING_ASSIGNMENT" for task in progress_tasks
        ),
        "needs_revision": sum(
            task["status"] == "NEEDS_REVISION" for task in progress_tasks
        ),
        "tracking": sum(task["status"] == "TRACKING" for task in progress_tasks),
        "pending_acceptance": sum(
            task["status"] == "PENDING_ACCEPTANCE" for task in progress_tasks
        ),
        "completed": sum(
            task["status"] in {"ACCEPTED", "AGGREGATED", "ARCHIVED"}
            for task in progress_tasks
        ),
    }
    # Notices addressed to whoever is looking.
    #
    # The bell used to be derived entirely from `tasks`, which meant it could
    # only ever show things that ask the reader for a decision. A task whose
    # description changed under someone asks for nothing and still has to
    # reach them, so it is read off the Outbox -- the same row Feishu delivers,
    # rather than a second channel that could disagree with it.
    notices: list[dict[str, Any]] = []
    if principal is not None:
        for row in db.all(
            "SELECT outbox_id, effect_id, effect_type, action_item_id, payload, "
            "created_sim_time FROM outbox_entries WHERE episode_id = ? "
            "AND effect_type IN "
            f"({','.join('?' * len(NOTIFICATION_EFFECT_TYPES))}) "
            "ORDER BY created_sim_time DESC, outbox_id DESC LIMIT 60",
            (episode["episode_id"], *sorted(NOTIFICATION_EFFECT_TYPES)),
        ):
            payload = _decode_json(row["payload"]) or {}
            if principal.actor_id not in (payload.get("recipient_actor_ids") or []):
                continue
            notification = payload.get("notification") or {}
            notices.append(
                {
                    "notice_id": row["effect_id"],
                    "kind": row["effect_type"],
                    "action_item_id": row["action_item_id"],
                    # A compound notice is about no action item at all, so the
                    # bell needs the other id to have anywhere to send anybody.
                    "compound_task_id": payload.get("compound_task_id"),
                    "title": notification.get("title", ""),
                    "summary": notification.get("summary", ""),
                    "fields": notification.get("fields", []),
                    "sim_time": row["created_sim_time"],
                    # A notice offering a decision is already surfaced by the
                    # task list; flagged so the bell does not show it twice.
                    "decides": bool(notification.get("decisions")),
                }
            )
            if len(notices) >= 12:
                break

    review_hints = []
    for raw_hint in db.all(
        "SELECT * FROM review_hints WHERE episode_id = ? "
        "ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, "
        "created_sim_time, hint_id",
        (service.episode_id,),
    ):
        hint = dict(raw_hint)
        hint["hint_payload"] = _decode_json(hint.get("hint_payload")) or {}
        review_hints.append(hint)

    state = {
        "episode": dict(episode),
        # Vocabularies the pages must offer verbatim.
        #
        # A return reason recorded on one surface is read back on the other, so
        # the web page and the Feishu card have to offer the identical list --
        # the server-rendered page used to get it by string substitution, and a
        # React page repeating it in TypeScript would let the two drift apart
        # silently. The quick statuses and help categories are here for the same
        # reason: the domain rejects anything outside them, and a button
        # offering a value the domain refuses is a defect the user finds.
        "vocabulary": {
            "return_reasons": list(ASSIGNMENT_RETURN_REASONS),
            "other_return_reason": OTHER_RETURN_REASON,
            "quick_signals": sorted(CoordinationService.QUICK_SIGNAL_TYPES),
            "assistance_categories": list(
                CoordinationService.ASSISTANCE_CATEGORIES
            ),
            # The upload limits the server enforces. A page that repeats them
            # in TypeScript lets somebody spend a minute choosing files and
            # then take a 413 -- the numbers have to be the same numbers.
            "max_attachment_count": MAX_ATTACHMENT_COUNT,
            "max_attachment_bytes": MAX_TOTAL_ATTACHMENT_BYTES,
        },
        "tasks": tasks,
        # Same coordinator queue, different entity. A hint has no task state
        # and becomes an ActionItem only through the explicit materialize API.
        "review_hints": review_hints,
        # Alongside the ordinary tasks rather than under them: to the person
        # reading the page these are both "something I owe somebody". They run
        # on their own stage machine, which is why they arrive as their own
        # list instead of being flattened into the one above.
        # Guarded the way the notices above are: this builder also serves an
        # unauthenticated view, and "whose turn is it" has no answer there.
        "compound_tasks": (
            compound_store.project(
                service.db, service.episode_id, actor_id=principal.actor_id
            )
            if principal
            else []
        ),
        "notices": notices,
        "pending_approvals": approvals,
        "final": final,
        "lineage": lineage,
        "result_processing": {
            "automatic": result_processing_mode != "disabled",
            "mode": result_processing_mode,
            "job": processing_job,
        },
        "report": build_report(
            db,
            service.fixture,
            episode_id=service.episode_id,
            run_id=service.run_id,
        ),
        "aggregator_actor_id": service.aggregator_actor_id,
        "participants": list(participants_by_id.values()),
        "memories": memories,
        "memory_lexicon": memory_lexicon_payload(),
        "meeting_progress": meeting_progress,
        "timeline": timeline,
        "agent_trace": _agent_trace(service),
    }
    return _project_workbench_state(state, principal)


SUMMARY_UPSTREAM_FIELDS = (
    "source_ref",
    "action_item_id",
    "title",
    "accepted_version_id",
    "submitted_by_actor_id",
    "submitted_by_display_name",
    "submission_summary",
    "responsibility",
)


def _summarize_collaboration_inputs(
    inputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep who delivered and in what direction; drop the delivery body."""

    if not inputs:
        return None
    return {
        "upstream_results": [
            {
                **{
                    key: result.get(key)
                    for key in SUMMARY_UPSTREAM_FIELDS
                    if key in result
                },
                "detail_level": "SUMMARY",
            }
            for result in inputs.get("upstream_results") or []
        ],
        "collective_decision": inputs.get("collective_decision"),
    }


def _version_summary(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        key: version.get(key)
        for key in (
            "version_id",
            "received_sim_time",
            "validation_status",
            "review_status",
        )
    }


def _project_workbench_state(
    state: dict[str, Any], principal: Principal | None
) -> dict[str, Any]:
    """Apply field-level access rules before a workbench payload leaves the server."""
    if principal is None:
        # Internal tests and diagnostics may request the trusted full projection.
        return state
    projected = deepcopy(state)
    projected["principal"] = {
        "actor_id": principal.actor_id,
        "roles": sorted(role.value for role in principal.roles),
        "auth_source": principal.auth_source,
    }
    if principal.is_coordinator:
        projected["allowed_surfaces"] = ["tasks", "manage", "diagnostics"]
        for task in projected["tasks"]:
            task.pop("collaboration_hints", None)
            current_assignments = task.get("current_assignments", [])
            task["my_assignment"] = next(
                (
                    assignment
                    for assignment in current_assignments
                    if assignment.get("actor_id") == principal.actor_id
                ),
                None,
            )
            task["is_mine"] = task.get("owner_actor_id") == principal.actor_id
            task["is_collaborator"] = principal.actor_id in task.get(
                "active_collaborator_actor_ids", []
            )
            task["has_collaborated"] = principal.actor_id in task.get(
                "historical_collaborator_actor_ids", []
            )
            task["can_contribute"] = bool(
                task["is_mine"] or task["is_collaborator"]
            )
        return projected

    projected["allowed_surfaces"] = ["tasks"]
    visible_tasks: list[dict[str, Any]] = []
    for task in projected["tasks"]:
        if not task.get("published_sim_time"):
            continue
        current_assignments = task.get("current_assignments", [])
        my_assignment = next(
            (
                assignment
                for assignment in current_assignments
                if assignment.get("actor_id") == principal.actor_id
            ),
            None,
        )
        is_mine = task.get("owner_actor_id") == principal.actor_id
        is_collaborator = principal.actor_id in task.get(
            "active_collaborator_actor_ids", []
        )
        has_collaborated = principal.actor_id in task.get(
            "historical_collaborator_actor_ids", []
        )
        can_contribute = bool(is_mine or is_collaborator)
        collaboration = task.get("collaboration_progress")
        is_required_participant = bool(
            collaboration
            and any(
                contribution.get("actor_id") == principal.actor_id
                for contribution in collaboration.get("contributions") or []
            )
        )
        assistance_requests = task.get("assistance_requests") or []
        is_help_target = any(
            request.get("target_actor_id") == principal.actor_id
            and request.get("status") in ("OPEN", "ACKNOWLEDGED")
            for request in assistance_requests
        )
        if not (
            my_assignment
            or is_mine
            or is_collaborator
            or has_collaborated
            or is_required_participant
            or is_help_target
        ):
            continue
        task["my_assignment"] = my_assignment
        task["is_mine"] = is_mine
        task["is_collaborator"] = is_collaborator
        task["has_collaborated"] = has_collaborated
        task["can_contribute"] = can_contribute
        task["collaboration_hints"] = [
            hint
            for hint in task.get("collaboration_hints", [])
            if hint.get("actor_id") != principal.actor_id
        ]
        if collaboration:
            for contribution in collaboration.get("contributions") or []:
                if (
                    contribution.get("contribution_type") == "VOTE"
                    and contribution.get("actor_id") != principal.actor_id
                ):
                    contribution["payload"] = None
                    contribution["payload_ref"] = None
                if (
                    contribution.get("contribution_type") == "BALLOT"
                    and contribution.get("status") != "SUBMITTED"
                    and contribution.get("actor_id") != principal.actor_id
                ):
                    contribution["payload"] = None
                    contribution["payload_ref"] = None
            structure = (task.get("proposal_metadata") or {}).get(
                "collaboration_structure"
            ) or {}
            # The confirmed final owner has to read what colleagues delivered in
            # order to organize it; everyone else in the structure only needs to
            # know who delivered and in what direction.
            if structure.get("required_owner_actor_id") != principal.actor_id:
                task["collaboration_inputs"] = (
                    _summarize_collaboration_inputs(
                        task.get("collaboration_inputs")
                    )
                    if is_required_participant
                    else None
                )
        metadata = task.get("proposal_metadata") or {}
        task["proposal_metadata"] = {
            key: metadata[key]
            for key in (
                "source_timestamp",
                "source_quote",
                "deliverable",
                "work_requirements",
                "priority",
                "collaboration_mode",
                "collaborator_names",
                "collaboration_structure",
                # So a reader can tell a checked transcript quote from a note
                # somebody typed. Without it the two are the same grey line.
                "origin",
            )
            if key in metadata
        }
        task["is_help_target"] = is_help_target
        sanitized_assignments = []
        for assignment in current_assignments:
            item = dict(assignment)
            if assignment.get("actor_id") != principal.actor_id:
                item["response_message"] = None
            sanitized_assignments.append(item)
        task["assignments"] = sanitized_assignments
        task["current_assignments"] = sanitized_assignments
        if not is_mine:
            task["promised_by_sim_time"] = None
            task["active_commitment"] = None
            task["latest_progress"] = None
            task["last_owner_signal"] = None
            visible_activity = []
            for activity in task.get("activity") or []:
                item = dict(activity)
                other_actor = item.get("actor_id") not in {
                    None,
                    principal.actor_id,
                }
                if other_actor and item.get("kind") in {
                    "COMMITMENT",
                    "PROGRESS",
                    "STATUS",
                    "DELIVERY",
                    "CONTRIBUTION",
                    "INTERVENTION",
                }:
                    continue
                if other_actor and item.get("kind") == "ASSIGNMENT":
                    item["detail"] = "同任务成员的派发回应已更新"
                visible_activity.append(item)
            task["activity"] = visible_activity
        if not can_contribute:
            task["active_commitment"] = None
            task["latest_version"] = _version_summary(task.get("latest_version"))
            task["current_version"] = _version_summary(task.get("current_version"))
            task["current_valid_version_id"] = None
            task["accepted_task_result"] = None
            task["latest_progress"] = None
            if not has_collaborated:
                task["activity"] = []
                task["contribution_versions"] = []
            else:
                task["contribution_versions"] = [
                    version
                    for version in task.get("contribution_versions", [])
                    if version.get("submitted_by_actor_id") == principal.actor_id
                ]
                for version in task["contribution_versions"]:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
            if not task["is_help_target"]:
                task["assistance_requests"] = [
                    {
                        "assistance_request_id": request.get(
                            "assistance_request_id"
                        ),
                        "status": request.get("status"),
                        "category": request.get("category"),
                        "target_display_name": request.get(
                            "target_display_name"
                        ),
                        "created_sim_time": request.get("created_sim_time"),
                    }
                    for request in assistance_requests
                    if (
                        request.get("target_actor_id") == principal.actor_id
                        or request.get("requester_actor_id") == principal.actor_id
                    )
                ]
                active = task.get("active_assistance")
                task["active_assistance"] = (
                    {
                        "assistance_request_id": active.get(
                            "assistance_request_id"
                        ),
                        "status": active.get("status"),
                        "category": active.get("category"),
                        "target_display_name": active.get(
                            "target_display_name"
                        ),
                    }
                    if active
                    else None
                )
        else:
            for version_field in ("latest_version", "current_version"):
                version = task.get(version_field)
                if version:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
            accepted_result = task.get("accepted_task_result")
            if accepted_result:
                accepted_result.pop("processing_metadata", None)
                accepted_result.pop("source_manifest", None)
            if not is_mine:
                task["contribution_versions"] = [
                    version
                    for version in task.get("contribution_versions", [])
                    if version.get("submitted_by_actor_id") == principal.actor_id
                ]
                for version in task["contribution_versions"]:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
        visible_tasks.append(task)
    projected["tasks"] = visible_tasks
    projected["review_hints"] = []
    projected["pending_approvals"] = []
    if not projected.get("final") or projected["final"].get("status") != "RELEASED":
        projected["final"] = None
        projected["lineage"] = []
    projected["result_processing"] = {
        "automatic": projected["result_processing"]["automatic"],
        "mode": projected["result_processing"]["mode"],
        "job": None,
    }
    projected["report"] = None
    projected["timeline"] = []
    projected["agent_trace"] = []
    projected.pop("aggregator_actor_id", None)
    return projected


def serve_dashboard(
    service: CoordinationService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    result_processing_mode: str = "local",
) -> None:
    if result_processing_mode not in {"bailian", "local", "disabled"}:
        raise ValueError("invalid result processing mode")
    principal_provider = VirtualSessionPrincipalProvider(
        service.db,
        episode_id=service.episode_id,
        secret=os.environ.get("COLWORK_SESSION_SECRET"),
    )
    authorization = AuthorizationService(service.db, episode_id=service.episode_id)

    approval_path = re.compile(r"^/api/approvals/([^/]+)$")
    action_path = re.compile(
        r"^/api/action-items/([^/]+)/(revise|amend|dispatch|assignment-response|ignore|merge|signal|assistance|personal-commitment|submit|ballot-draft|ballot|vote)$"
    )
    review_hint_path = re.compile(r"^/api/review-hints/([^/]+)/materialize$")
    collaboration_structure_path = re.compile(
        r"^/api/collaboration-structures/question-vote$"
    )
    collaboration_structure_revoke_path = re.compile(
        r"^/api/collaboration-structures/question-vote/([^/]+)/revoke$"
    )
    assistance_path = re.compile(
        r"^/api/assistance/([^/]+)/(acknowledge|resolve|cancel)$"
    )
    final_generate_path = re.compile(r"^/api/final/generate$")
    action_add_path = re.compile(r"^/api/action-items$")
    memory_declare_path = re.compile(r"^/api/memories/declare$")
    memory_path = re.compile(
        r"^/api/memories/([^/]+)/(confirm|replace|reject|withdraw)$"
    )
    artifact_review_path = re.compile(r"^/api/artifact-versions/([^/]+)/review$")
    artifact_contribution_path = re.compile(
        r"^/api/artifact-versions/([^/]+)/contribution$"
    )
    artifact_retry_path = re.compile(
        r"^/api/artifact-versions/([^/]+)/retry-processing$"
    )
    compound_create_path = re.compile(r"^/api/compound-tasks$")
    compound_path = re.compile(
        r"^/api/compound-tasks/([^/]+)/(input|owner-stage|revoke)$"
    )

    class Handler(BaseHTTPRequestHandler):
        def _message_id(self, payload: Any) -> str:
            """The client's idempotency key, required rather than invented.

            Generating one here would make every retry a fresh write, which is
            exactly the failure the receipt table exists to prevent.
            """

            message_id = str(payload.get("message_id", "") or "").strip()
            if not message_id:
                raise ValueError("message_id is required")
            return message_id

        def _read_json_body(self) -> Any:
            """Enforce the raw body ceiling before allocating or decoding it."""

            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise RequestTooLarge("Content-Length is not a number") from error
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise RequestTooLarge(
                    f"请求体超过 {MAX_REQUEST_BYTES // (1024 * 1024)}MB 上限"
                )
            return json.loads(self.rfile.read(length) or b"{}")

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _principal(self) -> Principal:
            return principal_provider.resolve_authorization_header(
                self.headers.get("Authorization")
            )

        def _audit_rejection(
            self,
            *,
            event_type: str,
            principal: Principal | None,
            operation: str,
            reason: str,
            actor_hint: str | None = None,
        ) -> None:
            # A denied request must not be allowed to fail the response path just
            # because diagnostic persistence is unavailable.
            try:
                service.record_security_rejection(
                    event_type=event_type,
                    actor_id=(principal.actor_id if principal else actor_hint),
                    operation=operation,
                    reason=reason,
                )
            except Exception:
                return

        def do_GET(self) -> None:  # noqa: N802 - standard library API
            parsed = urlparse(self.path)
            from .static_assets import serves as bundle_serves

            if parsed.path == "/":
                # The root is a door, not a page: send people to the one
                # surface every participant is allowed to open.
                self.send_response(302)
                self.send_header("Location", "/tasks")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif bundle_serves(parsed.path):
                from .static_assets import AssetMissing, read_asset

                try:
                    body, content_type = read_asset(parsed.path)
                except AssetMissing:
                    from .static_assets import MISSING_BUNDLE_PAGE

                    body, content_type = MISSING_BUNDLE_PAGE, "text/html; charset=utf-8"
                    self.send_response(503)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/observatory":
                from .observatory import build_observatory

                try:
                    principal = self._principal()
                    authorization.require_coordinator(principal)
                except Exception as error:  # noqa: BLE001 - mirrors /api/state
                    self._json(403, {"message": str(error)})
                    return
                requested = parse_qs(parsed.query)
                self._json(
                    200,
                    build_observatory(
                        service.db,
                        episode_id=requested.get(
                            "episode_id", [service.episode_id]
                        )[0],
                        run_id=requested.get("run_id", [service.run_id])[0],
                    ),
                )
            elif parsed.path == "/api/session/actors":
                self._json(200, {"actors": principal_provider.list_selectable_actors()})
            elif parsed.path == "/api/state":
                principal: Principal | None = None
                try:
                    principal = self._principal()
                    surface = parse_qs(parsed.query).get("surface", ["tasks"])[0]
                    if surface == "manage" or surface == "diagnostics":
                        authorization.require_coordinator(principal)
                    elif surface != "tasks":
                        raise ValueError("unknown workbench surface")
                    projected_state = workbench_state(
                        service,
                        result_processing_mode=result_processing_mode,
                        principal=principal,
                    )
                    if principal.is_participant and not principal.is_coordinator:
                        service.record_restricted_field_projection(
                            actor_id=principal.actor_id,
                            session_id=principal.session_id,
                            surface=surface,
                            hidden_fields=[
                                "management_review_policy",
                                "other_participant_delivery_body",
                                "other_participant_private_memory",
                                "unreleased_final",
                                "global_audit_timeline",
                            ],
                        )
                    self._json(200, projected_state)
                except PrincipalError as error:
                    self._audit_rejection(
                        event_type=(
                            "AuthorizationRejected"
                            if principal
                            else "AuthenticationRejected"
                        ),
                        principal=principal,
                        operation=f"GET {parsed.path}",
                        reason=str(error),
                    )
                    self._json(
                        401,
                        {"error": "UNAUTHENTICATED", "message": str(error)},
                    )
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - standard library API
            parsed = urlparse(self.path)
            if parsed.path == "/api/session":
                actor_hint: str | None = None
                try:
                    payload = self._read_json_body()
                    actor_hint = str(payload.get("actor_id", "")) or None
                    self._json(
                        200, principal_provider.issue(actor_hint or "")
                    )
                except RequestTooLarge as error:
                    self._json(
                        413,
                        {"error": "PAYLOAD_TOO_LARGE", "message": str(error)},
                    )
                except (PrincipalError, ValueError) as error:
                    self._audit_rejection(
                        event_type="AuthenticationRejected",
                        principal=None,
                        actor_hint=actor_hint,
                        operation=f"POST {parsed.path}",
                        reason=str(error),
                    )
                    self._json(
                        403,
                        # The audit above keeps the English on purpose; what a
                        # person sees is a different job, and this branch was
                        # the one 403 still answering in it.
                        {"error": "AUTHORIZATION", "message": user_message(error)},
                    )
                return
            approval_match = approval_path.match(parsed.path)
            final_generate_match = final_generate_path.match(parsed.path)
            action_match = action_path.match(parsed.path)
            action_add_match = action_add_path.match(parsed.path)
            review_hint_match = review_hint_path.match(parsed.path)
            collaboration_structure_match = collaboration_structure_path.match(
                parsed.path
            )
            assistance_match = assistance_path.match(parsed.path)
            memory_match = memory_path.match(parsed.path)
            memory_declare_match = memory_declare_path.match(parsed.path)
            structure_revoke_match = collaboration_structure_revoke_path.match(
                parsed.path
            )
            artifact_match = artifact_review_path.match(parsed.path)
            artifact_contribution_match = artifact_contribution_path.match(
                parsed.path
            )
            artifact_retry_match = artifact_retry_path.match(parsed.path)
            compound_create_match = compound_create_path.match(parsed.path)
            compound_match = compound_path.match(parsed.path)
            if (
                not approval_match
                and not final_generate_match
                and not action_match
                and not action_add_match
                and not review_hint_match
                and not collaboration_structure_match
                and not structure_revoke_match
                and not assistance_match
                and not memory_match
                and not memory_declare_match
                and not artifact_match
                and not artifact_contribution_match
                and not artifact_retry_match
                and not compound_create_match
                and not compound_match
            ):
                self._json(404, {"error": "not_found"})
                return
            principal: Principal | None = None
            try:
                payload = self._read_json_body()
                principal = self._principal()
                if compound_create_match:
                    # Declaring the shape is the coordinator's, the same as
                    # dispatching an ordinary task: it puts work on five
                    # people's plates without asking them first.
                    authorization.require_coordinator(principal)
                    result = compound_store.create_compound_task(
                        service.db,
                        run_id=service.run_id,
                        episode_id=service.episode_id,
                        kind=payload.get("kind", ""),
                        title=payload.get("title", ""),
                        body=payload.get("body", ""),
                        owner_actor_id=payload.get("owner_actor_id", ""),
                        member_actor_ids=payload.get("member_actor_ids", []),
                        source_span=payload.get("source_span", ""),
                        selection_count=payload.get("selection_count"),
                        sim_time=service.now(),
                        message_id=self._message_id(payload),
                    )
                    self._json(200, result)
                    return
                elif compound_match:
                    # Only that you belong to this meeting. Which people a
                    # compound task is for is its own roster, and the stage
                    # machine already checks it -- asking the same question
                    # twice in two places is how the two answers drift apart.
                    #
                    # Specifically not require_participant: a coordinator who
                    # is also one of the five filling something in got a 403
                    # here before the roster was ever consulted.
                    authorization.require_episode(principal)
                    compound_task_id, operation = compound_match.groups()
                    if operation == "input":
                        result = compound_store.submit_input(
                            service.db,
                            compound_task_id,
                            run_id=service.run_id,
                            actor_id=principal.actor_id,
                            payload=payload.get("payload") or {},
                            sim_time=service.now(),
                            message_id=self._message_id(payload),
                        )
                    elif operation == "owner-stage":
                        result = compound_store.finish_owner_stage(
                            service.db,
                            compound_task_id,
                            run_id=service.run_id,
                            actor_id=principal.actor_id,
                            payload=payload.get("payload") or {},
                            sim_time=service.now(),
                            message_id=self._message_id(payload),
                        )
                    else:
                        result = compound_store.revoke(
                            service.db,
                            compound_task_id,
                            run_id=service.run_id,
                            actor_id=principal.actor_id,
                            reason=payload.get("reason", ""),
                            sim_time=service.now(),
                            message_id=self._message_id(payload),
                        )
                    self._json(200, result)
                    return
                elif review_hint_match:
                    authorization.require_coordinator(principal)
                    result = service.materialize_review_hint(
                        review_hint_match.group(1),
                        actor_id=principal.actor_id,
                        title=payload.get("title", ""),
                        deliverable=payload.get("deliverable", ""),
                        acceptance_criteria=payload.get(
                            "acceptance_criteria", ""
                        ),
                        priority=payload.get("priority", "P1"),
                        team_required_by_sim_time=payload.get(
                            "team_required_by_sim_time"
                        ),
                        work_requirements=payload.get("work_requirements"),
                        message_id=self._message_id(payload),
                    )
                elif approval_match:
                    authorization.require_coordinator(principal)
                    result = service.decide_approval(
                        approval_match.group(1),
                        actor_id=principal.actor_id,
                        approve=bool(payload.get("approve")),
                        comment=payload.get("comment", ""),
                    )
                    service.dispatch_all(session_id="workbench_dispatcher")
                elif action_add_match:
                    authorization.require_coordinator(principal)
                    result = service.add_action_item(
                        actor_id=principal.actor_id,
                        title=payload.get("title", ""),
                        deliverable=payload.get("deliverable", ""),
                        source_note=payload.get("source_note", ""),
                        acceptance_criteria=payload.get("acceptance_criteria", ""),
                        priority=payload.get("priority", "P1"),
                        team_required_by_sim_time=payload.get(
                            "team_required_by_sim_time"
                        ),
                        message_id=self._message_id(payload),
                    )
                    self._json(200, result)
                    return
                elif final_generate_match:
                    authorization.require_coordinator(principal)
                    # `aggregate` itself will happily summarise a half-finished
                    # meeting -- `eval` and the CLI rely on that. Asking for a
                    # final from the console means something narrower: that the
                    # work is done. Refusing here keeps that promise without
                    # taking the looser behaviour away from the other callers,
                    # and it is a real refusal rather than a greyed-out button
                    # somebody can POST straight past.
                    outstanding = [
                        dict(row)["title"]
                        for row in service.db.all(
                            "SELECT title FROM action_items WHERE episode_id = ? "
                            "AND required = TRUE AND status NOT IN "
                            "('ACCEPTED','AGGREGATED','ARCHIVED','REJECTED') "
                            "ORDER BY action_item_id",
                            (service.episode_id,),
                        )
                    ]
                    if outstanding:
                        self._json(
                            409,
                            {
                                "error": "TASKS_OUTSTANDING",
                                "message": (
                                    f"还有 {len(outstanding)} 项必需任务没验收完："
                                    + "、".join(outstanding[:3])
                                    + ("…" if len(outstanding) > 3 else "")
                                ),
                                "outstanding": outstanding,
                            },
                        )
                        return
                    result = {
                        "final_deliverable_id": service.aggregate(),
                        "outstanding": [],
                    }
                    service.dispatch_all(session_id="workbench_dispatcher")
                elif collaboration_structure_match:
                    authorization.require_coordinator(principal)
                    result = service.confirm_question_vote_structure(
                        collection_action_item_ids=payload.get(
                            "collection_action_item_ids", []
                        ),
                        decision_action_item_id=payload.get(
                            "decision_action_item_id", ""
                        ),
                        final_owner_actor_id=payload.get(
                            "final_owner_actor_id", ""
                        ),
                        voter_actor_ids=payload.get("voter_actor_ids", []),
                        selection_count=payload.get("selection_count", 8),
                        source_span=payload.get("source_span", ""),
                        actor_id=principal.actor_id,
                        message_id=payload.get("message_id", ""),
                    )
                elif assistance_match:
                    authorization.require_episode(principal)
                    assistance_id, assistance_action = assistance_match.groups()
                    result = service.update_assistance(
                        assistance_id,
                        actor_id=principal.actor_id,
                        action=assistance_action,
                        resolution_summary=payload.get("resolution_summary", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif structure_revoke_match:
                    authorization.require_coordinator(principal)
                    result = service.revoke_question_vote_structure(
                        structure_revoke_match.group(1),
                        actor_id=principal.actor_id,
                        reason=payload.get("reason", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif memory_declare_match:
                    authorization.require_participant(principal)
                    result = service.declare_collaboration_memory(
                        actor_id=principal.actor_id,
                        topic=payload.get("topic", ""),
                        code=payload.get("code", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif memory_match:
                    authorization.require_participant(principal)
                    memory_id, memory_action = memory_match.groups()
                    result = service.decide_collaboration_memory(
                        memory_id,
                        actor_id=principal.actor_id,
                        action=memory_action,
                        replacement_code=payload.get("replacement_code", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_retry_match:
                    authorization.require_coordinator(principal)
                    result = service.retry_task_result_processing(
                        artifact_retry_match.group(1),
                        actor_id=principal.actor_id,
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_contribution_match:
                    authorization.require_participant(principal)
                    result = service.decide_contribution(
                        artifact_contribution_match.group(1),
                        actor_id=principal.actor_id,
                        action=payload.get("action", ""),
                        comment=payload.get("comment", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_match:
                    authorization.require_coordinator(principal)
                    result = service.review_artifact(
                        artifact_match.group(1),
                        actor_id=principal.actor_id,
                        approve=bool(payload.get("approve")),
                        comment=payload.get("comment", ""),
                        message_id=payload.get("message_id", ""),
                        completion_report=payload.get("completion_report"),
                    )
                else:
                    action_id, operation = action_match.groups()
                    message_id = payload.get("message_id", "")
                    if operation == "revise":
                        authorization.require_coordinator(principal)
                        result = service.revise_action_proposal(
                            action_id,
                            actor_id=principal.actor_id,
                            title=payload.get("title", ""),
                            deliverable=payload.get("deliverable", ""),
                            acceptance_criteria=payload.get(
                                "acceptance_criteria", ""
                            ),
                            priority=payload.get("priority", "P1"),
                            message_id=message_id,
                            team_required_by_sim_time=payload.get(
                                "team_required_by_sim_time"
                            ),
                            work_requirements=payload.get("work_requirements"),
                            management_review_policy=payload.get(
                                "management_review_policy"
                            ),
                        )
                    elif operation == "amend":
                        # No coordinator check: the service requires the caller
                        # to be the task's own owner, which the coordinator is
                        # not unless the task was dispatched to them.
                        authorization.require_participant(principal)
                        result = service.amend_task_description(
                            action_id,
                            actor_id=principal.actor_id,
                            title=payload.get("title", ""),
                            deliverable=payload.get("deliverable", ""),
                            message_id=message_id,
                        )
                    elif operation == "dispatch":
                        authorization.require_coordinator(principal)
                        result = service.dispatch_action(
                            action_id,
                            actor_id=principal.actor_id,
                            owner_actor_id=payload.get("owner_actor_id", ""),
                            collaborator_actor_ids=payload.get(
                                "collaborator_actor_ids", []
                            ),
                            assignment_message=payload.get(
                                "assignment_message", ""
                            ),
                            message_id=message_id,
                        )
                    elif operation == "assignment-response":
                        authorization.require_participant(principal)
                        result = service.respond_to_assignment(
                            action_id,
                            actor_id=principal.actor_id,
                            decision=payload.get("decision", ""),
                            response_message=payload.get(
                                "response_message", ""
                            ),
                            message_id=message_id,
                        )
                    elif operation == "ignore":
                        authorization.require_coordinator(principal)
                        result = service.ignore_action(
                            action_id,
                            actor_id=principal.actor_id,
                            reason=payload.get("reason", ""),
                            message_id=message_id,
                        )
                    elif operation == "merge":
                        authorization.require_coordinator(principal)
                        result = service.merge_action(
                            action_id,
                            target_action_item_id=payload.get(
                                "target_action_item_id", ""
                            ),
                            actor_id=principal.actor_id,
                            message_id=message_id,
                        )
                    elif operation == "signal":
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.record_progress_signal(
                            action_id,
                            actor_id=principal.actor_id,
                            signal_type=payload.get("signal_type", ""),
                            valid_until=payload.get("valid_until"),
                            note=payload.get("note", ""),
                            message_id=message_id,
                        )
                    elif operation == "assistance":
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.request_assistance(
                            action_id,
                            actor_id=principal.actor_id,
                            target_actor_id=payload.get("target_actor_id", ""),
                            category=payload.get("category", "OTHER"),
                            summary=payload.get("summary", ""),
                            blocking_action_item_id=payload.get(
                                "blocking_action_item_id"
                            ),
                            message_id=message_id,
                        )
                    elif operation == "personal-commitment":
                        authorization.require_action_owner(principal, action_id)
                        result = service.revise_personal_commitment(
                            action_id,
                            actor_id=principal.actor_id,
                            proposed_deadline_sim_time=payload.get(
                                "proposed_deadline_sim_time", ""
                            ),
                            reason=payload.get("reason", ""),
                            message_id=message_id,
                        )
                    elif operation == "ballot-draft":
                        authorization.require_participant(principal)
                        result = service.prepare_question_ballot_draft(
                            action_id,
                            actor_id=principal.actor_id,
                            processing_mode=(
                                "bailian"
                                if result_processing_mode == "bailian"
                                else "local"
                            ),
                            message_id=message_id,
                        )
                    elif operation == "ballot":
                        authorization.require_participant(principal)
                        result = service.open_question_ballot(
                            action_id,
                            actor_id=principal.actor_id,
                            options=payload.get("options", []),
                            message_id=message_id,
                        )
                    elif operation == "vote":
                        authorization.require_participant(principal)
                        result = service.submit_question_vote(
                            action_id,
                            actor_id=principal.actor_id,
                            scores=payload.get("scores", {}),
                            message_id=message_id,
                        )
                    else:
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.submit_artifact(
                            action_id,
                            actor_id=principal.actor_id,
                            message_id=message_id,
                            payload=payload.get("delivery", {}),
                        )
                self._json(200, result)
            except PrincipalError as error:
                self._audit_rejection(
                    event_type=(
                        "AuthorizationRejected"
                        if principal
                        else "AuthenticationRejected"
                    ),
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    403,
                    {"error": "AUTHORIZATION", "message": user_message(error)},
                )
            except PermissionError as error:
                self._audit_rejection(
                    event_type="AuthorizationRejected",
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    403,
                    {"error": "AUTHORIZATION", "message": user_message(error)},
                )
            except RequestTooLarge as error:
                self._audit_rejection(
                    event_type="RequestRejected",
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    413,
                    {"error": "PAYLOAD_TOO_LARGE", "message": str(error)},
                )
            except (KeyError, ValueError) as error:
                # Conflict is decided on the English the domain raised, not on
                # the sentence shown to the reader: the status code is part of
                # the API and must not move when a translation is reworded.
                raw = str(error)
                conflict = any(
                    token in raw
                    for token in ("already", "claimed", "competing", "published")
                )
                self._json(
                    409 if conflict else 400,
                    {"error": type(error).__name__, "message": user_message(error)},
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    try:
        server = SingleInstanceHTTPServer((host, port), Handler)
    except OSError as error:
        raise RuntimeError(
            f"workbench address http://{host}:{port} is already in use; "
            "stop the existing instance before starting another"
        ) from error
    server.timeout = 1.0
    try:
        while True:
            server.handle_request()
    finally:
        server.server_close()
