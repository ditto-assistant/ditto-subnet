import { createServerFn } from '@tanstack/react-start'
import { setResponseHeader } from '@tanstack/react-start/server'
import { z } from 'zod'
import {
  startBenchmarkRolloutInputSchema,
  supersedeBenchmarkRolloutInputSchema,
  selectActiveBenchmarkInputSchema,
  quarantineContextInputSchema,
  ownerAttestationLookupInputSchema,
  openAthReviewInputSchema,
  listCopyReviewsInputSchema,
  releaseValidatorAssignmentInputSchema,
  retryValidationInputSchema,
  reinstateValidationInputSchema,
  withdrawValidationInputSchema,
  benchmarkContractRefreshLookupInputSchema,
  refreshBenchmarkContractInputSchema,
  screenedImageRebuildLookupInputSchema,
  rebuildScreenedImageInputSchema,
  benchmarkContractMigrationLookupInputSchema,
  migrateBenchmarkContractInputSchema,
  validationRetryLookupInputSchema,
  resolveCopyReviewInputSchema,
  resolveScreeningQuarantineInputSchema,
  resolveScreeningDisputeInputSchema,
  screeningQuarantineBatchContextInputSchema,
  screeningQuarantineBatchExecuteInputSchema,
  screeningQuarantineBatchPreviewInputSchema,
  screeningArtifactInputSchema,
  baselineDiffFileInputSchema,
  baselineDiffInputSchema,
  sourceDiffFileInputSchema,
  sourceDiffInputSchema,
  sourceExcerptInputSchema,
  sourceListingInputSchema,
  scoreOutlierFiltersSchema,
  queueValidatorScoreRetestsInputSchema,
  replaceValidatorScoreInputSchema,
  releaseValidatorScoreRetestInputSchema,
  applyScreenerReviewSettingsInputSchema,
  updateArtifactReleaseSettingsInputSchema,
  updateSubmissionSettingsInputSchema,
  setContinualRetestSettingsInputSchema,
  setQueuePolicySettingsInputSchema,
  setInferenceConcurrencySettingsInputSchema,
  setValidatorSlotSettingsInputSchema,
  inferenceRouteCalibrationInputSchema,
  inferenceRoutingPolicyInputSchema,
} from '../lib/admin.schemas'
import {
  startBenchmarkRollout as startBenchmarkRolloutService,
  supersedeBenchmarkRollout as supersedeBenchmarkRolloutService,
  selectActiveBenchmark as selectActiveBenchmarkService,
  fetchBenchmarkRolloutControl,
  fetchCopyReviews,
  fetchCopyReviewSourceDiff,
  fetchCopyReviewSourceDiffFile,
  fetchMinerFeeSummary,
  fetchQuarantineBaselineDiff,
  fetchQuarantineBaselineDiffFile,
  fetchQuarantineSourceExcerpt,
  fetchQuarantineSourceFiles,
  fetchOwnerAttestations,
  fetchScreeningQuarantineContext,
  fetchScreeningQuarantineContexts,
  fetchScreeningQuarantines,
  fetchScreeningDisputes,
  fetchScreeningSubmissions,
  fetchScreeningArtifact,
  fetchValidatorAssignments,
  executeScreeningQuarantineBatch,
  openAthReview as openAthReviewService,
  previewScreeningQuarantineBatch,
  resolveCopyReview,
  resolveScreeningQuarantine,
  resolveScreeningDispute,
  releaseValidatorAssignment,
  fetchValidationRetry,
  retryValidation,
  reinstateValidation,
  withdrawValidation,
  fetchBenchmarkContractRefresh,
  refreshBenchmarkContract,
  fetchScreenedImageRebuild,
  rebuildScreenedImage as rebuildScreenedImageService,
  fetchBenchmarkContractMigration,
  migrateBenchmarkContract,
  fetchScoreOutliers,
  queueValidatorScoreRetests as queueValidatorScoreRetestsService,
  replaceValidatorScore,
  releaseValidatorScoreRetest,
  fetchScreenerReviewControl,
  fetchScreenerCapacity,
  applyScreenerReviewSettings as applyScreenerReviewSettingsService,
  fetchArtifactReleaseControl,
  fetchInferenceRoutes,
  calibrateInferenceRoute as calibrateInferenceRouteService,
  updateInferenceRoutingPolicy as updateInferenceRoutingPolicyService,
  updateArtifactReleaseSettings as updateArtifactReleaseSettingsService,
  fetchSubmissionSettingsControl,
  updateSubmissionSettings as updateSubmissionSettingsService,
  fetchContinualRetestSettings,
  setContinualRetestSettings as setContinualRetestSettingsService,
  fetchQueuePolicySettings,
  setQueuePolicySettings as setQueuePolicySettingsService,
  fetchInferenceConcurrencySettings,
  fetchValidatorSlotSettings,
  fetchValidatorFleet,
  setInferenceConcurrencySettings as setInferenceConcurrencySettingsService,
  setValidatorSlotSettings as setValidatorSlotSettingsService,
} from './admin.service'
import { authMiddleware, sameOriginMiddleware, writeAuthMiddleware } from './auth.functions'

