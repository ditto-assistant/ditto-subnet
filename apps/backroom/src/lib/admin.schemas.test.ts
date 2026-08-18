import { describe, expect, expectTypeOf, it } from 'vitest'
import type { input as ZodInput, output as ZodOutput } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'
import {
  auditReasonSchema,
  CEILING_DISABLED,
  platformSupportsRetestCohortSize,
  parseContinualRetestSettingsControl,
  continualRetestSettingsForPlatform,
  continualRetestFieldSupport,
  continualRetestSettingsSchema,
  continualRetestSettingsWriteSchema,
  effectiveContinualRetestSettingsSchema,
  validatorFleetSchema,
  copyReviewListSchema,
  openAthReviewInputSchema,
  resolveCopyReviewInputSchema,
  resolveScreeningQuarantineInputSchema,
  screeningDisputeListSchema,
  screeningQuarantineListSchema,
  screeningQuarantineBatchExecuteInputSchema,
  screeningQuarantineBatchPreviewInputSchema,
  screeningArtifactSchema,
  screeningSubmissionListSchema,
  releaseValidatorAssignmentInputSchema,
  validatorAssignmentListSchema,
  screenerReviewControlSchema,
  screenerReviewSettingsSchema,
  applyScreenerReviewSettingsInputSchema,
  efficiencyBonusConfirmation,
  efficiencyBonusSettingsControlSchema,
  efficiencyBonusSettingsSchema,
  setEfficiencyBonusSettingsInputSchema,
  inferenceRoutingInventorySchema,
  inferenceRouteCalibrationInputSchema,
  inferenceRouteConfirmation,
  inferenceRoutingPolicyInputSchema,
  inferencePolicyConfirmation,
  inferenceConcurrencySettingsSchema,
  INFERENCE_CONCURRENCY_CONFIRMATION,
  MAX_CHAT_CONCURRENCY,
  MAX_CHAT_TOKEN_BUDGET,
  MAX_EMBEDDING_CONCURRENCY,
  setInferenceConcurrencySettingsInputSchema,
  QUEUE_POLICY_CONFIRMATION,
  effectiveQueuePolicySettingsSchema,
  queuePolicySettingsControlSchema,
  queuePolicySettingsSchema,
  queuePolicySettingsWriteSchema,
  setQueuePolicySettingsInputSchema,
  validatorSlotConfirmation,
  validatorIssuanceConfirmation,
  validatorSlotSettingsControlSchema,
  validatorSlotSettingsSchema,
  setValidatorIssuancePauseInputSchema,
  setValidatorSlotSettingsInputSchema,
  EVICT_VALIDATION_CONFIRMATION,
  REINSTATE_VALIDATION_CONFIRMATION,
  evictValidationInputSchema,
  evictValidationResponseSchema,
  reinstateValidationInputSchema,
  reinstateValidationResponseSchema,
  validationQueueWithdrawalSchema,
  validationRetryDetailSchema,
  validationRetryTicketSchema,
  withdrawValidationInputSchema,
  ARTIFACT_RELEASE_DEFAULT_HOURS,
  ARTIFACT_RELEASE_MAX_HOURS,
  artifactReleaseWindowGloss,
  artifactReleaseRevisionSchema,
  artifactReleaseConfirmation,
  updateArtifactReleaseSettingsInputSchema,
  listStuckSubmissionsInputSchema,
  stuckSubmissionSchema,
  listLeaseRevocationsInputSchema,
  leaseRevocationsListSchema,
  screenerCapacityViewSchema,
  screenerProviderSettingsConfirmation,
  screenerProviderSettingsSchema,
  authorizeConfirmationBundleRetestInputSchema,
  confirmationBundleListInputSchema,
  confirmationBundleListSchema,
  confirmationBundleSettingsConfirmation,
  confirmationBundleSettingsControlSchema,
  confirmationBundleViewSchema,
  setConfirmationBundleSettingsInputSchema,
  sourceReviewFindingSchema,
  queueValidatorScoreRetestsInputSchema,
  v9ContractRetestListSchema,
  publicLeaderboardSchema,
} from './admin.schemas'

type GeneratedConfirmationBundleView = PlatformComponents['schemas']['ConfirmationBundleView']
type GeneratedConfirmationBundleList =
  PlatformComponents['schemas']['AdminConfirmationBundleListResponse']
type GeneratedSourceReviewFinding = PlatformComponents['schemas']['SourceReviewFinding']

describe('admin API schemas', () => {
  it('mirrors the Platform 100M hosted chat-token hard ceiling', () => {
    const settings = {
      chat_request_budget: 8192,
      chat_token_budget: MAX_CHAT_TOKEN_BUDGET,
      chat_per_ticket_concurrency: 16,
      chat_per_validator_concurrency: 48,
      chat_global_concurrency: 96,
      embedding_per_ticket_concurrency: 8,
      embedding_per_validator_concurrency: 24,
      embedding_global_concurrency: 32,
      benchmark_runtime: {
        case_concurrency: 1,
        relay_delay_fingerprint_mode: 'off' as const,
        relay_delay_fingerprint_min_ms: 25,
        relay_delay_fingerprint_max_ms: 250,
      },
    }
    expect(MAX_CHAT_TOKEN_BUDGET).toBe(100_000_000)
    expect(inferenceConcurrencySettingsSchema.parse(settings)).toEqual(settings)
    expect(() => inferenceConcurrencySettingsSchema.parse({
      ...settings,
      chat_token_budget: MAX_CHAT_TOKEN_BUDGET + 1,
    })).toThrow(/100000000/)
  })

  it('mirrors the shared Platform and relay concurrency ceiling', () => {
    const settings = {
      chat_request_budget: 8192,
      chat_token_budget: 75_000_000,
      chat_per_ticket_concurrency: 512,
      chat_per_validator_concurrency: 512,
      chat_global_concurrency: 512,
      embedding_per_ticket_concurrency: 512,
      embedding_per_validator_concurrency: 512,
      embedding_global_concurrency: 512,
      benchmark_runtime: {
        case_concurrency: 4,
        relay_delay_fingerprint_mode: 'shadow' as const,
        relay_delay_fingerprint_min_ms: 25,
        relay_delay_fingerprint_max_ms: 250,
      },
    }
    expect(MAX_CHAT_CONCURRENCY).toBe(512)
    expect(MAX_EMBEDDING_CONCURRENCY).toBe(512)
    expect(inferenceConcurrencySettingsSchema.parse(settings)).toEqual(settings)
    expect(() => inferenceConcurrencySettingsSchema.parse({
      ...settings,
      embedding_global_concurrency: 513,
    })).toThrow(/512/)
  })

  it('requires the v10 runtime object on the new write path', () => {
    const settings = {
      chat_request_budget: 8192,
      chat_token_budget: 25_000_000,
      chat_per_ticket_concurrency: 16,
      chat_per_validator_concurrency: 48,
      chat_global_concurrency: 96,
      embedding_per_ticket_concurrency: 12,
      embedding_per_validator_concurrency: 48,
      embedding_global_concurrency: 96,
    }
    expect(inferenceConcurrencySettingsSchema.parse(settings).benchmark_runtime).toEqual({
      case_concurrency: 1,
      relay_delay_fingerprint_mode: 'off',
      relay_delay_fingerprint_min_ms: 25,
      relay_delay_fingerprint_max_ms: 250,
    })
    expect(() =>
      setInferenceConcurrencySettingsInputSchema.parse({
        expectedRevision: 0,
        settings,
        reason: 'raise benchmark throughput',
        confirmation: INFERENCE_CONCURRENCY_CONFIRMATION,
      }),
    ).toThrow(/benchmark_runtime/)
  })

  it('requires exact confirmation for v9 contract retests only', () => {
    const base = {
      validatorHotkey: '5Validator',
      reason: 'Replace obsolete signed v9 score evidence',
      items: [{
        agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
        expectedSnapshot: 'ab'.repeat(32),
        expectedRunId: 'run-shadow',
      }],
    }
    expect(queueValidatorScoreRetestsInputSchema.parse(base)).toMatchObject({
      basis: 'statistical_outlier',
      confirmation: null,
    })
    expect(() => queueValidatorScoreRetestsInputSchema.parse({
      ...base,
      basis: 'v9_contract_mismatch',
    })).toThrow(/QUEUE V9 CONTRACT RETESTS/)
    expect(queueValidatorScoreRetestsInputSchema.parse({
      ...base,
      basis: 'v9_contract_mismatch',
      confirmation: 'QUEUE V9 CONTRACT RETESTS',
    }).basis).toBe('v9_contract_mismatch')
    expect(() => queueValidatorScoreRetestsInputSchema.parse({
      ...base,
      confirmation: 'QUEUE V9 CONTRACT RETESTS',
    })).toThrow(/only valid/)
  })

  it('parses nullable legacy v9 contract evidence without inventing identity', () => {
    const parsed = v9ContractRetestListSchema.parse({
      items: [{
        agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
        agent_name: 'shadow-agent',
        miner_hotkey: '5Miner',
        agent_status: 'evaluating',
        validator_hotkey: '5Validator',
        run_id: 'run-shadow',
        composite: 0.7,
        snapshot: 'ab'.repeat(32),
        observed_revision: null,
        observed_manifest_sha256: null,
        observed_rollout_mode: null,
        semantic_gate_factor_bps: null,
        ticket_status: 'scored',
        replacement_pending: false,
        replacement_queued: false,
        queue_position: null,
        queue_allowed: true,
        queue_blocking_reason: null,
      }],
      count: 1,
      limit: 100,
      offset: 0,
      required_revision: 'v9-base-enforce-efficiency-v1',
      required_manifest_sha256: 'cd'.repeat(32),
      required_rollout_mode: 'enforce',
    })
    expect(parsed.items[0].observed_revision).toBeNull()
    expect(parsed.required_rollout_mode).toBe('enforce')
  })

  it('preserves the fenced multi-provider capacity contract', () => {
    const parsed = screenerCapacityViewSchema.parse({
      snapshot: {
        environment: 'prod', controller_epoch: 'prod:epoch', provider_settings_revision: 0, runnable_backlog: 4,
        active_leases: 1, desired_slots: 2, global_cap: 6,
        targon_capability: 'nogo', targon_available: 6,
        targon_healthy: 0, targon_pending: 0, targon_draining: 0,
        gce_target: 2, gce_healthy: 1, gce_pending: 1, gce_draining: 0,
        fallback_reason: 'ROOTLESSKIT_OPERATION_NOT_PERMITTED',
        last_provider_success_at: '2026-08-02T00:00:00Z',
        last_provider_error_code: null, last_provider_error_at: null,
        events: [], controller_heartbeat_at: '2026-08-02T00:00:00Z',
        controller_lease_expires_at: '2026-08-02T00:03:00Z',
        updated_at: '2026-08-02T00:00:00Z',
      },
      nodes: [{
        environment: 'prod', node_id: 'targon-slot-01-wk123456', provider: 'targon',
        provider_resource_id: 'wk-123456', screener_hotkey: '5Node', status: 'active',
        capacity: 1, token_expires_at: '2026-08-02T06:00:00Z',
        registered_at: '2026-08-02T00:00:00Z', rotated_at: '2026-08-02T00:00:00Z',
        revoked_at: null, status_reason: null, heartbeat_seen_at: null,
        software_version: null, protocol_version: null, policy_version: null,
        current_phase: null,
      }],
      events: [],
      provider_control: {
        current: {
          environment: 'prod', revision: 0, parent_revision: 0,
          settings: {
            runtime_provider_priority: ['targon', 'gcp'],
            source_review_provider_priority: ['targon', 'gcp'],
            build_provider_priority: ['targon', 'gcp'],
          },
          reason: 'Built-in default', actor: 'platform', created_at: null,
        },
        history: [],
      },
    })
    expect(parsed.snapshot?.gce_target).toBe(2)
    expect(parsed.nodes[0].provider_resource_id).toBe('wk-123456')
    expect(parsed.builds).toEqual([])
  })

  it('preserves detailed operator reasons without an upper bound', () => {
    const detailed = `Evidence: ${'x'.repeat(2_000)}`

    expect(auditReasonSchema(3).parse(detailed)).toBe(detailed)
    expect(auditReasonSchema(8).parse(detailed)).toBe(detailed)
    expect(() => auditReasonSchema(3).parse('no')).toThrow()
    expect(() => auditReasonSchema(8).parse('short')).toThrow()
  })

  it('uses safe provider defaults across a Platform-first rolling deploy', () => {
    const parsed = screenerCapacityViewSchema.parse({
      snapshot: null,
      nodes: [],
      events: [],
      builds: [],
    })

    expect(parsed.provider_control.current.settings).toEqual({
      runtime_provider_priority: ['targon', 'gcp'],
      source_review_provider_priority: ['targon', 'gcp'],
      build_provider_priority: ['targon', 'gcp'],
    })
  })

  it('accepts a gcp-then-targon list but confirms it as an exact first-provider string', () => {
    const settings = screenerProviderSettingsSchema.parse({
      runtime_provider_priority: ['gcp', 'targon'],
      source_review_provider_priority: ['gcp'],
      build_provider_priority: ['gcp', 'targon'],
    })

    expect(screenerProviderSettingsConfirmation(settings)).toBe(
      'APPLY SCREENER PROVIDERS BUILDS=gcp>targon RUNTIME=gcp>targon SOURCE_REVIEW=gcp',
    )
  })

  it('parses aggregate inference controls and rejects unsafe operator input', () => {
    const inventory = inferenceRoutingInventorySchema.parse({
      routing_mode: 'adaptive',
      aggregate_route: null,
      policies: [{
        model: 'openai/gpt-oss-20b', revision: 3, enabled: false,
        speed_weight: 0.5, cost_weight: 0.4, exploration_weight: 0.1,
        exploration_ticket_budget: 5, min_tool_accuracy: 0.8,
        min_composite: 0.7, min_calibration_samples: 60,
        max_error_rate: 0.05, max_timeout_rate: 0.03,
        cooldown_seconds: 300, ewma_alpha: 0.2,
        updated_at: '2026-07-22T00:00:00Z',
      }],
      routes: [{
        model: 'openai/gpt-oss-20b', provider: 'Weights & Biases',
        profile_revision: 'route-v1', quantization: 'fp8', status: 'healthy',
        calibration_status: 'shadow', calibration_revision: 2,
        calibration_manifest_sha256: null,
        calibration_sample_count: 0, calibration_tool_accuracy: null,
        calibration_composite: null, sample_count: 31, selected_ticket_count: 4,
        exploration_ticket_count: 1, last_selected_at: null,
        ewma_tokens_per_second: 161.4, ewma_latency_ms: 260,
        ewma_error_rate: 0.01, ewma_timeout_rate: 0.02,
        prompt_price_per_token: 0.00000003,
        completion_price_per_token: 0.00000013,
        updated_at: '2026-07-22T00:00:00Z',
      }],
      audits: [],
      provider_telemetry: [{
        provider: 'Groq', request_count: 12, completed_count: 11, failed_count: 1,
        inflight_count: 0, timeout_count: 1, upstream_attempt_count: 14,
        prompt_tokens: 125_000, completion_tokens: 8_000,
        cost_microusd: 250_000, average_latency_ms: 210,
      }],
    })
    const route = inventory.routes[0]

    expect(route.provider).toBe('Weights & Biases')
    expect(inventory.routing_mode).toBe('adaptive')
    expect(inventory.aggregate_route).toBeNull()
    expect(inventory.provider_telemetry[0].provider).toBe('Groq')
    expect(() => inferenceRouteCalibrationInputSchema.parse({
      profileRevision: route.profile_revision, model: route.model,
      provider: route.provider, expectedRevision: route.calibration_revision,
      action: 'eligible', manifestSha256: 'not-a-digest',
      toolAccuracy: 1.1, composite: 0.8, sampleCount: 0, confirmation: '',
    })).toThrow()
    expect(inferenceRouteConfirmation('eligible', route.profile_revision)).toBe(
      'ELIGIBLE INFERENCE ROUTE route-v1',
    )
    expect(inferencePolicyConfirmation(route.model)).toBe(
      'UPDATE INFERENCE POLICY openai/gpt-oss-20b',
    )
    expect(() => inferenceRoutingPolicyInputSchema.parse({
      model: route.model, expectedRevision: 3, enabled: true,
      speedWeight: 0, costWeight: 0, explorationWeight: 0,
      explorationTicketBudget: 5, minToolAccuracy: 0.8, minComposite: 0.7,
      minCalibrationSamples: 60, maxErrorRate: 0.05, maxTimeoutRate: 0.03,
      cooldownSeconds: 300, ewmaAlpha: 0.2, confirmation: '',
    })).toThrow('Routing weights cannot all be zero')
  })

  it('locks legacy inference inventories to aggregate mode when mode is absent', () => {
    const inventory = inferenceRoutingInventorySchema.parse({
      policies: [],
      routes: [],
      audits: [],
    })
    expect(inventory.routing_mode).toBe('aggregate_throughput')
    expect(inventory.aggregate_route).toBeNull()
    expect(inventory.provider_telemetry).toEqual([])
  })

  it('parses the platform quarantine contract', () => {
    const result = screeningQuarantineListSchema.parse({
      count: 1,
      items: [
        {
          quarantine_id: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          attempt_id: '20236f60-c143-43b0-b03e-2cbe51f281d8',
          miner_hotkey: '5Miner',
          agent_name: 'memory-agent',
          artifact_sha256: 'artifact',
          policy_version: 7,
          manifest_digest: 'manifest',
          finding_digest: 'finding',
          reason_code: 'source_review_suspicious',
          status: 'active',
          created_at: '2026-07-14T12:00:00Z',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
        },
      ],
    })
    expect(result.items[0].policy_version).toBe(7)
    expect(result.items[0].agent_version).toBeNull()
  })

  it('requires an auditable quarantine resolution reason', () => {
    expect(() =>
      resolveScreeningQuarantineInputSchema.parse({
        quarantineId: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
        resolution: 'release',
        reason: 'x',
      }),
    ).toThrow()
    expect(() =>
      resolveScreeningQuarantineInputSchema.parse({
        quarantineId: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
        resolution: 'release',
        reason: '   ',
      }),
    ).toThrow()
  })

  it('requires unique guarded decisions and explicit batch confirmation', () => {
    const decision = {
      quarantineId: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
      expectedAgentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
      expectedArtifactSha256: 'ab'.repeat(32),
      resolution: 'rescreen' as const,
      reason: 'Run the preserved artifact against the current policy',
    }
    expect(() =>
      screeningQuarantineBatchPreviewInputSchema.parse({
        decisions: [decision, decision],
      }),
    ).toThrow(/only once/)
    expect(() =>
      screeningQuarantineBatchExecuteInputSchema.parse({
        decisions: [decision],
        previewToken: `1234567890.${'a'.repeat(64)}`,
        confirmed: false,
      }),
    ).toThrow()
  })

  it('parses a private miner dispute from the platform admin API', () => {
    const result = screeningDisputeListSchema.parse({
      count: 1,
      items: [
        {
          dispute_id: '44444444-4444-4444-8444-444444444444',
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          quarantine_id: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
          miner_hotkey: '5Miner',
          agent_name: 'memory-agent',
          artifact_sha256: 'ab'.repeat(32),
          message: 'The flagged code is generic routing, not benchmark-specific logic.',
          status: 'pending',
          created_at: '2026-07-15T12:00:00Z',
          original_reason: 'Source appeared benchmark-specific.',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
        },
      ],
    })

    expect(result.items[0].message).toContain('generic routing')
    expect(result.items[0].original_reason).toContain('benchmark-specific')
  })

  it('parses rejected screening history and short-lived artifact access', () => {
    const history = screeningSubmissionListSchema.parse({
      count: 1,
      items: [
        {
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          miner_hotkey: '5Miner',
          agent_name: 'memory-agent',
          agent_version: 2,
          artifact_sha256: 'ab'.repeat(32),
          agent_status: 'rejected',
          screening_policy_version: 7,
          screening_reason: 'Docker image build failed',
          submitted_at: '2026-07-14T12:00:00Z',
          attempts: [
            {
              // Legacy attempts are UUID database values without RFC version
              // and variant bits; the production API still returns them.
              attempt_id: '20236f60-c143-f3b0-203e-2cbe51f281d8',
              policy_version: 7,
              status: 'rejected',
              screener_hotkey: '5Screener',
              started_at: '2026-07-14T12:01:00Z',
              deadline: '2026-07-14T12:31:00Z',
              finished_at: '2026-07-14T12:02:00Z',
              reason: 'Docker image build failed',
              reason_code: 'exact-cross-miner-duplicate',
              duplicate_of: '11111111-1111-4111-8111-111111111111',
              duplicate_name: 'Jackie',
              duplicate_version: 1,
            },
          ],
        },
      ],
    })
    const artifact = screeningArtifactSchema.parse({
      agent_id: history.items[0].agent_id,
      sha256: history.items[0].artifact_sha256,
      download_url: 'https://signed.example/agent.tar.gz?signature=short-lived',
      expires_at: '2026-07-14T12:10:00Z',
    })

    expect(history.items[0].attempts[0].status).toBe('rejected')
    expect(history.items[0].agent_version).toBe(2)
    expect(history.items[0].attempts[0].duplicate_name).toBe('Jackie')
    expect(history.items[0].attempts[0].attempt_id).toBe(
      '20236f60-c143-f3b0-203e-2cbe51f281d8',
    )
    expect(artifact.download_url).toContain('signed.example')
  })

  it('parses live validator assignments and requires an audit reason to release', () => {
    const assignments = validatorAssignmentListSchema.parse({
      count: 1,
      items: [
        {
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          agent_name: 'memory-agent',
          miner_hotkey: '5Miner',
          validator_hotkey: '5Validator',
          issued_at: '2026-07-15T07:00:00Z',
          deadline: '2026-07-15T08:30:00Z',
          bench_version: 2,
          attempt_count: 1,
          score_count: 2,
          provisional_composite: 1.25,
        },
      ],
    })
    expect(assignments.items[0].score_count).toBe(2)
    expect(assignments.items[0].provisional_composite).toBe(1.25)
    expect(() =>
      releaseValidatorAssignmentInputSchema.parse({
        agentId: assignments.items[0].agent_id,
        validatorHotkey: assignments.items[0].validator_hotkey,
        expectedDeadline: assignments.items[0].deadline,
        reason: 'short',
      }),
    ).toThrow()
  })
})

