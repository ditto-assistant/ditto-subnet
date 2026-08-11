import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  startBenchmarkRollout,
  expandBenchmarkRollout,
  supersedeBenchmarkRollout,
  selectActiveBenchmark,
  fetchAthReview,
  fetchBenchmarkRolloutControl,
  fetchCopyReviews,
  fetchBenchmarkContractRefresh,
  fetchScreenedImageRebuild,
  fetchBenchmarkContractMigration,
  fetchScreeningSubmission,
  fetchScreeningSubmissions,
  fetchOwnerAttestations,
  fetchScreeningDisputes,
  fetchValidationRetry,
  fetchValidatorScoreReplacement,
  invalidateCopyReviewsCache,
  openAthReview,
  resolveCopyReview,
  retryValidation,
  withdrawValidation,
  evictValidation,
  reinstateValidation,
  fetchLeaseRevocations,
  replaceValidatorScore,
  fetchScoreOutliers,
  fetchV9ContractRetests,
  releaseValidatorScoreRetest,
  queueValidatorScoreRetests,
  refreshBenchmarkContract,
  rebuildScreenedImage,
  migrateBenchmarkContract,
  fetchInferenceRoutes,
  calibrateInferenceRoute,
  updateInferenceRoutingPolicy,
  fetchArtifactReleaseControl,
  updateArtifactReleaseSettings,
  fetchSubmissionSettingsControl,
  updateSubmissionSettings,
  fetchBurnSettings,
  setBurnSettings,
  fetchContinualRetestSettings,
  setContinualRetestSettings,
  fetchQueuePolicySettings,
  setQueuePolicySettings,
  fetchValidatorSlotSettings,
  setValidatorSlotSettings,
  fetchValidatorFleet,
  fetchAgentScores,
  fetchAgentScoreHistory,
  fetchScoreLeaderboard,
  fetchOwnerFootprint,
  authorizeConfirmationBundleRetest,
  fetchConfirmationBundle,
  fetchConfirmationBundles,
  fetchConfirmationBundleSettings,
  setConfirmationBundleSettings,
} from './admin.service'
import { deriveRequestId } from '../lib/idempotency'

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const originalBaseUrl = process.env.DITTO_PLATFORM_API_BASE_URL

const review = {
  review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  agent_id: '11111111-1111-4111-8111-111111111111',
  miner_hotkey: '5Miner',
  agent_name: 'held-agent',
  agent_version: 2,
  submitted_at: '2026-07-15T12:00:00Z',
  status: 'pending',
  opened_at: '2026-07-15T13:00:00Z',
  resolved_at: null,
  resolved_by: null,
  resolution: null,
  resolution_reason: null,
  original: {
    review_kind: 'copy',
    duplicate_of: '22222222-2222-4222-8222-222222222222',
    reason: 'legacy hold',
    policy_version: 1,
    fingerprint_versions: { lexical: null, structural: null, prompt: null },
    reference_provenance: 'legacy',
    backfilled: true,
  },
}

const similarity = {
  candidate_version: 2,
  reference_version: 2,
  compatible: true,
  applicable: true,
  candidate_cardinality: 100,
  reference_cardinality: 90,
  jaccard: 0.1,
  containment: 0.2,
  above_threshold: false,
  decision_role: 'trigger',
}

const comparison = {
  availability: 'available',
  bulk_eligible: true,
  algorithm_version: 'reference-aware-v2',
  lexical_fingerprint_version: 2,
  normalized_source_fingerprint_version: 'v2',
  prompt_fingerprint_version: 'p2',
  canonical_reference_revision: '959cd69',
  reference_corpus_id: '21dc06cd',
  reference_exclusion_mode: 'starter-kit-mainline-history',
  miner_exclusion_mode: 'cross-miner-only',
  same_miner_excluded: false,
  chronology_direction: 'reference-before-candidate',
  chronology_eligible: true,
  exact_byte_match: false,
  normalized_source_match: false,
  lexical: similarity,
  structural: { ...similarity, decision_role: 'advisory' },
  prompt: { ...similarity, candidate_version: 'p2', reference_version: 'p2', decision_role: 'advisory' },
  triggered: false,
  triggered_signal: null,
  current_decision: 'clear',
}

const benchmarkRollout = {
  active_version: 5,
  desired_version: 5,
  status: 'activated',
  blocked_reason: null,
  capability_bench_version: 6,
  canary_capable_validator_count: 4,
  v3_capable_validator_count: 4,
  current_hybrid_top_five: ['11111111-1111-4111-8111-111111111111'],
  qualification_converged: true,
  members: [
    {
      agent_id: '11111111-1111-4111-8111-111111111111',
      position: 1,
      score_count: 3,
      currently_top_five: true,
    },
  ],
  contracts: [
    {
      version: 5,
      minimum_screening_policy_version: 9,
      requires_screened_image: true,
      capable_validator_count: 4,
    },
    {
      version: 6,
      minimum_screening_policy_version: 9,
      requires_screened_image: true,
      capable_validator_count: 4,
    },
  ],
  available_target_versions: [6],
  active_contract_candidates: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
  invalidateCopyReviewsCache()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
  if (originalBaseUrl === undefined) delete process.env.DITTO_PLATFORM_API_BASE_URL
  else process.env.DITTO_PLATFORM_API_BASE_URL = originalBaseUrl
})

describe('Bench v9 confirmation bundle administration', () => {
  const digest = 'a'.repeat(64)
  const timestamp = '2026-08-08T12:00:00Z'
  const bundleId = '11111111-1111-4111-8111-111111111111'
  const requestId = '22222222-2222-4222-8222-222222222222'
  const nextBundleId = '33333333-3333-4333-8333-333333333333'
  const actor = 'operator@example.com'
  const settings = {
    mode: 'shadow',
    top_n: 5,
    daily_bundle_cap: 20,
    daily_dollar_cap_microusd: 2_000_000,
    per_bundle_request_cap: 500,
    per_bundle_token_cap: 2_000_000,
    profile_revision: 'v9-confirmation-shadow-1',
    profile_checksum: digest,
    challenger_z: 1.64,
  }
  const offSettings = {
    mode: 'off',
    top_n: 5,
    daily_bundle_cap: 0,
    daily_dollar_cap_microusd: 0,
    per_bundle_request_cap: 0,
    per_bundle_token_cap: 0,
    profile_revision: null,
    profile_checksum: null,
    challenger_z: 1.64,
  }
  const revision = {
    revision: 1,
    parent_revision: 0,
    scope: '*',
    settings,
    checksum: digest,
    reason: 'measure confirmation cost before enforcement',
    actor,
    created_at: timestamp,
  }
  const settingsControl = {
    current: [revision],
    history: [revision],
    default: offSettings,
    effective: {
      revision: 1,
      scope: '*',
      settings,
      checksum: digest,
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
  const pendingBundle = {
    bundle_id: bundleId,
    artifact_sha256: digest,
    bench_version: 9,
    profile_revision: 'v9-confirmation-shadow-1',
    profile_checksum: digest,
    retest_generation: 0,
    generation_reason: 'initial',
    source_bundle_id: null,
    state: 'pending',
    settings_revision: 1,
    settings_checksum: digest,
    qualification_status: null,
    completion_mode: null,
    completion_ticket_id: null,
    evidence_sha256: null,
    reporter_hotkey: null,
    bundle_signature: null,
    evidence_root: null,
    verified_at: null,
    completed_at: null,
    created_at: timestamp,
    updated_at: timestamp,
    subjects: [],
    dimensions: [],
    tickets: [],
  }
  const listResponse = {
    items: [pendingBundle],
    count: 1,
    budget: {
      utc_day: '2026-08-08',
      revision: 1,
      issued_attempts: 1,
      outstanding_reserved_microusd: 50_000,
      settled_microusd: 0,
    },
    shadow_calibration: {
      observed_from_utc_day: '2026-08-08',
      observed_through_utc_day: '2026-08-08',
      observation_days: 1,
      confirmation_profile_revision: 'v9-confirmation-shadow-1',
      confirmation_profile_checksum: digest,
      base_run_count: 4,
      measured_base_cost_microusd: 130_000,
      confirmation_bundle_count: 1,
      measured_bundle_cost_microusd: 60_000,
      completed_bundle_count: 1,
      qualified_bundle_count: 1,
      promotion_rate_bps: 10_000,
      projected_daily_spend_microusd: 580_000,
      epoch_duration_seconds: null,
      projected_epoch_spend_microusd: null,
      epoch_projection_unavailable_reason:
        'Bench v9 has no configured epoch duration; no projection was guessed.',
    },
  }

  it('reads the strict settings control from the Platform admin boundary', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(settingsControl))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchConfirmationBundleSettings()).resolves.toEqual(settingsControl)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundle-settings',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('sends a complete settings revision with exact actor and confirmation, then refreshes', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(revision))
      .mockResolvedValueOnce(Response.json(settingsControl))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setConfirmationBundleSettings(
        {
          scope: '*',
          expectedRevision: 0,
          settings,
          reason: revision.reason,
          confirmation: 'APPLY V9 CONFIRMATION MODE SHADOW',
        },
        actor,
      ),
    ).resolves.toEqual(settingsControl)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundle-settings',
    )
    expect(init.headers).toEqual(
      expect.objectContaining({
        'X-Admin-Actor': actor,
        Authorization: 'Bearer secret',
      }),
    )
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 0,
      settings,
      reason: revision.reason,
      actor,
      confirmation: 'APPLY V9 CONFIRMATION MODE SHADOW',
    })
  })

  it('rejects a partial or misconfirmed settings write before contacting Platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setConfirmationBundleSettings(
        {
          scope: '*',
          expectedRevision: 0,
          settings: { ...settings, per_bundle_request_cap: 0 },
          reason: revision.reason,
          confirmation: 'APPLY V9 CONFIRMATION MODE SHADOW',
        },
        actor,
      ),
    ).rejects.toThrow(/per_bundle_request_cap/)
    await expect(
      setConfirmationBundleSettings(
        {
          scope: '*',
          expectedRevision: 0,
          settings,
          reason: revision.reason,
          confirmation: 'APPLY V9 CONFIRMATION MODE ENFORCE',
        },
        actor,
      ),
    ).rejects.toThrow(/confirmation must be exactly/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('lists bounded bundle state without dropping budget or integer evidence fields', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(listResponse))
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchConfirmationBundles({ state: 'pending', limit: 25 })
    const requestUrl = new URL((fetchMock.mock.calls[0] as [string])[0])
    expect(requestUrl.pathname).toBe('/api/v1/admin/confirmation-bundles')
    expect(requestUrl.searchParams.get('state')).toBe('pending')
    expect(requestUrl.searchParams.get('limit')).toBe('25')
    expect(requestUrl.searchParams.get('offset')).toBe('0')
    expect(parsed.budget.outstanding_reserved_microusd).toBe(50_000)
    expect(parsed.shadow_calibration.measured_base_cost_microusd).toBe(130_000)
    expect(parsed.shadow_calibration.projected_epoch_spend_microusd).toBeNull()
    expect(parsed.items[0].settings_checksum).toBe(digest)
  })

  it('reads one exact bundle id and fails closed on stale audit visibility', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(pendingBundle))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchConfirmationBundle({ bundleId })).resolves.toEqual(pendingBundle)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundles/${bundleId}`,
    )

    const { settings_checksum: _removed, ...stale } = pendingBundle
    fetchMock.mockResolvedValueOnce(Response.json(stale))
    await expect(fetchConfirmationBundle({ bundleId })).rejects.toThrow(
      /settings_checksum/,
    )
  })

  it('authorizes one generation-bound retest with idempotency, actor, and exact phrase', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const nextBundle = {
      ...pendingBundle,
      bundle_id: nextBundleId,
      retest_generation: 1,
      generation_reason: 'operator_retest',
      source_bundle_id: bundleId,
    }
    const response = {
      authorization_id: requestId,
      superseded_bundle_id: bundleId,
      bundle: nextBundle,
      replayed: false,
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(response))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      authorizeConfirmationBundleRetest(
        {
          bundleId,
          requestId,
          expectedGeneration: 0,
          reason: 'provider recovered and fresh evidence is required',
          confirmation: 'AUTHORIZE CONFIRMATION BUNDLE RETEST',
        },
        actor,
      ),
    ).resolves.toEqual(response)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      `https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundles/${bundleId}/authorize-retest`,
    )
    expect(init.headers).toEqual(expect.objectContaining({ 'X-Admin-Actor': actor }))
    expect(JSON.parse(String(init.body))).toEqual({
      request_id: requestId,
      expected_generation: 0,
      reason: 'provider recovered and fresh evidence is required',
      actor,
      confirmation: 'AUTHORIZE CONFIRMATION BUNDLE RETEST',
    })
  })

  it('surfaces Platform CAS refusals with the required re-read recovery', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json(
          { detail: 'confirmation generation changed concurrently' },
          { status: 409 },
        ),
      ),
    )

    await expect(
      authorizeConfirmationBundleRetest(
        {
          bundleId,
          requestId,
          expectedGeneration: 0,
          reason: 'provider recovered and fresh evidence is required',
          confirmation: 'AUTHORIZE CONFIRMATION BUNDLE RETEST',
        },
        actor,
      ),
    ).rejects.toThrow(/Nothing was applied.*get_confirmation_bundle/s)
  })
})

describe('inference route administration', () => {
  const route = {
    model: 'openai/gpt-oss-20b', provider: 'Weights & Biases',
    profile_revision: 'oss-wandb-fp8-v1', quantization: 'fp8', status: 'healthy',
    calibration_status: 'shadow', calibration_revision: 2,
    calibration_manifest_sha256: null,
    calibration_sample_count: 0, calibration_tool_accuracy: null,
    calibration_composite: null, sample_count: 10, selected_ticket_count: 4,
    exploration_ticket_count: 1, last_selected_at: null,
    ewma_tokens_per_second: 160, ewma_latency_ms: 260,
    ewma_error_rate: 0.01, ewma_timeout_rate: 0,
    prompt_price_per_token: 0.00000003, completion_price_per_token: 0.00000013,
    updated_at: '2026-07-22T00:00:00Z',
  }
  const policy = {
    model: route.model, revision: 3, enabled: false, speed_weight: 0.5, cost_weight: 0.4,
    exploration_weight: 0.1, exploration_ticket_budget: 5,
    min_tool_accuracy: 0.8, min_composite: 0.7, min_calibration_samples: 60,
    max_error_rate: 0.05, max_timeout_rate: 0.03, cooldown_seconds: 300,
    ewma_alpha: 0.2, updated_at: '2026-07-22T00:00:00Z',
  }
  const inventory = {
    routing_mode: 'adaptive', aggregate_route: null,
    policies: [policy], routes: [route], audits: [],
    provider_telemetry: [],
    relay_recovery_telemetry: {
      benchmark_relay_abort_ticket_count: 0,
      broker_recovery_exhausted_ticket_count: 0,
    },
  }

  it('reads the aggregate route inventory through the platform admin boundary', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(inventory))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchInferenceRoutes()).resolves.toEqual(inventory)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/inference-routes',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('writes reviewed calibration evidence and refreshes the route inventory', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const digest = 'ab'.repeat(32)
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ profile_revision: route.profile_revision }))
      .mockResolvedValueOnce(Response.json({
        routing_mode: 'adaptive',
        policies: [policy], routes: [{ ...route, calibration_status: 'eligible' }],
        audits: [],
      }))
    vi.stubGlobal('fetch', fetchMock)

    await calibrateInferenceRoute('operator@omniaura.ai', {
      profileRevision: route.profile_revision, model: route.model, provider: route.provider,
      expectedRevision: route.calibration_revision,
      action: 'eligible', manifestSha256: digest, toolAccuracy: 0.91,
      composite: 0.84, sampleCount: 60,
      confirmation: `ELIGIBLE INFERENCE ROUTE ${route.profile_revision}`,
    })

    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/inference-routes/oss-wandb-fp8-v1/calibration',
    )
    expect(request.headers).toMatchObject({ 'X-Admin-Actor': 'operator@omniaura.ai' })
    expect(JSON.parse(String(request.body))).toEqual({
      model: route.model, provider: route.provider, action: 'eligible',
      expected_revision: route.calibration_revision,
      manifest_sha256: digest, tool_accuracy: 0.91, composite: 0.84,
      sample_count: 60,
      confirmation: `ELIGIBLE INFERENCE ROUTE ${route.profile_revision}`,
    })
  })

  it('replaces one model policy with exact operator attribution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ model: route.model, enabled: true }))
      .mockResolvedValueOnce(Response.json(inventory))
    vi.stubGlobal('fetch', fetchMock)

    await updateInferenceRoutingPolicy('operator@omniaura.ai', {
      model: route.model, expectedRevision: policy.revision,
      enabled: true, speedWeight: 0.7, costWeight: 0.3,
      explorationWeight: 0, explorationTicketBudget: 5, minToolAccuracy: 0.8,
      minComposite: 0.7, minCalibrationSamples: 60, maxErrorRate: 0.05,
      maxTimeoutRate: 0.03, cooldownSeconds: 300, ewmaAlpha: 0.2,
      confirmation: `UPDATE INFERENCE POLICY ${route.model}`,
    })

    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/inference-routes/policy/openai%2Fgpt-oss-20b',
    )
    expect(request.method).toBe('PUT')
    expect(request.headers).toMatchObject({ 'X-Admin-Actor': 'operator@omniaura.ai' })
    expect(JSON.parse(String(request.body))).toMatchObject({ expected_revision: policy.revision })
  })
})

describe('screening submission admin service', () => {
  it('forwards explicit pagination for screening history and disputes', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ items: [], count: 294 }))
      .mockResolvedValueOnce(Response.json({ items: [], count: 73 }))
    vi.stubGlobal('fetch', fetchMock)

    await fetchScreeningSubmissions(50, 100)
    await fetchScreeningDisputes('pending', 50, 50)

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/screening-submissions?limit=50&offset=100',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/screening-disputes?status=pending&limit=50&offset=50',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('gets one exact submission without requesting artifact data', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const submission = {
      agent_id: agentId,
      miner_hotkey: '5Miner',
      agent_name: 'exact-agent',
      agent_version: 2,
      artifact_sha256: 'ab'.repeat(32),
      agent_status: 'scored',
      screening_policy_version: 9,
      screening_reason: null,
      screening_reason_code: null,
      submitted_at: '2026-07-19T12:00:00Z',
      attempts: [],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(submission))
    vi.stubGlobal('fetch', fetchMock)

    // A platform that predates the coldkey field parses as "unknown", not as
    // a schema failure that would take the whole review surface down.
    await expect(fetchScreeningSubmission({ agentId })).resolves.toEqual({
      ...submission,
      miner_coldkey: null,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}`,
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('passes the payment coldkey through on a submission read', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        miner_hotkey: '5Miner',
        miner_coldkey: '5Cold',
        agent_name: 'exact-agent',
        agent_version: 2,
        artifact_sha256: 'ab'.repeat(32),
        agent_status: 'scored',
        screening_policy_version: 9,
        screening_reason: null,
        screening_reason_code: null,
        submitted_at: '2026-07-19T12:00:00Z',
        attempts: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchScreeningSubmission({ agentId })
    expect(detail.miner_coldkey).toBe('5Cold')
  })
})

