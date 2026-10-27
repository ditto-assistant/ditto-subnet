import {
  closeGeneratorRoundInputSchema,
  defaultGeneratorResponseSchema,
  generatorRoundDatasetSchema,
  generatorRoundDetailSchema,
  generatorRoundIndexInputSchema,
  generatorRoundListSchema,
  generatorRoundLoopSchema,
  generatorRoundPreviewSchema,
  generatorRoundTickSchema,
  listGeneratorRoundsInputSchema,
  registerDefaultGeneratorInputSchema,
  defaultGeneratorUploadResponseSchema,
  tickGeneratorRoundsInputSchema,
  releaseRoundLeaseInputSchema,
  republishRoundCommitmentInputSchema,
  voidGeneratorRoundInputSchema,
  generatorBoardSchema,
  generatorProbeSchema,
  generatorPromotionCandidatesSchema,
  generatorRunDetailSchema,
  generatorRunIdInputSchema,
  generatorSettingsControlSchema,
  generatorSettingsRevisionSchema,
  generatorSubmissionDetailSchema,
  generatorSubmissionIdInputSchema,
  generatorSubmissionListSchema,
  generatorPromotionInputSchema,
  listGeneratorSubmissionsInputSchema,
  rejectGeneratorInputSchema,
  reinstateGeneratorInputSchema,
  rescoreGeneratorInputSchema,
  rotateGeneratorProbeInputSchema,
  setGeneratorSettingsInputSchema,
  GENERATOR_SETTINGS_SCOPE,
} from '../lib/generator-competition.schemas'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'

// Shadow generator competition operator surface. Reads are plain admin GETs
// over Platform's /admin/generator-competition endpoints plus the public
// board. Generator source never crosses this boundary (Platform does not serve
// it to admins), a run's seeds appear only after it finished, probe seeds are
// never served at all, and a round's secret only after its privacy window.

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

// Reads default a field an older Platform omits. A write must not use that
// default to report a setting Platform silently dropped: an older Platform
// ignores unknown fields and stores the revision without them. A dropped field
// whose requested value equals that default changed nothing and is fine.
function droppedSettings(
  raw: unknown,
  requested: Record<string, unknown>,
  stored: Record<string, unknown>,
) {
  const returned = (raw as { settings?: unknown } | null)?.settings
  const returnedFields = returned !== null && typeof returned === 'object' ? returned : {}
  return Object.keys(requested).filter(
    (field) => !(field in returnedFields) && requested[field] !== stored[field],
  )
}

export async function setGeneratorSettings(rawInput: unknown, actor: string) {
  const input = setGeneratorSettingsInputSchema.parse(rawInput)
  let raw: unknown
  try {
    // The POST's own response is the revision this write created; a re-read
    // could report a concurrent write as if it were this one.
    raw = await platformAdminRequest(`${BASE}/settings`, {
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
  const created = generatorSettingsRevisionSchema.parse(raw)
  const dropped = droppedSettings(raw, input.settings, created.settings)
  if (dropped.length > 0) {
    throw new Error(
      `Platform stored revision ${created.revision} without ${dropped.join(', ')}: it predates `
      + 'that setting, so the stored revision uses the default rather than the value you sent '
      + '(for upload_fee_required, generator uploads stay free). Deploy the Platform release '
      + 'that supports it, then re-read get_generator_competition_settings and write again.',
    )
  }
  return created
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

export async function reinstateGenerator(rawInput: unknown, actor: string) {
  const input = reinstateGeneratorInputSchema.parse(rawInput)
  return generatorSubmissionDetailSchema.parse(
    await platformAdminRequest(
      `${BASE}/submissions/${encodeURIComponent(input.submissionId)}/reinstate`,
      {
        method: 'POST',
        actor,
        body: {
          expected_status: input.expectedStatus,
          expected_status_reason: input.expectedStatusReason,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
          override_operator_ruling: input.overrideOperatorRuling,
        },
      },
    ),
  )
}

// --- rounds ----------------------------------------------------------------

function roundRefusal(cause: unknown) {
  if (!(cause instanceof PlatformAdminError)) return null
  if (cause.status !== 409 && cause.status !== 422) return null
  return new Error(
    `${cause.message}. Nothing was applied: re-read get_generator_round and retry with its current status.`,
  )
}

async function roundWrite<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation()
  } catch (cause) {
    throw roundRefusal(cause) ?? cause
  }
}

// A registration refusal is about the source or the confirmation, not a round,
// so its recovery never points at get_generator_round.
async function defaultGeneratorWrite<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation()
  } catch (cause) {
    if (cause instanceof PlatformAdminError && (cause.status === 409 || cause.status === 422)) {
      throw new Error(
        `${cause.message}. Nothing was registered: send the confirmation "REGISTER DEFAULT GENERATOR" with a source archive (an uploadKey from this operator's create_default_generator_upload, or sourceBase64) whose SHA-256 matches sourceSha256 and that no earlier upload already holds.`,
      )
    }
    throw cause
  }
}