const confirmationDigest = 'a'.repeat(64)
const confirmationTimestamp = '2026-08-08T12:00:00Z'
const confirmationBundleId = '11111111-1111-4111-8111-111111111111'
const confirmationTicketId = '22222222-2222-4222-8222-222222222222'

function confirmationSettings(mode: 'off' | 'shadow' | 'enforce' = 'off') {
  const active = mode !== 'off'
  return {
    mode,
    eligibility_mode: 'rank' as const,
    top_n: 5,
    min_base_score_micros: 950_000,
    daily_bundle_cap: active ? 20 : 0,
    daily_dollar_cap_microusd: active ? 2_000_000 : 0,
    per_bundle_request_cap: active ? 500 : 0,
    per_bundle_token_cap: active ? 2_000_000 : 0,
    profile_revision: active ? 'v9-confirmation-shadow-1' : null,
    profile_checksum: active ? confirmationDigest : null,
    challenger_z: 1.64,
  }
}

function confirmationSettingsControl() {
  const settings = confirmationSettings('shadow')
  const revision = {
    revision: 1,
    parent_revision: 0,
    scope: '*',
    settings,
    checksum: confirmationDigest,
    reason: 'measure v9 confirmation costs before enforcement',
    actor: 'operator@example.com',
    created_at: confirmationTimestamp,
  }
  return {
    current: [revision],
    history: [revision],
    default: confirmationSettings(),
    effective: {
      revision: 1,
      scope: '*',
      settings,
      checksum: confirmationDigest,
      source: 'revision',
      configured: true,
      issuance_active: true,
      max_top_n: 10,
      max_daily_bundle_cap: 1_000,
      max_daily_dollar_microusd: 1_000_000_000,
      max_bundle_request_cap: 100_000,
      max_bundle_token_cap: 100_000_000,
    },
  }
}

function confirmationCalibration() {
  return {
    observed_from_utc_day: '2026-08-01',
    observed_through_utc_day: '2026-08-08',
    observation_days: 8,
    confirmation_profile_revision: 'v9-confirmation-shadow-1',
    confirmation_profile_checksum: confirmationDigest,
    base_run_count: 40,
    measured_base_cost_microusd: 130_000,
    confirmation_bundle_count: 10,
    measured_bundle_cost_microusd: 60_000,
    completed_bundle_count: 8,
    qualified_bundle_count: 2,
    promotion_rate_bps: 2_500,
    projected_daily_spend_microusd: 725_000,
    epoch_duration_seconds: null,
    projected_epoch_spend_microusd: null,
    epoch_projection_unavailable_reason:
      'Bench v9 has no configured epoch duration; no projection was guessed.',
  }
}

function confirmationAblation(intervention: 'inference' | 'embedding') {
  return {
    status: 'not_run' as const,
    evidence_sha256: confirmationDigest,
    latency_ms: 5,
    request_count: 0 as const,
    input_tokens: 0 as const,
    output_tokens: 0 as const,
    provider_cost_microusd: 0 as const,
    synthetic: true as const,
    evidence: {
      contract_version: 'ablation-v1',
      bench_version: 9 as const,
      artifact_sha256: confirmationDigest,
      intervention,
      mode: 'shadow' as const,
      status: 'not_run' as const,
      reason: 'disabled',
      profile_revision: 'v9-confirmation-shadow-1',
      profile_checksum: confirmationDigest,
      threshold_manifest_sha256: confirmationDigest,
      coordinator_sha256: confirmationDigest,
      dataset_sha256: confirmationDigest,
      case_set_sha256: confirmationDigest,
      baseline_scores_sha256: null,
      ablated_scores_sha256: null,
      baseline_mean_micros: null,
      ablated_mean_micros: null,
      delta_micros: null,
      threshold_micros: 400_000,
      sample_count: 0,
      affected_call_count: 0,
      semantic_factor_bps: null,
      applied_factor_bps: null,
      synthetic_usage: {
        synthetic: true as const,
        intervention,
        budget: {
          max_chat_requests: intervention === 'inference' ? 10 : 0,
          max_chat_input_bytes: intervention === 'inference' ? 1_000 : 0,
          max_embedding_requests: intervention === 'embedding' ? 10 : 0,
          max_embedding_inputs: intervention === 'embedding' ? 20 : 0,
          max_embedding_input_bytes: intervention === 'embedding' ? 1_000 : 0,
        },
        chat_attempts: 0,
        chat_applied: 0,
        chat_input_bytes: 0,
        embedding_attempts: 0,
        embedding_applied: 0,
        embedding_inputs: 0,
        embedding_input_bytes: 0,
        rejected_requests: 0,
        budget_exhausted: false,
        upstream_requests: 0 as const,
        upstream_input_tokens: 0 as const,
        upstream_output_tokens: 0 as const,
        upstream_provider_cost_microusd: 0 as const,
      },
    },
  }
}

function confirmationLongMem() {
  const capabilities = [
    'extraction',
    'multi_session_reasoning',
    'temporal_reasoning',
    'knowledge_update',
    'preference',
    'abstention',
  ] as const
  return {
    status: 'completed' as const,
    evidence_sha256: confirmationDigest,
    latency_ms: 2_000,
    request_count: 3,
    input_tokens: 120,
    output_tokens: 30,
    provider_cost_microusd: 60_000,
    synthetic: false as const,
    evidence: {
      schema_version: 2 as const,
      artifact_sha256: confirmationDigest,
      bench_version: 9 as const,
      profile_checksum: confirmationDigest,
      case_set_digest: confirmationDigest,
      dataset_revision: 'longmemeval-s-v1',
      dataset_sha256: confirmationDigest,
      score: {
        longmem_mean_micros: 500_000,
        longmem_stderr_micros: 100_000,
        case_count: 12,
        per_capability: capabilities.map((capability) => ({
          capability,
          correct: 1,
          count: 2,
          mean_micros: 500_000,
        })),
      },
      provider_evidence: [
        {
          lane: 'judge',
          cost_source: 'provider_receipt_v1' as const,
          currency: 'USD' as const,
          provider: 'openrouter',
          profile_revision: 'judge-v1',
          model: 'openai/gpt-4o-2024-08-06',
          fallback_used: false as const,
          requests: 1,
          successes: 1,
          receipted_requests: 1,
          prompt_tokens: 20,
          completion_tokens: 10,
          total_tokens: 30,
          cost_usd_micros: 10_000,
          receipt_set_sha256: 'b'.repeat(64),
        },
        {
          lane: 'reader',
          cost_source: 'provider_receipt_v1' as const,
          currency: 'USD' as const,
          provider: 'openrouter',
          profile_revision: 'reader-v1',
          model: 'openai/gpt-oss-20b',
          fallback_used: false as const,
          requests: 2,
          successes: 2,
          receipted_requests: 2,
          prompt_tokens: 100,
          completion_tokens: 20,
          total_tokens: 120,
          cost_usd_micros: 50_000,
          receipt_set_sha256: confirmationDigest,
        },
      ],
    },
  }
}

function confirmationBundle() {
  const longmemeval = confirmationLongMem()
  const inference = confirmationAblation('inference')
  const embedding = confirmationAblation('embedding')
  return {
    bundle_id: confirmationBundleId,
    artifact_sha256: confirmationDigest,
    bench_version: 9,
    profile_revision: 'v9-confirmation-shadow-1',
    profile_checksum: confirmationDigest,
    retest_generation: 0,
    generation_reason: 'initial',
    source_bundle_id: null,
    state: 'completed',
    settings_revision: 1,
    settings_checksum: confirmationDigest,
    qualification_status: 'unqualified',
    completion_mode: 'shadow',
    completion_ticket_id: confirmationTicketId,
    evidence_sha256: confirmationDigest,
    reporter_hotkey: '5Validator',
    bundle_signature: 'ab'.repeat(64),
    evidence_root: {
      schema_version: 1,
      artifact_sha256: confirmationDigest,
      bench_version: 9,
      confirmation_profile_revision: 'v9-confirmation-shadow-1',
      confirmation_profile_checksum: confirmationDigest,
      settings_revision: 1,
      settings_checksum: confirmationDigest,
      retest_generation: 0,
      ablation_coordinator_latency_ms: 10,
      composite_policy: {
        schema_version: 1,
        revision: 'composite-v9-test-1',
        formula_revision: 'weighted-quality-gates-v1',
        base_weight_bps: 6_000,
        longmem_weight_bps: 4_000,
        checksum: confirmationDigest,
      },
      longmemeval,
      inference_ablation: inference,
      embedding_ablation: embedding,
      totals: {
        request_count: 3,
        input_tokens: 120,
        output_tokens: 30,
        provider_cost_microusd: 60_000,
        latency_ms: 2_010,
      },
    },
    verified_at: confirmationTimestamp,
    completed_at: confirmationTimestamp,
    created_at: confirmationTimestamp,
    updated_at: confirmationTimestamp,
    subjects: [
      {
        agent_id: '33333333-3333-4333-8333-333333333333',
        bench_version: 9,
        artifact_sha256: confirmationDigest,
        result_status: 'provisional',
        base_evidence_sha256: confirmationDigest,
        base_quality_micros: 700_000,
        base_stderr_micros: 20_000,
        base_model_factor_bps: 10_000,
        base_tool_factor_bps: 10_000,
        full_quality_micros: null,
        full_stderr_micros: null,
        semantic_factor_bps: null,
        applied_factor_bps: null,
        full_effective_micros: null,
        bundle_id: confirmationBundleId,
        created_at: confirmationTimestamp,
        updated_at: confirmationTimestamp,
      },
    ],
    dimensions: [
      { dimension: 'longmemeval', ...longmemeval, created_at: confirmationTimestamp },
      {
        dimension: 'inference_ablation',
        ...inference,
        created_at: confirmationTimestamp,
      },
      {
        dimension: 'embedding_ablation',
        ...embedding,
        created_at: confirmationTimestamp,
      },
    ],
    tickets: [
      {
        ticket_id: confirmationTicketId,
        validator_hotkey: '5Validator',
        slot_id: 'slot-1',
        status: 'scored',
        attempt: 1,
        issued_at: confirmationTimestamp,
        deadline: '2026-08-08T13:30:00Z',
        failure_reason: null,
        failed_at: null,
      },
    ],
  } satisfies GeneratedConfirmationBundleView
}

type MutableConfirmationValue<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends null
        ? null
        : T extends ReadonlyArray<infer Item>
          ? Array<MutableConfirmationValue<Item>>
          : T extends object
            ? { -readonly [Key in keyof T]: MutableConfirmationValue<T[Key]> }
            : T
type MutableConfirmationBundle = MutableConfirmationValue<
  ReturnType<typeof confirmationBundle>
>

