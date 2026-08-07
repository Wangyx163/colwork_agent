from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median
from typing import Any

from .models import parse_time, stable_hash
from .store import Database


def _hours(start: str, end: str) -> float:
    return round((parse_time(end) - parse_time(start)).total_seconds() / 3600, 2)


def _gate(passed: bool, **evidence: Any) -> dict[str, Any]:
    return {"passed": bool(passed), "evidence": evidence}


def _rate(numerator: int, denominator: int, reason: str) -> dict[str, Any]:
    if denominator == 0:
        return {
            "numerator": numerator,
            "denominator": 0,
            "rate": "N/A",
            "reason": reason,
        }
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 3),
        "reason": None,
    }


def _duration_summary(samples: list[float], reason: str) -> dict[str, Any]:
    if not samples:
        return {
            "samples": [],
            "denominator": 0,
            "max_hours": "N/A",
            "median_hours": "N/A",
            "reason": reason,
        }
    return {
        "samples": samples,
        "denominator": len(samples),
        "max_hours": max(samples),
        "median_hours": round(median(samples), 2),
        "reason": None,
    }


def _payload(row: Any) -> dict[str, Any]:
    value = row["payload"]
    return json.loads(value) if isinstance(value, str) else value


def build_report(
    database: Database,
    fixture: dict[str, Any],
    *,
    episode_id: str = "episode_p0",
    run_id: str = "run_p0",
) -> dict[str, Any]:
    episode = database.one(
        "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
    )
    if not episode:
        raise ValueError("evaluation requires a bootstrapped episode")

    all_actions = database.all(
        "SELECT * FROM action_items WHERE episode_id = ? ORDER BY action_item_id",
        (episode_id,),
    )
    actions = [row for row in all_actions if bool(row["required"])]
    final = database.one(
        "SELECT * FROM final_deliverables WHERE final_deliverable_id = ?",
        (episode["current_final_deliverable_id"],),
    )
    messages = database.all(
        "SELECT m.* FROM mock_im_messages m JOIN outbox_entries o "
        "ON o.effect_id = m.effect_id WHERE o.episode_id = ? "
        "ORDER BY m.accepted_sequence",
        (episode_id,),
    )
    outbox = database.all(
        "SELECT * FROM outbox_entries WHERE episode_id = ? ORDER BY created_sim_time",
        (episode_id,),
    )
    interventions = database.all(
        "SELECT * FROM interventions WHERE episode_id = ? "
        "ORDER BY created_sim_time, intervention_id",
        (episode_id,),
    )
    approvals = database.all(
        "SELECT * FROM approvals WHERE episode_id = ? ORDER BY requested_sim_time",
        (episode_id,),
    )
    versions = database.all(
        "SELECT v.* FROM artifact_versions v JOIN action_items a "
        "ON a.action_item_id = v.action_item_id WHERE a.episode_id = ? "
        "ORDER BY v.received_sequence",
        (episode_id,),
    )
    accepted_results = database.all(
        "SELECT r.* FROM accepted_task_results r JOIN action_items a "
        "ON a.action_item_id = r.action_item_id WHERE a.episode_id = ? "
        "ORDER BY r.accepted_sim_time",
        (episode_id,),
    )
    assistance_requests = database.all(
        "SELECT * FROM assistance_requests WHERE episode_id = ? "
        "ORDER BY created_sim_time, assistance_request_id",
        (episode_id,),
    )
    episode_participants = database.all(
        "SELECT actor_id, role FROM episode_participants WHERE episode_id = ?",
        (episode_id,),
    )
    collaboration_memories = database.all(
        "SELECT m.* FROM collaboration_memories m "
        "JOIN episode_participants ep ON ep.actor_id = m.actor_id "
        "WHERE ep.episode_id = ? ORDER BY m.created_sim_time",
        (episode_id,),
    )
    revisions = database.all(
        "SELECT c.* FROM commitment_revisions c JOIN action_items a "
        "ON a.action_item_id = c.action_item_id WHERE a.episode_id = ? "
        "ORDER BY c.action_item_id, c.revision_no",
        (episode_id,),
    )
    audits = database.all(
        "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence_no", (run_id,)
    )
    event_counts = Counter(row["event_type"] for row in audits)
    invocation_event_types = {
        "AttachmentExtractionCompleted",
        "TaskResultProcessingStarted",
        "TaskResultProcessingSucceeded",
        "TaskResultProcessingFailed",
        "FinalOrganizationQueued",
        "FinalOrganizationStarted",
        "FinalOrganizationSkipped",
        "FinalOrganizationRetryScheduled",
        "FinalOrganizationDeadLettered",
        "FinalOrganizationSucceeded",
    }
    invocation_records: list[tuple[Any, dict[str, Any]]] = []
    missing_invocation_manifests: list[str] = []
    incomplete_invocation_manifests: list[str] = []
    invocation_principal_violations: list[str] = []
    invocation_field_violations: list[str] = []
    invocation_binary_leaks: list[str] = []
    invocation_hash_mismatches: list[str] = []
    versions_by_id = {row["version_id"]: row for row in versions}
    for event in audits:
        event_payload = _payload(event)
        invocation = event_payload.get("invocation")
        if event["event_type"] == "EpisodeActivated" and event_payload.get(
            "extraction_invocation"
        ):
            invocation = event_payload["extraction_invocation"]
        relevant = event["event_type"] in invocation_event_types or bool(invocation)
        if not relevant:
            continue
        if not isinstance(invocation, dict):
            missing_invocation_manifests.append(event["event_id"])
            continue
        invocation_records.append((event, invocation))
        required_keys = {
            "capability_type",
            "principal",
            "purpose",
            "field_allowlist",
            "entity_versions",
            "input_hash",
            "output_status",
        }
        capability_type = str(invocation.get("capability_type") or "")
        if (
            required_keys - set(invocation)
            or not isinstance(invocation.get("field_allowlist"), list)
            or not invocation.get("field_allowlist")
            or not isinstance(invocation.get("entity_versions"), dict)
            or not invocation.get("entity_versions")
            or not str(invocation.get("input_hash") or "")
            or (
                capability_type == "MODEL"
                and not invocation.get("prompt_version")
            )
            or (
                capability_type == "SKILL_ADAPTER"
                and not invocation.get("skill_version")
            )
        ):
            incomplete_invocation_manifests.append(event["event_id"])
        principal = invocation.get("principal") or {}
        if (
            not isinstance(principal, dict)
            or principal.get("actor_id") != "SYSTEM"
            or principal.get("episode_id") != episode_id
            or "SYSTEM" not in (principal.get("roles") or [])
        ):
            invocation_principal_violations.append(event["event_id"])
        field_allowlist = [
            str(field) for field in invocation.get("field_allowlist") or []
        ]
        if capability_type == "MODEL" and any(
            field.endswith(".data") or "base64" in field.casefold()
            for field in field_allowlist
        ):
            invocation_field_violations.append(event["event_id"])
        serialized_payload = json.dumps(event_payload, ensure_ascii=False)
        if any(
            marker in serialized_payload
            for marker in (";base64,", "data:application/", "data:text/")
        ):
            invocation_binary_leaks.append(event["event_id"])

        expected_hash: str | None = None
        if event["event_type"] == "AttachmentExtractionCompleted":
            version = versions_by_id.get(event["aggregate_id"])
            if version:
                raw_attachments = version["attachment_extractions"] or "[]"
                attachment_rows = (
                    json.loads(raw_attachments)
                    if isinstance(raw_attachments, str)
                    else raw_attachments
                )
                expected_hash = stable_hash(
                    [
                        {
                            "name": item.get("name"),
                            "type": item.get("type"),
                            "size": item.get("size"),
                            "content_sha256": item.get("content_sha256"),
                        }
                        for item in attachment_rows or []
                    ]
                )
        elif event["event_type"] == "TaskResultProcessingSucceeded":
            version = versions_by_id.get(event["aggregate_id"])
            if version and version["processing_metadata"]:
                raw_metadata = version["processing_metadata"]
                processing_metadata = (
                    json.loads(raw_metadata)
                    if isinstance(raw_metadata, str)
                    else raw_metadata
                )
                expected_hash = processing_metadata.get("input_hash")
        elif event["event_type"] == "FinalOrganizationSucceeded":
            final_id = event_payload.get("final_deliverable_id")
            generated = database.one(
                "SELECT payload FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (final_id,),
            )
            if generated:
                generated_payload = (
                    json.loads(generated["payload"])
                    if isinstance(generated["payload"], str)
                    else generated["payload"]
                )
                expected_hash = (generated_payload.get("processing") or {}).get(
                    "input_hash"
                )
        if expected_hash and invocation.get("input_hash") != expected_hash:
            invocation_hash_mismatches.append(event["event_id"])

    # Gate 1: the whole workflow closed, not merely individual nodes succeeding.
    archived_actions = sum(row["status"] == "ARCHIVED" for row in actions)
    e2e_ok = (
        episode["status"] == "ARCHIVED"
        and final is not None
        and final["status"] == "RELEASED"
        and archived_actions == len(actions)
    )

    # Gate 2: every logical effect maps to exactly one external message.
    message_effect_counts = Counter(row["effect_id"] for row in messages)
    duplicate_effects = {
        effect: count for effect, count in message_effect_counts.items() if count > 1
    }
    external_id_counts = Counter(row["external_message_id"] for row in messages)
    duplicate_external_ids = {
        message_id: count
        for message_id, count in external_id_counts.items()
        if count > 1
    }
    duplicate_send_ok = not duplicate_effects and not duplicate_external_ids

    # Gate 3: the released object uses exactly the latest valid version and records
    # field-level lineage for everything it contains.
    stale_lineage: list[dict[str, str]] = []
    missing_lineage: list[str] = []
    hash_mismatches: list[str] = []
    stale_result_refs: list[dict[str, str]] = []
    missing_result_refs: list[str] = []
    if final:
        final_payload = json.loads(final["payload"])
        lineage_rows = database.all(
            "SELECT * FROM final_field_lineage WHERE final_deliverable_id = ?",
            (final["final_deliverable_id"],),
        )
        lineage_by_path = {row["field_path"]: row for row in lineage_rows}
        for action in actions:
            key = action["deliverable_key"]
            fields = final_payload.get("deliverables", {}).get(key, {})
            for field in fields:
                path = f"deliverables.{key}.{field}"
                lineage = lineage_by_path.get(path)
                if not lineage:
                    missing_lineage.append(path)
                    continue
                if lineage["version_id"] != action["current_valid_version_id"]:
                    stale_lineage.append(
                        {
                            "field_path": path,
                            "lineage_version_id": lineage["version_id"],
                            "current_valid_version_id": action["current_valid_version_id"],
                        }
                    )
                version = database.one(
                    "SELECT payload FROM artifact_versions WHERE version_id = ?",
                    (lineage["version_id"],),
                )
                if not version:
                    hash_mismatches.append(path)
                    continue
                version_payload = json.loads(version["payload"])
                if (
                    field not in version_payload
                    or stable_hash(fields[field]) != lineage["value_hash"]
                    or stable_hash(version_payload[field]) != lineage["value_hash"]
                ):
                    hash_mismatches.append(path)
        expected_paths = {
            f"deliverables.{action['deliverable_key']}.{field}"
            for action in actions
            for field in json.loads(
                database.one(
                    "SELECT payload FROM artifact_versions WHERE version_id = ?",
                    (action["current_valid_version_id"],),
                )["payload"]
            )
        }
        extra_lineage = sorted(set(lineage_by_path) - expected_paths)
        sections_by_action = {
            section.get("action_item_id"): section
            for section in final_payload.get("organized_report", {}).get(
                "sections", []
            )
            if isinstance(section, dict)
        }
        accepted_payload = final_payload.get("accepted_task_results", {})
        for action in actions:
            accepted = next(
                (
                    row
                    for row in accepted_results
                    if row["action_item_id"] == action["action_item_id"]
                    and row["accepted_version_id"]
                    == action["current_valid_version_id"]
                ),
                None,
            )
            result_payload = accepted_payload.get(action["deliverable_key"])
            section = sections_by_action.get(action["action_item_id"])
            if not accepted or not isinstance(result_payload, dict) or not section:
                missing_result_refs.append(action["action_item_id"])
                continue
            expected_result_id = accepted["accepted_task_result_id"]
            if (
                result_payload.get("accepted_task_result_id")
                != expected_result_id
                or result_payload.get("accepted_version_id")
                != action["current_valid_version_id"]
                or section.get("source_version_id")
                != action["current_valid_version_id"]
                or section.get("accepted_task_result_id")
                != expected_result_id
            ):
                stale_result_refs.append(
                    {
                        "action_item_id": action["action_item_id"],
                        "expected_result_id": expected_result_id,
                        "final_result_id": str(
                            section.get("accepted_task_result_id") or ""
                        ),
                    }
                )
    else:
        missing_lineage = ["final_deliverable"]
        missing_result_refs = ["final_deliverable"]
        extra_lineage = []
    accepted_pairs = {
        (row["action_item_id"], row["accepted_version_id"])
        for row in accepted_results
    }
    missing_accepted_results = [
        row["action_item_id"]
        for row in actions
        if (row["action_item_id"], row["current_valid_version_id"])
        not in accepted_pairs
    ]
    e2e_ok = e2e_ok and not missing_accepted_results
    lineage_ok = (
        not stale_lineage
        and not missing_lineage
        and not extra_lineage
        and not hash_mismatches
        and not missing_accepted_results
        and not stale_result_refs
        and not missing_result_refs
    )

    # Gate 4: final release was approved before its outbox command existed;
    # L1/L2 social touches stayed within the configured daily budget.
    bypasses: list[str] = []
    protected = database.all(
        "SELECT * FROM outbox_entries WHERE episode_id = ? "
        "AND effect_type = 'FINAL_RELEASE'",
        (episode_id,),
    )
    for entry in protected:
        approval = database.one(
            "SELECT * FROM approvals WHERE effect_id = ?", (entry["effect_id"],)
        )
        if (
            not approval
            or approval["status"] not in ("APPROVED", "EXECUTED")
            or not approval["decided_sim_time"]
            or parse_time(approval["decided_sim_time"])
            > parse_time(entry["created_sim_time"])
        ):
            bypasses.append(entry["effect_id"])
    touches_by_owner_day: defaultdict[str, int] = defaultdict(int)
    for row in interventions:
        if row["level"] in ("L1", "L2") and row["status"] not in (
            "SUPERSEDED",
            "FAILED",
        ):
            touches_by_owner_day[f'{row["target_actor_id"]}:{row["created_sim_time"][:10]}'] += 1
    budget = int(fixture["policy"]["daily_touch_budget"])
    budget_violations = {
        key: count for key, count in touches_by_owner_day.items() if count > budget
    }
    valid_help_target_ids = {
        row["actor_id"]
        for row in episode_participants
        if row["role"]
        in ("COORDINATOR", "AGGREGATOR", "PARTICIPANT", "ACTION_OWNER")
    }
    invalid_help_targets = [
        row["assistance_request_id"]
        for row in assistance_requests
        if row["target_actor_id"] not in valid_help_target_ids
        or row["target_actor_id"] == row["requester_actor_id"]
    ]
    authorization_bypasses = event_counts["AuthorizationBypassed"]
    restricted_field_exposures = event_counts["RestrictedFieldExposed"]
    guard_ok = (
        not bypasses
        and not budget_violations
        and not invalid_help_targets
        and authorization_bypasses == 0
        and restricted_field_exposures == 0
        and not missing_invocation_manifests
        and not incomplete_invocation_manifests
        and not invocation_principal_violations
        and not invocation_field_violations
        and not invocation_binary_leaks
        and not invocation_hash_mismatches
    )

    # Gate 5: the injected crash was recovered and the retried send was deduplicated.
    reclaimed = event_counts["ProcessRecovered"]
    deduplicated = event_counts["OutboxDeliveryDeduplicated"]
    recovery_ok = reclaimed >= 1 and deduplicated >= 1 and duplicate_send_ok

    proposed = len(actions)
    confirmed = sum(bool(row["confirmed_sim_time"]) for row in actions)
    valid_actions = sum(bool(row["current_valid_version_id"]) for row in actions)
    aggregated_actions = sum(
        row["status"] in ("AGGREGATED", "ARCHIVED") for row in actions
    )

    versions_by_action: defaultdict[str, list[Any]] = defaultdict(list)
    for version in versions:
        versions_by_action[version["action_item_id"]].append(version)
    revisions_by_action: defaultdict[str, list[Any]] = defaultdict(list)
    for revision in revisions:
        revisions_by_action[revision["action_item_id"]].append(revision)

    personal_on_time = 0
    personal_deadline_count = 0
    team_on_time = 0
    team_deadline_count = 0
    late_items: list[dict[str, Any]] = []
    team_late_items: list[dict[str, Any]] = []
    confirmation_latency_by_action: dict[str, float] = {}
    delivery_latency_by_action: dict[str, float] = {}
    for action in actions:
        action_id = action["action_item_id"]
        if action["confirmed_sim_time"]:
            confirmation_latency_by_action[action_id] = _hours(
                action["created_sim_time"], action["confirmed_sim_time"]
            )
        passed = sorted(
            (
                row
                for row in versions_by_action[action_id]
                if row["validation_status"] == "PASSED"
            ),
            key=lambda row: int(row["received_sequence"]),
        )
        if not passed:
            continue
        first_passed = passed[0]
        if action["confirmed_sim_time"]:
            delivery_latency_by_action[action_id] = _hours(
                action["confirmed_sim_time"], first_passed["received_sim_time"]
            )
        effective = [
            row
            for row in revisions_by_action[action_id]
            if parse_time(row["created_sim_time"])
            <= parse_time(first_passed["received_sim_time"])
        ]
        deadline = (
            max(effective, key=lambda row: int(row["revision_no"]))[
                "promised_deadline_sim_time"
            ]
            if effective
            else action["deadline_sim_time"]
        )
        if deadline:
            personal_deadline_count += 1
            if parse_time(first_passed["received_sim_time"]) <= parse_time(deadline):
                personal_on_time += 1
            else:
                late_items.append(
                    {
                        "action_item_id": action_id,
                        "late_hours": _hours(
                            deadline, first_passed["received_sim_time"]
                        ),
                    }
                )
        team_deadline = action["team_required_by_sim_time"]
        if team_deadline:
            team_deadline_count += 1
            if parse_time(first_passed["received_sim_time"]) <= parse_time(
                team_deadline
            ):
                team_on_time += 1
            else:
                team_late_items.append(
                    {
                        "action_item_id": action_id,
                        "late_hours": _hours(
                            team_deadline, first_passed["received_sim_time"]
                        ),
                    }
                )

    generated_latency = (
        [_hours(episode["created_sim_time"], final["generated_sim_time"])]
        if final
        else []
    )
    final_wait = (
        [_hours(final["generated_sim_time"], final["approved_sim_time"])]
        if final and final["approved_sim_time"]
        else []
    )
    archived_event = next(
        (row for row in reversed(audits) if row["event_type"] == "EpisodeArchived"),
        None,
    )
    end_to_end = (
        [_hours(episode["created_sim_time"], archived_event["sim_time"])]
        if archived_event
        else []
    )

    audits_by_aggregate: defaultdict[str, list[Any]] = defaultdict(list)
    for event in audits:
        audits_by_aggregate[event["aggregate_id"]].append(event)

    conflict_resolution_hours: list[float] = []
    for action in actions:
        action_events = audits_by_aggregate[action["action_item_id"]]
        open_detection: Any | None = None
        for event in action_events:
            if event["event_type"] == "ScheduleConflictDetected":
                open_detection = open_detection or event
            elif (
                event["event_type"] == "ScheduleConflictResolved"
                and open_detection
            ):
                conflict_resolution_hours.append(
                    _hours(open_detection["sim_time"], event["sim_time"])
                )
                open_detection = None
    active_schedule_conflicts = sum(
        bool(
            action["team_required_by_sim_time"]
            and action["deadline_sim_time"]
            and parse_time(action["deadline_sim_time"])
            > parse_time(action["team_required_by_sim_time"])
        )
        for action in actions
    )

    publish_to_claim: list[float] = []
    claim_to_submit: list[float] = []
    submit_to_processing: list[float] = []
    processing_to_review: list[float] = []
    return_to_resubmit: list[float] = []
    acceptance_to_report: list[float] = []
    for action in actions:
        action_events = audits_by_aggregate[action["action_item_id"]]
        published_event = next(
            (
                event
                for event in action_events
                if event["event_type"] == "ActionItemPublishedForClaim"
            ),
            None,
        )
        claimed_event = next(
            (
                event
                for event in action_events
                if event["event_type"] in ("ActionItemClaimed", "ActionItemConfirmed")
            ),
            None,
        )
        first_version = (
            min(
                versions_by_action[action["action_item_id"]],
                key=lambda row: int(row["received_sequence"]),
            )
            if versions_by_action[action["action_item_id"]]
            else None
        )
        if published_event and claimed_event:
            publish_to_claim.append(
                _hours(published_event["sim_time"], claimed_event["sim_time"])
            )
        if claimed_event and first_version:
            claim_to_submit.append(
                _hours(claimed_event["sim_time"], first_version["received_sim_time"])
            )
        for version in versions_by_action[action["action_item_id"]]:
            if version["processing_updated_sim_time"]:
                submit_to_processing.append(
                    _hours(
                        version["received_sim_time"],
                        version["processing_updated_sim_time"],
                    )
                )
            if version["processing_updated_sim_time"] and version["reviewed_sim_time"]:
                processing_to_review.append(
                    _hours(
                        version["processing_updated_sim_time"],
                        version["reviewed_sim_time"],
                    )
                )
            if version["review_status"] == "REJECTED" and version["reviewed_sim_time"]:
                next_version = next(
                    (
                        candidate
                        for candidate in versions_by_action[action["action_item_id"]]
                        if int(candidate["received_sequence"])
                        > int(version["received_sequence"])
                    ),
                    None,
                )
                if next_version:
                    return_to_resubmit.append(
                        _hours(
                            version["reviewed_sim_time"],
                            next_version["received_sim_time"],
                        )
                    )
    for accepted in accepted_results:
        report_event = next(
            (
                event
                for event in audits_by_aggregate[
                    accepted["accepted_task_result_id"]
                ]
                if event["event_type"]
                in ("TaskCollaborationReportGenerated", "TaskCollaborationReportFailed")
            ),
            None,
        )
        if report_event:
            acceptance_to_report.append(
                _hours(accepted["accepted_sim_time"], report_event["sim_time"])
            )

    funnel_counts = {
        "proposed": proposed,
        "published": sum(bool(row["published_sim_time"]) for row in actions),
        "claimed": sum(bool(row["owner_actor_id"]) for row in actions),
        "submitted": sum(bool(versions_by_action[row["action_item_id"]]) for row in actions),
        "pending_acceptance": sum(
            any(
                event["event_type"] == "ArtifactVersionAwaitingAcceptance"
                for version in versions_by_action[row["action_item_id"]]
                for event in audits_by_aggregate[version["version_id"]]
            )
            for row in actions
        ),
        "accepted": sum(
            (row["action_item_id"], row["current_valid_version_id"])
            in accepted_pairs
            for row in actions
        ),
        "confirmed": confirmed,
        "valid_delivered": valid_actions,
        "aggregated": aggregated_actions,
        "archived": archived_actions,
    }
    flow_signals = {
        "SIG-FUNNEL-001": {
            "counts": funnel_counts,
            "conversion": {
                "proposed_to_published": _rate(
                    funnel_counts["published"], proposed, "no required proposed action items"
                ),
                "published_to_claimed": _rate(
                    funnel_counts["claimed"],
                    funnel_counts["published"],
                    "no published action items",
                ),
                "claimed_to_submitted": _rate(
                    funnel_counts["submitted"],
                    funnel_counts["claimed"],
                    "no claimed action items",
                ),
                "submitted_to_pending_acceptance": _rate(
                    funnel_counts["pending_acceptance"],
                    funnel_counts["submitted"],
                    "no submitted action items",
                ),
                "pending_acceptance_to_accepted": _rate(
                    funnel_counts["accepted"],
                    funnel_counts["pending_acceptance"],
                    "no action items reached pending acceptance",
                ),
                "proposed_to_confirmed": _rate(
                    confirmed, proposed, "no required proposed action items"
                ),
                "confirmed_to_valid_delivered": _rate(
                    valid_actions, confirmed, "no confirmed action items"
                ),
                "valid_delivered_to_aggregated": _rate(
                    aggregated_actions, valid_actions, "no valid delivered action items"
                ),
                "aggregated_to_archived": _rate(
                    archived_actions, aggregated_actions, "no aggregated action items"
                ),
            },
        },
        "SIG-ONTIME-001": {
            **_rate(
                personal_on_time,
                personal_deadline_count,
                "no delivered action items with a personal commitment",
            ),
            "late_items": late_items,
        },
        "SIG-SCHEDULE-001": {
            "personal_commitment_delivery": _rate(
                personal_on_time,
                personal_deadline_count,
                "no delivered action items with a personal commitment",
            ),
            "team_need_by_delivery": _rate(
                team_on_time,
                team_deadline_count,
                "no delivered action items with a team need-by time",
            ),
            "detected_conflicts": event_counts["ScheduleConflictDetected"],
            "resolved_conflicts": event_counts["ScheduleConflictResolved"],
            "active_conflicts": active_schedule_conflicts,
            "resolution_time": _duration_summary(
                conflict_resolution_hours,
                "no schedule conflict was resolved",
            ),
            "personal_late_items": late_items,
            "team_late_items": team_late_items,
        },
        "SIG-LATENCY-001": {
            "confirmation_by_action_hours": confirmation_latency_by_action,
            "confirmation": _duration_summary(
                list(confirmation_latency_by_action.values()),
                "no confirmed action items",
            ),
            "confirmation_to_first_valid_by_action_hours": delivery_latency_by_action,
            "confirmation_to_first_valid": _duration_summary(
                list(delivery_latency_by_action.values()),
                "no action item has a confirmed-to-valid interval",
            ),
            "publish_to_claim": _duration_summary(
                publish_to_claim, "no published action item was claimed"
            ),
            "claim_to_first_submit": _duration_summary(
                claim_to_submit, "no claimed action item was submitted"
            ),
            "submit_to_processing": _duration_summary(
                submit_to_processing, "no task-result processing completed"
            ),
            "processing_to_review": _duration_summary(
                processing_to_review, "no processed version was reviewed"
            ),
            "return_to_resubmit": _duration_summary(
                return_to_resubmit, "no rejected version was resubmitted"
            ),
            "acceptance_to_collaboration_report": _duration_summary(
                acceptance_to_report, "no accepted result produced a report"
            ),
            "episode_to_final_ready": _duration_summary(
                generated_latency, "final deliverable has not been generated"
            ),
            "final_approval_wait": _duration_summary(
                final_wait, "final deliverable has not been approved"
            ),
            "episode_to_archive": _duration_summary(
                end_to_end, "episode has not been archived"
            ),
        },
    }

    touch_deliveries = [
        row for row in interventions if row["status"] == "DELIVERED"
    ]
    owner_ids = sorted(
        {row["owner_actor_id"] for row in actions if row["owner_actor_id"]}
    )
    touches_by_actor_counter = Counter(
        row["target_actor_id"] for row in touch_deliveries
    )
    touches_by_actor = {
        actor_id: touches_by_actor_counter.get(actor_id, 0) for actor_id in owner_ids
    }
    touch_mean: float | str = (
        round(sum(touches_by_actor.values()) / len(owner_ids), 2)
        if owner_ids
        else "N/A"
    )
    touch_max: int | str = max(touches_by_actor.values()) if owner_ids else "N/A"

    progress_window = timedelta(hours=fixture["policy"]["progress_window_hours"])
    observable_touches: list[dict[str, Any]] = []
    progress_events = {
        "ActionItemConfirmed",
        "ActionItemClaimed",
        "ActionItemRevisedByOwner",
        "ActionItemProgressUpdated",
        "CommitmentRevisionActivated",
        "ArtifactVersionReceived",
        "ActionItemCurrentValidVersionUpdated",
    }
    for touch in touch_deliveries:
        if touch["level"] not in ("L1", "L2"):
            continue
        out = database.one(
            "SELECT delivered_sim_time FROM outbox_entries WHERE effect_id = ?",
            (touch["effect_id"],),
        )
        if not out or not out["delivered_sim_time"]:
            continue
        start = parse_time(out["delivered_sim_time"])
        end = start + progress_window
        progressed = any(
            event["aggregate_id"] == touch["action_item_id"]
            and event["event_type"] in progress_events
            and start <= parse_time(event["sim_time"]) <= end
            for event in audits
        )
        observable_touches.append(
            {
                "level": touch["level"],
                "action_type": touch["action_type"],
                "actor_id": touch["target_actor_id"],
                "progressed": progressed,
            }
        )

    def progress_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return _rate(
            sum(bool(row["progressed"]) for row in rows),
            len(rows),
            "no observable L1/L2 touches",
        )

    progress_by_level = {
        level: progress_group(
            [row for row in observable_touches if row["level"] == level]
        )
        for level in ("L1", "L2")
    }
    progress_by_actor = {
        actor_id: progress_group(
            [row for row in observable_touches if row["actor_id"] == actor_id]
        )
        for actor_id in owner_ids
    }
    suppressed_by_level: Counter[str] = Counter()
    for event in audits:
        if event["event_type"] == "InterventionSuppressed":
            suppressed_by_level[_payload(event).get("level", "UNKNOWN")] += 1
    signal_events = [
        event for event in audits if event["event_type"] == "ProgressSignalRecorded"
    ]
    signal_types: Counter[str] = Counter()
    valid_signal_count = 0
    expired_signal_count = 0
    for event in signal_events:
        signal_payload = _payload(event)
        signal_types[str(signal_payload.get("signal_type") or "UNKNOWN")] += 1
        valid_until = signal_payload.get("valid_until")
        if valid_until and parse_time(valid_until) >= parse_time(
            episode["current_sim_time"]
        ):
            valid_signal_count += 1
        else:
            expired_signal_count += 1
    last_signal_by_action = {
        action["action_item_id"]: {
            "signal_type": action["last_owner_signal_type"],
            "signal_at": action["last_owner_signal_at"],
            "valid_until": action["last_owner_signal_valid_until"],
        }
        for action in actions
    }

    progress_inquiries = [
        row
        for row in interventions
        if row["action_type"] == "PROGRESS_INQUIRY"
    ]
    assistance_statuses = Counter(row["status"] for row in assistance_requests)
    inquiry_suppressions = Counter(
        str(_payload(event).get("reason") or "UNKNOWN")
        for event in audits
        if event["event_type"] == "ProgressInquirySuppressed"
    )
    help_response_hours: list[float] = []
    help_resolution_hours: list[float] = []
    for request in assistance_requests:
        request_events = audits_by_aggregate[request["assistance_request_id"]]
        response_event = next(
            (
                event
                for event in request_events
                if event["event_type"]
                in ("AssistanceAcknowledged", "AssistanceResolved")
            ),
            None,
        )
        if response_event:
            help_response_hours.append(
                _hours(request["created_sim_time"], response_event["sim_time"])
            )
        if request["resolved_sim_time"]:
            help_resolution_hours.append(
                _hours(request["created_sim_time"], request["resolved_sim_time"])
            )
    effect_signals = {
        "SIG-SIGNAL-001": {
            "recorded": len(signal_events),
            "valid_at_report_time": valid_signal_count,
            "expired_at_report_time": expired_signal_count,
            "by_type": dict(signal_types),
            "last_by_action": last_signal_by_action,
            "excluded_refresh_read_system_messages": 0,
            "exclusion_evidence": (
                "refresh/read/system UI events are intentionally not persisted "
                "as business signals"
            ),
        },
        "SIG-INQUIRY-001": {
            "created_by_level": dict(
                Counter(row["level"] for row in progress_inquiries)
            ),
            "budget_suppressed": sum(suppressed_by_level.values()),
            "cooldown_suppressed": inquiry_suppressions["INQUIRY_COOLDOWN"],
            "assistance_suppressed": inquiry_suppressions[
                "UNRESOLVED_ASSISTANCE"
            ],
            "response_within_progress_window": progress_group(
                [
                    row
                    for row in observable_touches
                    if row["action_type"] == "PROGRESS_INQUIRY"
                ]
            ),
            "L3": {
                "count": "N/A",
                "reason": "OUT_OF_SCOPE_P0",
            },
        },
        "SIG-HELP-001": {
            "status_counts": {
                name: assistance_statuses[name]
                for name in ("OPEN", "ACKNOWLEDGED", "RESOLVED", "CANCELLED")
            },
            "created": len(assistance_requests),
            "acknowledged": event_counts["AssistanceAcknowledged"],
            "resolved": event_counts["AssistanceResolved"],
            "cancelled": event_counts["AssistanceCancelled"],
            "first_response": _duration_summary(
                help_response_hours, "no assistance request received a response"
            ),
            "resolution": _duration_summary(
                help_resolution_hours, "no assistance request was closed"
            ),
            "valid_target_rate": _rate(
                len(assistance_requests) - len(invalid_help_targets),
                len(assistance_requests),
                "no assistance requests",
            ),
            "invalid_target_ids": invalid_help_targets,
        },
        "SIG-TOUCH-001": {
            "touches_by_actor": touches_by_actor,
            "owner_mean": touch_mean,
            "owner_max": touch_max,
            "owner_count": len(owner_ids),
            "reason": None if owner_ids else "no assigned action owners",
            "levels": dict(Counter(row["level"] for row in touch_deliveries)),
        },
        "SIG-PROGRESS-001": {
            "overall": progress_group(observable_touches),
            "by_level": progress_by_level,
            "by_actor": progress_by_actor,
            "window_hours": fixture["policy"]["progress_window_hours"],
        },
        "SIG-ESC-001": {
            "created_by_level": dict(Counter(row["level"] for row in interventions)),
            "budget_suppressed_by_level": dict(suppressed_by_level),
            "manager_escalation_enabled": False,
        },
    }

    unresolved_fields = 0
    for action in all_actions:
        raw_metadata = action["proposal_metadata"] or "{}"
        metadata = (
            json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
        )
        unresolved_fields += len(metadata.get("uncertainties", []))
    first_versions = [
        min(rows, key=lambda row: int(row["received_sequence"]))
        for rows in versions_by_action.values()
        if rows
    ]
    first_passed = sum(
        row["validation_status"] == "PASSED" for row in first_versions
    )
    failed_action_ids = {
        row["action_item_id"]
        for row in versions
        if row["validation_status"] == "FAILED"
    }
    rework_closed = 0
    for action_id in failed_action_ids:
        ordered = sorted(
            versions_by_action[action_id],
            key=lambda row: int(row["received_sequence"]),
        )
        first_failure_index = next(
            index
            for index, row in enumerate(ordered)
            if row["validation_status"] == "FAILED"
        )
        rework_closed += int(
            any(
                row["validation_status"] == "PASSED"
                for row in ordered[first_failure_index + 1 :]
            )
        )

    approval_status_names = (
        "PENDING",
        "APPROVED",
        "REJECTED",
        "SUPERSEDED",
        "FAILED",
        "EXECUTED",
    )
    approval_statuses = Counter(row["status"] for row in approvals)
    current_time = episode["current_sim_time"]
    approval_waits = [
        _hours(
            row["requested_sim_time"],
            row["decided_sim_time"] or current_time,
        )
        for row in approvals
    ]
    outbox_status_names = ("PENDING", "CLAIMED", "RETRY_WAIT", "DELIVERED", "DEAD_LETTER")
    outbox_statuses = Counter(row["status"] for row in outbox)
    attachment_statuses: Counter[str] = Counter()
    processing_error_codes: Counter[str] = Counter()
    semantic_alignment_statuses: Counter[str] = Counter()
    for version in versions:
        raw_attachments = version["attachment_extractions"] or "[]"
        attachments = (
            json.loads(raw_attachments)
            if isinstance(raw_attachments, str)
            else raw_attachments
        )
        for attachment in attachments or []:
            attachment_statuses[
                str(attachment.get("extraction_status") or "UNKNOWN")
            ] += 1
        if version["processing_error_code"]:
            processing_error_codes[str(version["processing_error_code"])] += 1
        raw_result = version["processing_result"]
        if raw_result:
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            alignment = (result.get("task_alignment") or {}).get("status")
            if alignment:
                semantic_alignment_statuses[str(alignment)] += 1
    auth_rejection_events = [
        event
        for event in audits
        if event["event_type"]
        in ("AuthenticationRejected", "AuthorizationRejected")
    ]
    auth_rejections_by_reason = Counter(
        str(_payload(event).get("reason") or "UNKNOWN")
        for event in auth_rejection_events
    )
    memory_statuses = Counter(row["status"] for row in collaboration_memories)
    node_signals = {
        "SIG-AUTH-001": {
            "principal_parse_failures": event_counts["AuthenticationRejected"],
            "authorization_rejections": event_counts["AuthorizationRejected"],
            "by_reason": dict(auth_rejections_by_reason),
            "field_projection_applications": event_counts[
                "RestrictedFieldProjectionApplied"
            ],
            "authorization_bypasses": authorization_bypasses,
            "restricted_field_exposures": restricted_field_exposures,
        },
        "SIG-CONTEXT-001": {
            "audited_lifecycle_events": (
                len(invocation_records) + len(missing_invocation_manifests)
            ),
            "call_starts": sum(
                event["event_type"]
                in (
                    "EpisodeActivated",
                    "AttachmentExtractionCompleted",
                    "TaskResultProcessingStarted",
                    "FinalOrganizationStarted",
                )
                for event, _invocation in invocation_records
            ),
            "by_purpose": dict(
                Counter(
                    str(invocation.get("purpose") or "UNKNOWN")
                    for _event, invocation in invocation_records
                )
            ),
            "output_statuses": dict(
                Counter(
                    str(invocation.get("output_status") or "UNKNOWN")
                    for _event, invocation in invocation_records
                )
            ),
            "manifest_complete_rate": _rate(
                len(invocation_records) - len(incomplete_invocation_manifests),
                len(invocation_records) + len(missing_invocation_manifests),
                "no model or Adapter invocation events",
            ),
            "missing_manifest_count": len(missing_invocation_manifests),
            "incomplete_manifest_count": len(incomplete_invocation_manifests),
            "principal_violation_count": len(invocation_principal_violations),
            "field_violation_count": len(invocation_field_violations),
            "binary_leak_count": len(invocation_binary_leaks),
            "input_hash_mismatch_count": len(invocation_hash_mismatches),
        },
        "SIG-EXTRACT-001": {
            "extracted_action_items": event_counts["ActionItemProposed"],
            "unresolved_fields": unresolved_fields,
            "identity_key_deduplicated": event_counts["ActionItemsMerged"],
            "owner_revisions": event_counts["ActionItemRevisedByOwner"],
            "rejected": event_counts["ActionItemRejectedByOwner"]
            + event_counts["ActionItemIgnored"],
        },
        "SIG-VALIDATE-001": {
            "first_validation_pass_rate": _rate(
                first_passed, len(first_versions), "no artifact versions received"
            ),
            "failed_versions": sum(
                row["validation_status"] == "FAILED" for row in versions
            ),
            "rework_closed_rate": _rate(
                rework_closed, len(failed_action_ids), "no failed versions to rework"
            ),
            "duplicate_content_hashes": event_counts[
                "ArtifactVersionDuplicateReceived"
            ],
            "versions_by_action": {
                action_id: len(rows) for action_id, rows in versions_by_action.items()
            },
            "task_result_processing": {
                "ready": sum(
                    row["processing_status"] == "READY" for row in versions
                ),
                "failed": sum(
                    row["processing_status"] == "FAILED" for row in versions
                ),
                "retry_wait": sum(
                    row["processing_status"] == "RETRY_WAIT" for row in versions
                ),
                "accepted_task_results": len(accepted_results),
                "collaboration_reports_ready": sum(
                    row["collaboration_report_status"] == "READY"
                    for row in accepted_results
                ),
                "collaboration_reports_failed": sum(
                    row["collaboration_report_status"] == "FAILED"
                    for row in accepted_results
                ),
                "memory_status_counts": dict(
                    Counter(row["status"] for row in collaboration_memories)
                ),
            },
        },
        "SIG-RESULT-001": {
            "versions_received": len(versions),
            "validation_failed_versions": sum(
                row["validation_status"] == "FAILED" for row in versions
            ),
            "attachment_extraction_statuses": dict(attachment_statuses),
            "technical_error_codes": dict(processing_error_codes),
            "semantic_alignment_statuses": dict(semantic_alignment_statuses),
            "acceptance_packages_ready": event_counts[
                "TaskResultProcessingSucceeded"
            ],
            "returned_for_revision": event_counts[
                "ArtifactVersionReturnedForRevision"
            ],
            "accepted_task_results": len(accepted_results),
            "rework_closed_rate": _rate(
                rework_closed,
                len(failed_action_ids),
                "no validation-failed version required rework",
            ),
        },
        "SIG-MEM-001": {
            "collaboration_reports_ready": sum(
                row["collaboration_report_status"] == "READY"
                for row in accepted_results
            ),
            "collaboration_reports_failed": sum(
                row["collaboration_report_status"] == "FAILED"
                for row in accepted_results
            ),
            "memory_candidates": event_counts["CollaborationMemoryProposed"],
            "memory_status_counts": dict(memory_statuses),
            "confirmed": event_counts["CollaborationMemoryConfirmed"],
            "replaced": event_counts["CollaborationMemoryReplaced"],
            "rejected": event_counts["CollaborationMemoryRejected"],
            "prohibited_label_attempts_blocked": (
                event_counts["CollaborationMemoryProhibitedLabelRejected"]
                + event_counts["CollaborationMemoryInvalidValueRejected"]
            ),
        },
        "SIG-APPROVAL-001": {
            "status_counts": {
                name: approval_statuses[name] for name in approval_status_names
            },
            "wait": _duration_summary(approval_waits, "no approvals requested"),
        },
        "SIG-OUTBOX-001": {
            "status_counts": {
                name: outbox_statuses[name] for name in outbox_status_names
            },
            "created": len(outbox),
            "claimed": (
                event_counts["OutboxEntryClaimed"]
                + event_counts["FinalOrganizationStarted"]
            ),
            "retries": sum(max(0, int(row["attempt_count"]) - 1) for row in outbox),
            "adapter_deduplicated": event_counts["OutboxDeliveryDeduplicated"],
            "delivered": outbox_statuses["DELIVERED"],
            "dead_letter": outbox_statuses["DEAD_LETTER"],
        },
    }

    effect_external_ids: defaultdict[str, set[str]] = defaultdict(set)
    for message in messages:
        effect_external_ids[message["effect_id"]].add(message["external_message_id"])
    duplicate_external_send_count = sum(
        max(0, len(external_ids) - 1)
        for external_ids in effect_external_ids.values()
    )
    gate_summary = {
        "GATE-E2E-001": _gate(
            e2e_ok,
            episode_archived=episode["status"] == "ARCHIVED",
            required_action_items_completed=archived_actions,
            required_action_items_total=len(actions),
            accepted_task_results_current=(
                len(actions) - len(missing_accepted_results)
            ),
            accepted_task_results_total=len(actions),
            final_released=bool(final and final["status"] == "RELEASED"),
        ),
        "GATE-DUP-001": _gate(
            duplicate_external_send_count == 0 and duplicate_send_ok,
            duplicate_external_send_count=duplicate_external_send_count,
            duplicate_effects=duplicate_effects,
        ),
        "GATE-VER-001": _gate(
            lineage_ok,
            stale_field_count=len(stale_lineage) + len(hash_mismatches),
            missing_lineage_count=len(missing_lineage),
            missing_accepted_task_result_count=len(missing_accepted_results),
            accepted_result_version_mismatch_count=(
                len(stale_result_refs) + len(missing_result_refs)
            ),
            stale_accepted_result_ref_count=len(stale_result_refs),
            missing_accepted_result_ref_count=len(missing_result_refs),
            stale_lineage=stale_lineage,
            missing_lineage=missing_lineage,
            extra_lineage=extra_lineage,
            hash_mismatches=hash_mismatches,
            missing_accepted_task_results=missing_accepted_results,
            stale_accepted_result_refs=stale_result_refs,
            missing_accepted_result_refs=missing_result_refs,
        ),
        "GATE-GUARD-001": _gate(
            guard_ok,
            authorization_bypass_count=authorization_bypasses,
            restricted_field_exposure_count=restricted_field_exposures,
            final_release_bypass_count=len(bypasses),
            approval_bypass_count=len(bypasses),
            touch_budget_violation_count=len(budget_violations),
            invalid_help_target_count=len(invalid_help_targets),
            missing_invocation_manifest_count=len(missing_invocation_manifests),
            incomplete_invocation_manifest_count=len(
                incomplete_invocation_manifests
            ),
            invocation_principal_violation_count=len(
                invocation_principal_violations
            ),
            invocation_field_violation_count=len(invocation_field_violations),
            invocation_binary_leak_count=len(invocation_binary_leaks),
            invocation_input_hash_mismatch_count=len(invocation_hash_mismatches),
            approval_bypasses=bypasses,
            budget_violations=budget_violations,
            invalid_help_target_ids=invalid_help_targets,
            invalid_invocation_event_ids=sorted(
                {
                    *missing_invocation_manifests,
                    *incomplete_invocation_manifests,
                    *invocation_principal_violations,
                    *invocation_field_violations,
                    *invocation_binary_leaks,
                    *invocation_hash_mismatches,
                }
            ),
        ),
        "GATE-REC-001": _gate(
            recovery_ok,
            recovery_scenario_passed=recovery_ok,
            recovered_claimed_entries=reclaimed,
            deduplicated_deliveries=deduplicated,
            outbox_status_summary=dict(outbox_statuses),
        ),
    }
    legacy_gates = {
        "G1_e2e_closure": gate_summary["GATE-E2E-001"],
        "G2_zero_duplicate_sends": gate_summary["GATE-DUP-001"],
        "G3_version_and_lineage": gate_summary["GATE-VER-001"],
        "G4_no_guard_bypass": gate_summary["GATE-GUARD-001"],
        "G5_critical_recovery": gate_summary["GATE-REC-001"],
    }
    failed_objects = sorted(
        {
            *(row["action_item_id"] for row in actions if row["status"] != "ARCHIVED"),
            *bypasses,
            *budget_violations.keys(),
            *(row["field_path"] for row in stale_lineage),
            *missing_lineage,
            *hash_mismatches,
            *invalid_help_targets,
            *missing_invocation_manifests,
            *incomplete_invocation_manifests,
            *invocation_principal_violations,
            *invocation_field_violations,
            *invocation_binary_leaks,
            *invocation_hash_mismatches,
        }
    )
    evidence_refs = {
        "run_id": run_id,
        "episode_id": episode_id,
        "audit_sequence": {
            "first": audits[0]["sequence_no"] if audits else None,
            "last": audits[-1]["sequence_no"] if audits else None,
            "count": len(audits),
        },
        "failed_object_ids": failed_objects,
        "final_deliverable_id": final["final_deliverable_id"] if final else None,
        "fixture_pack_id": fixture["pack_id"],
    }
    required_flow_signals = {
        "SIG-FUNNEL-001",
        "SIG-SCHEDULE-001",
        "SIG-LATENCY-001",
    }
    required_effect_signals = {
        "SIG-SIGNAL-001",
        "SIG-INQUIRY-001",
        "SIG-HELP-001",
        "SIG-TOUCH-001",
    }
    required_node_signals = {
        "SIG-AUTH-001",
        "SIG-CONTEXT-001",
        "SIG-EXTRACT-001",
        "SIG-RESULT-001",
        "SIG-MEM-001",
        "SIG-APPROVAL-001",
        "SIG-OUTBOX-001",
    }
    report_validation_errors = [
        *(
            f"missing flow signal: {name}"
            for name in sorted(required_flow_signals - set(flow_signals))
        ),
        *(
            f"missing effect signal: {name}"
            for name in sorted(required_effect_signals - set(effect_signals))
        ),
        *(
            f"missing node signal: {name}"
            for name in sorted(required_node_signals - set(node_signals))
        ),
    ]
    report_valid = not report_validation_errors
    passed = report_valid and all(
        value["passed"] for value in gate_summary.values()
    )
    return {
        "schema_version": "1.3",
        "run_id": run_id,
        "episode_id": episode_id,
        "report_valid": report_valid,
        "report_validation_errors": report_validation_errors,
        "passed": passed,
        "gate_summary": gate_summary,
        "flow_signals": flow_signals,
        "effect_signals": effect_signals,
        "node_signals": node_signals,
        "evidence_refs": evidence_refs,
        # Backward-compatible aliases for the current CLI and workbench.
        "gates": legacy_gates,
        "signals": {
            "flow": flow_signals,
            "effect": effect_signals,
            "node": node_signals,
        },
        "evidence": {
            **evidence_refs,
            "fixture_source": fixture["source"],
        },
    }