describe('benchmark rollout admin service', () => {
  it('reads rollout state without issuing the start request', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(benchmarkRollout))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchBenchmarkRolloutControl()

    expect(result.status).toBe('activated')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('retries a transient status timeout once instead of failing the page', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new DOMException('aborted due to timeout', 'TimeoutError'))
      .mockResolvedValueOnce(Response.json(benchmarkRollout))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchBenchmarkRolloutControl()

    expect(result.status).toBe('activated')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('blames read latency, not the token, when the status read keeps timing out', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockRejectedValue(new DOMException('aborted due to timeout', 'TimeoutError'))
    vi.stubGlobal('fetch', fetchMock)

    // The bug this replaces: an operator read "confirm platform exposes the
    // authenticated rollout-status endpoint and Backroom has its admin token"
    // off a timeout, and spent an hour on an endpoint and a token that were
    // both fine.
    const error = await fetchBenchmarkRolloutControl().catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(Error)
    const message = (error as Error).message
    expect(message).toContain('did not answer')
    expect(message).toContain('latency')
    expect(message).toContain('not the admin token')
    // Retries are bounded: two attempts, then an answer.
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('names the admin token only when the platform actually rejects it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValue(Response.json({ detail: 'invalid admin token' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)

    const error = await fetchBenchmarkRolloutControl().catch((cause: unknown) => cause)

    expect((error as Error).message).toContain('rejected')
    expect((error as Error).message).toContain('DITTO_ADMIN_API_TOKEN')
    // 401 is a verdict, not a blip: retrying it just repeats the rejection.
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('reports a platform fault as a platform fault', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    // A fresh Response per attempt: a body can only be read once, and the
    // retry genuinely re-fetches.
    const fetchMock = vi.fn(async () =>
      Response.json(
        { message: 'benchmark rollout status exceeded its 12s read budget' },
        { status: 503 },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const error = await fetchBenchmarkRolloutControl().catch((cause: unknown) => cause)

    const message = (error as Error).message
    expect(message).toContain('503')
    expect(message).toContain('read budget')
    expect(message).not.toContain('DITTO_ADMIN_API_TOKEN')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('carries a degraded section through so the console can say so', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          ...benchmarkRollout,
          active_contract_candidates: [],
          degraded_sections: ['active_contract_candidates'],
        }),
      ),
    )

    const result = await fetchBenchmarkRolloutControl()

    expect(result.degraded_sections).toEqual(['active_contract_candidates'])
  })

  it('treats a platform without the bounded read as nothing omitted', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(benchmarkRollout)))

    const result = await fetchBenchmarkRolloutControl()

    expect(result.degraded_sections).toEqual([])
  })

  it('starts a selected rollout with CAS, reason, confirmation, and attribution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: 'collecting' }))
      .mockResolvedValueOnce(
        Response.json({ ...benchmarkRollout, desired_version: 6, status: 'collecting' }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const result = await startBenchmarkRollout('operator@omniaura.ai', {
      desiredVersion: 6,
      expectedActiveVersion: 5,
      reason: 'v6 scorer capacity verified',
      confirmation: 'START BENCHMARK V6',
    })

    expect(result.status).toBe('collecting')
    expect(timeoutSpy).toHaveBeenNthCalledWith(1, 120_000)
    // The follow-up status read gets the status endpoint's own budget, which
    // outlasts the platform's server-side read budget on purpose.
    expect(timeoutSpy).toHaveBeenNthCalledWith(2, 25_000)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/6',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer secret',
          'X-Admin-Actor': 'operator@omniaura.ai',
        }),
        body: JSON.stringify({
          actor: 'operator@omniaura.ai',
          reason: 'v6 scorer capacity verified',
          confirmation: 'START BENCHMARK V6',
          expected_active_version: 5,
        }),
      }),
    )
  })

  it('expands an open rollout with frozen-target guards and attribution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        active_version: 7,
        desired_version: 8,
        status: 'collecting',
        blocked_reason: null,
        capability_bench_version: 8,
        canary_capable_validator_count: 4,
        v3_capable_validator_count: 4,
        current_hybrid_top_five: [],
        qualification_converged: false,
        members: [],
        expansion: { previous_target: 10, new_target: 15, appended_members: 5 },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await expandBenchmarkRollout('operator@omniaura.ai', {
      desiredVersion: 8,
      expectedActiveVersion: 7,
      expectedCurrentTarget: 10,
      newTarget: 15,
      reason: 'restore the intended top fifteen rollout cohort',
      confirmation: 'EXPAND BENCHMARK V8 TO 15',
    })

    expect(result.expansion).toEqual({
      previous_target: 10,
      new_target: 15,
      appended_members: 5,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/8/expand',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer secret',
          'X-Admin-Actor': 'operator@omniaura.ai',
        }),
        body: JSON.stringify({
          actor: 'operator@omniaura.ai',
          reason: 'restore the intended top fifteen rollout cohort',
          confirmation: 'EXPAND BENCHMARK V8 TO 15',
          expected_active_version: 7,
          expected_current_target: 10,
          new_target: 15,
        }),
      }),
    )
  })

  it('supersedes only the selected open version with a distinct confirmation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: 'superseded' }))
      .mockResolvedValueOnce(
        Response.json({ ...benchmarkRollout, desired_version: 6, status: 'superseded' }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const result = await supersedeBenchmarkRollout('operator@omniaura.ai', {
      desiredVersion: 6,
      reason: 'v6 contract must be replaced',
      confirmation: 'SUPERSEDE BENCHMARK V6',
    })

    expect(result.status).toBe('superseded')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/6/supersede',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('selects active authority with CAS and explicit confirmation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ active_version: 5 }))
      .mockResolvedValueOnce(Response.json({ ...benchmarkRollout, active_version: 5 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await selectActiveBenchmark('operator@omniaura.ai', {
      desiredVersion: 5,
      expectedActiveVersion: 4,
      reason: 'restore completed v5 authority',
      confirmation: 'ACTIVATE BENCHMARK V5',
    })

    expect(result.active_version).toBe(5)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/5/select-active',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          actor: 'operator@omniaura.ai',
          reason: 'restore completed v5 authority',
          confirmation: 'ACTIVATE BENCHMARK V5',
          expected_active_version: 4,
        }),
      }),
    )
  })

  it('rejects malformed rollout telemetry', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({ ...benchmarkRollout, canary_capable_validator_count: -1 }),
      ),
    )

    await expect(fetchBenchmarkRolloutControl()).rejects.toThrow()
  })
})

describe('artifact release administration', () => {
  const control = {
    current: {
      revision: 0,
      parent_revision: 0,
      disclosure: 'public',
      embargo_hours: 24,
      reason: 'Built-in privacy-first default',
      actor: 'platform',
      created_at: null,
    },
    history: [],
  }

  it('reads the effective embargo without mutating it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchArtifactReleaseControl()).resolves.toEqual(control)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/artifact-release-settings',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('writes CAS, reason, confirmation, and operator attribution before refreshing', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ ...control.current, embargo_hours: 12 }))
      .mockResolvedValueOnce(
        Response.json({
          current: { ...control.current, revision: 1, embargo_hours: 12 },
          history: [],
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await updateArtifactReleaseSettings('operator@omniaura.ai', {
      expectedRevision: 0,
      disclosure: 'public',
      embargoHours: 12,
      reason: 'screening capacity is ready for staged release',
      confirmation: 'SET SOURCE EMBARGO 12 HOURS',
    })

    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/artifact-release-settings',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@omniaura.ai' }),
        body: JSON.stringify({
          expected_revision: 0,
          disclosure: 'public',
          embargo_hours: 12,
          reason: 'screening capacity is ready for staged release',
          actor: 'operator@omniaura.ai',
          confirmation: 'SET SOURCE EMBARGO 12 HOURS',
        }),
      }),
    )
  })
})