describe('Bench v9 confirmation bundle schemas', () => {
  it('stays statically exhaustive against the generated Platform response types', () => {
    expectTypeOf<keyof ZodOutput<typeof confirmationBundleViewSchema>>().toEqualTypeOf<
      keyof GeneratedConfirmationBundleView
    >()
    expectTypeOf<ZodOutput<typeof confirmationBundleViewSchema>>().toMatchTypeOf<
      GeneratedConfirmationBundleView
    >()
    expectTypeOf<keyof ZodOutput<typeof confirmationBundleListSchema>>().toEqualTypeOf<
      keyof GeneratedConfirmationBundleList
    >()
    expectTypeOf<ZodOutput<typeof confirmationBundleListSchema>>().toMatchTypeOf<
      GeneratedConfirmationBundleList
    >()
  })

  it('keeps the shipped settings default off and parses a complete shadow revision', () => {
    const parsed = confirmationBundleSettingsControlSchema.parse(
      confirmationSettingsControl(),
    )
    expect(parsed.default.mode).toBe('off')
    expect(parsed.effective.issuance_active).toBe(true)
  })

  it.each(['shadow', 'enforce'] as const)(
    'rejects an incomplete %s settings write instead of filling omitted caps',
    (mode) => {
      const settings = confirmationSettings(mode)
      expect(() =>
        setConfirmationBundleSettingsInputSchema.parse({
          scope: '*',
          expectedRevision: 1,
          settings: { ...settings, per_bundle_token_cap: 0 },
          reason: 'operator reviewed the bounded confirmation spend',
          confirmation: confirmationBundleSettingsConfirmation(mode),
        }),
      ).toThrow(/per_bundle_token_cap/)
    },
  )

  it.each(['off', 'shadow', 'enforce'] as const)(
    'requires the exact mode-bound confirmation for %s',
    (mode) => {
      const input = {
        scope: '*',
        expectedRevision: 1,
        settings: confirmationSettings(mode),
        reason: 'operator reviewed the bounded confirmation spend',
        confirmation: confirmationBundleSettingsConfirmation(mode),
      }
      expect(setConfirmationBundleSettingsInputSchema.parse(input)).toEqual(input)
      const parseWrongMode = () =>
        setConfirmationBundleSettingsInputSchema.parse({
          ...input,
          confirmation: 'APPLY V9 CONFIRMATION MODE SHADOW',
        })
      if (mode === 'shadow') expect(parseWrongMode).not.toThrow()
      else expect(parseWrongMode).toThrow()
    },
  )

  it('rejects unknown write fields instead of silently stripping them', () => {
    expect(() =>
      setConfirmationBundleSettingsInputSchema.parse({
        scope: '*',
        expectedRevision: 1,
        settings: confirmationSettings('off'),
        reason: 'disable bundle issuance while retaining evidence',
        confirmation: 'APPLY V9 CONFIRMATION MODE OFF',
        activateRewards: true,
      }),
    ).toThrow(/Unrecognized key/)
  })

  it('fails closed when Platform omits the completion audit fields', () => {
    const { settings_checksum: _missing, ...stale } = confirmationBundle()
    expect(() => confirmationBundleViewSchema.parse(stale)).toThrow(/settings_checksum/)
  })

  it('accepts a carried-forward v12 confirmation bundle end to end', () => {
    // The evidence stack carries forward to every epoch in
    // V9EvidenceBenchVersion. Backroom pinned z.literal(9) in six places, so a
    // v12 bundle threw at parse and took /confirmation-bundles plus the
    // get_confirmation_bundle MCP tool down with it. The `9` fixtures above
    // cannot catch that -- only an explicitly non-9 bundle can.
    const bundle = JSON.parse(JSON.stringify(confirmationBundle())) as Record<string, unknown>
    const retarget = (node: unknown): void => {
      if (Array.isArray(node)) {
        node.forEach(retarget)
        return
      }
      if (node === null || typeof node !== 'object') return
      const record = node as Record<string, unknown>
      if (typeof record.bench_version === 'number') record.bench_version = 12
      Object.values(record).forEach(retarget)
    }
    retarget(bundle)

    const parsed = confirmationBundleViewSchema.parse(bundle)
    expect(parsed.bench_version).toBe(12)
    expect(parsed.evidence_root?.bench_version).toBe(12)
    expect(parsed.evidence_root?.longmemeval.evidence.bench_version).toBe(12)
    expect(parsed.evidence_root?.inference_ablation.evidence.bench_version).toBe(12)
    expect(parsed.subjects[0]?.bench_version).toBe(12)
  })

  it('preserves receipt-derived LongMem lane cost and integer score fields', () => {
    const parsed = confirmationBundleViewSchema.parse(confirmationBundle())
    expect(parsed.generation_reason).toBe('initial')
    expect(parsed.source_bundle_id).toBeNull()
    const longmem = parsed.evidence_root?.longmemeval.evidence
    expect(longmem?.provider_evidence.find((lane) => lane.lane === 'reader')).toMatchObject({
      lane: 'reader',
      fallback_used: false,
      cost_usd_micros: 50_000,
      receipt_set_sha256: confirmationDigest,
    })
    expect(longmem?.score.longmem_mean_micros).toBe(500_000)
  })

  it('accepts both legal superseded audit shapes and rejects partial completion', () => {
    const completed = structuredClone(confirmationBundle()) as GeneratedConfirmationBundleView
    completed.state = 'superseded'
    expect(confirmationBundleViewSchema.parse(completed).completed_at).toBe(confirmationTimestamp)

    const unspent = structuredClone(confirmationBundle()) as GeneratedConfirmationBundleView
    unspent.state = 'superseded'
    unspent.qualification_status = null
    unspent.completion_mode = null
    unspent.completion_ticket_id = null
    unspent.evidence_sha256 = null
    unspent.reporter_hotkey = null
    unspent.bundle_signature = null
    unspent.evidence_root = null
    unspent.verified_at = null
    unspent.completed_at = null
    unspent.dimensions = []
    expect(confirmationBundleViewSchema.parse(unspent).completed_at).toBeNull()

    const partial = structuredClone(unspent)
    partial.evidence_sha256 = confirmationDigest
    expect(() => confirmationBundleViewSchema.parse(partial)).toThrow(/completion fields are inconsistent/)

    const impossibleDimensions = structuredClone(unspent)
    impossibleDimensions.dimensions = completed.dimensions
    expect(() => confirmationBundleViewSchema.parse(impossibleDimensions)).toThrow(
      /cannot publish dimensions/,
    )
  })

  it('requires source lineage for every non-initial generation', () => {
    const retest = structuredClone(confirmationBundle()) as GeneratedConfirmationBundleView
    retest.retest_generation = 1
    retest.generation_reason = 'operator_retest'
    retest.source_bundle_id = '66666666-6666-4666-8666-666666666666'
    retest.evidence_root!.retest_generation = 1
    expect(confirmationBundleViewSchema.parse(retest).source_bundle_id).toBe(
      '66666666-6666-4666-8666-666666666666',
    )
    retest.source_bundle_id = null
    expect(() => confirmationBundleViewSchema.parse(retest)).toThrow(/generation lineage/)
  })

  it.each([
    ['fallback', (bundle: MutableConfirmationBundle) => {
      bundle.evidence_root!.longmemeval.evidence.provider_evidence[0].fallback_used = true
    }],
    ['missing receipt', (bundle: MutableConfirmationBundle) => {
      bundle.evidence_root!.longmemeval.evidence.provider_evidence[0].receipted_requests = 0
    }],
    ['token mismatch', (bundle: MutableConfirmationBundle) => {
      bundle.evidence_root!.longmemeval.evidence.provider_evidence[0].total_tokens = 121
    }],
    ['capability mean mismatch', (bundle: MutableConfirmationBundle) => {
      bundle.evidence_root!.longmemeval.evidence.score.per_capability[0].mean_micros = 400_000
    }],
  ] as const)('rejects adversarial LongMem %s evidence', (_name, mutate) => {
    const bundle = JSON.parse(JSON.stringify(confirmationBundle()))
    mutate(bundle)
    expect(() => confirmationBundleViewSchema.parse(bundle)).toThrow()
  })

  it('rejects duplicate LongMem provider lanes', () => {
    const bundle = confirmationBundle()
    const lanes = bundle.evidence_root!.longmemeval.evidence.provider_evidence
    lanes.push({ ...lanes[0] })
    expect(() => confirmationBundleViewSchema.parse(bundle)).toThrow(/expected array/)
  })

  it('rejects missing or reordered LongMem provider lanes', () => {
    const missing = confirmationBundle()
    missing.evidence_root!.longmemeval.evidence.provider_evidence.pop()
    expect(() => confirmationBundleViewSchema.parse(missing)).toThrow(/expected array/)

    const reordered = confirmationBundle()
    reordered.evidence_root!.longmemeval.evidence.provider_evidence.reverse()
    expect(() => confirmationBundleViewSchema.parse(reordered)).toThrow(/judge then reader/)
  })

  it('rejects a non-binary ablation factor', () => {
    const bundle = JSON.parse(JSON.stringify(confirmationBundle()))
    const evidence = bundle.evidence_root!.inference_ablation.evidence
    evidence.status = 'passed'
    evidence.baseline_scores_sha256 = confirmationDigest
    evidence.ablated_scores_sha256 = confirmationDigest
    evidence.baseline_mean_micros = 800_000
    evidence.ablated_mean_micros = 200_000
    evidence.delta_micros = 600_000
    evidence.sample_count = 2
    evidence.semantic_factor_bps = 5_000
    evidence.applied_factor_bps = 10_000
    expect(() => confirmationBundleViewSchema.parse(bundle)).toThrow()
  })

  it('rejects upstream provider accounting on synthetic ablations', () => {
    const bundle = JSON.parse(JSON.stringify(confirmationBundle()))
    bundle.evidence_root!.embedding_ablation.evidence.synthetic_usage.upstream_requests = 1
    expect(() => confirmationBundleViewSchema.parse(bundle)).toThrow(/expected 0/)
  })

  it('rejects a shadow completion that claims a fully confirmed subject', () => {
    const bundle = JSON.parse(JSON.stringify(confirmationBundle()))
    bundle.subjects[0].result_status = 'full_confirmed'
    expect(() => confirmationBundleViewSchema.parse(bundle)).toThrow(/shadow/)
  })

  it('rejects completion provenance that does not bind the signed root and scored ticket', () => {
    const wrongRoot = confirmationBundle()
    wrongRoot.evidence_root!.settings_checksum = 'b'.repeat(64)
    expect(() => confirmationBundleViewSchema.parse(wrongRoot)).toThrow(/bind/)

    const wrongTicket = confirmationBundle()
    wrongTicket.completion_ticket_id = '44444444-4444-4444-8444-444444444444'
    expect(() => confirmationBundleViewSchema.parse(wrongTicket)).toThrow(/scored ticket/)
  })

  it('rejects contradictory composite, coordinator, and root-total provenance', () => {
    const wrongWeights = confirmationBundle()
    wrongWeights.evidence_root!.composite_policy.base_weight_bps = 5_000
    expect(() => confirmationBundleViewSchema.parse(wrongWeights)).toThrow(/weights/)

    const splitCoordinator = confirmationBundle()
    splitCoordinator.evidence_root!.embedding_ablation.evidence.coordinator_sha256 =
      'f'.repeat(64)
    expect(() => confirmationBundleViewSchema.parse(splitCoordinator)).toThrow(/coordinator/)

    const wrongLatency = confirmationBundle()
    wrongLatency.evidence_root!.totals.latency_ms += 1
    expect(() => confirmationBundleViewSchema.parse(wrongLatency)).toThrow(/root totals/)
  })

  it('parses bounded list filters and rejects reward/activation controls', () => {
    expect(confirmationBundleListInputSchema.parse({ state: 'failed', limit: 25 })).toEqual({
      state: 'failed',
      limit: 25,
      offset: 0,
    })
    expect(() =>
      confirmationBundleListInputSchema.parse({ state: 'failed', limit: 25, reward: true }),
    ).toThrow(/Unrecognized key/)
  })

  it('requires exact retest confirmation, reason, generation, and idempotency id', () => {
    const input = {
      bundleId: confirmationBundleId,
      requestId: '55555555-5555-4555-8555-555555555555',
      expectedGeneration: 0,
      reason: 'fresh evidence approved after provider recovery',
      confirmation: 'AUTHORIZE CONFIRMATION BUNDLE RETEST',
    }
    expect(authorizeConfirmationBundleRetestInputSchema.parse(input)).toEqual(input)
    expect(() =>
      authorizeConfirmationBundleRetestInputSchema.parse({
        ...input,
        confirmation: 'RETEST',
      }),
    ).toThrow()
  })

  it('validates the list response count and budget without dropping bundle evidence', () => {
    const parsed = confirmationBundleListSchema.parse({
      items: [confirmationBundle()],
      count: 1,
      budget: {
        utc_day: '2026-08-08',
        revision: 3,
        issued_attempts: 2,
        outstanding_reserved_microusd: 10_000,
        settled_microusd: 50_000,
      },
      shadow_calibration: confirmationCalibration(),
    })
    expect(parsed.items[0].evidence_root?.totals.provider_cost_microusd).toBe(60_000)
    expect(parsed.shadow_calibration.measured_base_cost_microusd).toBe(130_000)
    expect(parsed.shadow_calibration.promotion_rate_bps).toBe(2_500)
  })

  it('rejects internally inconsistent shadow calibration aggregates', () => {
    expect(() =>
      confirmationBundleListSchema.parse({
        items: [],
        count: 0,
        budget: {
          utc_day: '2026-08-08',
          revision: 0,
          issued_attempts: 0,
          outstanding_reserved_microusd: 0,
          settled_microusd: 0,
        },
        shadow_calibration: {
          ...confirmationCalibration(),
          completed_bundle_count: 0,
          qualified_bundle_count: 1,
          confirmation_profile_checksum: null,
          epoch_projection_unavailable_reason: null,
        },
      }),
    ).toThrow()
  })

  it('rejects a daily projection whose sample window is miscounted', () => {
    expect(() =>
      confirmationBundleListSchema.parse({
        items: [],
        count: 0,
        budget: {
          utc_day: '2026-08-08',
          revision: 0,
          issued_attempts: 0,
          outstanding_reserved_microusd: 0,
          settled_microusd: 0,
        },
        shadow_calibration: { ...confirmationCalibration(), observation_days: 7 },
      }),
    ).toThrow(/observation days/)
  })

  it('rejects a total count smaller than the returned bounded page', () => {
    expect(() =>
      confirmationBundleListSchema.parse({
        items: [confirmationBundle()],
        count: 0,
        budget: {
          utc_day: '2026-08-08',
          revision: 0,
          issued_attempts: 0,
          outstanding_reserved_microusd: 0,
          settled_microusd: 0,
        },
        shadow_calibration: confirmationCalibration(),
      }),
    ).toThrow(/count cannot be smaller/)
  })
})

describe('source review causal evidence schema', () => {
  const generatedFinding = {
    artifact_sha256: 'a'.repeat(64),
    prompt_revision: 'source-review-v2',
    risk_level: 'high',
    confidence: 0.97,
    categories: ['benchmark_emulation'],
    evidence: [
      { path: 'src/serve.ts', line: 42, category: 'benchmark_emulation' },
      { path: 'src/score.ts', line: 87, category: 'benchmark_emulation' },
    ],
    summary: 'A served trigger routes around the model and rewrites the graded answer.',
    causal_evidence: {
      schema_version: 2,
      authority_transition: 'model_output_overwritten',
      scorer_visible_effect: 'answer',
      role_bindings: [
        { path: 'src/serve.ts', line: 42, category: 'benchmark_emulation', role: 'served_trigger' },
        { path: 'src/score.ts', line: 87, category: 'benchmark_emulation', role: 'scorer_visible_effect' },
      ],
    },
  } satisfies GeneratedSourceReviewFinding

  it('stays statically exhaustive against the generated Platform finding type', () => {
    expectTypeOf<keyof ZodOutput<typeof sourceReviewFindingSchema>>().toEqualTypeOf<
      keyof GeneratedSourceReviewFinding
    >()
    expectTypeOf<ZodOutput<typeof sourceReviewFindingSchema>>().toMatchTypeOf<
      GeneratedSourceReviewFinding
    >()
  })

  it('parses the generated v2 finding shape without stripping causal proof', () => {
    const parsed = sourceReviewFindingSchema.parse(generatedFinding)
    expect(parsed.causal_evidence).toEqual(generatedFinding.causal_evidence)
  })

  it('accepts the generated legacy shape when optional finding fields are absent', () => {
    const legacyFinding = {
      artifact_sha256: 'a'.repeat(64),
      prompt_revision: 'source-review-v1',
      risk_level: 'low',
      confidence: 0.8,
      categories: ['safe'],
      summary: 'No causal finding was recorded by the legacy source review.',
    } satisfies GeneratedSourceReviewFinding

    const parsed = sourceReviewFindingSchema.parse(legacyFinding)
    expect(parsed.evidence).toEqual([])
    expect(parsed).not.toHaveProperty('causal_evidence')
  })

  it('rejects unknown, duplicate, incompatible, and unbound causal proof', () => {
    expect(() => sourceReviewFindingSchema.parse({
      ...generatedFinding,
      causal_evidence: { ...generatedFinding.causal_evidence, private_prompt: 'hidden' },
    })).toThrow(/Unrecognized key/)
    expect(() => sourceReviewFindingSchema.parse({
      ...generatedFinding,
      causal_evidence: {
        ...generatedFinding.causal_evidence,
        role_bindings: [
          generatedFinding.causal_evidence.role_bindings[0],
          generatedFinding.causal_evidence.role_bindings[0],
        ],
      },
    })).toThrow(/unique/)
    expect(() => sourceReviewFindingSchema.parse({
      ...generatedFinding,
      causal_evidence: {
        ...generatedFinding.causal_evidence,
        authority_transition: 'tool_execution_bypassed',
        scorer_visible_effect: 'answer',
      },
    })).toThrow(/incompatible/)
    expect(() => sourceReviewFindingSchema.parse({
      ...generatedFinding,
      causal_evidence: {
        ...generatedFinding.causal_evidence,
        role_bindings: [{ ...generatedFinding.causal_evidence.role_bindings[0], line: 999 }],
      },
    })).toThrow(/does not reference/)
  })
})

