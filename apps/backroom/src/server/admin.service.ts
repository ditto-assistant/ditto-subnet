import '@tanstack/react-start/server-only'

import type { operations as PlatformOperations } from '../generated/platform-api'

import {
  athReviewAuditSchema,
  copyReviewConsoleListSchema,
  copyReviewCurrentComparisonSchema,
  copyReviewListSchema,
  type CopyReviewGeneration,
  getAthReviewInputSchema,
  openAthReviewInputSchema,
  openAthReviewResponseSchema,
  resolveCopyReviewInputSchema,
  resolveCopyReviewResponseSchema,
  baselineDiffFileDetailSchema,
  baselineDiffFileInputSchema,
  baselineDiffInputSchema,
  baselineDiffManifestSchema,
  benchmarkRolloutControlSchema,
  ownerAttestationLookupInputSchema,
  ownerAttestationsSchema,
  quarantineContextInputSchema,
  rescreenRejectedSubmissionInputSchema,
  rescreenRejectedSubmissionResponseSchema,
  retryFailedScreeningNowInputSchema,
  retryFailedScreeningNowResponseSchema,
  resolveScreeningQuarantineInputSchema,
  resolveScreeningQuarantineResponseSchema,
  resolveScreeningDisputeInputSchema,
  resolveScreeningDisputeResponseSchema,
  screeningDisputeListSchema,
  screeningQuarantineBatchContextInputSchema,
  screeningQuarantineBatchContextResponseSchema,
  screeningQuarantineBatchExecuteInputSchema,
  screeningQuarantineBatchExecuteResponseSchema,
  screeningQuarantineBatchPreviewInputSchema,
  screeningQuarantineBatchPreviewResponseSchema,
  screeningQuarantineContextSchema,
  screeningQuarantineListSchema,
  screeningArtifactInputSchema,
  screeningArtifactSchema,
  screeningSubmissionLookupInputSchema,
  screeningSubmissionSchema,
  screeningSubmissionListSchema,
  sourceDiffFileDetailSchema,
  sourceDiffFileInputSchema,
  sourceDiffInputSchema,
  sourceDiffManifestSchema,
  sourceExcerptInputSchema,
  sourceExcerptSchema,
  sourceListingInputSchema,
  sourceListingSchema,
  unavailableCopyReviewComparison,
  validatorAssignmentListSchema,
  releaseValidatorAssignmentInputSchema,
  releaseValidatorAssignmentResponseSchema,
  retryValidationInputSchema,
  retryValidationResponseSchema,
  withdrawValidationInputSchema,
  withdrawValidationResponseSchema,
  evictValidationInputSchema,
  evictValidationResponseSchema,
  reinstateValidationInputSchema,
  reinstateValidationResponseSchema,
  validatorScoreReplacementLookupInputSchema,
  validatorScoreReplacementDetailSchema,
  replaceValidatorScoreInputSchema,
  replaceValidatorScoreResponseSchema,
  releaseValidatorScoreRetestInputSchema,
  releaseValidatorScoreRetestResponseSchema,
  queueValidatorScoreRetestsInputSchema,
  queueValidatorScoreRetestsResponseSchema,
  scoreOutlierFiltersSchema,
  scoreOutlierListSchema,
  validationRetryDetailSchema,
  validationRetryLookupInputSchema,
  listStuckSubmissionsInputSchema,
  stuckSubmissionsListSchema,
  listLeaseRevocationsInputSchema,
  leaseRevocationsListSchema,
  batchRetryValidationInputSchema,
  batchRetryValidationResponseSchema,
  agentScoringReadinessInputSchema,
  agentScoringReadinessSchema,
  benchmarkContractRefreshLookupInputSchema,
  benchmarkContractRefreshDetailSchema,
  refreshBenchmarkContractInputSchema,
  refreshBenchmarkContractResponseSchema,
  screenedImageRebuildLookupInputSchema,
  screenedImageRebuildDetailSchema,
  rebuildScreenedImageInputSchema,
  rebuildScreenedImageResponseSchema,
  benchmarkContractMigrationLookupInputSchema,
  benchmarkContractMigrationDetailSchema,
  migrateBenchmarkContractInputSchema,
  migrateBenchmarkContractResponseSchema,
  benchmarkRolloutQualificationLookupInputSchema,
  benchmarkRolloutQualificationDetailSchema,
  qualifyBenchmarkRolloutInputSchema,
  qualifyBenchmarkRolloutResponseSchema,
  expandBenchmarkRolloutInputSchema,
  expandBenchmarkRolloutResponseSchema,
  startBenchmarkRolloutInputSchema,
  supersedeBenchmarkRolloutInputSchema,
  selectActiveBenchmarkInputSchema,
  applyScreenerReviewSettingsInputSchema,
  efficiencyBonusSettingsControlSchema,
  efficiencyBonusSettingsRevisionSchema,
  setEfficiencyBonusSettingsInputSchema,
  burnSettingsControlSchema,
  burnSettingsRevisionSchema,
  setBurnSettingsInputSchema,
  continualRetestSettingsForPlatform,
  parseContinualRetestSettingsControl,
  setContinualRetestSettingsInputSchema,
  inferenceConcurrencySettingsControlSchema,
  queuePolicySettingsControlSchema,
  setInferenceConcurrencySettingsInputSchema,
  setQueuePolicySettingsInputSchema,
  validatorSlotSettingsControlSchema,
  setValidatorSlotSettingsInputSchema,
  validatorFleetSchema,
  artifactReleaseControlSchema,
  submissionSettingsControlSchema,
  screenerReviewControlSchema,
  screenerReviewRevisionSchema,
  screenerCapacityViewSchema,
  inferenceRouteCalibrationInputSchema,
  inferenceRoutingInventorySchema,
  inferenceRoutingPolicyInputSchema,
  updateArtifactReleaseSettingsInputSchema,
  updateSubmissionSettingsInputSchema,
  agentScoresLookupInputSchema,
  agentScoresDetailSchema,
  agentScoreHistorySchema,
  ownerFootprintLookupInputSchema,
  ownerFootprintSchema,
  ownerFootprintDetailSchema,
  publicAgentScoresSchema,
  publicLeaderboardSchema,
  publicSubmissionPipelineSchema,
  scoreLeaderboardInputSchema,
  scoreLeaderboardPageSchema,
  type PublicLeaderboardEntry,
  type PublicSubmissionPipeline,
  authorizeConfirmationBundleRetestInputSchema,
  confirmationBundleDetailInputSchema,
  confirmationBundleListInputSchema,
  confirmationBundleListSchema,
  confirmationBundleRetestResponseSchema,
  confirmationBundleSettingsControlSchema,
  confirmationBundleViewSchema,
  setConfirmationBundleSettingsInputSchema,
} from '../lib/admin.schemas'
import {
  PlatformAdminError,
  isPlatformPublicNotFound,
  platformAdminRequest,
  platformPublicRequest,
} from './ditto.server'
import { minerFeeSummarySchema } from '../lib/miner-fees'
import { deriveRequestId } from '../lib/idempotency'


/**
 * Longer than the platform's own read budget for this endpoint, on purpose.
 * The platform bounds the read and answers with a named 503 when it overruns;
 * aborting first would replace that diagnosis with a bare client-side abort,
 * which is precisely how this page once sent an operator hunting for a missing
 * endpoint and a bad admin token while the real problem was read latency.
 */
const ROLLOUT_STATUS_TIMEOUT_MS = 25_000

/** Turn a failed rollout-status read into copy that names the real dependency. */
export function benchmarkRolloutStatusError(error: unknown) {
  if (!(error instanceof PlatformAdminError)) return error
  if (error.failure === 'timeout') {
    return new Error(
      `${error.message} The endpoint answered other requests, so this is ` +
        'platform read latency, not the admin token and not a missing route. ' +
        'Retry; if it persists, check the platform API and its database.',
    )
  }
  if (error.failure === 'auth') {
    return new Error(
      `The platform API rejected Backroom's admin token (${error.status}): ` +
        `${error.message} Check DITTO_ADMIN_API_TOKEN in the Worker secrets.`,
    )
  }
  if (error.failure === 'server') {
    return new Error(
      `The platform API failed while reading the rollout status ` +
        `(${error.status}): ${error.message}`,
    )
  }
  return new Error(error.message)
}

