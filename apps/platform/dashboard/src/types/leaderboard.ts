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
  /** Mean share of the revealed miner-weight mass across all miner-bearing
   * vectors (0–1; a vector that omits this miner contributes 0). */
  share: number;
}

export interface EmissionRecipient {
  agent_id?: string | null;
  miner_hotkey?: string | null;
  /** "champion" | "joint_champion" | "tail" */
  role?: string;
  share_of_miner_pool?: number;
  shared_seed_confirmations?: number | null;
}

export interface RawLeaderDecision {
  /** "paired" | "unpaired" | anything else reads as the fixed margin. */
  method?: string;
  challenger_lead: number;
  required_lead: number;
  required_score?: number;
  score_ceiling?: number;
  /** Required score has passed the top of the score domain: nothing can win. */
  ceiling_deadlocked?: boolean;
}

/** KOTH emissions fold parameters. Consensus constants are always read from
 * here, never hardcoded in copy. */
export interface EmissionsFold {
  margin?: number;
  dethrone_z?: number;
  champion_share?: number;
  tail_size?: number | null;
  rank_shares?: number[];
  tie_weighting_active?: boolean;
  tie_weighting_required_protocol?: number;
  /** Protocol 24: cap on how much of the remaining headroom the dethrone band
   * may consume, and whether the fleet has activated it. */
  ceiling_headroom_share?: number;
  ceiling_band_clamp_active?: boolean;
  ceiling_band_clamp_required_protocol?: number;
  champion_miner_hotkey?: string | null;
  champion_agent_id?: string | null;
  raw_leader_agent_id?: string | null;
  raw_leader_decision?: RawLeaderDecision | null;
  /** What the best rival needs. Unlike raw_leader_decision this survives the
   * champion also being the raw leader, which is when readers most want it. */
  champion_defense?: RawLeaderDecision | null;
  allocation_mode?: "ranked" | "score_ceiling_pool";
  score_ceiling_pool_size?: number;
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

export interface LeaderboardFamilyMember {
  agent_id: string;
  agent_name: string;
  agent_version?: number | null;
  canonical_composite: number;
  /** Match against the entry's crown_first_seen to find the generation that
   * supplies the fold anchor. May sit on a different hotkey than the winner. */
  submitted_at?: string | null;
  miner_hotkey?: string | null;
  /** Accepted continual-retest seeds belonging to this exact submission. */
  confirmation_seed_depth?: number;
}

export interface LeaderboardFamily {
  /** Only unranked children; the representative is the containing entry. */
  members: LeaderboardFamilyMember[];
}

export interface LeaderboardEntry {
  miner_hotkey: string;
  agent_id?: string;
  agent_name?: string | null;
  agent_version?: number | null;
  /** Minimal children for the expandable owner-family grouping. */
  submission_family?: LeaderboardFamily | null;
  /** Mean settled platform-metered validator lease cost on this score's bench version. */
  average_run_cost_microusd?: number | null;
  /** Settled leases included in average_run_cost_microusd. */
  inference_run_count?: number;
  composite: number;
  /** Bench v9 confirmation phase. Only full_confirmed can rank in enforce mode. */
  v9_confirmation_status?: "base_only" | "provisional" | "full_confirmed" | null;
  /** Independently verified full v9 composite; absent before confirmation. */
  v9_full_confirmed_composite?: number | null;
  v9_confirmation_evidence_sha256?: string | null;
  /** Authoritative quality and primary ranking key after continual aggregation. */
  official_composite?: number | null;
  /** Score after continual aggregation but before relative efficiency. */
  pre_efficiency_composite?: number | null;
  /** Frozen legacy curve-v1/v2 upside fraction, when that fold is active. */
  efficiency_bonus?: number | null;
  /** Frozen Bench-v9 bounded multiplier; supersedes efficiency_bonus when present. */
  efficiency_factor?: number | null;
  /** True when the surfaced curve-v3 tie-break or legacy fold is active. */
  efficiency_fold_applied?: boolean;
  /** Adjustment projection; curve v3 uses this only after exact quality equality. */
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
  /** The arrival the KOTH fold orders on: the lineage's earliest
   * band-equivalent submission, not this tarball's upload. Earlier than
   * first_seen means a sibling generation supplies it. */
  crown_first_seen?: string | null;
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

export type V9GateResult =
  | "passed"
  | "below_threshold"
  | "zero_inference"
  | "insufficient_evidence"
  | "not_applicable";

export interface V9ModelUseGate {
  administered_cases: number;
  eligible_cases: number;
  successful_inference_cases: number;
  missing_inference_cases: number;
  observed_requests: number;
  successful_requests: number;
  request_coverage_bps: number;
  coverage_bps: number;
  threshold_bps: number;
  result: V9GateResult;
  factor_bps: 0 | 10000;
}

export interface V9AuthoritativeToolGate {
  expected_executions: number;
  matched_executions: number;
  missing_executions: number;
  unexpected_executions: number;
  observed_executions: number;
  coverage_bps: number;
  threshold_bps: number;
  result: V9GateResult;
  factor_bps: 0 | 10000;
}

/** Privacy-safe subset of the signature-bound v9 base evidence.
 *
 * The evidence stack was carried forward to v10 (#859) and v11 (#861); this
 * union must track `V9EvidenceBenchVersion` in the shared protocol package.
 */
export interface V9BaseEvidence {
  bench_version: 9 | 10 | 11;
  score_gates: {
    rollout_mode: "shadow" | "enforce";
    model_use: V9ModelUseGate;
    authoritative_tool: V9AuthoritativeToolGate;
  };
}

/** Board-level state of the relative-efficiency adjustment.
 *
 * `active` means the frozen factors rank the board. `preview` means the
 * platform recomputed them read-only because the adjustment is switched off —
 * numbers to inspect, not numbers that count. Neither says the cohort actually
 * produced anything: an adjustment can be switched on and still assign no
 * factor, because curve v3 fails closed and only qualified agents join the
 * cohort. `cohort_size` against `n_min` is what distinguishes "off" from
 * "on but nothing qualified", which are otherwise identical on screen.
 */
export interface EfficiencyBoardState {
  active: boolean;
  preview?: boolean;
  bench_version: number;
  curve_version?: number;
  /** Qualified agents in the frozen cohort. Zero when nothing cleared the gates. */
  cohort_size?: number | null;
  /** Minimum qualified agents before a cohort can assign any factor. */
  n_min?: number | null;
  /** Neutral reference (factor 1.0): nearest-rank P25 of cohort token costs. */
  reference_p25_tokens?: number | null;
  factor_alpha?: number | null;
  minimum_factor?: number | null;
  maximum_factor?: number | null;
}

export interface LeaderboardPayload {
  entries?: LeaderboardEntry[];
  /** Active confirmation policy; only enforce changes rank and emissions authority. */
  v9_confirmation_mode?: "shadow" | "enforce" | null;
  available_bench_versions?: number[];
  active_bench_version?: number | null;
  desired_bench_version?: number | null;
  current_bench_version?: number | null;
  /** "current" | "historical" */
  selection_mode?: string;
  generated_at?: string;
  emissions?: EmissionsFold | null;
  efficiency?: EfficiencyBoardState | null;
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
  validator_uid?: number;
  validator_hotkey?: string;
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
  v9_base?: V9BaseEvidence | null;
  case_results?: CaseResult[];
}

export interface ScoresPayload {
  scores?: ConsensusScore[];
  quorum?: number | null;
  median_composite?: number | null;
}
