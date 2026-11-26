import {
  listModelContextSubmissionsInputSchema,
  modelContextBoardSchema,
  modelContextRunDetailSchema,
  modelContextRunIdInputSchema,
  modelContextScorerStatusSchema,
  modelContextSeasonListSchema,
  modelContextSeasonSchema,
  modelContextSettingsControlSchema,
  modelContextSettingsRevisionSchema,
  modelContextSubmissionDetailSchema,
  modelContextSubmissionIdInputSchema,
  modelContextSubmissionListSchema,
  rejectModelContextInputSchema,
  rescoreModelContextInputSchema,
  rotateModelContextSeasonInputSchema,
  setModelContextSettingsInputSchema,
} from '../lib/model-context.schemas'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'

// model_context (CPM) competition operator surface, contract #3572 section 8.
// Reads are plain admin GETs over Platform's /admin/model-context endpoints
// plus the public board and seasons. Every write goes through Platform's
// confirmation-gated endpoint under the signed-in operator as the audit
// actor; Platform appends each rescore, rejection and rotation to its
// model_context_operator_actions audit, which get_model_context_submission
// serves back.

const BASE = '/api/v1/admin/model-context'

// Keep Platform's refusal text verbatim and append what was not applied and
// how to recover. A 409 is a state conflict (stale revision or status, wrong
// confirmation, lane off, no open season, a run already in flight): re-read.
// A 422 is the request itself: correct it; re-reading fixes nothing.
function refusal(cause: unknown, reread: string) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  const recovery =
    cause.status === 422
      ? 'correct the refused fields and resubmit.'
      : `re-read ${reread} and resubmit with what it reports.`
  return new Error(`${cause.message}. Nothing was applied: ${recovery}`)
}

async function guarded<T>(reread: string, operation: () => Promise<T>): Promise<T> {
  try {
    return await operation()
  } catch (cause) {
    throw refusal(cause, reread) ?? cause
  }
}

export async function fetchModelContextSettings() {
  return modelContextSettingsControlSchema.parse(await platformAdminRequest(`${BASE}/settings`))
}

export async function setModelContextSettings(rawInput: unknown, actor: string) {
  const input = setModelContextSettingsInputSchema.parse(rawInput)
  // The POST's own response is the revision this write created; a re-read
  // could report a concurrent write as if it were this one.
  return guarded('get_model_context_settings', async () =>
    modelContextSettingsRevisionSchema.parse(
      await platformAdminRequest(`${BASE}/settings`, {
        method: 'POST',
        actor,
        body: {
          scope: '*',
          expected_revision: input.expectedRevision,
          settings: input.settings,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}

export async function listModelContextSubmissions(rawInput: unknown) {
  const input = listModelContextSubmissionsInputSchema.parse(rawInput)
  const params = new URLSearchParams({
    limit: String(input.limit),
    offset: String(input.offset),
  })
  if (input.hotkey) params.set('hotkey', input.hotkey)
  if (input.status) params.set('status', input.status)
  const page = modelContextSubmissionListSchema.parse(
    await platformAdminRequest(`${BASE}/submissions?${params}`),
  )
  // Platform's total counts exactly what the filters match. An older
  // Platform omits it: counts_by_status is subnet-wide, so it bounds only a
  // request with no hotkey filter, and a hotkey filter (with or without a
  // status) falls back to "a full page may have more".
  const total =
    page.total ??
    (input.hotkey
      ? null
      : input.status
        ? (page.counts_by_status[input.status] ?? 0)
        : Object.values(page.counts_by_status).reduce((sum, count) => sum + count, 0))
  return {
    ...page,
    total,
    limit: input.limit,
    offset: input.offset,
    returned: page.submissions.length,
    has_more:
      total === null
        ? page.submissions.length === input.limit
        : input.offset + page.submissions.length < total,
  }
}

export async function fetchModelContextSubmission(rawInput: unknown) {
  const { submissionId } = modelContextSubmissionIdInputSchema.parse(rawInput)
  return modelContextSubmissionDetailSchema.parse(
    await platformAdminRequest(`${BASE}/submissions/${encodeURIComponent(submissionId)}`),
  )
}

export async function fetchModelContextRun(rawInput: unknown) {
  const { runId } = modelContextRunIdInputSchema.parse(rawInput)
  return modelContextRunDetailSchema.parse(
    await platformAdminRequest(`${BASE}/runs/${encodeURIComponent(runId)}`),
  )
}

export async function rescoreModelContextSubmission(rawInput: unknown, actor: string) {
  const input = rescoreModelContextInputSchema.parse(rawInput)
  return guarded('get_model_context_submission', async () =>
    modelContextSubmissionDetailSchema.parse(
      await platformAdminRequest(
        `${BASE}/submissions/${encodeURIComponent(input.submissionId)}/rescore`,
        {
          method: 'POST',
          actor,
          body: {
            expected_status: input.expectedStatus,
            reason: input.reason,
            actor,
            confirmation: input.confirmation,
          },
        },
      ),
    ),
  )
}

export async function rejectModelContextSubmission(rawInput: unknown, actor: string) {
  const input = rejectModelContextInputSchema.parse(rawInput)
  return guarded('get_model_context_submission', async () =>
    modelContextSubmissionDetailSchema.parse(
      await platformAdminRequest(
        `${BASE}/submissions/${encodeURIComponent(input.submissionId)}/reject`,
        {
          method: 'POST',
          actor,
          body: {
            expected_status: input.expectedStatus,
            reason: input.reason,
            actor,
            confirmation: input.confirmation,
          },
        },
      ),
    ),
  )
}

// The public board plus the season it ranks (open block, hash, seeds, bench
// version, floors and close time), so one read answers "what is scored on
// what".
export async function fetchModelContextBoard() {
  const [board, seasons] = await Promise.all([
    platformAdminRequest('/api/v1/model-context/board'),
    platformAdminRequest('/api/v1/model-context/seasons?limit=1&offset=0'),
  ])
  const parsedSeasons = modelContextSeasonListSchema.parse(seasons)
  return {
    board: modelContextBoardSchema.parse(board),
    season:
      parsedSeasons.seasons.find((row) => row.season === parsedSeasons.current) ?? null,
  }
}

export async function fetchModelContextScorer() {
  return modelContextScorerStatusSchema.parse(await platformAdminRequest(`${BASE}/scorer`))
}

export async function rotateModelContextSeason(rawInput: unknown, actor: string) {
  const input = rotateModelContextSeasonInputSchema.parse(rawInput)
  return guarded('get_model_context_board', async () =>
    modelContextSeasonSchema.parse(
      await platformAdminRequest(`${BASE}/seasons/rotate`, {
        method: 'POST',
        actor,
        body: { reason: input.reason, actor, confirmation: input.confirmation },
      }),
    ),
  )
}