export async function listGeneratorRounds(rawInput: unknown) {
  const { limit, offset } = listGeneratorRoundsInputSchema.parse(rawInput)
  return generatorRoundListSchema.parse(
    await platformAdminRequest(`${BASE}/rounds?limit=${limit}&offset=${offset}`),
  )
}

export async function fetchGeneratorRound(rawInput: unknown) {
  const { roundIndex } = generatorRoundIndexInputSchema.parse(rawInput)
  return generatorRoundDetailSchema.parse(await platformAdminRequest(`${BASE}/rounds/${roundIndex}`))
}

export async function fetchGeneratorRoundPreview() {
  return generatorRoundPreviewSchema.parse(await platformAdminRequest(`${BASE}/rounds/preview`))
}

export async function fetchGeneratorRoundLoop() {
  return generatorRoundLoopSchema.parse(await platformAdminRequest(`${BASE}/rounds/loop`))
}

export async function tickGeneratorRounds(rawInput: unknown, actor: string) {
  tickGeneratorRoundsInputSchema.parse(rawInput)
  return generatorRoundTickSchema.parse(
    await platformAdminRequest(`${BASE}/rounds/tick`, { method: 'POST', actor, body: {} }),
  )
}

export async function fetchGeneratorRoundDataset(rawInput: unknown) {
  const { roundIndex } = generatorRoundIndexInputSchema.parse(rawInput)
  return generatorRoundDatasetSchema.parse(
    await platformAdminRequest(`${BASE}/rounds/${roundIndex}/dataset`),
  )
}

export async function voidGeneratorRound(rawInput: unknown, actor: string) {
  const input = voidGeneratorRoundInputSchema.parse(rawInput)
  return roundWrite(async () =>
    generatorRoundDetailSchema.parse(
      await platformAdminRequest(`${BASE}/rounds/${input.roundIndex}/void`, {
        method: 'POST',
        actor,
        body: {
          expected_status: input.expectedStatus,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}

export async function closeGeneratorRound(rawInput: unknown, actor: string) {
  const input = closeGeneratorRoundInputSchema.parse(rawInput)
  return roundWrite(async () =>
    generatorRoundDetailSchema.parse(
      await platformAdminRequest(`${BASE}/rounds/${input.roundIndex}/close`, {
        method: 'POST',
        actor,
        body: {
          expected_status: 'running',
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}

export async function republishGeneratorRoundCommitment(rawInput: unknown, actor: string) {
  const input = republishRoundCommitmentInputSchema.parse(rawInput)
  return roundWrite(async () =>
    generatorRoundDetailSchema.parse(
      await platformAdminRequest(`${BASE}/rounds/${input.roundIndex}/republish-commitment`, {
        method: 'POST',
        actor,
        body: {
          expected_status: 'committed',
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}

export async function releaseGeneratorRoundLease(rawInput: unknown, actor: string) {
  const input = releaseRoundLeaseInputSchema.parse(rawInput)
  return roundWrite(async () =>
    generatorRoundDetailSchema.parse(
      await platformAdminRequest(`${BASE}/rounds/${input.roundIndex}/release-lease`, {
        method: 'POST',
        actor,
        body: {
          agent_id: input.agentId,
          validator_hotkey: input.validatorHotkey,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}

export async function createDefaultGeneratorUpload(actor: string) {
  return defaultGeneratorUploadResponseSchema.parse(
    await platformAdminRequest(`${BASE}/default-generator/upload-url`, {
      method: 'POST',
      actor,
      body: {},
    }),
  )
}

export async function registerDefaultGenerator(rawInput: unknown, actor: string) {
  const input = registerDefaultGeneratorInputSchema.parse(rawInput)
  if ((input.sourceBase64 === undefined) === (input.uploadKey === undefined)) {
    throw new Error(
      'Provide exactly one of uploadKey (from create_default_generator_upload) or sourceBase64. Nothing was registered.',
    )
  }
  return defaultGeneratorWrite(async () =>
    defaultGeneratorResponseSchema.parse(
      await platformAdminRequest(`${BASE}/default-generator`, {
        method: 'POST',
        actor,
        body: {
          miner_hotkey: input.minerHotkey,
          source_sha256: input.sourceSha256,
          source_base64: input.sourceBase64 ?? null,
          upload_key: input.uploadKey ?? null,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    ),
  )
}