describe('screener review settings schemas', () => {
  const settings = {
    mode: 'shadow',
    l2_model: 'moonshotai/kimi-k3',
    l2_fallback_models: ['z-ai/glm-5.2', 'openai/gpt-5.6-sol'],
    l3_enabled: true,
    l3_model: 'openai/gpt-5.6-sol',
    timeout_seconds: 900,
    max_steps: 18,
    source_review_max_steps: 24,
    source_review_max_read_bytes: 1_200_000,
    source_review_reasoning_effort: 'high',
    max_input_tokens: 425_000,
    max_output_tokens: 20_000,
    max_completion_tokens: 2_400,
    max_cost_usd: 2,
    critic_reasoning_effort: 'medium',
    cache_ttl_seconds: 604_800,
    audit_retention_days: 30,
  }

  it('parses current, history, and signed worker application status', () => {
    const parsed = screenerReviewControlSchema.parse({
      current: [],
      history: [],
      known_instances: ['ditto-screener-prod'],
      applied_instances: [{
        instance_id: 'ditto-screener-prod', revision: 42, scope: '*', mode: 'shadow',
        checksum: 'ab'.repeat(32), source: 'platform', seen_at: '2026-07-21T12:00:00Z',
        fresh: true, matches_effective: true, expected_revision: 42,
        expected_scope: '*', expected_checksum: 'ab'.repeat(32),
      }],
      shadow_observations: [],
    })
    expect(parsed.applied_instances[0]?.revision).toBe(42)
  })

  it('fills L1 Luna budget defaults when older payloads omit them', () => {
    const { source_review_max_steps, source_review_max_read_bytes, source_review_reasoning_effort, ...legacy } =
      settings
    expect(source_review_max_steps).toBe(24)
    expect(source_review_max_read_bytes).toBe(1_200_000)
    expect(source_review_reasoning_effort).toBe('high')
    const parsed = screenerReviewSettingsSchema.parse(legacy)
    expect(parsed.source_review_max_steps).toBe(24)
    expect(parsed.source_review_max_read_bytes).toBe(1_200_000)
    expect(parsed.source_review_reasoning_effort).toBe('high')
  })

  it('rejects duplicate model chains and short audit reasons', () => {
    expect(() => applyScreenerReviewSettingsInputSchema.parse({
      scope: '*', expectedRevision: 0,
      settings: { ...settings, l2_fallback_models: ['moonshotai/kimi-k3'] },
      reason: 'short', confirmation: 'APPLY SCREENER REVIEW * SHADOW',
    })).toThrow()
  })
})


describe('copy review schemas', () => {
  it('parses a public-safe deferred-review trigger snapshot', () => {
    const parsed = copyReviewListSchema.parse({
      items: [{
        review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        agent_id: '11111111-1111-4111-8111-111111111111',
        miner_hotkey: '5Miner',
        agent_name: 'qualified-agent',
        agent_version: 1,
        submitted_at: '2026-08-01T12:00:00Z',
        status: 'pending',
        opened_at: '2026-08-01T13:00:00Z',
        resolved_at: null,
        resolved_by: null,
        resolution: null,
        resolution_reason: null,
        original: {
          review_kind: 'deferred_source_review',
          duplicate_of: null,
          reason: 'Score qualified this submission for deferred source review',
          policy_version: 9,
          fingerprint_versions: {},
          reference_provenance: 'post-score-v1',
          backfilled: false,
          deferred_review: {
            mode: 'enforce',
            triggers: ['top_five', 'tool_anomaly'],
            rank: 3,
            cohort_size: 12,
            peer_count: 11,
            candidate: { composite: 0.81, tool: 0.94 },
            thresholds: { tool: { median: 0.43, mad: 0.05, cutoff: 0.73 } },
            screening_attempt_id: '22222222-2222-4222-8222-222222222222',
            screening_reason_code: 'review-budget-exhausted',
            review_audit: {
              stage: 'l2',
              reason_code: 'max-steps-exhausted',
              prompt_revision: 'source-review-v9',
              harness_revision: null,
              max_steps: 18,
              steps_used: 18,
              max_read_bytes: null,
              read_bytes_used: null,
              max_input_tokens: 425000,
              input_tokens_used: 425000,
              max_output_tokens: null,
              output_tokens_used: null,
              max_cost_usd: 2,
              cost_usd_used: 1.98,
            },
            review_audit_digest: 'c'.repeat(64),
          },
        },
      }],
      count: 1,
      limit: 200,
      offset: 0,
      generation: 'active',
      active_bench_version: 8,
    })

    expect(parsed.items[0]?.original.deferred_review?.triggers).toEqual([
      'top_five',
      'tool_anomaly',
    ])
    expect(parsed.items[0]?.original.deferred_review?.review_audit?.steps_used).toBe(18)
    expect(parsed.items[0]?.original.deferred_review?.review_audit_digest).toBe('c'.repeat(64))
    expect(parsed.items[0]?.original.duplicate_of).toBeNull()
  })

  it('parses a durable copy-review list without mutable comparison evidence', () => {
    const parsed = copyReviewListSchema.parse({
      items: [
        {
          review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          agent_id: '11111111-1111-4111-8111-111111111111',
          miner_hotkey: '5Miner',
          agent_name: 'held-agent',
          agent_version: null,
          submitted_at: '2026-07-15T12:00:00Z',
          status: 'pending',
          opened_at: '2026-07-15T13:00:00Z',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
          original: {
            duplicate_of: null,
            reason: 'legacy hold',
            policy_version: 1,
            fingerprint_versions: { lexical: null, structural: null, prompt: null },
            reference_provenance: 'legacy',
            backfilled: true,
          },
        },
      ],
      count: 1,
      limit: 200,
      offset: 0,
      generation: 'active',
      active_bench_version: 8,
    })
    expect(parsed.items[0]?.original.reason).toBe('legacy hold')
    expect(parsed.items[0]?.status).toBe('pending')
  })

  it('rejects a resolve input with a too-short reason', () => {
    expect(() =>
      resolveCopyReviewInputSchema.parse({
        agentId: '11111111-1111-4111-8111-111111111111',
        resolution: 'clear',
        reason: 'no',
      }),
    ).toThrow()
  })

  it('preserves detailed ATH hold and resolution reasons over 500 characters', () => {
    const reason = `Detailed source evidence: ${'e'.repeat(1_000)}`
    const identity = {
      agentId: '11111111-1111-4111-8111-111111111111',
      expectedSha256: 'ab'.repeat(32),
      expectedScoreCount: 3,
      reason,
    }

    expect(openAthReviewInputSchema.parse(identity).reason).toBe(reason)
    expect(resolveCopyReviewInputSchema.parse({
      agentId: identity.agentId,
      resolution: 'clear',
      reason,
    }).reason).toBe(reason)
  })

  it('only accepts canonical clear or reject resolutions', () => {
    expect(() =>
      resolveCopyReviewInputSchema.parse({
        agentId: '11111111-1111-4111-8111-111111111111',
        resolution: 'rescreen',
        reason: 'wrong action for this queue',
      }),
    ).toThrow()
  })
})

describe('efficiency bonus settings schemas', () => {
  const settings = {
    enabled: true,
    fold_enabled: false,
    cap: 0.05,
    deep_cap: 0.1,
    deep_frontier_ratio: 0.5,
    factor_alpha: 0.25,
    minimum_factor: 0.85,
    maximum_factor: 1.1,
    cohort_size: 25,
    min_cohort: 8,
    epoch_hours: 24,
    quality_floor: 0,
    memory_floor: 0,
  }

  function input(overrides: Record<string, unknown> = {}) {
    const { settings: settingsOverride, ...rest } = overrides
    const merged = { ...settings, ...((settingsOverride as typeof settings) ?? {}) }
    return {
      scope: '*',
      expectedRevision: 0,
      reason: 'enable the v7 efficiency bonus for the shadow window',
      confirmation: efficiencyBonusConfirmation(merged.enabled),
      ...rest,
      settings: merged,
    }
  }

  it('reads the seed default when no revision has ever been written', () => {
    const parsed = efficiencyBonusSettingsControlSchema.parse({
      current: [],
      history: [],
      seed_default: { ...settings, enabled: false },
      effective: {
        revision: 0,
        scope: '*',
        settings: { ...settings, enabled: false },
        checksum_settings: { ...settings, enabled: false },
        checksum: '',
        source: 'seed',
        fold_effective: false,
        max_age_seconds: 5,
      },
    })
    expect(parsed.effective.source).toBe('seed')
    expect(parsed.effective.revision).toBe(0)
    expect(parsed.current).toEqual([])
  })

  it('reads a governing revision with its operator audit trail', () => {
    const revision = {
      revision: 3,
      parent_revision: 2,
      scope: '*',
      settings: { ...settings, fold_enabled: true },
      checksum_settings: { ...settings, fold_enabled: true },
      reason: 'fold the bonus into validator weights',
      actor: 'peyton@omniaura.ai',
      created_at: '2026-07-23T12:00:00Z',
      checksum: 'ab'.repeat(32),
    }
    const parsed = efficiencyBonusSettingsControlSchema.parse({
      current: [revision],
      history: [revision],
      seed_default: { ...settings, enabled: false },
      effective: {
        revision: 3,
        scope: '*',
        settings: { ...settings, fold_enabled: true },
        checksum_settings: { ...settings, fold_enabled: true },
        checksum: 'ab'.repeat(32),
        source: 'revision',
        fold_effective: true,
        max_age_seconds: 5,
      },
    })
    expect(parsed.current[0]?.actor).toBe('peyton@omniaura.ai')
    expect(parsed.current[0]?.checksum_settings).toEqual({
      ...settings,
      fold_enabled: true,
    })
    expect(parsed.effective.fold_effective).toBe(true)
  })

  it('accepts a policy inside the platform check_config envelope', () => {
    const parsed = setEfficiencyBonusSettingsInputSchema.parse(input())
    expect(parsed.scope).toBe('*')
    expect(parsed.settings.cap).toBe(0.05)
  })

  it('defaults the scope to the subnet-global policy', () => {
    const { scope: _scope, ...withoutScope } = input()
    expect(setEfficiencyBonusSettingsInputSchema.parse(withoutScope).scope).toBe('*')
  })

  it('rejects a cap above the deep cap', () => {
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(
        input({ settings: { cap: 0.08, deep_cap: 0.06 } }),
      ),
    ).toThrow()
  })

  it('rejects a cap above the 0.10 ceiling and a non-positive cap', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, cap: 0.2, deep_cap: 0.2 }),
    ).toThrow()
    expect(() => efficiencyBonusSettingsSchema.parse({ ...settings, cap: 0 })).toThrow()
  })

  it('rejects a deep frontier ratio outside the open unit interval', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, deep_frontier_ratio: 1 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, deep_frontier_ratio: 0 }),
    ).toThrow()
  })

  it('defaults bounded-factor knobs when reading a legacy revision', () => {
    const {
      factor_alpha: _factorAlpha,
      minimum_factor: _minimumFactor,
      maximum_factor: _maximumFactor,
      ...legacy
    } = settings
    const parsed = efficiencyBonusSettingsSchema.parse(legacy)
    expect(parsed.factor_alpha).toBe(0.25)
    expect(parsed.minimum_factor).toBe(0.85)
    expect(parsed.maximum_factor).toBe(1.1)
  })

  it('does not default bounded-factor knobs on a whole-policy write', () => {
    const {
      factor_alpha: _factorAlpha,
      minimum_factor: _minimumFactor,
      maximum_factor: _maximumFactor,
      ...legacy
    } = settings
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse({
        ...input(),
        settings: legacy,
      }),
    ).toThrow()
  })

  it('rejects bounded-factor knobs outside the platform envelope', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, factor_alpha: 0 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, factor_alpha: 1.01 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, minimum_factor: 0 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, minimum_factor: 1.01 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, maximum_factor: 0.99 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, maximum_factor: 100.01 }),
    ).toThrow()
  })

  it('accepts a widened factor envelope inside the platform check_config range', () => {
    const parsed = efficiencyBonusSettingsSchema.parse({
      ...settings,
      minimum_factor: 0.5,
      maximum_factor: 50,
    })
    expect(parsed.minimum_factor).toBe(0.5)
    expect(parsed.maximum_factor).toBe(50)
  })

  it('rejects a cohort smaller than the activation gate', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, cohort_size: 5, min_cohort: 8 }),
    ).toThrow()
  })

  it('rejects fractional cohort sizes and a sub-hour epoch', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, cohort_size: 25.5 }),
    ).toThrow()
    expect(() => efficiencyBonusSettingsSchema.parse({ ...settings, epoch_hours: 0 })).toThrow()
  })

  it('rejects floors outside the unit interval', () => {
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, quality_floor: 1.5 }),
    ).toThrow()
    expect(() =>
      efficiencyBonusSettingsSchema.parse({ ...settings, memory_floor: -0.1 }),
    ).toThrow()
  })

  it('refuses a fold that the platform would clamp off', () => {
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(
        input({ settings: { enabled: false, fold_enabled: true } }),
      ),
    ).toThrow(/fold_enabled requires enabled/)
  })

  it('requires the exact confirmation for the resulting master switch', () => {
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(
        input({ confirmation: 'APPLY EFFICIENCY BONUS DISABLED' }),
      ),
    ).toThrow(/APPLY EFFICIENCY BONUS ENABLED/)
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(
        input({ settings: { enabled: false }, confirmation: 'APPLY EFFICIENCY BONUS ENABLED' }),
      ),
    ).toThrow(/APPLY EFFICIENCY BONUS DISABLED/)
  })

  it('requires an auditable reason and a non-negative expected revision', () => {
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(input({ reason: 'short' })),
    ).toThrow()
    expect(() =>
      setEfficiencyBonusSettingsInputSchema.parse(input({ expectedRevision: -1 })),
    ).toThrow()
  })

  it('names the confirmation string for both master-switch states', () => {
    expect(efficiencyBonusConfirmation(true)).toBe('APPLY EFFICIENCY BONUS ENABLED')
    expect(efficiencyBonusConfirmation(false)).toBe('APPLY EFFICIENCY BONUS DISABLED')
  })
})