describe('source release policy administration', () => {
  const control = {
    current: {
      revision: 1,
      parent_revision: 0,
      disclosure: 'public',
      embargo_hours: 48,
      reason: 'Adopt the agreed 48-hour window',
      actor: 'migration',
      created_at: '2026-07-24T12:00:00Z',
    },
    history: [],
  }

  it('sends the whole policy, including the field the platform would reset', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ ...control.current, revision: 2, disclosure: 'never' }),
      )
      .mockResolvedValueOnce(
        Response.json({
          current: { ...control.current, revision: 2, disclosure: 'never' },
          history: [],
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await updateArtifactReleaseSettings('peyton@omniaura.ai', {
      expectedRevision: 1,
      disclosure: 'never',
      embargoHours: 48,
      reason: 'subnet policy: submitted source is not published',
      confirmation: 'SET SOURCE DISCLOSURE NEVER',
    })

    // The exact body matters more on this board than on most: an omitted
    // field is one the platform resets, and here that means the subnet's
    // release visibility changing as a side effect of an unrelated edit.
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/artifact-release-settings',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          expected_revision: 1,
          disclosure: 'never',
          embargo_hours: 48,
          reason: 'subnet policy: submitted source is not published',
          actor: 'peyton@omniaura.ai',
          confirmation: 'SET SOURCE DISCLOSURE NEVER',
        }),
      }),
    )
  })

  it('reads a withheld policy back without losing the retained window', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          current: { ...control.current, disclosure: 'never' },
          history: [control.current],
        }),
      ),
    )

    const read = await fetchArtifactReleaseControl()
    expect(read.current.disclosure).toBe('never')
    // Retained, not dropped: resuming release restores this window.
    expect(read.current.embargo_hours).toBe(48)
    expect(read.history[0].disclosure).toBe('public')
  })
})

describe('submission cooldown administration', () => {
  const control = {
    current: {
      revision: 1,
      parent_revision: 0,
      cooldown_seconds: 3600,
      fee_amount_rao: 40_000_000,
      reason: 'Initialize existing one-hour submission cooldown',
      actor: 'migration',
      created_at: '2026-07-24T12:00:00Z',
    },
    history: [],
  }

  it('reads and updates the platform-owned cooldown contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json({ ...control.current, cooldown_seconds: 1800 }))
      .mockResolvedValueOnce(
        Response.json({
          current: { ...control.current, revision: 2, parent_revision: 1, cooldown_seconds: 1800 },
          history: [],
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchSubmissionSettingsControl()).resolves.toEqual(control)
    await updateSubmissionSettings('operator@omniaura.ai', {
      expectedRevision: 1,
      cooldownSeconds: 1800,
      feeAmountRao: 40_000_000,
      reason: 'reduce cadence for the current capacity window',
      confirmation: 'SET SUBMISSION COOLDOWN 1800 SECONDS FEE 40000000 RAO',
    })

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/submission-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          expected_revision: 1,
          cooldown_seconds: 1800,
          fee_amount_rao: 40_000_000,
          reason: 'reduce cadence for the current capacity window',
          actor: 'operator@omniaura.ai',
          confirmation: 'SET SUBMISSION COOLDOWN 1800 SECONDS FEE 40000000 RAO',
        }),
      }),
    )
  })
})

describe('emission burn administration', () => {
  const control = (burn_share: number, revision = 0) => ({
    current: revision === 0 ? [] : [
      {
        revision,
        parent_revision: revision - 1,
        scope: '*',
        settings: { burn_share },
        reason: 'owner-approved emission burn change',
        actor: 'operator@omniaura.ai',
        created_at: '2026-08-08T12:00:00Z',
        checksum: 'ab'.repeat(32),
      },
    ],
    history: [],
    default: { burn_share: 0 },
    effective: {
      revision,
      scope: '*',
      settings: { burn_share },
      checksum: revision === 0 ? '' : 'ab'.repeat(32),
      source: revision === 0 ? 'default' : 'revision',
      max_age_seconds: 5,
      miner_emission_share: 1 - burn_share,
      min_burn_share: 0,
      max_burn_share: 1,
      live_validator_count: 3,
    },
  })

  it('reads the policy and appends a revision, returning the refreshed read', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const before = control(0)
    const after = control(0.4, 1)
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(before))
      .mockResolvedValueOnce(Response.json(after.current[0]))
      .mockResolvedValueOnce(Response.json(after))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchBurnSettings()).resolves.toEqual(before)
    await expect(
      setBurnSettings(
        {
          expectedRevision: 0,
          settings: { burn_share: 0.4 },
          reason: 'owner-approved emission burn change',
          confirmation: 'APPLY BURN SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).resolves.toEqual(after)

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/burn-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: { burn_share: 0.4 },
          reason: 'owner-approved emission burn change',
          actor: 'operator@omniaura.ai',
          confirmation: 'APPLY BURN SETTINGS',
        }),
      }),
    )
  })

  it('rejects a share outside the unit interval before it reaches the platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setBurnSettings(
        {
          expectedRevision: 0,
          settings: { burn_share: 1.5 },
          reason: 'owner-approved emission burn change',
          confirmation: 'APPLY BURN SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('names the recovery when the platform refuses a stale revision', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(
        Response.json(
          {
            message:
              'burn settings changed; refresh before applying (expected 0, current 2)',
          },
          { status: 409 },
        ),
      ),
    )

    await expect(
      setBurnSettings(
        {
          expectedRevision: 0,
          settings: { burn_share: 0.4 },
          reason: 'owner-approved emission burn change',
          confirmation: 'APPLY BURN SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(/Nothing was applied: re-read get_burn_settings/)
  })
})

