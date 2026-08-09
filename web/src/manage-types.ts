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
}

export interface Assignment {
  assignment_id: string;
  action_item_id: string;
  assignee_actor_id: string;
  assignee_display_name?: string;
  role?: string;
  status: string;
  definition_version?: number;
}

export interface AssistanceRequest {
  assistance_id: string;
  action_item_id: string;
  status: string;
  summary?: string;
  requested_by_actor_id?: string;
  requested_sim_time?: string;
}

export interface Notice {
  notice_id: string;
  kind: string;
  action_item_id: string;
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
    sections?: { heading?: string; body?: string; source_version_id?: string }[];
    conclusions?: string[];
    risks?: string[];
    [key: string]: unknown;
  } | null;
  release_review?: Record<string, unknown> | null;
}

export interface MeetingProgress {
  total: number;
  pending_assignment: number;
  needs_revision: number;
  tracking: number;
  pending_acceptance: number;
  completed: number;
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
}

export interface ManageState {
  episode: Record<string, unknown> & { episode_id: string; title?: string };
  vocabulary: Vocabulary;
  tasks: Task[];
  notices: Notice[];
  pending_approvals: Approval[];
  /** Null until an episode has produced one -- a meeting still in flight has
   *  no final deliverable, and typing it non-null hides that from the caller. */
  final: FinalDeliverable | null;
  meeting_progress: MeetingProgress;
  participants: Participant[];
  principal: { actor_id: string; roles: string[]; episode_id?: string };
  allowed_surfaces: string[];
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
