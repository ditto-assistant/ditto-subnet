// Response shapes for MCP tools whose payloads are dominated by nested history
// or repetition rather than by information needed on every call.
//
// Both are presentation only. Every platform field is still reachable; the
// gating, snapshot checks, and audit records behind them are untouched.

import type { z } from 'zod'
import type {
  batchRetryValidationResponseSchema,
  screeningQuarantineListSchema,
  screeningSubmissionListSchema,
  stuckSubmissionsListSchema,
} from './admin.schemas'
import {
  compactListField,
  hoistSharedFields,
  type ResponseRow,
} from './mcp-response'

type BatchRetryResponse = z.infer<typeof batchRetryValidationResponseSchema>
type ScreeningQuarantineList = z.infer<typeof screeningQuarantineListSchema>
type ScreeningSubmissionList = z.infer<typeof screeningSubmissionListSchema>
type StuckSubmissionsList = z.infer<typeof stuckSubmissionsListSchema>

const BATCH_RETRY_STATUSES = ['granted', 'idempotent', 'skipped'] as const

function timestamp(value: string | null | undefined) {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY
}

export function compactScreeningSubmissions(
  response: ScreeningSubmissionList,
  detail: 'summary' | 'full',
) {
  const items = response.items.map((submission) => {
    if (detail === 'full') return submission as unknown as ResponseRow
    const { attempts, ...rest } = submission
    const latestAttempt = [...attempts].sort(
      (left, right) =>
        timestamp(right.started_at) - timestamp(left.started_at) ||
        right.attempt_id.localeCompare(left.attempt_id),
    )[0]
    return {
      ...rest,
      attempt_count: attempts.length,
      latest_attempt: latestAttempt ?? null,
    } as ResponseRow
  })
  return compactListField({ ...response, detail, items }, 'items', {
    pin: ['agent_id'],
  })
}

export function compactScreeningQuarantines(
  response: ScreeningQuarantineList,
  detail: 'summary' | 'full',
) {
  const items = response.items.map((quarantine) => {
    if (detail === 'full') return quarantine as unknown as ResponseRow
    const { evidence, finding, ...rest } = quarantine
    const findingSummary = finding
      ? (() => {
          const {
            evidence: findingEvidence,
            artifact_sha256: _repeatedArtifact,
            ...summary
          } = finding
          return { ...summary, evidence_count: findingEvidence.length }
        })()
      : null
    return {
      ...rest,
      evidence_count: evidence?.length ?? null,
      evidence_codes: evidence?.map((item) => item.code) ?? null,
      finding: findingSummary,
    } as ResponseRow
  })
  return compactListField({ ...response, detail, items }, 'items', {
    pin: ['quarantine_id'],
  })
}

/**
 * Compact one batch validator-retry response.
 *
 * The platform answers with one self-describing row per item, which for a
 * fleet-wide recovery means the operator's whole reason paragraph, the actor,
 * the timestamp, the bench version, and the granted validator hotkeys repeated
 * verbatim N times, plus `agent_id` at two nesting levels.
 *
 * Here the rows are grouped by outcome — so the status stops being a per-row
 * string — the recovery is flattened into its row, and anything identical
 * across a group is lifted into that group's `shared` object. A batch where
 * every item was granted collapses to a count, one shared block, and one short
 * row per agent carrying only what actually differs.
 */
export function compactBatchRetryResponse(response: BatchRetryResponse) {
  const rows = response.results.map((item) => {
    const row: ResponseRow = { agent_id: item.agent_id }
    if (item.detail !== null) row.detail = item.detail
    if (item.recovery !== null) {
      for (const [key, value] of Object.entries(item.recovery)) {
        // `agent_id` is already the row's identity; the platform repeats it
        // inside the recovery object.
        if (key !== 'agent_id') row[key] = value
      }
    }
    return { status: item.status, row }
  })

  const counts: Record<string, number> = { total: response.results.length }
  const results: Record<string, unknown> = {}
  for (const status of BATCH_RETRY_STATUSES) {
    const group = rows.filter((entry) => entry.status === status)
    counts[status] = group.length
    if (group.length === 0) continue
    const { shared, rows: items } = hoistSharedFields(
      group.map((entry) => entry.row),
      { pin: ['agent_id'] },
    )
    results[status] =
      Object.keys(shared).length > 0 ? { shared, items } : { items }
  }

  return { counts, results }
}

function ticketStateCounts(tickets: ReadonlyArray<{ status: string }>) {
  const counts: Record<string, number> = {}
  for (const ticket of tickets) {
    counts[ticket.status] = (counts[ticket.status] ?? 0) + 1
  }
  return counts
}

/**
 * Compact one fleet-wide stuck-submission list.
 *
 * `summary` (the default) keeps every field an operator needs to triage and to
 * act — including the concurrency snapshot a retry requires — but replaces the
 * complete per-validator ticket history with its per-status counts. `full`
 * keeps the ticket arrays, for diagnosing one submission rather than surveying
 * the fleet.
 *
 * `quorum` is dropped from the rows in both modes: the envelope already
 * carries it, and the platform repeats the same number on every row.
 */
export function compactStuckSubmissions(
  response: StuckSubmissionsList,
  detail: 'summary' | 'full',
) {
  const submissions = response.submissions.map((submission) => {
    if (detail === 'full') return submission as unknown as ResponseRow
    const { tickets, ...rest } = submission
    return { ...rest, ticket_states: ticketStateCounts(tickets) } as ResponseRow
  })

  return compactListField({ ...response, detail, submissions }, 'submissions', {
    pin: ['agent_id'],
    omit: ['quorum'],
  })
}
