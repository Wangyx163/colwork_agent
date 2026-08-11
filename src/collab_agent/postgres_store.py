from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads

from .models import canonical_json


def database_url_for_schema(
    database_url: str, schema_name: str, *, create: bool = False
) -> str:
    """Return a connection URL pinned to one validated PostgreSQL schema."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema_name):
        raise ValueError("invalid PostgreSQL schema name")
    if create:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(schema_name)
                )
            )
    parsed = urlsplit(database_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    existing_options = [value for key, value in query if key == "options"]
    query = [(key, value) for key, value in query if key != "options"]
    options = " ".join(
        [*existing_options, f"-csearch_path={schema_name}"]
    ).strip()
    query.append(("options", options))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _postgres_sql(sql: str) -> str:
    translated = sql.replace("?", "%s")
    translated = translated.replace(
        "json_extract(requested_action, '$.final_deliverable_id')",
        "requested_action ->> 'final_deliverable_id'",
    )
    translated = translated.replace(
        "substr(created_sim_time, 1, 10)",
        "substr(created_sim_time::text, 1, 10)",
    )
    return translated


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


class PostgresCursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]]):
        self.cursor = cursor

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> "PostgresCursor":
        self.cursor.execute(_postgres_sql(sql), params)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return _normalize_row(self.cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        return [_normalize_row(row) for row in self.cursor.fetchall()]  # type: ignore[misc]

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount


class PostgresDatabase:
    """P0 PostgreSQL adapter with the same small interface as the SQLite adapter."""

    def __init__(self, database_url: str, *, schema_path: str | Path | None = None):
        self.database_url = database_url
        self.schema_path = Path(schema_path) if schema_path else None
        self.connection = psycopg.connect(
            database_url, autocommit=True, row_factory=dict_row
        )
        set_json_loads(
            lambda data: data.decode("utf-8")
            if isinstance(data, (bytes, bytearray, memoryview))
            else str(data),
            self.connection,
        )

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        # Web and Agent Worker are independent processes and may start together.
        # A session advisory lock makes the check/create/migrate sequence atomic
        # across their separate PostgreSQL connections.
        lock_name = "colwork_agent_schema_initialize_v1"
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_name,))
        try:
            existing = self.one("SELECT to_regclass('organizations') AS table_name")
            if existing and existing["table_name"]:
                self._migrate_claimable_action_items()
                return
            if not self.schema_path:
                raise RuntimeError(
                    "PostgreSQL schema is missing; run setup_postgres.ps1"
                )
            schema = self.schema_path.read_text(encoding="utf-8")
            with self.connection.cursor() as cursor:
                cursor.execute(schema, prepare=False)
        finally:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (lock_name,)
                )

    def _migrate_claimable_action_items(self) -> None:
        """Apply the small, idempotent P0-to-meeting-intake schema extension."""
        with self.connection.cursor() as cursor:
            cursor.execute("DROP INDEX IF EXISTS one_active_episode")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS review_hints (
                    hint_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('OPEN', 'MATERIALIZED', 'DISMISSED')
                    ),
                    source_timestamp TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    evidence_text TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    hint_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    materialized_action_item_id TEXT REFERENCES action_items(action_item_id),
                    created_sim_time TIMESTAMPTZ NOT NULL,
                    resolved_sim_time TIMESTAMPTZ,
                    resolved_by_actor_id TEXT REFERENCES actors(actor_id),
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (episode_id, candidate_id)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS review_hint_status_lookup "
                "ON review_hints (episode_id, status, created_sim_time)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS active_episode_lookup "
                "ON episodes (organization_id, status) WHERE status = 'ACTIVE'"
            )
            cursor.execute(
                "ALTER TABLE action_items ALTER COLUMN owner_actor_id DROP NOT NULL"
            )
            cursor.execute(
                "ALTER TABLE action_items ALTER COLUMN deadline_sim_time DROP NOT NULL"
            )
            cursor.execute(
                "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS proposal_metadata "
                "JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            cursor.execute(
                "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS "
                "published_sim_time TIMESTAMPTZ"
            )
            cursor.execute(
                "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS "
                "team_required_by_sim_time TIMESTAMPTZ"
            )
            cursor.execute(
                "UPDATE action_items SET team_required_by_sim_time = deadline_sim_time "
                "WHERE team_required_by_sim_time IS NULL"
            )
            for column, definition in (
                ("last_owner_signal_at", "TIMESTAMPTZ"),
                ("last_owner_signal_type", "TEXT"),
                ("last_owner_signal_valid_until", "TIMESTAMPTZ"),
                ("last_owner_signal_actor_id", "TEXT REFERENCES actors(actor_id)"),
                ("definition_version", "INTEGER NOT NULL DEFAULT 1"),
            ):
                cursor.execute(
                    f"ALTER TABLE action_items ADD COLUMN IF NOT EXISTS "
                    f"{column} {definition}"
                )
            # Cross-meeting linkage. Added here as well as in the schema file
            # because an existing database never re-runs the schema script.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS action_item_links (
                    link_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    action_item_id TEXT NOT NULL
                        REFERENCES action_items(action_item_id),
                    prior_action_item_id TEXT NOT NULL
                        REFERENCES action_items(action_item_id),
                    relation TEXT NOT NULL CHECK (
                        relation IN ('CONTINUATION', 'DUPLICATE')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')
                    ),
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    confidence DOUBLE PRECISION,
                    proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                    proposed_sim_time TEXT NOT NULL,
                    decided_by_actor_id TEXT REFERENCES actors(actor_id),
                    decided_sim_time TEXT,
                    CHECK (action_item_id <> prior_action_item_id),
                    UNIQUE(action_item_id, prior_action_item_id)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS action_item_link_lookup "
                "ON action_item_links(action_item_id, status)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS assistance_requests (
                    assistance_request_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                    requester_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                    target_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    blocking_action_item_id TEXT REFERENCES action_items(action_item_id),
                    status TEXT NOT NULL CHECK (
                        status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'CANCELLED')
                    ),
                    created_sim_time TIMESTAMPTZ NOT NULL,
                    updated_sim_time TIMESTAMPTZ NOT NULL,
                    resolved_sim_time TIMESTAMPTZ,
                    resolution_summary TEXT
                )
                """
            )
            cursor.execute(
                "ALTER TABLE episode_participants DROP CONSTRAINT IF EXISTS "
                "episode_participants_role_check"
            )
            cursor.execute(
                "ALTER TABLE episode_participants ADD CONSTRAINT "
                "episode_participants_role_check CHECK (role IN "
                "('COORDINATOR', 'PARTICIPANT', 'AGGREGATOR', "
                "'ACTION_OWNER', 'MANAGER'))"
            )
            cursor.execute(
                "ALTER TABLE action_items DROP CONSTRAINT IF EXISTS "
                "action_items_status_check"
            )
            cursor.execute(
                "ALTER TABLE action_items ADD CONSTRAINT action_items_status_check "
                "CHECK (status IN ('PENDING_CONFIRMATION', 'PENDING_ASSIGNMENT', "
                "'NEEDS_REVISION', 'TRACKING', "
                "'PENDING_ACCEPTANCE', 'ACCEPTED', 'AGGREGATED', 'ARCHIVED', "
                "'REJECTED', 'BLOCKED'))"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS action_item_assignments (
                    assignment_id TEXT PRIMARY KEY,
                    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                    definition_version INTEGER NOT NULL CHECK (definition_version > 0),
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                    assignment_role TEXT NOT NULL CHECK (
                        assignment_role IN ('OWNER', 'COLLABORATOR')
                    ),
                    response_status TEXT NOT NULL CHECK (
                        response_status IN ('PENDING', 'ACCEPTED', 'RETURNED', 'SUPERSEDED')
                    ),
                    assignment_message TEXT NOT NULL DEFAULT '',
                    response_message TEXT,
                    assigned_sim_time TIMESTAMPTZ NOT NULL,
                    responded_sim_time TIMESTAMPTZ,
                    source_message_id TEXT NOT NULL,
                    UNIQUE (action_item_id, definition_version, actor_id)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS action_item_assignment_actor_lookup "
                "ON action_item_assignments(actor_id, response_status, action_item_id)"
            )
            cursor.execute(
                "ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS "
                "review_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'"
            )
            cursor.execute(
                "ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS "
                "reviewed_by TEXT REFERENCES actors(actor_id)"
            )
            cursor.execute(
                "ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS "
                "reviewed_sim_time TIMESTAMPTZ"
            )
            cursor.execute(
                "ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS "
                "review_comment TEXT"
            )
            for column, definition in (
                ("submitted_by_actor_id", "TEXT REFERENCES actors(actor_id)"),
                ("attachment_extractions", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                ("source_manifest", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("processing_status", "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'"),
                ("processing_result", "JSONB"),
                ("processing_metadata", "JSONB"),
                ("processing_error_code", "TEXT"),
                ("processing_error_stage", "TEXT"),
                ("processing_error_detail", "TEXT"),
                ("processing_retryable", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("processing_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("processing_updated_sim_time", "TIMESTAMPTZ"),
                ("processing_next_attempt_at", "TIMESTAMPTZ"),
            ):
                cursor.execute(
                    f"ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS "
                    f"{column} {definition}"
                )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS accepted_task_results (
                    accepted_task_result_id TEXT PRIMARY KEY,
                    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
                    accepted_version_id TEXT NOT NULL UNIQUE REFERENCES artifact_versions(version_id),
                    completed_content_refs JSONB NOT NULL,
                    completion_report TEXT NOT NULL,
                    normalized_result JSONB,
                    source_manifest JSONB NOT NULL,
                    processing_metadata JSONB NOT NULL,
                    collaboration_report JSONB,
                    collaboration_report_status TEXT NOT NULL DEFAULT 'PENDING',
                    collaboration_report_error TEXT,
                    accepted_by TEXT NOT NULL REFERENCES actors(actor_id),
                    accepted_sim_time TIMESTAMPTZ NOT NULL
                )
                """
            )
            for column, definition in (
                ("collaboration_report", "JSONB"),
                (
                    "collaboration_report_status",
                    "TEXT NOT NULL DEFAULT 'PENDING'",
                ),
                ("collaboration_report_error", "TEXT"),
            ):
                cursor.execute(
                    f"ALTER TABLE accepted_task_results ADD COLUMN IF NOT EXISTS "
                    f"{column} {definition}"
                )
            # Everything written before the A/B split was system-proposed.
            # IF EXISTS because a fresh database creates the table below with
            # the column already in place.
            cursor.execute(
                "ALTER TABLE IF EXISTS collaboration_memories ADD COLUMN "
                "IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'SYSTEM_OBSERVED'"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS collaboration_memories (
                    memory_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
                    topic TEXT NOT NULL,
                    value JSONB NOT NULL,
                    visibility TEXT NOT NULL CHECK (visibility = 'PRIVATE'),
                    status TEXT NOT NULL CHECK (
                        status IN ('PRIVATE_DRAFT', 'CONFIRMED', 'REJECTED', 'SUPERSEDED')
                    ),
                    evidence_refs JSONB NOT NULL,
                    created_sim_time TIMESTAMPTZ NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    confirmed_by TEXT REFERENCES actors(actor_id),
                    confirmed_sim_time TIMESTAMPTZ,
                    origin TEXT NOT NULL DEFAULT 'SYSTEM_OBSERVED',
                    supersedes_memory_id TEXT REFERENCES collaboration_memories(memory_id)
                )
                """
            )
            cursor.execute(
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
                    created_sim_time TIMESTAMPTZ NOT NULL,
                    CHECK (upstream_action_item_id <> downstream_action_item_id),
                    UNIQUE (upstream_action_item_id, downstream_action_item_id)
                )
                """
            )
            # The P1 participation table was first shipped as
            # `action_item_contributions`, which collides with the P0 concept of
            # a collaborator's ArtifactVersion contribution. It actually holds
            # BALLOT/VOTE/FEEDBACK inputs, so it is renamed in place; existing
            # rows and identifiers are preserved.
            cursor.execute(
                "SELECT to_regclass('action_item_contributions') AS legacy, "
                "to_regclass('action_item_participation_inputs') AS renamed"
            )
            names = cursor.fetchone()
            legacy, renamed = names["legacy"], names["renamed"]
            if legacy and renamed:
                raise RuntimeError(
                    "both action_item_contributions and "
                    "action_item_participation_inputs exist; resolve the "
                    "duplicate before starting the workbench"
                )
            if legacy:
                cursor.execute(
                    "ALTER TABLE action_item_contributions "
                    "RENAME TO action_item_participation_inputs"
                )
            cursor.execute(
                """
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
                    payload JSONB,
                    payload_ref TEXT,
                    source_span TEXT NOT NULL,
                    source_message_id TEXT,
                    submitted_sim_time TIMESTAMPTZ,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    UNIQUE (action_item_id, actor_id, contribution_type)
                )
                """
            )
            cursor.execute(
                "ALTER TABLE artifact_versions DROP CONSTRAINT IF EXISTS "
                "artifact_versions_review_status_check"
            )
            cursor.execute(
                "ALTER TABLE artifact_versions ADD CONSTRAINT "
                "artifact_versions_review_status_check CHECK (review_status IN "
                "('NOT_REQUIRED', 'PENDING', 'ACCEPTED', 'REJECTED'))"
            )
            cursor.execute(
                "ALTER TABLE approvals DROP CONSTRAINT IF EXISTS "
                "approvals_approval_type_check"
            )
            cursor.execute(
                "ALTER TABLE approvals ADD CONSTRAINT approvals_approval_type_check "
                "CHECK (approval_type IN "
                "('L3_INTERVENTION', 'FINAL_RELEASE', 'DEADLINE_CHANGE'))"
            )

    def reset_project_data(self) -> None:
        database = self.one("SELECT current_database() AS name")
        if not database or database["name"] != "colwork_agent":
            raise RuntimeError("refusing to reset a database other than colwork_agent")
        with self.transaction() as cursor:
            cursor.execute(
                "TRUNCATE TABLE organizations, audit_events, inbound_receipts, "
                "mock_im_messages CASCADE"
            )

    @contextmanager
    def transaction(self) -> Iterator[PostgresCursor]:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                yield PostgresCursor(cursor)

    def one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(_postgres_sql(sql), params)
            return _normalize_row(cursor.fetchone())

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(_postgres_sql(sql), params)
            return [_normalize_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    def append_audit(
        self,
        cursor: PostgresCursor,
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
    def json(row: dict[str, Any], field: str) -> Any:
        return json.loads(row[field])
