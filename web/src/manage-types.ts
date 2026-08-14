import type { CompoundTask } from "./tasks/CompoundPanel";
/** Shapes taken from a real `/api/state?surface=manage` response, not from
 *  the server source: only the fields these panels read are declared, so a
 *  field appearing here means something on screen depends on it. */

export type ActivityKind =
  | "COMMITMENT"
  | "PROGRESS"
  | "STATUS"
  | "DELIVERY"
  | "CONTRIBUTION"
  | "INTERVENTION"
  | "ASSIGNMENT"
  | "REVIEW"
  | "ASSISTANCE";

export interface Activity {
  kind: ActivityKind;
  title: string;
  detail: string;
  status: string;
  actor: string;
  actor_id: string;
  sim_time: string;
  sequence_no?: number;
  /** Present on COMMITMENT only. Kept apart from `detail` so the schedule bar
   *  never has to parse a date back out of a sentence. */
  promised_deadline_sim_time?: string;
  revision_no?: number;
}

/** Field names taken from a real row, not guessed: the delivered text lives
 *  inside `payload`, and the timestamps are `received_*` rather than
 *  `submitted_*`. Reading the wrong one renders an empty card that looks like
 *  a person submitted nothing. */
export interface Version {
  version_id: string;
  action_item_id: string;
  received_sequence?: number | string;
  received_sim_time?: string;
  payload?: {
    summary?: string;
    content?: string;
    links?: string[];
    files?: { name?: string }[];
    /** What the submitter said about finishing. Real deliveries often carry
     *  this and an empty `content`, so a card that only reads `content`
     *  reports an empty submission for a complete one. */
    completion_note?: string;
  } | null;
  validation_status?: string;
  review_status?: string;
  review_comment?: string | null;
  submitted_by_display_name?: string;
  attachment_extractions?: {
    name?: string;
    status?: string;
    text_characters?: number;
  }[];
  processing_status?: string;
  processing_error_detail?: string | null;
  processing_retryable?: boolean;
  /** What the per-version processor made of this submission.
   *
   *  Produced automatically between submission and acceptance by the prompt
   *  in task_result_processing.py, which reads the attachments' extracted
   *  text. Every point it states carries the source it came from, and it
   *  recommends without deciding -- acceptance stays with a person. */
  processing_result?: TaskResultReview | null;
  /** Only on a contribution version. AWAITING_OWNER is the outstanding one --
   *  read off a real payload, because `review_status` is a different field
   *  with different values and filtering on it silently matches nothing. */
  contribution_status?:
    | "AWAITING_OWNER"
    | "INCLUDED"
    | "REVISION_REQUESTED"
    | "PROMOTED";
  can_request_revision?: boolean;
}

export interface SourcedPoint {
  text: string;
  source_refs?: string[];
}

export interface TaskResultReview {
  task_interpretation?: string;
  task_alignment?: {
    status: "ALIGNED" | "PARTIAL" | "MISALIGNED" | "INSUFFICIENT";
    reason?: string;
    confidence?: number;
  };
  evidence_digest?: {
    title?: string;
    summary?: string;
    key_points?: SourcedPoint[];
  } | null;
  /** Null on purpose when the evidence does not support the task: an
   *  unsupported deliverable must not be written as though it existed. */
  normalized_result?: {
    title?: string;
    executive_summary?: string;
    key_points?: SourcedPoint[];
  } | null;
  gaps?: {
    issue: string;
    severity: "BLOCKING" | "MAJOR" | "MINOR";
    source_refs?: string[];
  }[];
  acceptance_advice?: { decision: "ACCEPT" | "REVISE"; reasons?: string[] };
  source_coverage?: {
    used_refs?: string[];
    unreadable_or_uninspected?: string[];
  };
}

/** Field names taken from a real payload. An assignment carries
 *  `assignment_role` / `response_status` / `display_name`, not the `role` /
 *  `status` / `assignee_*` that seem natural -- declaring the natural ones
 *  typechecks and then renders blanks. */
