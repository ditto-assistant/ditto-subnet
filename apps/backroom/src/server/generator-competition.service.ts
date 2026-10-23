import {
  generatorBoardSchema,
  generatorProbeSchema,
  generatorPromotionCandidatesSchema,
  generatorRunDetailSchema,
  generatorRunIdInputSchema,
  generatorSettingsControlSchema,
  generatorSubmissionDetailSchema,
  generatorSubmissionIdInputSchema,
  generatorSubmissionListSchema,
  generatorPromotionInputSchema,
  listGeneratorSubmissionsInputSchema,
  rejectGeneratorInputSchema,
  rescoreGeneratorInputSchema,
  rotateGeneratorProbeInputSchema,
  setGeneratorSettingsInputSchema,
  GENERATOR_SETTINGS_SCOPE,
} from '../lib/generator-competition.schemas'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'

// Shadow generator competition operator surface. Reads are plain admin GETs
// over Platform's /admin/generator-competition endpoints plus the public
// board. Generator source never crosses this boundary (Platform does not serve
// it to admins), a run's seeds appear only after it finished, and probe seeds
// are never served at all.

const BASE = '/api/v1/admin/generator-competition'

export async function fetchGeneratorSettings() {
  return generatorSettingsControlSchema.parse(await platformAdminRequest(`${BASE}/settings`))
}

// Keep Platform's refusal text verbatim and append the recovery for that
// refusal: only a stale or concurrent write is fixed by re-reading.
function settingsRefusal(cause: unknown) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  const recovery = /confirmation/i.test(cause.message)
    ? 'Nothing was applied: the confirmation must be "APPLY GENERATOR COMPETITION <MODE>" naming the mode this revision applies.'
    : cause.status === 422
      ? 'Nothing was applied: correct the refused settings (min_solver_owners <= min_panel <= panel_size, every field inside its bound) and resubmit with the same expectedRevision.'
      : 'Nothing was applied: re-read get_generator_competition_settings and resubmit with the revision it reports.'
  return new Error(`${cause.message}. ${recovery}`)
}

export async function setGeneratorSettings(rawInput: unknown, actor: string) {
  const input = setGeneratorSettingsInputSchema.parse(rawInput)
  try {
    await platformAdminRequest(`${BASE}/settings`, {
      method: 'POST',
      actor,
      body: {
        scope: GENERATOR_SETTINGS_SCOPE,
        expected_revision: input.expectedRevision,
        settings: input.settings,
        reason: input.reason,
        actor,
        confirmation: input.confirmation,
      },
    })
  } catch (cause) {
    throw settingsRefusal(cause) ?? cause
  }
  return fetchGeneratorSettings()
}

export async function listGeneratorSubmissions(rawInput: unknown) {
  const input = listGeneratorSubmissionsInputSchema.parse(rawInput)
  const params = new URLSearchParams({
    limit: String(input.limit),
    offset: String(input.offset),
  })
  if (input.hotkey) params.set('hotkey', input.hotkey)
  if (input.status) params.set('status', input.status)
  return generatorSubmissionListSchema.parse(
    await platformAdminRequest(`${BASE}/submissions?${params}`),
  )
}

export async function fetchGeneratorSubmission(rawInput: unknown) {
  const { submissionId } = generatorSubmissionIdInputSchema.parse(rawInput)
  return generatorSubmissionDetailSchema.parse(
    await platformAdminRequest(`${BASE}/submissions/${encodeURIComponent(submissionId)}`),
  )
}

export async function fetchGeneratorRun(rawInput: unknown) {
  const { runId } = generatorRunIdInputSchema.parse(rawInput)
  return generatorRunDetailSchema.parse(
    await platformAdminRequest(`${BASE}/runs/${encodeURIComponent(runId)}`),
  )
}

export async function fetchGeneratorProbe() {
  return generatorProbeSchema.parse(await platformAdminRequest(`${BASE}/probe`))
}

export async function fetchGeneratorPromotionCandidates(rawInput: unknown) {
  const { limit, offset } = generatorPromotionInputSchema.parse(rawInput)
  return generatorPromotionCandidatesSchema.parse(
    await platformAdminRequest(`${BASE}/promotion-candidates?limit=${limit}&offset=${offset}`),
  )
}

export async function fetchGeneratorBoard() {
  return generatorBoardSchema.parse(await platformAdminRequest('/api/v1/generator/board'))
}

export async function rotateGeneratorProbe(rawInput: unknown, actor: string) {
  const input = rotateGeneratorProbeInputSchema.parse(rawInput)
  return generatorProbeSchema.parse(
    await platformAdminRequest(`${BASE}/probe/rotate`, {
      method: 'POST',
      actor,
      body: { reason: input.reason, actor, confirmation: input.confirmation },
    }),
  )
}

export async function rescoreGenerator(rawInput: unknown, actor: string) {
  const input = rescoreGeneratorInputSchema.parse(rawInput)
  return generatorSubmissionDetailSchema.parse(
    await platformAdminRequest(
      `${BASE}/submissions/${encodeURIComponent(input.submissionId)}/rescore`,
      {
        method: 'POST',
        actor,
        body: { expected_status: 'active', reason: input.reason, actor },
      },
    ),
  )
}

export async function rejectGenerator(rawInput: unknown, actor: string) {
  const input = rejectGeneratorInputSchema.parse(rawInput)
  return generatorSubmissionDetailSchema.parse(
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
  )
}
