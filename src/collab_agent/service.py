from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .attachments import extract_attachments
from .content_pack import validate_content_pack
from .mock_im import MockIM
from .memory_lexicon import (
    SELF_DECLARED,
    SYSTEM_OBSERVED,
    canonical_topic,
    memory_value,
    projected_value,
    topic_origin,
)
from .memory_nomination import MEMORY_NOMINATION_PROMPT_VERSION
from .models import (
    ActionItemStatus,
    AssignmentResponse,
    AssignmentRole,
    ApprovalStatus,
    EpisodeStatus,
    OutboxStatus,
    SimulatedCrash,
    ValidationStatus,
    canonical_json,
    effect_id,
    iso_time,
    parse_time,
    read_text_file,
    stable_hash,
)
from .question_ballot import build_question_ballot_draft
from .store import Database


# Human-facing effects. These are event-driven and must always reach their
# recipient, so they are created directly rather than through _plan_intervention:
# a nudge may be suppressed by the daily touch budget and the cooldown, but a
# dispatch someone has to respond to may not.
NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED = "ASSIGNMENT_RESPONSE_REQUIRED"
NOTIFY_VOTE_REQUIRED = "VOTE_REQUIRED"
NOTIFY_REVIEW_DECIDED = "REVIEW_DECIDED"
NOTIFY_ASSISTANCE_REQUESTED = "ASSISTANCE_REQUESTED"
NOTIFY_RESULT_PENDING_REVIEW = "RESULT_PENDING_REVIEW"
NOTIFY_TASK_AMENDED = "TASK_AMENDED"
#: The two things a task owner may change about their own task, in the words
#: a reader uses for them rather than the column names.
AMENDABLE_FIELD_NAMES = {"title": "任务名称", "deliverable": "任务说明"}
NOTIFICATION_EFFECT_TYPES = frozenset(
    {
        NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED,
        NOTIFY_VOTE_REQUIRED,
        NOTIFY_REVIEW_DECIDED,
        NOTIFY_ASSISTANCE_REQUESTED,
        NOTIFY_RESULT_PENDING_REVIEW,
        NOTIFY_TASK_AMENDED,
    }
)

TASK_RESULT_MAX_ATTEMPTS = 3
# Seconds to wait before attempt N+1. A provider blip or a transient local
# socket failure needs real elapsed time to clear; without this the worker
# poll loop burns every attempt within seconds of the first failure.
TASK_RESULT_RETRY_BACKOFF_SECONDS = (5, 20, 60)


def wall_clock_now() -> str:
    """Real elapsed time, for retry gating only.

    Domain timestamps stay on VirtualClock so scenarios remain reproducible;
    a backoff deadline expressed in simulated time would never expire in the
    live workbench, where the clock only advances on explicit business events.
    """

    return datetime.now(UTC).isoformat()


def task_result_retry_deadline(attempt_count: int) -> str:
    index = min(max(attempt_count, 1), len(TASK_RESULT_RETRY_BACKOFF_SECONDS)) - 1
    delay = TASK_RESULT_RETRY_BACKOFF_SECONDS[index]
    return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()


def load_fixture(path: str | Path) -> dict[str, Any]:
    return validate_content_pack(json.loads(read_text_file(path)))


