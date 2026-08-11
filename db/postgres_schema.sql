-- PostgreSQL 18 target schema for the P0 modular monolith.
-- Application-generated TEXT identifiers keep fixtures deterministic and portable.

BEGIN;

CREATE TABLE organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE actors (
    actor_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('SIMULATED_USER', 'HUMAN_USER', 'AGENT')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE'))
);

CREATE TABLE episodes (
    episode_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    run_id TEXT NOT NULL UNIQUE,
    content_pack_id TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT', 'ACTIVE', 'READY_FOR_FINAL_APPROVAL', 'APPROVED',
        'ARCHIVED', 'BLOCKED', 'CANCELLED'
    )),
    transcript TEXT NOT NULL,
    current_sim_time TIMESTAMPTZ NOT NULL,
    created_sim_time TIMESTAMPTZ NOT NULL,
    evaluation_cutoff_sim_time TIMESTAMPTZ NOT NULL,
    current_final_deliverable_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
);
CREATE INDEX active_episode_lookup
    ON episodes (organization_id, status) WHERE status = 'ACTIVE';

CREATE TABLE episode_participants (
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    role TEXT NOT NULL CHECK (role IN (
        'COORDINATOR', 'PARTICIPANT',
        'AGGREGATOR', 'ACTION_OWNER', 'MANAGER'
    )),
    PRIMARY KEY (episode_id, actor_id, role)
);

CREATE TABLE action_items (
    action_item_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    identity_key TEXT NOT NULL,
    title TEXT NOT NULL,
    deliverable_key TEXT NOT NULL,
    owner_actor_id TEXT REFERENCES actors(actor_id),
    required BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_CONFIRMATION', 'PENDING_ASSIGNMENT', 'NEEDS_REVISION',
        'TRACKING', 'PENDING_ACCEPTANCE', 'ACCEPTED', 'AGGREGATED',
        'ARCHIVED', 'REJECTED', 'BLOCKED'
    )),
    deadline_sim_time TIMESTAMPTZ,
    team_required_by_sim_time TIMESTAMPTZ,
    sla_id TEXT NOT NULL,
    active_commitment_revision_id TEXT,
    current_valid_version_id TEXT,
    source_message_id TEXT NOT NULL,
    source_span TEXT NOT NULL,
    proposal_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_sim_time TIMESTAMPTZ,
    created_sim_time TIMESTAMPTZ NOT NULL,
    confirmed_sim_time TIMESTAMPTZ,
    last_owner_signal_at TIMESTAMPTZ,
    last_owner_signal_type TEXT,
    last_owner_signal_valid_until TIMESTAMPTZ,
    last_owner_signal_actor_id TEXT REFERENCES actors(actor_id),
    definition_version INTEGER NOT NULL DEFAULT 1 CHECK (definition_version > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (episode_id, identity_key),
    UNIQUE (episode_id, deliverable_key)
);

CREATE TABLE review_hints (
    hint_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'MATERIALIZED', 'DISMISSED')),
    source_timestamp TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    hint_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    materialized_action_item_id TEXT REFERENCES action_items(action_item_id),
    created_sim_time TIMESTAMPTZ NOT NULL,
    resolved_sim_time TIMESTAMPTZ,
    resolved_by_actor_id TEXT REFERENCES actors(actor_id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (episode_id, candidate_id)
);
CREATE INDEX review_hint_status_lookup
    ON review_hints (episode_id, status, created_sim_time);

CREATE TABLE action_item_assignments (
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
    UNIQUE(action_item_id, definition_version, actor_id)
);
CREATE INDEX action_item_assignment_actor_lookup
ON action_item_assignments(actor_id, response_status, action_item_id);

CREATE TABLE commitment_revisions (
    commitment_revision_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    deliverable_key TEXT NOT NULL,
    promised_deadline_sim_time TIMESTAMPTZ NOT NULL,
    source_message_id TEXT NOT NULL,
    source_span TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'REJECTED')),
    created_sim_time TIMESTAMPTZ NOT NULL,
    supersedes_revision_id TEXT REFERENCES commitment_revisions(commitment_revision_id),
    UNIQUE (action_item_id, revision_no)
);
CREATE UNIQUE INDEX one_active_commitment
    ON commitment_revisions (action_item_id) WHERE status = 'ACTIVE';

CREATE TABLE assistance_requests (
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
);

