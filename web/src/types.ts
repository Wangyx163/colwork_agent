export type RunSummary = {
  run_id: string;
  episode_id: string;
  status: string;
  created_sim_time: string;
  events: number;
};

export type AuditLane = {
  name: string;
  count: number;
  events: { seq: number; event: string }[];
};

export type LineageVersion = {
  version_id: string;
  action_item_id: string;
  title: string;
  received_sequence: number;
  review_status: string;
  superseded: boolean;
  field_count: number;
  contributed: boolean;
};

export type TokenSummary = {
  calls: number;
  total_tokens: number;
  median: number | null;
  p25: number | null;
  p75: number | null;
  iqr: number | null;
  min?: number;
  max?: number;
  outliers: number[];
  note?: string;
};

export type Observatory = {
  schema_version: string;
  run: { run_id: string; episode_id: string };
  runs: RunSummary[];
  headline: {
    duplicate_sends: number;
    deduplicated: number;
    delivered: number;
    human_overruled: number | null;
    model_advised: number | null;
    citation_hallucination_rate: number | null;
    conclusion_points: number | null;
    audit_events: number;
  };
  outbox: Record<string, number>;
  results: Record<string, number>;
  constitution_guards?: {
    authorization_rejected: number | null;
    authentication_rejected: number | null;
    missing_manifest: number | null;
    field_violations: number | null;
    principal_violations: number | null;
    binary_leaks: number | null;
    input_hash_mismatches: number | null;
  };
  human_gates: Record<string, number | null>;
  citations: Record<string, unknown>;
  audit: { total: number; first: number; last: number; lanes: AuditLane[] };
  tokens: {
    calls: { sequence_no: number; purpose: string; total_tokens: number }[];
    summary: TokenSummary;
  };
  lineage: {
    final_deliverable_id: string | null;
    fields: { field_path: string; version_id: string; title: string }[];
    versions: LineageVersion[];
  };
};