export const getBenchmarkRolloutControl = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchBenchmarkRolloutControl()
  })

export const getMinerFeeSummary = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchMinerFeeSummary()
  })

export const listInferenceRoutes = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchInferenceRoutes()
  })

export const calibrateInferenceRoute = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(inferenceRouteCalibrationInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return calibrateInferenceRouteService(context.session.email, data)
  })

export const updateInferenceRoutingPolicy = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(inferenceRoutingPolicyInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return updateInferenceRoutingPolicyService(context.session.email, data)
  })

export const startBenchmarkRollout = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(startBenchmarkRolloutInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return startBenchmarkRolloutService(context.session.email, data)
  })

export const supersedeBenchmarkRollout = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(supersedeBenchmarkRolloutInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return supersedeBenchmarkRolloutService(context.session.email, data)
  })

export const selectActiveBenchmark = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(selectActiveBenchmarkInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return selectActiveBenchmarkService(context.session.email, data)
  })

export const getScreenerReviewControl = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreenerReviewControl()
  })

export const getScreenerCapacity = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreenerCapacity()
  })

export const getArtifactReleaseControl = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchArtifactReleaseControl()
  })

export const setArtifactReleaseSettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(updateArtifactReleaseSettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return updateArtifactReleaseSettingsService(context.session.email, data)
  })

export const getSubmissionSettingsControl = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchSubmissionSettingsControl()
  })

export const setSubmissionSettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(updateSubmissionSettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return updateSubmissionSettingsService(context.session.email, data)
  })

export const getContinualRetestSettings = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchContinualRetestSettings()
  })

export const updateContinualRetestSettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(setContinualRetestSettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return setContinualRetestSettingsService(data, context.session.email)
  })

export const getValidatorSlotSettings = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchValidatorSlotSettings()
  })

// Advisory fleet context for the slot page. Separate from the settings read so a
// slow or unavailable heartbeat view can never delay or break the control that
// caps dispatch; the service already resolves it to null instead of throwing.
export const getValidatorFleet = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchValidatorFleet()
  })

export const updateValidatorSlotSettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(setValidatorSlotSettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return setValidatorSlotSettingsService(data, context.session.email)
  })

export const getInferenceConcurrencySettings = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchInferenceConcurrencySettings()
  })

export const updateInferenceConcurrencySettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(setInferenceConcurrencySettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return setInferenceConcurrencySettingsService(data, context.session.email)
  })

export const updateScreenerReviewSettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(applyScreenerReviewSettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return applyScreenerReviewSettingsService(context.session.email, data)
  })

export const getQueuePolicyControl = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchQueuePolicySettings()
  })

export const updateQueuePolicySettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(setQueuePolicySettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return setQueuePolicySettingsService(data, context.session.email)
  })

export const listScreeningQuarantines = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(
    z.object({
      status: z.enum(['active', 'resolved', 'all']),
      sort: z.enum(['oldest', 'newest']).default('oldest'),
    }),
  )
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningQuarantines(data.status, 200, 0, data.sort)
  })

export const decideScreeningQuarantine = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(resolveScreeningQuarantineInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return resolveScreeningQuarantine(data, context.session.email)
  })

export const getScreeningQuarantineContexts = createServerFn({ method: 'POST' })
  .middleware([authMiddleware, sameOriginMiddleware])
  .validator(screeningQuarantineBatchContextInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningQuarantineContexts(data)
  })

export const previewScreeningQuarantineDecisions = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(screeningQuarantineBatchPreviewInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return previewScreeningQuarantineBatch(data, context.session.email)
  })

export const executeScreeningQuarantineDecisions = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(screeningQuarantineBatchExecuteInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return executeScreeningQuarantineBatch(data, context.session.email)
  })

export const listScreeningDisputes = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(
    z.object({
      status: z.enum(['pending', 'resolved', 'all']),
      limit: z.number().int().min(1).max(200).default(50),
      offset: z.number().int().min(0).default(0),
    }),
  )
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningDisputes(data.status, data.limit, data.offset)
  })

