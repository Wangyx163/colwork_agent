from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .extraction import (
    align_collaboration_evidence,
    validate_extraction,
    validate_source_evidence,
)
from .models import (
    ActionItemStatus,
    EpisodeStatus,
    canonical_json,
    iso_time,
    read_text_file,
    stable_hash,
)
from .service import CoordinationService
from .store import Database


DEFAULT_POLICY = {
    "confirmation_timeout_hours": 4,
    "l1_lead_hours": 24,
    "l2_wait_hours": 12,
    "l3_overdue_hours": 24,
    "daily_touch_budget": 2,
    "progress_window_hours": 24,
    "check_in_lead_hours": 24,
    "silence_window_hours": 12,
    "inquiry_cooldown_hours": 12,
    "signal_default_ttl_hours": 24,
    "outbox_max_attempts": 5,
}


def load_meeting_service(
    database: Database,
    *,
    extraction_path: str | Path,
    transcript_path: str | Path,
    organization_name: str,
    coordinator_name: str,
    participant_names: Iterable[str] = (),
    timezone: str = "Australia/Sydney",
    im: object | None = None,
) -> CoordinationService:
    """Import one extraction as coordinator-dispatchable action items."""
    extraction = json.loads(read_text_file(extraction_path))
    transcript = read_text_file(transcript_path)
    items = validate_extraction(extraction)
    validate_source_evidence(items, transcript)
    items, _ = align_collaboration_evidence(
        items, transcript, authorized_speaker=coordinator_name
    )
    if not items:
        raise ValueError("extraction contains no action items")
    organization_name = organization_name.strip()
    coordinator_name = coordinator_name.strip()
    if not organization_name or not coordinator_name:
        raise ValueError("organization and coordinator names are required")
    participants: list[str] = []
    seen_names: set[str] = set()
    for raw_name in participant_names:
        name = str(raw_name or "").strip()
        if not name or name.casefold() in seen_names:
            continue
        participants.append(name)
        seen_names.add(name.casefold())
    if not participants:
        raise ValueError(
            "at least one explicit meeting participant is required; "
            "the roster is the P0 authorization boundary"
        )
    coordinator_is_participant = any(
        name.casefold() == coordinator_name.casefold() for name in participants
    )

    source_hash = extraction.get("input_sha256") or stable_hash(transcript)
    source_key = str(source_hash)[:20]
    episode_id = f"episode_meeting_{source_key}"
    run_id = f"run_meeting_{source_key}"
    organization_id = f"org_{stable_hash(organization_name)[:16]}"
    coordinator_id = f"actor_{stable_hash([organization_id, coordinator_name])[:16]}"
    participant_actors = [
        {
            "actor_id": f"actor_{stable_hash([organization_id, name])[:16]}",
            "display_name": name,
            "roles": ["PARTICIPANT"],
        }
        for name in participants
        if name.casefold() != coordinator_name.casefold()
    ]
    actor_id_by_name = {coordinator_name.casefold(): coordinator_id}
    actor_id_by_name.update(
        {
            participant["display_name"].casefold(): participant["actor_id"]
            for participant in participant_actors
        }
    )
    confirmed_aliases = {
        str(alias).strip().casefold(): str(canonical).strip()
        for alias, canonical in dict(
            (extraction.get("human_review") or {}).get("confirmed_aliases") or {}
        ).items()
        if str(alias).strip() and str(canonical).strip()
    }
    extraction_invocation = dict(extraction.get("invocation") or {})
    extraction_principal = dict(extraction_invocation.get("principal") or {})
    extraction_principal.update(
        {
            "actor_id": "SYSTEM",
            "episode_id": episode_id,
            "roles": ["SYSTEM"],
            "auth_source": "IMPORTED_EXTRACTION_ARTIFACT",
        }
    )
    extraction_invocation.update(
        {
            "capability_type": "MODEL",
            "principal": extraction_principal,
            "purpose": "ACTION_ITEM_EXTRACTION",
            "field_allowlist": extraction_invocation.get("field_allowlist")
            or ["transcript", "meeting_date"],
            "entity_versions": extraction_invocation.get("entity_versions")
            or {"transcript_sha256": source_hash},
            "prompt_version": extraction_invocation.get("prompt_version")
            or extraction.get("prompt_version")
            or "legacy-unversioned",
            "skill_version": None,
            "input_hash": extraction_invocation.get("input_hash") or source_hash,
            "output_status": "IMPORTED",
        }
    )
    now = datetime.now(ZoneInfo(timezone))
    start = iso_time(now)
    cutoff = iso_time(now + timedelta(days=365))

    action_configs: list[dict[str, Any]] = []
    prepared: list[tuple[str, str, str, Any, dict[str, Any]]] = []
    for item in items:
        item_key = stable_hash(
            [source_hash, item.source_timestamp, item.title, item.deliverable]
        )
        action_item_id = f"ai_{item_key[:20]}"
        deliverable_key = f"deliverable_{item_key[:20]}"
        collaborator_names: list[str] = []
        unresolved_collaborators: list[str] = []
        for raw_name in item.collaborator_names:
            canonical_name = confirmed_aliases.get(raw_name.casefold(), raw_name)
            actor_id = actor_id_by_name.get(canonical_name.casefold())
            if not actor_id:
                unresolved_collaborators.append(raw_name)
                continue
            if item.owner_name and canonical_name.casefold() == item.owner_name.casefold():
                continue
            if canonical_name not in collaborator_names:
                collaborator_names.append(canonical_name)
        collaborator_actor_ids = [
            actor_id_by_name[name.casefold()] for name in collaborator_names
        ]
        uncertainties = list(item.uncertainties)
        if unresolved_collaborators:
            uncertainties.append(
                "以下协作者不在显式参会名单，未自动加入："
                + "、".join(unresolved_collaborators)
            )
        metadata = {
            "source_timestamp": item.source_timestamp,
            "source_quote": item.source_quote,
            "deliverable": item.deliverable,
            "acceptance_criteria": "",
            "work_requirements": item.deliverable,
            "management_review_policy": "",
            "priority": "P1",
            "confidence": item.confidence,
            "uncertainties": uncertainties,
            "needs_confirmation": item.needs_confirmation,
            "suggested_owner_name": item.owner_name,
            "suggested_collaborator_names": item.collaborator_names,
            "collaboration_mode": (
                "RECORDED_COLLABORATION" if collaborator_names else "SOLO"
            ),
            "collaborator_names": collaborator_names,
            "collaborator_actor_ids": collaborator_actor_ids,
            "suggested_deadline_text": item.deadline_text,
            "suggested_deadline_iso": item.deadline_iso,
            "required_fields": ["summary"],
            "requires_human_acceptance": True,
            "extraction_provider": extraction.get("provider"),
            "extraction_model": extraction.get("model"),
            "extraction_prompt_version": extraction_invocation.get(
                "prompt_version"
            ),
            "input_sha256": source_hash,
        }
        action_configs.append(
            {
                "action_item_id": action_item_id,
                "title": item.title,
                "deliverable_key": deliverable_key,
                "required_fields": ["summary"],
            }
        )
        prepared.append(
            (action_item_id, deliverable_key, item_key, item, metadata)
        )

    fixture = {
        "pack_id": f"bailian-meeting-{source_key}",
        "pack_version": "1.0.0",
        "source": {
            "type": "bailian_extraction",
            "input_sha256": source_hash,
            "model": extraction.get("model"),
        },
        "timezone": timezone,
        "start_sim_time": start,
        "evaluation_cutoff_sim_time": cutoff,
        "organization": {
            "organization_id": organization_id,
            "name": organization_name,
        },
        "actors": [
            {
                "actor_id": coordinator_id,
                "display_name": coordinator_name,
                "roles": [
                    "COORDINATOR",
                    *(["PARTICIPANT"] if coordinator_is_participant else []),
                ],
            },
            *participant_actors,
        ],
        "transcript": [],
        "policy": dict(DEFAULT_POLICY),
        "action_items": action_configs,
    }
    service = CoordinationService(
        database, fixture, episode_id=episode_id, run_id=run_id, im=im
    )
    existing_episode = database.one(
        "SELECT episode_id, current_sim_time FROM episodes WHERE episode_id = ?",
        (episode_id,),
    )
    if existing_episode:
        if participants:
            stored_names = {
                row["display_name"]
                for row in database.all(
                    "SELECT a.display_name FROM episode_participants ep "
                    "JOIN actors a ON a.actor_id = ep.actor_id "
                    "WHERE ep.episode_id = ? AND ep.role IN "
                    "('PARTICIPANT','ACTION_OWNER')",
                    (episode_id,),
                )
            }
            if stored_names != set(participants):
                raise ValueError(
                    "this meeting already exists with a different participant "
                    "roster; use its original roster or import a new meeting"
                )
        backfill_keys = (
            "suggested_collaborator_names",
            "collaboration_mode",
            "collaborator_names",
            "collaborator_actor_ids",
        )
        with database.transaction() as cursor:
            for action_item_id, _, _, _, prepared_metadata in prepared:
                row = cursor.execute(
                    "SELECT proposal_metadata FROM action_items "
                    "WHERE episode_id = ? AND action_item_id = ?",
                    (episode_id, action_item_id),
                ).fetchone()
                if not row:
                    continue
                metadata = row["proposal_metadata"]
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                metadata = dict(metadata or {})
                added = {
                    key: prepared_metadata[key]
                    for key in backfill_keys
                    if key not in metadata
                }
                if not added:
                    continue
                metadata.update(added)
                cursor.execute(
                    "UPDATE action_items SET proposal_metadata = ?, "
                    "version = version + 1 WHERE action_item_id = ?",
                    (canonical_json(metadata), action_item_id),
                )
                database.append_audit(
                    cursor,
                    run_id=run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="ActionItemCollaborationPolicyBackfilled",
                    sim_time=existing_episode["current_sim_time"],
                    payload={"added_fields": added},
                    correlation_id=f"corr_collaboration_backfill_{source_key}",
                )
        service.reconcile_legacy_collaborator_candidates()
        return service

    correlation_id = f"corr_import_{source_key}"
    with database.transaction() as cursor:
        organization = cursor.execute(
            "SELECT organization_id FROM organizations WHERE organization_id = ?",
            (organization_id,),
        ).fetchone()
        if not organization:
            cursor.execute(
                "INSERT INTO organizations(organization_id, name, status, created_at) "
                "VALUES (?, ?, 'ACTIVE', ?)",
                (organization_id, organization_name, start),
            )
        coordinator = cursor.execute(
            "SELECT actor_id FROM actors WHERE actor_id = ?", (coordinator_id,)
        ).fetchone()
        if not coordinator:
            cursor.execute(
                "INSERT INTO actors(actor_id, organization_id, display_name, "
                "actor_type, status) VALUES (?, ?, ?, 'HUMAN_USER', 'ACTIVE')",
                (coordinator_id, organization_id, coordinator_name),
            )
        cursor.execute(
            """
            INSERT INTO episodes(
                episode_id, organization_id, run_id, content_pack_id,
                owner_actor_id, status, transcript, current_sim_time,
                created_sim_time, evaluation_cutoff_sim_time, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                episode_id,
                organization_id,
                run_id,
                fixture["pack_id"],
                coordinator_id,
                EpisodeStatus.ACTIVE,
                transcript,
                start,
                start,
                cutoff,
            ),
        )
        cursor.execute(
            "INSERT INTO episode_participants(episode_id, actor_id, role) "
            "VALUES (?, ?, 'COORDINATOR')",
            (episode_id, coordinator_id),
        )
        if coordinator_is_participant:
            cursor.execute(
                "INSERT INTO episode_participants(episode_id, actor_id, role) "
                "VALUES (?, ?, 'PARTICIPANT')",
                (episode_id, coordinator_id),
            )
        for participant in participant_actors:
            actor = cursor.execute(
                "SELECT actor_id FROM actors WHERE actor_id = ?",
                (participant["actor_id"],),
            ).fetchone()
            if not actor:
                cursor.execute(
                    "INSERT INTO actors(actor_id, organization_id, display_name, "
                    "actor_type, status) VALUES (?, ?, ?, 'HUMAN_USER', 'ACTIVE')",
                    (
                        participant["actor_id"],
                        organization_id,
                        participant["display_name"],
                    ),
                )
            cursor.execute(
                "INSERT INTO episode_participants(episode_id, actor_id, role) "
                "VALUES (?, ?, 'PARTICIPANT')",
                (episode_id, participant["actor_id"]),
            )
        database.append_audit(
            cursor,
            run_id=run_id,
            aggregate_type="Episode",
            aggregate_id=episode_id,
            event_type="EpisodeActivated",
            sim_time=start,
            payload={
                "content_pack_id": fixture["pack_id"],
                "source": "bailian_extraction",
                "extraction_invocation": extraction_invocation,
                "human_review": extraction.get("human_review"),
            },
            correlation_id=correlation_id,
        )
        for action_item_id, deliverable_key, identity_key, item, metadata in prepared:
            cursor.execute(
                """
                INSERT INTO action_items(
                    action_item_id, episode_id, identity_key, title,
                    deliverable_key, owner_actor_id, required, status,
                    deadline_sim_time, team_required_by_sim_time, sla_id,
                    source_message_id, source_span,
                    proposal_metadata, created_sim_time, version
                ) VALUES (?, ?, ?, ?, ?, NULL, TRUE, ?, NULL, NULL,
                          'sla_default', ?, ?, ?, ?, 1)
                """,
                (
                    action_item_id,
                    episode_id,
                    identity_key,
                    item.title,
                    deliverable_key,
                    ActionItemStatus.PENDING_CONFIRMATION,
                    f"transcript:{item.source_timestamp}",
                    item.source_quote,
                    canonical_json(metadata),
                    start,
                ),
            )
            database.append_audit(
                cursor,
                run_id=run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemProposed",
                sim_time=start,
                payload={
                    "owner_actor_id": None,
                    "deliverable_key": deliverable_key,
                    "source_timestamp": item.source_timestamp,
                    "confidence": item.confidence,
                    "claimable": True,
                },
                correlation_id=correlation_id,
            )
    return service