describe('continual retest administration', () => {
  // The policy fields the platform grew after this page shipped. Two different
  // fill-ins, and they differ on exactly one field: reading an absent field
  // yields the platform's own default, while a build old enough to omit
  // wave_membership predates #489 and is folding `strict`.
  const readDefaults = {
    wave_membership: 'participants',
    retest_cohort_size: 5,
    retest_eligibility_mode: 'fixed',
    retest_eligibility_z: 1.64,
    retest_cohort_max_size: 25,
  }
  const legacyEquivalent = { ...readDefaults, wave_membership: 'strict' }
  const settings = {
    aggregate_mode: 'fleet_ready',
    idle_retests_enabled: false,
    rollout_standdown: 'capable_validators',
    ...readDefaults,
  }
  const control = {
    current: [],
    history: [],
    default: settings,
    effective: {
      revision: 0,
      scope: '*',
      settings,
      checksum: '',
      source: 'default',
      fleet_protocol_ready: false,
      aggregate_active: false,
      max_age_seconds: 5,
      open_rollout_desired_version: null,
      rollout_standdown_active: false,
      emission_set_size: 5,
      max_retest_cohort_size: 25,
      max_retest_eligibility_z: 3,
      eligible_agent_count: null,
      resolved_cohort_size: null,
    },
  }
  const supportFor = (carried: boolean) => ({
    retest_cohort_size: carried,
    wave_membership: carried,
    retest_eligibility_mode: carried,
    retest_eligibility_z: carried,
    retest_cohort_max_size: carried,
  })
  // The platform carries every field, so reads and writes both keep them.
  const supported = {
    ...control,
    field_support: supportFor(true),
    cohort_sizing_supported: true,
  }

  it('reads and appends the exact platform-owned policy contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const nextSettings = {
      aggregate_mode: 'enabled',
      idle_retests_enabled: true,
      rollout_standdown: 'all',
      ...readDefaults,
      retest_cohort_size: 10,
    }
    const enabled = {
      ...control,
      effective: {
        ...control.effective,
        revision: 1,
        settings: nextSettings,
        aggregate_active: true,
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json({ revision: 1 }))
      .mockResolvedValueOnce(Response.json(enabled))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchContinualRetestSettings()).resolves.toEqual(supported)
    await expect(
      setContinualRetestSettings(
        {
          expectedRevision: 0,
          settings: nextSettings,
          reason: 'activate completed waves and use spare capacity',
          confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).resolves.toEqual({
      ...enabled,
      field_support: supportFor(true),
      cohort_sizing_supported: true,
    })

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      'https://platform-api.heyditto.ai/api/v1/admin/continual-retest-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: nextSettings,
          reason: 'activate completed waves and use spare capacity',
          actor: 'operator@omniaura.ai',
          confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
        }),
      }),
    )
  })

  it('refuses a write that omits the cohort size', async () => {
    // A revision stores the whole policy. Letting this through would write the
    // default 5 over a widened cohort as a side effect of flipping a switch.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setContinualRetestSettings(
        {
          expectedRevision: 0,
          settings: {
            aggregate_mode: 'enabled',
            idle_retests_enabled: true,
            rollout_standdown: 'all',
          },
          reason: 'flip the aggregate switch and nothing else',
          confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  const legacySettings = { aggregate_mode: 'fleet_ready', idle_retests_enabled: false }
  const legacyPayload = {
    current: [],
    history: [],
    default: legacySettings,
    effective: {
      revision: 0,
      scope: '*',
      settings: legacySettings,
      checksum: '',
      source: 'default',
      fleet_protocol_ready: false,
      aggregate_active: false,
      max_age_seconds: 5,
    },
  }

  it('defaults the stand-down policy when the platform predates it', async () => {
    // Backroom can deploy ahead of the platform. Reading must degrade to the
    // safe default rather than throwing and blanking the operator page.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json(legacyPayload)))

    await expect(fetchContinualRetestSettings()).resolves.toEqual({
      ...control,
      field_support: supportFor(false),
      cohort_sizing_supported: false,
    })
  })

  it('writes the rest of the policy to a platform that has no cohort size', async () => {
    // The platform request model forbids unknown fields, so sending one it does
    // not carry 422s the whole revision — the aggregate mode and the idle
    // switch included. Those it understands, and they must still land.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(legacyPayload))
      .mockResolvedValueOnce(Response.json({ revision: 1 }))
      .mockResolvedValueOnce(Response.json(legacyPayload))
    vi.stubGlobal('fetch', fetchMock)

    await setContinualRetestSettings(
      {
        expectedRevision: 0,
        settings: {
          aggregate_mode: 'enabled',
          idle_retests_enabled: true,
          rollout_standdown: 'all',
          ...legacyEquivalent,
        },
        reason: 'fold completed waves on the current platform build',
        confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
      },
      'operator@omniaura.ai',
    )

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/continual-retest-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: {
            aggregate_mode: 'enabled',
            idle_retests_enabled: true,
            rollout_standdown: 'all',
          },
          reason: 'fold completed waves on the current platform build',
          actor: 'operator@omniaura.ai',
          confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
        }),
      }),
    )
  })

  it('refuses to widen the cohort on a platform that cannot honour it', async () => {
    // Dropping the field here instead would answer "top 25" with five and call
    // it success, which is the silent collapse the write schema exists to stop.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(legacyPayload))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setContinualRetestSettings(
        {
          expectedRevision: 0,
          settings: {
            aggregate_mode: 'fleet_ready',
            idle_retests_enabled: false,
            rollout_standdown: 'capable_validators',
            ...legacyEquivalent,
            retest_cohort_size: 25,
          },
          reason: 'deeper list of good agents. top 25',
          confirmation: 'APPLY CONTINUAL RETEST SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(/does not accept a retest cohort size/)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('queue policy administration', () => {
  const settings = {
    rescore_cohort_size: 10,
    priority_cohort_size: 5,
    lane_cycle_size: 4,
    fresh_submission_slots: [0, 1, 3],
    owner_concurrent_submission_limit: 2,
    similarity_budget: {
      enabled: true,
      concurrent_submission_limit: 1,
      jaccard_threshold: 0.9,
      containment_threshold: 0.95,
    },
    deferred_source_review: {
      mode: 'off',
      min_cohort_size: 8,
      composite_mad_multiplier: 6,
      axis_mad_multiplier: 6,
      min_composite_delta: 0.1,
      min_axis_delta: 0.15,
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
  const control = {
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
      rollout_is_open: false,
      min_cohort_size: 5,
      max_cohort_size: 25,
    },
  }

  it('reads and appends the exact platform-owned policy contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const nextSettings = {
      ...settings,
      rescore_cohort_size: 20,
      priority_cohort_size: 8,
      prev_gen_carryover: { ...settings.prev_gen_carryover, enabled: true, min_score_count: 0 },
    }
    const applied = {
      ...control,
      effective: { ...control.effective, revision: 1, settings: nextSettings, source: 'revision' },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json({ revision: 1 }))
      .mockResolvedValueOnce(Response.json(applied))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchQueuePolicySettings()).resolves.toEqual(control)
    await expect(
      setQueuePolicySettings(
        {
          expectedRevision: 0,
          settings: nextSettings,
          reason: 'widen the rescore cohort and admit stranded prior-generation work',
          confirmation: 'APPLY QUEUE POLICY SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).resolves.toEqual(applied)

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/queue-policy-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: nextSettings,
          reason: 'widen the rescore cohort and admit stranded prior-generation work',
          actor: 'operator@omniaura.ai',
          confirmation: 'APPLY QUEUE POLICY SETTINGS',
        }),
      }),
    )
  })

  it('surfaces the platform refusal of a live lane change during an open rollout', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const detail =
      'lane_cycle_size cannot change while benchmark rollout 8 is open: the lane counter is completed jobs since rollout start mod N'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ detail }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setQueuePolicySettings(
        {
          expectedRevision: 3,
          settings: { ...settings, lane_cycle_size: 6, fresh_submission_slots: [0, 1, 2, 4] },
          reason: 'lengthen the lane cycle for the onboarding wave',
          confirmation: 'APPLY QUEUE POLICY SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(detail)
    // Nothing is re-read after a refusal, so the operator cannot mistake a
    // fresh GET for a successful apply.
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('names the recovery for a stale expected revision', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(
        Response.json(
          { detail: 'queue policy settings changed; refresh before applying (expected 0, current 2)' },
          { status: 409 },
        ),
      ),
    )

    await expect(
      setQueuePolicySettings(
        {
          expectedRevision: 0,
          settings,
          reason: 'restore the shipped lane cycle after the wave',
          confirmation: 'APPLY QUEUE POLICY SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(/expected 0, current 2.*get_queue_policy_settings/s)
  })

  it('rejects an unsatisfiable lane policy before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setQueuePolicySettings(
        {
          expectedRevision: 0,
          settings: { ...settings, fresh_submission_slots: [] },
          reason: 'stop serving fresh submissions for the rollout window',
          confirmation: 'APPLY QUEUE POLICY SETTINGS',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('copy review admin service', () => {
  const activeListMetadata = {
    generation: 'active' as const,
    active_bench_version: 8,
  }

  it('joins bounded durable rows to the separately fetched current comparison', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    process.env.DITTO_PLATFORM_API_BASE_URL = 'https://platform.example.test'
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ items: [review], count: 1, limit: 200, offset: 0, ...activeListMetadata }))
      .mockResolvedValueOnce(Response.json(comparison))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCopyReviews()

    expect(fetchMock.mock.calls[0]?.[0]).toContain('status=pending&generation=active&limit=200&offset=0')
    expect(fetchMock.mock.calls[1]?.[0]).toContain(`${review.agent_id}/current-comparison`)
    expect(result.bulk_eligible_count).toBe(1)
    expect(result.items[0]?.current_comparison).toMatchObject({
      availability: 'available',
      bulk_eligible: true,
      current_decision: 'clear',
    })
  })

  it('fails a comparison closed without losing the review row', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ items: [review], count: 1, limit: 200, offset: 0, ...activeListMetadata }))
      .mockResolvedValueOnce(Response.json({ detail: 'current comparison unavailable' }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCopyReviews()

    expect(result.bulk_eligible_count).toBe(0)
    expect(result.items[0]?.current_comparison).toEqual({
      availability: 'unavailable',
      bulk_eligible: false,
      reason: 'current comparison unavailable',
    })
  })

  it('consumes embedded comparisons in one request when the platform provides them', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const embedded = { ...review, current_comparison: comparison }
    const unavailableRow = {
      ...review,
      review_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      agent_id: '33333333-3333-4333-8333-333333333333',
      current_comparison: {
        availability: 'unavailable',
        bulk_eligible: false,
        reason: 'current comparison unavailable',
      },
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ items: [embedded, unavailableRow], count: 2, limit: 200, offset: 0, ...activeListMetadata }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCopyReviews()

    // One list request, zero per-row comparison fan-out.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toContain('include=current_comparison')
    expect(result.bulk_eligible_count).toBe(1)
    expect(result.items[1]?.current_comparison.availability).toBe('unavailable')
  })

  it('serves repeat views from the TTL cache and invalidates on resolve', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const resolved = {
      ...review,
      status: 'resolved',
      resolved_at: '2026-07-16T12:00:00Z',
      resolved_by: 'operator@example.com',
      resolution: 'clear',
      resolution_reason: 'cleared',
    }
    const fetchMock = vi.fn((url: string | URL, init?: RequestInit) => {
      const target = String(url)
      if (init?.method === 'POST') {
        return Promise.resolve(
          Response.json({ review: resolved, agent_status: 'scored', idempotent: false }),
        )
      }
      if (target.includes('current-comparison')) {
        return Promise.resolve(Response.json(comparison))
      }
      return Promise.resolve(
        Response.json({ items: [review], count: 1, limit: 200, offset: 0, ...activeListMetadata }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchCopyReviews()
    await fetchCopyReviews()
    // One list fetch + one comparison: the second view is a cache hit.
    expect(fetchMock).toHaveBeenCalledTimes(2)

    await resolveCopyReview(
      { agentId: review.agent_id, resolution: 'clear', reason: 'cleared' },
      'operator@example.com',
    )
    await fetchCopyReviews()
    // Resolve (1 call) invalidated the cache, so the next view rebuilds (2 calls).
    expect(fetchMock).toHaveBeenCalledTimes(5)
  })

  it('carries the matched submission identity when the platform provides it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const named = {
      ...review,
      original: {
        ...review.original,
        duplicate_of_name: 'jackie',
        duplicate_of_version: 3,
        duplicate_of_hotkey: '5G9QoBvJLt',
        duplicate_of_submitted_at: '2026-07-15T04:52:56Z',
      },
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ items: [named], count: 1, limit: 200, offset: 0, ...activeListMetadata }))
      .mockResolvedValueOnce(Response.json(comparison))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCopyReviews()

    expect(result.items[0]?.original.duplicate_of_name).toBe('jackie')
    expect(result.items[0]?.original.duplicate_of_version).toBe(3)
  })

  it('sends canonical clear and parses the durable idempotent response', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const resolved = {
      ...review,
      status: 'resolved',
      resolved_at: '2026-07-16T12:00:00Z',
      resolved_by: 'operator@example.com',
      resolution: 'clear',
      resolution_reason: 'Current calibrated evidence is clear',
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ review: resolved, agent_status: 'scored', idempotent: false }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await resolveCopyReview(
      { agentId: review.agent_id, resolution: 'clear', reason: 'Current calibrated evidence is clear' },
      'operator@example.com',
    )

    expect(result.review.resolution).toBe('clear')
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ resolution: 'clear', reason: 'Current calibrated evidence is clear' }),
    })
  })

  it('opens a guarded benchmark-overfit hold with the operator identity', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const held = {
      ...review,
      original: {
        ...review.original,
        review_kind: 'benchmark_overfit',
        duplicate_of: null,
        reason: 'Deterministic benchmark-family routing',
      },
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        review: held,
        agent_status: 'ath_pending_review',
        idempotent: false,
        reopened: true,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await openAthReview(
      {
        agentId: review.agent_id,
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 3,
        reason: 'Deterministic benchmark-family routing',
      },
      'operator@example.com',
    )

    expect(result.agent_status).toBe('ath_pending_review')
    expect(result.reopened).toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/${review.agent_id}/open`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          expected_sha256: 'ab'.repeat(32),
          expected_score_count: 3,
          reason: 'Deterministic benchmark-family routing',
        }),
      }),
    )
  })

  it('fetches the durable audit context for one ATH hold', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        review: {
          ...review,
          original: {
            ...review.original,
            review_kind: 'benchmark_overfit',
            duplicate_of: null,
            reason: 'Deterministic benchmark-family routing',
          },
        },
        agent_status: 'ath_pending_review',
        held_artifact_sha256: 'ab'.repeat(32),
        held_score_count: 3,
        previous_status: 'scored',
        opened_by: 'operator@example.com',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchAthReview({ agentId: review.agent_id })

    expect(result.review.original.reason).toBe('Deterministic benchmark-family routing')
    expect(result.opened_by).toBe('operator@example.com')
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/${review.agent_id}/audit`,
      expect.any(Object),
    )
  })

  it('inspects and retries one exact validation snapshot with operator attribution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const requestId = '11111111-1111-4111-8111-111111111111'
    const snapshot = 'ab'.repeat(32)
    const recovery = {
      recovery_id: requestId,
      agent_id: agentId,
      actor: 'operator@example.com',
      reason: 'Verified validator OOM',
      score_count: 0,
      bench_version: 2,
      expected_snapshot: snapshot,
      granted_validator_hotkeys: ['5Validator'],
      created_at: '2026-07-18T18:00:00Z',
    }
    const detail = {
      agent_id: agentId,
      miner_hotkey: '5Miner',
      agent_name: 'valid-agent',
      agent_version: 1,
      agent_status: 'evaluating',
      score_count: 0,
      quorum: 3,
      snapshot,
      automatic_retry_available: false,
      recovery_allowed: true,
      blocking_reason: null,
      withdrawal_allowed: true,
      withdrawal_blocking_reason: null,
      withdrawal: null,
      tickets: [],
      recoveries: [],
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json({ recovery, idempotent: false }))
    vi.stubGlobal('fetch', fetchMock)

    expect((await fetchValidationRetry({ agentId })).snapshot).toBe(snapshot)
    await retryValidation(
      { agentId, expectedSnapshot: snapshot, reason: 'Verified validator OOM' },
      'operator@example.com',
    )

    // The caller never supplies an idempotency key. It is derived from exactly
    // the fields the platform compares on replay, so re-issuing the same action
    // against the same state is still one request.
    const derivedRetryId = await deriveRequestId('validation-retry', [
      agentId,
      'operator@example.com',
      'Verified validator OOM',
      snapshot,
    ])
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/retry`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          request_id: derivedRetryId,
          expected_snapshot: snapshot,
          reason: 'Verified validator OOM',
        }),
      }),
    )

    const withdrawal = {
      withdrawal_id: requestId,
      agent_id: agentId,
      bench_version: 2,
      actor: 'operator@example.com',
      reason: 'Three validator attempts exhausted their budget',
      expected_snapshot: snapshot,
      score_count: 0,
      created_at: '2026-07-18T18:00:00Z',
    }
    fetchMock.mockResolvedValueOnce(Response.json({ withdrawal, idempotent: false }))
    await withdrawValidation(
      {
        agentId,
        expectedSnapshot: snapshot,
        reason: withdrawal.reason,
        confirmation: 'REMOVE FROM VALIDATOR QUEUE',
      },
      'operator@example.com',
    )
    const derivedWithdrawId = await deriveRequestId('validation-withdraw', [
      agentId,
      'operator@example.com',
      withdrawal.reason,
      snapshot,
    ])
    // A withdrawal is a different action from a retry of the same agent by the
    // same operator, so the namespace keeps their keys apart.
    expect(derivedWithdrawId).not.toBe(derivedRetryId)
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/withdraw`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          request_id: derivedWithdrawId,
          expected_snapshot: snapshot,
          reason: withdrawal.reason,
          confirmation: 'REMOVE FROM VALIDATOR QUEUE',
        }),
      }),
    )
  })

  it('evicts live validator leases under its own confirmation and idempotency key', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'cd'.repeat(32)
    const reason = 'Hung through three full leases with zero scores reported'
    const eviction = {
      eviction_id: '33333333-3333-4333-8333-333333333333',
      agent_id: agentId,
      bench_version: 7,
      actor: 'operator@example.com',
      reason,
      expected_snapshot: snapshot,
      score_count: 0,
      evicted_validator_hotkeys: ['5ValidatorA', '5ValidatorB'],
      created_at: '2026-07-27T18:00:00Z',
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        eviction,
        evicted_leases: [
          {
            validator_hotkey: '5ValidatorA',
            slot_id: 'slot-1',
            bench_version: 7,
            issued_at: '2026-07-27T17:00:00Z',
            original_deadline: '2026-07-27T18:30:00Z',
            attempt_count: 9,
            audit_id: '44444444-4444-4444-8444-444444444444',
          },
          {
            validator_hotkey: '5ValidatorB',
            slot_id: 'slot-2',
            bench_version: 7,
            issued_at: '2026-07-27T17:05:00Z',
            original_deadline: '2026-07-27T18:35:00Z',
            attempt_count: 9,
            audit_id: '55555555-5555-4555-8555-555555555555',
          },
        ],
        freed_slots: 2,
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await evictValidation(
      {
        agentId,
        expectedSnapshot: snapshot,
        reason,
        confirmation: 'EVICT LIVE VALIDATOR LEASES',
      },
      'operator@example.com',
    )

    expect(result.freed_slots).toBe(2)
    expect(result.eviction.evicted_validator_hotkeys).toEqual([
      '5ValidatorA',
      '5ValidatorB',
    ])

    const derivedEvictId = await deriveRequestId('validation-evict', [
      agentId,
      'operator@example.com',
      reason,
      snapshot,
    ])
    // The platform stores both routes' request ids as the primary key of one
    // shared table, so an eviction must not derive the withdrawal's key: a
    // replay has to mean the same eviction, never a different action.
    expect(derivedEvictId).not.toBe(
      await deriveRequestId('validation-withdraw', [
        agentId,
        'operator@example.com',
        reason,
        snapshot,
      ]),
    )
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/evict`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          request_id: derivedEvictId,
          expected_snapshot: snapshot,
          reason,
          confirmation: 'EVICT LIVE VALIDATOR LEASES',
        }),
      }),
    )

    // The removal phrase never reaches the network from here.
    fetchMock.mockClear()
    await expect(
      evictValidation(
        {
          agentId,
          expectedSnapshot: snapshot,
          reason,
          confirmation: 'REMOVE FROM VALIDATOR QUEUE',
        },
        'operator@example.com',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reinstates an evicted submission under a third phrase and a third namespace', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'ef'.repeat(32)
    const reason =
      'source review found no hang primitives; the eviction was a capacity call'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        reinstatement: {
          reinstatement_id: '66666666-6666-4666-8666-666666666666',
          withdrawal_id: '33333333-3333-4333-8333-333333333333',
          agent_id: agentId,
          bench_version: 7,
          actor: 'operator@example.com',
          reason,
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
        },
        eviction: {
          eviction_id: '33333333-3333-4333-8333-333333333333',
          agent_id: agentId,
          bench_version: 7,
          actor: 'operator@example.com',
          reason: 'Hung through three full leases with zero scores reported',
          expected_snapshot: snapshot,
          score_count: 0,
          evicted_validator_hotkeys: ['5ValidatorA'],
          created_at: '2026-07-27T18:00:00Z',
          reinstated_at: '2026-07-27T19:00:00Z',
        },
        restored_bench_version: 7,
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await reinstateValidation(
      {
        agentId,
        expectedSnapshot: snapshot,
        reason,
        confirmation: 'REINSTATE TO VALIDATOR QUEUE',
      },
      'operator@example.com',
    )

    expect(result.restored_bench_version).toBe(7)
    // The eviction it reversed comes back resolved, not deleted.
    expect(result.eviction.reinstated_at).toBe('2026-07-27T19:00:00Z')
    // The budget the reversal did NOT touch, recorded on the audit row: the
    // per-agent no-fault bound is still 12 and four of them are still spent.
    expect(result.reinstatement.retry_budget_snapshot).toMatchObject({
      agent_infra_retry_grants: 4,
      max_agent_infra_retry_grants: 12,
      operator_recoveries: 1,
    })

    const derivedReinstateId = await deriveRequestId('validation-reinstate', [
      agentId,
      'operator@example.com',
      reason,
      snapshot,
    ])
    // Three actions, three namespaces. A reinstatement deriving the eviction's
    // key would collide with the very action it reverses, so re-sending a
    // reversal could be answered as a replay of the eviction that caused it.
    for (const namespace of ['validation-evict', 'validation-withdraw']) {
      expect(derivedReinstateId).not.toBe(
        await deriveRequestId(namespace, [
          agentId,
          'operator@example.com',
          reason,
          snapshot,
        ]),
      )
    }
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/reinstate`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          request_id: derivedReinstateId,
          expected_snapshot: snapshot,
          reason,
          confirmation: 'REINSTATE TO VALIDATOR QUEUE',
        }),
      }),
    )

    // Neither other phrase reaches the network from here.
    for (const confirmation of [
      'EVICT LIVE VALIDATOR LEASES',
      'REMOVE FROM VALIDATOR QUEUE',
    ]) {
      fetchMock.mockClear()
      await expect(
        reinstateValidation(
          { agentId, expectedSnapshot: snapshot, reason, confirmation },
          'operator@example.com',
        ),
      ).rejects.toThrow()
      expect(fetchMock).not.toHaveBeenCalled()
    }
  })

  it('inspects and replaces one validator score with exact concurrency guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const validatorHotkey = '5Validator'
    const requestId = '11111111-1111-4111-8111-111111111111'
    const snapshot = 'ab'.repeat(32)
    const detail = {
      agent_id: agentId,
      validator_hotkey: validatorHotkey,
      agent_status: 'scored',
      bench_version: 4,
      score_count: 3,
      quorum: 3,
      snapshot,
      run_id: 'run-123',
      composite: 0.488,
      ticket_status: 'scored',
      ticket_deadline: '2026-07-20T04:00:00Z',
      replacement_pending: false,
      replacement_request_id: null,
      replacement_reason: null,
      replacement_actor: null,
      replacement_allowed: true,
      blocking_reason: null,
    }
    const replacement = {
      request_id: requestId,
      agent_id: agentId,
      validator_hotkey: validatorHotkey,
      original_run_id: 'run-123',
      bench_version: 4,
      replacement_deadline: '2026-07-20T05:30:00Z',
      preserved_score_count: 3,
      idempotent: false,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json(replacement))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchValidatorScoreReplacement({ agentId, validatorHotkey })).resolves.toEqual(
      detail,
    )
    await expect(
      replaceValidatorScore(
        {
          agentId,
          validatorHotkey,
          expectedSnapshot: snapshot,
          expectedRunId: 'run-123',
          reason: 'Verified validator relay failure corrupted this run',
        },
        'operator@example.com',
      ),
    ).resolves.toEqual(replacement)

    const derivedReplacementId = await deriveRequestId('score-replacement', [
      agentId,
      validatorHotkey,
      'operator@example.com',
      'Verified validator relay failure corrupted this run',
      snapshot,
      'run-123',
    ])
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/validators/${validatorHotkey}/replace-score`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          request_id: derivedReplacementId,
          expected_snapshot: snapshot,
          expected_run_id: 'run-123',
          reason: 'Verified validator relay failure corrupted this run',
        }),
      }),
    )
  })

  it('lists score outliers and releases a pending re-test ticket', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const validatorHotkey = '5Validator'
    const snapshot = 'ab'.repeat(32)
    const deadline = '2026-07-20T05:30:00Z'
    const outliers = {
      items: [{
        agent_id: agentId,
        agent_name: 'outlying-agent',
        miner_hotkey: '5Miner',
        agent_status: 'scored',
        bench_version: 4,
        snapshot,
        median_composite: 0.82,
        direction: 'low',
        outlier: { validator_hotkey: validatorHotkey, run_id: 'run-low', composite: 0.1 },
        peers: [
          { validator_hotkey: '5PeerA', run_id: 'run-a', composite: 0.82 },
          { validator_hotkey: '5PeerB', run_id: 'run-b', composite: 0.84 },
        ],
        deviation: 0.72,
        peer_spread: 0.02,
        ticket_status: 'issued',
        replacement_pending: true,
        replacement_queued: false,
        queue_position: null,
        replacement_deadline: deadline,
        replacement_allowed: false,
        blocking_reason: 'replacement score is already pending',
        queue_allowed: false,
        queue_blocking_reason: 'replacement score is already queued or pending',
      }],
      count: 1,
      limit: 50,
      offset: 0,
      bench_version: 7,
    }
    const released = {
      request_id: '11111111-1111-4111-8111-111111111111',
      agent_id: agentId,
      validator_hotkey: validatorHotkey,
      status: 'scored',
      preserved_run_id: 'run-low',
      idempotent: false,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(outliers))
      .mockResolvedValueOnce(Response.json(released))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchScoreOutliers({ limit: 50, offset: 0 })).resolves.toEqual(outliers)
    await expect(releaseValidatorScoreRetest({
      agentId,
      validatorHotkey,
      expectedSnapshot: snapshot,
      expectedDeadline: deadline,
      reason: 'Validator evidence cleared and the ticket can be released',
    }, 'operator@example.com')).resolves.toEqual(released)
    const derivedReleaseId = await deriveRequestId('score-retest-release', [
      agentId,
      validatorHotkey,
      'operator@example.com',
      'Validator evidence cleared and the ticket can be released',
      snapshot,
      deadline,
    ])
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/validators/${validatorHotkey}/release-ticket`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          request_id: derivedReleaseId,
          expected_snapshot: snapshot,
          expected_deadline: deadline,
          reason: 'Validator evidence cleared and the ticket can be released',
        }),
      }),
    )
  })

  it('reads a score-outlier list from a platform that does not report the era', async () => {
    // Backroom and the platform deploy separately, so Backroom can be live
    // against a build that predates the era-scoped scan. That list still has
    // to render — it just cannot be labelled with an era it never reported.
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(Response.json({ items: [], count: 0, limit: 50, offset: 0 })),
    )

    await expect(fetchScoreOutliers({ limit: 50, offset: 0 })).resolves.toMatchObject({
      count: 0,
      bench_version: null,
    })
  })

  it('queues exact score outliers for one validator in one audited request', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const validatorHotkey = '5Validator'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const requestId = '11111111-1111-4111-8111-111111111111'
    const snapshot = 'ab'.repeat(32)
    const queued = {
      validator_hotkey: validatorHotkey,
      activated: 0,
      queued: 1,
      idempotent: 0,
      skipped: 0,
      results: [{
        agent_id: agentId,
        request_id: requestId,
        status: 'queued',
        detail: null,
        queue_position: 1,
      }],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(queued))
    vi.stubGlobal('fetch', fetchMock)

    await expect(queueValidatorScoreRetests({
      validatorHotkey,
      reason: 'Shared validator provider failure across these outliers',
      items: [{
        agentId,
        expectedSnapshot: snapshot,
        expectedRunId: 'run-low',
      }],
    }, 'operator@example.com')).resolves.toEqual(queued)
    // Derived per item exactly as the single replacement derives it, so the
    // same outlier queued twice is one request rather than two.
    const derivedQueueId = await deriveRequestId('score-replacement', [
      agentId,
      validatorHotkey,
      'operator@example.com',
      'Shared validator provider failure across these outliers',
      snapshot,
      'run-low',
    ])
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/validators/${validatorHotkey}/queue-score-retests`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          reason: 'Shared validator provider failure across these outliers',
          items: [{
            agent_id: agentId,
            request_id: derivedQueueId,
            expected_snapshot: snapshot,
            expected_run_id: 'run-low',
          }],
        }),
      }),
    )
  })

  it('previews and queues exact v9 contract mismatches with a distinct request id', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const validatorHotkey = '5Validator'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const snapshot = 'ab'.repeat(32)
    const preview = {
      items: [{
        agent_id: agentId,
        agent_name: 'shadow-agent',
        miner_hotkey: '5Miner',
        agent_status: 'evaluating',
        validator_hotkey: validatorHotkey,
        run_id: 'run-shadow',
        composite: 0.7,
        snapshot,
        observed_revision: 'v9-base-shadow-calibration-v1',
        observed_manifest_sha256: 'cd'.repeat(32),
        observed_rollout_mode: 'shadow',
        semantic_gate_factor_bps: 0,
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
      required_manifest_sha256: 'ef'.repeat(32),
      required_rollout_mode: 'enforce',
    }
    const queued = {
      validator_hotkey: validatorHotkey,
      activated: 0,
      queued: 1,
      idempotent: 0,
      skipped: 0,
      results: [{
        agent_id: agentId,
        request_id: '11111111-1111-4111-8111-111111111111',
        status: 'queued',
        detail: null,
        queue_position: 1,
      }],
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(preview))
      .mockResolvedValueOnce(Response.json(queued))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchV9ContractRetests({ limit: 100, offset: 0 })).resolves.toEqual(preview)
    await expect(queueValidatorScoreRetests({
      validatorHotkey,
      basis: 'v9_contract_mismatch',
      confirmation: 'QUEUE V9 CONTRACT RETESTS',
      reason: 'Replace obsolete signed v9 score evidence',
      items: [{ agentId, expectedSnapshot: snapshot, expectedRunId: 'run-shadow' }],
    }, 'operator@example.com')).resolves.toEqual(queued)
    const derivedQueueId = await deriveRequestId('score-replacement', [
      agentId,
      validatorHotkey,
      'v9_contract_mismatch',
      'operator@example.com',
      'Replace obsolete signed v9 score evidence',
      snapshot,
      'run-shadow',
    ])
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/v9-contract-retests?limit=100&offset=0',
      expect.any(Object),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/validators/${validatorHotkey}/queue-score-retests`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          reason: 'Replace obsolete signed v9 score evidence',
          basis: 'v9_contract_mismatch',
          confirmation: 'QUEUE V9 CONTRACT RETESTS',
          items: [{
            agent_id: agentId,
            request_id: derivedQueueId,
            expected_snapshot: snapshot,
            expected_run_id: 'run-shadow',
          }],
        }),
      }),
    )
  })

  it('inspects and rebuilds a stale benchmark contract with exact guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const detail = {
      agent_id: agentId,
      agent_name: 'stale-v3-agent',
      agent_status: 'evaluating',
      artifact_sha256: 'ab'.repeat(32),
      bench_version: 1,
      dataset_sha256: 'cd'.repeat(32),
      score_count: 0,
      screening_attempt_active: false,
      refresh_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json({
        agent_id: agentId,
        agent_status: 'screening_failed',
        bench_version: 3,
        expired_ticket_count: 1,
      }))
    vi.stubGlobal('fetch', fetchMock)

    expect((await fetchBenchmarkContractRefresh({ agentId })).refresh_allowed).toBe(true)
    await refreshBenchmarkContract(
      {
        agentId,
        expectedSha256: detail.artifact_sha256,
        expectedBenchVersion: 3,
        expectedDatasetSha256: detail.dataset_sha256,
        expectedScoreCount: 0,
        reason: 'Confirmed generator and validator dataset drift',
      },
      'operator@example.com',
    )

    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/refresh-benchmark-contract`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          reason: 'Confirmed generator and validator dataset drift',
          expected_sha256: detail.artifact_sha256,
          expected_bench_version: 3,
          expected_dataset_sha256: detail.dataset_sha256,
          expected_score_count: 0,
        }),
      }),
    )
  })

  it('inspects and rebuilds only a stale screened image with exact guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const detail = {
      agent_id: agentId,
      agent_name: 'stale-image-agent',
      agent_status: 'evaluating',
      artifact_sha256: 'ab'.repeat(32),
      bench_version: 8,
      score_count: 0,
      screened_image_sha256: 'cd'.repeat(32),
      screened_image_upload_id: '22345678-1234-4234-8234-123456789012',
      screening_attempt_active: false,
      validator_ticket_active: true,
      rebuild_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json({
        agent_id: agentId,
        agent_status: 'evaluating',
        bench_version: 8,
        expired_ticket_count: 3,
      }))
    vi.stubGlobal('fetch', fetchMock)

    expect((await fetchScreenedImageRebuild({ agentId })).rebuild_allowed).toBe(true)
    await rebuildScreenedImage(
      {
        agentId,
        expectedSha256: detail.artifact_sha256,
        expectedBenchVersion: 8,
        expectedScoreCount: 0,
        expectedImageSha256: detail.screened_image_sha256,
        expectedImageUploadId: detail.screened_image_upload_id,
        reason: 'Healthy validators reject this legacy image archive',
      },
      'operator@example.com',
    )

    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/rebuild-screened-image`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'operator@example.com' }),
        body: JSON.stringify({
          reason: 'Healthy validators reject this legacy image archive',
          expected_sha256: detail.artifact_sha256,
          expected_bench_version: 8,
          expected_score_count: 0,
          expected_image_sha256: detail.screened_image_sha256,
          expected_image_upload_id: detail.screened_image_upload_id,
        }),
      }),
    )
  })

  it('inspects and migrates a zero-score v2 contract with exact guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const detail = {
      agent_id: agentId,
      agent_name: 'legacy-v2-agent',
      agent_status: 'evaluating',
      artifact_sha256: 'ab'.repeat(32),
      source_bench_version: 2,
      target_bench_version: 3,
      source_dataset_sha256: 'cd'.repeat(32),
      target_dataset_sha256: null,
      source_score_count: 0,
      target_score_count: 0,
      screening_attempt_active: false,
      validator_run_active: false,
      migration_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json({
        agent_id: agentId,
        agent_status: 'screening_failed',
        source_bench_version: 2,
        target_bench_version: 3,
        target_dataset_sha256: 'ef'.repeat(32),
        expired_ticket_count: 2,
      }))
    vi.stubGlobal('fetch', fetchMock)

    expect(
      (await fetchBenchmarkContractMigration({ agentId })).migration_allowed,
    ).toBe(true)
    await migrateBenchmarkContract(
      {
        agentId,
        expectedSha256: detail.artifact_sha256,
        expectedSourceDatasetSha256: detail.source_dataset_sha256,
        reason: 'Legacy zero-score artifact requires the v3 contract',
      },
      'operator@example.com',
    )

    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/migrate-benchmark-contract`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'X-Admin-Actor': 'operator@example.com',
        }),
        body: JSON.stringify({
          reason: 'Legacy zero-score artifact requires the v3 contract',
          expected_sha256: detail.artifact_sha256,
          expected_source_bench_version: 2,
          expected_target_bench_version: 3,
          expected_source_dataset_sha256: detail.source_dataset_sha256,
          expected_source_score_count: 0,
          expected_target_score_count: 0,
        }),
      }),
    )
  })
})

describe('production score reads', () => {
  const topAgentId = '11111111-1111-4111-8111-111111111111'
  const provisionalAgentId = '22222222-2222-4222-8222-222222222222'
  const supersededAgentId = '33333333-3333-4333-8333-333333333333'

  const breakdown = {
    formula:
      '(0.5 * tool_mean + 0.5 * memory_mean) * benchmark_quality_multiplier * token_efficiency_multiplier',
    tool_weight: 0.5,
    memory_weight: 0.5,
    base_accuracy: 0.957,
    benchmark_quality_multiplier: 1,
    pre_token_composite: 0.957,
    token_efficiency_multiplier: 1,
    token_penalty: 0,
    maximum_token_penalty: 0.1,
    final_composite: 0.957,
  }

  const topEntry = {
    rank: 1,
    finalized: true,
    score_count: 3,
    score_quorum: 3,
    agent_id: topAgentId,
    agent_name: 'apex-agent',
    agent_version: 4,
    miner_hotkey: '5TopMiner',
    miner_uid: 12,
    registered: true,
    emission_eligible: true,
    composite: 0.957,
    raw_composite: 0.965,
    composite_stderr: 0.003,
    settled_composite: null,
    rollout_composite: null,
    rollout_score_count: null,
    tool_mean: 0.981,
    memory_mean: 0.933,
    first_seen: '2026-07-01T00:00:00Z',
    median_ms: 2100,
    n: 40,
    eligible: true,
    bench_version: 7,
    dataset_sha256: 'ab'.repeat(32),
    composite_breakdown: breakdown,
    history: [0.91, 0.957],
    case_results: [{ category: 'web_search', kind: 'tool', score: 1 }],
  }

  const provisionalEntry = {
    rank: 2,
    finalized: false,
    score_count: 1,
    score_quorum: 3,
    agent_id: provisionalAgentId,
    agent_name: 'challenger-agent',
    agent_version: 1,
    miner_hotkey: '5NewMiner',
    miner_uid: null,
    registered: null,
    emission_eligible: false,
    composite: 0.948,
    raw_composite: null,
    composite_stderr: null,
    settled_composite: null,
    rollout_composite: null,
    rollout_score_count: null,
    tool_mean: 0.97,
    memory_mean: 0.926,
    first_seen: '2026-07-20T00:00:00Z',
    median_ms: 2450,
    n: 40,
    eligible: true,
    bench_version: 7,
    dataset_sha256: 'cd'.repeat(32),
    composite_breakdown: null,
    history: null,
  }

  const emissions = {
    margin: 0.02,
    dethrone_z: 1.64,
    band_decay_min_bench_version: 5,
    band_decay_start_composite: 0.9,
    band_decay_rate: 12,
    champion_share: 0.6,
    rank_shares: [0.6, 0.2, 0.1, 0.06, 0.04],
    tail_size: 4,
    champion_agent_id: topAgentId,
    champion_miner_hotkey: '5TopMiner',
    raw_leader_agent_id: topAgentId,
    raw_leader_miner_hotkey: '5TopMiner',
    raw_leader_decision: {
      challenger_lead: 0,
      required_lead: 0.011,
      margin_lead: 0.011,
      statistical_lead: 0.005,
      method: 'paired',
      dethrones: false,
    },
    recipients: [
      {
        role: 'champion',
        agent_id: topAgentId,
        miner_hotkey: '5TopMiner',
        raw_rank: 1,
        share_of_miner_pool: 0.6,
        shared_seed_confirmations: 7,
      },
    ],
  }

  const leaderboard = {
    generated_at: '2026-07-23T00:00:00Z',
    count: 2,
    current_bench_version: 7,
    active_bench_version: 7,
    desired_bench_version: 7,
    available_bench_versions: [7, 6, 5, 4, 3, 2],
    selection_mode: 'authoritative',
    entries: [topEntry, provisionalEntry],
    emissions,
  }

  function scoreRow(overrides: Record<string, unknown>) {
    return {
      validator_hotkey: '5ValA',
      composite: 0.957,
      tool_mean: 0.981,
      memory_mean: 0.933,
      raw_composite: 0.965,
      composite_breakdown: null,
      median_ms: 2100,
      n: 40,
      bench_version: 7,
      seed: 424242,
      run_id: 'run-a7',
      ticket_deadline: '2026-07-22T00:00:00Z',
      generated_at: '2026-07-21T00:00:00Z',
      transform_robustness: 0.9,
      audit_case_count: 8,
      transcript_sha256: 'aa'.repeat(32),
      signature: 'f0'.repeat(64),
      ...overrides,
    }
  }

  const agentScores = {
    agent_id: topAgentId,
    miner_hotkey: '5TopMiner',
    status: 'scored',
    quorum: 3,
    score_count: 6,
    median_composite: 0.957,
    dataset_seed: 987654321,
    dataset_sha256: 'cd'.repeat(32),
    dataset_run_size: 'full',
    dataset_seed_block: 123456,
    dataset_seed_block_hash: 'ef'.repeat(32),
    scores: [
      scoreRow({ validator_hotkey: '5ValA', composite: 0.91, bench_version: 6, seed: 111, run_id: 'run-a6', generated_at: '2026-07-10T00:00:00Z' }),
      scoreRow({ validator_hotkey: '5ValB', composite: 0.92, bench_version: 6, seed: 111, run_id: 'run-b6', generated_at: '2026-07-10T01:00:00Z' }),
      scoreRow({ validator_hotkey: '5ValC', composite: 0.9, bench_version: 6, seed: 111, run_id: 'run-c6', generated_at: '2026-07-10T02:00:00Z' }),
      scoreRow({ validator_hotkey: '5ValA', composite: 0.957, seed: 424242, run_id: 'run-a7' }),
      scoreRow({ validator_hotkey: '5ValB', composite: 0.955, seed: 424242, run_id: 'run-b7', generated_at: '2026-07-21T01:00:00Z' }),
      scoreRow({ validator_hotkey: '5ValC', composite: 0.96, seed: 424242, run_id: 'run-c7', generated_at: '2026-07-21T02:00:00Z' }),
    ],
    generated_at: '2026-07-23T00:00:00Z',
  }

  it('reads the authoritative leaderboard without the admin token', async () => {
    delete process.env.DITTO_ADMIN_API_TOKEN
    const fetchMock = vi.fn().mockResolvedValue(Response.json(leaderboard))
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchScoreLeaderboard({ status: 'finalized', limit: 1, offset: 0 })

    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/public/leaderboard',
      expect.objectContaining({ method: 'GET' }),
    )
    const headers = (fetchMock.mock.calls[0]?.[1] as { headers: Record<string, string> }).headers
    expect(headers).not.toHaveProperty('Authorization')
    expect(page.count).toBe(1)
    expect(page.entries).toHaveLength(1)
    expect(page.entries[0]).toMatchObject({
      rank: 1,
      agent_id: topAgentId,
      composite: 0.957,
      emission_eligible: true,
    })
    // Heavy per-case payloads are stripped for compact MCP responses.
    expect(page.entries[0]).not.toHaveProperty('case_results')
    expect(page.emissions).toMatchObject({
      champion_agent_id: topAgentId,
      raw_leader_decision: { required_lead: 0.011, dethrones: false },
      recipients: [{ shared_seed_confirmations: 7 }],
    })
  })

  it('filters provisional entries and forwards a historical bench version', async () => {
    const historical = {
      ...leaderboard,
      selection_mode: 'historical',
      current_bench_version: 6,
      emissions: null,
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(historical))
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchScoreLeaderboard({ benchVersion: 6, status: 'provisional' })

    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/public/leaderboard?bench_version=6',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(page.count).toBe(1)
    expect(page.entries[0]).toMatchObject({ agent_id: provisionalAgentId, finalized: false })
    expect(page.emissions).toBeNull()
  })

  it('resolves a miner hotkey to its leaderboard submission with rank context', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(Response.json(agentScores))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ minerHotkey: '5TopMiner' })

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `https://platform-api.heyditto.ai/api/v1/public/agent/${topAgentId}/scores`,
      expect.objectContaining({ method: 'GET' }),
    )
    expect(detail).toMatchObject({
      agent_id: topAgentId,
      median_composite: 0.957,
      active_bench_version: 7,
      leaderboard: { rank: 1, emission_eligible: true },
    })
    expect(detail.scores).toHaveLength(6)
    expect(detail.scores[3]).toMatchObject({
      validator_hotkey: '5ValA',
      // Seeds are normalised to exact decimal strings, small ones included, so
      // the field has one type regardless of magnitude.
      seed: '424242',
      bench_version: 7,
    })
  })

  it('returns null leaderboard context for a submission that is not the board row', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(
        Response.json({ ...agentScores, agent_id: supersededAgentId }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: supersededAgentId })

    expect(detail.leaderboard).toBeNull()
    expect(detail.median_composite).toBe(0.957)
  })

  // Regression: production agent e00296b3 (dominator-4) on 2026-07-27. It held
  // 1 of 3 accepted scores — list_stuck_submissions reported score_count 1 and
  // ticket_states {"scored": 1} for it at the same moment — and get_agent_scores
  // answered with the bare error "no public scores for this agent", which reads
  // identically to an unknown agent id and to an agent with zero scores. The
  // platform is not at fault: /agent/{id}/scores is the SETTLED record and 404s
  // by design, while /agent/{id}/pipeline publishes exactly these pre-quorum
  // scores. Backroom was reading only the first surface.
  const PROVISIONAL_SEED = '6211616870656561578'

  const pipeline = {
    generated_at: '2026-07-27T21:00:00Z',
    agent_id: provisionalAgentId,
    status: 'evaluating',
    active_bench_version: 7,
    score_bench_version: 7,
    score_count: 1,
    quorum: 3,
    score_floor: 0.9,
    provisional_scores: [
      {
        composite: 0.948,
        raw_composite: 0.951,
        token_usage: { total: 12345 },
        token_efficiency: { penalty: 0 },
        composite_breakdown: breakdown,
        seed: PROVISIONAL_SEED,
        run_size: 'full',
        bench_version: 7,
        datagen_version: 'v1.2.3',
        seed_source: 'on_chain',
        dataset_sha256: 'cd'.repeat(32),
        accepted_at: '2026-07-27T18:30:00Z',
        reproduction_command: 'dittobench-datagen ...',
        verification_command: 'dittobench-datagen ... --sha-only',
        case_results: [{ case_id: 'c1', passed: true }],
        transcript_sha256: 'bb'.repeat(32),
      },
    ],
    confirmation_scores: [],
    final_composite: null,
    screening_attempts: [],
    validation_attempts: [
      { validator_hotkey: '5ValA', status: 'scored', bench_version: 7 },
    ],
    dispute: null,
  }

  function notFound(detail: string) {
    return Response.json({ detail }, { status: 404 })
  }

  it('answers a below-quorum submission with its provisional scores, not an error', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(notFound('no public scores for this agent'))
      .mockResolvedValueOnce(Response.json(pipeline))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: provisionalAgentId })

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `https://platform-api.heyditto.ai/api/v1/public/agent/${provisionalAgentId}/pipeline`,
      expect.objectContaining({ method: 'GET' }),
    )
    // The read path stays a read: three GETs against the credential-free public
    // ledger, no admin token, nothing written.
    const headers = fetchMock.mock.calls.map(
      (call) => (call[1] as { headers: Record<string, string> }).headers,
    )
    expect(headers.every((header) => !('Authorization' in header))).toBe(true)
    expect(fetchMock.mock.calls.every((call) => call[1].method === 'GET')).toBe(true)

    expect(detail).toMatchObject({
      agent_id: provisionalAgentId,
      status: 'evaluating',
      finalized: false,
      score_count: 1,
      quorum: 3,
      // No canonical aggregate exists below quorum, and Backroom does not
      // manufacture one from a single score.
      median_composite: null,
      // The pin is published with the settled record, not derived from a row.
      dataset_seed: null,
      dataset_sha256: null,
    })
    // The accepted score that DOES exist, with its exact 64-bit seed.
    expect(detail.scores).toHaveLength(1)
    expect(detail.scores[0]).toMatchObject({
      composite: 0.948,
      seed: PROVISIONAL_SEED,
      bench_version: 7,
      generated_at: '2026-07-27T18:30:00Z',
      transcript_sha256: 'bb'.repeat(32),
      // Withheld by the platform before quorum: not published yet, never
      // "no validator scored it", and never inferred from a scored ticket.
      validator_hotkey: null,
      tool_mean: null,
      memory_mean: null,
      run_id: null,
    })
    expect(JSON.parse(JSON.stringify(detail)).scores[0].seed).toBe(PROVISIONAL_SEED)
    // Leaderboard context still answers "provisional where, exactly".
    expect(detail.leaderboard).toMatchObject({ finalized: false, score_count: 1 })
    expect(detail.active_bench_version).toBe(7)
  })

  it('marks a settled submission finalized on the same field', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(Response.json(agentScores))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: topAgentId })

    // The settled record is served from the settled endpoint alone: no
    // pipeline round-trip, and the strict per-validator contract is unchanged.
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(detail.finalized).toBe(true)
    expect(detail.median_composite).toBe(0.957)
    expect(detail.scores[3]).toMatchObject({ validator_hotkey: '5ValA', run_id: 'run-a7' })
  })

  it('reports a below-quorum submission with no accepted score at all', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(notFound('no public scores for this agent'))
      .mockResolvedValueOnce(
        Response.json({ ...pipeline, score_count: 0, provisional_scores: [] }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: provisionalAgentId })

    expect(detail).toMatchObject({ finalized: false, score_count: 0 })
    expect(detail.scores).toEqual([])
  })

  it('reads a provisional submission that holds no leaderboard row', async () => {
    // The board's provisional overlay is one row per emission owner and drops
    // an owner that already holds a finalized row, so a real below-quorum
    // submission is often absent from it. The pre-quorum surface is keyed by
    // agent id and publishes no miner hotkey, so that field is null rather than
    // guessed.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(notFound('no public scores for this agent'))
      .mockResolvedValueOnce(
        Response.json({ ...pipeline, agent_id: supersededAgentId }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: supersededAgentId })

    expect(detail.leaderboard).toBeNull()
    expect(detail.miner_hotkey).toBeNull()
    expect(detail.finalized).toBe(false)
    expect(detail.scores).toHaveLength(1)
  })

  it('separates an unknown agent id from a provisional one', async () => {
    const unknownAgentId = '44444444-4444-4444-8444-444444444444'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(notFound('no public scores for this agent'))
      .mockResolvedValueOnce(notFound('submission not found'))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAgentScores({ agentId: unknownAgentId })).rejects.toThrow(
      /No submission exists with agent id 44444444/,
    )
  })

  it('does not swallow a public-ledger failure that is not a 404', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(Response.json({ detail: 'upstream exploded' }, { status: 503 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAgentScores({ agentId: provisionalAgentId })).rejects.toThrow(
      /upstream exploded/,
    )
    // A 503 is not "still evaluating": no pipeline fallback masks it.
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('names the provisional state instead of 404ing a score-history read', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(notFound('no public scores for this agent'))
      .mockResolvedValueOnce(Response.json(pipeline))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      fetchAgentScoreHistory({ agentId: provisionalAgentId }),
    ).rejects.toThrow(/'evaluating' with 1 of 3 accepted scores on bench version 7/)
  })

  it('rejects an unknown miner hotkey with an actionable error', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json(leaderboard))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAgentScores({ minerHotkey: '5Stranger' })).rejects.toThrow(
      /No leaderboard submission found/,
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('requires exactly one of agentId and minerHotkey', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAgentScores({})).rejects.toThrow(/exactly one/i)
    await expect(
      fetchAgentScores({ agentId: topAgentId, minerHotkey: '5TopMiner' }),
    ).rejects.toThrow(/exactly one/i)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('groups accepted scores per bench version with version-over-version deltas', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json(agentScores))
    vi.stubGlobal('fetch', fetchMock)

    const history = await fetchAgentScoreHistory({ agentId: topAgentId })

    // An exact agent id needs no leaderboard resolution round-trip.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/public/agent/${topAgentId}/scores`,
      expect.objectContaining({ method: 'GET' }),
    )
    expect(history.total_score_count).toBe(6)
    expect(history.versions).toHaveLength(2)
    expect(history.versions[0]).toMatchObject({
      bench_version: 6,
      score_count: 3,
      median_composite: 0.91,
      min_composite: 0.9,
      max_composite: 0.92,
      seeds: ['111'],
      composite_delta_vs_previous: null,
    })
    expect(history.versions[1]).toMatchObject({
      bench_version: 7,
      score_count: 3,
      median_composite: 0.957,
      seeds: ['424242'],
    })
    expect(history.versions[1]?.composite_delta_vs_previous).toBeCloseTo(0.047, 10)
    expect(history.versions[1]?.validators).toEqual(['5ValA', '5ValB', '5ValC'])
  })

  // Regression: production agent 454a09ad (lihai) on 2026-07-25. Every seed
  // below is a verbatim copy from
  // GET /api/v1/public/agent/454a09ad-.../scores and every one of them exceeds
  // Number.MAX_SAFE_INTEGER, so get_agent_scores and get_score_history failed
  // outright with `too_big` for this agent and for every other agent whose
  // seeds were derived on chain. The body has to be raw text: writing these
  // digits as JavaScript number literals in a fixture would round them before
  // the code under test ever ran, which is exactly the bug.
  const PROD_DATASET_SEED = '989366151180340909'
  const PROD_SCORE_SEEDS = [
    '6211616870656561578',
    '8713514997902241464',
    '8811366100733494301',
  ]

  function productionScoresBody() {
    const rows = PROD_SCORE_SEEDS.map((seed, index) =>
      JSON.stringify(
        scoreRow({
          validator_hotkey: `5Val${index}`,
          bench_version: 7,
          seed: 0,
          run_id: `run-${index}`,
          generated_at: `2026-07-25T0${index}:00:00Z`,
        }),
      ).replace('"seed":0', `"seed":${seed}`),
    )
    return JSON.stringify({
      ...agentScores,
      dataset_seed: 0,
      scores: [],
    })
      .replace('"dataset_seed":0', `"dataset_seed":${PROD_DATASET_SEED}`)
      .replace('"scores":[]', `"scores":[${rows.join(',')}]`)
  }

  function rawJsonResponse(body: string) {
    return new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  it('preserves 64-bit production seeds exactly through get_agent_scores', async () => {
    const body = productionScoresBody()
    // Guard the fixture itself: it must carry the real digits on the wire.
    expect(body).toContain(`"dataset_seed":${PROD_DATASET_SEED}`)
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(leaderboard))
      .mockResolvedValueOnce(rawJsonResponse(body))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchAgentScores({ agentId: topAgentId })

    expect(detail.dataset_seed).toBe(PROD_DATASET_SEED)
    expect(detail.scores.map((score) => score.seed)).toEqual(PROD_SCORE_SEEDS)
    // The whole point: the seed survives serialization into an MCP result.
    const serialized = JSON.parse(JSON.stringify(detail)) as typeof detail
    expect(serialized.dataset_seed).toBe(PROD_DATASET_SEED)
    expect(serialized.scores.map((score) => score.seed)).toEqual(PROD_SCORE_SEEDS)
    // A plain JSON.parse of the same body would have silently rounded them.
    const rounded = (JSON.parse(body) as { dataset_seed: number }).dataset_seed
    expect(String(rounded)).not.toBe(PROD_DATASET_SEED)
  })

  it('preserves 64-bit production seeds exactly through get_score_history', async () => {
    const fetchMock = vi.fn().mockResolvedValue(rawJsonResponse(productionScoresBody()))
    vi.stubGlobal('fetch', fetchMock)

    const history = await fetchAgentScoreHistory({ agentId: topAgentId })

    expect(history.versions).toHaveLength(1)
    expect(history.versions[0]?.seeds).toEqual(PROD_SCORE_SEEDS)
    expect(JSON.parse(JSON.stringify(history.versions[0]?.seeds))).toEqual(PROD_SCORE_SEEDS)
  })
})

