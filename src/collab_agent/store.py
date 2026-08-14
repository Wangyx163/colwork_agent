from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import canonical_json


SQLITE_SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    run_id TEXT NOT NULL,
    content_pack_id TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL,
    transcript TEXT NOT NULL,
    current_sim_time TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    evaluation_cutoff_sim_time TEXT NOT NULL,
    current_final_deliverable_id TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS active_episode_lookup
ON episodes(organization_id, status) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS episode_participants (
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    role TEXT NOT NULL,
    PRIMARY KEY (episode_id, actor_id, role)
);

CREATE TABLE IF NOT EXISTS action_items (
    action_item_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    identity_key TEXT NOT NULL,
    title TEXT NOT NULL,
    deliverable_key TEXT NOT NULL,
    owner_actor_id TEXT REFERENCES actors(actor_id),
    required INTEGER NOT NULL,
    status TEXT NOT NULL,
    deadline_sim_time TEXT,
    team_required_by_sim_time TEXT,
    sla_id TEXT NOT NULL,
    active_commitment_revision_id TEXT,
    current_valid_version_id TEXT,
    source_message_id TEXT NOT NULL,
    source_span TEXT NOT NULL,
    proposal_metadata TEXT NOT NULL DEFAULT '{}',
    published_sim_time TEXT,
    created_sim_time TEXT NOT NULL,
    confirmed_sim_time TEXT,
    last_owner_signal_at TEXT,
    last_owner_signal_type TEXT,
    last_owner_signal_valid_until TEXT,
    last_owner_signal_actor_id TEXT REFERENCES actors(actor_id),
    definition_version INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(episode_id, identity_key)
);

