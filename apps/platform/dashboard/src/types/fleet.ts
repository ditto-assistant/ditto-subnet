// Fleet report, operations snapshot, health, and validator-name wire shapes
// (/public/operations, /public/screeners, /public/health,
// /public/validator-names).

import type { BenchmarkProgress, PipelineFeed } from "./pipeline";

export interface StackIdentity {
  provenance?: string | null;
  image_digest?: string | null;
  source_revision?: string | null;
  version?: string | null;
}

export interface StackComponentHealth {
  /** "healthy" | "degraded" | "unreachable" | "identity_mismatch" | "unknown". */
  health?: string | null;
  required?: boolean | null;
  ready?: boolean | null;
  model_ready?: boolean | null;
  /** Unix seconds. */
  observed_at?: number | null;
  observed_identity?: StackIdentity | null;
}

/** What the validator's own probe of its scorer actually saw. The status
 * above is the validator's conclusion; this separates a scorer that never
 * answered from one that answered with something unusable. */
export interface ScorerProbe {
  /** "served" | "served_degraded" | "http_error" | "unreadable" | "timeout" |
   * "connect_error" | "not_probed". */
  outcome?: string;
  /** Unix seconds. */
  observed_at?: number | null;
  http_status?: number | null;
  reason?: string | null;
  /** Unix seconds; null means "not since this validator started". */
  last_served_at?: number | null;
  consecutive_failures?: number | null;
}

export interface ScorerBenchmarks {
  /** "fresh_verified" | "legacy_v2" | "unreachable" | "identity_mismatch". */
  status?: string;
  supported_bench_versions?: Array<number | string>;
  /** Unix seconds. */
  observed_at?: number | null;
  software_version?: string | null;
  source_revision?: string | null;
  probe?: ScorerProbe | null;
}

export interface ValidatorCapabilities {
  screened_images?: boolean | null;
  require_screened_image?: boolean | null;
  source_build_fallback?: boolean | null;
  full_stack_managed?: boolean | null;
  stack_updater?: boolean | null;
  sandbox_egress_restricted?: boolean | null;
  executor_isolation?: string | null;
  scorer_benchmarks?: ScorerBenchmarks | null;
}

export interface ValidatorStack {
  /** "managed" (signed GHCR release) or source build. */
  mode?: string;
  compose_schema?: number | string;
  release_descriptor_digest?: string | null;
  components?: Record<string, StackIdentity | null | undefined>;
}

export type ValidatorUpdaterPhase =
  | "prepared"
  | "drained"
  | "old_stopped"
  | "candidate_started"
  | "committed"
  | "rollback_pending"
  | "rollback_ready";

export type ValidatorUpdaterState =
  | "not_managed"
  | "disabled"
  | "unavailable"
  | "idle"
  | "prefetched"
  | "draining"
  | "replacing"
  | "verifying"
  | "rollback"
  | "backoff"
  | "retry_ready"
  | "suppressed";

/** Privacy-safe managed updater telemetry signed into heartbeat protocol v23. */
export interface ValidatorUpdaterStatus {
  enabled: boolean;
  channel?: "compat-2" | null;
  state: ValidatorUpdaterState;
  transaction_phase?: ValidatorUpdaterPhase | null;
  current_descriptor?: string | null;
  current_version?: string | null;
  candidate_descriptor?: string | null;
  candidate_version?: string | null;
  failed_candidate_count: number;
  /** Unix seconds. */
  retry_after?: number | null;
  suppressed: boolean;
  /** Unix seconds. */
  last_success_at?: number | null;
  /** Unix seconds. */
  last_failure_at?: number | null;
  last_failure_reason?: string | null;
  /** Unix seconds. */
  observed_at: number;
}

export interface SystemMetrics {
  cpu_percent: number;
  memory_percent: number;
  disk_percent: number;
  /** "healthy" | "degraded" | anything else reads "Not reported". */
  docker_status?: string | null;
  running_containers?: number | null;
  unhealthy_containers?: number | null;
}

export interface ScreeningProgress {
  /** "preparing" | "downloading" | … | "source_review_<n>". */
  stage?: string;
  started_at?: string;
}

export interface ConfirmationSubject {
  agent_id: string;
  agent_name: string;
}

/** Independent LongMemEval + ablation confirmation work. It is deliberately
 * excluded from ordinary slot capacity and active_benchmarks. */