export async function fetchBenchmarkRolloutControl() {
  let payload: unknown
  try {
    payload = await platformAdminRequest('/api/v1/admin/benchmark-rollout', {
      timeoutMs: ROLLOUT_STATUS_TIMEOUT_MS,
      // One bounded retry. The read is idempotent and starts nothing, so a
      // transient timeout costs a second attempt rather than a dead page.
      retries: 1,
    })
  } catch (error) {
    throw benchmarkRolloutStatusError(error)
  }
  return benchmarkRolloutControlSchema.parse(payload)
}

export async function fetchMinerFeeSummary() {
  const payload = await platformAdminRequest('/api/v1/admin/miner-fees')
  return minerFeeSummarySchema.parse(payload)
}

export async function fetchInferenceRoutes() {
  const payload = await platformAdminRequest('/api/v1/admin/inference-routes')
  return inferenceRoutingInventorySchema.parse(payload)
}

export async function updateInferenceRoutingPolicy(actor: string, rawInput: unknown) {
  const input = inferenceRoutingPolicyInputSchema.parse(rawInput)
  await platformAdminRequest(
    `/api/v1/admin/inference-routes/policy/${encodeURIComponent(input.model)}`,
    {
      method: 'PUT',
      actor,
      body: {
        enabled: input.enabled,
        expected_revision: input.expectedRevision,
        speed_weight: input.speedWeight,
        cost_weight: input.costWeight,
        exploration_weight: input.explorationWeight,
        exploration_ticket_budget: input.explorationTicketBudget,
        min_tool_accuracy: input.minToolAccuracy,
        min_composite: input.minComposite,
        min_calibration_samples: input.minCalibrationSamples,
        max_error_rate: input.maxErrorRate,
        max_timeout_rate: input.maxTimeoutRate,
        cooldown_seconds: input.cooldownSeconds,
        ewma_alpha: input.ewmaAlpha,
        confirmation: input.confirmation,
      },
    },
  )
  return fetchInferenceRoutes()
}

export async function calibrateInferenceRoute(actor: string, rawInput: unknown) {
  const input = inferenceRouteCalibrationInputSchema.parse(rawInput)
  await platformAdminRequest(
    `/api/v1/admin/inference-routes/${encodeURIComponent(input.profileRevision)}/calibration`,
    {
      method: 'POST',
      actor,
      body: {
        model: input.model,
        provider: input.provider,
        expected_revision: input.expectedRevision,
        action: input.action,
        manifest_sha256: input.manifestSha256,
        tool_accuracy: input.toolAccuracy,
        composite: input.composite,
        sample_count: input.sampleCount,
        confirmation: input.confirmation,
      },
    },
  )
  return fetchInferenceRoutes()
}

export async function startBenchmarkRollout(actor: string, rawInput: unknown) {
  const input = startBenchmarkRolloutInputSchema.parse(rawInput)
  await platformAdminRequest(
    `/api/v1/admin/benchmark-rollout/${input.desiredVersion}`,
    {
      method: 'POST',
      actor,
      // Starting a rollout renders and pins five target-version datasets before
      // committing the snapshot. Keep ordinary admin calls fail-fast, but allow
      // this intentionally long-running, idempotent operation to finish.
      timeoutMs: 120_000,
      body: {
        actor,
        reason: input.reason,
        confirmation: input.confirmation,
        expected_active_version: input.expectedActiveVersion,
      },
    },
  )
  return fetchBenchmarkRolloutControl()
}

export async function expandBenchmarkRollout(actor: string, rawInput: unknown) {
  const input = expandBenchmarkRolloutInputSchema.parse(rawInput)
  type ExpandRequest = PlatformOperations['expand_rollout_api_v1_admin_benchmark_rollout__desired_version__expand_post']['requestBody']['content']['application/json']
  const payload = await platformAdminRequest(
    `/api/v1/admin/benchmark-rollout/${input.desiredVersion}/expand`,
    {
      method: 'POST',
      actor,
      // Expansion may render and pin several target-version datasets before
      // committing the guarded suffix, just like rollout start.
      timeoutMs: 120_000,
      body: {
        actor,
        reason: input.reason,
        confirmation: input.confirmation,
        expected_active_version: input.expectedActiveVersion,
        expected_current_target: input.expectedCurrentTarget,
        new_target: input.newTarget,
      } satisfies ExpandRequest,
    },
  )
  return expandBenchmarkRolloutResponseSchema.parse(payload)
}

export async function supersedeBenchmarkRollout(actor: string, rawInput: unknown) {
  const input = supersedeBenchmarkRolloutInputSchema.parse(rawInput)
  await platformAdminRequest(
    `/api/v1/admin/benchmark-rollout/${input.desiredVersion}/supersede`,
    {
      method: 'POST',
      actor,
      body: {
        actor,
        reason: input.reason,
        confirmation: input.confirmation,
      },
    },
  )
  return fetchBenchmarkRolloutControl()
}

export async function selectActiveBenchmark(actor: string, rawInput: unknown) {
  const input = selectActiveBenchmarkInputSchema.parse(rawInput)
  await platformAdminRequest(
    `/api/v1/admin/benchmark-rollout/${input.desiredVersion}/select-active`,
    {
      method: 'POST',
      actor,
      body: {
        actor,
        reason: input.reason,
        confirmation: input.confirmation,
        expected_active_version: input.expectedActiveVersion,
      },
    },
  )
  return fetchBenchmarkRolloutControl()
}

export async function fetchScreenerReviewControl() {
  const payload = await platformAdminRequest('/api/v1/admin/screener-review-settings')
  return screenerReviewControlSchema.parse(payload)
}

export async function fetchScreenerCapacity() {
  const payload = await platformAdminRequest('/api/v1/admin/screener-capacity')
  return screenerCapacityViewSchema.parse(payload)
}

export async function fetchArtifactReleaseControl() {
  const payload = await platformAdminRequest('/api/v1/admin/artifact-release-settings')
  return artifactReleaseControlSchema.parse(payload)
}

export async function updateArtifactReleaseSettings(actor: string, rawInput: unknown) {
  const input = updateArtifactReleaseSettingsInputSchema.parse(rawInput)
  type ArtifactReleaseRequest = PlatformOperations['create_settings_revision_api_v1_admin_artifact_release_settings_post']['requestBody']['content']['application/json']
  const body = {
    expected_revision: input.expectedRevision,
    disclosure: input.disclosure,
    embargo_hours: input.embargoHours,
    reason: input.reason,
    actor,
    confirmation: input.confirmation,
  } satisfies ArtifactReleaseRequest
  await platformAdminRequest('/api/v1/admin/artifact-release-settings', {
    method: 'POST',
    actor,
    // Every field the platform's request model declares. An omitted one is a
    // field the platform resets to its default, and on this board that means
    // the subnet's release policy changing as a side effect of an unrelated
    // window edit.
    body,
  })
  return fetchArtifactReleaseControl()
}

const SUBMISSION_SETTINGS_PATH = '/api/v1/admin/submission-settings'

export async function fetchSubmissionSettingsControl() {
  const payload = await platformAdminRequest(SUBMISSION_SETTINGS_PATH)
  return submissionSettingsControlSchema.parse(payload)
}

export async function updateSubmissionSettings(actor: string, rawInput: unknown) {
  const input = updateSubmissionSettingsInputSchema.parse(rawInput)
  await platformAdminRequest(SUBMISSION_SETTINGS_PATH, {
    method: 'POST',
    actor,
    body: {
      expected_revision: input.expectedRevision,
      cooldown_seconds: input.cooldownSeconds,
      fee_amount_rao: input.feeAmountRao,
      reason: input.reason,
      actor,
      confirmation: input.confirmation,
    },
  })
  return fetchSubmissionSettingsControl()
}