export interface Assignment {
  assignment_id: string;
  action_item_id?: string;
  actor_id?: string;
  display_name?: string;
  assignment_role?: "OWNER" | "COLLABORATOR";
  response_status: string;
  response_message?: string;
  definition_version?: number;
}

/** Field names copied out of a real projection, not imagined. The id is
 *  `assistance_request_id` and the asker is `requester_actor_id`; the shorter
 *  spellings that read naturally are not the ones the server sends, and
 *  reading them yields undefined -- which typechecks, renders, and produces a
 *  request to /api/assistance/undefined/acknowledge. */
export interface AssistanceRequest {
  assistance_request_id: string;
  action_item_id: string;
  status: "OPEN" | "ACKNOWLEDGED" | "RESOLVED" | "CANCELLED";
  summary?: string;
  category?: string;
  requester_actor_id?: string;
  requester_display_name?: string;
  target_actor_id?: string;
  target_display_name?: string;
  created_sim_time?: string;
  resolution_summary?: string | null;
  resolved_sim_time?: string | null;
}

export interface Notice {
  notice_id: string;
  kind: string;
  action_item_id: string;
  compound_task_id?: string;
  title: string;
  summary: string;
  fields: { label: string; value: string }[];
  sim_time: string;
  /** True when the notice offers a decision, which the task list already
   *  surfaces -- the bell uses this to avoid showing the same ask twice. */
  decides: boolean;
}

export interface MyAssignment {
  assignment_id: string;
  assignment_role: "OWNER" | "COLLABORATOR";
  response_status: string;
  assignment_message?: string;
  definition_version?: number;
}

export interface Contribution {
  contribution_type: "BALLOT" | "VOTE";
  actor_id: string;
  status: string;
  payload?: Record<string, unknown> | null;
}

export interface CollaborationProgress {
  structure_type: string;
  selection_count?: number;
  dependencies_ready?: boolean;
  contributions?: Contribution[];
  options?: { option_id: string; text: string }[];
  [key: string]: unknown;
}

export interface Task {
  action_item_id: string;
  title: string;
  status: string;
  required: number;
  owner_actor_id: string;
  owner_display_name: string;
  assigned_owner_display_name: string | null;
  deliverable_key: string;
  deadline_sim_time: string | null;
  team_required_by_sim_time: string | null;
  promised_by_sim_time: string | null;
  schedule_status: string;
  schedule_risk_reason: string;
  definition_version: number;
  version: number;
  source_span: string;
  proposal_metadata?: Record<string, unknown>;
  activity: Activity[];
  assignments: Assignment[];
  current_assignments: Assignment[];
  assistance_requests: AssistanceRequest[];
  active_assistance: AssistanceRequest | null;
  collaborators: { actor_id: string; display_name?: string }[];
  latest_version: Version | null;
  current_version: Version | null;
  /** Versions submitted by collaborators rather than the owner. They are not
   *  deliveries: only the owner can fold one in, ask for changes, or promote
   *  it to be the final candidate. */
  contribution_versions?: Version[];
  accepted_task_result: Record<string, unknown> | null;
  latest_progress: Record<string, unknown> | null;
  /* Present on the participant surface. */
  my_assignment?: MyAssignment | null;
  is_mine?: boolean;
  is_collaborator?: boolean;
  can_contribute?: boolean;
  collaboration_progress?: CollaborationProgress | null;
  last_owner_signal?: {
    signal_type: string;
    signal_at: string;
    valid_until?: string;
    note?: string;
  } | null;
}

export interface Approval {
  approval_id: string;
  subject_id?: string;
  approval_type?: string;
  status?: string;
  summary?: string;
  requested_sim_time?: string;
}

export interface FinalDeliverable {
  final_deliverable_id: string | null;
  revision_no?: number;
  status?: string;
  generated_sim_time?: string | null;
  approved_sim_time?: string | null;
  payload?: {
    organized_report?: OrganizedReport | null;
    /** How the report above was produced. Shown, because "a model wrote this"
     *  and "a template assembled this" are different claims and the reader is
     *  entitled to know which one they are looking at. */
    processing?: {
      mode?: string;
      provider?: string | null;
      model?: string | null;
      prompt_version?: string;
    } | null;
    [key: string]: unknown;
  } | null;
  /** The decision on releasing this final, or null while none has been made.
   *  Typed rather than left loose because the page reads two of its fields and
   *  an `unknown` there is a compile error waiting for whoever renders it. */
  release_review?: {
    approval_id: string;
    status: string;
    comment: string;
    decided_sim_time: string | null;
  } | null;
}