CREATE TABLE artifact_versions (
    version_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    submitted_by_actor_id TEXT REFERENCES actors(actor_id),
    received_sim_time TIMESTAMPTZ NOT NULL,
    received_sequence BIGINT NOT NULL UNIQUE CHECK (received_sequence > 0),
    payload JSONB NOT NULL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('PENDING', 'PASSED', 'FAILED')),
    validation_errors JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_message_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_version_id TEXT REFERENCES artifact_versions(version_id),
    review_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED' CHECK (
        review_status IN ('NOT_REQUIRED', 'PENDING', 'ACCEPTED', 'REJECTED')
    ),
    reviewed_by TEXT REFERENCES actors(actor_id),
    reviewed_sim_time TIMESTAMPTZ,
    review_comment TEXT,
    attachment_extractions JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    processing_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED' CHECK (
        processing_status IN ('NOT_REQUIRED', 'PENDING', 'PROCESSING', 'READY', 'RETRY_WAIT', 'FAILED')
    ),
    processing_result JSONB,
    processing_metadata JSONB,
    processing_error_code TEXT,
    processing_error_stage TEXT,
    processing_error_detail TEXT,
    processing_retryable BOOLEAN NOT NULL DEFAULT FALSE,
    processing_attempt_count INTEGER NOT NULL DEFAULT 0,
    processing_updated_sim_time TIMESTAMPTZ,
    -- Wall-clock retry gate. VirtualClock sim_time does not advance in the live
    -- workbench, so a simulated backoff deadline would never expire.
    processing_next_attempt_at TIMESTAMPTZ,
    UNIQUE (action_item_id, content_hash)
);

CREATE TABLE accepted_task_results (
    accepted_task_result_id TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    accepted_version_id TEXT NOT NULL UNIQUE REFERENCES artifact_versions(version_id),
    completed_content_refs JSONB NOT NULL,
    completion_report TEXT NOT NULL,
    normalized_result JSONB,
    source_manifest JSONB NOT NULL,
    processing_metadata JSONB NOT NULL,
    collaboration_report JSONB,
    collaboration_report_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        collaboration_report_status IN ('PENDING', 'READY', 'FAILED')
    ),
    collaboration_report_error TEXT,
    accepted_by TEXT NOT NULL REFERENCES actors(actor_id),
    accepted_sim_time TIMESTAMPTZ NOT NULL
);

CREATE TABLE collaboration_memories (
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
    -- SYSTEM_OBSERVED entries are proposed from audit facts and confirmed by
    -- their subject; SELF_DECLARED entries describe what someone wants from
    -- others, which the system must never infer.
    origin TEXT NOT NULL DEFAULT 'SYSTEM_OBSERVED'
        CHECK (origin IN ('SYSTEM_OBSERVED', 'SELF_DECLARED')),
    supersedes_memory_id TEXT REFERENCES collaboration_memories(memory_id)
);

CREATE TABLE action_item_dependencies (
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
);

-- Proposed continuity between an action item and one from an earlier meeting.
-- A PROPOSED row changes no task state; only a person moves it to CONFIRMED.
-- The UNIQUE pair makes re-running the proposer a no-op, so a link somebody
-- already rejected is never resurrected.
CREATE TABLE action_item_links (
    link_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    prior_action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    relation TEXT NOT NULL CHECK (relation IN ('CONTINUATION', 'DUPLICATE')),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')),
    source TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION,
    proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    proposed_sim_time TEXT NOT NULL,
    decided_by_actor_id TEXT REFERENCES actors(actor_id),
    decided_sim_time TEXT,
    CHECK (action_item_id <> prior_action_item_id),
    UNIQUE(action_item_id, prior_action_item_id)
);
CREATE INDEX action_item_link_lookup
ON action_item_links(action_item_id, status);

CREATE TABLE action_item_participation_inputs (
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
);

CREATE TABLE interventions (
    intervention_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    target_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    level TEXT NOT NULL CHECK (level IN ('L1', 'L2', 'L3')),
    action_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    social_cost INTEGER NOT NULL CHECK (social_cost >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'PLANNED', 'PENDING_APPROVAL', 'DELIVERED', 'SUPERSEDED', 'FAILED'
    )),
    effect_id TEXT NOT NULL UNIQUE,
    outbox_id TEXT,
    correlation_id TEXT NOT NULL,
    created_sim_time TIMESTAMPTZ NOT NULL
);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT REFERENCES action_items(action_item_id),
    approval_type TEXT NOT NULL CHECK (approval_type IN (
        'L3_INTERVENTION', 'FINAL_RELEASE', 'DEADLINE_CHANGE'
    )),
    effect_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'EXECUTED', 'FAILED'
    )),
    requested_action JSONB NOT NULL,
    requested_by TEXT NOT NULL,
    requested_sim_time TIMESTAMPTZ NOT NULL,
    decided_by TEXT REFERENCES actors(actor_id),
    decided_sim_time TIMESTAMPTZ,
    execution_outbox_id TEXT,
    correlation_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (
        (status = 'PENDING' AND decided_by IS NULL AND decided_sim_time IS NULL)
        OR status <> 'PENDING'
    )
);