describe('queue policy settings schemas', () => {
  const settings = {
    rescore_cohort_size: 10,
    priority_cohort_size: 5,
    lane_cycle_size: 4,
    fresh_submission_slots: [0, 1, 3],
    owner_concurrent_submission_limit: 2,
    deferred_source_review: {
      mode: 'off',
      min_cohort_size: 8,
      composite_mad_multiplier: 6,
      axis_mad_multiplier: 6,
      min_composite_delta: 0.1,
      min_axis_delta: 0.15,
    },
    similarity_budget: {
      enabled: true,
      concurrent_submission_limit: 1,
      jaccard_threshold: 0.9,
      containment_threshold: 0.95,
    },
    prev_gen_carryover: {
      enabled: false,
      max_agents: 10,
      min_score_count: 2,
      include_exhausted: false,
      dedupe_scope: 'coldkey',
      require_cohort_complete: true,
      require_desired_era_drained: true,
    },
  }

  function input(overrides: Record<string, unknown> = {}) {
    const { settings: settingsOverride, ...rest } = overrides
    return {
      scope: '*',
      expectedRevision: 0,
      reason: 'widen the fresh-submission lane for the onboarding wave',
      confirmation: QUEUE_POLICY_CONFIRMATION,
      ...rest,
      settings: { ...settings, ...((settingsOverride as typeof settings) ?? {}) },
    }
  }

  it('fills the shipped default for a policy sent as an empty object', () => {
    // Every knob is optional on the wire and the platform fills defaults, so an
    // operator who omits one gets current behavior rather than a parse error.
    expect(queuePolicySettingsSchema.parse({})).toEqual(settings)
  })

  it('reads the shipped default when no revision has ever been written', () => {
    const parsed = queuePolicySettingsControlSchema.parse({
      current: [],
      history: [],
      default: settings,
      effective: {
        revision: 0,
        scope: '*',
        settings,
        checksum: '',
        source: 'default',
        open_rollout_desired_version: null,
        open_rollout_rescore_cohort_target: null,
        open_rollout_priority_cohort_target: null,
        open_rollout_overrides_setting: false,
        rollout_locked_fields: [],
      },
    })
    expect(parsed.effective.source).toBe('default')
    expect(parsed.effective.revision).toBe(0)
    expect(parsed.current).toEqual([])
  })

  it('reads the frozen cohort targets of an open rollout with its locked fields', () => {
    const revision = {
      revision: 4,
      parent_revision: 3,
      scope: '*',
      settings: { ...settings, rescore_cohort_size: 20, priority_cohort_size: 8 },
      reason: 'widen the rescore cohort for the v8 rollout',
      actor: 'peyton@omniaura.ai',
      created_at: '2026-07-25T12:00:00Z',
      checksum: 'ab'.repeat(32),
    }
    const parsed = queuePolicySettingsControlSchema.parse({
      current: [revision],
      history: [revision],
      default: settings,
      effective: {
        revision: 4,
        scope: '*',
        settings: revision.settings,
        checksum: 'ab'.repeat(32),
        source: 'revision',
        // The open rollout froze the previous sizes, so the settings above are
        // next-rollout policy and these targets are what actually governs.
        open_rollout_desired_version: 8,
        open_rollout_rescore_cohort_target: 10,
        open_rollout_priority_cohort_target: 5,
        open_rollout_overrides_setting: true,
        rollout_locked_fields: ['lane_cycle_size', 'fresh_submission_slots'],
      },
    })
    expect(parsed.current[0]?.actor).toBe('peyton@omniaura.ai')
    expect(parsed.effective.open_rollout_rescore_cohort_target).toBe(10)
    expect(parsed.effective.rollout_locked_fields).toEqual([
      'lane_cycle_size',
      'fresh_submission_slots',
    ])
  })

  it('defaults the rollout projection when the platform predates it', () => {
    // Backroom can deploy ahead of the platform. Reading must degrade to "no
    // open rollout" rather than throwing and blanking the policy.
    const parsed = queuePolicySettingsControlSchema.parse({
      current: [],
      history: [],
      default: settings,
      effective: {
        revision: 0,
        scope: '*',
        settings,
        checksum: '',
        source: 'default',
      },
    })
    expect(parsed.effective.open_rollout_desired_version).toBeNull()
    expect(parsed.effective.open_rollout_overrides_setting).toBe(false)
    expect(parsed.effective.rollout_locked_fields).toEqual([])
  })

  it('defaults the scope to the subnet-global policy', () => {
    const { scope: _scope, ...withoutScope } = input()
    expect(setQueuePolicySettingsInputSchema.parse(withoutScope).scope).toBe('*')
  })

  // The drift guard. `z.object` silently drops keys it does not declare, so a
  // field the platform requires but this schema omits is invisible here and a
  // 422 there -- that is how `require_desired_era_drained` was lost, and how
  // `owner_concurrent_submission_limit` broke every queue-policy write after
  // ditto-platform#476 shipped it. Enumerating the contract makes the next
  // platform-side addition fail loudly in CI instead of silently in prod.
  it('declares exactly the platform-owned policy field set', () => {
    expect(Object.keys(queuePolicySettingsSchema.parse({})).sort()).toEqual([
      'deferred_source_review',
      'fresh_submission_slots',
      'lane_cycle_size',
      'owner_concurrent_submission_limit',
      'prev_gen_carryover',
      'priority_cohort_size',
      'rescore_cohort_size',
      'similarity_budget',
    ])
  })

  it('requires every policy field on write, because a revision stores the whole object', () => {
    const { owner_concurrent_submission_limit: _limit, ...missingLimit } = settings
    expect(() => queuePolicySettingsWriteSchema.parse(missingLimit)).toThrow()

    const { prev_gen_carryover: _carryover, ...missingCarryover } = settings
    expect(() => queuePolicySettingsWriteSchema.parse(missingCarryover)).toThrow()

    const { similarity_budget: _similarity, ...missingSimilarity } = settings
    expect(() => queuePolicySettingsWriteSchema.parse(missingSimilarity)).toThrow()

    const { deferred_source_review: _deferred, ...missingDeferred } = settings
    expect(() => queuePolicySettingsWriteSchema.parse(missingDeferred)).toThrow()

    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        similarity_budget: { ...settings.similarity_budget, jaccard_threshold: undefined },
      }),
    ).toThrow()

    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        deferred_source_review: {
          ...settings.deferred_source_review,
          composite_mad_multiplier: undefined,
        },
      }),
    ).toThrow()

    // The carryover block is stored whole too, and the platform names its
    // missing keys separately.
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        prev_gen_carryover: { ...settings.prev_gen_carryover, require_desired_era_drained: undefined },
      }),
    ).toThrow()

    expect(queuePolicySettingsWriteSchema.parse(settings)).toEqual(settings)
  })

  it('keeps the owner concurrency limit inside the platform range', () => {
    expect(() =>
      queuePolicySettingsWriteSchema.parse({ ...settings, owner_concurrent_submission_limit: 0 }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsWriteSchema.parse({ ...settings, owner_concurrent_submission_limit: 4 }),
    ).toThrow()
  })

  it('keeps the similarity budget inside the platform bounds', () => {
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        similarity_budget: { ...settings.similarity_budget, concurrent_submission_limit: 4 },
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        similarity_budget: { ...settings.similarity_budget, containment_threshold: 0.69 },
      }),
    ).toThrow()
  })

  it('keeps deferred anomaly review inside the platform bounds', () => {
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        deferred_source_review: { ...settings.deferred_source_review, min_cohort_size: 4 },
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        deferred_source_review: {
          ...settings.deferred_source_review,
          axis_mad_multiplier: 20.1,
        },
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsWriteSchema.parse({
        ...settings,
        deferred_source_review: { ...settings.deferred_source_review, mode: 'disabled' },
      }),
    ).toThrow()
  })

  it.each(['off', 'observe', 'enforce', 'bypass'] as const)(
    'carries the %s source-review mode through read and write',
    (mode) => {
      // `bypass` is the no-source-review mode. The read schema matters as much
      // as the write one: an enum that did not know the value would fail the
      // whole queue-policy read the moment an operator selected it, taking the
      // board down with it.
      const deferred = { ...settings.deferred_source_review, mode }
      expect(
        queuePolicySettingsWriteSchema.parse({ ...settings, deferred_source_review: deferred })
          .deferred_source_review.mode,
      ).toBe(mode)
      expect(
        queuePolicySettingsSchema.parse({ ...settings, deferred_source_review: deferred })
          .deferred_source_review.mode,
      ).toBe(mode)
    },
  )

  it('surfaces whether an open rollout is actually locking the lane fields', () => {
    // `rollout_locked_fields` is a constant; `rollout_is_open` is the only
    // field that says whether the lock is live. It was being stripped.
    const parsed = effectiveQueuePolicySettingsSchema.parse({
      revision: 3,
      scope: '*',
      settings,
      checksum: 'ab'.repeat(32),
      source: 'revision',
      rollout_locked_fields: ['lane_cycle_size', 'fresh_submission_slots'],
      rollout_is_open: true,
      min_cohort_size: 5,
      max_cohort_size: 25,
    })
    expect(parsed.rollout_is_open).toBe(true)
    expect(parsed.min_cohort_size).toBe(5)
    expect(parsed.max_cohort_size).toBe(25)
  })

  it('rejects a priority cohort larger than the rescore cohort', () => {
    expect(() =>
      setQueuePolicySettingsInputSchema.parse(
        input({ settings: { rescore_cohort_size: 6, priority_cohort_size: 12 } }),
      ),
    ).toThrow(/at most rescore_cohort_size/)
  })

  it('rejects cohort sizes outside the platform range', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({ ...settings, rescore_cohort_size: 26 }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        rescore_cohort_size: 4,
        priority_cohort_size: 4,
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsSchema.parse({ ...settings, lane_cycle_size: 13 }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        lane_cycle_size: 1,
        fresh_submission_slots: [0],
      }),
    ).toThrow()
  })

  it('carries both previous-generation gates through a round trip', () => {
    // The bug this pins: `require_desired_era_drained` was missing from the
    // schema, so `z.object` stripped it out of every read and left it out of
    // every write. The platform requires the whole carryover object, so the
    // omission made the board unwritable and the gate unreadable at once.
    const parsed = queuePolicySettingsSchema.parse({
      ...settings,
      prev_gen_carryover: {
        ...settings.prev_gen_carryover,
        require_desired_era_drained: false,
      },
    })
    expect(parsed.prev_gen_carryover).toEqual({
      ...settings.prev_gen_carryover,
      require_desired_era_drained: false,
    })
  })

  it('defaults the previous generation to last and knows nothing of a retired era', () => {
    const parsed = queuePolicySettingsSchema.parse({
      rescore_cohort_size: 10,
      priority_cohort_size: 5,
      lane_cycle_size: 4,
      fresh_submission_slots: [0, 1, 3],
    })
    // `allow_retired_era_backfill` is gone, and gone is stronger than off. It
    // was a live switch that re-opened a retired benchmark era from Backroom;
    // the platform now refuses sub-v7 work in the schema, so there is no knob
    // left to read.
    expect(parsed.prev_gen_carryover).not.toHaveProperty('allow_retired_era_backfill')
    expect(parsed.prev_gen_carryover.require_desired_era_drained).toBe(true)
  })

  it('rejects a fresh-submission slot outside the lane cycle', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        lane_cycle_size: 4,
        fresh_submission_slots: [0, 1, 4],
      }),
    ).toThrow(/lane position in \[0, 4\)/)
  })

  it('rejects duplicate fresh-submission slots', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({ ...settings, fresh_submission_slots: [0, 1, 1] }),
    ).toThrow(/unique lane positions/)
  })

  it('refuses to starve new miners with an empty fresh lane', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({ ...settings, fresh_submission_slots: [] }),
    ).toThrow(/never starved/)
  })

  it('refuses a fresh lane that leaves no rollout cohort slot', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        lane_cycle_size: 4,
        fresh_submission_slots: [0, 1, 2, 3],
      }),
    ).toThrow(/at least one cohort slot/)
  })

  it('accepts previous-generation carryover only as an explicit operator choice', () => {
    expect(queuePolicySettingsSchema.parse({}).prev_gen_carryover.enabled).toBe(false)
    const parsed = queuePolicySettingsSchema.parse({
      ...settings,
      prev_gen_carryover: { enabled: true, min_score_count: 0, dedupe_scope: 'none' },
    })
    expect(parsed.prev_gen_carryover).toEqual({
      enabled: true,
      max_agents: 10,
      min_score_count: 0,
      include_exhausted: false,
      dedupe_scope: 'none',
      require_cohort_complete: true,
      require_desired_era_drained: true,
    })
  })

  it('rejects carryover bounds the platform would refuse', () => {
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        prev_gen_carryover: { min_score_count: 3 },
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        prev_gen_carryover: { max_agents: 51 },
      }),
    ).toThrow()
    expect(() =>
      queuePolicySettingsSchema.parse({
        ...settings,
        prev_gen_carryover: { dedupe_scope: 'ip' },
      }),
    ).toThrow()
  })

  it('requires the exact confirmation, an auditable reason, and a real revision', () => {
    expect(() =>
      setQueuePolicySettingsInputSchema.parse(
        input({ confirmation: 'apply queue policy settings' }),
      ),
    ).toThrow()
    expect(() =>
      setQueuePolicySettingsInputSchema.parse(input({ reason: 'lanes' })),
    ).toThrow()
    expect(() =>
      setQueuePolicySettingsInputSchema.parse(input({ expectedRevision: -1 })),
    ).toThrow()
  })
})