export interface OrganizedReport {
  title?: string;
  executive_summary?: string;
  key_findings?: {
    text: string;
    source_version_ids?: string[];
    source_result_ids?: string[];
  }[];
  sections?: {
    action_item_id?: string;
    heading?: string;
    summary?: string;
    detail?: string;
    links?: string[];
    attachments?: { name?: string }[];
    source_version_id?: string;
  }[];
  risks_or_gaps?: { text?: string; source_version_ids?: string[] }[];
}

export interface MeetingProgress {
  total: number;
  pending_assignment: number;
  needs_revision: number;
  tracking: number;
  pending_acceptance: number;
  completed: number;
}

export interface ReviewHint {
  hint_id: string;
  candidate_id: string;
  status: "OPEN" | "MATERIALIZED" | "DISMISSED";
  source_timestamp: string;
  source_quote: string;
  evidence_text: string;
  reason_code: string;
  materialized_action_item_id?: string | null;
  hint_payload?: {
    kind_hints?: string[];
    facts_found?: string[];
    evidence_unit_ids?: string[];
  };
}

export interface Participant {
  actor_id: string;
  display_name: string;
  roles?: string[];
}

/** Lists the pages must offer verbatim rather than repeat.
 *
 *  A return reason chosen here is read back from a Feishu card, and the domain
 *  refuses any signal or category outside its own set -- so these travel from
 *  the server instead of being written twice. */
export interface Vocabulary {
  return_reasons: string[];
  other_return_reason: string;
  quick_signals: string[];
  assistance_categories: string[];
  max_attachment_count: number;
  max_attachment_bytes: number;
}

export interface ManageState {
  episode: Record<string, unknown> & { episode_id: string; title?: string };
  vocabulary: Vocabulary;
  tasks: Task[];
  review_hints: ReviewHint[];
  compound_tasks?: CompoundTask[];
  notices: Notice[];
  pending_approvals: Approval[];
  /** Scope changes waiting on the coordinator. */
  pending_scope_changes?: {
    request_id: string;
    action_item_id: string;
    proposed_by: string;
    proposed_deliverable: string;
    current_deliverable: string;
    reason: string;
    title: string;
  }[];
  /** Handoffs offered to the reader and not yet answered. */
  pending_handoffs?: {
    handoff_id: string;
    action_item_id: string;
    from_display_name: string;
    assignment_role: string;
    reason: string;
    title: string;
    team_required_by_sim_time: string | null;
  }[];
  /** Null until an episode has produced one -- a meeting still in flight has
   *  no final deliverable, and typing it non-null hides that from the caller. */
  final: FinalDeliverable | null;
  meeting_progress: MeetingProgress;
  participants: Participant[];
  principal: { actor_id: string; roles: string[]; episode_id?: string };
  allowed_surfaces: string[];
  /* The collaboration handbook: what the system has observed about how
     someone works, and the vocabulary they can use to say it themselves. */
  memories?: {
    memory_id: string;
    actor_id: string;
    topic: string;
    status: string;
    value?: { code?: string; statement?: string } | null;
  }[];
  memory_lexicon?: {
    version?: string;
    topics?: {
      topic: string;
      title: string;
      prompt: string;
      origin?: string;
      values: { code: string; label: string; collaborator_hint?: string }[];
    }[];
  };
  aggregator_actor_id: string;
}

/** The four zones, in the order the work actually moves through them. The
 *  numbering on screen is this sequence, not decoration. */
export const ZONES = [
  { id: "tasks", n: "01", name: "任务" },
  { id: "dispatch", n: "02", name: "派发" },
  { id: "review", n: "03", name: "验收" },
  { id: "final", n: "04", name: "终稿" },
] as const;