CREATE TABLE outbox_entries (
    outbox_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    action_item_id TEXT REFERENCES action_items(action_item_id),
    effect_type TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'CLAIMED', 'RETRY_WAIT', 'DELIVERED', 'DEAD_LETTER'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at_sim_time TIMESTAMPTZ NOT NULL,
    claimed_by_session TEXT,
    claimed_at_real TIMESTAMPTZ,
    external_receipt_id TEXT,
    last_error_code TEXT,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    created_sim_time TIMESTAMPTZ NOT NULL,
    delivered_sim_time TIMESTAMPTZ,
    CHECK ((status = 'DELIVERED') = (delivered_sim_time IS NOT NULL))
);
CREATE INDEX outbox_dispatch_queue
    ON outbox_entries (available_at_sim_time, created_sim_time)
    WHERE status IN ('PENDING', 'RETRY_WAIT');

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence_no BIGINT NOT NULL CHECK (sequence_no > 0),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL DEFAULT 1 CHECK (event_version > 0),
    sim_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    recorded_at_real TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, sequence_no)
);

CREATE TABLE final_deliverables (
    final_deliverable_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_APPROVAL', 'APPROVED', 'RELEASED', 'SUPERSEDED'
    )),
    generated_sim_time TIMESTAMPTZ NOT NULL,
    approved_sim_time TIMESTAMPTZ,
    payload JSONB NOT NULL,
    schema_version TEXT NOT NULL,
    supersedes_final_deliverable_id TEXT REFERENCES final_deliverables(final_deliverable_id),
    UNIQUE (episode_id, revision_no)
);

CREATE TABLE final_field_lineage (
    final_deliverable_id TEXT NOT NULL REFERENCES final_deliverables(final_deliverable_id),
    field_path TEXT NOT NULL,
    action_item_id TEXT NOT NULL REFERENCES action_items(action_item_id),
    version_id TEXT NOT NULL REFERENCES artifact_versions(version_id),
    source_field_path TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    PRIMARY KEY (final_deliverable_id, field_path)
);

CREATE TABLE inbound_receipts (
    message_id TEXT PRIMARY KEY,
    accepted_sequence BIGINT NOT NULL UNIQUE CHECK (accepted_sequence > 0),
    processed_result JSONB NOT NULL,
    accepted_sim_time TIMESTAMPTZ NOT NULL
);

CREATE TABLE mock_im_messages (
    external_message_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender_actor_id TEXT NOT NULL,
    recipient_actor_ids JSONB NOT NULL,
    effect_type TEXT NOT NULL,
    content TEXT NOT NULL,
    accepted_sequence BIGINT NOT NULL UNIQUE CHECK (accepted_sequence > 0),
    accepted_sim_time TIMESTAMPTZ NOT NULL
);

ALTER TABLE episodes ADD CONSTRAINT episodes_current_final_fk
    FOREIGN KEY (current_final_deliverable_id)
    REFERENCES final_deliverables(final_deliverable_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE action_items ADD CONSTRAINT action_items_active_commitment_fk
    FOREIGN KEY (active_commitment_revision_id)
    REFERENCES commitment_revisions(commitment_revision_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE action_items ADD CONSTRAINT action_items_current_valid_version_fk
    FOREIGN KEY (current_valid_version_id)
    REFERENCES artifact_versions(version_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE interventions ADD CONSTRAINT interventions_outbox_fk
    FOREIGN KEY (outbox_id) REFERENCES outbox_entries(outbox_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE approvals ADD CONSTRAINT approvals_execution_outbox_fk
    FOREIGN KEY (execution_outbox_id) REFERENCES outbox_entries(outbox_id) DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();


-- A shape the meeting decided on, rather than a set of tasks wired together.
-- Its stages alternate between everybody and one person, and that alternation
-- is what the earlier dependency-based version could not express: there was no
-- stage at which somebody was asked to fill in their own options.
CREATE TABLE compound_tasks (
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
    member_actor_ids JSONB NOT NULL,
    selection_count INTEGER,
    source_span TEXT NOT NULL,
    created_sim_time TIMESTAMPTZ NOT NULL,
    stage_entered_sim_time TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0)
);

-- One person's answer at one stage. Options are rows here rather than text to
-- be parsed later: a person typing one option per line already knows where
-- they end, and asking a model to rediscover that is a step that can only
-- lose information.
CREATE TABLE compound_task_inputs (
    input_id TEXT PRIMARY KEY,
    compound_task_id TEXT NOT NULL REFERENCES compound_tasks(compound_task_id),
    stage TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    payload JSONB NOT NULL,
    source_message_id TEXT NOT NULL,
    created_sim_time TIMESTAMPTZ NOT NULL,
    UNIQUE (compound_task_id, stage, actor_id)
);

COMMIT;