export async function applyScreenerReviewSettings(actor: string, rawInput: unknown) {
  const input = applyScreenerReviewSettingsInputSchema.parse(rawInput)
  const payload = await platformAdminRequest('/api/v1/admin/screener-review-settings', {
    method: 'POST',
    actor,
    body: {
      scope: input.scope,
      expected_revision: input.expectedRevision,
      settings: input.settings,
      reason: input.reason,
      actor,
      confirmation: input.confirmation,
    },
  })
  return screenerReviewRevisionSchema.parse(payload)
}

const EFFICIENCY_BONUS_SETTINGS_PATH = '/api/v1/admin/efficiency-bonus-settings'

export async function fetchEfficiencyBonusSettings() {
  const payload = await platformAdminRequest(EFFICIENCY_BONUS_SETTINGS_PATH)
  return efficiencyBonusSettingsControlSchema.parse(payload)
}

// The platform answers a stale `expected_revision`, and a concurrent write that
// wins the same parent revision, with 409 and a message naming the revision now
// current. Name the recovery so an operator re-reads the policy instead of
// retrying a guard that can no longer hold.
function efficiencyBonusConflict(cause: unknown) {
  const message = cause instanceof Error ? cause.message : String(cause)
  if (!/efficiency bonus settings changed/i.test(message)) return null
  return new Error(
    `${message}. Nothing was applied: re-read get_efficiency_bonus_settings and resubmit with the revision it reports.`,
  )
}