export const decideScreeningDispute = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(resolveScreeningDisputeInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return resolveScreeningDispute(data, context.session.email)
  })

export const listCopyReviews = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(listCopyReviewsInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchCopyReviews(data.generation)
  })

export const decideCopyReview = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(resolveCopyReviewInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return resolveCopyReview(data, context.session.email)
  })

export const openAthReview = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(openAthReviewInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return openAthReviewService(data, context.session.email)
  })

export const listScreeningSubmissions = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(
    z.object({
      limit: z.number().int().min(1).max(200).default(50),
      offset: z.number().int().min(0).default(0),
    }),
  )
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningSubmissions(data.limit, data.offset)
  })

export const getScreeningArtifact = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(screeningArtifactInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningArtifact(data, context.session.email)
  })

export const listValidatorAssignments = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchValidatorAssignments()
  })

export const releaseActiveValidatorAssignment = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(releaseValidatorAssignmentInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return releaseValidatorAssignment(data, context.session.email)
  })

export const getValidationRetry = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(validationRetryLookupInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchValidationRetry(data)
  })

export const retryValidationAfterInfrastructureFailure = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(retryValidationInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return retryValidation(data, context.session.email)
  })

export const withdrawFailedValidationFromQueue = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(withdrawValidationInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return withdrawValidation(data, context.session.email)
  })

export const reinstateRemovedValidationInQueue = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(reinstateValidationInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return reinstateValidation(data, context.session.email)
  })

export const listScoreOutliers = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(scoreOutlierFiltersSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScoreOutliers(data)
  })

export const requestScoreRetest = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(replaceValidatorScoreInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return replaceValidatorScore(data, context.session.email)
  })

export const queueValidatorScoreRetests = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(queueValidatorScoreRetestsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return queueValidatorScoreRetestsService(data, context.session.email)
  })

export const releaseScoreRetestTicket = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(releaseValidatorScoreRetestInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return releaseValidatorScoreRetest(data, context.session.email)
  })

export const getBenchmarkContractRefresh = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(benchmarkContractRefreshLookupInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchBenchmarkContractRefresh(data)
  })

export const rebuildBenchmarkContract = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(refreshBenchmarkContractInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return refreshBenchmarkContract(data, context.session.email)
  })

export const getScreenedImageRebuild = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(screenedImageRebuildLookupInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreenedImageRebuild(data)
  })

export const rebuildScreenedImage = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(rebuildScreenedImageInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return rebuildScreenedImageService(data, context.session.email)
  })

export const getBenchmarkContractMigration = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(benchmarkContractMigrationLookupInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchBenchmarkContractMigration(data)
  })

export const migrateZeroScoreBenchmarkContract = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(migrateBenchmarkContractInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return migrateBenchmarkContract(data, context.session.email)
  })

export const getScreeningQuarantineContext = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(quarantineContextInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchScreeningQuarantineContext(data)
  })

// Signed owner links are identity metadata, not miner source: the ordinary
// read session is enough, exactly as it is for the rest of the review context.
export const getOwnerAttestations = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(ownerAttestationLookupInputSchema)
  .handler(({ data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchOwnerAttestations(data)
  })

// Source listings and excerpts expose miner source, so like artifact
// downloads they require write access and are audited with the operator.
export const listScreeningSourceFiles = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(sourceListingInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchQuarantineSourceFiles(data, context.session.email)
  })

export const readScreeningSourceFile = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(sourceExcerptInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchQuarantineSourceExcerpt(data, context.session.email)
  })

// Copy-review diffs surface miner source across two submissions, so like the
// source listings above they need write access and are audited with the operator.
export const getCopyReviewSourceDiff = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(sourceDiffInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchCopyReviewSourceDiff(data, context.session.email)
  })

export const getCopyReviewSourceDiffFile = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(sourceDiffFileInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchCopyReviewSourceDiffFile(data, context.session.email)
  })

// Baseline diffs render miner source against the starter kit, so they carry the
// same write-access requirement and operator audit as every other source read.
export const getScreeningBaselineDiff = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(baselineDiffInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchQuarantineBaselineDiff(data, context.session.email)
  })

export const readScreeningBaselineDiffFile = createServerFn({ method: 'GET' })
  .middleware([writeAuthMiddleware])
  .validator(baselineDiffFileInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchQuarantineBaselineDiffFile(data, context.session.email)
  })