describe('validator slot settings schemas', () => {
  const settings = {
    max_concurrent_slots: 2,
    disk_percent_ceiling: 90,
    memory_percent_ceiling: 90,
    cpu_percent_ceiling: 0,
    resource_block_percent_ceiling: 95,
    paused_validator_hotkeys: [],
  }

  function input(overrides: Record<string, unknown> = {}) {
    const { settings: settingsOverride, ...rest } = overrides
    const merged = { ...settings, ...((settingsOverride as typeof settings) ?? {}) }
    return {
      scope: '*',
      expectedRevision: 0,
      reason: 'ramp the fleet to three slots now that dispatch is stable',
      confirmation: validatorSlotConfirmation(merged.max_concurrent_slots),
      ...rest,
      settings: merged,
    }
  }

  it('reads the module default when no revision has ever been written', () => {
    // The exact payload production answers today, before any operator revision.
    const parsed = validatorSlotSettingsControlSchema.parse({
      current: [],
      history: [],
      default: settings,
      effective: {
        revision: 0,
        scope: '*',
        settings,
        checksum: '',
        source: 'default',
        hard_slot_ceiling: 8,
        disk_restricted_slots: 1,
        max_age_seconds: 5.0,
      },
    })
    expect(parsed.effective.source).toBe('default')
    expect(parsed.effective.revision).toBe(0)
    expect(parsed.effective.hard_slot_ceiling).toBe(8)
    expect(parsed.effective.settings.max_concurrent_slots).toBe(2)
    expect(parsed.current).toEqual([])
  })

  it('reads a stored revision with its audit trail', () => {
    const revision = {
      revision: 1,
      parent_revision: 0,
      scope: '*',
      settings: {
        ...settings,
        max_concurrent_slots: 3,
        disk_percent_ceiling: 85,
      },
      reason: 'ramp the fleet to three slots now that dispatch is stable',
      actor: 'peyton@omniaura.ai',
      created_at: '2026-07-25T12:00:00Z',
      checksum: 'ab'.repeat(32),
    }
    const parsed = validatorSlotSettingsControlSchema.parse({
      current: [revision],
      history: [revision],
      default: settings,
      effective: {
        revision: 1,
        scope: '*',
        settings: revision.settings,
        checksum: 'ab'.repeat(32),
        source: 'revision',
        hard_slot_ceiling: 8,
        disk_restricted_slots: 1,
        max_age_seconds: 5.0,
      },
    })
    expect(parsed.effective.source).toBe('revision')
    expect(parsed.current[0].actor).toBe('peyton@omniaura.ai')
  })

  // The empty-default failure class: the platform 422s a partial body, but a
  // client that defaults its knobs pre-fills the omission into a full body and
  // the operator silently ships a default they never chose. Every knob is
  // required so the omission cannot survive the client either.
  it('rejects a partial policy rather than silently defaulting the omitted knob', () => {
    expect(() => validatorSlotSettingsSchema.parse({})).toThrow()
    expect(() => validatorSlotSettingsSchema.parse({ max_concurrent_slots: 3 })).toThrow()
    expect(() => validatorSlotSettingsSchema.parse({ disk_percent_ceiling: 85 })).toThrow()
    // The exact shape a Backroom that predates the resource ceilings sends:
    // `z.object` strips what it does not declare, so an undeclared field is
    // dropped from every write body and reset to the platform default on the
    // first operator save. Declared here BECAUSE the platform has it.
    expect(() =>
      validatorSlotSettingsSchema.parse({
        max_concurrent_slots: 3,
        disk_percent_ceiling: 90,
      }),
    ).toThrow()
    expect(() =>
      validatorSlotSettingsSchema.parse({
        max_concurrent_slots: 3,
        disk_percent_ceiling: 90,
        memory_percent_ceiling: 90,
        cpu_percent_ceiling: 0,
      }),
    ).toThrow()
    expect(() =>
      setValidatorSlotSettingsInputSchema.parse({
        expectedRevision: 0,
        settings: { max_concurrent_slots: 3 },
        reason: 'ramp the fleet to three slots now that dispatch is stable',
        confirmation: 'APPLY VALIDATOR SLOT CAP 3',
      }),
    ).toThrow()
  })

  it('holds the cap inside the protocol slot ceiling', () => {
    expect(validatorSlotSettingsSchema.parse({ ...settings, max_concurrent_slots: 1 })).toEqual({
      ...settings,
      max_concurrent_slots: 1,
    })
    expect(validatorSlotSettingsSchema.parse({ ...settings, max_concurrent_slots: 8 })).toEqual({
      ...settings,
      max_concurrent_slots: 8,
    })
    expect(() =>
      validatorSlotSettingsSchema.parse({ ...settings, max_concurrent_slots: 0 }),
    ).toThrow()
    expect(() =>
      validatorSlotSettingsSchema.parse({ ...settings, max_concurrent_slots: 9 }),
    ).toThrow()
    expect(() =>
      validatorSlotSettingsSchema.parse({ ...settings, max_concurrent_slots: 2.5 }),
    ).toThrow()
  })

  // Heartbeat cpu/memory/disk percentages all arrive on the same 5% grid, so an
  // off-grid ceiling fires at the next grid point up and misdescribes itself: 87
  // behaves exactly like 90. Every ceiling is held to it, not just disk.
  it('rejects a ceiling off the heartbeat grid or outside its range', () => {
    for (const key of [
      'disk_percent_ceiling',
      'memory_percent_ceiling',
      'cpu_percent_ceiling',
      'resource_block_percent_ceiling',
    ]) {
      expect(() => validatorSlotSettingsSchema.parse({ ...settings, [key]: 87 })).toThrow(
        /multiple of 5/,
      )
      // Below 50 a ceiling throttles a healthy host instead of protecting one.
      expect(() => validatorSlotSettingsSchema.parse({ ...settings, [key]: 45 })).toThrow()
      expect(() => validatorSlotSettingsSchema.parse({ ...settings, [key]: 105 })).toThrow()
      // Zero is the documented "do not gate on this resource", not out of range.
      expect(
        validatorSlotSettingsSchema.parse({
          ...settings,
          [key]: CEILING_DISABLED,
          resource_block_percent_ceiling: 95,
        }),
      ).toMatchObject({
        [key]: key === 'resource_block_percent_ceiling' ? 95 : CEILING_DISABLED,
      })
    }
    expect(
      validatorSlotSettingsSchema.parse({ ...settings, disk_percent_ceiling: 85 }),
    ).toEqual({ ...settings, disk_percent_ceiling: 85 })
  })

  // The hard stop is the tier ABOVE the throttle. Underneath it, the throttle can
  // never be reached, so the policy would silently mean something else.
  it('refuses a hard stop below the throttle it is supposed to sit above', () => {
    expect(() =>
      validatorSlotSettingsSchema.parse({
        ...settings,
        disk_percent_ceiling: 95,
        resource_block_percent_ceiling: 90,
      }),
    ).toThrow()
    // Disabling the hard stop leaves only the throttle, which is legal.
    expect(
      validatorSlotSettingsSchema.parse({
        ...settings,
        disk_percent_ceiling: 95,
        resource_block_percent_ceiling: CEILING_DISABLED,
      }),
    ).toMatchObject({ resource_block_percent_ceiling: CEILING_DISABLED })
  })

  // The confirmation names the resulting cap so the number is stated twice. It
  // is never derived from settings.max_concurrent_slots, which is the whole
  // point: a caller who fat-fingers one half no longer agrees with the other.
  it('requires a confirmation that names the cap this revision applies', () => {
    expect(validatorSlotConfirmation(3)).toBe('APPLY VALIDATOR SLOT CAP 3')
    expect(() =>
      setValidatorSlotSettingsInputSchema.parse(
        input({ settings: { max_concurrent_slots: 3 }, confirmation: 'APPLY VALIDATOR SLOT CAP 2' }),
      ),
    ).toThrow(/APPLY VALIDATOR SLOT CAP 3/)
    expect(() =>
      setValidatorSlotSettingsInputSchema.parse(
        input({ settings: { max_concurrent_slots: 3 }, confirmation: 'APPLY VALIDATOR SLOT CAP' }),
      ),
    ).toThrow(/APPLY VALIDATOR SLOT CAP 3/)
    expect(() =>
      setValidatorSlotSettingsInputSchema.parse(
        input({ settings: { max_concurrent_slots: 3 }, confirmation: 'apply validator slot cap 3' }),
      ),
    ).toThrow()
    expect(
      setValidatorSlotSettingsInputSchema.parse(
        input({ settings: { max_concurrent_slots: 3 }, confirmation: 'APPLY VALIDATOR SLOT CAP 3' }),
      ).settings.max_concurrent_slots,
    ).toBe(3)
  })

  it('requires an auditable reason, a real revision, and the subnet-global scope', () => {
    expect(() => setValidatorSlotSettingsInputSchema.parse(input({ reason: 'ramp' }))).toThrow()
    expect(() =>
      setValidatorSlotSettingsInputSchema.parse(input({ expectedRevision: -1 })),
    ).toThrow()
    expect(() => setValidatorSlotSettingsInputSchema.parse(input({ scope: 'v7' }))).toThrow()
    // Scope has exactly one legal value, so defaulting it cannot ship a policy
    // the operator did not choose the way a defaulted knob can.
    const { scope: _omitted, ...withoutScope } = input()
    expect(setValidatorSlotSettingsInputSchema.parse(withoutScope).scope).toBe('*')
  })

  it('requires canonical paused hotkeys and the exact per-validator confirmation', () => {
    const hotkey = `5${'A'.repeat(47)}`
    const other = `5${'B'.repeat(47)}`
    expect(
      validatorSlotSettingsSchema.parse({
        ...settings,
        paused_validator_hotkeys: [hotkey, other],
      }).paused_validator_hotkeys,
    ).toEqual([hotkey, other])
    expect(() =>
      validatorSlotSettingsSchema.parse({
        ...settings,
        paused_validator_hotkeys: [other, hotkey],
      }),
    ).toThrow(/sorted and duplicate-free/)
    expect(() =>
      validatorSlotSettingsSchema.parse({
        ...settings,
        paused_validator_hotkeys: [hotkey, hotkey],
      }),
    ).toThrow(/sorted and duplicate-free/)
    expect(() =>
      validatorSlotSettingsSchema.parse({
        ...settings,
        paused_validator_hotkeys: ['5short'],
      }),
    ).toThrow()

    const confirmation = validatorIssuanceConfirmation(hotkey, true)
    expect(confirmation).toBe(`PAUSE VALIDATOR ${hotkey}`)
    expect(
      setValidatorIssuancePauseInputSchema.parse({
        validatorHotkey: hotkey,
        paused: true,
        expectedRevision: 4,
        reason: 'drain the validator after repeated stalls',
        confirmation,
      }),
    ).toMatchObject({ validatorHotkey: hotkey, paused: true, expectedRevision: 4 })
    expect(() =>
      setValidatorIssuancePauseInputSchema.parse({
        validatorHotkey: hotkey,
        paused: true,
        expectedRevision: 4,
        reason: 'drain the validator after repeated stalls',
        confirmation: `RESUME VALIDATOR ${hotkey}`,
      }),
    ).toThrow(/PAUSE VALIDATOR/)
  })
})

describe('validator fleet schema', () => {
  it('degrades an unreadable heartbeat field instead of blanking the page', () => {
    const parsed = validatorFleetSchema.parse({
      generated_at: '2026-07-25T21:17:06Z',
      validators: [
        {
          validator_hotkey: '5HKpbkeL',
          // A validator on an older protocol reports neither multi-slot
          // capacity nor host metrics. It is still a real validator and still
          // belongs on the page, holding the one slot it advertises.
          configured_slots: 'unknown',
          online: true,
        },
      ],
    })

    expect(parsed.validators[0]).toEqual({
      validator_hotkey: '5HKpbkeL',
      configured_slots: 1,
      healthy_slot_count: 0,
      admission: 'accepting',
      active_benchmark_count: 0,
      online: true,
      // Not reported is not the same as healthy, so it stays null rather than
      // becoming a number the ceiling could be compared against.
      disk_percent: null,
      // An older platform that predates the bench-serviceability gate does not
      // do the gate, so every validator is effectively serving; a malformed
      // value on an advisory page must never blank the cap control.
      bench_serviceability: 'serving',
      orphaned_slots: [],
      updater_status: null,
    })
  })

  it('reports the active bench version the fleet is scoring now', () => {
    const parsed = validatorFleetSchema.parse({
      generated_at: '2026-07-28T12:00:00Z',
      active_bench_version: 7,
      validators: [],
    })
    expect(parsed.active_bench_version).toBe(7)
  })

  it('defaults active_bench_version to null on a platform that does not report it', () => {
    const parsed = validatorFleetSchema.parse({
      generated_at: '2026-07-28T12:00:00Z',
      validators: [],
    })
    expect(parsed.active_bench_version).toBeNull()
  })

  it('reads bench_serviceability and orphaned slots when the platform reports them', () => {
    const parsed = validatorFleetSchema.parse({
      generated_at: '2026-07-28T12:00:00Z',
      active_bench_version: 7,
      validators: [
        {
          validator_hotkey: '5Obsolete',
          configured_slots: 4,
          online: true,
          bench_serviceability: 'software_obsolete',
          orphaned_slots: [
            {
              agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
              agent_name: 'mnemox-v55',
              bench_version: 7,
              evicted_at: '2026-07-28T11:00:00Z',
              original_deadline: '2026-07-28T12:30:00Z',
              orphaned_for_seconds: 3600,
              protocol_version: 16,
              reason: 'validator_still_claims_slot',
              slot_id: 'slot-2',
              state: 'still_running',
            },
          ],
        },
      ],
    })

    expect(parsed.validators[0].bench_serviceability).toBe('software_obsolete')
    expect(parsed.validators[0].orphaned_slots).toHaveLength(1)
    expect(parsed.validators[0].orphaned_slots[0]).toMatchObject({
      slot_id: 'slot-2',
      state: 'still_running',
      orphaned_for_seconds: 3600,
    })
  })
})

describe('continual retest cohort sizing against an older platform', () => {
  const legacyEffective = {
    revision: 3,
    scope: '*',
    settings: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
    checksum: 'a'.repeat(64),
    source: 'revision',
    fleet_protocol_ready: true,
    aggregate_active: true,
    max_age_seconds: 5,
  }
  const payload = (effective: Record<string, unknown>) => ({
    current: [],
    history: [],
    default: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
    effective,
  })
  const legacySupport = {
    tie_weighting_mode: false,
    retest_cohort_size: false,
    wave_membership: false,
    retest_eligibility_mode: false,
    retest_eligibility_z: false,
    retest_cohort_max_size: false,
  }
  const fullSupport = Object.fromEntries(
    Object.keys(legacySupport).map((field) => [field, true]),
  )
  // The policy an operator would send to a build that has none of these: every
  // extended field at the value that build already behaves as.
  const legacyPolicy = {
    aggregate_mode: 'enabled' as const,
    tie_weighting_mode: 'disabled' as const,
    idle_retests_enabled: true,
    rollout_standdown: 'all' as const,
    wave_membership: 'strict' as const,
    retest_cohort_size: 5,
    retest_eligibility_mode: 'fixed' as const,
    retest_eligibility_z: 1.64,
    retest_cohort_max_size: 25,
  }

  it('tells a defaulted cohort size apart from a reported one', () => {
    // Reading defaults the field so the page still renders, which makes the
    // parsed policy useless for deciding what the platform will accept.
    expect(platformSupportsRetestCohortSize(payload(legacyEffective))).toBe(false)
    expect(
      platformSupportsRetestCohortSize(
        payload({ ...legacyEffective, settings: { ...legacyEffective.settings, retest_cohort_size: 5 } }),
      ),
    ).toBe(true)
    expect(platformSupportsRetestCohortSize(null)).toBe(false)
    expect(platformSupportsRetestCohortSize({})).toBe(false)
  })

  it('records support on the parsed control state', () => {
    const parsed = parseContinualRetestSettingsControl(payload(legacyEffective))

    expect(parsed.cohort_sizing_supported).toBe(false)
    expect(parsed.effective.settings.retest_cohort_size).toBe(5)
  })

  it('drops an unsupported field only when the request changes nothing', () => {
    const control = {
      field_support: legacySupport,
      effective: { emission_set_size: 5, max_retest_cohort_size: 25 },
    }

    // The platform request model forbids unknown fields, so every field that
    // build has never heard of has to come off or the aggregate mode and idle
    // switch go down with it.
    expect(continualRetestSettingsForPlatform(legacyPolicy, control)).toEqual({
      aggregate_mode: 'enabled',
      idle_retests_enabled: true,
      rollout_standdown: 'all',
    })
    // Asking to go deeper is a real request, and answering it with five would
    // be a lie the operator has no way to see.
    expect(() =>
      continualRetestSettingsForPlatform({ ...legacyPolicy, retest_cohort_size: 25 }, control),
    ).toThrow(/does not accept a retest cohort size/)
    // A platform that carries the fields gets the operator's policy verbatim.
    expect(
      continualRetestSettingsForPlatform(
        { ...legacyPolicy, retest_cohort_size: 25 },
        {
          field_support: fullSupport,
          effective: { emission_set_size: 5, max_retest_cohort_size: 25 },
        },
      ),
    ).toEqual({ ...legacyPolicy, retest_cohort_size: 25 })
  })

  it('guards every extended field, not just the cohort size', () => {
    const control = {
      field_support: legacySupport,
      effective: { emission_set_size: 5, max_retest_cohort_size: 25 },
    }

    // The rollback path is the one an operator reaches for in an incident, so
    // being quietly answered with `participants` is the worst possible failure.
    expect(() =>
      continualRetestSettingsForPlatform(
        { ...legacyPolicy, wave_membership: 'participants' },
        control,
      ),
    ).toThrow(/does not accept a wave membership policy/)
    expect(() =>
      continualRetestSettingsForPlatform(
        { ...legacyPolicy, retest_eligibility_mode: 'statistical' },
        control,
      ),
    ).toThrow(/does not accept a retest eligibility mode/)
    expect(() =>
      continualRetestSettingsForPlatform({ ...legacyPolicy, retest_eligibility_z: 2 }, control),
    ).toThrow(/does not accept a tie-tolerance band/)
    expect(() =>
      continualRetestSettingsForPlatform(
        { ...legacyPolicy, retest_cohort_max_size: 10 },
        control,
      ),
    ).toThrow(/does not accept a cohort ceiling/)
  })

  it('reports support per field so one new platform field needs no new helper', () => {
    const partial = payload({
      ...legacyEffective,
      settings: { ...legacyEffective.settings, wave_membership: 'participants' },
    })

    expect(continualRetestFieldSupport(partial)).toEqual({
      tie_weighting_mode: false,
      retest_cohort_size: false,
      wave_membership: true,
      retest_eligibility_mode: false,
      retest_eligibility_z: false,
      retest_cohort_max_size: false,
    })
    expect(continualRetestFieldSupport(null).wave_membership).toBe(false)
  })
})

describe('continual retest write contract', () => {
  const complete = {
    aggregate_mode: 'fleet_ready' as const,
    tie_weighting_mode: 'fleet_ready' as const,
    idle_retests_enabled: false,
    rollout_standdown: 'capable_validators' as const,
    wave_membership: 'strict' as const,
    retest_cohort_size: 10,
    retest_eligibility_mode: 'statistical' as const,
    retest_eligibility_z: 1.64,
    retest_cohort_max_size: 25,
  }

  it('refuses a body missing any field a revision would overwrite', () => {
    expect(continualRetestSettingsWriteSchema.parse(complete)).toEqual(complete)

    // A revision stores the whole policy, so each omission is a silent write of
    // the platform default over whatever is live — a reverted rollback, a
    // collapsed cohort, a discarded tie band.
    for (const field of [
      'tie_weighting_mode',
      'wave_membership',
      'retest_cohort_size',
      'retest_eligibility_mode',
      'retest_eligibility_z',
      'retest_cohort_max_size',
    ] as const) {
      const { [field]: _omitted, ...partial } = complete
      expect(continualRetestSettingsWriteSchema.safeParse(partial).success).toBe(false)
    }
  })

  it('mirrors the platform bounds so a rejectable policy never leaves this page', () => {
    expect(
      continualRetestSettingsWriteSchema.safeParse({ ...complete, retest_eligibility_z: 3.5 })
        .success,
    ).toBe(false)
    // Zero is a real setting, not a disabled one: it admits exact ties only.
    expect(
      continualRetestSettingsWriteSchema.safeParse({ ...complete, retest_eligibility_z: 0 })
        .success,
    ).toBe(true)
    expect(
      continualRetestSettingsWriteSchema.safeParse({ ...complete, retest_cohort_max_size: 4 })
        .success,
    ).toBe(false)
    expect(
      continualRetestSettingsWriteSchema.safeParse({ ...complete, retest_cohort_max_size: 26 })
        .success,
    ).toBe(false)
  })

  it('fires the cross-field rule when the ceiling cuts into the cohort', () => {
    const bad = continualRetestSettingsWriteSchema.safeParse({
      ...complete,
      retest_cohort_size: 20,
      retest_cohort_max_size: 10,
    })

    expect(bad.success).toBe(false)
    expect(bad.error?.issues[0]?.path).toEqual(['retest_cohort_max_size'])
    expect(bad.error?.issues[0]?.message).toMatch(/cannot be below retest_cohort_size/)
    // Equal is the boundary the platform allows, not one it rejects.
    expect(
      continualRetestSettingsWriteSchema.safeParse({
        ...complete,
        retest_cohort_size: 25,
        retest_cohort_max_size: 25,
      }).success,
    ).toBe(true)
  })

  it('tolerates an older platform on the read path', () => {
    // Reading has to keep the page alive against a build that predates every
    // one of these; the defaults are what that build actually does.
    expect(
      continualRetestSettingsSchema.parse({
        aggregate_mode: 'fleet_ready',
        idle_retests_enabled: false,
      }),
    ).toEqual({
      aggregate_mode: 'fleet_ready',
      tie_weighting_mode: 'disabled',
      idle_retests_enabled: false,
      rollout_standdown: 'capable_validators',
      wave_membership: 'participants',
      retest_cohort_size: 5,
      retest_eligibility_mode: 'fixed',
      retest_eligibility_z: 1.64,
      retest_cohort_max_size: 25,
    })
  })

  it('surfaces the platform bounds and what the band actually admitted', () => {
    const parsed = parseContinualRetestSettingsControl({
      current: [],
      history: [],
      default: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
      effective: {
        revision: 4,
        scope: '*',
        settings: {
          aggregate_mode: 'fleet_ready',
          idle_retests_enabled: false,
          retest_cohort_size: 5,
          retest_eligibility_mode: 'statistical',
        },
        checksum: 'b'.repeat(64),
        source: 'revision',
        fleet_protocol_ready: true,
        aggregate_active: true,
        max_age_seconds: 5,
        max_retest_eligibility_z: 3,
        resolved_cohort_size: 9,
      },
    })

    // The gap between what was asked for and what was admitted is the whole
    // point of statistical mode, so it cannot be stripped on the way through.
    expect(parsed.effective.resolved_cohort_size).toBe(9)
    expect(parsed.effective.max_retest_eligibility_z).toBe(3)
    // Absent is null rather than a fabricated number.
    expect(
      effectiveContinualRetestSettingsSchema.parse({
        revision: 1,
        scope: '*',
        settings: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
        checksum: '',
        source: 'default',
        fleet_protocol_ready: false,
        aggregate_active: false,
        max_age_seconds: 5,
      }).resolved_cohort_size,
    ).toBeNull()
  })
})