export async function setEfficiencyBonusSettings(rawInput: unknown, actor: string) {
  const input = setEfficiencyBonusSettingsInputSchema.parse(rawInput)
  try {
    const payload = await platformAdminRequest(EFFICIENCY_BONUS_SETTINGS_PATH, {
      method: 'POST',
      actor,
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
    return efficiencyBonusSettingsRevisionSchema.parse(payload)
  } catch (cause) {
    throw efficiencyBonusConflict(cause) ?? cause
  }
}

const BURN_SETTINGS_PATH = '/api/v1/admin/burn-settings'

export async function fetchBurnSettings() {
  const payload = await platformAdminRequest(BURN_SETTINGS_PATH)
  return burnSettingsControlSchema.parse(payload)
}

// The platform refuses a burn revision on a stale `expected_revision` or a
// concurrent write that won the same parent, and its message names the revision
// now current. Keep that wording verbatim and only append the recovery — an
// operator reading "nothing was applied" about the emission split must not have
// to wonder whether Backroom paraphrased it.
function burnSettingsConflict(cause: unknown) {
  const message = cause instanceof Error ? cause.message : String(cause)
  if (!/burn settings changed/i.test(message)) return null
  return new Error(
    `${message}. Nothing was applied: re-read get_burn_settings and resubmit with the revision it reports.`,
  )
}

export async function setBurnSettings(rawInput: unknown, actor: string) {
  const input = setBurnSettingsInputSchema.parse(rawInput)
  try {
    const payload = await platformAdminRequest(BURN_SETTINGS_PATH, {
      method: 'POST',
      actor,
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
    burnSettingsRevisionSchema.parse(payload)
  } catch (cause) {
    throw burnSettingsConflict(cause) ?? cause
  }
  return fetchBurnSettings()
}

const CONFIRMATION_BUNDLE_SETTINGS_PATH = '/api/v1/admin/confirmation-bundle-settings'
const CONFIRMATION_BUNDLES_PATH = '/api/v1/admin/confirmation-bundles'

export async function fetchConfirmationBundleSettings() {
  const payload = await platformAdminRequest(CONFIRMATION_BUNDLE_SETTINGS_PATH)
  return confirmationBundleSettingsControlSchema.parse(payload)
}

function confirmationBundleWriteRefusal(cause: unknown, readTool: string) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  return new Error(
    `${cause.message}. Nothing was applied: re-read ${readTool} and resubmit with its current revision or generation.`,
  )
}

export async function setConfirmationBundleSettings(rawInput: unknown, actor: string) {
  const input = setConfirmationBundleSettingsInputSchema.parse(rawInput)
  try {
    await platformAdminRequest(CONFIRMATION_BUNDLE_SETTINGS_PATH, {
      method: 'POST',
      actor,
      // A settings revision is the complete policy, never a patch. In
      // particular, Backroom does not infer or preserve a profile identity the
      // operator did not include in this exact reviewed write.
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
  } catch (cause) {
    throw (
      confirmationBundleWriteRefusal(cause, 'get_confirmation_bundle_settings') ??
      cause
    )
  }
  return fetchConfirmationBundleSettings()
}

export async function fetchConfirmationBundles(rawInput: unknown = {}) {
  const input = confirmationBundleListInputSchema.parse(rawInput)
  const query = new URLSearchParams({
    limit: String(input.limit),
    offset: String(input.offset),
  })
  if (input.state !== undefined) query.set('state', input.state)
  const payload = await platformAdminRequest(`${CONFIRMATION_BUNDLES_PATH}?${query}`)
  return confirmationBundleListSchema.parse(payload)
}

export async function fetchConfirmationBundle(rawInput: unknown) {
  const input = confirmationBundleDetailInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `${CONFIRMATION_BUNDLES_PATH}/${encodeURIComponent(input.bundleId)}`,
  )
  return confirmationBundleViewSchema.parse(payload)
}

export async function authorizeConfirmationBundleRetest(
  rawInput: unknown,
  actor: string,
) {
  const input = authorizeConfirmationBundleRetestInputSchema.parse(rawInput)
  try {
    const payload = await platformAdminRequest(
      `${CONFIRMATION_BUNDLES_PATH}/${encodeURIComponent(input.bundleId)}/authorize-retest`,
      {
        method: 'POST',
        actor,
        body: {
          request_id: input.requestId,
          expected_generation: input.expectedGeneration,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      },
    )
    return confirmationBundleRetestResponseSchema.parse(payload)
  } catch (cause) {
    throw (
      confirmationBundleWriteRefusal(cause, 'get_confirmation_bundle') ?? cause
    )
  }
}

const CONTINUAL_RETEST_SETTINGS_PATH = '/api/v1/admin/continual-retest-settings'

export async function fetchContinualRetestSettings() {
  const payload = await platformAdminRequest(CONTINUAL_RETEST_SETTINGS_PATH)
  return parseContinualRetestSettingsControl(payload)
}

export async function setContinualRetestSettings(rawInput: unknown, actor: string) {
  const input = setContinualRetestSettingsInputSchema.parse(rawInput)
  // Read the contract before writing to it. Backroom and the platform deploy
  // separately, so this page can be running against a build that has no cohort
  // size; the platform forbids unknown fields, and a rejected revision takes
  // the aggregate mode, the idle switch, and the stand-down policy down with
  // it. `expected_revision` still guards the write, so reading first races
  // nothing.
  const current = await fetchContinualRetestSettings()
  await platformAdminRequest(CONTINUAL_RETEST_SETTINGS_PATH, {
    method: 'POST',
    actor,
    body: {
      scope: input.scope,
      expected_revision: input.expectedRevision,
      settings: continualRetestSettingsForPlatform(input.settings, current),
      reason: input.reason,
      actor,
      confirmation: input.confirmation,
    },
  })
  return fetchContinualRetestSettings()
}

const QUEUE_POLICY_SETTINGS_PATH = '/api/v1/admin/queue-policy-settings'

export async function fetchQueuePolicySettings() {
  const payload = await platformAdminRequest(QUEUE_POLICY_SETTINGS_PATH)
  return queuePolicySettingsControlSchema.parse(payload)
}

// The platform refuses a queue policy revision two ways, and it owns the wording
// of both: a stale `expected_revision` (or a concurrent write that won the same
// parent) and a live lane change attempted while a benchmark rollout is open.
// Keep its detail text verbatim and only append the recovery, so an operator
// never reads a Backroom paraphrase of a refusal the platform decided.
function queuePolicyRefusal(cause: unknown) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  const recovery = /rollout/i.test(cause.message)
    ? 'Nothing was applied: the lane cycle stays locked while a benchmark rollout is open — read effective.rollout_locked_fields from get_queue_policy_settings. Next-rollout cohort sizes can still be changed, and they only take effect at the next rollout start.'
    : 'Nothing was applied: re-read get_queue_policy_settings and resubmit with the revision it reports.'
  return new Error(`${cause.message}. ${recovery}`)
}

export async function setQueuePolicySettings(rawInput: unknown, actor: string) {
  const input = setQueuePolicySettingsInputSchema.parse(rawInput)
  try {
    await platformAdminRequest(QUEUE_POLICY_SETTINGS_PATH, {
      method: 'POST',
      actor,
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
  } catch (cause) {
    throw queuePolicyRefusal(cause) ?? cause
  }
  return fetchQueuePolicySettings()
}

const INFERENCE_CONCURRENCY_SETTINGS_PATH = '/api/v1/admin/inference-concurrency-settings'

export async function fetchInferenceConcurrencySettings() {
  const payload = await platformAdminRequest(INFERENCE_CONCURRENCY_SETTINGS_PATH)
  return inferenceConcurrencySettingsControlSchema.parse(payload)
}

// The platform refuses a revision on a stale `expected_revision`, on a
// concurrent write that won the same parent, and on a scope other than '*'. It
// owns the wording of all three; keep the detail verbatim and append only the
// recovery.
function inferenceConcurrencyRefusal(cause: unknown) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  return new Error(
    `${cause.message}. Nothing was applied: re-read get_inference_concurrency_settings and resubmit with the revision it reports.`,
  )
}

export async function setInferenceConcurrencySettings(rawInput: unknown, actor: string) {
  const input = setInferenceConcurrencySettingsInputSchema.parse(rawInput)
  try {
    await platformAdminRequest(INFERENCE_CONCURRENCY_SETTINGS_PATH, {
      method: 'POST',
      actor,
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
  } catch (cause) {
    throw inferenceConcurrencyRefusal(cause) ?? cause
  }
  return fetchInferenceConcurrencySettings()
}

const VALIDATOR_SLOT_SETTINGS_PATH = '/api/v1/admin/validator-slot-settings'

export async function fetchValidatorSlotSettings() {
  const payload = await platformAdminRequest(VALIDATOR_SLOT_SETTINGS_PATH)
  return validatorSlotSettingsControlSchema.parse(payload)
}

// The platform refuses a slot revision three ways and owns the wording of all
// three: a stale `expected_revision` (or a concurrent write that won the same
// parent), a confirmation that does not name the resulting cap, and a scope
// other than the subnet-global `*`. Keep its detail text verbatim and only
// append the recovery, so an operator never reads a Backroom paraphrase of a
// refusal the platform decided.
function validatorSlotRefusal(cause: unknown) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  const recovery = /confirmation/i.test(cause.message)
    ? 'Nothing was applied: the confirmation must name the cap this revision applies, typed out rather than derived from the number above it.'
    : 'Nothing was applied: re-read the current policy (Refresh policy, or get_validator_slot_settings) and resubmit with the revision it reports.'
  return new Error(`${cause.message}. ${recovery}`)
}

export async function setValidatorSlotSettings(rawInput: unknown, actor: string) {
  const input = setValidatorSlotSettingsInputSchema.parse(rawInput)
  try {
    await platformAdminRequest(VALIDATOR_SLOT_SETTINGS_PATH, {
      method: 'POST',
      actor,
      body: {
        scope: input.scope,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
  } catch (cause) {
    throw validatorSlotRefusal(cause) ?? cause
  }
  // Re-read rather than returning the POST's revision row: the operator wants
  // the `effective` block, which is where hard_slot_ceiling, disk_restricted_slots
  // and the TTL live, and which is what the dispatch path will resolve.
  return fetchValidatorSlotSettings()
}

// The platform's existing public heartbeat view. It is the only place the two
// numbers a cap decision needs — advertised slots and reported disk headroom —
// are already published, so the console reads it rather than asking the platform
// for a new admin endpoint.
const VALIDATOR_FLEET_PATH = '/api/v1/public/validators'

// Advisory context, so failure is not an error. A stale or unreachable fleet
// read must never take down the page that carries the slot kill switch: the
// caller renders a blank fleet block and the cap controls stay usable.
export async function fetchValidatorFleet() {
  try {
    const payload = await platformAdminRequest(VALIDATOR_FLEET_PATH, {
      timeoutMs: 8_000,
      retries: 1,
    })
    return validatorFleetSchema.parse(payload)
  } catch {
    return null
  }
}

export async function fetchScreeningQuarantines(
  status: 'active' | 'resolved' | 'all',
  limit = 200,
  offset = 0,
  sort: 'oldest' | 'newest' = 'oldest',
) {
  const query = new URLSearchParams({
    status,
    sort,
    limit: String(limit),
    offset: String(offset),
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-quarantines?${query.toString()}`,
  )
  return screeningQuarantineListSchema.parse(payload)
}

export async function resolveScreeningQuarantine(
  rawInput: unknown,
  actor: string,
) {
  const input = resolveScreeningQuarantineInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-quarantines/${encodeURIComponent(input.quarantineId)}/resolve`,
    {
      method: 'POST',
      actor,
      body: { resolution: input.resolution, reason: input.reason },
    },
  )
  return resolveScreeningQuarantineResponseSchema.parse(payload)
}

function toPlatformBatchDecision(decision: {
  quarantineId: string
  expectedAgentId: string
  expectedArtifactSha256: string
  resolution: 'release' | 'rescreen' | 'reject'
  reason: string
}) {
  return {
    quarantine_id: decision.quarantineId,
    expected_agent_id: decision.expectedAgentId,
    expected_artifact_sha256: decision.expectedArtifactSha256,
    resolution: decision.resolution,
    reason: decision.reason,
  }
}

export async function fetchScreeningQuarantineContexts(rawInput: unknown) {
  const input = screeningQuarantineBatchContextInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    '/api/v1/admin/screening-quarantines/batch-context',
    {
      method: 'POST',
      body: { quarantine_ids: input.quarantineIds },
    },
  )
  return screeningQuarantineBatchContextResponseSchema.parse(payload)
}

export async function previewScreeningQuarantineBatch(rawInput: unknown, actor: string) {
  const input = screeningQuarantineBatchPreviewInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    '/api/v1/admin/screening-quarantines/batch-preview',
    {
      method: 'POST',
      actor,
      body: { decisions: input.decisions.map(toPlatformBatchDecision) },
    },
  )
  return screeningQuarantineBatchPreviewResponseSchema.parse(payload)
}

export async function executeScreeningQuarantineBatch(rawInput: unknown, actor: string) {
  const input = screeningQuarantineBatchExecuteInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    '/api/v1/admin/screening-quarantines/batch-resolve',
    {
      method: 'POST',
      actor,
      body: {
        decisions: input.decisions.map(toPlatformBatchDecision),
        preview_token: input.previewToken,
        confirmed: input.confirmed,
      },
    },
  )
  return screeningQuarantineBatchExecuteResponseSchema.parse(payload)
}

export async function fetchScreeningDisputes(
  status: 'pending' | 'resolved' | 'all',
  limit = 200,
  offset = 0,
) {
  const query = new URLSearchParams({
    status,
    limit: String(limit),
    offset: String(offset),
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-disputes?${query.toString()}`,
  )
  return screeningDisputeListSchema.parse(payload)
}

export async function resolveScreeningDispute(rawInput: unknown, actor: string) {
  const input = resolveScreeningDisputeInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-disputes/${encodeURIComponent(input.disputeId)}/resolve`,
    {
      method: 'POST',
      actor,
      body: { resolution: input.resolution, reason: input.reason },
    },
  )
  return resolveScreeningDisputeResponseSchema.parse(payload)
}

async function fetchCopyReviewCurrentComparison(agentId: string) {
  try {
    const payload = await platformAdminRequest(
      `/api/v1/admin/copy-reviews/${encodeURIComponent(agentId)}/current-comparison`,
    )
    return copyReviewCurrentComparisonSchema.parse(payload)
  } catch (cause) {
    return unavailableCopyReviewComparison(
      cause instanceof Error ? cause.message : 'Current comparison is unavailable',
    )
  }
}

// Assembling the console list fans out one current-comparison per pending
// row against the platform (and the subnet database behind it). Cache the
// assembled result per isolate with a short TTL and share concurrent builds,
// so operators refreshing or several open tabs cost one fan-out per minute
// instead of one per view. Resolutions invalidate immediately.
const COPY_REVIEWS_CACHE_TTL_MS = 60_000
const copyReviewsCache = new Map<CopyReviewGeneration, {
  promise: ReturnType<typeof buildCopyReviews>
  expiresAt: number
}>()

export function invalidateCopyReviewsCache() {
  copyReviewsCache.clear()
}

export function fetchCopyReviews(generation: CopyReviewGeneration = 'active') {
  const cached = copyReviewsCache.get(generation)
  if (cached && cached.expiresAt > Date.now()) {
    return cached.promise
  }
  const promise = buildCopyReviews(generation).catch((cause) => {
    copyReviewsCache.delete(generation)
    throw cause
  })
  copyReviewsCache.set(generation, {
    promise,
    expiresAt: Date.now() + COPY_REVIEWS_CACHE_TTL_MS,
  })
  return promise
}

async function buildCopyReviews(generation: CopyReviewGeneration) {
  const query = new URLSearchParams({
    status: 'pending',
    generation,
    limit: '200',
    offset: '0',
    // Platforms with #163 embed the comparison per row, making the whole
    // console list ONE platform request. Older platforms ignore the param
    // and return null comparisons, handled by the fan-out fallback below.
    include: 'current_comparison',
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews?${query.toString()}`,
  )
  const reviews = copyReviewListSchema.parse(payload)

  // Fallback for rows without an embedded comparison: bounded fan-out that
  // fails each comparison closed so one legacy row can never turn into a
  // page outage or an unsafe bulk-clear candidate.
  const items = []
  const concurrency = 6
  for (let index = 0; index < reviews.items.length; index += concurrency) {
    const chunk = reviews.items.slice(index, index + concurrency)
    const comparisons = await Promise.all(
      chunk.map((item) =>
        item.current_comparison
          ? Promise.resolve(item.current_comparison)
          : fetchCopyReviewCurrentComparison(item.agent_id),
      ),
    )
    items.push(
      ...chunk.map((item, chunkIndex) => ({
        ...item,
        current_comparison: comparisons[chunkIndex],
      })),
    )
  }

  return copyReviewConsoleListSchema.parse({
    ...reviews,
    items,
    bulk_eligible_count: items.filter((item) => item.current_comparison.bulk_eligible).length,
  })
}

export async function resolveCopyReview(rawInput: unknown, actor: string) {
  const input = resolveCopyReviewInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews/${encodeURIComponent(input.agentId)}/resolve`,
    {
      method: 'POST',
      actor,
      body: { resolution: input.resolution, reason: input.reason },
    },
  )
  invalidateCopyReviewsCache()
  return resolveCopyReviewResponseSchema.parse(payload)
}

export async function fetchAthReview(rawInput: unknown) {
  const input = getAthReviewInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews/${encodeURIComponent(input.agentId)}/audit`,
  )
  return athReviewAuditSchema.parse(payload)
}

export async function openAthReview(rawInput: unknown, actor: string) {
  const input = openAthReviewInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews/${encodeURIComponent(input.agentId)}/open`,
    {
      method: 'POST',
      actor,
      body: {
        expected_sha256: input.expectedSha256,
        expected_score_count: input.expectedScoreCount,
        reason: input.reason,
      },
    },
  )
  invalidateCopyReviewsCache()
  return openAthReviewResponseSchema.parse(payload)
}

export async function fetchScreeningSubmissions(limit = 200, offset = 0) {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions?${query.toString()}`,
  )
  return screeningSubmissionListSchema.parse(payload)
}

export async function fetchScreeningSubmission(rawInput: unknown) {
  const input = screeningSubmissionLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}`,
  )
  return screeningSubmissionSchema.parse(payload)
}

/**
 * Signed owner links for one miner hotkey.
 *
 * The link is symmetric — two hotkeys, both endpoints signed, no direction —
 * and each endpoint proves its half with either its own hotkey or the coldkey
 * bound to it by payment records. That makes it a stronger ownership signal
 * than the payment-coldkey inference a reviewer otherwise falls back on: a
 * shared coldkey says the same wallet paid, a signature says the key holder
 * signed. `evidence_grade` reports how much of the proof was hotkey-side, but
 * it is reviewer context and does not gate anything.
 *
 * It is also narrow: the platform uses the link to exempt near-duplicate
 * plagiarism screening between the two hotkeys' submissions, and for nothing
 * else — emission-slot allocation stays partitioned by payment-time coldkey.
 * Only direct links are reported; the relation is not transitive.
 *
 * Revoked links come back with the rest and are marked, because the question a
 * dispute turns on is whether the link was live when the submission under
 * review was made, not whether it is live now. An unknown hotkey answers with
 * empty lists rather than an error.
 *
 * This should later fold into `get_miner_owner_footprint` (in flight, not
 * merged) so a reviewer gets the proven link and the payment-record inference
 * from one call instead of correlating two.
 */
export async function fetchOwnerAttestations(rawInput: unknown) {
  const input = ownerAttestationLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/owner-attestations/${encodeURIComponent(input.hotkey)}`,
  )
  return ownerAttestationsSchema.parse(payload)
}

export async function rescreenRejectedSubmission(
  rawInput: unknown,
  actor: string,
) {
  const input = rescreenRejectedSubmissionInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/rescreen`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_score_count: input.expectedScoreCount,
      },
    },
  )
  return rescreenRejectedSubmissionResponseSchema.parse(payload)
}

export async function retryFailedScreeningNow(rawInput: unknown, actor: string) {
  const input = retryFailedScreeningNowInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/retry-now`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_score_count: input.expectedScoreCount,
        expected_attempt_id: input.expectedAttemptId,
      },
    },
  )
  return retryFailedScreeningNowResponseSchema.parse(payload)
}

export async function fetchScreeningArtifact(rawInput: unknown, actor: string) {
  const input = screeningArtifactInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/artifact`,
    { actor },
  )
  return screeningArtifactSchema.parse(payload)
}

export async function fetchValidatorAssignments() {
  const payload = await platformAdminRequest('/api/v1/admin/validator-assignments')
  return validatorAssignmentListSchema.parse(payload)
}

export async function releaseValidatorAssignment(rawInput: unknown, actor: string) {
  const input = releaseValidatorAssignmentInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/validator-assignments/${encodeURIComponent(input.agentId)}/${encodeURIComponent(input.validatorHotkey)}/release`,
    {
      method: 'POST',
      actor,
      body: {
        expected_deadline: input.expectedDeadline,
        reason: input.reason,
      },
    },
  )
  return releaseValidatorAssignmentResponseSchema.parse(payload)
}

export async function fetchValidationRetry(rawInput: unknown) {
  const input = validationRetryLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}`,
  )
  return validationRetryDetailSchema.parse(payload)
}

export async function retryValidation(rawInput: unknown, actor: string) {
  const input = retryValidationInputSchema.parse(rawInput)
  const requestId = await deriveRequestId('validation-retry', [
    input.agentId,
    actor,
    input.reason,
    input.expectedSnapshot,
  ])
  type RetryRequest = PlatformOperations['retry_validation_after_infrastructure_failure_api_v1_admin_validation_retries__agent_id__retry_post']['requestBody']['content']['application/json']
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/retry`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        reason: input.reason,
      } satisfies RetryRequest,
    },
  )
  return retryValidationResponseSchema.parse(payload)
}

export async function withdrawValidation(rawInput: unknown, actor: string) {
  const input = withdrawValidationInputSchema.parse(rawInput)
  const requestId = await deriveRequestId('validation-withdraw', [
    input.agentId,
    actor,
    input.reason,
    input.expectedSnapshot,
  ])
  type WithdrawRequest = PlatformOperations['withdraw_failed_validation_from_queue_api_v1_admin_validation_retries__agent_id__withdraw_post']['requestBody']['content']['application/json']
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/withdraw`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        reason: input.reason,
        confirmation: input.confirmation,
      } satisfies WithdrawRequest,
    },
  )
  return withdrawValidationResponseSchema.parse(payload)
}