-- Recall evidence that is intentionally below the ActionItem creation bar.
-- It has no assignment or delivery state; a coordinator must explicitly
-- materialize it after supplying a usable task definition.
CREATE TABLE IF NOT EXISTS review_hints (
    hint_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'MATERIALIZED', 'DISMISSED')),
    source_timestamp TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    hint_payload TEXT NOT NULL DEFAULT '{}',
    materialized_action_item_id TEXT REFERENCES action_items(action_item_id),
    created_sim_time TEXT NOT NULL,
    resolved_sim_time TEXT,
    resolved_by_actor_id TEXT REFERENCES actors(actor_id),
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(episode_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS review_hint_status_lookup
ON review_hints(episode_id, status, created_sim_time);

CREATE TABLE IF NOT EXISTS action_item_assignments (
    assignment_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    definition_version INTEGER NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    assignment_role TEXT NOT NULL CHECK (
        assignment_role IN ('OWNER', 'COLLABORATOR')
    ),
    response_status TEXT NOT NULL CHECK (
        response_status IN ('PENDING', 'ACCEPTED', 'RETURNED', 'SUPERSEDED')
    ),
    assignment_message TEXT NOT NULL DEFAULT '',
    response_message TEXT,
    assigned_sim_time TEXT NOT NULL,
    responded_sim_time TEXT,
    source_message_id TEXT NOT NULL,
    UNIQUE(action_item_id, definition_version, actor_id)
);
CREATE INDEX IF NOT EXISTS action_item_assignment_actor_lookup
ON action_item_assignments(actor_id, response_status, action_item_id);

CREATE TABLE IF NOT EXISTS commitment_revisions (
    commitment_revision_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    revision_no INTEGER NOT NULL,
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    deliverable_key TEXT NOT NULL,
    promised_deadline_sim_time TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_span TEXT NOT NULL,
    status TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    supersedes_revision_id TEXT,
    UNIQUE(action_item_id, revision_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_commitment
ON commitment_revisions(action_item_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS assistance_requests (
    assistance_request_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    requester_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    target_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    blocking_action_item_id TEXT REFERENCES action_items(action_item_id),
    status TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    updated_sim_time TEXT NOT NULL,
    resolved_sim_time TEXT,
    resolution_summary TEXT
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    version_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    submitted_by_actor_id TEXT REFERENCES actors(actor_id),
    received_sim_time TEXT NOT NULL,
    received_sequence INTEGER NOT NULL,
    payload TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_errors TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_version_id TEXT,
    review_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    reviewed_by TEXT REFERENCES actors(actor_id),
    reviewed_sim_time TEXT,
    review_comment TEXT,
    attachment_extractions TEXT NOT NULL DEFAULT '[]',
    source_manifest TEXT NOT NULL DEFAULT '{}',
    processing_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    processing_result TEXT,
    processing_metadata TEXT,
    processing_error_code TEXT,
    processing_error_stage TEXT,
    processing_error_detail TEXT,
    processing_retryable INTEGER NOT NULL DEFAULT 0,
    processing_attempt_count INTEGER NOT NULL DEFAULT 0,
    processing_updated_sim_time TEXT,
    processing_next_attempt_at TEXT,
    UNIQUE(action_item_id, content_hash)
);

CREATE TABLE IF NOT EXISTS accepted_task_results (
    accepted_task_result_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    accepted_version_id TEXT NOT NULL UNIQUE REFERENCES artifact_versions(version_id),
    completed_content_refs TEXT NOT NULL,
    completion_report TEXT NOT NULL,
    normalized_result TEXT,
    source_manifest TEXT NOT NULL,
    processing_metadata TEXT NOT NULL,
    collaboration_report TEXT,
    collaboration_report_status TEXT NOT NULL DEFAULT 'PENDING',
    collaboration_report_error TEXT,
    accepted_by TEXT NOT NULL REFERENCES actors(actor_id),
    accepted_sim_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collaboration_memories (
    memory_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    topic TEXT NOT NULL,
    value TEXT NOT NULL,
    visibility TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    version INTEGER NOT NULL,
    confirmed_by TEXT REFERENCES actors(actor_id),
    confirmed_sim_time TEXT,
    origin TEXT NOT NULL DEFAULT 'SYSTEM_OBSERVED',
    supersedes_memory_id TEXT REFERENCES collaboration_memories(memory_id)
);

CREATE TABLE IF NOT EXISTS action_item_dependencies (
    dependency_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    upstream_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    downstream_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    gate_type TEXT NOT NULL CHECK (gate_type = 'ALL_REQUIRED'),
    source_span TEXT NOT NULL,
    bound_upstream_version_id TEXT REFERENCES artifact_versions(version_id),
    created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_sim_time TEXT NOT NULL,
    CHECK (upstream_action_item_id <> downstream_action_item_id),
    UNIQUE(upstream_action_item_id, downstream_action_item_id)
);

CREATE TABLE IF NOT EXISTS action_item_participation_inputs (
    contribution_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    contribution_type TEXT NOT NULL CHECK (
        contribution_type IN ('BALLOT', 'VOTE', 'FEEDBACK')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'SUBMITTED', 'SUPERSEDED')
    ),
    payload TEXT,
    payload_ref TEXT,
    source_span TEXT NOT NULL,
    source_message_id TEXT,
    submitted_sim_time TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(action_item_id, actor_id, contribution_type)
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    target_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    level TEXT NOT NULL,
    action_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    social_cost INTEGER NOT NULL,
    status TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    outbox_id TEXT,
    correlation_id TEXT NOT NULL,
    created_sim_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT,
    approval_type TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_sim_time TEXT NOT NULL,
    decided_by TEXT,
    decided_sim_time TEXT,
    execution_outbox_id TEXT,
    correlation_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS outbox_entries (
    outbox_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT,
    effect_type TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at_sim_time TEXT NOT NULL,
    claimed_by_session TEXT,
    claimed_at_real TEXT,
    external_receipt_id TEXT,
    last_error_code TEXT,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    created_sim_time TEXT NOT NULL,
    delivered_sim_time TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    sim_time TEXT NOT NULL,
    payload TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    recorded_at_real TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS final_deliverables (
    final_deliverable_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    generated_sim_time TEXT NOT NULL,
    approved_sim_time TEXT,
    payload TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    supersedes_final_deliverable_id TEXT,
    UNIQUE(episode_id, revision_no)
);

CREATE TABLE IF NOT EXISTS final_field_lineage (
    final_deliverable_id TEXT NOT NULL REFERENCES final_deliverables(final_deliverable_id),
    field_path TEXT NOT NULL,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    version_id TEXT NOT NULL REFERENCES artifact_versions(version_id),
    source_field_path TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    PRIMARY KEY(final_deliverable_id, field_path)
);

CREATE TABLE IF NOT EXISTS inbound_receipts (
    message_id TEXT PRIMARY KEY,
    accepted_sequence INTEGER NOT NULL UNIQUE,
    processed_result TEXT NOT NULL,
    accepted_sim_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_im_messages (
    external_message_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender_actor_id TEXT NOT NULL,
    recipient_actor_ids TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    content TEXT NOT NULL,
    accepted_sequence INTEGER NOT NULL UNIQUE,
    accepted_sim_time TEXT NOT NULL
);

-- A shape the meeting decided on, rather than a set of tasks wired together.
-- Its stages alternate between everybody and one person, and that alternation
-- is what the earlier dependency-based version could not express: there was no
-- stage at which somebody was asked to fill in their own options.
CREATE TABLE IF NOT EXISTS compound_tasks (
    compound_task_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    kind TEXT NOT NULL CHECK (kind IN ('VOTE', 'SUBMIT')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN ('COLLECTING','MERGING','VOTING','FINALIZING','DONE','REVOKED')
    ),
    -- Read off the meeting rather than chosen in a form: the person at the
    -- stage where the headcount drops is the one doing the merging.
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    member_actor_ids TEXT NOT NULL,
    selection_count INTEGER,
    source_span TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    stage_entered_sim_time TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0)
);

-- One person's answer at one stage. Options are rows here rather than text to
-- be parsed later: a person typing one option per line already knows where
-- they end, and asking a model to rediscover that is a step that can only
-- lose information.
CREATE TABLE IF NOT EXISTS compound_task_inputs (
    input_id TEXT PRIMARY KEY,
    compound_task_id TEXT NOT NULL REFERENCES compound_tasks(compound_task_id),
    stage TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    payload TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    UNIQUE (compound_task_id, stage, actor_id)
);

CREATE TABLE IF NOT EXISTS scope_change_requests (
    request_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    definition_version INTEGER NOT NULL,
    proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    proposed_deliverable TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by_actor_id TEXT,
    decision_comment TEXT,
    source_message_id TEXT NOT NULL,
    proposed_sim_time TEXT NOT NULL,
    decided_sim_time TEXT
);

CREATE TABLE IF NOT EXISTS assignment_handoffs (
    handoff_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    definition_version INTEGER NOT NULL,
    from_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    to_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    assignment_role TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    response_message TEXT,
    source_message_id TEXT NOT NULL,
    proposed_sim_time TEXT NOT NULL,
    decided_sim_time TEXT
);

CREATE TABLE IF NOT EXISTS compound_task_skips (
    skip_id TEXT PRIMARY KEY,
    compound_task_id TEXT NOT NULL REFERENCES compound_tasks(compound_task_id),
    stage TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    skipped_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    reason TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_sim_time TEXT NOT NULL,
    UNIQUE (compound_task_id, stage, actor_id)
);
"""


class Database:
    def __init__(self, path: str | Path, *, allow_cross_thread: bool = False):
        """Open one SQLite connection.

        `allow_cross_thread` only lifts sqlite3's own thread guard; it is not a
        licence to share a connection. It exists for `ThreadLocalDatabase`,
        which still gives every thread its own connection but needs to close
        them all from whichever thread runs shutdown. Leave it False anywhere a
        connection is genuinely single-threaded, so the guard keeps catching
        accidental sharing.
        """

        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=not allow_cross_thread,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def _rename_legacy_participation_table(self) -> None:
        """Rename the P1 participation table before the schema script runs.

        `action_item_contributions` collided with the P0 concept of a
        collaborator's ArtifactVersion contribution while actually holding
        BALLOT/VOTE/FEEDBACK inputs. This must happen first: the schema script
        would otherwise create an empty table under the new name and strand the
        rows in the old one.
        """

        tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "action_item_contributions" not in tables:
            return
        if "action_item_participation_inputs" in tables:
            raise RuntimeError(
                "both action_item_contributions and "
                "action_item_participation_inputs exist; resolve the duplicate "
                "before starting the workbench"
            )
        self.connection.execute(
            "ALTER TABLE action_item_contributions "
            "RENAME TO action_item_participation_inputs"
        )
        self.connection.commit()

    def initialize(self) -> None:
        self._rename_legacy_participation_table()
        self.connection.executescript(SQLITE_SCHEMA)
        self.connection.execute("DROP INDEX IF EXISTS one_active_episode")
        existing = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(action_items)"
            ).fetchall()
        }
        if "proposal_metadata" not in existing:
            self.connection.execute(
                "ALTER TABLE action_items ADD COLUMN proposal_metadata "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "published_sim_time" not in existing:
            self.connection.execute(
                "ALTER TABLE action_items ADD COLUMN published_sim_time TEXT"
            )
        if "team_required_by_sim_time" not in existing:
            self.connection.execute(
                "ALTER TABLE action_items ADD COLUMN team_required_by_sim_time TEXT"
            )
            self.connection.execute(
                "UPDATE action_items SET team_required_by_sim_time = deadline_sim_time "
                "WHERE team_required_by_sim_time IS NULL"
            )
        action_migrations = {
            "last_owner_signal_at": "TEXT",
            "last_owner_signal_type": "TEXT",
            "last_owner_signal_valid_until": "TEXT",
            "last_owner_signal_actor_id": "TEXT",
            "definition_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in action_migrations.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE action_items ADD COLUMN {name} {definition}"
                )
        version_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(artifact_versions)"
            ).fetchall()
        }
        migrations = {
            "submitted_by_actor_id": "TEXT",
            "review_status": "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
            "reviewed_by": "TEXT",
            "reviewed_sim_time": "TEXT",
            "review_comment": "TEXT",
            "attachment_extractions": "TEXT NOT NULL DEFAULT '[]'",
            "source_manifest": "TEXT NOT NULL DEFAULT '{}'",
            "processing_status": "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
            "processing_result": "TEXT",
            "processing_metadata": "TEXT",
            "processing_error_code": "TEXT",
            "processing_error_stage": "TEXT",
            "processing_error_detail": "TEXT",
            "processing_retryable": "INTEGER NOT NULL DEFAULT 0",
            "processing_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "processing_updated_sim_time": "TEXT",
            "processing_next_attempt_at": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in version_columns:
                self.connection.execute(
                    f"ALTER TABLE artifact_versions ADD COLUMN {name} {definition}"
                )
        accepted_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(accepted_task_results)"
            ).fetchall()
        }
        accepted_migrations = {
            "collaboration_report": "TEXT",
            "collaboration_report_status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "collaboration_report_error": "TEXT",
        }
        for name, definition in accepted_migrations.items():
            if name not in accepted_columns:
                self.connection.execute(
                    f"ALTER TABLE accepted_task_results ADD COLUMN {name} {definition}"
                )
        memory_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(collaboration_memories)"
            ).fetchall()
        }
        if memory_columns and "origin" not in memory_columns:
            # Everything written before the A/B split was system-proposed.
            self.connection.execute(
                "ALTER TABLE collaboration_memories ADD COLUMN origin "
                "TEXT NOT NULL DEFAULT 'SYSTEM_OBSERVED'"
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_item_dependencies (
                dependency_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                upstream_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                downstream_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                gate_type TEXT NOT NULL CHECK (gate_type = 'ALL_REQUIRED'),
                source_span TEXT NOT NULL,
                bound_upstream_version_id TEXT REFERENCES artifact_versions(version_id),
                created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                created_sim_time TEXT NOT NULL,
                CHECK (upstream_action_item_id <> downstream_action_item_id),
                UNIQUE(upstream_action_item_id, downstream_action_item_id)
            );
            CREATE TABLE IF NOT EXISTS action_item_links (
                link_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                prior_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                relation TEXT NOT NULL CHECK (
                    relation IN ('CONTINUATION', 'DUPLICATE')
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')
                ),
                source TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                confidence REAL,
                proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                proposed_sim_time TEXT NOT NULL,
                decided_by_actor_id TEXT REFERENCES actors(actor_id),
                decided_sim_time TEXT,
                CHECK (action_item_id <> prior_action_item_id),
                UNIQUE(action_item_id, prior_action_item_id)
            );
            CREATE TABLE IF NOT EXISTS action_item_participation_inputs (
                contribution_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                contribution_type TEXT NOT NULL CHECK (
                    contribution_type IN ('BALLOT', 'VOTE', 'FEEDBACK')
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'SUBMITTED', 'SUPERSEDED')
                ),
                payload TEXT,
                payload_ref TEXT,
                source_span TEXT NOT NULL,
                source_message_id TEXT,
                submitted_sim_time TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(action_item_id, actor_id, contribution_type)
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        finally:
            cursor.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def append_audit(
        self,
        cursor: sqlite3.Cursor,
        *,
        run_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        sim_time: str,
        payload: dict[str, Any],
        correlation_id: str,
        causation_id: str | None = None,
    ) -> int:
        row = cursor.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
            "FROM audit_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        sequence = int(row["next_sequence"])
        cursor.execute(
            """
            INSERT INTO audit_events(
                event_id, run_id, sequence_no, aggregate_type, aggregate_id,
                event_type, event_version, sim_time, payload, correlation_id,
                causation_id
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                f"evt_{uuid4().hex}",
                run_id,
                sequence,
                aggregate_type,
                aggregate_id,
                event_type,
                sim_time,
                canonical_json(payload),
                correlation_id,
                causation_id,
            ),
        )
        return sequence

    @staticmethod
    def json(row: sqlite3.Row, field: str) -> Any:
        return json.loads(row[field])