describe('validator slot administration', () => {
  const settings = {
      max_concurrent_slots: 2,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    }
  // The exact payload production answers today, before any operator revision.
  const control = {
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
  }

  it('reads and appends the exact platform-owned slot contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const nextSettings = {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 85,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    }
    const applied = {
      ...control,
      effective: {
        ...control.effective,
        revision: 1,
        settings: nextSettings,
        checksum: 'ab'.repeat(32),
        source: 'revision',
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json({ revision: 1 }))
      .mockResolvedValueOnce(Response.json(applied))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchValidatorSlotSettings()).resolves.toEqual(control)
    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: nextSettings,
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          confirmation: 'APPLY VALIDATOR SLOT CAP 3',
        },
        'operator@omniaura.ai',
      ),
    ).resolves.toEqual(applied)

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/admin/validator-slot-settings',
      expect.objectContaining({
        method: 'POST',
        // The audited actor is always the signed-in operator, never a tool
        // argument, and the confirmation is passed through exactly as typed.
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: nextSettings,
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          actor: 'operator@omniaura.ai',
          confirmation: 'APPLY VALIDATOR SLOT CAP 3',
        }),
      }),
    )
  })

  it('names the recovery for a stale expected revision', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json(
        {
          detail:
            'validator slot settings changed; refresh before applying (expected 0, current 2)',
        },
        { status: 409 },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    },
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          confirmation: 'APPLY VALIDATOR SLOT CAP 3',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(/expected 0, current 2.*get_validator_slot_settings/s)
    // Nothing is re-read after a refusal, so the operator cannot mistake a
    // fresh GET for a successful apply.
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces a platform confirmation refusal verbatim', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const detail = 'confirmation must be exactly APPLY VALIDATOR SLOT CAP 3'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(Response.json({ detail }, { status: 409 })),
    )

    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    },
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          confirmation: 'APPLY VALIDATOR SLOT CAP 3',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow(/APPLY VALIDATOR SLOT CAP 3.*naming the cap|must name the cap/s)
  })

  // The empty-default failure class. The platform 422s a partial body, but a
  // client that defaults its knobs would pre-fill the omission into a full body
  // and ship a default the operator never chose.
  it('rejects a partial policy before any admin call rather than defaulting it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: { max_concurrent_slots: 3 },
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          confirmation: 'APPLY VALIDATOR SLOT CAP 3',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a cap the protocol ceiling cannot support before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: {
      max_concurrent_slots: 12,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    },
          reason: 'raise the cap past the advertised protocol ceiling',
          confirmation: 'APPLY VALIDATOR SLOT CAP 12',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  // The number is stated twice on purpose. A caller who types 2 into the
  // confirmation while ramping to 3 no longer agrees with themselves, and the
  // mismatch is caught before the admin API is ever reached.
  it('refuses a confirmation that names a different cap than the one being applied', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      setValidatorSlotSettings(
        {
          expectedRevision: 0,
          settings: {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    },
          reason: 'ramp the fleet to three slots now that dispatch is stable',
          confirmation: 'APPLY VALIDATOR SLOT CAP 2',
        },
        'operator@omniaura.ai',
      ),
    ).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('owner-link attestations', () => {
  const queried = '5QueriedHotkey'
  const counterparty = '5AlphaLinkedHotkey'

  const link = {
    attestation_id: '77777777-7777-4777-8777-777777777777',
    netuid: 118,
    // Sorted pair, not old/new: the link carries no direction.
    hotkey_lo: counterparty,
    hotkey_hi: queried,
    counterparty,
    evidence_grade: 'hotkey-hotkey',
    lo_key_kind: 'hotkey',
    lo_signer: counterparty,
    hi_key_kind: 'hotkey',
    hi_signer: queried,
    nonce: '88888888-8888-4888-8888-888888888888',
    issued_at: '2026-07-01T00:00:00Z',
    created_at: '2026-07-01T00:05:00Z',
    revoked_at: null,
    revoked_by: null,
    revoked_reason: null,
    active: true,
  }

  const response = {
    hotkey: queried,
    netuid: 118,
    attestations: [link],
    linked_hotkeys: [
      {
        hotkey: counterparty,
        attestation_id: link.attestation_id,
        evidence_grade: 'hotkey-hotkey',
      },
    ],
    linkage_basis: 'signed_owner_attestation',
    scope_caveat:
      'Exempts near-duplicate plagiarism screening between the linked hotkeys only; not an input to emission-slot allocation.',
  }

  it('reads the signed links for one hotkey from the admin endpoint', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(response))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchOwnerAttestations({ hotkey: queried })).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/owner-attestations/${queried}`,
      expect.objectContaining({ method: 'GET' }),
    )
  })

  // All three grades are the same link as far as the exemption goes, so all
  // three have to survive the read intact: a parse that quietly dropped or
  // narrowed one would invite a reviewer to invent a strength threshold the
  // platform does not apply.
  it('round-trips every evidence grade without ranking them', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const grades = ['coldkey-coldkey', 'mixed', 'hotkey-hotkey'] as const
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          ...response,
          attestations: grades.map((grade, index) => ({
            ...link,
            attestation_id: `1111111${index}-1111-4111-8111-111111111111`,
            hotkey_lo: `5Linked${grade}`,
            counterparty: `5Linked${grade}`,
            evidence_grade: grade,
            lo_key_kind: grade === 'coldkey-coldkey' ? 'coldkey' : 'hotkey',
            hi_key_kind: grade === 'hotkey-hotkey' ? 'hotkey' : 'coldkey',
          })),
          linked_hotkeys: grades.map((grade, index) => ({
            hotkey: `5Linked${grade}`,
            attestation_id: `1111111${index}-1111-4111-8111-111111111111`,
            evidence_grade: grade,
          })),
        }),
      ),
    )

    const detail = await fetchOwnerAttestations({ hotkey: queried })

    expect(detail.attestations?.map((row) => row.evidence_grade)).toEqual([
      'coldkey-coldkey',
      'mixed',
      'hotkey-hotkey',
    ])
    expect(detail.attestations?.map((row) => row.counterparty)).toEqual([
      '5Linkedcoldkey-coldkey',
      '5Linkedmixed',
      '5Linkedhotkey-hotkey',
    ])
    expect(detail.linked_hotkeys?.map((row) => row.evidence_grade)).toEqual([
      'coldkey-coldkey',
      'mixed',
      'hotkey-hotkey',
    ])
    // Every grade is an active link; none of them is downgraded on the way in.
    expect(detail.attestations?.every((row) => row.active === true)).toBe(true)
  })

  // What a dispute turns on is whether the link was live when the submission
  // under review was made, so a revoked link must survive the read and arrive
  // marked, rather than being filtered into a cleaner-looking answer.
  it('returns a revoked link and keeps its revocation record', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        ...response,
        // Revoked links drop out of linked_hotkeys, which is why the
        // attestation list is the one a dispute has to be read against.
        linked_hotkeys: [],
        attestations: [
          {
            ...link,
            revoked_at: '2026-07-20T00:00:00Z',
            revoked_by: 'peyton@omniaura.ai',
            revoked_reason: 'Miner reported the linked hotkey compromised',
            active: false,
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchOwnerAttestations({ hotkey: queried })

    expect(detail.attestations).toHaveLength(1)
    expect(detail.attestations?.[0]).toMatchObject({
      counterparty,
      hotkey_lo: counterparty,
      hotkey_hi: queried,
      revoked_at: '2026-07-20T00:00:00Z',
      revoked_by: 'peyton@omniaura.ai',
      active: false,
    })
    expect(detail.linked_hotkeys).toEqual([])
  })

  // Having no signed link is the ordinary case, not a failure — and it is the
  // answer that matters most when a miner claims otherwise.
  it('reads an unknown hotkey as empty lists rather than an error', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          hotkey: '5NeverLinked',
          netuid: 118,
          attestations: [],
          linked_hotkeys: [],
          linkage_basis: 'signed_owner_attestation',
          scope_caveat: 'No signed owner link is recorded for this hotkey.',
        }),
      ),
    )

    const detail = await fetchOwnerAttestations({ hotkey: '5NeverLinked' })

    expect(detail.attestations).toEqual([])
    expect(detail.linked_hotkeys).toEqual([])
  })

  // A platform that predates a field parses as unknown instead of failing and
  // taking the whole review surface down with it. Identity stays strict: a
  // link we cannot name both ends of is not a link a reviewer can act on.
  it('parses a platform that predates the grade, revocation, and scope fields', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          hotkey: queried,
          attestations: [
            {
              attestation_id: link.attestation_id,
              hotkey_lo: counterparty,
              hotkey_hi: queried,
              counterparty,
            },
          ],
        }),
      ),
    )

    const detail = await fetchOwnerAttestations({ hotkey: queried })

    expect(detail.attestations?.[0]).toMatchObject({
      counterparty,
      // Unknown, not "ungraded-and-therefore-weaker".
      evidence_grade: null,
      lo_key_kind: null,
      hi_key_kind: null,
      revoked_at: null,
      // Unknown, not "live": revoked_at stays the field a dispute reads.
      active: null,
      netuid: null,
    })
    expect(detail.linked_hotkeys).toEqual([])
    expect(detail.scope_caveat).toBeNull()
  })

  it('refuses a link whose counterparty the platform did not name', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          hotkey: queried,
          attestations: [{ ...link, counterparty: undefined }],
        }),
      ),
    )

    await expect(fetchOwnerAttestations({ hotkey: queried })).rejects.toThrow()
  })

  it('rejects a hotkey shorter than any real key before calling the platform', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchOwnerAttestations({ hotkey: '5' })).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('validator fleet context', () => {
  // The platform already publishes advertised capacity and reported disk on its
  // heartbeat view, so the slot console reads that rather than asking for a new
  // admin endpoint.
  const fleet = {
    generated_at: '2026-07-25T21:17:06.573455Z',
    active_bench_version: 7,
    validators: [
      {
        validator_hotkey: '5CqJAjSjabcdefghijklmnopqrstuvwxyz01234567890',
        software_version: '7.0.0',
        protocol_version: 15,
        state: 'running_benchmark',
        configured_slots: 4,
        healthy_slots: ['slot-0', 'slot-1', 'slot-2', 'slot-3'],
        admission: 'accepting',
        active_benchmarks: [{ slot_id: 'slot-0' }, { slot_id: 'slot-1' }],
        assignment_state: 'synchronized',
        reported_at: '2026-07-25T21:16:00Z',
        seen_at: '2026-07-25T21:16:30Z',
        online: true,
        availability: 'available',
        health: 'healthy',
        bench_serviceability: 'serving',
        system_metrics: {
          cpu_percent: 40,
          memory_percent: 55,
          disk_percent: 85,
          docker_status: 'healthy',
          running_containers: 4,
          unhealthy_containers: 0,
        },
      },
    ],
  }

  afterEach(() => {
    vi.unstubAllGlobals()
    delete process.env.DITTO_ADMIN_API_TOKEN
  })

  it('keeps only what a cap decision needs from the heartbeat view', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(fleet))
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchValidatorFleet()

    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/public/validators',
      expect.objectContaining({ method: 'GET' }),
    )
    // Only the fields a cap decision reads survive the parse; the rest of the
    // heartbeat view (stack identity, per-check progress, capabilities) is
    // dropped rather than shipped to the browser.
    expect(parsed?.validators[0]).toEqual({
      validator_hotkey: fleet.validators[0].validator_hotkey,
      configured_slots: 4,
      healthy_slot_count: 4,
      admission: 'accepting',
      active_benchmark_count: 2,
      online: true,
      disk_percent: 85,
      bench_serviceability: 'serving',
      orphaned_slots: [],
    })
  })

  // Advisory context must never take down the page that carries the kill switch.
  it('resolves to null when the fleet read fails', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(Response.json({ detail: 'nope' }, { status: 503 })),
    )

    await expect(fetchValidatorFleet()).resolves.toBeNull()
  })
})

describe('owner footprint', () => {
  const alphaAgentId = '55555555-5555-4555-8555-555555555555'

  const footprint = {
    identifier: '5TopMiner',
    identifier_kind: 'miner_hotkey',
    depth: 1,
    miner_coldkeys: ['5Cold'],
    hotkey_count: 2,
    submission_count: 3,
    expansion_complete: true,
    ownership_basis: 'evaluation_payment_records',
    linkage_caveat:
      'Coldkeys here are payment-time records of who paid for each evaluation, not on-chain metagraph ownership.',
    hotkeys: [
      {
        miner_hotkey: '5TopMiner',
        miner_coldkeys: ['5Cold'],
        link_hop: 0,
        submission_count: 2,
        paid_submission_count: 2,
        latest_submitted_at: '2026-07-22T00:00:00Z',
        agents_truncated: false,
        agents: [
          {
            agent_id: alphaAgentId,
            agent_name: 'apex-agent',
            agent_version: 4,
            agent_status: 'scored',
            artifact_sha256: 'ab'.repeat(32),
            submitted_at: '2026-07-22T00:00:00Z',
            miner_coldkey: '5Cold',
          },
        ],
      },
      {
        miner_hotkey: '5Sibling',
        miner_coldkeys: ['5Cold'],
        link_hop: 1,
        submission_count: 1,
        paid_submission_count: 1,
        latest_submitted_at: '2026-07-19T00:00:00Z',
        agents_truncated: false,
        agents: [],
      },
    ],
  }

  const leaderboard = {
    generated_at: '2026-07-23T00:00:00Z',
    count: 1,
    current_bench_version: 7,
    active_bench_version: 7,
    desired_bench_version: 7,
    available_bench_versions: [7],
    selection_mode: 'authoritative',
    entries: [
      {
        rank: 1,
        finalized: true,
        score_count: 3,
        score_quorum: 3,
        agent_id: alphaAgentId,
        agent_name: 'apex-agent',
        agent_version: 4,
        miner_hotkey: '5TopMiner',
        miner_uid: 12,
        registered: true,
        emission_eligible: true,
        composite: 0.957,
        tool_mean: 0.981,
        memory_mean: 0.933,
        first_seen: '2026-07-01T00:00:00Z',
        eligible: true,
        bench_version: 7,
      },
    ],
    emissions: null,
  }

  it('joins linked hotkeys with their current leaderboard standing', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(footprint))
      .mockResolvedValueOnce(Response.json(leaderboard))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchOwnerFootprint({ key: '5TopMiner' })

    // Linkage comes from the admin ledger; standings from the public board.
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/miner-owners/5TopMiner?depth=1&agents_per_hotkey=10',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://platform-api.heyditto.ai/api/v1/public/leaderboard',
      expect.objectContaining({ method: 'GET' }),
    )
    const publicHeaders = (
      fetchMock.mock.calls[1]?.[1] as { headers: Record<string, string> }
    ).headers
    expect(publicHeaders).not.toHaveProperty('Authorization')

    expect(detail.hotkey_count).toBe(2)
    expect(detail.ranked_hotkey_count).toBe(1)
    expect(detail.active_bench_version).toBe(7)
    expect(detail.hotkeys[0]).toMatchObject({
      miner_hotkey: '5TopMiner',
      link_hop: 0,
      leaderboard: { rank: 1, emission_eligible: true },
    })
    // A linked hotkey with no board row is absent from the leaderboard, which
    // is not the same as ineligible.
    expect(detail.hotkeys[1]?.leaderboard).toBeNull()
    expect(detail.ownership_basis).toBe('evaluation_payment_records')
  })

  it('forwards depth and per-hotkey bounds to the platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ ...footprint, depth: 2, expansion_complete: false }),
      )
      .mockResolvedValueOnce(Response.json(leaderboard))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await fetchOwnerFootprint({
      key: '5Cold',
      depth: 2,
      agentsPerHotkey: 3,
    })

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/miner-owners/5Cold?depth=2&agents_per_hotkey=3',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(detail.expansion_complete).toBe(false)
  })

  it('rejects a key outside the accepted length before calling the platform', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchOwnerFootprint({ key: '5' })).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('lease revocation ledger', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    delete process.env.DITTO_ADMIN_API_TOKEN
  })

  it('sends every filter, repeating the ones the platform takes as lists', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        generated_at: '2026-07-27T18:00:00Z',
        total: 1,
        revocations: [
          {
            audit_id: '22222222-2222-4222-8222-222222222222',
            agent_id: agentId,
            validator_hotkey: '5Validator',
            slot_id: 'slot-1',
            bench_version: 7,
            action: 'operator_evicted',
            reason: 'operator_evicted_occupied_not_progressing',
            context: 'issue_ticket',
            recorded_at: '2026-07-27T17:59:00Z',
            evidence: { lease_age_seconds: 5400, heartbeat_seen_at: null },
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchLeaseRevocations({
      agentId,
      validatorHotkey: '5Validator',
      action: ['operator_evicted', 'force_expired'],
      context: ['issue_ticket'],
      since: '2026-07-27T00:00:00Z',
      limit: 25,
      offset: 5,
    })

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    const query = new URL(url).searchParams
    expect(new URL(url).pathname).toBe('/api/v1/admin/lease-revocations')
    expect(query.get('agent_id')).toBe(agentId)
    expect(query.get('validator_hotkey')).toBe('5Validator')
    // `action` and `context` are `Annotated[list[str] | None, Query()]` on the
    // platform, so a second value has to arrive as a second parameter; joining
    // them would filter for one literal action named "a,b" and answer empty.
    expect(query.getAll('action')).toEqual(['operator_evicted', 'force_expired'])
    expect(query.getAll('context')).toEqual(['issue_ticket'])
    expect(query.get('since')).toBe('2026-07-27T00:00:00Z')
    expect(query.get('limit')).toBe('25')
    expect(query.get('offset')).toBe('5')
    // Evidence survives the round trip untouched, including a null value that a
    // "drop the empties" pass would have quietly removed.
    expect(parsed.revocations[0].evidence).toEqual({
      lease_age_seconds: 5400,
      heartbeat_seen_at: null,
    })
  })

  it('reads an empty production ledger without inventing a failure', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        generated_at: '2026-07-27T18:00:00Z',
        total: 0,
        revocations: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchLeaseRevocations({})

    // No filters, just the bounded defaults, which is what an operator opening
    // the ledger cold actually sends.
    const query = new URL((fetchMock.mock.calls[0] as [string, RequestInit])[0])
      .searchParams
    expect(query.get('limit')).toBe('50')
    expect(query.get('offset')).toBe('0')
    expect(query.get('agent_id')).toBeNull()
    // Empty is an answer: the platform has revoked nothing in the window, so a
    // run that died did so by some other path.
    expect(parsed).toEqual({
      generated_at: '2026-07-27T18:00:00Z',
      total: 0,
      revocations: [],
    })
  })
})