export interface ConfirmationProgress {
  bundle_id: string;
  slot_id: string;
  bench_version: 9 | 10 | 11 | 12;
  mode: "shadow" | "enforce";
  profile_revision: string;
  attempt: number;
  issued_at: string;
  deadline: string;
  stage?:
    | "preparing"
    | "running_confirmation"
    | "finalizing"
    | "submitting_result"
    | "failed_retrying"
    | null;
  completed?: number | null;
  total?: number | null;
  reported_agent_id?: string | null;
  progress_reported_at?: string | null;
  subjects: ConfirmationSubject[];
}

/** One fleet row. Validators are keyed by validator_hotkey; the screener
 * fleet shares one hotkey, so each worker is distinguished by instance_id. */
export interface FleetEntry {
  validator_hotkey?: string;
  screener_hotkey?: string;
  instance_id?: string | null;
  /** Worker state: "polling" | "running_benchmark" | "screening" | "idle" | … */
  state?: string | null;
  /** "available" | "stale" | "offline" | "paused". */
  availability?: string | null;
  /** "healthy" | "warning". */
  health?: string | null;
  /** "assignment_mismatch" | "assigning" | "heartbeat_stale". */
  assignment_state?: string | null;
  assigned_agent_id?: string | null;
  assigned_agent_name?: string | null;
  reported_agent_id?: string | null;
  active_agent_id?: string | null;
  active_agent_name?: string | null;
  screening_progress?: ScreeningProgress | null;
  software_version?: string | null;
  protocol_version?: number | string;
  /** Screeners only. */
  policy_version?: number | string | null;
  first_seen_at?: string | null;
  reported_at?: string | null;
  seen_at?: string | null;
  active_benchmark?: BenchmarkProgress | null;
  active_benchmarks?: BenchmarkProgress[];
  assigned_benchmarks?: BenchmarkProgress[];
  confirmation_benchmarks?: ConfirmationProgress[];
  healthy_slots?: string[];
  configured_slots?: number | null;
  /** Backroom refuses new work for this exact validator; live leases may drain. */
  issuance_paused?: boolean;
  /** Ordinary slots currently funded by Platform policy. Confirmation slots are separate. */
  allowed_slots?: number | null;
  /** "accepting" or a warn label. */
  admission?: string | null;
  capabilities?: ValidatorCapabilities | null;
  stack?: ValidatorStack | null;
  updater_status?: ValidatorUpdaterStatus | null;
  stack_health?: Record<string, StackComponentHealth | null | undefined> | null;
  system_metrics?: SystemMetrics | null;
}

/** /public/screeners, and the validators slice of /public/operations. */
export interface FleetReport {
  validators?: FleetEntry[];
  screeners?: FleetEntry[];
  reported_count?: number;
  generated_at?: string;
}

// ── Operations snapshot (/public/operations) ─────────────────

export type SubmissionImageBuildStatus =
  | "queued"
  | "leased"
  | "running"
  | "succeeded"
  | "fallback_required"
  | "canceled"
  | "consumed";

export interface SubmissionImageBuild {
  agent_id: string;
  agent_name?: string | null;
  agent_version?: number | null;
  status: SubmissionImageBuildStatus;
  provider?: "targon" | "gcp" | null;
  attempt_count: number;
  output_sha256?: string | null;
  output_size_bytes?: number | null;
  error_code?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  consumed_at?: string | null;
  updated_at: string;
}

export interface SubmissionImageBuildSnapshot {
  window_hours: number;
  active_count: number;
  targon_completed_count: number;
  fallback_authorized_count: number;
  builds: SubmissionImageBuild[];
}

export interface OperationsPayload {
  active_bench_version?: number | null;
  desired_bench_version?: number | null;
  benchmark_rollout_status?: string | null;
  validators: FleetReport;
  activity?: PipelineFeed;
  submission_builds?: SubmissionImageBuildSnapshot;
  generated_at?: string;
}

// ── Health (/public/health) ──────────────────────────────────

export interface HealthPayload {
  miners?: number;
  scored_miners?: number | null;
  scored_agents?: number | null;
  total_scores?: number | null;
  scores_24h?: number | null;
  avg_latency_ms?: number | null;
  last_scored_at?: string | null;
  generated_at?: string;
}

// ── Validator names (/public/validator-names) ────────────────

export interface ValidatorNameEntry {
  validator_hotkey?: string;
  display_name?: string;
  stake_weight?: number;
}

export interface ValidatorNamesPayload {
  validators?: ValidatorNameEntry[];
}
