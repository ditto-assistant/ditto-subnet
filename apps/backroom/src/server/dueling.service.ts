import {
  duelingSeasonSchema,
  duelingSeasonsSchema,
  duelingSlicesSchema,
  getDuelingSeasonInputSchema,
  listDuelingSlicesInputSchema,
  retryDuelingSliceInputSchema,
  rotateDuelingSeasonInputSchema,
} from '../lib/dueling.schemas'
import { PlatformAdminError, platformAdminRequest } from './ditto.server'

const BASE = '/api/v1/admin/generator-competition/dueling'

// Without a season id: the dueling target plus the open (current), committed
// (next) and recent seasons. With one: that season in full.
export async function fetchDuelingSeason(rawInput: unknown) {
  const { seasonId } = getDuelingSeasonInputSchema.parse(rawInput)
  if (seasonId) {
    return duelingSeasonSchema.parse(
      await platformAdminRequest(`${BASE}/seasons/${encodeURIComponent(seasonId)}`),
    )
  }
  return duelingSeasonsSchema.parse(await platformAdminRequest(`${BASE}/seasons`))
}

export async function listDuelingSlices(rawInput: unknown) {
  const input = listDuelingSlicesInputSchema.parse(rawInput)
  const params = new URLSearchParams({
    limit: String(input.limit),
    offset: String(input.offset),
  })
  if (input.seasonId) params.set('season_id', input.seasonId)
  if (input.preparationId) params.set('preparation_id', input.preparationId)
  if (input.status) params.set('status', input.status)
  return duelingSlicesSchema.parse(await platformAdminRequest(`${BASE}/slices?${params}`))
}

export async function rotateDuelingSeason(rawInput: unknown, actor: string) {
  const input = rotateDuelingSeasonInputSchema.parse(rawInput)
  try {
    return duelingSeasonSchema.parse(
      await platformAdminRequest(`${BASE}/rotate`, {
        method: 'POST',
        actor,
        body: {
          ...(input.seasonId ? { season_id: input.seasonId } : {}),
          immediate: input.immediate,
          reason: input.reason,
          actor,
          confirmation: input.confirmation,
        },
      }),
    )
  } catch (cause) {
    if (cause instanceof PlatformAdminError && cause.status === 409) {
      throw new Error(
        `Nothing was rotated: ${cause.message}. Re-read get_dueling_season and retry only `
        + 'against an open season that has not already been rotated.',
      )
    }
    throw cause
  }
}

// Re-queue exactly one failed slice preparation (audited); returns its row.
export async function retryDuelingSlicePreparation(rawInput: unknown, actor: string) {
  const input = retryDuelingSliceInputSchema.parse(rawInput)
  try {
    return duelingSlicesSchema.shape.slices.element.parse(
      await platformAdminRequest(
        `${BASE}/slices/${encodeURIComponent(input.preparationId)}/retry`,
        {
          method: 'POST',
          actor,
          body: {
            expected_status: input.expectedStatus,
            expected_error: input.expectedError,
            reason: input.reason,
            actor,
            confirmation: input.confirmation,
            force: input.force,
          },
        },
      ),
    )
  } catch (cause) {
    if (cause instanceof PlatformAdminError && cause.status === 409) {
      throw new Error(
        `Nothing was retried: ${cause.message}. Re-read list_dueling_slices `
        + '(preparationId) and retry against the status and error it shows.',
      )
    }
    throw cause
  }
}
