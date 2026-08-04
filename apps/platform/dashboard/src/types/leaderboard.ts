// Leaderboard, emissions fold, rollout, chain weights, and consensus-score
// wire shapes (/public/leaderboard, /public/bench/rollout, /public/weights,
// /public/agent/{id}/scores).

// ── Leaderboard (/public/leaderboard) ────────────────────────

/** Client-side annotation: revealed on-chain validator support for one row. */
export interface ChainWeightInfo {
  /** Vectors that assigned this miner any weight. */
  weighted: number;
  /** Vectors whose top revealed choice is this miner. */
  champion: number;
  /** Total revealed miner-bearing vectors in the snapshot. */
  vectors: number;
}

export interface EmissionRecipient {
  agent_id?: string | null;
  miner_hotkey?: string | null;
  /** "champion" | "tail" */
  role?: string;
  share_of_miner_pool?: number;
  shared_seed_confirmations?: number | null;
}

export interface RawLeaderDecision {
  /** "paired" | "unpaired" | anything else reads as the fixed margin. */
  method?: string;
  challenger_lead: number;
  required_lead: number;
}

/** KOTH emissions fold parameters. Consensus constants are always read from
 * here, never hardcoded in copy. */
export interface EmissionsFold {
  margin?: number;
  dethrone_z?: number;
  champion_share?: number;
  tail_size?: number | null;
  rank_shares?: number[];
  champion_miner_hotkey?: string | null;
  champion_agent_id?: string | null;
  raw_leader_agent_id?: string | null;
  raw_leader_decision?: RawLeaderDecision | null;
  recipients?: EmissionRecipient[];
}

export interface PerCategoryScore {
  category: string;
  mean: number;
  count: number;
}

/** Anti-overfit / scoring-integrity telemetry for a run. */
export interface IntegrityTelemetry {
  paraphrase_applied?: number | null;
  paraphrase_attempted?: number | null;
  paraphrase_fallback?: number | null;
  lexical_gap_rewritten?: number | null;
  lexical_gap_questions?: number | null;
  lexical_gap_mean_before?: number | null;
  lexical_gap_mean_after?: number | null;
  capped_tool_cases?: number | null;
  seeding_waves?: number | null;
}

/** How the final composite was assembled from the base accuracy. */
export interface CompositeBreakdown {
  base_accuracy: number;
  benchmark_quality_multiplier: number;
  pre_token_composite: number;
  final_composite: number;
  token_penalty?: number | null;
  token_efficiency_multiplier?: number | null;
  maximum_token_penalty?: number | null;
}

export interface TokenEfficiency {
  observed_total_tokens?: number | null;
  baseline_total_tokens?: number | null;
  budget_percentile?: number;
}

/** One redacted per-case result (never the answer key). */
export interface CaseResult {
  /** "memory" | "tool" */
  kind: string;
  category: string;
  score: number;
  /** Memory cases: binary deterministic verdict. */
  correct?: boolean | null;
  /** Tool cases: continuous trajectory grade. */
  tool_score?: number | null;
  latency_ms?: number | null;
  notes?: string[] | null;
}

export interface LeaderboardEntry {
  miner_hotkey: string;
  agent_id?: string;
  agent_name?: string | null;
  agent_version?: number | null;
  /** Mean settled platform-metered validator lease cost on this score's bench version. */
  average_run_cost_microusd?: number | null;
  /** Settled leases included in average_run_cost_microusd. */
  inference_run_count?: number;
  composite: number;
  /** Score after continual aggregation and any active efficiency fold. */
  official_composite?: number | null;
  /** Score after continual aggregation but before relative efficiency. */
  pre_efficiency_composite?: number | null;
  /** Frozen Bench v7+ upside fraction, when the fold is active. */
  efficiency_bonus?: number | null;
  /** Final folded score; equal to official_composite while the fold is active. */
  effective_composite?: number | null;
  /** Settled active-version median shown mid-rollout (loose != null check). */
  settled_composite?: number | null;
  composite_stderr?: number | null;
  tool_mean: number;
  memory_mean: number;
  median_ms?: number | null;
  /** Cases scored; n >= 100 distinguishes a zero-scoring full run. */
  n?: number | null;
  first_seen?: string;
  bench_version?: number | null;
  /** Missing counts as eligible (older APIs omit it). */
  eligible?: boolean;
  /** Missing counts as finalized. */
  finalized?: boolean;
  /** Strict === true means registered; null/missing is UNKNOWN, not false. */
  registered?: boolean | null;
  emission_eligible?: boolean;
  miner_uid?: number | null;
  score_count?: number;
  score_quorum?: number;
  rollout_score_count?: number | null;
  rollout_composite?: number | null;
  /** Composite trend, oldest first. */
  history?: number[];
  models?: { harness?: string | null; datagen?: string | null };
  per_category?: PerCategoryScore[];
  integrity?: IntegrityTelemetry | null;
  tokens?: number | null;
  dataset_sha256?: string | null;
  calibration_brier?: number | null;
  calibration_n?: number | null;
  transform_robustness?: number | null;
  audit_case_count?: number | null;
  case_results?: CaseResult[];
  token_efficiency?: TokenEfficiency | null;
  composite_breakdown?: CompositeBreakdown | null;
  /** Client-assigned display rank (finalized and provisional tiers count separately). */
  rank?: number | null;
  /** Client-side annotation from the emissions fold. */
  _emission?: EmissionRecipient | null;
  /** Client-side annotation from the chain weights snapshot. */
  _chainWeight?: ChainWeightInfo | null;
}

export interface LeaderboardPayload {
  entries?: LeaderboardEntry[];
  available_bench_versions?: number[];
  active_bench_version?: number | null;
  desired_bench_version?: number | null;
  current_bench_version?: number | null;
  /** "current" | "historical" */
  selection_mode?: string;
  generated_at?: string;
  emissions?: EmissionsFold | null;
  count?: number;
}

// ── Benchmark rollout (/public/bench/rollout) ────────────────

export interface RolloutMember {
  position?: number;
  score_count?: number;
}

export interface RolloutState {
  active_version?: number | null;
  desired_version?: number | null;
  /** "collecting" | "blocked_ineligible" | "superseded" | "activated" | "inactive" */
  status?: string | null;
  ranked_quorum_agents?: number | null;
  min_ranked_quorum_agents?: number | null;
  priority_cohort_size?: number | null;
  priority_complete?: boolean;
  cohort_size?: number | null;
  cohort_ready_count?: number | null;
  members?: RolloutMember[];
  blocked_reason?: string | null;
  qualification_blockers?: string[];
  qualification_converged?: boolean;
  canary_capable_validator_count?: number | null;
}

// ── Chain weights (/public/weights) ──────────────────────────

export interface ChainWeight {
  hotkey: string;
  value: number;
  uid: number;
}

export interface ChainWeightVector {
  weights?: ChainWeight[];
}

export interface ChainWeightsSnapshot {
  vectors?: ChainWeightVector[];
  owner_hotkey?: string | null;
  block?: number;
}

// ── Consensus scores (/public/agent/{id}/scores) ─────────────

export interface ConsensusScore {
  validator_hotkey?: string;
  composite: number;
  bench_version?: number | null;
  composite_breakdown?: CompositeBreakdown | null;
  case_results?: CaseResult[];
}

export interface ScoresPayload {
  scores?: ConsensusScore[];
  quorum?: number | null;
  median_composite?: number | null;
}