export async function evictValidation(rawInput: unknown, actor: string) {
  const input = evictValidationInputSchema.parse(rawInput)
  // A distinct namespace from 'validation-withdraw' on purpose. The platform
  // stores both routes' request ids as the primary key of one shared table, so
  // an eviction deriving the withdrawal's key would be answered as a replay of
  // a different action. Distinct namespaces keep replay meaning exactly what it
  // says: the same operator re-issuing the same eviction against the same state.
  const requestId = await deriveRequestId('validation-evict', [
    input.agentId,
    actor,
    input.reason,
    input.expectedSnapshot,
  ])
  type EvictRequest = PlatformOperations['evict_submission_from_validator_queue_api_v1_admin_validation_retries__agent_id__evict_post']['requestBody']['content']['application/json']
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/evict`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        reason: input.reason,
        confirmation: input.confirmation,
      } satisfies EvictRequest,
    },
  )
  return evictValidationResponseSchema.parse(payload)
}

export async function reinstateValidation(rawInput: unknown, actor: string) {
  const input = reinstateValidationInputSchema.parse(rawInput)
  // Its own namespace, for the same reason 'validation-evict' is not
  // 'validation-withdraw' — and here the stakes are the opposite direction. A
  // reinstatement that derived the eviction's key would collide with the very
  // action it reverses, so re-sending a reversal could be answered as a replay
  // of the eviction. Distinct namespaces keep 'idempotent' meaning the same
  // operator re-issuing the same reinstatement against the same state.
  const requestId = await deriveRequestId('validation-reinstate', [
    input.agentId,
    actor,
    input.reason,
    input.expectedSnapshot,
  ])
  type ReinstateRequest = PlatformOperations['reinstate_removed_submission_to_validator_queue_api_v1_admin_validation_retries__agent_id__reinstate_post']['requestBody']['content']['application/json']
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/reinstate`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        reason: input.reason,
        confirmation: input.confirmation,
      } satisfies ReinstateRequest,
    },
  )
  return reinstateValidationResponseSchema.parse(payload)
}