class CoordinationService:
    ATTACHMENT_EXTRACTOR_VERSION = "attachment-extractor.v1"
    TASK_RESULT_CONTEXT_FIELDS = [
        "processing_purpose",
        "task_contract.title",
        "task_contract.deliverable",
        "task_contract.acceptance_criteria",
        "task_contract.work_requirements",
        "task_contract.management_review_policy",
        "task_contract.meeting_source_timestamp",
        "task_contract.meeting_source_quote",
        "submission_claim.summary",
        "submission_claim.content",
        "submission_claim.completion_note",
        "submission_claim.submitted_by_actor_id",
        "submission_claim.contributor_role",
        "evidence.links[].url",
        "evidence.links[].inspection_status",
        "evidence.attachments[].metadata",
        "evidence.attachments[].extracted_text",
        "previous_version_context[]",
        "collaboration_inputs.upstream_results[]",
        "collaboration_inputs.collective_decision",
    ]
    FINAL_ORGANIZATION_CONTEXT_FIELDS = [
        "action_item_id",
        "title",
        "owner",
        "deliverable",
        "acceptance_criteria",
        "deadline",
        "version_id",
        "payload.summary",
        "payload.content",
        "payload.links",
        "attachments[].extracted_text",
        "accepted_task_result",
    ]
    COLLABORATION_HINT_CONTEXT_FIELDS = [
        "purpose",
        "action_item_id",
        "subject_actor_id",
        "confirmed_memories[].memory_id",
        "confirmed_memories[].topic",
        "confirmed_memories[].statement",
        "confirmed_memories[].version",
        "confirmed_memories[].evidence_refs",
    ]
    QUESTION_BALLOT_CONTEXT_FIELDS = [
        "sources[].source_ref",
        "sources[].title",
        "sources[].completion_report",
        "sources[].normalized_result",
        "sources[].submitted_content",
        "sources[].attachment_texts[].name",
        "sources[].attachment_texts[].extracted_text",
    ]
    OWNER_SIGNAL_TYPES = {
        "CLAIMED",
        "ASSIGNMENT_ACCEPTED",
        "PROMISE_REVISED",
        "ON_TRACK",
        "AT_RISK",
        "BLOCKED",
        "WAITING_INPUT",
        "READY_TO_SUBMIT",
        "ASSISTANCE_REQUESTED",
        "ARTIFACT_SUBMITTED",
        "REWORK_RESPONSE",
    }
    QUICK_SIGNAL_TYPES = {
        "ON_TRACK",
        "AT_RISK",
        "BLOCKED",
        "WAITING_INPUT",
        "READY_TO_SUBMIT",
    }

    #: Named so the page offering these buttons and the guard rejecting
    #: anything else read from one list. Inline, they drifted the moment a
    #: category was added on one side.
    ASSISTANCE_CATEGORIES = (
        "EXPERTISE",
        "CAPACITY",
        "DEPENDENCY",
        "DECISION",
        "OTHER",
    )

    def __init__(
        self,
        database: Database,
        fixture: dict[str, Any],
        *,
        episode_id: str = "episode_p0",
        run_id: str = "run_p0",
        im: Any | None = None,
        memory_nominator: Any | None = None,
    ):
        # Off unless something injects one. The counting rules below need no
        # provider, no network and no tokens, so a demo and the evaluation both
        # run without a model deciding anything about a person.
        self.memory_nominator = memory_nominator
        self.db = database
        self.fixture = fixture
        # The IM adapter is injected so a live tenant (FeishuIM) can replace the
        # deterministic MockIM without touching the dispatcher. Both honour the
        # same EffectId idempotency contract, so the evaluation stays valid.
        self.im = im if im is not None else MockIM(database)
        self._episode_id = episode_id
        self._run_id = run_id
        self.action_config = {
            item["action_item_id"]: item for item in fixture["action_items"]
        }

    @property
    def episode_id(self) -> str:
        return self._episode_id

    @property
    def run_id(self) -> str:
        return self._run_id

    def actor_with_role(self, role: str) -> str:
        role_aliases = {
            "COORDINATOR": ("COORDINATOR", "AGGREGATOR"),
            "AGGREGATOR": ("COORDINATOR", "AGGREGATOR"),
            "PARTICIPANT": ("PARTICIPANT", "ACTION_OWNER"),
            "ACTION_OWNER": ("PARTICIPANT", "ACTION_OWNER"),
        }
        allowed = role_aliases.get(role, (role,))
        placeholders = ", ".join("?" for _ in allowed)
        row = self.db.one(
            "SELECT actor_id FROM episode_participants "
            f"WHERE episode_id = ? AND role IN ({placeholders}) "
            "ORDER BY CASE role WHEN ? THEN 0 ELSE 1 END, actor_id LIMIT 1",
            (self.episode_id, *allowed, allowed[0]),
        )
        if not row:
            raise ValueError(f"episode has no {role} actor")
        return row["actor_id"]

    @property
    def aggregator_actor_id(self) -> str:
        return self.actor_with_role("COORDINATOR")

    def now(self) -> str:
        row = self.db.one(
            "SELECT current_sim_time FROM episodes WHERE episode_id = ?",
            (self.episode_id,),
        )
        if not row:
            return iso_time(self.fixture["start_sim_time"])
        return row["current_sim_time"]

    @staticmethod
    def proposal_metadata(action: Any) -> dict[str, Any]:
        raw = action["proposal_metadata"] or "{}"
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    def task_contributor_role(
        self,
        action: Any,
        actor_id: str,
        *,
        cursor: Any | None = None,
    ) -> str | None:
        """Return the actor's role inside one task without creating sub-tasks."""

        if action["owner_actor_id"] == actor_id:
            return "OWNER"
        definition_version = int(action["definition_version"] or 1)
        assignment_query = (
            "SELECT assignment_role, response_status FROM action_item_assignments "
            "WHERE action_item_id = ? AND definition_version = ? AND actor_id = ?"
        )
        assignment_parameters = (
            action["action_item_id"],
            definition_version,
            actor_id,
        )
        assignment = (
            cursor.execute(assignment_query, assignment_parameters).fetchone()
            if cursor is not None
            else self.db.one(assignment_query, assignment_parameters)
        )
        if assignment:
            if (
                assignment["assignment_role"] == AssignmentRole.COLLABORATOR
                and assignment["response_status"] == AssignmentResponse.ACCEPTED
                and action["status"]
                in {
                    ActionItemStatus.TRACKING,
                    ActionItemStatus.PENDING_ACCEPTANCE,
                    ActionItemStatus.ACCEPTED,
                    ActionItemStatus.AGGREGATED,
                }
            ):
                return "MEETING_COLLABORATOR"
            return None
        metadata = self.proposal_metadata(action)
        if actor_id in metadata.get("collaborator_actor_ids", []):
            return "MEETING_COLLABORATOR"
        query = (
            "SELECT 1 FROM assistance_requests WHERE episode_id = ? "
            "AND action_item_id = ? AND target_actor_id = ? "
            "AND status IN ('OPEN','ACKNOWLEDGED') LIMIT 1"
        )
        parameters = (self.episode_id, action["action_item_id"], actor_id)
        active_help = (
            cursor.execute(query, parameters).fetchone()
            if cursor is not None
            else self.db.one(query, parameters)
        )
        return "REQUESTED_COLLABORATOR" if active_help else None

    @staticmethod
    def artifact_is_contribution(version: Any, owner_actor_id: str | None) -> bool:
        """A collaborator version is evidence for a task, not its final candidate."""

        submitted_by = version["submitted_by_actor_id"]
        return bool(
            submitted_by
            and owner_actor_id
            and submitted_by != owner_actor_id
            and version["review_status"] == "NOT_REQUIRED"
        )

    @staticmethod
    def _decoded_json(value: Any, fallback: Any) -> Any:
        if value is None:
            return fallback
        if isinstance(value, str):
            return json.loads(value)
        return value

    def _dependency_rows(
        self, action_item_id: str, *, cursor: Any | None = None
    ) -> list[Any]:
        query = (
            "SELECT d.*, upstream.title AS upstream_title, "
            "upstream.status AS upstream_status, "
            "upstream.current_valid_version_id, "
            "r.accepted_task_result_id FROM action_item_dependencies d "
            "JOIN action_items upstream ON upstream.action_item_id = "
            "d.upstream_action_item_id "
            "LEFT JOIN accepted_task_results r ON r.accepted_version_id = "
            "upstream.current_valid_version_id "
            "WHERE d.episode_id = ? AND d.downstream_action_item_id = ? "
            "ORDER BY d.upstream_action_item_id"
        )
        parameters = (self.episode_id, action_item_id)
        rows = (
            cursor.execute(query, parameters).fetchall()
            if cursor is not None
            else self.db.all(query, parameters)
        )
        return list(rows)

    def collaboration_progress(
        self, action_item_id: str, *, cursor: Any | None = None
    ) -> dict[str, Any] | None:
        """Derive P1 progress from existing tasks and lightweight relations."""

        dependencies = self._dependency_rows(action_item_id, cursor=cursor)
        contribution_query = (
            "SELECT c.*, actor.display_name FROM action_item_participation_inputs c "
            "JOIN actors actor ON actor.actor_id = c.actor_id "
            "WHERE c.episode_id = ? AND c.action_item_id = ? "
            "ORDER BY c.contribution_type, actor.display_name, c.actor_id"
        )
        parameters = (self.episode_id, action_item_id)
        contribution_rows = (
            cursor.execute(contribution_query, parameters).fetchall()
            if cursor is not None
            else self.db.all(contribution_query, parameters)
        )
        if not dependencies and not contribution_rows:
            return None

        action_query = (
            "SELECT proposal_metadata FROM action_items WHERE action_item_id = ?"
        )
        action_row = (
            cursor.execute(action_query, (action_item_id,)).fetchone()
            if cursor is not None
            else self.db.one(action_query, (action_item_id,))
        )
        structure = (
            self._decoded_json(action_row["proposal_metadata"], {}) or {}
        ).get("collaboration_structure") or {}

        dependency_items: list[dict[str, Any]] = []
        for row in dependencies:
            current_version_id = row["current_valid_version_id"]
            bound_version_id = row["bound_upstream_version_id"]
            satisfied = bool(current_version_id and row["accepted_task_result_id"])
            stale = bool(
                bound_version_id
                and current_version_id
                and bound_version_id != current_version_id
            )
            dependency_items.append(
                {
                    "dependency_id": row["dependency_id"],
                    "upstream_action_item_id": row["upstream_action_item_id"],
                    "upstream_title": row["upstream_title"],
                    "upstream_status": row["upstream_status"],
                    "accepted_version_id": current_version_id,
                    "accepted_task_result_id": row["accepted_task_result_id"],
                    "bound_upstream_version_id": bound_version_id,
                    "satisfied": satisfied,
                    "stale": stale,
                }
            )

        contributions: list[dict[str, Any]] = []
        for row in contribution_rows:
            contributions.append(
                {
                    "contribution_id": row["contribution_id"],
                    "actor_id": row["actor_id"],
                    "display_name": row["display_name"],
                    "contribution_type": row["contribution_type"],
                    "status": row["status"],
                    "payload": self._decoded_json(row["payload"], None),
                    "payload_ref": row["payload_ref"],
                    "submitted_sim_time": row["submitted_sim_time"],
                    "version": int(row["version"]),
                }
            )

        ballot = next(
            (
                item
                for item in contributions
                if item["contribution_type"] == "BALLOT"
                and item["status"] == "SUBMITTED"
            ),
            None,
        )
        votes = [
            item for item in contributions if item["contribution_type"] == "VOTE"
        ]
        submitted_votes = [item for item in votes if item["status"] == "SUBMITTED"]
        dependencies_ready = all(
            item["satisfied"] and not item["stale"] for item in dependency_items
        )
        vote_summary: dict[str, Any] | None = None
        if ballot:
            scores_by_option: dict[str, list[int]] = {
                str(option["option_id"]): []
                for option in ballot["payload"].get("options", [])
            }
            for vote in submitted_votes:
                for option_id, score in (vote["payload"] or {}).get("scores", {}).items():
                    if option_id in scores_by_option:
                        scores_by_option[option_id].append(int(score))
            ranked = []
            option_by_id = {
                str(option["option_id"]): option
                for option in ballot["payload"].get("options", [])
            }
            for option_id, scores in scores_by_option.items():
                option = option_by_id[option_id]
                ranked.append(
                    {
                        **option,
                        "score_total": sum(scores),
                        "score_count": len(scores),
                        "score_average": (
                            round(sum(scores) / len(scores), 4) if scores else None
                        ),
                    }
                )
            ranked.sort(
                key=lambda item: (
                    -(item["score_total"]),
                    -(item["score_average"] or 0),
                    str(item["option_id"]),
                )
            )
            selection_count = int(ballot["payload"].get("selection_count") or 8)
            vote_summary = {
                "required_vote_count": len(votes),
                "submitted_vote_count": len(submitted_votes),
                "complete": len(votes) == len(submitted_votes),
                "selection_count": selection_count,
                "ranked_options": ranked,
                "selected_options": ranked[:selection_count],
            }

        return {
            "structure_type": "QUESTION_COLLECTION_VOTE",
            "selection_count": int(structure.get("selection_count") or 8),
            "dependencies": dependency_items,
            "dependencies_ready": dependencies_ready,
            "waiting_on_action_item_ids": [
                item["upstream_action_item_id"]
                for item in dependency_items
                if not item["satisfied"] or item["stale"]
            ],
            "contributions": contributions,
            "ballot_open": ballot is not None,
            "vote_summary": vote_summary,
            "final_submission_ready": bool(
                dependencies_ready
                and ballot
                and vote_summary
                and vote_summary["complete"]
            ),
        }

    def collaboration_input_context(
        self, action_item_id: str, *, detail_level: str = "FULL"
    ) -> dict[str, Any]:
        """Return only accepted upstream results and completed aggregate votes.

        ``detail_level`` decides how much of an accepted upstream delivery
        travels downstream.  ``FULL`` is for the confirmed final owner and the
        Agent working on their behalf: they have to read what colleagues
        actually wrote in order to organize it.  ``SUMMARY`` is for everyone
        else in the structure -- they learn who delivered and in what
        direction, not the delivery body.
        """

        if detail_level not in {"FULL", "SUMMARY"}:
            raise ValueError("unsupported collaboration input detail level")
        progress = self.collaboration_progress(action_item_id)
        if not progress:
            return {"upstream_results": [], "collective_decision": None}
        upstream_results: list[dict[str, Any]] = []
        for dependency in progress["dependencies"]:
            version_id = dependency["bound_upstream_version_id"] or dependency[
                "accepted_version_id"
            ]
            if not version_id:
                continue
            accepted = self.db.one(
                "SELECT r.accepted_task_result_id, r.accepted_version_id, "
                "r.completion_report, r.normalized_result, r.source_manifest "
                "FROM accepted_task_results r WHERE r.accepted_version_id = ?",
                (version_id,),
            )
            if not accepted:
                continue
            entry = {
                "source_ref": (
                    f'upstream:{dependency["upstream_action_item_id"]}:{version_id}'
                ),
                "action_item_id": dependency["upstream_action_item_id"],
                "title": dependency["upstream_title"],
                "accepted_task_result_id": accepted["accepted_task_result_id"],
                "accepted_version_id": version_id,
                "completion_report": accepted["completion_report"],
                "detail_level": detail_level,
            }
            upstream = self.db.one(
                "SELECT v.payload, v.attachment_extractions, v.submitted_by_actor_id, "
                "a.display_name AS submitted_by_display_name, "
                "i.proposal_metadata "
                "FROM artifact_versions v "
                "JOIN action_items i ON i.action_item_id = v.action_item_id "
                "LEFT JOIN actors a ON a.actor_id = v.submitted_by_actor_id "
                "WHERE v.version_id = ?",
                (version_id,),
            )
            payload = self._decoded_json(
                upstream["payload"] if upstream else None, {}
            ) or {}
            metadata = (
                self._decoded_json(upstream["proposal_metadata"], {}) or {}
                if upstream
                else {}
            )
            entry["submitted_by_actor_id"] = (
                upstream["submitted_by_actor_id"] if upstream else None
            )
            entry["submitted_by_display_name"] = (
                upstream["submitted_by_display_name"] if upstream else None
            )
            entry["submission_summary"] = str(payload.get("summary") or "")
            entry["responsibility"] = str(metadata.get("deliverable") or "")
            if detail_level == "FULL":
                entry["normalized_result"] = self._decoded_json(
                    accepted["normalized_result"], None
                )
                # The accepted delivery body is the only place the actual work
                # lives for text-first tasks.  It cleared human acceptance, so
                # the downstream owner is reading an approved result, not an
                # unreviewed claim.
                entry["submitted_content"] = str(payload.get("content") or "")
                entry["completion_note"] = str(
                    payload.get("completion_note") or ""
                )
                entry["attachment_texts"] = [
                    {
                        "name": item.get("name"),
                        "extracted_text": item.get("extracted_text") or "",
                    }
                    for item in (
                        self._decoded_json(
                            upstream["attachment_extractions"] if upstream else None,
                            [],
                        )
                        or []
                    )
                    if (item.get("extracted_text") or "")
                ]
            upstream_results.append(entry)
        vote_summary = progress.get("vote_summary")
        collective_decision = None
        if vote_summary and vote_summary.get("complete"):
            collective_decision = {
                "source_ref": "decision:votes",
                "required_vote_count": vote_summary["required_vote_count"],
                "submitted_vote_count": vote_summary["submitted_vote_count"],
                "ranked_options": vote_summary["ranked_options"],
                "selected_options": vote_summary["selected_options"],
            }
        return {
            "upstream_results": upstream_results,
            "collective_decision": collective_decision,
        }

    def build_collaboration_hint_context(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        subject_actor_id: str | None = None,
        message_id: str,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Build a bounded hint for self or a currently active task partner."""

        self._require_participant(actor_id)
        subject_actor_id = subject_actor_id or actor_id
        self._require_participant(subject_actor_id)
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        action = self.db.one(
            "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
            (action_item_id, self.episode_id),
        )
        if not action:
            raise KeyError(action_item_id)
        if not self.task_contributor_role(action, actor_id):
            raise PermissionError(
                "collaboration hints are available only on the actor's own task or active collaboration"
            )
        if not self.task_contributor_role(action, subject_actor_id):
            raise PermissionError(
                "the memory subject is not an active contributor on this task"
            )
        rows = self.db.all(
            "SELECT memory_id, topic, value, evidence_refs, version, "
            "confirmed_sim_time FROM collaboration_memories "
            "WHERE actor_id = ? AND status = 'CONFIRMED' "
            "ORDER BY confirmed_sim_time, memory_id",
            (subject_actor_id,),
        )
        memories: list[dict[str, Any]] = []
        for row in rows:
            value = self._decoded_json(row["value"], {})
            topic = canonical_topic(row["topic"])
            shown = projected_value(topic, value)
            # Same rule as the workbench projection: an entry with no code
            # predates the lexicon and has no colleague-facing wording, so
            # there is nothing here that may be shown to somebody else.
            if shown is None:
                continue
            memories.append(
                {
                    "memory_id": row["memory_id"],
                    "topic": topic,
                    "code": shown["code"],
                    "collaborator_hint": shown["collaborator_hint"],
                    "version": int(row["version"]),
                    **(
                        {
                            "evidence_refs": self._decoded_json(
                                row["evidence_refs"], []
                            )
                        }
                        if subject_actor_id == actor_id
                        else {}
                    ),
                }
            )
        raw_context = {
            "purpose": "COLLABORATION_HINT",
            "action_item_id": action_item_id,
            "subject_actor_id": subject_actor_id,
            "confirmed_memories": memories,
            "usage_rules": {
                "may": [
                    "adapt communication detail and collaboration cadence"
                ],
                "must_not": [
                    "authorize an action",
                    "change task state",
                    "decide acceptance",
                    "trigger punitive escalation",
                    "reveal evidence or history to another actor",
                    "reuse a partner hint outside the current active task",
                ],
            },
        }
        from .context_budget import bound_collaboration_hint_context

        context, context_manifest = bound_collaboration_hint_context(
            raw_context, token_budget=token_budget
        )
        input_hash = stable_hash(context)
        invocation = self._system_invocation_manifest(
            capability_type="CONTEXT_BUILDER",
            purpose="COLLABORATION_HINT",
            field_allowlist=self.COLLABORATION_HINT_CONTEXT_FIELDS,
            entity_versions={
                "action_item_id": action_item_id,
                "action_item_version": int(action["version"]),
                "subject_actor_id": subject_actor_id,
                "memory_versions": {
                    item["memory_id"]: item["version"]
                    for item in context["confirmed_memories"]
                },
            },
            input_hash=input_hash,
            output_status="SUCCEEDED",
            context_manifest=context_manifest,
        )
        result = {
            "context": context,
            "context_manifest": context_manifest,
            "invocation": invocation,
        }
        sim_time = self.now()
        with self.db.transaction() as cursor:
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="CollaborationHintContextBuilt",
                sim_time=sim_time,
                payload={
                    "subject_actor_id": subject_actor_id,
                    "requested_by_actor_id": actor_id,
                    "included_memory_count": len(
                        context["confirmed_memories"]
                    ),
                    "invocation": invocation,
                },
                correlation_id=f"corr_{message_id}",
            )
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def _assert_dependency_edges_acyclic(
        self, edges: list[tuple[str, str]], *, cursor: Any
    ) -> None:
        existing = cursor.execute(
            "SELECT upstream_action_item_id, downstream_action_item_id "
            "FROM action_item_dependencies WHERE episode_id = ?",
            (self.episode_id,),
        ).fetchall()
        graph: dict[str, set[str]] = {}
        for row in existing:
            graph.setdefault(row["upstream_action_item_id"], set()).add(
                row["downstream_action_item_id"]
            )
        for upstream, downstream in edges:
            graph.setdefault(upstream, set()).add(downstream)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("collaboration dependencies must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for target in graph.get(node, set()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in set(graph) | {target for values in graph.values() for target in values}:
            visit(node)

    def confirm_question_vote_structure(
        self,
        *,
        collection_action_item_ids: list[str],
        decision_action_item_id: str,
        final_owner_actor_id: str,
        voter_actor_ids: list[str],
        selection_count: int,
        source_span: str,
        actor_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Confirm the one P1 compound template without a workflow entity."""

        self._require_aggregator(actor_id)
        collection_ids = list(dict.fromkeys(str(value) for value in collection_action_item_ids))
        voter_ids = list(dict.fromkeys(str(value) for value in voter_actor_ids))
        source_span = source_span.strip()
        if not collection_ids:
            raise ValueError("at least one collection action item is required")
        if decision_action_item_id in collection_ids:
            raise ValueError("decision action item cannot depend on itself")
        if not voter_ids:
            raise ValueError("at least one voter is required")
        if not 1 <= int(selection_count) <= 8:
            raise ValueError("selection_count must be between 1 and 8")
        if not source_span:
            raise ValueError("meeting source span is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action_ids = [*collection_ids, decision_action_item_id]
            placeholders = ",".join("?" for _ in action_ids)
            actions = cursor.execute(
                "SELECT * FROM action_items WHERE episode_id = ? "
                f"AND action_item_id IN ({placeholders})",
                (self.episode_id, *action_ids),
            ).fetchall()
            by_id = {row["action_item_id"]: row for row in actions}
            missing = [value for value in action_ids if value not in by_id]
            if missing:
                raise KeyError(missing[0])
            decision = by_id[decision_action_item_id]
            if (
                decision["status"] != ActionItemStatus.PENDING_CONFIRMATION
                or decision["owner_actor_id"]
                or decision["published_sim_time"]
            ):
                raise ValueError(
                    "collaboration structure must be confirmed before the decision task is published"
                )
            participant_ids = [final_owner_actor_id, *voter_ids]
            participant_placeholders = ",".join("?" for _ in participant_ids)
            participants = cursor.execute(
                "SELECT DISTINCT actor_id FROM episode_participants "
                "WHERE episode_id = ? AND role IN ('PARTICIPANT','ACTION_OWNER') "
                f"AND actor_id IN ({participant_placeholders})",
                (self.episode_id, *participant_ids),
            ).fetchall()
            confirmed_participants = {row["actor_id"] for row in participants}
            invalid_participants = [
                value for value in participant_ids if value not in confirmed_participants
            ]
            if invalid_participants:
                raise PermissionError(
                    "all collaboration members must be explicit meeting participants"
                )
            edges = [(upstream, decision_action_item_id) for upstream in collection_ids]
            self._assert_dependency_edges_acyclic(edges, cursor=cursor)

            dependency_ids: list[str] = []
            for upstream, downstream in edges:
                existing_edge = cursor.execute(
                    "SELECT dependency_id FROM action_item_dependencies "
                    "WHERE upstream_action_item_id = ? AND downstream_action_item_id = ?",
                    (upstream, downstream),
                ).fetchone()
                if existing_edge:
                    dependency_ids.append(existing_edge["dependency_id"])
                    continue
                dependency_id = f"dep_{uuid4().hex}"
                cursor.execute(
                    "INSERT INTO action_item_dependencies(dependency_id, episode_id, "
                    "upstream_action_item_id, downstream_action_item_id, gate_type, "
                    "source_span, created_by_actor_id, created_sim_time) "
                    "VALUES (?, ?, ?, ?, 'ALL_REQUIRED', ?, ?, ?)",
                    (
                        dependency_id,
                        self.episode_id,
                        upstream,
                        downstream,
                        source_span,
                        actor_id,
                        sim_time,
                    ),
                )
                dependency_ids.append(dependency_id)

            contribution_ids: list[str] = []
            for participant_id, contribution_type in [
                (final_owner_actor_id, "BALLOT"),
                *((voter_id, "VOTE") for voter_id in voter_ids),
            ]:
                contribution_id = f"input_{uuid4().hex}"
                cursor.execute(
                    "INSERT INTO action_item_participation_inputs(contribution_id, episode_id, "
                    "action_item_id, actor_id, contribution_type, status, source_span) "
                    "VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                    (
                        contribution_id,
                        self.episode_id,
                        decision_action_item_id,
                        participant_id,
                        contribution_type,
                        source_span,
                    ),
                )
                contribution_ids.append(contribution_id)

            metadata = self.proposal_metadata(decision)
            metadata["collaboration_structure"] = {
                "structure_type": "QUESTION_COLLECTION_VOTE",
                "templates": ["PARALLEL_COLLECTION", "COLLECTIVE_DECISION"],
                "collection_action_item_ids": collection_ids,
                "voter_actor_ids": voter_ids,
                "required_owner_actor_id": final_owner_actor_id,
                "selection_count": int(selection_count),
                "source_span": source_span,
                "confirmed_by_actor_id": actor_id,
            }
            cursor.execute(
                "UPDATE action_items SET proposal_metadata = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (canonical_json(metadata), decision_action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=decision_action_item_id,
                event_type="CollaborationStructureConfirmed",
                sim_time=sim_time,
                payload={
                    "structure_type": "QUESTION_COLLECTION_VOTE",
                    "collection_action_item_ids": collection_ids,
                    "final_owner_actor_id": final_owner_actor_id,
                    "voter_actor_ids": voter_ids,
                    "selection_count": int(selection_count),
                    "dependency_ids": dependency_ids,
                    "contribution_ids": contribution_ids,
                },
                correlation_id=correlation_id,
            )
            result = {
                "structure_type": "QUESTION_COLLECTION_VOTE",
                "decision_action_item_id": decision_action_item_id,
                "dependency_ids": dependency_ids,
                "contribution_ids": contribution_ids,
                "status": "CONFIRMED",
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def revoke_question_vote_structure(
        self,
        decision_action_item_id: str,
        *,
        actor_id: str,
        reason: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Undo a structure confirmed on the wrong task.

        Structure confirmation is the one P1 step with no way back, so a
        misconfiguration used to strand a task behind dependencies it never
        needed, invisible to the page that creates them.  Revocation is allowed
        only while nothing has been built on top: before the decision task is
        published and before any ballot or vote is submitted.
        """

        self._require_aggregator(actor_id)
        reason = reason.strip()
        if not reason:
            raise ValueError("a revocation reason is required")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
                (decision_action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(decision_action_item_id)
            metadata = self.proposal_metadata(action)
            structure = metadata.get("collaboration_structure") or {}
            if structure.get("structure_type") != "QUESTION_COLLECTION_VOTE":
                raise ValueError("action item has no question-vote collaboration structure")
            if action["published_sim_time"] or action["owner_actor_id"]:
                raise ValueError(
                    "the decision task is already dispatched; revoke before dispatching"
                )
            submitted = cursor.execute(
                "SELECT COUNT(*) AS count FROM action_item_participation_inputs "
                "WHERE action_item_id = ? AND status = 'SUBMITTED'",
                (decision_action_item_id,),
            ).fetchone()
            if submitted and int(submitted["count"]) > 0:
                raise ValueError(
                    "a ballot or vote has already been submitted; the structure is locked"
                )
            dependencies = cursor.execute(
                "SELECT dependency_id, upstream_action_item_id FROM "
                "action_item_dependencies WHERE episode_id = ? "
                "AND downstream_action_item_id = ?",
                (self.episode_id, decision_action_item_id),
            ).fetchall()
            inputs = cursor.execute(
                "SELECT contribution_id FROM action_item_participation_inputs "
                "WHERE episode_id = ? AND action_item_id = ?",
                (self.episode_id, decision_action_item_id),
            ).fetchall()
            cursor.execute(
                "DELETE FROM action_item_dependencies WHERE episode_id = ? "
                "AND downstream_action_item_id = ?",
                (self.episode_id, decision_action_item_id),
            )
            cursor.execute(
                "DELETE FROM action_item_participation_inputs WHERE episode_id = ? "
                "AND action_item_id = ?",
                (self.episode_id, decision_action_item_id),
            )
            removed_structure = metadata.pop("collaboration_structure")
            cursor.execute(
                "UPDATE action_items SET proposal_metadata = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (canonical_json(metadata), decision_action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=decision_action_item_id,
                event_type="CollaborationStructureRevoked",
                sim_time=sim_time,
                payload={
                    "reason": reason[:500],
                    "revoked_by_actor_id": actor_id,
                    "removed_structure": removed_structure,
                    "dependency_ids": [
                        row["dependency_id"] for row in dependencies
                    ],
                    "contribution_ids": [
                        row["contribution_id"] for row in inputs
                    ],
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "decision_action_item_id": decision_action_item_id,
                "status": "REVOKED",
                "removed_dependency_count": len(dependencies),
                "removed_participation_input_count": len(inputs),
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def prepare_question_ballot_draft(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        processing_mode: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Generate and persist an editable draft from accepted upstream results."""

        self._require_participant(actor_id)
        if processing_mode not in {"local", "bailian"}:
            raise ValueError("unsupported ballot-draft processing mode")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        action = self.db.one(
            "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
            (action_item_id, self.episode_id),
        )
        if not action:
            raise KeyError(action_item_id)
        if action["owner_actor_id"] != actor_id:
            raise PermissionError(
                "only the final task owner may prepare the ballot draft"
            )
        progress = self.collaboration_progress(action_item_id)
        if not progress or not progress["dependencies_ready"]:
            raise ValueError(
                "all collection tasks must be accepted before preparing the ballot draft"
            )
        ballot = self.db.one(
            "SELECT * FROM action_item_participation_inputs WHERE action_item_id = ? "
            "AND actor_id = ? AND contribution_type = 'BALLOT'",
            (action_item_id, actor_id),
        )
        if not ballot:
            raise PermissionError("actor is not the confirmed ballot owner")
        if ballot["status"] == "SUBMITTED":
            raise ValueError("the opened ballot is locked")
        context = self.collaboration_input_context(action_item_id)
        upstream_results = context["upstream_results"]
        draft = build_question_ballot_draft(
            upstream_results, processing_mode=processing_mode
        )
        expected_versions = sorted(
            item["accepted_version_id"] for item in upstream_results
        )
        sim_time = self.now()
        result = {
            "action_item_id": action_item_id,
            "contribution_id": ballot["contribution_id"],
            "status": "DRAFT_READY",
            **draft,
        }
        invocation = self._system_invocation_manifest(
            capability_type=("PROMPT" if draft.get("model") else "RULE"),
            purpose="QUESTION_BALLOT_DRAFT",
            field_allowlist=self.QUESTION_BALLOT_CONTEXT_FIELDS,
            entity_versions={
                "action_item_id": action_item_id,
                "action_item_version": int(action["version"]),
                "accepted_upstream_versions": expected_versions,
            },
            input_hash=draft["input_hash"],
            output_status="SUCCEEDED",
            prompt_version=draft["prompt_version"],
            context_manifest=draft.get("context_manifest"),
        )
        invocation["model"] = draft.get("model")
        invocation["usage"] = draft.get("usage") or {}
        invocation["generation_mode"] = draft["generation_mode"]
        if draft.get("attempted_model"):
            invocation["attempted_model"] = draft["attempted_model"]
        result["invocation"] = invocation
        stored_payload = {
            "draft": True,
            "options": draft["options"],
            "upstream_version_ids": expected_versions,
            "generation": {
                key: draft.get(key)
                for key in (
                    "generation_mode",
                    "model",
                    "prompt_version",
                    "repair_count",
                    "fallback_reason",
                    "attempted_model",
                    "input_hash",
                    "context_manifest",
                    "usage",
                )
                if draft.get(key) is not None
            },
        }
        with self.db.transaction() as cursor:
            current_progress = self.collaboration_progress(
                action_item_id, cursor=cursor
            )
            current_versions = sorted(
                item["accepted_version_id"]
                for item in current_progress["dependencies"]
                if item["accepted_version_id"]
            )
            if (
                not current_progress["dependencies_ready"]
                or current_versions != expected_versions
            ):
                raise ValueError(
                    "accepted upstream versions changed while preparing the ballot draft"
                )
            # Preparing the draft is the first point at which this downstream task
            # actually consumes the accepted upstream results. A task may have been
            # dispatched and accepted before those results existed, so assignment
            # activation alone cannot reliably establish lineage.
            for dependency in current_progress["dependencies"]:
                cursor.execute(
                    "UPDATE action_item_dependencies SET bound_upstream_version_id = ? "
                    "WHERE dependency_id = ?",
                    (
                        dependency["accepted_version_id"],
                        dependency["dependency_id"],
                    ),
                )
            current_ballot = cursor.execute(
                "SELECT status FROM action_item_participation_inputs "
                "WHERE contribution_id = ?",
                (ballot["contribution_id"],),
            ).fetchone()
            if not current_ballot or current_ballot["status"] == "SUBMITTED":
                raise ValueError("the opened ballot is locked")
            cursor.execute(
                "UPDATE action_item_participation_inputs SET payload = ?, payload_ref = ?, "
                "source_message_id = ?, submitted_sim_time = NULL, version = version + 1 "
                "WHERE contribution_id = ? AND status = 'PENDING'",
                (
                    canonical_json(stored_payload),
                    f'contribution:{ballot["contribution_id"]}:draft',
                    message_id,
                    ballot["contribution_id"],
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItemContribution",
                aggregate_id=ballot["contribution_id"],
                event_type="QuestionBallotDraftPrepared",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "option_count": len(draft["options"]),
                    "upstream_version_ids": expected_versions,
                    "generation": stored_payload["generation"],
                    "invocation": invocation,
                },
                correlation_id=f"corr_{message_id}",
            )
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def open_question_ballot(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        options: list[dict[str, Any]],
        message_id: str,
    ) -> dict[str, Any]:
        self._require_participant(actor_id)
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        if not isinstance(options, list) or len(options) < 2 or len(options) > 100:
            raise ValueError("ballot must contain between 2 and 100 options")
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            metadata = self.proposal_metadata(action)
            structure = metadata.get("collaboration_structure") or {}
            if structure.get("structure_type") != "QUESTION_COLLECTION_VOTE":
                raise ValueError("action item has no question-vote collaboration structure")
            if action["owner_actor_id"] != actor_id:
                raise PermissionError("only the final task owner may open the ballot")
            progress = self.collaboration_progress(action_item_id, cursor=cursor)
            if not progress or not progress["dependencies_ready"]:
                raise ValueError("all collection tasks must be accepted before opening the ballot")
            allowed_upstreams = {
                item["upstream_action_item_id"]: item["accepted_version_id"]
                for item in progress["dependencies"]
            }
            normalized_options: list[dict[str, Any]] = []
            option_ids: set[str] = set()
            option_texts: set[str] = set()
            for raw in options:
                option_id = str(raw.get("option_id") or "").strip()
                text = str(raw.get("text") or "").strip()
                upstream_id = str(raw.get("source_action_item_id") or "").strip()
                if not option_id or len(option_id) > 64 or option_id in option_ids:
                    raise ValueError("ballot option ids must be unique and at most 64 characters")
                if not text or len(text) > 1000:
                    raise ValueError("ballot option text must be between 1 and 1000 characters")
                if upstream_id not in allowed_upstreams:
                    raise ValueError("every ballot option must reference an accepted collection task")
                normalized_text = "".join(text.lower().split())
                if normalized_text in option_texts:
                    raise ValueError("ballot option text must be unique")
                raw_refs = raw.get("source_refs") or [
                    {"action_item_id": upstream_id}
                ]
                if not isinstance(raw_refs, list):
                    raise ValueError("ballot option source_refs must be a list")
                source_refs: list[dict[str, str]] = []
                for raw_ref in raw_refs:
                    if not isinstance(raw_ref, dict):
                        raise ValueError("ballot option source_refs are invalid")
                    source_action_item_id = str(
                        raw_ref.get("action_item_id") or ""
                    ).strip()
                    if source_action_item_id not in allowed_upstreams:
                        raise ValueError(
                            "every ballot source must reference an accepted collection task"
                        )
                    expected_version_id = allowed_upstreams[
                        source_action_item_id
                    ]
                    supplied_version_id = str(
                        raw_ref.get("version_id") or expected_version_id
                    ).strip()
                    if supplied_version_id != expected_version_id:
                        raise ValueError(
                            "ballot source version is stale or does not match the accepted result"
                        )
                    source_ref = {
                        "action_item_id": source_action_item_id,
                        "version_id": expected_version_id,
                    }
                    if source_ref not in source_refs:
                        source_refs.append(source_ref)
                option_ids.add(option_id)
                option_texts.add(normalized_text)
                normalized_options.append(
                    {
                        "option_id": option_id,
                        "text": text,
                        "source_action_item_id": upstream_id,
                        "source_version_id": allowed_upstreams[upstream_id],
                        "source_refs": source_refs,
                    }
                )
            ballot = cursor.execute(
                "SELECT * FROM action_item_participation_inputs WHERE action_item_id = ? "
                "AND actor_id = ? AND contribution_type = 'BALLOT'",
                (action_item_id, actor_id),
            ).fetchone()
            if not ballot:
                raise PermissionError("actor is not the confirmed ballot owner")
            if ballot["status"] == "SUBMITTED":
                raise ValueError("the opened ballot is locked")
            selection_count = int(structure.get("selection_count") or 8)
            # Keeping every candidate makes the vote decorative: the meeting
            # asked for the top 7-8 out of more, so the ballot has to offer more
            # than it keeps for scoring to select anything.
            if len(normalized_options) <= selection_count:
                raise ValueError(
                    f"the ballot keeps {selection_count} options, so it must offer "
                    f"more than {selection_count} candidates; adjust the retained "
                    "count or add candidates"
                )
            ballot_payload = {
                "options": normalized_options,
                "selection_count": selection_count,
                "upstream_version_ids": sorted(allowed_upstreams.values()),
            }
            cursor.execute(
                "UPDATE action_item_participation_inputs SET status = 'SUBMITTED', payload = ?, "
                "payload_ref = ?, source_message_id = ?, submitted_sim_time = ?, "
                "version = version + 1 WHERE contribution_id = ?",
                (
                    canonical_json(ballot_payload),
                    f'contribution:{ballot["contribution_id"]}',
                    message_id,
                    sim_time,
                    ballot["contribution_id"],
                ),
            )
            cursor.execute(
                "UPDATE action_item_participation_inputs SET status = 'PENDING', payload = NULL, "
                "payload_ref = NULL, source_message_id = NULL, submitted_sim_time = NULL, "
                "version = version + 1 WHERE action_item_id = ? "
                "AND contribution_type = 'VOTE'",
                (action_item_id,),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="QuestionBallotOpened",
                sim_time=sim_time,
                payload={
                    "ballot_contribution_id": ballot["contribution_id"],
                    "option_count": len(normalized_options),
                    "upstream_version_ids": sorted(allowed_upstreams.values()),
                },
                correlation_id=f"corr_{message_id}",
            )
            voters = [
                row["actor_id"]
                for row in cursor.execute(
                    "SELECT actor_id FROM action_item_participation_inputs "
                    "WHERE action_item_id = ? AND contribution_type = 'VOTE'",
                    (action_item_id,),
                ).fetchall()
            ]
            # Scoring needs no access to the deliverable, so it belongs inline.
            # Every option must be scored, so the card renders one control per
            # option rather than a single decision button.
            self._notify(
                cursor,
                effect_type=NOTIFY_VOTE_REQUIRED,
                recipient_actor_ids=voters,
                action_item_id=action_item_id,
                title=f'待你打分：{action["title"]}',
                summary=(
                    f"共 {len(normalized_options)} 个候选，"
                    f'保留 {ballot_payload["selection_count"]} 项；'
                    "所有指定投票人完成后才解锁定稿提交。"
                ),
                fields=[
                    {"label": f"候选 {index + 1}", "value": option["text"]}
                    for index, option in enumerate(normalized_options)
                ],
                decisions=[
                    {
                        "name": "SUBMIT_VOTE",
                        "label": "提交评分",
                        "requires_reason": False,
                        "score_options": [
                            {"option_id": option["option_id"], "text": option["text"]}
                            for option in normalized_options
                        ],
                        "score_range": [1, 5],
                    }
                ],
                correlation_id=f"corr_{message_id}",
                sim_time=sim_time,
                trigger_key=f'{action_item_id}:ballot:{ballot["contribution_id"]}',
            )
            result = {
                "action_item_id": action_item_id,
                "ballot_contribution_id": ballot["contribution_id"],
                "status": "OPEN",
                "option_count": len(normalized_options),
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def submit_question_vote(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        scores: dict[str, Any],
        message_id: str,
    ) -> dict[str, Any]:
        self._require_participant(actor_id)
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        if not isinstance(scores, dict):
            raise ValueError("vote scores must be an object")
        sim_time = self.now()
        with self.db.transaction() as cursor:
            vote = cursor.execute(
                "SELECT * FROM action_item_participation_inputs WHERE episode_id = ? "
                "AND action_item_id = ? AND actor_id = ? AND contribution_type = 'VOTE'",
                (self.episode_id, action_item_id, actor_id),
            ).fetchone()
            if not vote:
                raise PermissionError("actor is not a confirmed voter for this task")
            if vote["status"] == "SUBMITTED":
                raise ValueError("a submitted vote is locked and cannot be changed")
            ballot = cursor.execute(
                "SELECT * FROM action_item_participation_inputs WHERE episode_id = ? "
                "AND action_item_id = ? AND contribution_type = 'BALLOT' "
                "AND status = 'SUBMITTED'",
                (self.episode_id, action_item_id),
            ).fetchone()
            if not ballot:
                raise ValueError("the ballot has not been opened")
            ballot_payload = self._decoded_json(ballot["payload"], {})
            option_ids = {
                str(option["option_id"]) for option in ballot_payload.get("options", [])
            }
            if set(scores) != option_ids:
                raise ValueError("every ballot option must receive exactly one score")
            normalized_scores: dict[str, int] = {}
            for option_id, raw_score in scores.items():
                if isinstance(raw_score, bool) or not isinstance(raw_score, int):
                    raise ValueError("vote scores must be integers from 1 to 5")
                if not 1 <= raw_score <= 5:
                    raise ValueError("vote scores must be integers from 1 to 5")
                normalized_scores[str(option_id)] = int(raw_score)
            cursor.execute(
                "UPDATE action_item_participation_inputs SET status = 'SUBMITTED', payload = ?, "
                "payload_ref = ?, source_message_id = ?, submitted_sim_time = ?, "
                "version = version + 1 WHERE contribution_id = ?",
                (
                    canonical_json(
                        {
                            "ballot_contribution_id": ballot["contribution_id"],
                            "ballot_version": int(ballot["version"]),
                            "scores": normalized_scores,
                        }
                    ),
                    f'contribution:{vote["contribution_id"]}',
                    message_id,
                    sim_time,
                    vote["contribution_id"],
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItemContribution",
                aggregate_id=vote["contribution_id"],
                event_type="QuestionVoteSubmitted",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "actor_id": actor_id,
                    "ballot_contribution_id": ballot["contribution_id"],
                    "scored_option_count": len(normalized_scores),
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "action_item_id": action_item_id,
                "contribution_id": vote["contribution_id"],
                "status": "SUBMITTED",
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        result["progress"] = self.collaboration_progress(action_item_id)
        return result

    def _invalidate_downstream_inputs(
        self,
        cursor: Any,
        *,
        upstream_action_item_id: str,
        accepted_version_id: str,
        sim_time: str,
        correlation_id: str,
    ) -> None:
        """Reopen only downstream tasks that were bound to an older input."""

        dependencies = cursor.execute(
            "SELECT * FROM action_item_dependencies WHERE episode_id = ? "
            "AND upstream_action_item_id = ? "
            "AND bound_upstream_version_id IS NOT NULL "
            "AND bound_upstream_version_id <> ?",
            (self.episode_id, upstream_action_item_id, accepted_version_id),
        ).fetchall()
        for dependency in dependencies:
            downstream_id = dependency["downstream_action_item_id"]
            downstream = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (downstream_id,),
            ).fetchone()
            if not downstream:
                continue
            previous_bound = dependency["bound_upstream_version_id"]
            cursor.execute(
                "UPDATE action_item_dependencies SET bound_upstream_version_id = ? "
                "WHERE dependency_id = ?",
                (accepted_version_id, dependency["dependency_id"]),
            )
            if downstream["owner_actor_id"]:
                cursor.execute(
                    "UPDATE artifact_versions SET review_status = 'REJECTED', "
                    "review_comment = '上游已验收版本发生变化，需要基于新输入重做', "
                    "reviewed_sim_time = ? WHERE action_item_id = ? "
                    "AND review_status = 'PENDING'",
                    (sim_time, downstream_id),
                )
                cursor.execute(
                    "UPDATE action_items SET current_valid_version_id = NULL, "
                    "status = ?, version = version + 1 WHERE action_item_id = ?",
                    (ActionItemStatus.TRACKING, downstream_id),
                )
            cursor.execute(
                "UPDATE action_item_participation_inputs SET status = 'PENDING', "
                "payload = NULL, payload_ref = NULL, source_message_id = NULL, "
                "submitted_sim_time = NULL, version = version + 1 "
                "WHERE action_item_id = ?",
                (downstream_id,),
            )
            self._supersede_pending_final(
                cursor, downstream_id, sim_time, correlation_id
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=downstream_id,
                event_type="CollaborationInputVersionChanged",
                sim_time=sim_time,
                payload={
                    "upstream_action_item_id": upstream_action_item_id,
                    "from_version_id": previous_bound,
                    "to_version_id": accepted_version_id,
                    "downstream_reopened": bool(downstream["owner_actor_id"]),
                },
                correlation_id=correlation_id,
            )

    def _system_invocation_manifest(
        self,
        *,
        capability_type: str,
        purpose: str,
        field_allowlist: list[str],
        entity_versions: dict[str, Any],
        input_hash: str,
        output_status: str,
        prompt_version: str | None = None,
        skill_version: str | None = None,
        context_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the auditable access context for one capability invocation.

        The manifest is evidence attached to existing AuditEvents.  It is not a
        second workflow entity and it grants no permissions by itself.
        """
        manifest = {
            "capability_type": capability_type,
            "principal": {
                "actor_id": "SYSTEM",
                "episode_id": self.episode_id,
                "roles": ["SYSTEM"],
                "auth_source": "INTERNAL_SERVICE",
            },
            "purpose": purpose,
            "field_allowlist": list(field_allowlist),
            "entity_versions": dict(entity_versions),
            "prompt_version": prompt_version,
            "skill_version": skill_version,
            "input_hash": input_hash,
            "output_status": output_status,
        }
        if context_manifest:
            manifest["context"] = dict(context_manifest)
        return manifest

    def _require_aggregator(self, actor_id: str) -> None:
        row = self.db.one(
            "SELECT 1 FROM episode_participants WHERE episode_id = ? "
            "AND actor_id = ? AND role IN ('COORDINATOR', 'AGGREGATOR')",
            (self.episode_id, actor_id),
        )
        if not row:
            raise PermissionError("only the meeting coordinator may manage action items")

    def _require_participant(self, actor_id: str) -> None:
        row = self.db.one(
            "SELECT 1 FROM episode_participants WHERE episode_id = ? "
            "AND actor_id = ? AND role IN ('PARTICIPANT', 'ACTION_OWNER')",
            (self.episode_id, actor_id),
        )
        if not row:
            raise PermissionError("only a meeting participant may perform this action")

    def record_security_rejection(
        self,
        *,
        event_type: str,
        actor_id: str | None,
        operation: str,
        reason: str,
        resource_id: str | None = None,
    ) -> None:
        """Append a bounded security audit without creating workflow state.

        Authentication/authorization failures are observable attempts, not
        ActionItem signals.  The HTTP boundary calls this after denying the
        request; direct deterministic evaluations call it at the same boundary.
        """
        if event_type not in {"AuthenticationRejected", "AuthorizationRejected"}:
            raise ValueError("unsupported security rejection event")
        episode = self.db.one(
            "SELECT episode_id FROM episodes WHERE episode_id = ?",
            (self.episode_id,),
        )
        if not episode:
            return
        sim_time = self.now()
        aggregate_id = resource_id or self.episode_id
        with self.db.transaction() as cursor:
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type=("Episode" if not resource_id else "ProtectedResource"),
                aggregate_id=aggregate_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "actor_id": actor_id,
                    "operation": str(operation)[:200],
                    "reason": str(reason)[:300],
                },
                correlation_id=(
                    f"corr_security_{stable_hash([event_type, actor_id, operation, reason, sim_time])[:20]}"
                ),
            )

    def record_restricted_field_projection(
        self,
        *,
        actor_id: str,
        session_id: str,
        surface: str,
        hidden_fields: list[str],
    ) -> None:
        """Audit a participant projection once per signed session and surface."""
        normalized_fields = sorted(
            {str(field).strip() for field in hidden_fields if str(field).strip()}
        )
        if not normalized_fields:
            return
        aggregate_id = (
            "projection_"
            + stable_hash([self.episode_id, actor_id, session_id, surface])[:24]
        )
        if self.db.one(
            "SELECT 1 FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type = 'RestrictedFieldProjectionApplied'",
            (self.run_id, aggregate_id),
        ):
            return
        sim_time = self.now()
        with self.db.transaction() as cursor:
            if cursor.execute(
                "SELECT 1 FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
                "AND event_type = 'RestrictedFieldProjectionApplied'",
                (self.run_id, aggregate_id),
            ).fetchone():
                return
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="AccessProjection",
                aggregate_id=aggregate_id,
                event_type="RestrictedFieldProjectionApplied",
                sim_time=sim_time,
                payload={
                    "actor_id": actor_id,
                    "surface": surface,
                    "hidden_fields": normalized_fields,
                },
                correlation_id=f"corr_{aggregate_id}",
            )

    def _record_owner_signal(
        self,
        cursor: Any,
        *,
        action_item_id: str,
        actor_id: str,
        signal_type: str,
        sim_time: str,
        correlation_id: str,
        valid_until: str | None = None,
        note: str = "",
    ) -> str:
        """Persist only an explicit business action as an owner signal."""
        signal_type = signal_type.strip().upper()
        if signal_type not in self.OWNER_SIGNAL_TYPES:
            raise ValueError("unsupported owner signal type")
        action = cursor.execute(
            "SELECT * FROM action_items WHERE action_item_id = ? "
            "AND episode_id = ?",
            (action_item_id, self.episode_id),
        ).fetchone()
        if not action:
            raise KeyError(action_item_id)
        contributor_role = self.task_contributor_role(
            action, actor_id, cursor=cursor
        )
        if not contributor_role:
            raise PermissionError(
                "only the assigned owner or an active collaborator may create a task signal"
            )
        if valid_until:
            signal_valid_until = iso_time(valid_until)
            if parse_time(signal_valid_until) <= parse_time(sim_time):
                raise ValueError("signal valid_until must be later than the current time")
        else:
            ttl_hours = self.fixture.get("policy", {}).get(
                "signal_default_ttl_hours",
                self.fixture.get("policy", {}).get("progress_window_hours", 24),
            )
            signal_valid_until = iso_time(
                parse_time(sim_time) + timedelta(hours=ttl_hours)
            )
        cursor.execute(
            "UPDATE action_items SET last_owner_signal_at = ?, "
            "last_owner_signal_type = ?, last_owner_signal_valid_until = ?, "
            "last_owner_signal_actor_id = ?, version = version + 1 "
            "WHERE action_item_id = ?",
            (
                sim_time,
                signal_type,
                signal_valid_until,
                actor_id,
                action_item_id,
            ),
        )
        self.db.append_audit(
            cursor,
            run_id=self.run_id,
            aggregate_type="ActionItem",
            aggregate_id=action_item_id,
            event_type="ProgressSignalRecorded",
            sim_time=sim_time,
            payload={
                "signal_type": signal_type,
                "valid_until": signal_valid_until,
                "note": note.strip(),
                "actor_id": actor_id,
                "contributor_role": contributor_role,
            },
            correlation_id=correlation_id,
        )
        return signal_valid_until

    def bootstrap(self) -> dict[str, Any]:
        existing = self.db.one(
            "SELECT * FROM episodes WHERE episode_id = ?", (self.episode_id,)
        )
        if existing:
            return dict(existing)

        start = iso_time(self.fixture["start_sim_time"])
        organization = self.fixture["organization"]
        correlation_id = "corr_bootstrap"
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO organizations VALUES (?, ?, 'ACTIVE', ?)",
                (organization["organization_id"], organization["name"], start),
            )
            for actor in self.fixture["actors"]:
                cursor.execute(
                    "INSERT INTO actors VALUES (?, ?, ?, 'SIMULATED_USER', 'ACTIVE')",
                    (
                        actor["actor_id"],
                        organization["organization_id"],
                        actor["display_name"],
                    ),
                )

            transcript_text = "\n".join(
                f'{line["speaker"]}：{line["text"]}'
                for line in self.fixture["transcript"]
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
                    self.episode_id,
                    organization["organization_id"],
                    self.run_id,
                    self.fixture["pack_id"],
                    "owner_lead",
                    EpisodeStatus.ACTIVE,
                    transcript_text,
                    start,
                    start,
                    iso_time(self.fixture["evaluation_cutoff_sim_time"]),
                ),
            )
            for actor in self.fixture["actors"]:
                for role in actor["roles"]:
                    cursor.execute(
                        "INSERT INTO episode_participants VALUES (?, ?, ?)",
                        (self.episode_id, actor["actor_id"], role),
                    )

            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Episode",
                aggregate_id=self.episode_id,
                event_type="EpisodeActivated",
                sim_time=start,
                payload={"content_pack_id": self.fixture["pack_id"]},
                correlation_id=correlation_id,
            )

            for index, item in enumerate(self.fixture["action_items"]):
                initial_owner = item.get("owner_actor_id")
                team_required_by = iso_time(item["team_required_by_sim_time"])
                identity_key = stable_hash(
                    [self.episode_id, initial_owner, item["deliverable_key"]]
                )
                cursor.execute(
                    """
                    INSERT INTO action_items(
                        action_item_id, episode_id, identity_key, title,
                        deliverable_key, owner_actor_id, required, status,
                        deadline_sim_time, team_required_by_sim_time, sla_id,
                        source_message_id, source_span, proposal_metadata,
                        created_sim_time, version
                    ) VALUES (?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, 'sla_default',
                              ?, ?, ?, ?, 1)
                    """,
                    (
                        item["action_item_id"],
                        self.episode_id,
                        identity_key,
                        item["title"],
                        item["deliverable_key"],
                        initial_owner,
                        ActionItemStatus.PENDING_CONFIRMATION,
                        (
                            team_required_by
                            if initial_owner
                            else None
                        ),
                        team_required_by,
                        f"transcript_{index}",
                        item["source_span"],
                        canonical_json(
                            {
                                "deliverable": item["title"],
                                "work_requirements": item["work_requirements"],
                                "management_review_policy": item[
                                    "management_review_policy"
                                ],
                                "acceptance_criteria": item[
                                    "management_review_policy"
                                ],
                                "required_fields": item["required_fields"],
                                "requires_human_acceptance": True,
                            }
                        ),
                        start,
                    ),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=item["action_item_id"],
                    event_type="ActionItemProposed",
                    sim_time=start,
                    payload={
                        "owner_actor_id": initial_owner,
                        "deliverable_key": item["deliverable_key"],
                        "source_span": item["source_span"],
                    },
                    correlation_id=correlation_id,
                )
                if initial_owner:
                    confirmation_effect = effect_id(
                        episode_id=self.episode_id,
                        subject_id=item["action_item_id"],
                        effect_type="CONFIRMATION_REQUEST",
                        trigger_key="initial",
                    )
                    self._create_outbox(
                        cursor,
                        action_item_id=item["action_item_id"],
                        effect_type="CONFIRMATION_REQUEST",
                        effect=confirmation_effect,
                        recipient_actor_ids=[initial_owner],
                        content=(
                            f'请确认任务“{item["title"]}”，承诺时间为 '
                            f'{team_required_by}；如需修改请直接回复。'
                        ),
                        correlation_id=correlation_id,
                        sim_time=start,
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ActionItem",
                        aggregate_id=item["action_item_id"],
                        event_type="ActionItemConfirmationRequested",
                        sim_time=start,
                        payload={"effect_id": confirmation_effect},
                        correlation_id=correlation_id,
                    )

        return dict(
            self.db.one(
                "SELECT * FROM episodes WHERE episode_id = ?", (self.episode_id,)
            )
        )

    def revise_action_proposal(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        title: str,
        deliverable: str,
        acceptance_criteria: str,
        priority: str,
        message_id: str,
        team_required_by_sim_time: str | None = None,
        work_requirements: str | None = None,
        management_review_policy: str | None = None,
    ) -> dict[str, Any]:
        self._require_aggregator(actor_id)
        title = title.strip()
        deliverable = deliverable.strip()
        acceptance_criteria = acceptance_criteria.strip()
        work_requirements = (
            work_requirements.strip()
            if work_requirements is not None
            else deliverable
        )
        management_review_policy = (
            management_review_policy.strip()
            if management_review_policy is not None
            else acceptance_criteria
        )
        priority = priority.strip().upper() or "P1"
        if not title or not deliverable or not work_requirements:
            raise ValueError("title, deliverable, and work requirements are required")
        team_required_by = (
            iso_time(team_required_by_sim_time)
            if team_required_by_sim_time
            else None
        )
        if team_required_by and parse_time(team_required_by) <= parse_time(self.now()):
            raise ValueError("team required time must be later than the current time")
        if priority not in {"P0", "P1", "P2"}:
            raise ValueError("priority must be P0, P1, or P2")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] or action["status"] not in {
                ActionItemStatus.PENDING_CONFIRMATION,
                ActionItemStatus.NEEDS_REVISION,
            }:
                raise ValueError("only a draft or returned task definition may be edited")
            metadata = self.proposal_metadata(action)
            # Record what the coordinator actually changed. The audit previously
            # stored only the new values, so "how much human correction did the
            # extraction need" -- the cheapest and most direct product metric --
            # was not derivable from the trail.
            before = {
                "title": action["title"],
                "deliverable": metadata.get("deliverable", ""),
                "acceptance_criteria": metadata.get("acceptance_criteria", ""),
                "work_requirements": metadata.get("work_requirements", ""),
                "management_review_policy": metadata.get(
                    "management_review_policy", ""
                ),
                "priority": metadata.get("priority", ""),
                "team_required_by_sim_time": action["team_required_by_sim_time"],
            }
            after = {
                "title": title,
                "deliverable": deliverable,
                "acceptance_criteria": acceptance_criteria,
                "work_requirements": work_requirements,
                "management_review_policy": management_review_policy,
                "priority": priority,
                "team_required_by_sim_time": (
                    team_required_by or action["team_required_by_sim_time"]
                ),
            }
            changed_fields = {
                field: {"before": before[field], "after": after[field]}
                for field in after
                if str(before[field] or "") != str(after[field] or "")
            }
            metadata.update(
                {
                    "deliverable": deliverable,
                    "acceptance_criteria": acceptance_criteria,
                    "work_requirements": work_requirements,
                    "management_review_policy": management_review_policy,
                    "priority": priority,
                }
            )
            cursor.execute(
                "UPDATE action_items SET title = ?, proposal_metadata = ?, "
                "team_required_by_sim_time = COALESCE(?, team_required_by_sim_time), "
                "version = version + 1 WHERE action_item_id = ?",
                (
                    title,
                    canonical_json(metadata),
                    team_required_by,
                    action_item_id,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemProposalRevised",
                sim_time=sim_time,
                payload={
                    "title": title,
                    "deliverable": deliverable,
                    "acceptance_criteria": acceptance_criteria,
                    "work_requirements": work_requirements,
                    "management_review_policy": management_review_policy,
                    "team_required_by_sim_time": team_required_by
                    or action["team_required_by_sim_time"],
                    "priority": priority,
                    "revised_by": actor_id,
                    "changed_fields": changed_fields,
                    "changed_field_count": len(changed_fields),
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "action_item_id": action_item_id,
                "title": title,
                "team_required_by_sim_time": team_required_by
                or action["team_required_by_sim_time"],
                "proposal_metadata": metadata,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    AMENDABLE_STATUSES = frozenset(
        {
            ActionItemStatus.TRACKING,
            ActionItemStatus.BLOCKED,
            ActionItemStatus.PENDING_ACCEPTANCE,
        }
    )

    def amend_task_description(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        title: str,
        deliverable: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Let the person doing the work correct what their task says.

        Deliberately narrower than `revise_action_proposal`, which refuses a
        task that already has an owner. That refusal is right: what was
        dispatched and accepted is a commitment, and quietly changing who owes
        what by when would rewrite an agreement behind the people who made it.
        Wording is not that -- a title that mis-describes the work is a defect
        the owner is best placed to fix.

        So the seam is drawn around what may move. The owner, the
        collaborators, the team's required date, the personal promise and the
        definition version are all untouched here; only the title and the
        description are. Whoever else is working on the task is told, because
        a description that changes silently is worse than one left wrong.
        """

        title = title.strip()
        deliverable = deliverable.strip()
        if not title or not deliverable:
            raise ValueError("title and description are required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] != actor_id:
                raise PermissionError(
                    "only the task owner may amend its description"
                )
            if action["status"] not in self.AMENDABLE_STATUSES:
                raise ValueError(
                    "only a task being worked on may have its description amended"
                )

            metadata = self.proposal_metadata(action)
            before = {
                "title": action["title"],
                "deliverable": metadata.get("deliverable", ""),
            }
            after = {"title": title, "deliverable": deliverable}
            changed_fields = {
                field: {"before": before[field], "after": after[field]}
                for field in after
                if str(before[field] or "") != str(after[field] or "")
            }
            if not changed_fields:
                result = {
                    "action_item_id": action_item_id,
                    "title": title,
                    "changed_fields": {},
                    "notified_actor_ids": [],
                }
                self._record_inbound(
                    cursor,
                    message_id=message_id,
                    result=result,
                    sim_time=sim_time,
                )
                return result

            metadata["deliverable"] = deliverable
            # `work_requirements` is what the dispatch actually put in front of
            # the assignee. Leaving it on the old wording would make the task
            # read one way here and another way on the card someone accepted.
            metadata["work_requirements"] = deliverable
            cursor.execute(
                "UPDATE action_items SET title = ?, proposal_metadata = ?, "
                "version = version + 1 WHERE action_item_id = ?",
                (title, canonical_json(metadata), action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemDescriptionAmended",
                sim_time=sim_time,
                payload={
                    "amended_by": actor_id,
                    "changed_fields": changed_fields,
                    "changed_field_count": len(changed_fields),
                },
                correlation_id=correlation_id,
            )

            # Everyone else attached to the task, coordinator included: they
            # are working against a description that just changed under them.
            audience = [
                *metadata.get("collaborator_actor_ids", []),
                *(
                    row["actor_id"]
                    for row in cursor.execute(
                        "SELECT actor_id FROM episode_participants "
                        "WHERE episode_id = ? AND role IN "
                        "('COORDINATOR', 'AGGREGATOR')",
                        (self.episode_id,),
                    ).fetchall()
                ),
            ]
            recipients = [actor for actor in dict.fromkeys(audience) if actor != actor_id]
            author = cursor.execute(
                "SELECT display_name FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            self._notify(
                cursor,
                effect_type=NOTIFY_TASK_AMENDED,
                recipient_actor_ids=recipients,
                action_item_id=action_item_id,
                title=f'{author["display_name"]} 修改了任务说明',
                summary=title,
                # What it now says, and what it used to. Naming the columns
                # ("title", "deliverable") would show a reader the schema
                # instead of the change.
                fields=[
                    {
                        "label": "改动",
                        "value": "、".join(
                            AMENDABLE_FIELD_NAMES[field]
                            for field in sorted(changed_fields)
                        ),
                    },
                    {"label": "现在的说明", "value": deliverable},
                ]
                + (
                    [
                        {
                            "label": "原标题",
                            "value": changed_fields["title"]["before"],
                        }
                    ]
                    if "title" in changed_fields
                    else []
                ),
                # No decision: nothing is being asked of the reader, and a
                # button that only dismisses trains people to dismiss.
                decisions=[],
                correlation_id=correlation_id,
                sim_time=sim_time,
                # Keyed on the content, so re-saving the same wording collapses
                # onto one message instead of pinging everyone twice.
                trigger_key=f"{action_item_id}:{stable_hash(after)}",
                subject_id=action_item_id,
            )

            result = {
                "action_item_id": action_item_id,
                "title": title,
                "changed_fields": changed_fields,
                "notified_actor_ids": recipients,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def dispatch_action(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        owner_actor_id: str,
        collaborator_actor_ids: list[str] | None,
        assignment_message: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Dispatch one task definition version to one owner and optional collaborators."""

        self._require_aggregator(actor_id)
        owner_actor_id = owner_actor_id.strip()
        collaborator_ids = sorted(
            {str(item).strip() for item in (collaborator_actor_ids or []) if str(item).strip()}
        )
        assignment_message = assignment_message.strip()
        if not owner_actor_id:
            raise ValueError("one primary owner is required")
        if owner_actor_id in collaborator_ids:
            raise ValueError("primary owner cannot also be a collaborator")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] or action["status"] not in {
                ActionItemStatus.PENDING_CONFIRMATION,
                ActionItemStatus.NEEDS_REVISION,
            }:
                raise ValueError("only a draft or returned task may be dispatched")
            if not action["team_required_by_sim_time"]:
                raise ValueError("team required time must be set before dispatch")
            metadata = self.proposal_metadata(action)
            if not str(metadata.get("deliverable") or action["deliverable_key"]).strip():
                raise ValueError("deliverable is required before dispatch")
            if not str(metadata.get("work_requirements") or "").strip():
                raise ValueError("work requirements are required before dispatch")
            structure = metadata.get("collaboration_structure") or {}
            required_owner_actor_id = str(
                structure.get("required_owner_actor_id") or ""
            )
            if (
                structure.get("structure_type") == "QUESTION_COLLECTION_VOTE"
                and required_owner_actor_id
                and owner_actor_id != required_owner_actor_id
            ):
                raise PermissionError(
                    "the decision task must be dispatched to its confirmed final owner"
                )

            participant_ids = [owner_actor_id, *collaborator_ids]
            placeholders = ", ".join("?" for _ in participant_ids)
            participants = cursor.execute(
                "SELECT DISTINCT actor_id FROM episode_participants WHERE episode_id = ? "
                f"AND actor_id IN ({placeholders}) "
                "AND role IN ('PARTICIPANT','ACTION_OWNER')",
                (self.episode_id, *participant_ids),
            ).fetchall()
            valid_ids = {row["actor_id"] for row in participants}
            missing = sorted(set(participant_ids) - valid_ids)
            if missing:
                raise PermissionError(
                    "all assignment recipients must be explicit meeting participants: "
                    + ", ".join(missing)
                )

            definition_version = int(action["definition_version"] or 1)
            redispatched = action["status"] == ActionItemStatus.NEEDS_REVISION
            if redispatched:
                definition_version += 1
            metadata["collaborator_actor_ids"] = collaborator_ids
            cursor.execute(
                "UPDATE action_items SET status = ?, definition_version = ?, "
                "proposal_metadata = ?, published_sim_time = COALESCE(published_sim_time, ?), "
                "confirmed_sim_time = NULL, version = version + 1 WHERE action_item_id = ?",
                (
                    ActionItemStatus.PENDING_ASSIGNMENT,
                    definition_version,
                    canonical_json(metadata),
                    sim_time,
                    action_item_id,
                ),
            )
            recipients = [(owner_actor_id, AssignmentRole.OWNER), *[
                (collaborator_id, AssignmentRole.COLLABORATOR)
                for collaborator_id in collaborator_ids
            ]]
            assignments: list[dict[str, Any]] = []
            for target_actor_id, role in recipients:
                assignment_id = f"asg_{uuid4().hex}"
                cursor.execute(
                    """
                    INSERT INTO action_item_assignments(
                        assignment_id, action_item_id, definition_version, actor_id,
                        assignment_role, response_status, assignment_message,
                        assigned_sim_time, source_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id,
                        action_item_id,
                        definition_version,
                        target_actor_id,
                        role,
                        AssignmentResponse.PENDING,
                        assignment_message,
                        sim_time,
                        message_id,
                    ),
                )
                assignments.append(
                    {
                        "assignment_id": assignment_id,
                        "actor_id": target_actor_id,
                        "assignment_role": role,
                        "response_status": AssignmentResponse.PENDING,
                    }
                )
            event_type = "ActionItemDispatchedAgain" if redispatched else "ActionItemDispatchCreated"
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "definition_version": definition_version,
                    "owner_actor_id": owner_actor_id,
                    "collaborator_actor_ids": collaborator_ids,
                    "assignment_message": assignment_message,
                    "dispatched_by": actor_id,
                },
                correlation_id=correlation_id,
            )
            # No assignment notification is enqueued here. Feishu already
            # pushes assignment cards through AssignmentNotifier, which
            # projects pending assignments directly; adding an Outbox effect
            # too would send the same person two cards for one dispatch,
            # because the two paths derive different EffectIds for the same
            # business event. Consolidating onto one of them is a decision
            # for whoever owns the Feishu surface.
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.PENDING_ASSIGNMENT,
                "definition_version": definition_version,
                "assignments": assignments,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def respond_to_assignment(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        decision: str,
        response_message: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Accept the current task definition or return the whole dispatch for revision."""

        self._require_participant(actor_id)
        decision = decision.strip().upper()
        response_message = response_message.strip()
        if decision not in {"ACCEPT", "RETURN_FOR_REVISION"}:
            raise ValueError("decision must be ACCEPT or RETURN_FOR_REVISION")
        if decision == "RETURN_FOR_REVISION" and not response_message:
            raise ValueError("a revision reason is required")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["status"] != ActionItemStatus.PENDING_ASSIGNMENT:
                raise ValueError("task is not waiting for assignment responses")
            definition_version = int(action["definition_version"] or 1)
            assignment = cursor.execute(
                "SELECT * FROM action_item_assignments WHERE action_item_id = ? "
                "AND definition_version = ? AND actor_id = ?",
                (action_item_id, definition_version, actor_id),
            ).fetchone()
            if not assignment:
                raise PermissionError("actor is not assigned to this task version")
            if assignment["response_status"] != AssignmentResponse.PENDING:
                raise ValueError("assignment response is already final")

            if decision == "RETURN_FOR_REVISION":
                cursor.execute(
                    "UPDATE action_item_assignments SET response_status = ?, "
                    "response_message = ?, responded_sim_time = ? WHERE assignment_id = ?",
                    (
                        AssignmentResponse.RETURNED,
                        response_message,
                        sim_time,
                        assignment["assignment_id"],
                    ),
                )
                cursor.execute(
                    "UPDATE action_item_assignments SET response_status = ? "
                    "WHERE action_item_id = ? AND definition_version = ? "
                    "AND assignment_id <> ? AND response_status IN ('PENDING','ACCEPTED')",
                    (
                        AssignmentResponse.SUPERSEDED,
                        action_item_id,
                        definition_version,
                        assignment["assignment_id"],
                    ),
                )
                cursor.execute(
                    "UPDATE action_items SET status = ?, owner_actor_id = NULL, "
                    "active_commitment_revision_id = NULL, confirmed_sim_time = NULL, "
                    "version = version + 1 WHERE action_item_id = ?",
                    (ActionItemStatus.NEEDS_REVISION, action_item_id),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItemAssignment",
                    aggregate_id=assignment["assignment_id"],
                    event_type="ActionItemAssignmentReturned",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": action_item_id,
                        "definition_version": definition_version,
                        "actor_id": actor_id,
                        "assignment_role": assignment["assignment_role"],
                        "reason": response_message,
                    },
                    correlation_id=correlation_id,
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="ActionItemDispatchSuperseded",
                    sim_time=sim_time,
                    payload={
                        "definition_version": definition_version,
                        "returned_by": actor_id,
                        "reason": response_message,
                    },
                    correlation_id=correlation_id,
                )
                result = {
                    "action_item_id": action_item_id,
                    "status": ActionItemStatus.NEEDS_REVISION,
                    "definition_version": definition_version,
                    "returned_by": actor_id,
                    "reason": response_message,
                }
                self._record_inbound(
                    cursor, message_id=message_id, result=result, sim_time=sim_time
                )
                return result

            cursor.execute(
                "UPDATE action_item_assignments SET response_status = ?, "
                "response_message = ?, responded_sim_time = ? WHERE assignment_id = ?",
                (
                    AssignmentResponse.ACCEPTED,
                    response_message or None,
                    sim_time,
                    assignment["assignment_id"],
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItemAssignment",
                aggregate_id=assignment["assignment_id"],
                event_type="ActionItemAssignmentAccepted",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "definition_version": definition_version,
                    "actor_id": actor_id,
                    "assignment_role": assignment["assignment_role"],
                    "message": response_message,
                },
                correlation_id=correlation_id,
            )
            current_assignments = cursor.execute(
                "SELECT * FROM action_item_assignments WHERE action_item_id = ? "
                "AND definition_version = ? ORDER BY assignment_role DESC, actor_id",
                (action_item_id, definition_version),
            ).fetchall()
            pending = [
                row for row in current_assignments
                if row["response_status"] == AssignmentResponse.PENDING
            ]
            if pending:
                result = {
                    "action_item_id": action_item_id,
                    "status": ActionItemStatus.PENDING_ASSIGNMENT,
                    "definition_version": definition_version,
                    "accepted_by": actor_id,
                    "pending_count": len(pending),
                }
                self._record_inbound(
                    cursor, message_id=message_id, result=result, sim_time=sim_time
                )
                return result

            owners = [
                row for row in current_assignments
                if row["assignment_role"] == AssignmentRole.OWNER
                and row["response_status"] == AssignmentResponse.ACCEPTED
            ]
            if len(owners) != 1 or any(
                row["response_status"] != AssignmentResponse.ACCEPTED
                for row in current_assignments
            ):
                raise ValueError("current dispatch cannot be activated")
            owner_id = owners[0]["actor_id"]
            team_required_by = action["team_required_by_sim_time"]
            if not team_required_by:
                raise ValueError("task has no team required time")
            revision_row = cursor.execute(
                "SELECT COALESCE(MAX(revision_no), 0) + 1 AS revision_no "
                "FROM commitment_revisions WHERE action_item_id = ?",
                (action_item_id,),
            ).fetchone()
            revision_no = int(revision_row["revision_no"])
            revision_id = f"com_{uuid4().hex}"
            cursor.execute(
                """
                INSERT INTO commitment_revisions(
                    commitment_revision_id, action_item_id, revision_no,
                    owner_actor_id, deliverable_key, promised_deadline_sim_time,
                    source_message_id, source_span, status, created_sim_time,
                    supersedes_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'assignment.accept', 'ACTIVE', ?, NULL)
                """,
                (
                    revision_id,
                    action_item_id,
                    revision_no,
                    owner_id,
                    action["deliverable_key"],
                    team_required_by,
                    message_id,
                    sim_time,
                ),
            )
            cursor.execute(
                "UPDATE action_items SET owner_actor_id = ?, deadline_sim_time = ?, "
                "active_commitment_revision_id = ?, status = ?, confirmed_sim_time = ?, "
                "version = version + 1 WHERE action_item_id = ? AND status = ?",
                (
                    owner_id,
                    team_required_by,
                    revision_id,
                    ActionItemStatus.TRACKING,
                    sim_time,
                    action_item_id,
                    ActionItemStatus.PENDING_ASSIGNMENT,
                ),
            )
            progress = self.collaboration_progress(action_item_id, cursor=cursor)
            if progress and progress["dependencies"]:
                for dependency in progress["dependencies"]:
                    if dependency["accepted_version_id"]:
                        cursor.execute(
                            "UPDATE action_item_dependencies SET bound_upstream_version_id = ? "
                            "WHERE dependency_id = ?",
                            (
                                dependency["accepted_version_id"],
                                dependency["dependency_id"],
                            ),
                        )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemTrackingActivated",
                sim_time=sim_time,
                payload={
                    "definition_version": definition_version,
                    "owner_actor_id": owner_id,
                    "collaborator_actor_ids": [
                        row["actor_id"]
                        for row in current_assignments
                        if row["assignment_role"] == AssignmentRole.COLLABORATOR
                    ],
                    "commitment_revision_id": revision_id,
                    "promised_deadline_sim_time": team_required_by,
                },
                correlation_id=correlation_id,
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CommitmentRevision",
                aggregate_id=revision_id,
                event_type="CommitmentRevisionActivated",
                sim_time=sim_time,
                payload={"action_item_id": action_item_id},
                correlation_id=correlation_id,
            )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=owner_id,
                signal_type="ASSIGNMENT_ACCEPTED",
                sim_time=sim_time,
                correlation_id=correlation_id,
            )
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.TRACKING,
                "definition_version": definition_version,
                "owner_actor_id": owner_id,
                "commitment_revision_id": revision_id,
                "promised_deadline_sim_time": team_required_by,
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def publish_action(
        self, action_item_id: str, *, actor_id: str, message_id: str
    ) -> dict[str, Any]:
        self._require_aggregator(actor_id)
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] or action["status"] != ActionItemStatus.PENDING_CONFIRMATION:
                raise ValueError("only an unclaimed proposal may be published")
            if not action["team_required_by_sim_time"]:
                raise ValueError("team required time must be set before publishing")
            cursor.execute(
                "UPDATE action_items SET published_sim_time = COALESCE(published_sim_time, ?), "
                "version = version + 1 WHERE action_item_id = ?",
                (sim_time, action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemPublishedForClaim",
                sim_time=sim_time,
                payload={"published_by": actor_id},
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "action_item_id": action_item_id,
                "published_sim_time": sim_time,
                "status": ActionItemStatus.PENDING_CONFIRMATION,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def ignore_action(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        reason: str,
        message_id: str,
    ) -> dict[str, Any]:
        self._require_aggregator(actor_id)
        reason = reason.strip()
        if not reason:
            raise ValueError("ignore reason is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] or action["status"] != ActionItemStatus.PENDING_CONFIRMATION:
                raise ValueError("only an unclaimed proposal may be ignored")
            cursor.execute(
                "UPDATE action_items SET status = ?, required = FALSE, "
                "version = version + 1 "
                "WHERE action_item_id = ?",
                (ActionItemStatus.REJECTED, action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemIgnored",
                sim_time=sim_time,
                payload={"ignored_by": actor_id, "reason": reason},
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.REJECTED,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def merge_action(
        self,
        source_action_item_id: str,
        *,
        target_action_item_id: str,
        actor_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        self._require_aggregator(actor_id)
        if source_action_item_id == target_action_item_id:
            raise ValueError("source and target action items must differ")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            source = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (source_action_item_id, self.episode_id),
            ).fetchone()
            target = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (target_action_item_id, self.episode_id),
            ).fetchone()
            if not source or not target:
                raise KeyError(source_action_item_id if not source else target_action_item_id)
            for action in (source, target):
                if (
                    action["owner_actor_id"]
                    or action["published_sim_time"]
                    or action["status"] != ActionItemStatus.PENDING_CONFIRMATION
                ):
                    raise ValueError("only unpublished, unclaimed proposals may be merged")
            target_metadata = self.proposal_metadata(target)
            source_metadata = self.proposal_metadata(source)
            merged_sources = list(target_metadata.get("merged_sources", []))
            merged_sources.append(
                {
                    "action_item_id": source_action_item_id,
                    "title": source["title"],
                    "source_timestamp": source_metadata.get("source_timestamp"),
                    "source_quote": source_metadata.get("source_quote"),
                }
            )
            target_metadata["merged_sources"] = merged_sources
            target_metadata["uncertainties"] = list(
                dict.fromkeys(
                    list(target_metadata.get("uncertainties", []))
                    + list(source_metadata.get("uncertainties", []))
                )
            )
            cursor.execute(
                "UPDATE action_items SET proposal_metadata = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (canonical_json(target_metadata), target_action_item_id),
            )
            cursor.execute(
                "UPDATE action_items SET status = ?, required = FALSE, "
                "version = version + 1 "
                "WHERE action_item_id = ?",
                (ActionItemStatus.REJECTED, source_action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=target_action_item_id,
                event_type="ActionItemsMerged",
                sim_time=sim_time,
                payload={
                    "source_action_item_id": source_action_item_id,
                    "target_action_item_id": target_action_item_id,
                    "merged_by": actor_id,
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "source_action_item_id": source_action_item_id,
                "target_action_item_id": target_action_item_id,
                "source_status": ActionItemStatus.REJECTED,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def _create_outbox(
        self,
        cursor: Any,
        *,
        action_item_id: str | None,
        effect_type: str,
        effect: str,
        recipient_actor_ids: list[str],
        content: str,
        correlation_id: str,
        sim_time: str,
        notification: dict[str, Any] | None = None,
    ) -> str:
        existing = cursor.execute(
            "SELECT outbox_id FROM outbox_entries WHERE effect_id = ?", (effect,)
        ).fetchone()
        if existing:
            return existing["outbox_id"]
        outbox_id = f"out_{uuid4().hex}"
        payload = {
            "conversation_id": "conv_main",
            "sender_actor_id": "agent",
            "recipient_actor_ids": recipient_actor_ids,
            "content": content,
        }
        if notification is not None:
            # Structured description of what the recipient is being asked to do.
            # A text-only adapter keeps using `content`; a card adapter renders
            # this instead, so neither transport has to parse prose to decide
            # which buttons to draw.
            payload["notification"] = notification
        cursor.execute(
            """
            INSERT INTO outbox_entries(
                outbox_id, run_id, episode_id, action_item_id, effect_type,
                effect_id, payload, status, attempt_count,
                available_at_sim_time, correlation_id, created_sim_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                outbox_id,
                self.run_id,
                self.episode_id,
                action_item_id,
                effect_type,
                effect,
                canonical_json(payload),
                OutboxStatus.PENDING,
                sim_time,
                correlation_id,
                sim_time,
            ),
        )
        self.db.append_audit(
            cursor,
            run_id=self.run_id,
            aggregate_type="OutboxEntry",
            aggregate_id=outbox_id,
            event_type="OutboxEntryCreated",
            sim_time=sim_time,
            payload={"effect_id": effect, "effect_type": effect_type},
            correlation_id=correlation_id,
        )
        return outbox_id

    def _notify(
        self,
        cursor: Any,
        *,
        effect_type: str,
        recipient_actor_ids: list[str],
        action_item_id: str,
        title: str,
        summary: str,
        correlation_id: str,
        sim_time: str,
        trigger_key: str,
        fields: list[dict[str, str]] | None = None,
        decisions: list[dict[str, Any]] | None = None,
        deep_link_path: str = "/tasks",
        subject_id: str = "",
    ) -> str | None:
        """Queue one human-facing notification through the existing Outbox.

        Returns None when there is nobody to tell, so callers do not have to
        guard. The EffectId is derived from the business trigger, which makes a
        replayed call collapse onto the same outbox row rather than sending a
        second message.
        """

        recipients = [actor for actor in dict.fromkeys(recipient_actor_ids) if actor]
        if not recipients:
            return None
        effect = effect_id(
            episode_id=self.episode_id,
            subject_id=",".join(sorted(recipients)),
            effect_type=effect_type,
            trigger_key=trigger_key,
        )
        notification = {
            "notification_contract_version": "notification.v1",
            "kind": effect_type,
            "action_item_id": action_item_id,
            # The entity a decision acts on, when that is not the action item
            # itself -- an assistance request, for instance. Kept separate from
            # trigger_key, which is an idempotency key and is composite at
            # several call sites.
            "subject_id": subject_id,
            "title": title,
            "summary": summary,
            "fields": fields or [],
            # Inline decisions are limited to choices that need no reading of
            # the deliverable itself; anything else links back to the workbench.
            "decisions": decisions or [],
            "deep_link_path": deep_link_path,
        }
        return self._create_outbox(
            cursor,
            action_item_id=action_item_id,
            effect_type=effect_type,
            effect=effect,
            recipient_actor_ids=recipients,
            content=f"{title}\n{summary}",
            correlation_id=correlation_id,
            sim_time=sim_time,
            notification=notification,
        )

    def _notify_result_pending_review(
        self,
        cursor: Any,
        *,
        version: Any,
        action: Any,
        processing_label: str,
        sim_time: str,
    ) -> None:
        """Tell the coordinator one final candidate is ready to accept.

        Only the owner's final candidate produces this card.  A collaborator's
        contribution is internal to the team -- the task owner decides what to
        do with it in the workbench -- so broadcasting every contribution would
        both spam the meeting and leak work-in-progress past the owner.

        It fires when processing settles rather than at submission, because
        `review_artifact` refuses to run before then: a card sent earlier would
        be one the recipient cannot act on.
        """

        if version["review_status"] != "PENDING":
            return
        if version["submitted_by_actor_id"] != action["owner_actor_id"]:
            return
        payload = self._decoded_json(version["payload"], {}) or {}
        attachments = self._decoded_json(version["attachment_extractions"], []) or []
        fields = [
            {"label": "完成摘要", "value": str(payload.get("summary") or "未填写")},
            {
                "label": "用时",
                "value": self._execution_elapsed_label(action["action_item_id"]),
            },
            {"label": "AI 处理状态", "value": processing_label},
        ]
        if attachments:
            fields.append(
                {
                    "label": "附件",
                    "value": "、".join(
                        str(item.get("name") or "附件") for item in attachments
                    ),
                }
            )
        # No body text and no inline verdict: accepting requires reading the
        # deliverable and the assist package, which only the workbench shows.
        self._notify(
            cursor,
            effect_type=NOTIFY_RESULT_PENDING_REVIEW,
            recipient_actor_ids=[self.aggregator_actor_id],
            action_item_id=action["action_item_id"],
            title=f'待你验收：{action["title"]}',
            summary=str(payload.get("summary") or "提交人未填写完成摘要"),
            fields=fields,
            decisions=[],
            deep_link_path="/manage",
            correlation_id=f'corr_task_result_{version["version_id"]}',
            sim_time=sim_time,
            trigger_key=f'{version["version_id"]}:pending_review',
        )

    def _execution_elapsed_label(self, action_item_id: str) -> str:
        """Wall-clock time from execution start to now, as a short label.

        Deliberately not derived from sim_time: VirtualClock only advances on
        explicit business events, so in the live workbench a simulated duration
        would read as zero for every task.  `audit_events.recorded_at_real` is
        the only wall clock the domain records.
        """

        started = self.db.one(
            "SELECT recorded_at_real FROM audit_events "
            "WHERE aggregate_id = ? AND event_type = 'ActionItemTrackingActivated' "
            "ORDER BY sequence_no LIMIT 1",
            (action_item_id,),
        )
        if not started or not started["recorded_at_real"]:
            return "未知"
        try:
            begin = parse_time(str(started["recorded_at_real"]).replace(" ", "T"))
        except Exception:
            return "未知"
        delta = datetime.now(UTC) - begin
        total_minutes = int(delta.total_seconds() // 60)
        if total_minutes < 1:
            return "不到 1 分钟"
        days, remainder = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(remainder, 60)
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分钟"
        return f"{minutes} 分钟"

    def _record_inbound(
        self,
        cursor: Any,
        *,
        message_id: str,
        result: dict[str, Any],
        sim_time: str,
    ) -> None:
        row = cursor.execute(
            "SELECT COALESCE(MAX(accepted_sequence), 0) + 1 AS next_sequence "
            "FROM inbound_receipts"
        ).fetchone()
        cursor.execute(
            "INSERT INTO inbound_receipts VALUES (?, ?, ?, ?)",
            (
                message_id,
                int(row["next_sequence"]),
                canonical_json(result),
                sim_time,
            ),
        )

    def _ignore_if_archived(
        self, *, message_id: str, aggregate_id: str
    ) -> dict[str, Any] | None:
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        episode = self.db.one(
            "SELECT status FROM episodes WHERE episode_id = ?", (self.episode_id,)
        )
        if not episode or episode["status"] != EpisodeStatus.ARCHIVED:
            return None
        sim_time = self.now()
        result = {
            "ignored": True,
            "reason": "episode_archived",
            "aggregate_id": aggregate_id,
        }
        with self.db.transaction() as cursor:
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Episode",
                aggregate_id=self.episode_id,
                event_type="LateMessageIgnored",
                sim_time=sim_time,
                payload={"message_id": message_id, "target_id": aggregate_id},
                correlation_id=f"corr_{message_id}",
            )
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def confirm_action(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        message_id: str,
        promised_deadline_sim_time: str | None = None,
        deliverable_key: str | None = None,
    ) -> dict[str, Any]:
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (action_item_id,),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] != actor_id:
                raise PermissionError("only the assigned owner may confirm an action item")

            current = cursor.execute(
                """
                SELECT * FROM commitment_revisions
                WHERE action_item_id = ? AND status = 'ACTIVE'
                """,
                (action_item_id,),
            ).fetchone()
            revision_no = 1 if not current else int(current["revision_no"]) + 1
            if current:
                cursor.execute(
                    "UPDATE commitment_revisions SET status = 'SUPERSEDED' "
                    "WHERE commitment_revision_id = ?",
                    (current["commitment_revision_id"],),
                )
            revision_id = f"com_{uuid4().hex}"
            deadline = iso_time(
                promised_deadline_sim_time or action["deadline_sim_time"]
            )
            deliverable = deliverable_key or action["deliverable_key"]
            cursor.execute(
                """
                INSERT INTO commitment_revisions(
                    commitment_revision_id, action_item_id, revision_no,
                    owner_actor_id, deliverable_key, promised_deadline_sim_time,
                    source_message_id, source_span, status, created_sim_time,
                    supersedes_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'message.content', 'ACTIVE', ?, ?)
                """,
                (
                    revision_id,
                    action_item_id,
                    revision_no,
                    actor_id,
                    deliverable,
                    deadline,
                    message_id,
                    sim_time,
                    current["commitment_revision_id"] if current else None,
                ),
            )
            cursor.execute(
                """
                UPDATE action_items
                SET active_commitment_revision_id = ?, deliverable_key = ?,
                    deadline_sim_time = ?, status = ?, confirmed_sim_time = ?,
                    version = version + 1
                WHERE action_item_id = ?
                """,
                (
                    revision_id,
                    deliverable,
                    deadline,
                    ActionItemStatus.TRACKING,
                    sim_time,
                    action_item_id,
                ),
            )
            event_type = (
                "ActionItemConfirmed" if revision_no == 1 else "ActionItemRevisedByOwner"
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "commitment_revision_id": revision_id,
                    "revision_no": revision_no,
                    "promised_deadline_sim_time": deadline,
                    "deliverable_key": deliverable,
                },
                correlation_id=correlation_id,
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CommitmentRevision",
                aggregate_id=revision_id,
                event_type="CommitmentRevisionActivated",
                sim_time=sim_time,
                payload={"action_item_id": action_item_id},
                correlation_id=correlation_id,
            )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="CLAIMED" if revision_no == 1 else "PROMISE_REVISED",
                sim_time=sim_time,
                correlation_id=correlation_id,
            )
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.TRACKING,
                "commitment_revision_id": revision_id,
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def claim_action(
        self,
        action_item_id: str,
        *,
        claimant_name: str | None = None,
        actor_id: str | None = None,
        promised_deadline_sim_time: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Atomically turn an unassigned proposed action into a tracked commitment."""
        claimant_name = (claimant_name or "").strip()
        if actor_id and claimant_name:
            raise ValueError("claim identity must come from either principal or legacy name")
        if not actor_id and not claimant_name:
            raise ValueError("claimant identity is required")
        if actor_id:
            self._require_participant(actor_id)
        if not message_id.strip():
            raise ValueError("message_id is required")
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        deadline = iso_time(promised_deadline_sim_time)
        if parse_time(deadline) <= parse_time(self.now()):
            raise ValueError("promised deadline must be later than the current time")

        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items "
                "WHERE action_item_id = ? AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            metadata = self.proposal_metadata(action)
            structure = metadata.get("collaboration_structure") or {}
            required_owner = structure.get("required_owner_actor_id")
            if required_owner and actor_id and actor_id != required_owner:
                raise PermissionError(
                    "this structured task must be claimed by its confirmed final owner"
                )
            progress = self.collaboration_progress(action_item_id, cursor=cursor)
            if progress and not progress["dependencies_ready"]:
                raise ValueError(
                    "action item is waiting for accepted upstream task results"
                )
            if action["owner_actor_id"]:
                raise ValueError("action item has already been claimed")
            if not action["published_sim_time"]:
                raise ValueError("action item has not been published for claim")
            team_required_by = action["team_required_by_sim_time"]
            if not team_required_by:
                raise ValueError("action item has no team required time")
            schedule_conflict = parse_time(deadline) > parse_time(team_required_by)

            episode = cursor.execute(
                "SELECT organization_id FROM episodes WHERE episode_id = ?",
                (self.episode_id,),
            ).fetchone()
            organization_id = episode["organization_id"]
            if actor_id:
                actor = cursor.execute(
                    "SELECT actor_id, display_name FROM actors "
                    "WHERE actor_id = ? AND organization_id = ? AND status = 'ACTIVE'",
                    (actor_id, organization_id),
                ).fetchone()
                if not actor:
                    raise PermissionError("principal actor is not active in this organization")
                participant = cursor.execute(
                    "SELECT 1 FROM episode_participants WHERE episode_id = ? "
                    "AND actor_id = ? AND role IN ('PARTICIPANT', 'ACTION_OWNER')",
                    (self.episode_id, actor_id),
                ).fetchone()
                if not participant:
                    raise PermissionError("actor did not attend this meeting")
                claimant_name = actor["display_name"]
            else:
                actor = cursor.execute(
                    "SELECT a.actor_id, a.display_name FROM actors a "
                    "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                    "WHERE a.organization_id = ? AND a.display_name = ? "
                    "AND a.status = 'ACTIVE' AND ep.episode_id = ? "
                    "AND ep.role IN ('PARTICIPANT', 'ACTION_OWNER') "
                    "ORDER BY a.actor_id LIMIT 1",
                    (organization_id, claimant_name, self.episode_id),
                ).fetchone()
                if not actor:
                    raise PermissionError(
                        "claimant must be an explicit meeting participant"
                    )
                actor_id = actor["actor_id"]
                claimant_name = actor["display_name"]

            if required_owner and actor_id != required_owner:
                raise PermissionError(
                    "this structured task must be claimed by its confirmed final owner"
                )

            claimed = cursor.execute(
                "UPDATE action_items SET owner_actor_id = ?, deadline_sim_time = ?, "
                "version = version + 1 WHERE action_item_id = ? "
                "AND episode_id = ? AND owner_actor_id IS NULL "
                "AND published_sim_time IS NOT NULL",
                (actor_id, deadline, action_item_id, self.episode_id),
            )
            if claimed.rowcount != 1:
                raise ValueError("action item was claimed by someone else")
            if progress and progress["dependencies"]:
                for dependency in progress["dependencies"]:
                    cursor.execute(
                        "UPDATE action_item_dependencies SET "
                        "bound_upstream_version_id = ? WHERE dependency_id = ?",
                        (
                            dependency["accepted_version_id"],
                            dependency["dependency_id"],
                        ),
                    )

            revision_id = f"com_{uuid4().hex}"
            cursor.execute(
                """
                INSERT INTO commitment_revisions(
                    commitment_revision_id, action_item_id, revision_no,
                    owner_actor_id, deliverable_key, promised_deadline_sim_time,
                    source_message_id, source_span, status, created_sim_time
                ) VALUES (?, ?, 1, ?, ?, ?, ?, 'claim.request', 'ACTIVE', ?)
                """,
                (
                    revision_id,
                    action_item_id,
                    actor_id,
                    action["deliverable_key"],
                    deadline,
                    message_id,
                    sim_time,
                ),
            )
            cursor.execute(
                "UPDATE action_items SET active_commitment_revision_id = ?, "
                "status = ?, confirmed_sim_time = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (
                    revision_id,
                    ActionItemStatus.TRACKING,
                    sim_time,
                    action_item_id,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemClaimed",
                sim_time=sim_time,
                payload={
                    "owner_actor_id": actor_id,
                    "claimant_name": claimant_name,
                    "promised_deadline_sim_time": deadline,
                    "team_required_by_sim_time": team_required_by,
                    "schedule_conflict": schedule_conflict,
                    "commitment_revision_id": revision_id,
                },
                correlation_id=correlation_id,
            )
            if progress and progress["dependencies"]:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="CollaborationInputsBound",
                    sim_time=sim_time,
                    payload={
                        "upstream_versions": {
                            item["upstream_action_item_id"]: item[
                                "accepted_version_id"
                            ]
                            for item in progress["dependencies"]
                        }
                    },
                    correlation_id=correlation_id,
                )
            if schedule_conflict:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="ScheduleConflictDetected",
                    sim_time=sim_time,
                    payload={
                        "owner_actor_id": actor_id,
                        "promised_deadline_sim_time": deadline,
                        "team_required_by_sim_time": team_required_by,
                    },
                    correlation_id=correlation_id,
                )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="CLAIMED",
                sim_time=sim_time,
                correlation_id=correlation_id,
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CommitmentRevision",
                aggregate_id=revision_id,
                event_type="CommitmentRevisionActivated",
                sim_time=sim_time,
                payload={"action_item_id": action_item_id},
                correlation_id=correlation_id,
            )
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.TRACKING,
                "owner_actor_id": actor_id,
                "owner_display_name": claimant_name,
                "promised_deadline_sim_time": deadline,
                "team_required_by_sim_time": team_required_by,
                "schedule_conflict": schedule_conflict,
                "commitment_revision_id": revision_id,
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def reject_action(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        reason: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Reject an assigned proposal without deleting its audit history."""
        reason = reason.strip()
        if not reason:
            raise ValueError("rejection reason is required")
        if not message_id.strip():
            raise ValueError("message_id is required")
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] != actor_id:
                raise PermissionError("only the assigned owner may reject an action item")
            if action["status"] != ActionItemStatus.PENDING_CONFIRMATION:
                raise ValueError("only a pending confirmation may be rejected")
            cursor.execute(
                "UPDATE action_items SET status = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (ActionItemStatus.REJECTED, action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemRejectedByOwner",
                sim_time=sim_time,
                payload={"rejected_by": actor_id, "reason": reason},
                correlation_id=correlation_id,
            )
            result = {
                "action_item_id": action_item_id,
                "status": ActionItemStatus.REJECTED,
                "reason": reason,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def record_progress_signal(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        signal_type: str,
        message_id: str,
        valid_until: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Record a low-cost structured response; page activity is never a signal."""
        signal_type = signal_type.strip().upper()
        note = note.strip()
        if signal_type not in self.QUICK_SIGNAL_TYPES:
            raise ValueError("signal_type must be a supported quick status")
        if not message_id.strip():
            raise ValueError("message_id is required")
        if len(note) > 1000:
            raise ValueError("signal note is too long")
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            contributor_role = self.task_contributor_role(
                action, actor_id, cursor=cursor
            )
            if not contributor_role:
                raise PermissionError(
                    "only the assigned owner or an active collaborator may update task status"
                )
            if action["status"] != ActionItemStatus.TRACKING:
                raise ValueError("quick status may only be updated while tracking")
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type=signal_type,
                sim_time=sim_time,
                correlation_id=correlation_id,
                valid_until=valid_until,
                note=note,
            )
            result = {
                "action_item_id": action_item_id,
                "status": action["status"],
                "signal_type": signal_type,
                "signal_at": sim_time,
                "valid_until": signal_valid_until,
                "note": note,
                "contributor_role": contributor_role,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def request_assistance(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        target_actor_id: str,
        category: str,
        summary: str,
        message_id: str,
        blocking_action_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask one attendee for help without creating a general dependency graph."""
        category = category.strip().upper()
        summary = summary.strip()
        target_actor_id = target_actor_id.strip()
        blocking_action_item_id = (blocking_action_item_id or "").strip() or None
        if category not in self.ASSISTANCE_CATEGORIES:
            raise ValueError("unsupported assistance category")
        if not summary:
            raise ValueError("assistance summary is required")
        if len(summary) > 2000:
            raise ValueError("assistance summary is too long")
        if not target_actor_id:
            raise ValueError("assistance target is required")
        if target_actor_id == actor_id:
            raise ValueError("assistance target must be another meeting attendee")
        if not message_id.strip():
            raise ValueError("message_id is required")
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if not self.task_contributor_role(action, actor_id, cursor=cursor):
                raise PermissionError(
                    "only the assigned owner or an active collaborator may request assistance"
                )
            if action["status"] != ActionItemStatus.TRACKING:
                raise ValueError("assistance may only be requested while tracking")
            target = cursor.execute(
                "SELECT ep.actor_id FROM episode_participants ep "
                "JOIN actors a ON a.actor_id = ep.actor_id "
                "WHERE ep.episode_id = ? AND ep.actor_id = ? "
                "AND ep.role IN ('COORDINATOR','AGGREGATOR','PARTICIPANT','ACTION_OWNER') "
                "AND a.status = 'ACTIVE'",
                (self.episode_id, target_actor_id),
            ).fetchone()
            if not target:
                raise PermissionError("assistance target must be a meeting attendee")
            unresolved = cursor.execute(
                "SELECT assistance_request_id FROM assistance_requests "
                "WHERE action_item_id = ? AND status IN ('OPEN','ACKNOWLEDGED') "
                "LIMIT 1",
                (action_item_id,),
            ).fetchone()
            if unresolved:
                raise ValueError("this task already has an unresolved assistance request")
            if blocking_action_item_id:
                blocking = cursor.execute(
                    "SELECT 1 FROM action_items WHERE action_item_id = ? "
                    "AND episode_id = ?",
                    (blocking_action_item_id, self.episode_id),
                ).fetchone()
                if not blocking:
                    raise ValueError("blocking task must belong to the same meeting")
            assistance_request_id = f"help_{uuid4().hex}"
            cursor.execute(
                "INSERT INTO assistance_requests(assistance_request_id, episode_id, "
                "action_item_id, requester_actor_id, target_actor_id, category, summary, "
                "blocking_action_item_id, status, created_sim_time, updated_sim_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
                (
                    assistance_request_id,
                    self.episode_id,
                    action_item_id,
                    actor_id,
                    target_actor_id,
                    category,
                    summary,
                    blocking_action_item_id,
                    sim_time,
                    sim_time,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="AssistanceRequest",
                aggregate_id=assistance_request_id,
                event_type="AssistanceRequested",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "requester_actor_id": actor_id,
                    "target_actor_id": target_actor_id,
                    "category": category,
                    "summary": summary,
                    "blocking_action_item_id": blocking_action_item_id,
                },
                correlation_id=correlation_id,
            )
            # Until now a help request only appeared on a page the target might
            # never open, which made asking for help unreliable by construction.
            requester = cursor.execute(
                "SELECT display_name FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            helped_action = cursor.execute(
                "SELECT title FROM action_items WHERE action_item_id = ?",
                (action_item_id,),
            ).fetchone()
            self._notify(
                cursor,
                effect_type=NOTIFY_ASSISTANCE_REQUESTED,
                recipient_actor_ids=[target_actor_id],
                action_item_id=action_item_id,
                title=f'{requester["display_name"]} 在任务上向你求助',
                summary=summary,
                fields=[
                    {"label": "任务", "value": helped_action["title"]},
                    {"label": "类别", "value": category},
                ],
                decisions=[
                    {
                        "name": "ASSISTANCE_ACKNOWLEDGE",
                        "label": "我来接手",
                        "requires_reason": False,
                    }
                ],
                correlation_id=correlation_id,
                sim_time=sim_time,
                trigger_key=assistance_request_id,
                subject_id=assistance_request_id,
            )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="ASSISTANCE_REQUESTED",
                sim_time=sim_time,
                correlation_id=correlation_id,
                note=summary,
            )
            result = {
                "assistance_request_id": assistance_request_id,
                "action_item_id": action_item_id,
                "target_actor_id": target_actor_id,
                "status": "OPEN",
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def update_assistance(
        self,
        assistance_request_id: str,
        *,
        actor_id: str,
        action: str,
        message_id: str,
        resolution_summary: str = "",
    ) -> dict[str, Any]:
        action = action.strip().upper()
        resolution_summary = resolution_summary.strip()
        if action not in {"ACKNOWLEDGE", "RESOLVE", "CANCEL"}:
            raise ValueError("unsupported assistance action")
        if action == "RESOLVE" and not resolution_summary:
            raise ValueError("resolution summary is required")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            request = cursor.execute(
                "SELECT * FROM assistance_requests WHERE assistance_request_id = ? "
                "AND episode_id = ?",
                (assistance_request_id, self.episode_id),
            ).fetchone()
            if not request:
                raise KeyError(assistance_request_id)
            if request["status"] not in ("OPEN", "ACKNOWLEDGED"):
                raise ValueError("assistance request is already closed")
            if action == "ACKNOWLEDGE":
                if actor_id != request["target_actor_id"]:
                    raise PermissionError("only the requested attendee may acknowledge help")
                if request["status"] != "OPEN":
                    raise ValueError("assistance request is already acknowledged")
                new_status = "ACKNOWLEDGED"
                event_type = "AssistanceAcknowledged"
            elif action == "CANCEL":
                if actor_id != request["requester_actor_id"]:
                    raise PermissionError("only the requester may cancel help")
                new_status = "CANCELLED"
                event_type = "AssistanceCancelled"
            else:
                if actor_id not in (
                    request["requester_actor_id"],
                    request["target_actor_id"],
                ):
                    raise PermissionError("only the requester or helper may resolve help")
                new_status = "RESOLVED"
                event_type = "AssistanceResolved"
            resolved_time = sim_time if new_status in ("RESOLVED", "CANCELLED") else None
            cursor.execute(
                "UPDATE assistance_requests SET status = ?, updated_sim_time = ?, "
                "resolved_sim_time = ?, resolution_summary = ? "
                "WHERE assistance_request_id = ?",
                (
                    new_status,
                    sim_time,
                    resolved_time,
                    resolution_summary or None,
                    assistance_request_id,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="AssistanceRequest",
                aggregate_id=assistance_request_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "action_item_id": request["action_item_id"],
                    "actor_id": actor_id,
                    "status": new_status,
                    "resolution_summary": resolution_summary,
                },
                correlation_id=correlation_id,
            )
            result = {
                "assistance_request_id": assistance_request_id,
                "action_item_id": request["action_item_id"],
                "status": new_status,
                "updated_sim_time": sim_time,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def update_progress(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        progress_summary: str,
        blocked: bool,
        blocker_reason: str,
        help_needed: str,
        next_step: str,
        message_id: str,
    ) -> dict[str, Any]:
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        progress_summary = progress_summary.strip()
        blocker_reason = blocker_reason.strip()
        help_needed = help_needed.strip()
        next_step = next_step.strip()
        if not progress_summary:
            raise ValueError("progress summary is required")
        if blocked and not blocker_reason:
            raise ValueError("blocker reason is required when blocked")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            contributor_role = self.task_contributor_role(
                action, actor_id, cursor=cursor
            )
            if not contributor_role:
                raise PermissionError(
                    "only the assigned owner or an active collaborator may update progress"
                )
            if action["status"] != ActionItemStatus.TRACKING:
                raise ValueError("progress may only be updated while tracking")
            payload = {
                "progress_summary": progress_summary,
                "blocked": bool(blocked),
                "blocker_reason": blocker_reason if blocked else "",
                "help_needed": help_needed,
                "next_step": next_step,
                "updated_by": actor_id,
                "contributor_role": contributor_role,
            }
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ActionItemProgressUpdated",
                sim_time=sim_time,
                payload=payload,
                correlation_id=f"corr_{message_id}",
            )
            self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="BLOCKED" if blocked else "ON_TRACK",
                sim_time=sim_time,
                correlation_id=f"corr_{message_id}",
                note=progress_summary,
            )
            result = {
                "action_item_id": action_item_id,
                "status": action["status"],
                "progress": payload,
                "updated_sim_time": sim_time,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def revise_personal_commitment(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        proposed_deadline_sim_time: str,
        reason: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Create a new personal commitment without changing the team need-by."""
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        if not message_id.strip():
            raise ValueError("message_id is required")
        reason = reason.strip()
        promised_by = iso_time(proposed_deadline_sim_time)
        if parse_time(promised_by) <= parse_time(self.now()):
            raise ValueError("personal promised time must be later than the current time")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])

        sim_time = self.now()
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ? "
                "AND episode_id = ?",
                (action_item_id, self.episode_id),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            if action["owner_actor_id"] != actor_id:
                raise PermissionError(
                    "only the task executor may revise the personal promise"
                )
            if action["status"] not in (
                ActionItemStatus.TRACKING,
                ActionItemStatus.PENDING_ACCEPTANCE,
            ):
                raise ValueError("this action item cannot revise its personal promise")
            current = cursor.execute(
                "SELECT * FROM commitment_revisions WHERE "
                "commitment_revision_id = ? AND status = 'ACTIVE'",
                (action["active_commitment_revision_id"],),
            ).fetchone()
            if not current:
                raise ValueError("action item has no active commitment")
            team_required_by = action["team_required_by_sim_time"]
            if not team_required_by:
                raise ValueError("action item has no team required time")
            previous_conflict = parse_time(
                current["promised_deadline_sim_time"]
            ) > parse_time(team_required_by)
            schedule_conflict = parse_time(promised_by) > parse_time(team_required_by)
            revision_id = f"com_{uuid4().hex}"
            revision_no = int(current["revision_no"]) + 1
            cursor.execute(
                "UPDATE commitment_revisions SET status = 'SUPERSEDED' "
                "WHERE commitment_revision_id = ?",
                (current["commitment_revision_id"],),
            )
            cursor.execute(
                """
                INSERT INTO commitment_revisions(
                    commitment_revision_id, action_item_id, revision_no,
                    owner_actor_id, deliverable_key, promised_deadline_sim_time,
                    source_message_id, source_span, status, created_sim_time,
                    supersedes_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'personal_promise.revision',
                          'ACTIVE', ?, ?)
                """,
                (
                    revision_id,
                    action_item_id,
                    revision_no,
                    actor_id,
                    current["deliverable_key"],
                    promised_by,
                    message_id,
                    sim_time,
                    current["commitment_revision_id"],
                ),
            )
            cursor.execute(
                "UPDATE action_items SET active_commitment_revision_id = ?, "
                "deadline_sim_time = ?, version = version + 1 "
                "WHERE action_item_id = ?",
                (revision_id, promised_by, action_item_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CommitmentRevision",
                aggregate_id=current["commitment_revision_id"],
                event_type="CommitmentRevisionSuperseded",
                sim_time=sim_time,
                payload={"superseded_by": revision_id},
                correlation_id=correlation_id,
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CommitmentRevision",
                aggregate_id=revision_id,
                event_type="CommitmentRevisionCreated",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "promised_deadline_sim_time": promised_by,
                    "reason": reason,
                },
                correlation_id=correlation_id,
            )
            if schedule_conflict:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="ScheduleConflictDetected",
                    sim_time=sim_time,
                    payload={
                        "promised_deadline_sim_time": promised_by,
                        "team_required_by_sim_time": team_required_by,
                    },
                    correlation_id=correlation_id,
                )
            elif previous_conflict:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action_item_id,
                    event_type="ScheduleConflictResolved",
                    sim_time=sim_time,
                    payload={
                        "promised_deadline_sim_time": promised_by,
                        "team_required_by_sim_time": team_required_by,
                    },
                    correlation_id=correlation_id,
                )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="PROMISE_REVISED",
                sim_time=sim_time,
                correlation_id=correlation_id,
                note=reason,
            )
            result = {
                "action_item_id": action_item_id,
                "commitment_revision_id": revision_id,
                "promised_deadline_sim_time": promised_by,
                "team_required_by_sim_time": team_required_by,
                "schedule_conflict": schedule_conflict,
                "status": "ACTIVE",
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def review_artifact(
        self,
        version_id: str,
        *,
        actor_id: str,
        approve: bool,
        comment: str,
        message_id: str,
        completion_report: str | None = None,
    ) -> dict[str, Any]:
        self._require_aggregator(actor_id)
        comment = comment.strip()
        if not approve and not comment:
            raise ValueError("a rejection comment is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            version = cursor.execute(
                "SELECT v.*, a.episode_id, a.status AS action_status, "
                "a.current_valid_version_id FROM artifact_versions v "
                "JOIN action_items a ON a.action_item_id = v.action_item_id "
                "WHERE v.version_id = ?",
                (version_id,),
            ).fetchone()
            if not version or version["episode_id"] != self.episode_id:
                raise KeyError(version_id)
            if version["review_status"] != "PENDING":
                same_decision = (
                    approve and version["review_status"] == "ACCEPTED"
                ) or (not approve and version["review_status"] == "REJECTED")
                if not same_decision:
                    raise ValueError(
                        "artifact version was already reviewed with the opposite decision"
                    )
                accepted = cursor.execute(
                    "SELECT accepted_task_result_id FROM accepted_task_results "
                    "WHERE accepted_version_id = ?",
                    (version_id,),
                ).fetchone()
                result = {
                    "version_id": version_id,
                    "action_item_id": version["action_item_id"],
                    "review_status": version["review_status"],
                    "action_status": version["action_status"],
                    "accepted_task_result_id": (
                        accepted["accepted_task_result_id"] if accepted else None
                    ),
                    "idempotent": True,
                }
                self._record_inbound(
                    cursor, message_id=message_id, result=result, sim_time=sim_time
                )
                return result
            if version["processing_status"] not in ("READY", "FAILED"):
                raise ValueError(
                    "task result processing must finish before human acceptance"
                )
            review_status = "ACCEPTED" if approve else "REJECTED"
            cursor.execute(
                "UPDATE artifact_versions SET review_status = ?, reviewed_by = ?, "
                "reviewed_sim_time = ?, review_comment = ? WHERE version_id = ?",
                (review_status, actor_id, sim_time, comment, version_id),
            )
            if approve:
                payload = (
                    json.loads(version["payload"])
                    if isinstance(version["payload"], str)
                    else dict(version["payload"])
                )
                source_manifest = (
                    json.loads(version["source_manifest"])
                    if isinstance(version["source_manifest"], str)
                    else dict(version["source_manifest"] or {})
                )
                processing_result = (
                    json.loads(version["processing_result"])
                    if isinstance(version["processing_result"], str)
                    else version["processing_result"]
                )
                task_result_id = f"result_{uuid4().hex}"
                report = (completion_report or "").strip() or str(
                    payload.get("summary")
                    or payload.get("completion_note")
                    or payload.get("content")
                    or "负责人已验收当前版本"
                )
                completed_content_refs = [
                    f'version:{version_id}:field:{field}'
                    for field in source_manifest.get("version_fields", [])
                ] + [
                    f'version:{version_id}:{item["source_ref"]}'
                    for item in source_manifest.get("attachments", [])
                    if item.get("extraction_status") == "EXTRACTED"
                ]
                processing_metadata = {
                    "status": version["processing_status"],
                    "processor": (
                        json.loads(version["processing_metadata"])
                        if isinstance(version["processing_metadata"], str)
                        else version["processing_metadata"]
                    ),
                    "error": (
                        {
                            "code": version["processing_error_code"],
                            "stage": version["processing_error_stage"],
                            "detail": version["processing_error_detail"],
                        }
                        if version["processing_status"] == "FAILED"
                        else None
                    ),
                    "review_comment": comment,
                }
                cursor.execute(
                    "INSERT INTO accepted_task_results(accepted_task_result_id, "
                    "action_item_id, accepted_version_id, completed_content_refs, "
                    "completion_report, normalized_result, source_manifest, "
                    "processing_metadata, accepted_by, accepted_sim_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_result_id,
                        version["action_item_id"],
                        version_id,
                        canonical_json(completed_content_refs),
                        report,
                        canonical_json(
                            (processing_result or {}).get("normalized_result")
                        ),
                        canonical_json(source_manifest),
                        canonical_json(processing_metadata),
                        actor_id,
                        sim_time,
                    ),
                )
                cursor.execute(
                    "UPDATE action_items SET current_valid_version_id = ?, status = ?, "
                    "version = version + 1 WHERE action_item_id = ?",
                    (version_id, ActionItemStatus.ACCEPTED, version["action_item_id"]),
                )
                self._invalidate_downstream_inputs(
                    cursor,
                    upstream_action_item_id=version["action_item_id"],
                    accepted_version_id=version_id,
                    sim_time=sim_time,
                    correlation_id=f"corr_{message_id}",
                )
                event_type = "ArtifactVersionAcceptedByCoordinator"
                self._supersede_pending_final(
                    cursor,
                    version["action_item_id"],
                    sim_time,
                    f"corr_{message_id}",
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="AcceptedTaskResult",
                    aggregate_id=task_result_id,
                    event_type="AcceptedTaskResultFrozen",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": version["action_item_id"],
                        "accepted_version_id": version_id,
                        "accepted_by": actor_id,
                    },
                    correlation_id=f"corr_{message_id}",
                )
            else:
                restored_status = ActionItemStatus.TRACKING
                if version["current_valid_version_id"]:
                    included_in_current_final = cursor.execute(
                        "SELECT 1 FROM final_field_lineage l "
                        "JOIN episodes e ON e.current_final_deliverable_id = "
                        "l.final_deliverable_id "
                        "JOIN final_deliverables f ON f.final_deliverable_id = "
                        "l.final_deliverable_id "
                        "WHERE e.episode_id = ? AND l.action_item_id = ? "
                        "AND l.version_id = ? AND f.status <> 'SUPERSEDED' LIMIT 1",
                        (
                            self.episode_id,
                            version["action_item_id"],
                            version["current_valid_version_id"],
                        ),
                    ).fetchone()
                    restored_status = (
                        ActionItemStatus.AGGREGATED
                        if included_in_current_final
                        else ActionItemStatus.ACCEPTED
                    )
                cursor.execute(
                    "UPDATE action_items SET status = ?, version = version + 1 "
                    "WHERE action_item_id = ?",
                    (restored_status, version["action_item_id"]),
                )
                event_type = "ArtifactVersionReturnedForRevision"
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "action_item_id": version["action_item_id"],
                    "reviewed_by": actor_id,
                    "comment": comment,
                },
                correlation_id=f"corr_{message_id}",
            )
            reviewed_action = cursor.execute(
                "SELECT title, owner_actor_id FROM action_items "
                "WHERE action_item_id = ?",
                (version["action_item_id"],),
            ).fetchone()
            # A verdict is information, not a decision the recipient makes, so
            # it carries no inline actions -- only a link back for the rework.
            self._notify(
                cursor,
                effect_type=NOTIFY_REVIEW_DECIDED,
                recipient_actor_ids=[
                    version["submitted_by_actor_id"],
                    reviewed_action["owner_actor_id"] if reviewed_action else None,
                ],
                action_item_id=version["action_item_id"],
                title=(
                    f'验收通过：{reviewed_action["title"]}'
                    if approve
                    else f'需要重做：{reviewed_action["title"]}'
                ),
                summary=comment or ("已通过验收" if approve else "负责人要求重做"),
                fields=[{"label": "版本", "value": version_id}],
                correlation_id=f"corr_{message_id}",
                sim_time=sim_time,
                trigger_key=f"{version_id}:{review_status}",
            )
            result = {
                "version_id": version_id,
                "action_item_id": version["action_item_id"],
                "review_status": review_status,
                "action_status": (
                    ActionItemStatus.ACCEPTED
                    if approve
                    else restored_status
                ),
                "accepted_task_result_id": task_result_id if approve else None,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        if approve:
            collaboration = self.generate_task_collaboration_artifacts(
                result["accepted_task_result_id"]
            )
            result["collaboration_report_status"] = collaboration["status"]
            result["memory_candidate_ids"] = collaboration.get(
                "memory_candidate_ids", []
            )
        return result

    def generate_task_collaboration_artifacts(
        self, accepted_task_result_id: str
    ) -> dict[str, Any]:
        """Generate a factual task report and private, correctable memory drafts."""
        accepted = self.db.one(
            "SELECT r.*, a.episode_id, a.title, a.owner_actor_id, "
            "a.deadline_sim_time, a.team_required_by_sim_time, "
            "a.proposal_metadata, actor.display_name AS owner_display_name "
            "FROM accepted_task_results r "
            "JOIN action_items a ON a.action_item_id = r.action_item_id "
            "JOIN actors actor ON actor.actor_id = a.owner_actor_id "
            "WHERE r.accepted_task_result_id = ?",
            (accepted_task_result_id,),
        )
        if not accepted or accepted["episode_id"] != self.episode_id:
            raise KeyError(accepted_task_result_id)
        if accepted["collaboration_report_status"] == "READY":
            report = (
                json.loads(accepted["collaboration_report"])
                if isinstance(accepted["collaboration_report"], str)
                else accepted["collaboration_report"]
            )
            return {
                "status": "READY",
                "accepted_task_result_id": accepted_task_result_id,
                "memory_candidate_ids": report.get("memory_candidate_ids", []),
                "idempotent": True,
            }

        sim_time = self.now()
        try:
            metadata = (
                json.loads(accepted["proposal_metadata"])
                if isinstance(accepted["proposal_metadata"], str)
                else dict(accepted["proposal_metadata"] or {})
            )
            commitments = [
                dict(row)
                for row in self.db.all(
                    "SELECT commitment_revision_id, revision_no, "
                    "promised_deadline_sim_time, status, created_sim_time, "
                    "supersedes_revision_id FROM commitment_revisions "
                    "WHERE action_item_id = ? ORDER BY revision_no",
                    (accepted["action_item_id"],),
                )
            ]
            assistance = [
                dict(row)
                for row in self.db.all(
                    "SELECT assistance_request_id, requester_actor_id, target_actor_id, category, status, "
                    "created_sim_time, resolved_sim_time, resolution_summary "
                    "FROM assistance_requests WHERE action_item_id = ? "
                    "ORDER BY created_sim_time",
                    (accepted["action_item_id"],),
                )
            ]
            versions = [
                dict(row)
                for row in self.db.all(
                    "SELECT version_id, received_sim_time, received_sequence, "
                    "validation_status, review_status, review_comment, "
                    "processing_status, submitted_by_actor_id FROM artifact_versions "
                    "WHERE action_item_id = ? ORDER BY received_sequence",
                    (accepted["action_item_id"],),
                )
            ]
            signal_events = self.db.all(
                "SELECT event_id, event_type, sim_time, payload FROM audit_events "
                "WHERE run_id = ? AND aggregate_id = ? "
                "AND event_type = 'ProgressSignalRecorded' ORDER BY sequence_no",
                (self.run_id, accepted["action_item_id"]),
            )
            signal_summary = []
            for event in signal_events:
                payload = (
                    json.loads(event["payload"])
                    if isinstance(event["payload"], str)
                    else event["payload"]
                )
                signal_summary.append(
                    {
                        "event_id": event["event_id"],
                        "signal_type": payload.get("signal_type"),
                        "sim_time": event["sim_time"],
                        "valid_until": payload.get("valid_until"),
                        "actor_id": payload.get("actor_id"),
                        "contributor_role": payload.get("contributor_role"),
                    }
                )
            actor_names = {
                row["actor_id"]: row["display_name"]
                for row in self.db.all(
                    "SELECT actor_id, display_name FROM actors"
                )
            }
            collaborator_sources: dict[str, set[str]] = {}
            for actor_id in metadata.get("collaborator_actor_ids", []):
                collaborator_sources.setdefault(actor_id, set()).add(
                    "MEETING_RECORDED"
                )
            for item in assistance:
                collaborator_sources.setdefault(
                    item["target_actor_id"], set()
                ).add("ASSISTANCE_REQUEST")
            for item in versions:
                submitter = item.get("submitted_by_actor_id")
                if submitter and submitter != accepted["owner_actor_id"]:
                    collaborator_sources.setdefault(submitter, set()).add(
                        "DELIVERY_CONTRIBUTION"
                    )
            collaborator_sources.pop(accepted["owner_actor_id"], None)
            aggregate_ids = {
                accepted["action_item_id"],
                accepted["accepted_version_id"],
                *(item["assistance_request_id"] for item in assistance),
                *(item["version_id"] for item in versions),
            }
            source_event_ids: list[str] = []
            for aggregate_id in sorted(aggregate_ids):
                source_event_ids.extend(
                    row["event_id"]
                    for row in self.db.all(
                        "SELECT event_id FROM audit_events WHERE run_id = ? "
                        "AND aggregate_id = ? ORDER BY sequence_no",
                        (self.run_id, aggregate_id),
                    )
                )
            report: dict[str, Any] = {
                "report_version": "task-collaboration-report.v1",
                "action_item_id": accepted["action_item_id"],
                "accepted_version_id": accepted["accepted_version_id"],
                "task_definition": {
                    "title": accepted["title"],
                    "deliverable": metadata.get("deliverable", ""),
                    "work_requirements": metadata.get("work_requirements", ""),
                },
                "executor": {
                    "actor_id": accepted["owner_actor_id"],
                    "display_name": accepted["owner_display_name"],
                },
                "recorded_collaborators": [
                    {
                        "actor_id": actor_id,
                        "display_name": actor_names.get(actor_id, actor_id),
                        "sources": sorted(sources),
                    }
                    for actor_id, sources in sorted(collaborator_sources.items())
                ],
                "schedule": {
                    "team_required_by_sim_time": accepted[
                        "team_required_by_sim_time"
                    ],
                    "final_promised_by_sim_time": accepted["deadline_sim_time"],
                },
                "commitment_revisions": commitments,
                "signals": signal_summary,
                "assistance": assistance,
                "delivery_versions": versions,
                "acceptance": {
                    "accepted_task_result_id": accepted_task_result_id,
                    "accepted_by": accepted["accepted_by"],
                    "accepted_sim_time": accepted["accepted_sim_time"],
                    "completion_report": accepted["completion_report"],
                },
                "final_result_refs": {
                    "accepted_version_id": accepted["accepted_version_id"],
                    "completed_content_refs": (
                        json.loads(accepted["completed_content_refs"])
                        if isinstance(accepted["completed_content_refs"], str)
                        else accepted["completed_content_refs"]
                    ),
                },
                "source_event_ids": sorted(set(source_event_ids)),
                "memory_candidate_ids": [],
            }
            quick_signals = [
                item
                for item in signal_summary
                if item["signal_type"] in self.QUICK_SIGNAL_TYPES
            ]
            candidate_specs: list[tuple[str, dict[str, Any], list[str]]] = []
            # A version resubmitted because the coordinator rejected the previous
            # one is a property of the task, not a working preference.  Only
            # voluntary revisions count toward an iteration habit.
            # 交付模式 used to be counted here, from how many versions somebody
            # submitted voluntarily. It is asked in the questionnaire now: it is
            # a headline working style a person can state about themselves, and
            # observation is better spent on the behaviour they misjudge. The
            # guard below would reject it anyway.
            resolved_help = [item for item in assistance if item["status"] == "RESOLVED"]
            if resolved_help:
                candidate_specs.append(
                    (
                        "HELP_SEEKING",
                        {
                            **memory_value("HELP_SEEKING", "ASK_WHEN_BLOCKED"),
                            "observation_count": len(resolved_help),
                        },
                        [
                            f'assistance:{item["assistance_request_id"]}'
                            for item in resolved_help
                        ],
                    )
                )
            if len(quick_signals) >= 2:
                candidate_specs.append(
                    (
                        "PROGRESS_SIGNAL",
                        {
                            **memory_value("PROGRESS_SIGNAL", "QUICK_SIGNAL"),
                            "observation_count": len(quick_signals),
                        },
                        [item["event_id"] for item in quick_signals],
                    )
                )
            # A nominator, when one is injected, may reach the nine labels the
            # counting rules cannot -- they only ever emit ITERATIVE_REVIEW,
            # ASK_WHEN_BLOCKED and QUICK_SIGNAL, one per topic. It is additive
            # and subordinate: a topic the rules already covered is left alone,
            # every label is re-checked against the lexicon, every citation
            # against this task's own events, and the result lands as a draft
            # like any other. A provider failure loses nominations, not the
            # report -- the deterministic floor has already been computed.
            if self.memory_nominator is not None:
                covered = {topic for topic, _, _ in candidate_specs}
                settled = [
                    {"topic": row["topic"], "code": str(code)}
                    for row in self.db.all(
                        "SELECT topic, value FROM collaboration_memories "
                        "WHERE actor_id = ? AND status IN "
                        "('CONFIRMED','REJECTED','SUPERSEDED')",
                        (accepted["owner_actor_id"],),
                    )
                    if (code := (self._decoded_json(row["value"], {}) or {}).get("code"))
                ]
                try:
                    nominated = self.memory_nominator.nominate(
                        report,
                        evidence_refs=set(source_event_ids)
                        | {f'version:{item["version_id"]}' for item in versions}
                        | {
                            f'assistance:{item["assistance_request_id"]}'
                            for item in assistance
                        },
                        existing=settled,
                    )
                except Exception as error:  # noqa: BLE001 - nominations are optional
                    report["nomination_error"] = repr(error)
                    nominated = []
                for item in nominated:
                    if item.topic in covered:
                        continue
                    candidate_specs.append(
                        (
                            item.topic,
                            {
                                **memory_value(item.topic, item.code),
                                "nominated_by": "model",
                                "prompt_version": (
                                    MEMORY_NOMINATION_PROMPT_VERSION
                                ),
                            },
                            list(item.evidence_refs),
                        )
                    )

            # How broadly a behaviour was seen is reported rather than used as a
            # gate.  Requiring two tasks would suppress almost every candidate in
            # a three-to-five task meeting, where most people own one task; the
            # rework exclusion above already removes the case that actually
            # misread task shape as preference.  The count travels with the
            # draft so the page can say "observed in 1 task" instead of implying
            # a settled habit, and the subject still chooses among all values.
            candidate_specs = [
                (
                    topic,
                    {
                        **value,
                        "distinct_action_item_count": self._memory_observation_breadth(
                            actor_id=accepted["owner_actor_id"],
                            topic=topic,
                            code=str(value["code"]),
                            current_action_item_id=accepted["action_item_id"],
                        ),
                        "scope": "THIS_TASK_ONLY_UNTIL_CONFIRMED",
                    },
                    evidence_refs,
                )
                for topic, value, evidence_refs in candidate_specs
            ]
            # Nothing observed may land on a self-declared topic. Those are the
            # ones the system has no way to see without guessing at intent, so
            # a rule or a nomination reaching one is a bug, and writing it would
            # put words in somebody's mouth under the label "observed".
            candidate_specs = [
                spec
                for spec in candidate_specs
                if topic_origin(spec[0]) == SYSTEM_OBSERVED
            ]
            memory_ids: list[str] = []
            with self.db.transaction() as cursor:
                for topic, value, evidence_refs in candidate_specs:
                    memory_id = f"memory_{uuid4().hex}"
                    cursor.execute(
                        "INSERT INTO collaboration_memories(memory_id, actor_id, topic, "
                        "value, visibility, status, evidence_refs, created_sim_time, "
                        "version, origin) VALUES (?, ?, ?, ?, 'PRIVATE', "
                        "'PRIVATE_DRAFT', ?, ?, 1, 'SYSTEM_OBSERVED')",
                        (
                            memory_id,
                            accepted["owner_actor_id"],
                            topic,
                            canonical_json(value),
                            canonical_json(evidence_refs),
                            sim_time,
                        ),
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="CollaborationMemory",
                        aggregate_id=memory_id,
                        event_type="CollaborationMemoryProposed",
                        sim_time=sim_time,
                        payload={
                            "actor_id": accepted["owner_actor_id"],
                            "topic": topic,
                            "evidence_refs": evidence_refs,
                        },
                        correlation_id=f"corr_{accepted_task_result_id}",
                    )
                    memory_ids.append(memory_id)
                report["memory_candidate_ids"] = memory_ids
                cursor.execute(
                    "UPDATE accepted_task_results SET collaboration_report = ?, "
                    "collaboration_report_status = 'READY', "
                    "collaboration_report_error = NULL "
                    "WHERE accepted_task_result_id = ?",
                    (canonical_json(report), accepted_task_result_id),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="AcceptedTaskResult",
                    aggregate_id=accepted_task_result_id,
                    event_type="TaskCollaborationReportGenerated",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": accepted["action_item_id"],
                        "accepted_version_id": accepted["accepted_version_id"],
                        "memory_candidate_ids": memory_ids,
                    },
                    correlation_id=f"corr_{accepted_task_result_id}",
                )
            return {
                "status": "READY",
                "accepted_task_result_id": accepted_task_result_id,
                "memory_candidate_ids": memory_ids,
            }
        except Exception as error:
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE accepted_task_results SET "
                    "collaboration_report_status = 'FAILED', "
                    "collaboration_report_error = ? "
                    "WHERE accepted_task_result_id = ?",
                    (str(error)[:1000], accepted_task_result_id),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="AcceptedTaskResult",
                    aggregate_id=accepted_task_result_id,
                    event_type="TaskCollaborationReportFailed",
                    sim_time=sim_time,
                    payload={"error": str(error)[:500]},
                    correlation_id=f"corr_{accepted_task_result_id}",
                )
            return {
                "status": "FAILED",
                "accepted_task_result_id": accepted_task_result_id,
                "error": str(error)[:1000],
            }

    def _memory_observation_breadth(
        self,
        *,
        actor_id: str,
        topic: str,
        code: str,
        current_action_item_id: str,
    ) -> int:
        """How many distinct action items support this topic/value for an actor.

        Counts the task being reported plus any earlier task whose accepted
        result already produced the same draft.  Evidence refs carry the task
        identity indirectly, so previous drafts are matched on topic and code.
        """

        rows = self.db.all(
            "SELECT value, evidence_refs FROM collaboration_memories "
            "WHERE actor_id = ? AND topic = ? AND status IN "
            "('PRIVATE_DRAFT','CONFIRMED','SUPERSEDED')",
            (actor_id, topic),
        )
        seen = {current_action_item_id}
        for row in rows:
            value = self._decoded_json(row["value"], {}) or {}
            if str(value.get("code") or "") != code:
                continue
            refs = self._decoded_json(row["evidence_refs"], []) or []
            for ref in refs:
                version_id = str(ref).removeprefix("version:")
                owner = self.db.one(
                    "SELECT action_item_id FROM artifact_versions WHERE version_id = ?",
                    (version_id,),
                )
                if owner:
                    seen.add(owner["action_item_id"])
                    continue
                assistance_id = str(ref).removeprefix("assistance:")
                helper = self.db.one(
                    "SELECT action_item_id FROM assistance_requests "
                    "WHERE assistance_request_id = ?",
                    (assistance_id,),
                )
                if helper:
                    seen.add(helper["action_item_id"])
        return len(seen)

    def declare_collaboration_memory(
        self,
        *,
        actor_id: str,
        topic: str,
        code: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Record what someone wants from collaborators, stated by themselves.

        These topics describe intent the system cannot observe without guessing,
        so there is no draft stage: the subject declares them directly.  They
        also give a newcomer a manual before any task history exists.
        """

        self._require_participant(actor_id)
        canonical = canonical_topic(topic)
        if topic_origin(canonical) != SELF_DECLARED:
            raise ValueError(
                "this topic is proposed from observed facts and must be confirmed, "
                "not declared"
            )
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        value = memory_value(canonical, code)
        sim_time = self.now()
        memory_id = f"memory_{uuid4().hex}"
        with self.db.transaction() as cursor:
            previous = cursor.execute(
                "SELECT memory_id, version FROM collaboration_memories "
                "WHERE actor_id = ? AND topic = ? AND status = 'CONFIRMED'",
                (actor_id, canonical),
            ).fetchone()
            if previous:
                cursor.execute(
                    "UPDATE collaboration_memories SET status = 'SUPERSEDED' "
                    "WHERE memory_id = ?",
                    (previous["memory_id"],),
                )
            cursor.execute(
                "INSERT INTO collaboration_memories(memory_id, actor_id, topic, "
                "value, visibility, status, evidence_refs, created_sim_time, "
                "version, origin, confirmed_by, confirmed_sim_time, "
                "supersedes_memory_id) VALUES (?, ?, ?, ?, 'PRIVATE', 'CONFIRMED', "
                "'[]', ?, ?, 'SELF_DECLARED', ?, ?, ?)",
                (
                    memory_id,
                    actor_id,
                    canonical,
                    canonical_json(value),
                    sim_time,
                    int(previous["version"]) + 1 if previous else 1,
                    actor_id,
                    sim_time,
                    previous["memory_id"] if previous else None,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CollaborationMemory",
                aggregate_id=memory_id,
                event_type="CollaborationMemoryDeclared",
                sim_time=sim_time,
                payload={
                    "actor_id": actor_id,
                    "topic": canonical,
                    "code": value["code"],
                    "supersedes_memory_id": (
                        previous["memory_id"] if previous else None
                    ),
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "memory_id": memory_id,
                "topic": canonical,
                "code": value["code"],
                "status": "CONFIRMED",
                "origin": SELF_DECLARED,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def decide_collaboration_memory(
        self,
        memory_id: str,
        *,
        actor_id: str,
        action: str,
        message_id: str,
        replacement_code: str = "",
        corrected_value: str = "",
    ) -> dict[str, Any]:
        self._require_participant(actor_id)
        action = action.strip().upper()
        replacement_code = (replacement_code or corrected_value).strip().upper()
        if action == "CORRECT":
            action = "REPLACE"
        if action not in {"CONFIRM", "REPLACE", "REJECT", "WITHDRAW"}:
            raise ValueError("unsupported memory action")
        if action == "REPLACE" and not replacement_code:
            raise ValueError("replacement memory code is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        memory_preview = self.db.one(
            "SELECT actor_id, topic FROM collaboration_memories WHERE memory_id = ?",
            (memory_id,),
        )
        if not memory_preview:
            raise KeyError(memory_id)
        if memory_preview["actor_id"] != actor_id:
            raise PermissionError("private collaboration memory belongs to another actor")
        validated_replacement: dict[str, str] | None = None
        if action == "REPLACE":
            try:
                validated_replacement = memory_value(
                    memory_preview["topic"], replacement_code
                )
            except ValueError:
                sim_time = self.now()
                with self.db.transaction() as cursor:
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="CollaborationMemory",
                        aggregate_id=memory_id,
                        event_type="CollaborationMemoryInvalidValueRejected",
                        sim_time=sim_time,
                        payload={
                            "actor_id": actor_id,
                            "reason": "VALUE_NOT_IN_PRESET_LEXICON",
                        },
                        correlation_id=f"corr_{message_id}",
                    )
                raise
        sim_time = self.now()
        with self.db.transaction() as cursor:
            memory = cursor.execute(
                "SELECT * FROM collaboration_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if not memory:
                raise KeyError(memory_id)
            if memory["actor_id"] != actor_id:
                raise PermissionError("private collaboration memory belongs to another actor")
            if action == "WITHDRAW":
                # Taking an entry back down without putting another up.
                # Declaring again would supersede it, but that forces a person
                # to state something in order to stop stating something, and
                # "I would rather this were not shown" is its own answer. A
                # withdrawn entry stops reaching colleagues and becomes
                # unanswered again, so the questionnaire can ask once more.
                if memory["status"] != "CONFIRMED":
                    raise ValueError("only a confirmed memory may be withdrawn")
                cursor.execute(
                    "UPDATE collaboration_memories SET status = 'SUPERSEDED' "
                    "WHERE memory_id = ?",
                    (memory_id,),
                )
                resulting_id = memory_id
                status = "SUPERSEDED"
                event_type = "CollaborationMemoryWithdrawn"
            elif action == "REJECT":
                if memory["status"] != "PRIVATE_DRAFT":
                    raise ValueError("only a draft memory may be rejected")
                cursor.execute(
                    "UPDATE collaboration_memories SET status = 'REJECTED' "
                    "WHERE memory_id = ?",
                    (memory_id,),
                )
                resulting_id = memory_id
                status = "REJECTED"
                event_type = "CollaborationMemoryRejected"
            elif action == "CONFIRM":
                if memory["status"] == "CONFIRMED":
                    status = "CONFIRMED"
                    resulting_id = memory_id
                    event_type = "CollaborationMemoryConfirmed"
                elif memory["status"] == "PRIVATE_DRAFT":
                    cursor.execute(
                        "UPDATE collaboration_memories SET status = 'SUPERSEDED' "
                        "WHERE actor_id = ? AND topic = ? AND status = 'CONFIRMED' "
                        "AND memory_id <> ?",
                        (actor_id, memory["topic"], memory_id),
                    )
                    cursor.execute(
                        "UPDATE collaboration_memories SET status = 'CONFIRMED', "
                        "confirmed_by = ?, confirmed_sim_time = ? WHERE memory_id = ?",
                        (actor_id, sim_time, memory_id),
                    )
                    status = "CONFIRMED"
                    resulting_id = memory_id
                    event_type = "CollaborationMemoryConfirmed"
                else:
                    raise ValueError("this memory can no longer be confirmed")
            else:
                if memory["status"] not in ("PRIVATE_DRAFT", "CONFIRMED"):
                    raise ValueError("this memory can no longer be replaced")
                replacement_value = validated_replacement
                if replacement_value is None:
                    raise ValueError("replacement memory value was not validated")
                cursor.execute(
                    "UPDATE collaboration_memories SET status = 'SUPERSEDED' "
                    "WHERE actor_id = ? AND topic = ? AND status IN "
                    "('PRIVATE_DRAFT','CONFIRMED')",
                    (actor_id, memory["topic"]),
                )
                resulting_id = f"memory_{uuid4().hex}"
                evidence_refs = (
                    json.loads(memory["evidence_refs"])
                    if isinstance(memory["evidence_refs"], str)
                    else list(memory["evidence_refs"] or [])
                )
                cursor.execute(
                    "INSERT INTO collaboration_memories(memory_id, actor_id, topic, "
                    "value, visibility, status, evidence_refs, created_sim_time, "
                    "version, confirmed_by, confirmed_sim_time, supersedes_memory_id) "
                    "VALUES (?, ?, ?, ?, 'PRIVATE', 'CONFIRMED', ?, ?, ?, ?, ?, ?)",
                    (
                        resulting_id,
                        actor_id,
                        memory["topic"],
                        canonical_json(
                            {**replacement_value, "replaced_by_actor": True}
                        ),
                        canonical_json([*evidence_refs, f"memory:{memory_id}"]),
                        sim_time,
                        int(memory["version"]) + 1,
                        actor_id,
                        sim_time,
                        memory_id,
                    ),
                )
                status = "CONFIRMED"
                event_type = "CollaborationMemoryReplaced"
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="CollaborationMemory",
                aggregate_id=resulting_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "actor_id": actor_id,
                    "source_memory_id": memory_id,
                    "status": status,
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "memory_id": resulting_id,
                "status": status,
                "topic": memory["topic"],
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def advance_time(self, target: str) -> str:
        current = parse_time(self.now())
        target_time = parse_time(target)
        if target_time < current:
            raise ValueError("VirtualClock cannot move backwards")
        target_iso = iso_time(target_time)
        timezone = ZoneInfo(self.fixture["timezone"])
        current_local = current.astimezone(timezone)
        target_local = target_time.astimezone(timezone)
        reset_dates = []
        next_date = current_local.date() + timedelta(days=1)
        while next_date <= target_local.date():
            reset_dates.append(next_date)
            next_date += timedelta(days=1)
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE episodes SET current_sim_time = ?, version = version + 1 "
                "WHERE episode_id = ?",
                (target_iso, self.episode_id),
            )
            for reset_date in reset_dates:
                reset_time = datetime.combine(
                    reset_date, time.min, tzinfo=timezone
                ).isoformat()
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="Episode",
                    aggregate_id=self.episode_id,
                    event_type="DailyBudgetReset",
                    sim_time=reset_time,
                    payload={"budget_date": reset_date.isoformat()},
                    correlation_id=(
                        f"corr_budget_{stable_hash([self.episode_id, reset_date.isoformat()])[:12]}"
                    ),
                )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Episode",
                aggregate_id=self.episode_id,
                event_type="ClockAdvanced",
                sim_time=target_iso,
                payload={"from_time": iso_time(current), "to_time": target_iso},
                correlation_id=f"corr_clock_{stable_hash(target_iso)[:12]}",
            )
        return target_iso

    def submit_artifact(
        self,
        action_item_id: str,
        *,
        actor_id: str,
        message_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        late = self._ignore_if_archived(
            message_id=message_id, aggregate_id=action_item_id
        )
        if late:
            return late
        existing_receipt = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing_receipt:
            return json.loads(existing_receipt["processed_result"])

        sim_time = self.now()
        if not isinstance(payload, dict):
            raise ValueError("delivery payload must be an object")
        authorized_action = self.db.one(
            "SELECT * FROM action_items "
            "WHERE action_item_id = ? AND episode_id = ?",
            (action_item_id, self.episode_id),
        )
        if not authorized_action:
            raise KeyError(action_item_id)
        authorized_role = self.task_contributor_role(authorized_action, actor_id)
        if not authorized_role:
            # Reject before decoding or parsing a potentially expensive file.
            raise PermissionError(
                "only the assigned owner or an active collaborator may submit an artifact"
            )
        if authorized_role == "OWNER":
            collaboration = self.collaboration_progress(action_item_id)
            if collaboration and not collaboration["final_submission_ready"]:
                raise ValueError(
                    "structured task cannot submit a final result until upstream "
                    "results, ballot, and all required votes are complete"
                )
        raw_files = payload.get("files") or []
        if not isinstance(raw_files, list):
            raise ValueError("delivery files must be an array")
        attachment_extractions = extract_attachments(raw_files)
        attachment_input_hash = stable_hash(
            [
                {
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "size": item.get("size"),
                    "content_sha256": item.get("content_sha256"),
                }
                for item in attachment_extractions
            ]
        )
        attachment_failed_count = sum(
            item.get("extraction_status") == "FAILED"
            for item in attachment_extractions
        )
        attachment_output_status = (
            "FAILED"
            if attachment_extractions
            and attachment_failed_count == len(attachment_extractions)
            else "PARTIAL"
            if attachment_failed_count
            else "SUCCEEDED"
        )
        safe_payload = dict(payload)
        safe_payload["files"] = [
            {
                key: attachment.get(key)
                for key in (
                    "name",
                    "type",
                    "size",
                    "content_sha256",
                    "extraction_status",
                    "text_characters",
                    "truncated",
                    "extraction_error",
                )
                if key in attachment
            }
            for attachment in attachment_extractions
        ]
        source_manifest = {
            "submission": {
                "submitted_by_actor_id": actor_id,
                "contributor_role": authorized_role,
            },
            "version_fields": [
                field
                for field in safe_payload
                if field not in ("files", "links")
                and safe_payload.get(field) not in (None, "", [])
            ],
            "links": [
                {"source_ref": f"link:{index}", "url": str(value)}
                for index, value in enumerate(safe_payload.get("links") or [])
                if str(value).strip()
            ],
            "attachments": [
                {
                    "source_ref": f"attachment:{index}",
                    **{
                        key: attachment.get(key)
                        for key in (
                            "name",
                            "type",
                            "size",
                            "content_sha256",
                            "extraction_status",
                            "text_characters",
                            "truncated",
                        )
                        if key in attachment
                    },
                }
                for index, attachment in enumerate(attachment_extractions)
            ],
        }
        content_hash = stable_hash(
            {
                "payload": safe_payload,
                "attachment_extractions": attachment_extractions,
                "submitted_by_actor_id": actor_id,
            }
        )
        correlation_id = f"corr_{message_id}"
        with self.db.transaction() as cursor:
            action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (action_item_id,),
            ).fetchone()
            if not action:
                raise KeyError(action_item_id)
            contributor_role = self.task_contributor_role(
                action, actor_id, cursor=cursor
            )
            if not contributor_role:
                raise PermissionError(
                    "only the assigned owner or an active collaborator may submit an artifact"
                )
            metadata = self.proposal_metadata(action)
            requires_human_acceptance = bool(
                metadata.get("requires_human_acceptance", False)
            )
            if requires_human_acceptance and action["status"] not in (
                ActionItemStatus.TRACKING,
                ActionItemStatus.ACCEPTED,
                ActionItemStatus.AGGREGATED,
            ):
                raise ValueError(
                    "a delivery may only be submitted while tracking, accepted, or aggregated"
                )
            if requires_human_acceptance:
                pending_review = cursor.execute(
                    "SELECT version_id FROM artifact_versions "
                    "WHERE action_item_id = ? AND review_status = 'PENDING' LIMIT 1",
                    (action_item_id,),
                ).fetchone()
                if pending_review:
                    raise ValueError("an artifact version is already pending acceptance")
            config = self.action_config.get(action_item_id)
            if config:
                required_fields = config["required_fields"]
            else:
                required_fields = metadata.get("required_fields", ["content"])
            missing = [
                field
                for field in required_fields
                if field not in safe_payload or safe_payload[field] in (None, "", [])
            ]
            if requires_human_acceptance and not any(
                safe_payload.get(field) not in (None, "", [])
                for field in ("content", "links", "files")
            ):
                missing.append("content_or_link_or_file")
            if (
                requires_human_acceptance
                and not safe_payload.get("content")
                and not safe_payload.get("links")
                and raw_files
                and not any(
                    item.get("extraction_status") == "EXTRACTED"
                    and item.get("extracted_text")
                    for item in attachment_extractions
                )
            ):
                missing.append("readable_attachment_or_content")
            validation = (
                ValidationStatus.FAILED if missing else ValidationStatus.PASSED
            )
            duplicate = cursor.execute(
                "SELECT * FROM artifact_versions WHERE action_item_id = ? AND content_hash = ?",
                (action_item_id, content_hash),
            ).fetchone()
            if duplicate:
                duplicate_is_contribution = bool(
                    duplicate["submitted_by_actor_id"]
                    and duplicate["submitted_by_actor_id"]
                    != action["owner_actor_id"]
                    and duplicate["review_status"] == "NOT_REQUIRED"
                )
                signal_valid_until = self._record_owner_signal(
                    cursor,
                    action_item_id=action_item_id,
                    actor_id=actor_id,
                    signal_type="ARTIFACT_SUBMITTED",
                    sim_time=sim_time,
                    correlation_id=correlation_id,
                )
                result = {
                    "version_id": duplicate["version_id"],
                    "validation_status": duplicate["validation_status"],
                    "review_status": duplicate["review_status"],
                    "processing_status": duplicate["processing_status"],
                    "submission_kind": (
                        "CONTRIBUTION"
                        if duplicate_is_contribution
                        else "FINAL_CANDIDATE"
                    ),
                    "deduplicated": True,
                    "signal_valid_until": signal_valid_until,
                }
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=duplicate["version_id"],
                    event_type="ArtifactVersionDuplicateReceived",
                    sim_time=sim_time,
                    payload={
                        "message_id": message_id,
                        "submitted_by_actor_id": actor_id,
                        "contributor_role": contributor_role,
                    },
                    correlation_id=correlation_id,
                )
                if raw_files:
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ArtifactVersion",
                        aggregate_id=duplicate["version_id"],
                        event_type="AttachmentExtractionCompleted",
                        sim_time=sim_time,
                        payload={
                            "requested_by_actor_id": actor_id,
                            "deduplicated_submission": True,
                            "file_count": len(attachment_extractions),
                            "status_counts": dict(
                                Counter(
                                    item.get("extraction_status")
                                    for item in attachment_extractions
                                )
                            ),
                            "invocation": self._system_invocation_manifest(
                                capability_type="SKILL_ADAPTER",
                                purpose="TASK_RESULT_EVIDENCE_EXTRACTION",
                                field_allowlist=[
                                    "delivery.files[].name",
                                    "delivery.files[].type",
                                    "delivery.files[].size",
                                    "delivery.files[].data",
                                ],
                                entity_versions={
                                    "action_item_id": action_item_id,
                                    "artifact_version_id": duplicate["version_id"],
                                    "artifact_received_sequence": duplicate[
                                        "received_sequence"
                                    ],
                                },
                                skill_version=self.ATTACHMENT_EXTRACTOR_VERSION,
                                input_hash=attachment_input_hash,
                                output_status=attachment_output_status,
                            ),
                        },
                        correlation_id=correlation_id,
                    )
                self._record_inbound(
                    cursor, message_id=message_id, result=result, sim_time=sim_time
                )
                return result

            sequence_row = cursor.execute(
                "SELECT COALESCE(MAX(received_sequence), 0) + 1 AS next_sequence "
                "FROM artifact_versions"
            ).fetchone()
            latest_version = cursor.execute(
                "SELECT version_id FROM artifact_versions WHERE action_item_id = ? "
                "ORDER BY received_sequence DESC LIMIT 1",
                (action_item_id,),
            ).fetchone()
            version_id = f"ver_{uuid4().hex}"
            is_contribution = contributor_role != "OWNER"
            review_status = (
                "PENDING"
                if requires_human_acceptance
                and validation == ValidationStatus.PASSED
                and not is_contribution
                else "NOT_REQUIRED"
            )
            processing_status = (
                "PENDING"
                if requires_human_acceptance
                and validation == ValidationStatus.PASSED
                else "NOT_REQUIRED"
            )
            cursor.execute(
                """
                INSERT INTO artifact_versions(
                    version_id, action_item_id, submitted_by_actor_id, received_sim_time,
                    received_sequence, payload, validation_status,
                    validation_errors, source_message_id, content_hash,
                    supersedes_version_id, review_status
                    , attachment_extractions, source_manifest, processing_status,
                    processing_updated_sim_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    action_item_id,
                    actor_id,
                    sim_time,
                    int(sequence_row["next_sequence"]),
                    canonical_json(safe_payload),
                    validation,
                    canonical_json({"missing_fields": missing}),
                    message_id,
                    content_hash,
                    latest_version["version_id"] if latest_version else None,
                    review_status,
                    canonical_json(attachment_extractions),
                    canonical_json(source_manifest),
                    processing_status,
                    sim_time,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version_id,
                event_type="ArtifactVersionReceived",
                sim_time=sim_time,
                payload={
                    "action_item_id": action_item_id,
                    "content_hash": content_hash,
                    "submitted_by_actor_id": actor_id,
                    "contributor_role": contributor_role,
                    "submission_kind": (
                        "CONTRIBUTION" if is_contribution else "FINAL_CANDIDATE"
                    ),
                },
                correlation_id=correlation_id,
            )
            if raw_files:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=version_id,
                    event_type="AttachmentExtractionCompleted",
                    sim_time=sim_time,
                    payload={
                        "requested_by_actor_id": actor_id,
                        "deduplicated_submission": False,
                        "file_count": len(attachment_extractions),
                        "status_counts": dict(
                            Counter(
                                item.get("extraction_status")
                                for item in attachment_extractions
                            )
                        ),
                        "invocation": self._system_invocation_manifest(
                            capability_type="SKILL_ADAPTER",
                            purpose="TASK_RESULT_EVIDENCE_EXTRACTION",
                            field_allowlist=[
                                "delivery.files[].name",
                                "delivery.files[].type",
                                "delivery.files[].size",
                                "delivery.files[].data",
                            ],
                            entity_versions={
                                "action_item_id": action_item_id,
                                "artifact_version_id": version_id,
                                "artifact_received_sequence": int(
                                    sequence_row["next_sequence"]
                                ),
                            },
                            skill_version=self.ATTACHMENT_EXTRACTOR_VERSION,
                            input_hash=attachment_input_hash,
                            output_status=attachment_output_status,
                        ),
                    },
                    correlation_id=correlation_id,
                )
            validation_event = (
                "ArtifactVersionValidationPassed"
                if validation == ValidationStatus.PASSED
                else "ArtifactVersionValidationFailed"
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version_id,
                event_type=validation_event,
                sim_time=sim_time,
                payload={"missing_fields": missing},
                correlation_id=correlation_id,
            )
            if validation == ValidationStatus.PASSED:
                if requires_human_acceptance and is_contribution:
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ArtifactVersion",
                        aggregate_id=version_id,
                        event_type="ArtifactContributionAwaitingOwner",
                        sim_time=sim_time,
                        payload={
                            "action_item_id": action_item_id,
                            "submitted_by_actor_id": actor_id,
                            "contributor_role": contributor_role,
                        },
                        correlation_id=correlation_id,
                    )
                elif requires_human_acceptance:
                    cursor.execute(
                        "UPDATE action_items SET status = ?, version = version + 1 "
                        "WHERE action_item_id = ?",
                        (ActionItemStatus.PENDING_ACCEPTANCE, action_item_id),
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ArtifactVersion",
                        aggregate_id=version_id,
                        event_type="ArtifactVersionAwaitingAcceptance",
                        sim_time=sim_time,
                        payload={"action_item_id": action_item_id},
                        correlation_id=correlation_id,
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE action_items
                        SET current_valid_version_id = ?, status = ?, version = version + 1
                        WHERE action_item_id = ?
                        """,
                        (version_id, ActionItemStatus.ACCEPTED, action_item_id),
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ActionItem",
                        aggregate_id=action_item_id,
                        event_type="ActionItemCurrentValidVersionUpdated",
                        sim_time=sim_time,
                        payload={
                            "from_version_id": action["current_valid_version_id"],
                            "to_version_id": version_id,
                        },
                        correlation_id=correlation_id,
                    )
                    self._supersede_pending_final(
                        cursor, action_item_id, sim_time, correlation_id
                    )
            elif not action["current_valid_version_id"]:
                cursor.execute(
                    "UPDATE action_items SET status = ?, version = version + 1 "
                    "WHERE action_item_id = ?",
                    (ActionItemStatus.TRACKING, action_item_id),
                )
            signal_valid_until = self._record_owner_signal(
                cursor,
                action_item_id=action_item_id,
                actor_id=actor_id,
                signal_type="ARTIFACT_SUBMITTED",
                sim_time=sim_time,
                correlation_id=correlation_id,
            )
            result = {
                "version_id": version_id,
                "validation_status": validation,
                "review_status": review_status,
                "processing_status": processing_status,
                "submission_kind": (
                    "CONTRIBUTION" if is_contribution else "FINAL_CANDIDATE"
                ),
                "action_status": (
                    action["status"]
                    if is_contribution or validation == ValidationStatus.FAILED
                    else ActionItemStatus.PENDING_ACCEPTANCE
                    if requires_human_acceptance
                    else ActionItemStatus.ACCEPTED
                ),
                "attachment_extractions": [
                    {
                        "name": item.get("name"),
                        "status": item.get("extraction_status"),
                        "text_characters": item.get("text_characters", 0),
                    }
                    for item in attachment_extractions
                ],
                "missing_fields": missing,
                "deduplicated": False,
                "signal_valid_until": signal_valid_until,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def reconcile_legacy_collaborator_candidates(self) -> int:
        """Repair pending collaborator versions created before the contribution gate.

        Older builds moved every readable collaborator submission directly to
        PENDING_ACCEPTANCE.  Reclassifying an unreviewed version is safe because
        no human acceptance decision or accepted result exists yet.
        """

        sim_time = self.now()
        repaired = 0
        with self.db.transaction() as cursor:
            rows = cursor.execute(
                "SELECT v.version_id, v.action_item_id, v.submitted_by_actor_id, "
                "a.owner_actor_id, a.current_valid_version_id "
                "FROM artifact_versions v JOIN action_items a "
                "ON a.action_item_id = v.action_item_id "
                "WHERE a.episode_id = ? AND v.validation_status = 'PASSED' "
                "AND v.review_status = 'PENDING' "
                "AND v.submitted_by_actor_id IS NOT NULL "
                "AND v.submitted_by_actor_id <> a.owner_actor_id "
                "AND NOT EXISTS (SELECT 1 FROM audit_events ae "
                "WHERE ae.aggregate_id = v.version_id "
                "AND ae.event_type = 'ArtifactContributionPromotedToFinalCandidate')",
                (self.episode_id,),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    "UPDATE artifact_versions SET review_status = 'NOT_REQUIRED', "
                    "reviewed_by = NULL, reviewed_sim_time = NULL, "
                    "review_comment = NULL WHERE version_id = ? "
                    "AND review_status = 'PENDING'",
                    (row["version_id"],),
                )
                other_candidate = cursor.execute(
                    "SELECT 1 FROM artifact_versions WHERE action_item_id = ? "
                    "AND review_status = 'PENDING' LIMIT 1",
                    (row["action_item_id"],),
                ).fetchone()
                if not other_candidate:
                    restored_status = ActionItemStatus.TRACKING
                    if row["current_valid_version_id"]:
                        included_in_current_final = cursor.execute(
                            "SELECT 1 FROM final_field_lineage l "
                            "JOIN episodes e ON e.current_final_deliverable_id = "
                            "l.final_deliverable_id "
                            "JOIN final_deliverables f ON f.final_deliverable_id = "
                            "l.final_deliverable_id "
                            "WHERE e.episode_id = ? AND l.action_item_id = ? "
                            "AND l.version_id = ? AND f.status <> 'SUPERSEDED' LIMIT 1",
                            (
                                self.episode_id,
                                row["action_item_id"],
                                row["current_valid_version_id"],
                            ),
                        ).fetchone()
                        restored_status = (
                            ActionItemStatus.AGGREGATED
                            if included_in_current_final
                            else ActionItemStatus.ACCEPTED
                        )
                    cursor.execute(
                        "UPDATE action_items SET status = ?, version = version + 1 "
                        "WHERE action_item_id = ? AND status = ?",
                        (
                            restored_status,
                            row["action_item_id"],
                            ActionItemStatus.PENDING_ACCEPTANCE,
                        ),
                    )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=row["version_id"],
                    event_type="ArtifactContributionReclassified",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": row["action_item_id"],
                        "submitted_by_actor_id": row["submitted_by_actor_id"],
                        "reason": "LEGACY_COLLABORATOR_VERSION_WAS_NOT_FINAL_CANDIDATE",
                    },
                    correlation_id=f'corr_contribution_reclassify_{row["version_id"]}',
                )
                repaired += 1
            stale_analyses = cursor.execute(
                "SELECT v.version_id, v.action_item_id, v.processing_metadata "
                "FROM artifact_versions v JOIN action_items a "
                "ON a.action_item_id = v.action_item_id "
                "WHERE a.episode_id = ? AND v.validation_status = 'PASSED' "
                "AND v.review_status = 'NOT_REQUIRED' "
                "AND v.processing_status = 'READY' "
                "AND v.submitted_by_actor_id IS NOT NULL "
                "AND v.submitted_by_actor_id <> a.owner_actor_id "
                "AND EXISTS (SELECT 1 FROM audit_events ae "
                "WHERE ae.aggregate_id = v.version_id "
                "AND ae.event_type = 'ArtifactContributionReclassified')",
                (self.episode_id,),
            ).fetchall()
            for row in stale_analyses:
                metadata = (
                    json.loads(row["processing_metadata"])
                    if isinstance(row["processing_metadata"], str)
                    else dict(row["processing_metadata"] or {})
                )
                if metadata.get("purpose") == "CONTRIBUTION_ANALYSIS":
                    continue
                cursor.execute(
                    "UPDATE artifact_versions SET processing_status = 'PENDING', "
                    "processing_result = NULL, processing_metadata = NULL, "
                    "processing_attempt_count = 0, processing_error_code = NULL, "
                    "processing_error_stage = NULL, processing_error_detail = NULL, "
                    "processing_retryable = FALSE, processing_updated_sim_time = ? "
                    "WHERE version_id = ? AND processing_status = 'READY'",
                    (sim_time, row["version_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=row["version_id"],
                    event_type="ArtifactContributionAnalysisRequeued",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": row["action_item_id"],
                        "reason": "PROCESSING_PURPOSE_CHANGED_TO_CONTRIBUTION_ANALYSIS",
                        "previous_purpose": metadata.get("purpose"),
                    },
                    correlation_id=f'corr_contribution_requeue_{row["version_id"]}',
                )
                repaired += 1
            normalized_manifests = cursor.execute(
                "SELECT v.version_id, v.action_item_id, v.processing_metadata "
                "FROM artifact_versions v JOIN action_items a "
                "ON a.action_item_id = v.action_item_id "
                "WHERE a.episode_id = ? AND v.processing_status = 'READY' "
                "AND v.processing_metadata IS NOT NULL "
                "AND v.submitted_by_actor_id IS NOT NULL "
                "AND v.submitted_by_actor_id <> a.owner_actor_id",
                (self.episode_id,),
            ).fetchall()
            for row in normalized_manifests:
                metadata = (
                    json.loads(row["processing_metadata"])
                    if isinstance(row["processing_metadata"], str)
                    else dict(row["processing_metadata"] or {})
                )
                manifest = dict(metadata.get("context_manifest") or {})
                if (
                    metadata.get("purpose") != "CONTRIBUTION_ANALYSIS"
                    or manifest.get("purpose") == "CONTRIBUTION_ANALYSIS"
                ):
                    continue
                previous_purpose = manifest.get("purpose")
                manifest["purpose"] = "CONTRIBUTION_ANALYSIS"
                metadata["context_manifest"] = manifest
                cursor.execute(
                    "UPDATE artifact_versions SET processing_metadata = ? "
                    "WHERE version_id = ?",
                    (canonical_json(metadata), row["version_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=row["version_id"],
                    event_type="ArtifactContributionContextManifestNormalized",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": row["action_item_id"],
                        "previous_purpose": previous_purpose,
                        "normalized_purpose": "CONTRIBUTION_ANALYSIS",
                    },
                    correlation_id=f'corr_contribution_manifest_{row["version_id"]}',
                )
                repaired += 1
        return repaired

    def decide_contribution(
        self,
        version_id: str,
        *,
        actor_id: str,
        action: str,
        comment: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Let the task owner handle one collaborator contribution in-place."""

        decision = action.strip().upper()
        if decision not in {"INCLUDE", "REQUEST_REVISION", "PROMOTE"}:
            raise ValueError("contribution action must be INCLUDE, REQUEST_REVISION, or PROMOTE")
        comment = comment.strip()
        if decision == "REQUEST_REVISION" and not comment:
            raise ValueError("revision feedback is required")
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        sim_time = self.now()
        with self.db.transaction() as cursor:
            version = cursor.execute(
                "SELECT v.*, a.episode_id, a.owner_actor_id, "
                "a.proposal_metadata, "
                "a.status AS action_status FROM artifact_versions v "
                "JOIN action_items a ON a.action_item_id = v.action_item_id "
                "WHERE v.version_id = ?",
                (version_id,),
            ).fetchone()
            if not version or version["episode_id"] != self.episode_id:
                raise KeyError(version_id)
            if version["owner_actor_id"] != actor_id:
                raise PermissionError("only the task owner may handle a contribution")
            if (
                not version["submitted_by_actor_id"]
                or version["submitted_by_actor_id"] == version["owner_actor_id"]
            ):
                raise ValueError("the selected version is not a collaborator contribution")
            if version["validation_status"] != "PASSED":
                raise ValueError("only a validated contribution may be handled")
            contribution_action = cursor.execute(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (version["action_item_id"],),
            ).fetchone()
            previous_decision = cursor.execute(
                "SELECT event_type, payload FROM audit_events "
                "WHERE aggregate_id = ? AND event_type IN "
                "('ArtifactContributionIncluded',"
                "'ArtifactContributionRevisionRequested',"
                "'ArtifactContributionPromotedToFinalCandidate') "
                "ORDER BY sequence_no DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            if previous_decision:
                prior_status = {
                    "ArtifactContributionIncluded": "INCLUDED",
                    "ArtifactContributionRevisionRequested": "REVISION_REQUESTED",
                    "ArtifactContributionPromotedToFinalCandidate": "PROMOTED",
                }[previous_decision["event_type"]]
                result = {
                    "version_id": version_id,
                    "action_item_id": version["action_item_id"],
                    "contribution_status": prior_status,
                    "review_status": version["review_status"],
                    "action_status": version["action_status"],
                    "idempotent": True,
                }
                self._record_inbound(
                    cursor, message_id=message_id, result=result, sim_time=sim_time
                )
                return result
            if version["review_status"] != "NOT_REQUIRED":
                raise ValueError("the contribution has already entered final review")
            if decision == "REQUEST_REVISION" and not self.task_contributor_role(
                contribution_action,
                version["submitted_by_actor_id"],
                cursor=cursor,
            ):
                raise ValueError(
                    "revision requires an active collaboration; invite the collaborator again"
                )

            event_type = {
                "INCLUDE": "ArtifactContributionIncluded",
                "REQUEST_REVISION": "ArtifactContributionRevisionRequested",
                "PROMOTE": "ArtifactContributionPromotedToFinalCandidate",
            }[decision]
            if decision == "PROMOTE":
                if version["action_status"] not in (
                    ActionItemStatus.TRACKING,
                    ActionItemStatus.ACCEPTED,
                    ActionItemStatus.AGGREGATED,
                ):
                    raise ValueError("the task cannot accept a final candidate in its current state")
                competing = cursor.execute(
                    "SELECT version_id FROM artifact_versions "
                    "WHERE action_item_id = ? AND review_status = 'PENDING' "
                    "AND version_id <> ? LIMIT 1",
                    (version["action_item_id"], version_id),
                ).fetchone()
                if competing:
                    raise ValueError("another artifact version is already pending acceptance")
                next_processing_status = (
                    "PENDING"
                    if version["processing_status"] == "NOT_REQUIRED"
                    else version["processing_status"]
                )
                cursor.execute(
                    "UPDATE artifact_versions SET review_status = 'PENDING', "
                    "processing_status = ?, processing_updated_sim_time = ? "
                    "WHERE version_id = ?",
                    (next_processing_status, sim_time, version_id),
                )
                cursor.execute(
                    "UPDATE action_items SET status = ?, version = version + 1 "
                    "WHERE action_item_id = ?",
                    (ActionItemStatus.PENDING_ACCEPTANCE, version["action_item_id"]),
                )
                contribution_status = "PROMOTED"
                review_status = "PENDING"
                action_status = ActionItemStatus.PENDING_ACCEPTANCE
            else:
                contribution_status = (
                    "INCLUDED" if decision == "INCLUDE" else "REVISION_REQUESTED"
                )
                review_status = "NOT_REQUIRED"
                action_status = version["action_status"]
                if version["processing_status"] in {
                    "PENDING",
                    "RETRY_WAIT",
                    "FAILED",
                }:
                    cursor.execute(
                        "UPDATE artifact_versions SET processing_status = 'NOT_REQUIRED', "
                        "processing_error_code = NULL, processing_error_stage = NULL, "
                        "processing_error_detail = NULL, processing_retryable = FALSE, "
                        "processing_updated_sim_time = ? WHERE version_id = ?",
                        (sim_time, version_id),
                    )

            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "action_item_id": version["action_item_id"],
                    "decided_by": actor_id,
                    "submitted_by_actor_id": version["submitted_by_actor_id"],
                    "comment": comment,
                    "contribution_status": contribution_status,
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "version_id": version_id,
                "action_item_id": version["action_item_id"],
                "contribution_status": contribution_status,
                "review_status": review_status,
                "action_status": action_status,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def retry_task_result_processing(
        self,
        version_id: str,
        *,
        actor_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Requeue a recoverable call or a contract failure after code is fixed."""

        self._require_aggregator(actor_id)
        if not message_id.strip():
            raise ValueError("message_id is required")
        existing = self.db.one(
            "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
            (message_id,),
        )
        if existing:
            return json.loads(existing["processed_result"])
        transient_codes = {
            "NETWORK_TIMEOUT",
            "RATE_LIMIT",
            "PROVIDER_5XX",
            "INVALID_JSON",
            "INVALID_SCHEMA",
        }
        sim_time = self.now()
        with self.db.transaction() as cursor:
            version = cursor.execute(
                "SELECT v.*, a.owner_actor_id AS action_owner_actor_id "
                "FROM artifact_versions v "
                "JOIN action_items a ON a.action_item_id = v.action_item_id "
                "WHERE v.version_id = ? AND a.episode_id = ?",
                (version_id, self.episode_id),
            ).fetchone()
            if not version:
                raise KeyError(version_id)
            is_contribution = bool(
                version["review_status"] == "NOT_REQUIRED"
                and version["submitted_by_actor_id"]
                and version["submitted_by_actor_id"]
                != version["action_owner_actor_id"]
            )
            if is_contribution:
                handled = cursor.execute(
                    "SELECT 1 FROM audit_events WHERE aggregate_id = ? "
                    "AND event_type IN ('ArtifactContributionIncluded',"
                    "'ArtifactContributionRevisionRequested',"
                    "'ArtifactContributionPromotedToFinalCandidate') LIMIT 1",
                    (version_id,),
                ).fetchone()
                if handled:
                    raise ValueError("a handled contribution may not requeue analysis")
            if version["validation_status"] != "PASSED" or (
                version["review_status"] != "PENDING" and not is_contribution
            ):
                raise ValueError(
                    "only a validated final candidate or contribution may retry"
                )
            if version["processing_status"] != "FAILED":
                raise ValueError("only a failed processing attempt may be retried")
            if version["processing_error_code"] not in transient_codes:
                raise ValueError(
                    "only a recoverable provider or repaired contract failure may retry"
                )
            cursor.execute(
                "UPDATE artifact_versions SET processing_status = 'PENDING', "
                "processing_attempt_count = 0, processing_error_code = NULL, "
                "processing_error_stage = NULL, processing_error_detail = NULL, "
                "processing_retryable = TRUE, processing_updated_sim_time = ?, "
                "processing_next_attempt_at = NULL "
                "WHERE version_id = ?",
                (sim_time, version_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version_id,
                event_type="TaskResultProcessingRetryRequested",
                sim_time=sim_time,
                payload={
                    "requested_by": actor_id,
                    "previous_error_code": version["processing_error_code"],
                    "previous_attempt_count": int(
                        version["processing_attempt_count"]
                    ),
                },
                correlation_id=f"corr_{message_id}",
            )
            result = {
                "version_id": version_id,
                "processing_status": "PENDING",
                "retry_requested": True,
            }
            self._record_inbound(
                cursor, message_id=message_id, result=result, sim_time=sim_time
            )
        return result

    def process_task_result_once(
        self,
        *,
        processing_mode: str,
        allow_contribution_analysis: bool = False,
        processor: Callable[
            [dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]
        ]
        | None = None,
    ) -> dict[str, Any] | None:
        """Process one validated task version into a version-bound review package."""
        if processing_mode not in {"bailian", "local"}:
            raise ValueError("task result processing mode must be bailian or local")
        sim_time = self.now()
        review_filter = (
            "(v.review_status = 'PENDING' OR "
            "(v.review_status = 'NOT_REQUIRED' "
            "AND v.submitted_by_actor_id IS NOT NULL "
            "AND v.submitted_by_actor_id <> a.owner_actor_id "
            "AND NOT EXISTS (SELECT 1 FROM audit_events ae "
            "WHERE ae.aggregate_id = v.version_id AND ae.event_type IN "
            "('ArtifactContributionIncluded',"
            "'ArtifactContributionRevisionRequested',"
            "'ArtifactContributionPromotedToFinalCandidate'))))"
            if allow_contribution_analysis
            else "v.review_status = 'PENDING'"
        )
        with self.db.transaction() as cursor:
            version = cursor.execute(
                "SELECT v.* FROM artifact_versions v "
                "JOIN action_items a ON a.action_item_id = v.action_item_id "
                "WHERE a.episode_id = ? AND v.validation_status = 'PASSED' "
                f"AND ({review_filter}) "
                "AND v.processing_status IN ('PENDING','RETRY_WAIT') "
                "AND v.processing_attempt_count < ? "
                "AND (v.processing_next_attempt_at IS NULL "
                "     OR v.processing_next_attempt_at <= ?) "
                "ORDER BY v.received_sequence LIMIT 1",
                (self.episode_id, TASK_RESULT_MAX_ATTEMPTS, wall_clock_now()),
            ).fetchone()
            if not version:
                return None
            claimed = cursor.execute(
                "UPDATE artifact_versions SET processing_status = 'PROCESSING', "
                "processing_attempt_count = processing_attempt_count + 1, "
                "processing_updated_sim_time = ? WHERE version_id = ? "
                "AND processing_status IN ('PENDING','RETRY_WAIT')",
                (sim_time, version["version_id"]),
            )
            if claimed.rowcount != 1:
                return None
            attempt_count = int(version["processing_attempt_count"]) + 1

        version = self.db.one(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (version["version_id"],),
        )
        action = self.db.one(
            "SELECT * FROM action_items WHERE action_item_id = ?",
            (version["action_item_id"],),
        )
        metadata = self.proposal_metadata(action)
        payload = (
            json.loads(version["payload"])
            if isinstance(version["payload"], str)
            else dict(version["payload"])
        )
        attachments = (
            json.loads(version["attachment_extractions"])
            if isinstance(version["attachment_extractions"], str)
            else list(version["attachment_extractions"] or [])
        )
        version_source_manifest = (
            json.loads(version["source_manifest"])
            if isinstance(version["source_manifest"], str)
            else dict(version["source_manifest"] or {})
        )
        submission_manifest = dict(
            version_source_manifest.get("submission") or {}
        )
        is_contribution_analysis = bool(
            version["review_status"] == "NOT_REQUIRED"
            and version["submitted_by_actor_id"]
            and version["submitted_by_actor_id"] != action["owner_actor_id"]
        )
        processing_purpose = (
            "CONTRIBUTION_ANALYSIS"
            if is_contribution_analysis
            else "TASK_RESULT_REVIEW"
        )
        previous_versions = [
            {
                "version_id": row["version_id"],
                "validation_status": row["validation_status"],
                "review_status": row["review_status"],
                "review_comment": row["review_comment"],
            }
            for row in self.db.all(
                "SELECT version_id, validation_status, review_status, review_comment "
                "FROM artifact_versions WHERE action_item_id = ? "
                "AND received_sequence < ? ORDER BY received_sequence",
                (version["action_item_id"], version["received_sequence"]),
            )
        ]
        from .task_result_processing import (
            BailianTaskResultProcessor,
            TASK_RESULT_PROMPT_VERSION,
            TaskResultProcessingError,
            build_deterministic_task_result,
            build_task_result_context,
        )
        from .context_budget import ContextBudgetError, bound_task_result_context
        from .task_result_processing import task_result_prompt_overhead_tokens

        raw_context = build_task_result_context(
            action_item_id=action["action_item_id"],
            title=action["title"],
            deliverable=metadata.get("deliverable", action["deliverable_key"]),
            acceptance_criteria=metadata.get("acceptance_criteria", ""),
            source_timestamp=metadata.get("source_timestamp", ""),
            source_quote=metadata.get("source_quote", action["source_span"]),
            version_id=version["version_id"],
            payload=payload,
            attachments=attachments,
            previous_versions=previous_versions,
            work_requirements=metadata.get("work_requirements", ""),
            management_review_policy=metadata.get(
                "management_review_policy", ""
            ),
            submitted_by_actor_id=(
                version["submitted_by_actor_id"]
                or submission_manifest.get("submitted_by_actor_id")
            ),
            contributor_role=submission_manifest.get("contributor_role"),
            processing_purpose=processing_purpose,
            collaboration_inputs=self.collaboration_input_context(
                action["action_item_id"]
            ),
        )
        try:
            context, context_manifest = bound_task_result_context(
                raw_context,
                reserved_tokens=task_result_prompt_overhead_tokens(),
            )
        except ContextBudgetError as error:
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE artifact_versions SET processing_status = 'FAILED', "
                    "processing_error_code = 'CONTEXT_BUDGET_EXCEEDED', "
                    "processing_error_stage = 'CONTEXT_ASSEMBLY', "
                    "processing_error_detail = ?, processing_retryable = FALSE, "
                    "processing_updated_sim_time = ? WHERE version_id = ?",
                    (str(error)[:1000], sim_time, version["version_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=version["version_id"],
                    event_type="TaskResultProcessingFailed",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": action["action_item_id"],
                        "error_code": "CONTEXT_BUDGET_EXCEEDED",
                        "stage": "CONTEXT_ASSEMBLY",
                        "retryable": False,
                        "attempt_count": attempt_count,
                    },
                    correlation_id=f'corr_task_result_{version["version_id"]}',
                )
            return {
                "version_id": version["version_id"],
                "action_item_id": action["action_item_id"],
                "processing_status": "FAILED",
                "error_code": "CONTEXT_BUDGET_EXCEEDED",
                "stage": "CONTEXT_ASSEMBLY",
                "retryable": False,
            }
        context_input_hash = stable_hash(context)
        invocation_base = self._system_invocation_manifest(
            capability_type=(
                "MODEL" if processing_mode == "bailian" else "DETERMINISTIC"
            ),
            purpose=processing_purpose,
            field_allowlist=self.TASK_RESULT_CONTEXT_FIELDS,
            entity_versions={
                "action_item_id": action["action_item_id"],
                "action_item_version": int(action["version"]),
                "artifact_version_id": version["version_id"],
                "artifact_received_sequence": int(version["received_sequence"]),
            },
            prompt_version=TASK_RESULT_PROMPT_VERSION,
            input_hash=context_input_hash,
            output_status="STARTED",
            context_manifest=context_manifest,
        )
        with self.db.transaction() as cursor:
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ArtifactVersion",
                aggregate_id=version["version_id"],
                event_type="TaskResultProcessingStarted",
                sim_time=sim_time,
                payload={
                    "action_item_id": action["action_item_id"],
                    "attempt_count": attempt_count,
                    "invocation": invocation_base,
                },
                correlation_id=f'corr_task_result_{version["version_id"]}',
            )
        try:
            if processor is not None:
                result, processing_metadata = processor(context)
            elif processing_mode == "bailian":
                result, processing_metadata = BailianTaskResultProcessor().process(
                    context
                )
            else:
                result, processing_metadata = build_deterministic_task_result(context)
            processing_metadata = dict(processing_metadata or {})
            processing_metadata["prompt_version"] = TASK_RESULT_PROMPT_VERSION
            processing_metadata["input_hash"] = context_input_hash
            processing_metadata["context_manifest"] = context_manifest
            processing_metadata["purpose"] = processing_purpose
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE artifact_versions SET processing_status = 'READY', "
                    "processing_result = ?, processing_metadata = ?, "
                    "processing_error_code = NULL, processing_error_stage = NULL, "
                    "processing_error_detail = NULL, processing_retryable = FALSE, "
                    "processing_updated_sim_time = ? WHERE version_id = ?",
                    (
                        canonical_json(result),
                        canonical_json(processing_metadata),
                        sim_time,
                        version["version_id"],
                    ),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=version["version_id"],
                    event_type="TaskResultProcessingSucceeded",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": action["action_item_id"],
                        "processing_mode": processing_metadata.get("mode"),
                        "invocation": {
                            **invocation_base,
                            "output_status": "SUCCEEDED",
                            "provider": processing_metadata.get("provider"),
                            "model": processing_metadata.get("model"),
                            "usage": processing_metadata.get("usage") or {},
                        },
                    },
                    correlation_id=f'corr_task_result_{version["version_id"]}',
                )
                self._notify_result_pending_review(
                    cursor,
                    version=version,
                    action=action,
                    processing_label="AI 辅助包已就绪",
                    sim_time=sim_time,
                )
            return {
                "version_id": version["version_id"],
                "action_item_id": action["action_item_id"],
                "processing_status": "READY",
                "task_alignment": result["task_alignment"],
                "acceptance_advice": result["acceptance_advice"],
                "processing_purpose": processing_purpose,
            }
        except TaskResultProcessingError as error:
            retryable = bool(
                error.retryable and attempt_count < TASK_RESULT_MAX_ATTEMPTS
            )
            status = "RETRY_WAIT" if retryable else "FAILED"
            next_attempt_at = (
                task_result_retry_deadline(attempt_count) if retryable else None
            )
            notify_exhausted = not retryable
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE artifact_versions SET processing_status = ?, "
                    "processing_error_code = ?, processing_error_stage = ?, "
                    "processing_error_detail = ?, processing_retryable = ?, "
                    "processing_updated_sim_time = ?, "
                    "processing_next_attempt_at = ? WHERE version_id = ?",
                    (
                        status,
                        error.error_code,
                        error.stage,
                        str(error)[:1000],
                        retryable,
                        sim_time,
                        next_attempt_at,
                        version["version_id"],
                    ),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=version["version_id"],
                    event_type="TaskResultProcessingFailed",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": action["action_item_id"],
                        "error_code": error.error_code,
                        "stage": error.stage,
                        "retryable": retryable,
                        "attempt_count": attempt_count,
                        "invocation": {
                            **invocation_base,
                            "output_status": status,
                            "error_code": error.error_code,
                            "error_stage": error.stage,
                        },
                    },
                    correlation_id=f'corr_task_result_{version["version_id"]}',
                )
                if notify_exhausted:
                    # A submission stuck without an assist package is exactly
                    # when the coordinator most needs to know it is waiting;
                    # acceptance is still allowed, just unaided.
                    self._notify_result_pending_review(
                        cursor,
                        version=version,
                        action=action,
                        processing_label=f"AI 处理失败（{error.error_code}），需人工判断",
                        sim_time=sim_time,
                    )
            return {
                "version_id": version["version_id"],
                "action_item_id": action["action_item_id"],
                "processing_status": status,
                "error_code": error.error_code,
                "stage": error.stage,
                "retryable": retryable,
            }
        except Exception as error:
            # No adapter/programming defect may strand a task version in the
            # transient PROCESSING state.  Unknown failures are non-retryable
            # until their cause is classified explicitly.
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE artifact_versions SET processing_status = 'FAILED', "
                    "processing_error_code = 'UNEXPECTED_PROCESSING_ERROR', "
                    "processing_error_stage = 'TASK_RESULT_PROCESSING', "
                    "processing_error_detail = ?, processing_retryable = FALSE, "
                    "processing_updated_sim_time = ? WHERE version_id = ?",
                    (str(error)[:1000], sim_time, version["version_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=version["version_id"],
                    event_type="TaskResultProcessingFailed",
                    sim_time=sim_time,
                    payload={
                        "action_item_id": action["action_item_id"],
                        "error_code": "UNEXPECTED_PROCESSING_ERROR",
                        "stage": "TASK_RESULT_PROCESSING",
                        "retryable": False,
                        "attempt_count": attempt_count,
                        "invocation": {
                            **invocation_base,
                            "output_status": "FAILED",
                            "error_code": "UNEXPECTED_PROCESSING_ERROR",
                            "error_stage": "TASK_RESULT_PROCESSING",
                        },
                    },
                    correlation_id=f'corr_task_result_{version["version_id"]}',
                )
            return {
                "version_id": version["version_id"],
                "action_item_id": action["action_item_id"],
                "processing_status": "FAILED",
                "error_code": "UNEXPECTED_PROCESSING_ERROR",
                "stage": "TASK_RESULT_PROCESSING",
                "retryable": False,
            }

    def _supersede_pending_final(
        self, cursor: Any, action_item_id: str, sim_time: str, correlation_id: str
    ) -> None:
        episode = cursor.execute(
            "SELECT current_final_deliverable_id FROM episodes WHERE episode_id = ?",
            (self.episode_id,),
        ).fetchone()
        final_id = episode["current_final_deliverable_id"]
        if not final_id:
            return
        final = cursor.execute(
            "SELECT * FROM final_deliverables WHERE final_deliverable_id = ?",
            (final_id,),
        ).fetchone()
        if final["status"] in ("RELEASED", "SUPERSEDED"):
            return
        cursor.execute(
            "UPDATE final_deliverables SET status = 'SUPERSEDED' "
            "WHERE final_deliverable_id = ?",
            (final_id,),
        )
        cursor.execute(
            "UPDATE approvals SET status = 'SUPERSEDED' "
            "WHERE approval_type = 'FINAL_RELEASE' AND status = 'PENDING' "
            "AND json_extract(requested_action, '$.final_deliverable_id') = ?",
            (final_id,),
        )
        cursor.execute(
            "UPDATE action_items SET status = 'ACCEPTED' "
            "WHERE episode_id = ? AND status = 'AGGREGATED'",
            (self.episode_id,),
        )
        self.db.append_audit(
            cursor,
            run_id=self.run_id,
            aggregate_type="FinalDeliverable",
            aggregate_id=final_id,
            event_type="FinalDeliverableSuperseded",
            sim_time=sim_time,
            payload={"changed_action_item_id": action_item_id},
            correlation_id=correlation_id,
        )

    def evaluate_policy(self) -> list[dict[str, Any]]:
        now = parse_time(self.now())
        policy = self.fixture["policy"]
        check_in_lead = policy.get(
            "check_in_lead_hours", policy.get("l1_lead_hours", 24)
        )
        silence_window = policy.get(
            "silence_window_hours", policy.get("progress_window_hours", 24)
        )
        inquiry_cooldown = policy.get(
            "inquiry_cooldown_hours", policy.get("l2_wait_hours", 12)
        )
        l2_wait = policy.get("l2_wait_hours", 12)
        actions = self.db.all(
            "SELECT * FROM action_items WHERE episode_id = ? ORDER BY action_item_id",
            (self.episode_id,),
        )
        decisions: list[dict[str, Any]] = []
        for action in actions:
            if action["status"] not in (
                ActionItemStatus.PENDING_CONFIRMATION,
                ActionItemStatus.TRACKING,
            ):
                continue
            if action["status"] == ActionItemStatus.PENDING_CONFIRMATION:
                if not action["owner_actor_id"]:
                    continue
                due = parse_time(action["created_sim_time"]) + timedelta(
                    hours=policy["confirmation_timeout_hours"]
                )
                if now >= due:
                    decision = self._plan_intervention(
                        action=dict(action),
                        level="L1",
                        action_type="CONFIRMATION_REMINDER",
                        reason_code="CONFIRMATION_TIMEOUT",
                        trigger_key=f"confirmation:{iso_time(due)}",
                        target_actor_id=action["owner_actor_id"],
                    )
                    if decision:
                        decisions.append(decision)
                continue

            if action["current_valid_version_id"]:
                continue
            latest_version = self.db.one(
                """
                SELECT * FROM artifact_versions WHERE action_item_id = ?
                ORDER BY received_sequence DESC LIMIT 1
                """,
                (action["action_item_id"],),
            )
            if latest_version and latest_version["review_status"] == "PENDING":
                continue
            if latest_version and latest_version["validation_status"] == "FAILED":
                decision = self._plan_intervention(
                    action=dict(action),
                    level="L1",
                    action_type="REWORK_REQUEST",
                    reason_code="LATEST_VERSION_INVALID",
                    trigger_key=f'rework:{latest_version["version_id"]}',
                    target_actor_id=action["owner_actor_id"],
                )
                if decision:
                    decisions.append(decision)
                continue

            commitment = self.db.one(
                "SELECT * FROM commitment_revisions WHERE commitment_revision_id = ?",
                (action["active_commitment_revision_id"],),
            )
            if not commitment:
                continue
            deadline = parse_time(commitment["promised_deadline_sim_time"])
            check_in_due = deadline - timedelta(hours=check_in_lead)
            if now < check_in_due:
                continue
            valid_until = action["last_owner_signal_valid_until"]
            if valid_until and now < parse_time(valid_until):
                continue
            last_signal_raw = (
                action["last_owner_signal_at"]
                or action["confirmed_sim_time"]
                or action["created_sim_time"]
            )
            last_signal_at = parse_time(last_signal_raw)
            if now < last_signal_at + timedelta(hours=silence_window):
                continue
            unresolved_assistance = self.db.one(
                "SELECT assistance_request_id FROM assistance_requests "
                "WHERE action_item_id = ? AND status IN ('OPEN','ACKNOWLEDGED') "
                "LIMIT 1",
                (action["action_item_id"],),
            )
            if unresolved_assistance:
                self._record_inquiry_suppression(
                    action_item_id=action["action_item_id"],
                    reason="UNRESOLVED_ASSISTANCE",
                    trigger_key=(
                        f'assistance:{unresolved_assistance["assistance_request_id"]}:'
                        f'{commitment["commitment_revision_id"]}'
                    ),
                )
                continue
            previous_l1 = self.db.one(
                """
                SELECT * FROM interventions
                WHERE action_item_id = ? AND level = 'L1'
                  AND action_type = 'PROGRESS_INQUIRY'
                  AND status NOT IN ('SUPERSEDED','FAILED')
                ORDER BY created_sim_time DESC LIMIT 1
                """,
                (action["action_item_id"],),
            )
            previous_l2 = self.db.one(
                """
                SELECT * FROM interventions
                WHERE action_item_id = ? AND level = 'L2'
                  AND action_type = 'PROGRESS_INQUIRY'
                  AND status NOT IN ('SUPERSEDED','FAILED')
                ORDER BY created_sim_time DESC LIMIT 1
                """,
                (action["action_item_id"],),
            )
            signal_after_l1 = bool(
                previous_l1
                and last_signal_at > parse_time(previous_l1["created_sim_time"])
            )
            if not previous_l1 or (
                signal_after_l1
                and now
                >= parse_time(previous_l1["created_sim_time"])
                + timedelta(hours=inquiry_cooldown)
            ):
                decision = self._plan_intervention(
                    action=dict(action),
                    level="L1",
                    action_type="PROGRESS_INQUIRY",
                    reason_code=(
                        "NO_SIGNAL_BEFORE_DEADLINE"
                        if now <= deadline
                        else "OVERDUE_NO_SIGNAL"
                    ),
                    trigger_key=(
                        f'l1:{last_signal_raw}:{commitment["commitment_revision_id"]}'
                    ),
                    target_actor_id=action["owner_actor_id"],
                )
            elif (
                signal_after_l1
                and now
                < parse_time(previous_l1["created_sim_time"])
                + timedelta(hours=inquiry_cooldown)
            ):
                self._record_inquiry_suppression(
                    action_item_id=action["action_item_id"],
                    reason="INQUIRY_COOLDOWN",
                    trigger_key=(
                        f'cooldown:{previous_l1["intervention_id"]}:'
                        f'{last_signal_raw}'
                    ),
                )
                decision = None
            elif (
                now > deadline
                and previous_l1["status"] == "DELIVERED"
                and not signal_after_l1
                and not previous_l2
                and now
                >= parse_time(previous_l1["created_sim_time"])
                + timedelta(hours=l2_wait)
            ):
                decision = self._plan_intervention(
                    action=dict(action),
                    level="L2",
                    action_type="PROGRESS_INQUIRY",
                    reason_code="OVERDUE_WITHOUT_RESPONSE_AFTER_L1",
                    trigger_key=(
                        f'l2:{deadline.isoformat()}:{commitment["commitment_revision_id"]}'
                    ),
                    target_actor_id=action["owner_actor_id"],
                )
            else:
                decision = None
            if decision:
                decisions.append(decision)
        return decisions

    def _record_inquiry_suppression(
        self, *, action_item_id: str, reason: str, trigger_key: str
    ) -> None:
        if reason not in {"UNRESOLVED_ASSISTANCE", "INQUIRY_COOLDOWN"}:
            raise ValueError("unsupported inquiry suppression reason")
        correlation_id = (
            "corr_inquiry_suppression_"
            + stable_hash([self.episode_id, action_item_id, reason, trigger_key])[:24]
        )
        if self.db.one(
            "SELECT 1 FROM audit_events WHERE run_id = ? AND correlation_id = ? "
            "AND event_type = 'ProgressInquirySuppressed'",
            (self.run_id, correlation_id),
        ):
            return
        sim_time = self.now()
        with self.db.transaction() as cursor:
            if cursor.execute(
                "SELECT 1 FROM audit_events WHERE run_id = ? AND correlation_id = ? "
                "AND event_type = 'ProgressInquirySuppressed'",
                (self.run_id, correlation_id),
            ).fetchone():
                return
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="ActionItem",
                aggregate_id=action_item_id,
                event_type="ProgressInquirySuppressed",
                sim_time=sim_time,
                payload={"reason": reason, "trigger_key": trigger_key},
                correlation_id=correlation_id,
            )

    def _plan_intervention(
        self,
        *,
        action: dict[str, Any],
        level: str,
        action_type: str,
        reason_code: str,
        trigger_key: str,
        target_actor_id: str,
    ) -> dict[str, Any] | None:
        if level not in ("L1", "L2"):
            raise ValueError("P0 supports only L1 and L2 interventions")
        sim_time = self.now()
        effect = effect_id(
            episode_id=self.episode_id,
            subject_id=action["action_item_id"],
            effect_type=action_type,
            trigger_key=trigger_key,
        )
        existing = self.db.one(
            "SELECT * FROM interventions WHERE effect_id = ?", (effect,)
        )
        if existing:
            return None
        correlation_id = f"corr_{effect}"
        if level in ("L1", "L2"):
            day_prefix = sim_time[:10]
            touch_count = self.db.one(
                """
                SELECT COUNT(*) AS count FROM interventions
                WHERE target_actor_id = ? AND level IN ('L1','L2')
                  AND substr(created_sim_time, 1, 10) = ?
                  AND status NOT IN ('SUPERSEDED','FAILED')
                """,
                (target_actor_id, day_prefix),
            )["count"]
            if touch_count >= self.fixture["policy"]["daily_touch_budget"]:
                with self.db.transaction() as cursor:
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ActionItem",
                        aggregate_id=action["action_item_id"],
                        event_type="InterventionSuppressed",
                        sim_time=sim_time,
                        payload={
                            "level": level,
                            "reason_code": reason_code,
                            "suppression_reason": "DAILY_TOUCH_BUDGET",
                            "candidate_effect_id": effect,
                        },
                        correlation_id=correlation_id,
                    )
                return {"suppressed": True, "level": level, "effect_id": effect}

        intervention_id = f"int_{uuid4().hex}"
        status = "PLANNED"
        social_cost = {"L1": 2, "L2": 5}[level]
        content = {
            "L1": (
                f'任务“{action["title"]}”暂时没有新的业务信号。'
                "请快捷选择：按计划 / 有风险 / 被阻塞 / 等待输入 / 准备提交；"
                "需要时可补一句说明。"
            ),
            "L2": (
                f'任务“{action["title"]}”已逾期且上次询问后仍无业务信号。'
                "请更新快捷状态、发起参会者求助，或提交成果。"
            ),
        }[level]
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO interventions(
                    intervention_id, episode_id, action_item_id, target_actor_id,
                    level, action_type, reason_code, social_cost, status,
                    effect_id, correlation_id, created_sim_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intervention_id,
                    self.episode_id,
                    action["action_item_id"],
                    target_actor_id,
                    level,
                    action_type,
                    reason_code,
                    social_cost,
                    status,
                    effect,
                    correlation_id,
                    sim_time,
                ),
            )
            outbox_id = self._create_outbox(
                cursor,
                action_item_id=action["action_item_id"],
                effect_type=action_type,
                effect=effect,
                recipient_actor_ids=[target_actor_id],
                content=content,
                correlation_id=correlation_id,
                sim_time=sim_time,
            )
            cursor.execute(
                "UPDATE interventions SET outbox_id = ? WHERE intervention_id = ?",
                (outbox_id, intervention_id),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Intervention",
                aggregate_id=intervention_id,
                event_type="InterventionDecisionCreated",
                sim_time=sim_time,
                payload={
                    "level": level,
                    "reason_code": reason_code,
                    "effect_id": effect,
                },
                correlation_id=correlation_id,
            )
            if action_type == "PROGRESS_INQUIRY":
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="ActionItem",
                    aggregate_id=action["action_item_id"],
                    event_type="ProgressInquiryPlanned",
                    sim_time=sim_time,
                    payload={
                        "intervention_id": intervention_id,
                        "level": level,
                        "reason_code": reason_code,
                        "target_actor_id": target_actor_id,
                    },
                    correlation_id=correlation_id,
                )
        return {
            "intervention_id": intervention_id,
            "level": level,
            "effect_id": effect,
            "outbox_id": outbox_id,
        }

    def decide_approval(
        self,
        approval_id: str,
        *,
        actor_id: str,
        approve: bool,
        comment: str = "",
    ) -> dict[str, Any]:
        sim_time = self.now()
        with self.db.transaction() as cursor:
            approval = cursor.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not approval:
                raise KeyError(approval_id)
            episode = cursor.execute(
                "SELECT * FROM episodes WHERE episode_id = ?",
                (approval["episode_id"],),
            ).fetchone()
            if episode["owner_actor_id"] != actor_id:
                raise PermissionError("only the aggregator may decide approvals")
            if approval["approval_type"] != "FINAL_RELEASE":
                raise ValueError("P0 supports only FINAL_RELEASE approvals")
            comment = str(comment or "").strip()
            if not approve and not comment:
                raise ValueError("final release rejection requires feedback")
            if approval["status"] != ApprovalStatus.PENDING:
                approved_statuses = {
                    ApprovalStatus.APPROVED,
                    ApprovalStatus.EXECUTED,
                }
                if approval["status"] in approved_statuses:
                    if approve:
                        return dict(approval)
                    raise ValueError("approval already decided with opposite outcome")
                if approval["status"] == ApprovalStatus.REJECTED:
                    if not approve:
                        return dict(approval)
                    raise ValueError("approval already decided with opposite outcome")
                raise ValueError("approval is no longer pending")
            event_type = "ApprovalApproved" if approve else "ApprovalRejected"
            if not approve:
                cursor.execute(
                    """
                    UPDATE approvals SET status = 'REJECTED', decided_by = ?,
                        decided_sim_time = ?, version = version + 1
                    WHERE approval_id = ?
                    """,
                    (actor_id, sim_time, approval_id),
                )
                cursor.execute(
                    "UPDATE interventions SET status = 'SUPERSEDED' WHERE effect_id = ?",
                    (approval["effect_id"],),
                )
            else:
                requested = json.loads(approval["requested_action"])
                if approval["approval_type"] == "DEADLINE_CHANGE":
                    current = cursor.execute(
                        "SELECT * FROM commitment_revisions WHERE action_item_id = ? "
                        "AND status = 'ACTIVE'",
                        (approval["action_item_id"],),
                    ).fetchone()
                    if not current:
                        raise ValueError("deadline change requires an active commitment")
                    cursor.execute(
                        "UPDATE commitment_revisions SET status = 'SUPERSEDED' "
                        "WHERE commitment_revision_id = ?",
                        (current["commitment_revision_id"],),
                    )
                    revision_id = f"com_{uuid4().hex}"
                    cursor.execute(
                        """
                        INSERT INTO commitment_revisions(
                            commitment_revision_id, action_item_id, revision_no,
                            owner_actor_id, deliverable_key, promised_deadline_sim_time,
                            source_message_id, source_span, status, created_sim_time,
                            supersedes_revision_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'approval.requested_action',
                                  'ACTIVE', ?, ?)
                        """,
                        (
                            revision_id,
                            approval["action_item_id"],
                            int(current["revision_no"]) + 1,
                            current["owner_actor_id"],
                            current["deliverable_key"],
                            requested["proposed_deadline_sim_time"],
                            approval_id,
                            sim_time,
                            current["commitment_revision_id"],
                        ),
                    )
                    cursor.execute(
                        "UPDATE action_items SET active_commitment_revision_id = ?, "
                        "deadline_sim_time = ?, version = version + 1 "
                        "WHERE action_item_id = ?",
                        (
                            revision_id,
                            requested["proposed_deadline_sim_time"],
                            approval["action_item_id"],
                        ),
                    )
                    cursor.execute(
                        "UPDATE approvals SET status = 'EXECUTED', decided_by = ?, "
                        "decided_sim_time = ?, version = version + 1 "
                        "WHERE approval_id = ?",
                        (actor_id, sim_time, approval_id),
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="ActionItem",
                        aggregate_id=approval["action_item_id"],
                        event_type="DeadlineChangeApproved",
                        sim_time=sim_time,
                        payload={
                            "commitment_revision_id": revision_id,
                            "promised_deadline_sim_time": requested[
                                "proposed_deadline_sim_time"
                            ],
                            "approved_by": actor_id,
                        },
                        correlation_id=approval["correlation_id"],
                    )
                else:
                    final_id = requested.get("final_deliverable_id")
                    if (
                        approval["approval_type"] == "FINAL_RELEASE"
                        and final_id != episode["current_final_deliverable_id"]
                    ):
                        raise ValueError("cannot release a stale final deliverable")
                    if approval["approval_type"] == "FINAL_RELEASE":
                        final_row = cursor.execute(
                            "SELECT status, payload FROM final_deliverables "
                            "WHERE final_deliverable_id = ?",
                            (final_id,),
                        ).fetchone()
                        if not final_row or final_row["status"] != "PENDING_APPROVAL":
                            raise ValueError(
                                "final deliverable is not pending approval"
                            )
                        expected_rows = cursor.execute(
                            "SELECT a.action_item_id, a.status, "
                            "a.current_valid_version_id, "
                            "r.accepted_task_result_id FROM action_items a "
                            "LEFT JOIN accepted_task_results r "
                            "ON r.action_item_id = a.action_item_id "
                            "AND r.accepted_version_id = "
                            "a.current_valid_version_id "
                            "WHERE a.episode_id = ? AND a.required = TRUE",
                            (self.episode_id,),
                        ).fetchall()
                        expected = {
                            row["action_item_id"]: (
                                row["current_valid_version_id"],
                                row["accepted_task_result_id"],
                            )
                            for row in expected_rows
                        }
                        if not expected or any(
                            row["status"] != ActionItemStatus.AGGREGATED
                            or not row["current_valid_version_id"]
                            or not row["accepted_task_result_id"]
                            for row in expected_rows
                        ):
                            raise ValueError(
                                "final release inputs are no longer complete"
                            )
                        final_payload = (
                            json.loads(final_row["payload"])
                            if isinstance(final_row["payload"], str)
                            else final_row["payload"]
                        )
                        sections = (
                            final_payload.get("organized_report", {}).get(
                                "sections", []
                            )
                        )
                        actual = {
                            section.get("action_item_id"): (
                                section.get("source_version_id"),
                                section.get("accepted_task_result_id"),
                            )
                            for section in sections
                            if isinstance(section, dict)
                        }
                        if actual != expected:
                            raise ValueError(
                                "final release lineage is stale or incomplete"
                            )
                        lineage_versions = {
                            row["version_id"]
                            for row in cursor.execute(
                                "SELECT DISTINCT version_id FROM "
                                "final_field_lineage WHERE "
                                "final_deliverable_id = ?",
                                (final_id,),
                            ).fetchall()
                        }
                        if lineage_versions != {
                            version_id for version_id, _ in expected.values()
                        }:
                            raise ValueError(
                                "final release field lineage is incomplete"
                            )
                    recipient = requested.get(
                        "target_actor_id", self.aggregator_actor_id
                    )
                    outbox_id = self._create_outbox(
                        cursor,
                        action_item_id=approval["action_item_id"],
                        effect_type=requested["effect_type"],
                        effect=approval["effect_id"],
                        recipient_actor_ids=[recipient],
                        content=requested["content"],
                        correlation_id=approval["correlation_id"],
                        sim_time=sim_time,
                    )
                    cursor.execute(
                        """
                        UPDATE approvals SET status = 'APPROVED', decided_by = ?,
                            decided_sim_time = ?, execution_outbox_id = ?,
                            version = version + 1 WHERE approval_id = ?
                        """,
                        (actor_id, sim_time, outbox_id, approval_id),
                    )
                    cursor.execute(
                        "UPDATE interventions SET status = 'PLANNED', outbox_id = ? "
                        "WHERE effect_id = ?",
                        (outbox_id, approval["effect_id"]),
                    )
                    if approval["approval_type"] == "FINAL_RELEASE":
                        cursor.execute(
                            "UPDATE final_deliverables SET status = 'APPROVED', "
                            "approved_sim_time = ? WHERE final_deliverable_id = ?",
                            (sim_time, requested["final_deliverable_id"]),
                        )
                        cursor.execute(
                            "UPDATE episodes SET status = 'APPROVED', "
                            "version = version + 1 WHERE episode_id = ?",
                            (self.episode_id,),
                        )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Approval",
                aggregate_id=approval_id,
                event_type=event_type,
                sim_time=sim_time,
                payload={"decided_by": actor_id, "comment": comment},
                correlation_id=approval["correlation_id"],
            )
        return dict(
            self.db.one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        )

    def aggregation_source_bundle(self) -> list[dict[str, Any]]:
        actions = self.db.all(
            "SELECT * FROM action_items WHERE episode_id = ? AND required = TRUE",
            (self.episode_id,),
        )
        incomplete = [
            row["action_item_id"]
            for row in actions
            if row["status"] not in (ActionItemStatus.ACCEPTED, ActionItemStatus.AGGREGATED)
            or not row["current_valid_version_id"]
            or not self.db.one(
                "SELECT 1 FROM accepted_task_results WHERE action_item_id = ? "
                "AND accepted_version_id = ?",
                (row["action_item_id"], row["current_valid_version_id"]),
            )
        ]
        if incomplete:
            raise ValueError(f"cannot aggregate incomplete action items: {incomplete}")

        def sort_key(action: Any) -> tuple[str, str]:
            raw_metadata = action["proposal_metadata"] or "{}"
            metadata = (
                json.loads(raw_metadata)
                if isinstance(raw_metadata, str)
                else raw_metadata
            )
            return (
                str(metadata.get("source_timestamp") or action["created_sim_time"]),
                action["action_item_id"],
            )

        bundle: list[dict[str, Any]] = []
        for action in sorted(actions, key=sort_key):
            version = self.db.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?",
                (action["current_valid_version_id"],),
            )
            accepted_result = self.db.one(
                "SELECT * FROM accepted_task_results WHERE action_item_id = ? "
                "AND accepted_version_id = ?",
                (action["action_item_id"], version["version_id"]),
            )
            data = (
                json.loads(version["payload"])
                if isinstance(version["payload"], str)
                else dict(version["payload"])
            )
            attachments = (
                json.loads(version["attachment_extractions"])
                if isinstance(version["attachment_extractions"], str)
                else list(version["attachment_extractions"] or [])
            )
            safe_payload = dict(data)
            raw_metadata = action["proposal_metadata"] or "{}"
            metadata = (
                json.loads(raw_metadata)
                if isinstance(raw_metadata, str)
                else raw_metadata
            )
            owner = self.db.one(
                "SELECT display_name FROM actors WHERE actor_id = ?",
                (action["owner_actor_id"],),
            )
            bundle.append(
                {
                    "action_item_id": action["action_item_id"],
                    "title": action["title"],
                    "owner": owner["display_name"] if owner else action["owner_actor_id"],
                    "deliverable": metadata.get("deliverable", ""),
                    "acceptance_criteria": metadata.get("acceptance_criteria", ""),
                    "deadline": action["deadline_sim_time"],
                    "version_id": version["version_id"],
                    "payload": safe_payload,
                    "attachments": attachments,
                    "accepted_task_result": {
                        "accepted_task_result_id": accepted_result[
                            "accepted_task_result_id"
                        ],
                        "accepted_version_id": accepted_result[
                            "accepted_version_id"
                        ],
                        "completed_content_refs": (
                            json.loads(accepted_result["completed_content_refs"])
                            if isinstance(
                                accepted_result["completed_content_refs"], str
                            )
                            else accepted_result["completed_content_refs"]
                        ),
                        "completion_report": accepted_result["completion_report"],
                        "normalized_result": (
                            json.loads(accepted_result["normalized_result"])
                            if isinstance(accepted_result["normalized_result"], str)
                            else accepted_result["normalized_result"]
                        ),
                        "source_manifest": (
                            json.loads(accepted_result["source_manifest"])
                            if isinstance(accepted_result["source_manifest"], str)
                            else accepted_result["source_manifest"]
                        ),
                        "accepted_by": accepted_result["accepted_by"],
                        "accepted_sim_time": accepted_result[
                            "accepted_sim_time"
                        ],
                    },
                }
            )
        return bundle

    def _final_input_fingerprint(self) -> list[dict[str, str]]:
        return [
            {
                "action_item_id": row["action_item_id"],
                "version_id": row["current_valid_version_id"],
                "accepted_task_result_id": row["accepted_task_result_id"],
            }
            for row in self.db.all(
                "SELECT a.action_item_id, a.current_valid_version_id, "
                "r.accepted_task_result_id FROM action_items a "
                "JOIN accepted_task_results r ON r.action_item_id = a.action_item_id "
                "AND r.accepted_version_id = a.current_valid_version_id "
                "WHERE a.episode_id = ? AND a.required = TRUE "
                "ORDER BY a.action_item_id",
                (self.episode_id,),
            )
        ]

    def queue_final_organization(self, *, processing_mode: str) -> str | None:
        if processing_mode not in {"bailian", "local", "disabled"}:
            raise ValueError("invalid result processing mode")
        if processing_mode == "disabled":
            return None
        episode = self.db.one(
            "SELECT status, current_final_deliverable_id FROM episodes "
            "WHERE episode_id = ?",
            (self.episode_id,),
        )
        if not episode or episode["status"] == EpisodeStatus.ARCHIVED:
            return None
        actions = self.db.all(
            "SELECT a.action_item_id, a.status, a.current_valid_version_id, "
            "r.accepted_task_result_id FROM action_items a "
            "LEFT JOIN accepted_task_results r ON r.action_item_id = a.action_item_id "
            "AND r.accepted_version_id = a.current_valid_version_id "
            "WHERE a.episode_id = ? AND a.required = TRUE",
            (self.episode_id,),
        )
        if not actions or any(
            row["status"]
            not in (ActionItemStatus.ACCEPTED, ActionItemStatus.AGGREGATED)
            or not row["current_valid_version_id"]
            or not row["accepted_task_result_id"]
            for row in actions
        ):
            return None

        current_final_id = episode["current_final_deliverable_id"]
        force_reprocess = False
        if current_final_id:
            current = self.db.one(
                "SELECT status, payload FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (current_final_id,),
            )
            if not current:
                return None
            if current["status"] == "SUPERSEDED":
                # A newly accepted task version supersedes the pending final but
                # intentionally keeps the pointer for revision lineage. The new
                # version fingerprint below must enqueue the replacement without
                # requiring a user to click an aggregate button.
                pass
            elif current["status"] != "PENDING_APPROVAL":
                return None
            else:
                current_payload = json.loads(current["payload"])
                current_mode = (current_payload.get("processing") or {}).get("mode")
                mode_matches = (
                    current_mode == "bailian"
                    if processing_mode == "bailian"
                    else str(current_mode).startswith("deterministic")
                )
                if mode_matches:
                    return None
                force_reprocess = True

        from .result_processing import RESULT_ORGANIZATION_PROMPT_VERSION

        fingerprint = self._final_input_fingerprint()
        # Hash the exact binary-free source bundle that the organizer receives;
        # keep the compact version fingerprint separately for stale-work checks.
        input_hash = stable_hash(self.aggregation_source_bundle())
        trigger_key = stable_hash(
            [input_hash, processing_mode, RESULT_ORGANIZATION_PROMPT_VERSION]
        )
        organization_effect = effect_id(
            episode_id=self.episode_id,
            subject_id=self.episode_id,
            effect_type="FINAL_ORGANIZATION",
            trigger_key=trigger_key,
        )
        correlation_id = f"corr_final_organization_{trigger_key[:16]}"
        sim_time = self.now()
        invocation = self._system_invocation_manifest(
            capability_type=(
                "MODEL" if processing_mode == "bailian" else "DETERMINISTIC"
            ),
            purpose="FINAL_ORGANIZATION",
            field_allowlist=self.FINAL_ORGANIZATION_CONTEXT_FIELDS,
            entity_versions={
                "episode_id": self.episode_id,
                "accepted_sources": fingerprint,
            },
            prompt_version=RESULT_ORGANIZATION_PROMPT_VERSION,
            input_hash=input_hash,
            output_status="QUEUED",
        )
        with self.db.transaction() as cursor:
            existing = cursor.execute(
                "SELECT outbox_id FROM outbox_entries WHERE effect_id = ?",
                (organization_effect,),
            ).fetchone()
            if existing:
                return existing["outbox_id"]
            outbox_id = f"out_{uuid4().hex}"
            cursor.execute(
                """
                INSERT INTO outbox_entries(
                    outbox_id, run_id, episode_id, action_item_id, effect_type,
                    effect_id, payload, status, attempt_count,
                    available_at_sim_time, correlation_id, created_sim_time
                ) VALUES (?, ?, ?, NULL, 'FINAL_ORGANIZATION', ?, ?, 'PENDING',
                          0, ?, ?, ?)
                """,
                (
                    outbox_id,
                    self.run_id,
                    self.episode_id,
                    organization_effect,
                    canonical_json(
                        {
                            "input_hash": input_hash,
                            "version_fingerprint": fingerprint,
                            "processing_mode": processing_mode,
                            "prompt_version": RESULT_ORGANIZATION_PROMPT_VERSION,
                            "force_reprocess": force_reprocess,
                            "supersedes_final_deliverable_id": current_final_id,
                            "invocation": invocation,
                        }
                    ),
                    sim_time,
                    correlation_id,
                    sim_time,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="OutboxEntry",
                aggregate_id=outbox_id,
                event_type="FinalOrganizationQueued",
                sim_time=sim_time,
                payload={
                    "effect_id": organization_effect,
                    "input_hash": input_hash,
                    "processing_mode": processing_mode,
                    "invocation": invocation,
                },
                correlation_id=correlation_id,
            )
        return outbox_id

    def aggregate(
        self,
        organizer: Callable[
            [list[dict[str, Any]]], tuple[dict[str, Any], dict[str, Any]]
        ]
        | None = None,
        *,
        force_reprocess: bool = False,
    ) -> str:
        sim_time = self.now()
        episode = self.db.one(
            "SELECT current_final_deliverable_id FROM episodes WHERE episode_id = ?",
            (self.episode_id,),
        )
        if episode and episode["current_final_deliverable_id"]:
            current = self.db.one(
                "SELECT status FROM final_deliverables WHERE final_deliverable_id = ?",
                (episode["current_final_deliverable_id"],),
            )
            if (
                current
                and force_reprocess
                and current["status"] != "PENDING_APPROVAL"
            ):
                raise ValueError(
                    "only a pending-approval final deliverable can be reprocessed"
                )
            if (
                current
                and current["status"] != "SUPERSEDED"
                and not force_reprocess
            ):
                return episode["current_final_deliverable_id"]
        source_bundle = self.aggregation_source_bundle()
        from .result_processing import (
            RESULT_ORGANIZATION_PROMPT_VERSION,
            build_deterministic_report,
            validate_organized_report,
        )

        if organizer:
            organized_report, processing = organizer(source_bundle)
            organized_report = validate_organized_report(
                organized_report, source_bundle
            )
        else:
            organized_report = build_deterministic_report(source_bundle)
            processing = {
                "mode": "deterministic_template",
                "provider": "local",
                "model": None,
                "prompt_version": RESULT_ORGANIZATION_PROMPT_VERSION,
                "input_hash": stable_hash(source_bundle),
            }
        processing = dict(processing or {})
        processing["prompt_version"] = RESULT_ORGANIZATION_PROMPT_VERSION
        processing["input_hash"] = stable_hash(source_bundle)
        actions = self.db.all(
            "SELECT * FROM action_items WHERE episode_id = ? AND required = TRUE "
            "ORDER BY action_item_id",
            (self.episode_id,),
        )

        revision_row = self.db.one(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_revision "
            "FROM final_deliverables WHERE episode_id = ?",
            (self.episode_id,),
        )
        final_id = f"final_{uuid4().hex}"
        revision_no = int(revision_row["next_revision"])
        payload: dict[str, Any] = {
            "deliverables": {},
            "accepted_task_results": {},
            "organized_report": organized_report,
            "processing": processing,
        }
        source_by_action = {
            source["action_item_id"]: source for source in source_bundle
        }
        version_rows: dict[str, Any] = {}
        for action in actions:
            version = self.db.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?",
                (action["current_valid_version_id"],),
            )
            data = json.loads(version["payload"])
            payload["deliverables"][action["deliverable_key"]] = data
            payload["accepted_task_results"][action["deliverable_key"]] = (
                source_by_action[action["action_item_id"]]["accepted_task_result"]
            )
            version_rows[action["action_item_id"]] = version
        correlation_id = f"corr_{final_id}"
        with self.db.transaction() as cursor:
            previous = cursor.execute(
                "SELECT current_final_deliverable_id FROM episodes WHERE episode_id = ?",
                (self.episode_id,),
            ).fetchone()["current_final_deliverable_id"]
            if previous:
                cursor.execute(
                    "UPDATE final_deliverables SET status = 'SUPERSEDED' "
                    "WHERE final_deliverable_id = ? AND status <> 'RELEASED'",
                    (previous,),
                )
                cursor.execute(
                    "UPDATE approvals SET status = 'SUPERSEDED' "
                    "WHERE approval_type = 'FINAL_RELEASE' AND status = 'PENDING' "
                    "AND json_extract(requested_action, '$.final_deliverable_id') = ?",
                    (previous,),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="FinalDeliverable",
                    aggregate_id=previous,
                    event_type="FinalDeliverableSuperseded",
                    sim_time=sim_time,
                    payload={"reason": "RESULT_REPROCESSED"},
                    correlation_id=correlation_id,
                )
            cursor.execute(
                """
                INSERT INTO final_deliverables(
                    final_deliverable_id, episode_id, revision_no, status,
                    generated_sim_time, payload, schema_version,
                    supersedes_final_deliverable_id
                ) VALUES (?, ?, ?, 'PENDING_APPROVAL', ?, ?, '1.0', ?)
                """,
                (
                    final_id,
                    self.episode_id,
                    revision_no,
                    sim_time,
                    canonical_json(payload),
                    previous,
                ),
            )
            for action in actions:
                version = version_rows[action["action_item_id"]]
                data = json.loads(version["payload"])
                for field, value in data.items():
                    field_path = f'deliverables.{action["deliverable_key"]}.{field}'
                    cursor.execute(
                        """
                        INSERT INTO final_field_lineage(
                            final_deliverable_id, field_path, action_item_id,
                            version_id, source_field_path, value_hash
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            final_id,
                            field_path,
                            action["action_item_id"],
                            version["version_id"],
                            field,
                            stable_hash(value),
                        ),
                    )
                cursor.execute(
                    "UPDATE action_items SET status = 'AGGREGATED', version = version + 1 "
                    "WHERE action_item_id = ?",
                    (action["action_item_id"],),
                )
            cursor.execute(
                """
                UPDATE episodes SET current_final_deliverable_id = ?,
                    status = 'READY_FOR_FINAL_APPROVAL', version = version + 1
                WHERE episode_id = ?
                """,
                (final_id, self.episode_id),
            )
            release_effect = effect_id(
                episode_id=self.episode_id,
                subject_id=final_id,
                effect_type="FINAL_RELEASE",
                trigger_key=final_id,
            )
            approval_id = f"app_{uuid4().hex}"
            requested_action = {
                "final_deliverable_id": final_id,
                "target_actor_id": self.aggregator_actor_id,
                "effect_type": "FINAL_RELEASE",
                "content": f"终稿修订 {revision_no} 已批准并发布。",
            }
            cursor.execute(
                """
                INSERT INTO approvals(
                    approval_id, episode_id, approval_type, effect_id, status,
                    requested_action, requested_by, requested_sim_time,
                    correlation_id, version
                ) VALUES (?, ?, 'FINAL_RELEASE', ?, 'PENDING', ?, 'agent', ?, ?, 1)
                """,
                (
                    approval_id,
                    self.episode_id,
                    release_effect,
                    canonical_json(requested_action),
                    sim_time,
                    correlation_id,
                ),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="FinalDeliverable",
                aggregate_id=final_id,
                event_type="FinalDeliverableGenerated",
                sim_time=sim_time,
                payload={
                    "revision_no": revision_no,
                    "processing_mode": processing.get("mode"),
                    "processing_input_hash": processing.get("input_hash"),
                    "processing_prompt_version": processing.get("prompt_version"),
                },
                correlation_id=correlation_id,
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="Approval",
                aggregate_id=approval_id,
                event_type="ApprovalRequested",
                sim_time=sim_time,
                payload={"approval_type": "FINAL_RELEASE"},
                correlation_id=correlation_id,
            )
        return final_id

    def dispatch_final_organization_once(
        self,
        *,
        session_id: str,
        organizer: Callable[
            [list[dict[str, Any]]], tuple[dict[str, Any], dict[str, Any]]
        ]
        | None,
    ) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM outbox_entries WHERE effect_type = 'FINAL_ORGANIZATION' "
            "AND status IN ('PENDING','RETRY_WAIT') "
            "ORDER BY created_sim_time, outbox_id LIMIT 1"
        )
        if not row:
            return None
        sim_time = self.now()
        payload = json.loads(row["payload"])
        invocation = dict(payload.get("invocation") or {})
        with self.db.transaction() as cursor:
            changed = cursor.execute(
                "UPDATE outbox_entries SET status = 'CLAIMED', "
                "claimed_by_session = ?, claimed_at_real = CURRENT_TIMESTAMP, "
                "attempt_count = attempt_count + 1 "
                "WHERE outbox_id = ? AND status IN ('PENDING','RETRY_WAIT')",
                (session_id, row["outbox_id"]),
            ).rowcount
            if not changed:
                return None
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="OutboxEntry",
                aggregate_id=row["outbox_id"],
                event_type="FinalOrganizationStarted",
                sim_time=sim_time,
                payload={
                    "session_id": session_id,
                    "invocation": {
                        **invocation,
                        "output_status": "STARTED",
                    },
                },
                correlation_id=row["correlation_id"],
            )

        attempt_count = int(row["attempt_count"]) + 1
        if payload["version_fingerprint"] != self._final_input_fingerprint():
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE outbox_entries SET status = 'DELIVERED', "
                    "external_receipt_id = 'obsolete-input', delivered_sim_time = ?, "
                    "claimed_by_session = NULL, claimed_at_real = NULL "
                    "WHERE outbox_id = ?",
                    (sim_time, row["outbox_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="OutboxEntry",
                    aggregate_id=row["outbox_id"],
                    event_type="FinalOrganizationSkipped",
                    sim_time=sim_time,
                    payload={
                        "reason": "INPUT_VERSION_CHANGED",
                        "invocation": {
                            **invocation,
                            "output_status": "SKIPPED",
                        },
                    },
                    correlation_id=row["correlation_id"],
                )
            return {"status": "SKIPPED", "reason": "INPUT_VERSION_CHANGED"}

        try:
            processing_mode = payload["processing_mode"]
            if processing_mode == "bailian" and organizer is None:
                raise ValueError("bailian result organizer is not configured")
            final_id = self.aggregate(
                organizer=organizer if processing_mode == "bailian" else None,
                force_reprocess=bool(payload.get("force_reprocess")),
            )
        except Exception as error:  # worker boundary persists retry state
            max_attempts = int(self.fixture["policy"]["outbox_max_attempts"])
            retry_status = (
                OutboxStatus.DEAD_LETTER
                if attempt_count >= max_attempts
                else OutboxStatus.RETRY_WAIT
            )
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE outbox_entries SET status = ?, last_error_code = ?, "
                    "claimed_by_session = NULL, claimed_at_real = NULL "
                    "WHERE outbox_id = ?",
                    (retry_status, type(error).__name__, row["outbox_id"]),
                )
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="OutboxEntry",
                    aggregate_id=row["outbox_id"],
                    event_type=(
                        "FinalOrganizationDeadLettered"
                        if retry_status == OutboxStatus.DEAD_LETTER
                        else "FinalOrganizationRetryScheduled"
                    ),
                    sim_time=sim_time,
                    payload={
                        "attempt_count": attempt_count,
                        "error_type": type(error).__name__,
                        "error": str(error)[:300],
                        "invocation": {
                            **invocation,
                            "output_status": str(retry_status),
                            "error_code": type(error).__name__,
                        },
                    },
                    correlation_id=row["correlation_id"],
                )
            return {
                "status": retry_status,
                "attempt_count": attempt_count,
                "error": str(error)[:300],
            }

        final_row = self.db.one(
            "SELECT payload FROM final_deliverables WHERE final_deliverable_id = ?",
            (final_id,),
        )
        final_payload = (
            json.loads(final_row["payload"])
            if final_row and isinstance(final_row["payload"], str)
            else dict(final_row["payload"] or {})
            if final_row
            else {}
        )
        final_processing = dict(final_payload.get("processing") or {})
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE outbox_entries SET status = 'DELIVERED', "
                "external_receipt_id = ?, delivered_sim_time = ?, "
                "last_error_code = NULL, claimed_by_session = NULL, "
                "claimed_at_real = NULL WHERE outbox_id = ?",
                (final_id, sim_time, row["outbox_id"]),
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="OutboxEntry",
                aggregate_id=row["outbox_id"],
                event_type="FinalOrganizationSucceeded",
                sim_time=sim_time,
                payload={
                    "final_deliverable_id": final_id,
                    "attempt_count": attempt_count,
                    "invocation": {
                        **invocation,
                        "output_status": "SUCCEEDED",
                        "provider": final_processing.get("provider"),
                        "model": final_processing.get("model"),
                        "usage": final_processing.get("usage") or {},
                    },
                },
                correlation_id=row["correlation_id"],
            )
        return {
            "status": OutboxStatus.DELIVERED,
            "attempt_count": attempt_count,
            "final_deliverable_id": final_id,
        }

    def recover_dispatcher(self, session_id: str) -> int:
        sim_time = self.now()
        with self.db.transaction() as cursor:
            rows = cursor.execute(
                "SELECT outbox_id FROM outbox_entries "
                "WHERE status = 'CLAIMED' AND claimed_by_session <> ?",
                (session_id,),
            ).fetchall()
            cursor.execute(
                """
                UPDATE outbox_entries SET status = 'PENDING', claimed_by_session = NULL,
                    claimed_at_real = NULL
                WHERE status = 'CLAIMED' AND claimed_by_session <> ?
                """,
                (session_id,),
            )
            if rows:
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="Episode",
                    aggregate_id=self.episode_id,
                    event_type="ProcessRecovered",
                    sim_time=sim_time,
                    payload={"reclaimed_outbox_ids": [row["outbox_id"] for row in rows]},
                    correlation_id=f"corr_recover_{session_id}",
                )
        return len(rows)

    def dispatch_all(
        self,
        *,
        session_id: str,
        crash_after_accept_effect_id: str | None = None,
    ) -> int:
        delivered = 0
        while True:
            row = self.db.one(
                """
                SELECT * FROM outbox_entries
                WHERE status IN ('PENDING','RETRY_WAIT')
                  AND effect_type <> 'FINAL_ORGANIZATION'
                  AND available_at_sim_time <= ?
                ORDER BY created_sim_time, outbox_id LIMIT 1
                """,
                (self.now(),),
            )
            if not row:
                return delivered
            with self.db.transaction() as cursor:
                changed = cursor.execute(
                    """
                    UPDATE outbox_entries SET status = 'CLAIMED',
                        claimed_by_session = ?, claimed_at_real = CURRENT_TIMESTAMP,
                        attempt_count = attempt_count + 1
                    WHERE outbox_id = ? AND status IN ('PENDING','RETRY_WAIT')
                    """,
                    (session_id, row["outbox_id"]),
                ).rowcount
                if not changed:
                    continue
                self.db.append_audit(
                    cursor,
                    run_id=self.run_id,
                    aggregate_type="OutboxEntry",
                    aggregate_id=row["outbox_id"],
                    event_type="OutboxEntryClaimed",
                    sim_time=self.now(),
                    payload={"session_id": session_id},
                    correlation_id=row["correlation_id"],
                )
            payload = json.loads(row["payload"])
            receipt = self.im.send(
                {
                    "effect_id": row["effect_id"],
                    "effect_type": row["effect_type"],
                    **payload,
                },
                accepted_sim_time=self.now(),
            )
            if row["effect_id"] == crash_after_accept_effect_id:
                raise SimulatedCrash(
                    "crash after mock IM accepted, before Outbox DELIVERED"
                )
            self._complete_delivery(dict(row), receipt)
            delivered += 1

    def _complete_delivery(
        self, outbox: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        sim_time = self.now()
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                UPDATE outbox_entries SET status = 'DELIVERED',
                    external_receipt_id = ?, delivered_sim_time = ?,
                    claimed_by_session = NULL, claimed_at_real = NULL
                WHERE outbox_id = ?
                """,
                (receipt["external_message_id"], sim_time, outbox["outbox_id"]),
            )
            cursor.execute(
                "UPDATE interventions SET status = 'DELIVERED' WHERE effect_id = ?",
                (outbox["effect_id"],),
            )
            approval = cursor.execute(
                "SELECT * FROM approvals WHERE effect_id = ? AND status = 'APPROVED'",
                (outbox["effect_id"],),
            ).fetchone()
            if approval:
                cursor.execute(
                    "UPDATE approvals SET status = 'EXECUTED', version = version + 1 "
                    "WHERE approval_id = ?",
                    (approval["approval_id"],),
                )
                if approval["approval_type"] == "FINAL_RELEASE":
                    requested = json.loads(approval["requested_action"])
                    final_id = requested["final_deliverable_id"]
                    cursor.execute(
                        "UPDATE final_deliverables SET status = 'RELEASED' "
                        "WHERE final_deliverable_id = ?",
                        (final_id,),
                    )
                    cursor.execute(
                        "UPDATE action_items SET status = 'ARCHIVED', version = version + 1 "
                        "WHERE episode_id = ? AND status = 'AGGREGATED'",
                        (self.episode_id,),
                    )
                    cursor.execute(
                        "UPDATE episodes SET status = 'ARCHIVED', version = version + 1 "
                        "WHERE episode_id = ?",
                        (self.episode_id,),
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="FinalDeliverable",
                        aggregate_id=final_id,
                        event_type="FinalDeliverableReleased",
                        sim_time=sim_time,
                        payload={"external_message_id": receipt["external_message_id"]},
                        correlation_id=approval["correlation_id"],
                    )
                    self.db.append_audit(
                        cursor,
                        run_id=self.run_id,
                        aggregate_type="Episode",
                        aggregate_id=self.episode_id,
                        event_type="EpisodeArchived",
                        sim_time=sim_time,
                        payload={"final_deliverable_id": final_id},
                        correlation_id=approval["correlation_id"],
                    )
            event_type = (
                "OutboxDeliveryDeduplicated"
                if receipt["deduplicated"]
                else "OutboxDeliverySucceeded"
            )
            self.db.append_audit(
                cursor,
                run_id=self.run_id,
                aggregate_type="OutboxEntry",
                aggregate_id=outbox["outbox_id"],
                event_type=event_type,
                sim_time=sim_time,
                payload={
                    "effect_id": outbox["effect_id"],
                    "external_message_id": receipt["external_message_id"],
                },
                correlation_id=outbox["correlation_id"],
            )

    def pending_approval(self, approval_type: str) -> dict[str, Any] | None:
        row = self.db.one(
            """
            SELECT * FROM approvals
            WHERE episode_id = ? AND approval_type = ? AND status = 'PENDING'
            ORDER BY requested_sim_time LIMIT 1
            """,
            (self.episode_id, approval_type),
        )
        return dict(row) if row else None

    def action(self, action_item_id: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM action_items WHERE action_item_id = ?",
            (action_item_id,),
        )
        if not row:
            raise KeyError(action_item_id)
        return dict(row)

    def audit_events(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence_no",
                (self.run_id,),
            )
        ]

    def outbox(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM outbox_entries WHERE episode_id = ?",
                (self.episode_id,),
            )
        ]

    def action_items(self) -> Iterable[dict[str, Any]]:
        return (
            dict(row)
            for row in self.db.all(
                "SELECT * FROM action_items WHERE episode_id = ? ORDER BY action_item_id",
                (self.episode_id,),
            )
        )