describe('validation retry ticket failure fields', () => {
  const ticket = {
    validator_hotkey: '5Validator',
    status: 'expired',
    issued_at: '2026-07-18T12:00:00Z',
    deadline: '2026-07-18T13:30:00Z',
    bench_version: 7,
    attempt_count: 2,
    manual_retry_grants: 0,
    infra_retry_grants: 0,
    retry_after: '2026-07-18T19:30:00Z',
    retry_budget_exhausted: true,
  }

  it('defaults failure fields to null on a ticket that has not failed', () => {
    const issued = validationRetryTicketSchema.parse({
      ...ticket,
      status: 'issued',
      retry_after: null,
      retry_budget_exhausted: false,
    })
    expect(issued.failed_at).toBeNull()
    expect(issued.failure_reason).toBeNull()
    expect(issued.failure_detail).toBeNull()
  })

  it('reads the coarse reason and the validator diagnostic behind it', () => {
    const parsed = validationRetryTicketSchema.parse({
      ...ticket,
      failed_at: '2026-07-18T13:05:00Z',
      failure_reason: 'sandbox_oom',
      failure_detail: 'container killed: oom-killer, peak 14.2 GiB',
    })
    expect(parsed.failed_at).toBe('2026-07-18T13:05:00Z')
    expect(parsed.failure_reason).toBe('sandbox_oom')
    expect(parsed.failure_detail).toBe('container killed: oom-killer, peak 14.2 GiB')
  })

  it('tolerates a failed ticket with no diagnostic detail', () => {
    const parsed = validationRetryTicketSchema.parse({
      ...ticket,
      failed_at: '2026-07-18T13:05:00Z',
      failure_reason: 'infrastructure',
      failure_detail: null,
    })
    expect(parsed.failure_reason).toBe('infrastructure')
    expect(parsed.failure_detail).toBeNull()
  })
})

describe('validator queue eviction schemas', () => {
  const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
  const snapshot = 'ab'.repeat(32)
  const detail = {
    agent_id: agentId,
    miner_hotkey: '5Miner',
    agent_name: 'mnemox-v55',
    agent_version: 55,
    agent_status: 'evaluating',
    score_count: 0,
    quorum: 3,
    snapshot,
    automatic_retry_available: false,
    recovery_allowed: false,
    blocking_reason: null,
    withdrawal_allowed: false,
    withdrawal_blocking_reason: 'submission can still reach quorum automatically',
    withdrawal: null,
    tickets: [],
    recoveries: [],
  }

  it('reports the live leases an eviction would free', () => {
    const parsed = validationRetryDetailSchema.parse({
      ...detail,
      eviction_allowed: true,
      eviction_blocking_reason: null,
      live_ticket_count: 3,
    })

    // The exact 2026-07-27 shape: withdrawal refuses it, eviction is the move,
    // and the operator can see how much fleet capacity is being held.
    expect(parsed.withdrawal_allowed).toBe(false)
    expect(parsed.eviction_allowed).toBe(true)
    expect(parsed.live_ticket_count).toBe(3)
  })

  it('reads null, not false, against a platform that predates the eviction route', () => {
    const parsed = validationRetryDetailSchema.parse(detail)

    // Backroom and the platform deploy separately. `false` here would read as
    // "eviction is blocked", which is a different and unearned claim from
    // "this deployment cannot tell you".
    expect(parsed.eviction_allowed).toBeNull()
    expect(parsed.eviction_blocking_reason).toBeNull()
    expect(parsed.live_ticket_count).toBeNull()
  })

  it('keeps the withdrawal hotkey list tri-state', () => {
    const withdrawal = {
      withdrawal_id: '11111111-1111-4111-8111-111111111111',
      agent_id: agentId,
      bench_version: 7,
      actor: 'operator@omniaura.ai',
      reason: 'Hung through three full leases with nothing reported',
      expected_snapshot: snapshot,
      score_count: 0,
      created_at: '2026-07-27T18:00:00Z',
    }

    // null: an ordinary withdrawal. [] an eviction that arrived after the last
    // lease had already lapsed. A list: the leases it actually took. Collapsing
    // the first two would hide which of the three happened.
    expect(
      validationQueueWithdrawalSchema.parse(withdrawal).evicted_validator_hotkeys,
    ).toBeNull()
    expect(
      validationQueueWithdrawalSchema.parse({
        ...withdrawal,
        evicted_validator_hotkeys: [],
      }).evicted_validator_hotkeys,
    ).toEqual([])
    expect(
      validationQueueWithdrawalSchema.parse({
        ...withdrawal,
        evicted_validator_hotkeys: ['5ValidatorA', '5ValidatorB'],
      }).evicted_validator_hotkeys,
    ).toEqual(['5ValidatorA', '5ValidatorB'])
  })

  it('refuses the removal phrase, so an eviction can never be typed by mistake', () => {
    const input = {
      agentId,
      expectedSnapshot: snapshot,
      reason: 'Holding three validator slots with zero scores',
    }

    expect(
      evictValidationInputSchema.parse({
        ...input,
        confirmation: EVICT_VALIDATION_CONFIRMATION,
      }).confirmation,
    ).toBe('EVICT LIVE VALIDATOR LEASES')
    // The two phrases are not interchangeable in either direction: eviction
    // destroys benchmark runs a validator may still be executing, so an
    // operator must never reach it by editing a removal call's arguments.
    expect(() =>
      evictValidationInputSchema.parse({
        ...input,
        confirmation: 'REMOVE FROM VALIDATOR QUEUE',
      }),
    ).toThrow()
    expect(() =>
      withdrawValidationInputSchema.parse({
        ...input,
        confirmation: EVICT_VALIDATION_CONFIRMATION,
      }),
    ).toThrow()
    // Same audit floor as a withdrawal: a written reason, not a shrug.
    expect(() =>
      evictValidationInputSchema.parse({
        ...input,
        reason: 'stuck',
        confirmation: EVICT_VALIDATION_CONFIRMATION,
      }),
    ).toThrow()
  })

  it('parses an eviction answer including its per-lease audit ids', () => {
    const parsed = evictValidationResponseSchema.parse({
      eviction: {
        eviction_id: '11111111-1111-4111-8111-111111111111',
        agent_id: agentId,
        bench_version: 7,
        actor: 'operator@omniaura.ai',
        reason: 'Hung through three full leases with nothing reported',
        expected_snapshot: snapshot,
        score_count: 0,
        evicted_validator_hotkeys: ['5ValidatorA'],
        created_at: '2026-07-27T18:00:00Z',
      },
      evicted_leases: [
        {
          validator_hotkey: '5ValidatorA',
          slot_id: 'slot-1',
          bench_version: 7,
          issued_at: '2026-07-27T17:00:00Z',
          original_deadline: '2026-07-27T18:30:00Z',
          attempt_count: 9,
          audit_id: '22222222-2222-4222-8222-222222222222',
        },
      ],
      freed_slots: 1,
      idempotent: false,
    })

    expect(parsed.freed_slots).toBe(1)
    // The deadline the lease would otherwise have run to is the capacity freed,
    // and the audit id is where the revocation has to be defensible later.
    expect(parsed.evicted_leases[0].original_deadline).toBe('2026-07-27T18:30:00Z')
    expect(parsed.evicted_leases[0].audit_id).toBe(
      '22222222-2222-4222-8222-222222222222',
    )
  })
})

describe('validator queue reinstatement schemas', () => {
  const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
  const snapshot = 'ab'.repeat(32)
  const evictionId = '11111111-1111-4111-8111-111111111111'
  const detail = {
    agent_id: agentId,
    miner_hotkey: '5Miner',
    agent_name: 'mnemox-v55',
    agent_version: 55,
    agent_status: 'evaluating',
    score_count: 0,
    quorum: 3,
    snapshot,
    automatic_retry_available: false,
    recovery_allowed: false,
    blocking_reason: null,
    withdrawal_allowed: false,
    withdrawal_blocking_reason: 'submission is already removed from this queue',
    withdrawal: null,
    tickets: [],
    recoveries: [],
  }
  const eviction = {
    eviction_id: evictionId,
    agent_id: agentId,
    bench_version: 7,
    actor: 'operator@omniaura.ai',
    reason: 'Hung through three full leases with nothing reported',
    expected_snapshot: snapshot,
    score_count: 0,
    evicted_validator_hotkeys: ['5ValidatorA'],
    created_at: '2026-07-27T18:00:00Z',
  }
  const reinstatement = {
    reinstatement_id: '66666666-6666-4666-8666-666666666666',
    withdrawal_id: evictionId,
    agent_id: agentId,
    bench_version: 7,
    actor: 'operator@omniaura.ai',
    reason: 'source review found no hang primitives; freeing the fleet was not a verdict',
    expected_snapshot: snapshot,
    score_count: 0,
    retry_budget_snapshot: {
      attempts_used: 2,
      agent_infra_retry_grants: 4,
      max_agent_infra_retry_grants: 12,
      manual_retry_grants: 1,
      operator_recoveries: 1,
      max_operator_recoveries: 3,
    },
    created_at: '2026-07-27T19:00:00Z',
  }

  it('reports whether an eviction can be reversed right now', () => {
    const parsed = validationRetryDetailSchema.parse({
      ...detail,
      reinstatement_allowed: true,
      reinstatement_blocking_reason: null,
      withdrawal: { ...eviction, withdrawal_id: evictionId },
    })

    expect(parsed.reinstatement_allowed).toBe(true)
    expect(parsed.reinstatement_blocking_reason).toBeNull()
    // A removal that is still in force: recorded, not yet reversed.
    expect(parsed.withdrawal?.reinstated_at).toBeNull()
  })

  it('reads null, not false, against a platform that predates the reinstate route', () => {
    const parsed = validationRetryDetailSchema.parse(detail)

    // Same rule as the eviction fields: `false` would claim the reversal is
    // blocked, which is a different and unearned statement from "this
    // deployment cannot tell you".
    expect(parsed.reinstatement_allowed).toBeNull()
    expect(parsed.reinstatement_blocking_reason).toBeNull()
    expect(parsed.reinstatement).toBeNull()
  })

  it('keeps a reversed eviction visible instead of erasing it', () => {
    const parsed = validationRetryDetailSchema.parse({
      ...detail,
      reinstatement_allowed: false,
      reinstatement_blocking_reason: 'removal has already been reinstated',
      withdrawal: {
        ...eviction,
        withdrawal_id: evictionId,
        reinstated_at: '2026-07-27T19:00:00Z',
      },
      reinstatement,
    })

    // The eviction happened, it revoked a named lease, and the lease-audit feed
    // still carries that revocation. What changed is that it is now resolved —
    // which is why a non-null `withdrawal` cannot be read as "still removed".
    expect(parsed.withdrawal?.evicted_validator_hotkeys).toEqual(['5ValidatorA'])
    expect(parsed.withdrawal?.reinstated_at).toBe('2026-07-27T19:00:00Z')
    expect(parsed.reinstatement?.withdrawal_id).toBe(evictionId)
    expect(parsed.reinstatement_allowed).toBe(false)
  })

  it('refuses both other confirmation phrases, in both directions', () => {
    const input = {
      agentId,
      expectedSnapshot: snapshot,
      reason: 'source review found no hang primitives, only latency work',
    }

    expect(
      reinstateValidationInputSchema.parse({
        ...input,
        confirmation: REINSTATE_VALIDATION_CONFIRMATION,
      }).confirmation,
    ).toBe('REINSTATE TO VALIDATOR QUEUE')
    // Three operator actions, three phrases, no overlap in any direction: an
    // operator must never reverse an eviction while believing they are taking
    // one, and `z.literal` makes that a schema failure before any network call.
    for (const wrong of [EVICT_VALIDATION_CONFIRMATION, 'REMOVE FROM VALIDATOR QUEUE']) {
      expect(() =>
        reinstateValidationInputSchema.parse({ ...input, confirmation: wrong }),
      ).toThrow()
    }
    expect(() =>
      evictValidationInputSchema.parse({
        ...input,
        confirmation: REINSTATE_VALIDATION_CONFIRMATION,
      }),
    ).toThrow()
    expect(() =>
      withdrawValidationInputSchema.parse({
        ...input,
        confirmation: REINSTATE_VALIDATION_CONFIRMATION,
      }),
    ).toThrow()
    // Same audit floor as the two removals: a written reason, not a shrug.
    expect(() =>
      reinstateValidationInputSchema.parse({
        ...input,
        reason: 'oops',
        confirmation: REINSTATE_VALIDATION_CONFIRMATION,
      }),
    ).toThrow()
  })

  it('parses a reversal answer carrying the budget it left alone', () => {
    const parsed = reinstateValidationResponseSchema.parse({
      reinstatement,
      eviction: { ...eviction, reinstated_at: '2026-07-27T19:00:00Z' },
      restored_bench_version: 7,
      idempotent: false,
    })

    expect(parsed.restored_bench_version).toBe(7)
    expect(parsed.eviction.reinstated_at).toBe('2026-07-27T19:00:00Z')
    // The anti-laundering evidence: the per-agent no-fault bound is unchanged
    // and four of its grants are still spent on the other side of the cycle.
    expect(parsed.reinstatement.retry_budget_snapshot).toEqual({
      attempts_used: 2,
      agent_infra_retry_grants: 4,
      max_agent_infra_retry_grants: 12,
      manual_retry_grants: 1,
      operator_recoveries: 1,
      max_operator_recoveries: 3,
    })
  })

  it('records a null operator-recovery cap when the era had no fixed one', () => {
    const parsed = reinstateValidationResponseSchema.parse({
      reinstatement: {
        ...reinstatement,
        retry_budget_snapshot: {
          ...reinstatement.retry_budget_snapshot,
          max_operator_recoveries: null,
        },
      },
      eviction: { ...eviction, reinstated_at: '2026-07-27T19:00:00Z' },
      restored_bench_version: 7,
      idempotent: false,
    })

    // The cap is absent, not zero: the platform widened
    // max_operator_recoveries from `number` to `number | null` because the
    // bound moved to snapshot-guarded per-ticket grants, so a reinstatement row
    // for such an era carries null. `operator_recoveries` is still the count
    // spent.
    expect(parsed.reinstatement.retry_budget_snapshot.max_operator_recoveries).toBeNull()
    expect(parsed.reinstatement.retry_budget_snapshot.operator_recoveries).toBe(1)
  })

  it('still rejects a non-positive operator-recovery cap despite the nullable widening', () => {
    // `.positive()` is retained, so zero (or a negative) is not a legal "no cap"
    // sentinel — null is the only way to say the era had no fixed per-agent cap.
    expect(() =>
      reinstateValidationResponseSchema.parse({
        reinstatement: {
          ...reinstatement,
          retry_budget_snapshot: {
            ...reinstatement.retry_budget_snapshot,
            max_operator_recoveries: 0,
          },
        },
        eviction: { ...eviction, reinstated_at: '2026-07-27T19:00:00Z' },
        restored_bench_version: 7,
        idempotent: false,
      }),
    ).toThrow()
  })
})