export async function fetchStuckSubmissions(rawInput: unknown) {
  const input = listStuckSubmissionsInputSchema.parse(rawInput)
  const query = new URLSearchParams()
  for (const state of input.state ?? []) {
    query.append('state', state)
  }
  const suffix = query.toString() ? `?${query}` : ''
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries${suffix}`,
  )
  return stuckSubmissionsListSchema.parse(payload)
}

export async function fetchLeaseRevocations(rawInput: unknown) {
  const input = listLeaseRevocationsInputSchema.parse(rawInput)
  const query = new URLSearchParams()
  if (input.agentId) query.set('agent_id', input.agentId)
  if (input.validatorHotkey) query.set('validator_hotkey', input.validatorHotkey)
  // `action` and `context` are repeated query parameters on the platform side
  // (`Annotated[list[str] | None, Query()]`), so they are appended, not set.
  for (const action of input.action ?? []) query.append('action', action)
  for (const context of input.context ?? []) query.append('context', context)
  if (input.since) query.set('since', input.since)
  query.set('limit', String(input.limit))
  query.set('offset', String(input.offset))
  const payload = await platformAdminRequest(
    `/api/v1/admin/lease-revocations?${query}`,
  )
  return leaseRevocationsListSchema.parse(payload)
}

export async function batchRetryValidation(rawInput: unknown, actor: string) {
  const input = batchRetryValidationInputSchema.parse(rawInput)
  // Same derivation as the single retry, so retrying one agent through either
  // tool with the same reason and snapshot is one request, not two.
  const items = await Promise.all(
    input.items.map(async (item) => ({
      agent_id: item.agentId,
      request_id: await deriveRequestId('validation-retry', [
        item.agentId,
        actor,
        input.reason,
        item.expectedSnapshot,
      ]),
      expected_snapshot: item.expectedSnapshot,
    })),
  )
  const payload = await platformAdminRequest(
    '/api/v1/admin/validation-retries/batch-retry',
    {
      method: 'POST',
      actor,
      body: { reason: input.reason, items },
    },
  )
  return batchRetryValidationResponseSchema.parse(payload)
}

export async function fetchAgentScoringReadiness(rawInput: unknown) {
  const input = agentScoringReadinessInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/agents/${encodeURIComponent(input.agentId)}/scoring-readiness`,
  )
  return agentScoringReadinessSchema.parse(payload)
}

export async function fetchValidatorScoreReplacement(rawInput: unknown) {
  const input = validatorScoreReplacementLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/validators/${encodeURIComponent(input.validatorHotkey)}`,
  )
  return validatorScoreReplacementDetailSchema.parse(payload)
}

export async function replaceValidatorScore(rawInput: unknown, actor: string) {
  const input = replaceValidatorScoreInputSchema.parse(rawInput)
  const requestId = await deriveRequestId('score-replacement', [
    input.agentId,
    input.validatorHotkey,
    actor,
    input.reason,
    input.expectedSnapshot,
    input.expectedRunId,
  ])
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/validators/${encodeURIComponent(input.validatorHotkey)}/replace-score`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        expected_run_id: input.expectedRunId,
        reason: input.reason,
      },
    },
  )
  return replaceValidatorScoreResponseSchema.parse(payload)
}

export async function fetchScoreOutliers(rawInput: unknown) {
  const input = scoreOutlierFiltersSchema.parse(rawInput)
  const query = new URLSearchParams({
    limit: String(input.limit),
    offset: String(input.offset),
  })
  const payload = await platformAdminRequest(`/api/v1/admin/score-outliers?${query}`)
  return scoreOutlierListSchema.parse(payload)
}

export async function queueValidatorScoreRetests(rawInput: unknown, actor: string) {
  const input = queueValidatorScoreRetestsInputSchema.parse(rawInput)
  const items = await Promise.all(
    input.items.map(async (item) => ({
      agent_id: item.agentId,
      request_id: await deriveRequestId('score-replacement', [
        item.agentId,
        input.validatorHotkey,
        actor,
        input.reason,
        item.expectedSnapshot,
        item.expectedRunId,
      ]),
      expected_snapshot: item.expectedSnapshot,
      expected_run_id: item.expectedRunId,
    })),
  )
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/validators/${encodeURIComponent(input.validatorHotkey)}/queue-score-retests`,
    {
      method: 'POST',
      actor,
      body: { reason: input.reason, items },
    },
  )
  return queueValidatorScoreRetestsResponseSchema.parse(payload)
}

export async function releaseValidatorScoreRetest(rawInput: unknown, actor: string) {
  const input = releaseValidatorScoreRetestInputSchema.parse(rawInput)
  const requestId = await deriveRequestId('score-retest-release', [
    input.agentId,
    input.validatorHotkey,
    actor,
    input.reason,
    input.expectedSnapshot,
    input.expectedDeadline,
  ])
  const payload = await platformAdminRequest(
    `/api/v1/admin/validation-retries/${encodeURIComponent(input.agentId)}/validators/${encodeURIComponent(input.validatorHotkey)}/release-ticket`,
    {
      method: 'POST',
      actor,
      body: {
        request_id: requestId,
        expected_snapshot: input.expectedSnapshot,
        expected_deadline: input.expectedDeadline,
        reason: input.reason,
      },
    },
  )
  return releaseValidatorScoreRetestResponseSchema.parse(payload)
}

export async function fetchBenchmarkContractRefresh(rawInput: unknown) {
  const input = benchmarkContractRefreshLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/refresh-benchmark-contract`,
  )
  return benchmarkContractRefreshDetailSchema.parse(payload)
}

export async function refreshBenchmarkContract(rawInput: unknown, actor: string) {
  const input = refreshBenchmarkContractInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/refresh-benchmark-contract`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_bench_version: input.expectedBenchVersion,
        expected_dataset_sha256: input.expectedDatasetSha256,
        expected_score_count: input.expectedScoreCount,
      },
    },
  )
  return refreshBenchmarkContractResponseSchema.parse(payload)
}

export async function fetchScreenedImageRebuild(rawInput: unknown) {
  const input = screenedImageRebuildLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/rebuild-screened-image`,
  )
  return screenedImageRebuildDetailSchema.parse(payload)
}

