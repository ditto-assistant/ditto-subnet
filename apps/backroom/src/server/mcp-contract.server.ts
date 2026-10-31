import '@tanstack/react-start/server-only'
import type { BackroomSession } from '../lib/auth.types'

// Keep authorization and discovery independent of the SDK and tool registry.
export const BACKROOM_READ_SCOPE = 'backroom:read'
export const BACKROOM_ARTIFACT_SCOPE = 'backroom:artifact:read'
export const BACKROOM_WRITE_SCOPE = 'backroom:write'
/**
 * The scope an unauthenticated /mcp challenge advertises. MCP clients request
 * exactly the challenged scope, so pinning backroom:read here meant every
 * client connected read-only and consent could never offer the other levels.
 * Advertising the full set lets the operator pick the level on consent, which
 * still caps the grant to the account's live entitlement.
 */
export const BACKROOM_CHALLENGE_SCOPE = [
  BACKROOM_READ_SCOPE,
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_WRITE_SCOPE,
].join(' ')
export type McpGrantProps = {
  session: BackroomSession
  scopes: Array<string>
  clientName: string
  /**
   * The exact OAuth grant and client this access token belongs to. Stamped at
   * token issuance so an operator can match a live connection to the grant
   * listed (and revocable) on the Agent access page.
   */
  grant?: { id: string; clientId: string }
  /** Absolute expiry of this access token. Absent on tokens issued before it. */
  accessExpiresAt?: string
}

/**
 * The scopes this connection can actually exercise right now: the token's
 * granted scopes, further capped by the account's live Backroom level. A
 * read-level account never exercises artifact or write scopes, whatever an
 * older grant recorded.
 */
export function effectiveScopes(props: McpGrantProps) {
  return props.scopes.filter(
    (scope) =>
      scope === BACKROOM_READ_SCOPE ||
      ((scope === BACKROOM_ARTIFACT_SCOPE || scope === BACKROOM_WRITE_SCOPE) &&
        props.session.accessLevel === 'write'),
  )
}

export type BackroomEnv = {
  OAUTH_KV: KVNamespace
  OAUTH_PROVIDER?: import('@cloudflare/workers-oauth-provider').OAuthHelpers
  SESSION_SECRET: string
  /** Comma-separated `@omniaura.ai` administrators who may hold write grants. */
  BACKROOM_ADMIN_EMAILS?: string
  /** Comma-separated identities denied on every console and MCP request. */
  BACKROOM_BLOCKED_EMAILS?: string
}

export const WRITE_TOOL_NAMES = new Set([
  'set_generator_competition_settings',
  'rotate_generator_probe_seed',
  'rescore_generator_submission',
  'reject_generator_submission',
  'reinstate_generator_submission',
  'tick_generator_rounds',
  'void_generator_round',
  'close_generator_round',
  'republish_generator_round_commitment',
  'release_generator_round_lease',
  'register_default_generator',
  'create_default_generator_upload',
  'advance_scored_policy_rescreen',
  'record_treasury_settings',
  'record_treasury_runtime',
  'record_treasury_receipt',
  // Both mirror the Transfer once panel, whose preview and queue server
  // functions already require a write-level session.
  'preview_treasury_manual_transfer',
  'queue_treasury_manual_transfer',
  'preview_treasury_funding_reconciliation',
  'record_treasury_funding_reconciliation',
  'schedule_l2_report_canary',
  'register_canonical_starter_fixture',
  'review_canonical_starter_fixture',
  'schedule_canonical_starter_fixture',
  'create_screener_bootstrap_grant',
  'set_screener_provider_settings',
  'set_screener_node_channel_settings',
  'set_screener_node_replay_capacity',
  'register_screener_replay_process_key',
  'revoke_screener_replay_process_key',
  'resolve_screening_quarantine',
  'release_verified_v13_court_clear',
  'resolve_screening_dispute',
  'rescreen_rejected_submission',
  'retry_failed_screening_now',
  'retry_trusted_image_build',
  'expire_running_screening',
  'reject_screening_submission',
  'open_ath_review',
  'resolve_ath_review',
  'create_ath_rulings_upload',
  'execute_ath_rulings_batch',
  'execute_screening_quarantine_batch',
  'retry_validator_evaluation',
  'remove_failed_submission_from_queue',
  'evict_live_validator_leases',
  'reinstate_evicted_submission_to_queue',
  'batch_retry_validator_evaluation',
  'replace_validator_score',
  'queue_validator_score_retests',
  'refresh_benchmark_contract',
  'rebuild_screened_image',
  'migrate_zero_score_benchmark_contract',
  'qualify_scored_benchmark_rollout',
  'expand_benchmark_rollout_cohort',
  'start_benchmark_rollout',
  'issue_benchmark_canary',
  'cancel_benchmark_canary',
  'isolate_operator_diagnostic',
  'set_efficiency_bonus_settings',
  'set_continual_retest_settings',
  'set_queue_policy_settings',
  'apply_screener_review_settings',
  'rotate_screener_policy_manifest',
  'schedule_screener_policy_activation',
  'schedule_v13_review_clock',
  'restore_scored_screening_snapshot',
  'set_validator_slot_settings',
  'set_validator_issuance_pause',
  'set_scoring_lease_settings',
  'activate_v13_scorer_cohort',
  'rotate_v13_scorer_cohort',
  'apply_copy_court_settings',
  'set_inference_concurrency_settings',
  'start_runtime_profile',
  'set_submission_cooldown',
  'set_conversation_settings',
  'authorize_conversation_retry',
  'unban_hotkey',
  'set_source_release_policy',
  'set_cla_policy',
  'publish_cla_version',
  'revoke_miner_github_link',
  'publish_bounty_revision',
  'set_bounty_claims_paused',
  'review_bounty_submission',
  'reassign_bounty_reviewer',
  'control_bounty_slot',
  'decide_bounty_appeal',
  'award_bounty_claim',
  'set_bounty_payout_policy',
  'approve_bounty_obligation',
  'dispatch_bounty_obligation',
  'reconcile_bounty_transfer',
  'record_bounty_fact',
  'record_bounty_evaluation',
  'decide_bounty_evaluation',
  'set_burn_settings',
  'set_track_share_settings',
  'set_emission_allocation',
  'set_confirmation_bundle_settings',
  'authorize_confirmation_bundle_retest',
])