describe('artifact release window', () => {
  const write = (hours: number) => ({
    expectedRevision: 0,
    embargoHours: hours,
    reason: 'hold the crown source private while the disclosure review runs',
    confirmation: `SET SOURCE EMBARGO ${hours} HOURS`,
  })

  it('accepts any window between the floor and the one-year ceiling', () => {
    expect(ARTIFACT_RELEASE_MAX_HOURS).toBe(8760)
    // The default is a starting point, not the bound: the console has to let an
    // operator go past it, which is the whole point of the wider range.
    expect(ARTIFACT_RELEASE_DEFAULT_HOURS).toBeLessThan(ARTIFACT_RELEASE_MAX_HOURS)

    for (const hours of [6, 48, 100, 168, 720, 8760]) {
      expect(updateArtifactReleaseSettingsInputSchema.parse(write(hours)).embargoHours).toBe(
        hours,
      )
    }
    // Both ends still close. Widening the ceiling must not have loosened the
    // floor, or a typo could release the king's source in an hour.
    // Past a year the value is `never`, a policy rather than a duration.
    expect(() => updateArtifactReleaseSettingsInputSchema.parse(write(8761))).toThrow()
    expect(() => updateArtifactReleaseSettingsInputSchema.parse(write(5))).toThrow()
  })

  it('glosses only the windows that are hard to read in hours', () => {
    // Short windows read fine as hours; a "2 days" gloss on 48 is noise.
    expect(artifactReleaseWindowGloss(6)).toBeNull()
    expect(artifactReleaseWindowGloss(48)).toBeNull()
    expect(artifactReleaseWindowGloss(72)).toBe('3 days')
    expect(artifactReleaseWindowGloss(168)).toBe('7 days')
    expect(artifactReleaseWindowGloss(720)).toBe('30 days')
    // A custom window rarely lands on a whole day, and rounding it to one would
    // misstate when the source actually goes public.
    expect(artifactReleaseWindowGloss(100)).toBe('4d 4h')
  })
})

describe('source disclosure policy', () => {
  const write = (disclosure: string, hours = 48) => ({
    expectedRevision: 0,
    disclosure,
    embargoHours: hours,
    reason: 'subnet policy: submitted source is not published',
    confirmation:
      disclosure === 'never'
        ? 'SET SOURCE DISCLOSURE NEVER'
        : `SET SOURCE EMBARGO ${hours} HOURS`,
  })

  it('accepts exactly the two policies and nothing adjacent', () => {
    expect(updateArtifactReleaseSettingsInputSchema.parse(write('never')).disclosure).toBe(
      'never',
    )
    expect(updateArtifactReleaseSettingsInputSchema.parse(write('public')).disclosure).toBe(
      'public',
    )
    // "Never" is a policy, not a very large duration. A number here is the
    // representation this design exists to avoid.
    expect(() => updateArtifactReleaseSettingsInputSchema.parse(write('private'))).toThrow()
    expect(() => updateArtifactReleaseSettingsInputSchema.parse(write('36500'))).toThrow()
  })

  it('still requires an in-range window under a never policy', () => {
    // Mirrors the platform, which keeps `embargo_hours` NOT NULL and bounded
    // under every policy so resuming release restores an agreed window rather
    // than forcing one to be invented during the reversal.
    expect(updateArtifactReleaseSettingsInputSchema.parse(write('never', 72)).embargoHours).toBe(
      72,
    )
    expect(() => updateArtifactReleaseSettingsInputSchema.parse(write('never', 0))).toThrow()
  })

  it('declares every field the platform request model requires', () => {
    // The strip test. `z.object` drops what it does not declare and the
    // platform resets what it is not sent; on this board a missing field means
    // the subnet's release visibility changing as a side effect.
    expect(Object.keys(updateArtifactReleaseSettingsInputSchema.parse(write('never'))).sort()).toEqual(
      ['confirmation', 'disclosure', 'embargoHours', 'expectedRevision', 'reason'].sort(),
    )
  })

  it('defaults a missing policy to public rather than inheriting never', () => {
    // A platform build predating the field, or a caller that omits it, gets
    // the visible status quo. Defaulting the other way would keep the subnet
    // dark after an unrelated window change and look like nothing happened.
    const parsed = updateArtifactReleaseSettingsInputSchema.parse({
      expectedRevision: 0,
      embargoHours: 48,
      reason: 'restore the agreed window',
      confirmation: 'SET SOURCE EMBARGO 48 HOURS',
    })
    expect(parsed.disclosure).toBe('public')
    expect(artifactReleaseRevisionSchema.parse({
      revision: 1,
      parent_revision: 0,
      embargo_hours: 48,
      reason: 'seeded before the field existed',
      actor: 'migration',
      created_at: null,
    }).disclosure).toBe('public')
  })

  it('gives never its own confirmation phrase', () => {
    expect(artifactReleaseConfirmation(48, 'never')).toBe('SET SOURCE DISCLOSURE NEVER')
    // A phrase differing from the embargo one only in a number would be
    // submitted by habit, so the two share no shape.
    expect(artifactReleaseConfirmation(48, 'never')).not.toBe(artifactReleaseConfirmation(48))
    expect(artifactReleaseConfirmation(48)).toBe('SET SOURCE EMBARGO 48 HOURS')
  })
})

describe('why a validator ticket ended', () => {
  const ticket = {
    validator_hotkey: '5Validator',
    status: 'expired' as const,
    issued_at: '2026-07-27T15:00:00Z',
    deadline: '2026-07-27T16:30:00Z',
    bench_version: 7,
    attempt_count: 9,
    manual_retry_grants: 0,
    infra_retry_grants: 8,
    retry_after: null,
    retry_budget_exhausted: false,
  }

  it('surfaces the grant counter that separates a re-lease loop from silence', () => {
    const parsed = validationRetryTicketSchema.parse({
      ...ticket,
      slot_id: 'slot-1',
      failure_reason: 'infrastructure',
      failed_at: '2026-07-27T16:29:00Z',
      silently_expired: false,
    })

    // The exact 2026-07-27 shape. `expired` with a rewritten deadline is what a
    // silent lease and a reported failure both look like; only these fields
    // tell them apart. Eight no-fault grants against zero manual ones means the
    // validator was reporting fail_job(reason="infrastructure") the whole time
    // and the platform was minting a grant, raising the cap, and re-leasing.
    expect(parsed.infra_retry_grants).toBe(8)
    expect(parsed.manual_retry_grants).toBe(0)
    expect(parsed.silently_expired).toBe(false)
    expect(parsed.failure_reason).toBe('infrastructure')
    expect(parsed.retry_budget_exhausted).toBe(false)
  })

  it('keeps infra_retry_grants required, because a missing one is a contract break', () => {
    // The platform has returned it since ditto-platform #264, so absence is not
    // an old deployment; it is a response Backroom must not quietly accept and
    // then render as a submission with no no-fault grants at all.
    const { infra_retry_grants: _dropped, ...withoutGrants } = ticket
    expect(() => validationRetryTicketSchema.parse(withoutGrants)).toThrow()
  })

  it('reads null, not false, for the fields ditto-platform #515 has not shipped', () => {
    const parsed = validationRetryTicketSchema.parse(ticket)

    // #515 is still an unmerged draft. `false` for silently_expired would claim
    // "this expiry came with a reported reason", which is exactly the wrong
    // conclusion to hand an operator mid-incident.
    expect(parsed.silently_expired).toBeNull()
    expect(parsed.failure_reason).toBeNull()
    expect(parsed.failed_at).toBeNull()
    expect(parsed.slot_id).toBeNull()
  })

  it('counts silent expiries per submission, and survives the summary view', () => {
    const submission = {
      agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
      miner_hotkey: '5Miner',
      agent_name: 'mnemox-v55',
      agent_version: 55,
      bench_version: 7,
      score_count: 0,
      quorum: 3,
      retry_state: 'running' as const,
      automatic_retry_available: true,
      recovery_allowed: false,
      blocking_reason: null,
      earliest_retry_after: null,
      attempts_used: 9,
      exhausted_validator_count: 0,
      snapshot: 'ab'.repeat(32),
      ticket_states: {},
    }

    // silent_expiry_count is a submission field, not ticket history, so it
    // reaches an operator through the compact list. A count that climbs while score_count
    // stays at zero is a submission that is hanging, not merely slow.
    expect(
      stuckSubmissionSchema.parse({ ...submission, silent_expiry_count: 9 }),
    ).toMatchObject({ silent_expiry_count: 9, score_count: 0 })
    expect(stuckSubmissionSchema.parse(submission).silent_expiry_count).toBeNull()
  })

  it('defaults stuck-submission triage to a small server page', () => {
    expect(listStuckSubmissionsInputSchema.parse({})).toEqual({
      limit: 10,
      offset: 0,
    })
    expect(
      listStuckSubmissionsInputSchema.parse({ limit: 200, offset: 10 }),
    ).toMatchObject({ limit: 200, offset: 10 })
    expect(() => listStuckSubmissionsInputSchema.parse({ limit: 201 })).toThrow()
    expect(() => listStuckSubmissionsInputSchema.parse({ offset: -1 })).toThrow()
  })
})

describe('lease revocation reads', () => {
  it('bounds paging and leaves the two indexed filters optional', () => {
    expect(listLeaseRevocationsInputSchema.parse({})).toEqual({
      limit: 50,
      offset: 0,
    })
    expect(
      listLeaseRevocationsInputSchema.parse({
        agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
        validatorHotkey: '5Validator',
        action: ['operator_evicted'],
        context: ['issue_ticket'],
        since: '2026-07-27T00:00:00Z',
        limit: 200,
        offset: 10,
      }).limit,
    ).toBe(200)
    expect(() => listLeaseRevocationsInputSchema.parse({ limit: 201 })).toThrow()
    expect(() => listLeaseRevocationsInputSchema.parse({ limit: 0 })).toThrow()
    expect(() => listLeaseRevocationsInputSchema.parse({ offset: -1 })).toThrow()
  })

  it('returns evidence whole, whatever keys the reason code carries', () => {
    const parsed = leaseRevocationsListSchema.parse({
      generated_at: '2026-07-27T18:00:00Z',
      total: 1,
      revocations: [
        {
          audit_id: '22222222-2222-4222-8222-222222222222',
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          validator_hotkey: '5Validator',
          slot_id: 'slot-1',
          bench_version: 7,
          action: 'force_expired',
          reason: 'idle_capacity_reports_slot_free',
          context: 'issue_ticket',
          recorded_at: '2026-07-27T17:59:00Z',
          evidence: {
            heartbeat_seen_at: '2026-07-27T17:58:00Z',
            lease_age_seconds: 5400,
            original_deadline: '2026-07-27T18:30:00Z',
            attempt_count: 9,
            capacity: { running: [], free_slots: ['slot-1'] },
          },
        },
      ],
    })

    // `reason` alone is a bare code; the evidence is the verdict's basis, and
    // its keys vary per reason code by construction. A closed schema here would
    // drop exactly the fields an unusual revocation makes interesting, so
    // nothing is declared and nothing is stripped.
    expect(parsed.revocations[0].evidence).toEqual({
      heartbeat_seen_at: '2026-07-27T17:58:00Z',
      lease_age_seconds: 5400,
      original_deadline: '2026-07-27T18:30:00Z',
      attempt_count: 9,
      capacity: { running: [], free_slots: ['slot-1'] },
    })
  })

  it('accepts an action or context lane it has never seen', () => {
    const row = {
      audit_id: '22222222-2222-4222-8222-222222222222',
      agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
      validator_hotkey: '5Validator',
      slot_id: 'slot-1',
      bench_version: 7,
      action: 'operator_evicted',
      reason: 'operator_evicted_occupied_not_progressing',
      context: 'a_lane_that_does_not_exist_yet',
      recorded_at: '2026-07-27T17:59:00Z',
      evidence: {},
    }

    // The platform types these as plain `str` rather than a Literal precisely so
    // a new lane never turns an operator's read into a 500. Backroom must not
    // be stricter than the contract it wraps.
    expect(
      leaseRevocationsListSchema.parse({
        generated_at: '2026-07-27T18:00:00Z',
        total: 1,
        revocations: [row],
      }).revocations[0].action,
    ).toBe('operator_evicted')
  })

  it('parses an empty ledger as a finding rather than a failure', () => {
    // As of 2026-07-27 validator_lease_audit is empty in production:
    // force_expire_lease has never fired. Emptiness is a real answer — the
    // platform revoked nothing, so the run died some other way — and reading it
    // as "the feature is not wired up" is the misstep that cost a day.
    const parsed = leaseRevocationsListSchema.parse({
      generated_at: '2026-07-27T18:00:00Z',
      total: 0,
      revocations: [],
    })
    expect(parsed.total).toBe(0)
    expect(parsed.revocations).toEqual([])
  })
})

describe('public leaderboard rows that do not rank', () => {
  const rankedEntry = (
    overrides: Record<string, unknown> = {},
  ): Record<string, unknown> => ({
    rank: 1,
    finalized: true,
    score_count: 3,
    score_quorum: 3,
    agent_id: '3f1d6b8a-1c2e-4a5b-8d90-1f2e3a4b5c6d',
    agent_name: 'champion',
    miner_hotkey: '5Champion',
    composite: 0.71,
    tool_mean: 0.74,
    memory_mean: 0.68,
    first_seen: '2026-08-01T00:00:00Z',
    eligible: true,
    bench_version: 9,
    ...overrides,
  })

  const board = (entries: Record<string, unknown>[]): Record<string, unknown> => ({
    generated_at: '2026-08-13T00:00:00Z',
    count: entries.length,
    current_bench_version: 9,
    active_bench_version: 9,
    desired_bench_version: 9,
    available_bench_versions: [8, 9],
    selection_mode: 'authoritative',
    entries,
  })

  it('reads the whole board when a provisional v9 row carries a null rank', () => {
    // Reproduced against production on 2026-08-13: 4 of 36 entries came back
    // with `rank: null` and a required `z.number()` failed the entire response,
    // so `get_leaderboard` returned NO data at all rather than 32 ranked rows
    // plus 4 unranked ones. The blast radius is the point of this test: one
    // unranked row must never cost an operator the other 35.
    const provisional = ['Forever', 'Bmars_v9', 'unitao', 'warrior-v9'].map(
      (agent_name, index) =>
        rankedEntry({
          rank: null,
          finalized: false,
          score_count: 1,
          agent_id: `3f1d6b8a-1c2e-4a5b-8d90-1f2e3a4b5c6${index}`,
          agent_name,
          miner_hotkey: `5Provisional${index}`,
          eligible: false,
        }),
    )

    const parsed = publicLeaderboardSchema.parse(
      board([rankedEntry(), ...provisional]),
    )

    expect(parsed.entries).toHaveLength(5)
    expect(parsed.entries[0].rank).toBe(1)
    expect(parsed.entries.slice(1).map((entry) => entry.rank)).toEqual([
      null,
      null,
      null,
      null,
    ])
    expect(parsed.entries.slice(1).map((entry) => entry.agent_name)).toEqual([
      'Forever',
      'Bmars_v9',
      'unitao',
      'warrior-v9',
    ])
  })

  it('normalizes an omitted rank to null rather than undefined', () => {
    // The Platform field carries a default, so it is optional in the OpenAPI
    // schema even though the serializer emits an explicit null today. Callers
    // get one shape to branch on either way.
    const { rank: _omitted, ...withoutRank } = rankedEntry()
    const parsed = publicLeaderboardSchema.parse(board([withoutRank]))

    expect(parsed.entries[0].rank).toBeNull()
  })

  it('still rejects a rank that is not a positive integer', () => {
    // Nullable is not a licence to accept junk: 0 and -1 are contract
    // violations, not "unranked".
    for (const rank of [0, -1, 1.5]) {
      expect(() => publicLeaderboardSchema.parse(board([rankedEntry({ rank })]))).toThrow()
    }
  })

  it('accepts every rank the generated Platform type can send', () => {
    // The drift this guards: Platform declared `rank: int | None` in PR #583
    // while this schema kept PR #350's required `rank`, and nothing failed
    // until a real null reached production.
    expectTypeOf<
      PlatformComponents['schemas']['PublicLeaderboardEntry']['rank']
    >().toMatchTypeOf<
      ZodInput<typeof publicLeaderboardSchema>['entries'][number]['rank']
    >()
  })
})