export async function rebuildScreenedImage(rawInput: unknown, actor: string) {
  const input = rebuildScreenedImageInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/rebuild-screened-image`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_bench_version: input.expectedBenchVersion,
        expected_score_count: input.expectedScoreCount,
        expected_image_sha256: input.expectedImageSha256,
        expected_image_upload_id: input.expectedImageUploadId,
      },
    },
  )
  return rebuildScreenedImageResponseSchema.parse(payload)
}

export async function fetchBenchmarkContractMigration(rawInput: unknown) {
  const input = benchmarkContractMigrationLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/migrate-benchmark-contract`,
  )
  return benchmarkContractMigrationDetailSchema.parse(payload)
}

export async function migrateBenchmarkContract(rawInput: unknown, actor: string) {
  const input = migrateBenchmarkContractInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/migrate-benchmark-contract`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_source_bench_version: 2,
        expected_target_bench_version: 3,
        expected_source_dataset_sha256: input.expectedSourceDatasetSha256,
        expected_source_score_count: 0,
        expected_target_score_count: 0,
      },
    },
  )
  return migrateBenchmarkContractResponseSchema.parse(payload)
}

export async function fetchBenchmarkRolloutQualification(rawInput: unknown) {
  const input = benchmarkRolloutQualificationLookupInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/qualify-benchmark-rollout`,
  )
  return benchmarkRolloutQualificationDetailSchema.parse(payload)
}

export async function qualifyBenchmarkRollout(rawInput: unknown, actor: string) {
  const input = qualifyBenchmarkRolloutInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/qualify-benchmark-rollout`,
    {
      method: 'POST',
      actor,
      body: {
        reason: input.reason,
        expected_sha256: input.expectedSha256,
        expected_rollout_id: input.expectedRolloutId,
        expected_total_score_count: input.expectedTotalScoreCount,
        expected_source_score_count: input.expectedSourceScoreCount,
        expected_target_score_count: input.expectedTargetScoreCount,
      },
    },
  )
  return qualifyBenchmarkRolloutResponseSchema.parse(payload)
}

export async function fetchScreeningQuarantineContext(rawInput: unknown) {
  const input = quarantineContextInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-quarantines/${encodeURIComponent(input.quarantineId)}/context`,
  )
  return screeningQuarantineContextSchema.parse(payload)
}

export async function fetchQuarantineSourceFiles(rawInput: unknown, actor: string) {
  const input = sourceListingInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/source-files`,
    { actor },
  )
  return sourceListingSchema.parse(payload)
}

export async function fetchQuarantineSourceExcerpt(rawInput: unknown, actor: string) {
  const input = sourceExcerptInputSchema.parse(rawInput)
  const query = new URLSearchParams({
    path: input.path,
    start_line: String(input.startLine),
    end_line: String(input.endLine),
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/source-file?${query.toString()}`,
    { actor },
  )
  return sourceExcerptSchema.parse(payload)
}