export const TOOL_SCOPE_REQUIREMENTS = new Map<string, string>([
  ['get_agent_source_release_evidence', BACKROOM_READ_SCOPE],
  ['get_artifact_fetch_audit', BACKROOM_READ_SCOPE],
  ...[...WRITE_TOOL_NAMES].map((name) => [name, BACKROOM_WRITE_SCOPE] as const),
  ['get_screening_artifact', BACKROOM_ARTIFACT_SCOPE],
  // A closed round's dataset is every column's generated cases: private
  // benchmark content, gated like other private artifacts.
  ['get_generator_round_dataset', BACKROOM_ARTIFACT_SCOPE],
  ['get_v13_official_control_artifact', BACKROOM_ARTIFACT_SCOPE],
  ['get_screening_failure_diagnostic', BACKROOM_ARTIFACT_SCOPE],
  ['get_screening_verification_readiness', BACKROOM_ARTIFACT_SCOPE],
  ['download_runtime_profile', BACKROOM_ARTIFACT_SCOPE],
  // Trace records carry miner prompts and full model responses, so anything
  // that discloses record CONTENT gates on the artifact scope. Listing object
  // keys and sizes does not, and stays a plain read.
  ['download_inference_trace', BACKROOM_ARTIFACT_SCOPE],
  ['peek_inference_trace', BACKROOM_ARTIFACT_SCOPE],
  // Source listings and excerpts expose miner-submitted code, so they gate
  // on the same dedicated artifact scope as the tarball download.
  ['list_screening_source_files', BACKROOM_ARTIFACT_SCOPE],
  ['read_screening_source_file', BACKROOM_ARTIFACT_SCOPE],
  // A search returns the matching source lines themselves, so it discloses
  // exactly what an excerpt read does and gates identically.
  ['search_screening_source', BACKROOM_ARTIFACT_SCOPE],
  // Copy-review diffs render miner source from two submissions side by side,
  // so they gate on the same dedicated artifact scope.
  ['get_copy_review_source_diff', BACKROOM_ARTIFACT_SCOPE],
  ['read_copy_review_source_diff_file', BACKROOM_ARTIFACT_SCOPE],
  // Lineage diffs render the same two-submission source for any operator-named
  // pair, so they gate identically.
  ['get_lineage_source_diff', BACKROOM_ARTIFACT_SCOPE],
  // The court's dry-run triage reads both submissions' source server-side
  // (audited) and returns file paths and line locations, so it gates the same.
  ['preview_copy_court_triage', BACKROOM_ARTIFACT_SCOPE],
  ['get_screening_review_lineage', BACKROOM_ARTIFACT_SCOPE],
  // Baseline diffs render miner source against the starter kit, so they gate on
  // the same dedicated artifact scope.
  ['get_screening_baseline_diff', BACKROOM_ARTIFACT_SCOPE],
  ['read_screening_baseline_diff_file', BACKROOM_ARTIFACT_SCOPE],
])