export async function fetchCopyReviewSourceDiff(rawInput: unknown, actor: string) {
  const input = sourceDiffInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews/${encodeURIComponent(input.agentId)}/source-diff`,
    { actor },
  )
  return sourceDiffManifestSchema.parse(payload)
}

export async function fetchCopyReviewSourceDiffFile(rawInput: unknown, actor: string) {
  const input = sourceDiffFileInputSchema.parse(rawInput)
  const query = new URLSearchParams({ path: input.path })
  const payload = await platformAdminRequest(
    `/api/v1/admin/copy-reviews/${encodeURIComponent(input.agentId)}/source-diff/file?${query.toString()}`,
    { actor },
  )
  return sourceDiffFileDetailSchema.parse(payload)
}

// --- Production score reads (public score ledger) -------------------------
//
// The platform's score ledger is published, credential-free, at
// /api/v1/public/*: the exact rows validators fold into weights and the
// dittobench.ai dashboard renders. Backroom reads it through
// platformPublicRequest (never the admin token) so these operations are
// read-only by construction.

async function fetchLeaderboardSnapshot(benchVersion?: number) {
  const suffix = benchVersion === undefined ? '' : `?bench_version=${benchVersion}`
  const payload = await platformPublicRequest(`/api/v1/public/leaderboard${suffix}`)
  return publicLeaderboardSchema.parse(payload)
}

type PublicLeaderboard = ReturnType<typeof publicLeaderboardSchema.parse>

function resolveBoardAgent(
  board: PublicLeaderboard,
  input: { agentId?: string; minerHotkey?: string },
) {
  if (input.agentId) {
    return {
      agentId: input.agentId,
      entry: board.entries.find((entry) => entry.agent_id === input.agentId) ?? null,
    }
  }
  const entry = board.entries.find((candidate) => candidate.miner_hotkey === input.minerHotkey)
  if (!entry) {
    throw new Error(
      `No leaderboard submission found for miner hotkey ${input.minerHotkey}. ` +
        'Pass the agent UUID to read scores for a submission that is not the ' +
        "miner's current leaderboard row.",
    )
  }
  return { agentId: entry.agent_id, entry }
}

async function fetchPublicAgentScores(agentId: string) {
  const payload = await platformPublicRequest(
    `/api/v1/public/agent/${encodeURIComponent(agentId)}/scores`,
  )
  return publicAgentScoresSchema.parse(payload)
}

/**
 * The settled k=3 record, or null when the submission has not settled into one.
 *
 * The platform serves this endpoint only for a submission in a public status
 * (`scored` / `live`) and 404s everything else: still evaluating, below quorum,
 * or held for copy review. That 404 is a *state*, not an absence, so it is
 * returned as null and answered from the pre-quorum surface instead of being
 * re-raised as "no public scores for this agent" — a message an operator cannot
 * tell apart from a bad agent id.
 */
async function fetchSettledAgentScores(agentId: string) {
  try {
    return await fetchPublicAgentScores(agentId)
  } catch (error) {
    if (isPlatformPublicNotFound(error)) return null
    throw error
  }
}

/**
 * The in-progress view of a submission, from the same public ledger.
 *
 * This endpoint is keyed by agent id alone and exists for every submission the
 * platform has ever accepted, so its own 404 is the real "no such submission".
 */
async function fetchPublicSubmissionPipeline(agentId: string) {
  try {
    const payload = await platformPublicRequest(
      `/api/v1/public/agent/${encodeURIComponent(agentId)}/pipeline`,
    )
    return publicSubmissionPipelineSchema.parse(payload)
  } catch (error) {
    if (isPlatformPublicNotFound(error)) {
      throw new Error(
        `No submission exists with agent id ${agentId}. The platform holds ` +
          'neither a settled score record nor a scoring pipeline for it.',
      )
    }
    throw error
  }
}

export async function fetchScoreLeaderboard(rawInput: unknown) {
  const input = scoreLeaderboardInputSchema.parse(rawInput)
  const board = await fetchLeaderboardSnapshot(input.benchVersion)
  const filtered = board.entries.filter((entry) =>
    input.status === 'all' ? true : input.status === 'finalized' ? entry.finalized : !entry.finalized,
  )
  return scoreLeaderboardPageSchema.parse({
    generated_at: board.generated_at,
    current_bench_version: board.current_bench_version,
    active_bench_version: board.active_bench_version,
    desired_bench_version: board.desired_bench_version,
    available_bench_versions: board.available_bench_versions,
    selection_mode: board.selection_mode,
    status: input.status,
    count: filtered.length,
    limit: input.limit,
    offset: input.offset,
    entries: filtered.slice(input.offset, input.offset + input.limit),
    emissions: board.emissions ?? null,
  })
}

/**
 * The scoring record of a submission that has not reached quorum.
 *
 * Built from the platform's own pre-quorum surface so Backroom invents no
 * third semantics: the accepted scores are exactly the rows the platform
 * publishes for a below-quorum submission, and the aggregate stays null for
 * exactly as long as the platform keeps it null. `finalized: false` is the same
 * word a leaderboard entry uses for the same state.
 *
 * Every dataset-pin field stays null on purpose. The pin is published with the
 * settled record; each accepted row below carries the exact seed it was graded
 * against, and deriving a submission-level pin from those rows would be
 * Backroom asserting something the ledger has not.
 */
function provisionalAgentScores({
  pipeline,
  board,
  entry,
}: {
  pipeline: PublicSubmissionPipeline
  board: PublicLeaderboard
  entry: PublicLeaderboardEntry | null
}) {
  return agentScoresDetailSchema.parse({
    agent_id: pipeline.agent_id,
    miner_hotkey: entry?.miner_hotkey ?? null,
    status: pipeline.status,
    finalized: false,
    quorum: pipeline.quorum,
    score_count: pipeline.score_count,
    median_composite: pipeline.final_composite ?? null,
    dataset_seed: null,
    dataset_sha256: null,
    dataset_run_size: null,
    dataset_seed_block: null,
    dataset_seed_block_hash: null,
    scores: pipeline.provisional_scores.map((score) => ({
      // Withheld before quorum by the platform, not missing here. See
      // agentScoreRowSchema.
      validator_hotkey: null,
      run_id: null,
      tool_mean: null,
      memory_mean: null,
      median_ms: null,
      n: null,
      composite: score.composite,
      raw_composite: score.raw_composite ?? null,
      composite_breakdown: score.composite_breakdown ?? null,
      seed: score.seed,
      bench_version: score.bench_version ?? null,
      generated_at: score.accepted_at,
      transcript_sha256: score.transcript_sha256 ?? null,
    })),
    generated_at: pipeline.generated_at,
    active_bench_version: board.active_bench_version,
    desired_bench_version: board.desired_bench_version,
    leaderboard: entry,
  })
}

export async function fetchAgentScores(rawInput: unknown) {
  const input = agentScoresLookupInputSchema.parse(rawInput)
  // The authoritative board also resolves hotkeys and carries the agent's
  // current rank/eligibility context, so one snapshot serves both purposes.
  const board = await fetchLeaderboardSnapshot()
  const { agentId, entry } = resolveBoardAgent(board, input)
  const scores = await fetchSettledAgentScores(agentId)
  if (scores === null) {
    return provisionalAgentScores({
      pipeline: await fetchPublicSubmissionPipeline(agentId),
      board,
      entry,
    })
  }
  return agentScoresDetailSchema.parse({
    ...scores,
    finalized: true,
    active_bench_version: board.active_bench_version,
    desired_bench_version: board.desired_bench_version,
    leaderboard: entry,
  })
}

/**
 * Resolve one hotkey or coldkey to the miner footprint its payment records
 * imply, with each linked hotkey's current leaderboard standing.
 *
 * Two sources, deliberately kept apart. The linkage comes from the platform's
 * admin endpoint over `evaluation_payments` — moderation metadata that the
 * platform never publishes on the public scoring wire, and Backroom does not
 * change that. The standings come from the same credential-free public ledger
 * every other score tool reads, joined here by hotkey. Composing in Backroom
 * keeps the admin endpoint off the chain-cached metagraph and keeps coldkeys
 * out of any public response.
 */
export async function fetchOwnerFootprint(rawInput: unknown) {
  const input = ownerFootprintLookupInputSchema.parse(rawInput)
  const query = new URLSearchParams({
    depth: String(input.depth),
    agents_per_hotkey: String(input.agentsPerHotkey),
  })
  const payload = await platformAdminRequest(
    `/api/v1/admin/miner-owners/${encodeURIComponent(input.key)}?${query.toString()}`,
  )
  const footprint = ownerFootprintSchema.parse(payload)

  // One board snapshot serves every linked hotkey; the leaderboard is already
  // one best submission per miner, so a hotkey has at most one row.
  const board = await fetchLeaderboardSnapshot()
  const standings = new Map(
    board.entries.map((entry) => [entry.miner_hotkey, entry] as const),
  )
  const hotkeys = footprint.hotkeys.map((hotkey) => ({
    ...hotkey,
    leaderboard: standings.get(hotkey.miner_hotkey) ?? null,
  }))

  return ownerFootprintDetailSchema.parse({
    ...footprint,
    hotkeys,
    active_bench_version: board.active_bench_version,
    desired_bench_version: board.desired_bench_version,
    leaderboard_generated_at: board.generated_at,
    ranked_hotkey_count: hotkeys.filter((hotkey) => hotkey.leaderboard !== null)
      .length,
  })
}

function median(values: Array<number>) {
  const sorted = [...values].sort((left, right) => left - right)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 1
    ? sorted[middle]!
    : (sorted[middle - 1]! + sorted[middle]!) / 2
}

export async function fetchAgentScoreHistory(rawInput: unknown) {
  const input = agentScoresLookupInputSchema.parse(rawInput)
  let agentId = input.agentId
  if (!agentId) {
    agentId = resolveBoardAgent(await fetchLeaderboardSnapshot(), input).agentId
  }
  const settled = await fetchSettledAgentScores(agentId)
  if (settled === null) {
    // Version-over-version deltas need settled medians, and the pre-quorum
    // surface publishes neither a bench-version median nor the validator
    // identities this groups by. Say which of the two states this is instead of
    // re-raising a 404 that reads the same for a bad agent id.
    const pipeline = await fetchPublicSubmissionPipeline(agentId)
    throw new Error(
      `Agent ${agentId} has no settled score history: it is '${pipeline.status}' ` +
        `with ${pipeline.score_count} of ${pipeline.quorum} accepted scores on ` +
        `bench version ${pipeline.score_bench_version}. Call get_agent_scores ` +
        'for its provisional scores.',
    )
  }
  const record = settled

  type ScoreRow = (typeof record.scores)[number]
  const groups = new Map<number | null, Array<ScoreRow>>()
  for (const score of record.scores) {
    const rows = groups.get(score.bench_version) ?? []
    rows.push(score)
    groups.set(score.bench_version, rows)
  }
  // Legacy (null-version) scores first, then ascending bench versions, so
  // composite_delta_vs_previous reads as version-over-version movement.
  const orderedVersions = [...groups.keys()].sort((left, right) => {
    if (left === null) return -1
    if (right === null) return 1
    return left - right
  })

  let previousMedian: number | null = null
  const versions = orderedVersions.map((benchVersion) => {
    const rows = groups.get(benchVersion)!
    const composites = rows.map((row) => row.composite)
    const medianComposite = median(composites)
    const generatedAt = rows.map((row) => row.generated_at).sort()
    const version = {
      bench_version: benchVersion,
      score_count: rows.length,
      median_composite: medianComposite,
      min_composite: Math.min(...composites),
      max_composite: Math.max(...composites),
      median_tool_mean: median(rows.map((row) => row.tool_mean)),
      median_memory_mean: median(rows.map((row) => row.memory_mean)),
      first_scored_at: generatedAt[0]!,
      last_scored_at: generatedAt[generatedAt.length - 1]!,
      validators: rows.map((row) => row.validator_hotkey),
      seeds: [...new Set(rows.map((row) => row.seed))],
      composite_delta_vs_previous:
        previousMedian === null ? null : medianComposite - previousMedian,
    }
    previousMedian = medianComposite
    return version
  })

  return agentScoreHistorySchema.parse({
    agent_id: record.agent_id,
    miner_hotkey: record.miner_hotkey,
    status: record.status,
    quorum: record.quorum,
    total_score_count: record.score_count,
    versions,
    generated_at: record.generated_at,
  })
}

export async function fetchQuarantineBaselineDiff(rawInput: unknown, actor: string) {
  const input = baselineDiffInputSchema.parse(rawInput)
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/baseline-diff`,
    { actor },
  )
  return baselineDiffManifestSchema.parse(payload)
}

export async function fetchQuarantineBaselineDiffFile(rawInput: unknown, actor: string) {
  const input = baselineDiffFileInputSchema.parse(rawInput)
  const query = new URLSearchParams({ path: input.path })
  const payload = await platformAdminRequest(
    `/api/v1/admin/screening-submissions/${encodeURIComponent(input.agentId)}/baseline-diff/file?${query.toString()}`,
    { actor },
  )
  return baselineDiffFileDetailSchema.parse(payload)
}
