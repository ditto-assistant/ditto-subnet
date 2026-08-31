// Response shapes for MCP tools whose payloads are dominated by nested history
// or repetition rather than by information needed on every call.
//
// Both are presentation only. Every platform field is still reachable; the
// gating, snapshot checks, and audit records behind them are untouched.

import type { z } from 'zod'
import type {
  batchRetryValidationResponseSchema,
  copyReviewListSchema,
  ownerFootprintDetailSchema,
  screeningQuarantineListSchema,
  screeningSubmissionListSchema,
  stuckSubmissionsListSchema,
  validatorAssignmentListSchema,
  validatorFleetObservabilitySchema,
} from './admin.schemas'
import {
  compactListField,
  hoistSharedFields,
  type ResponseRow,
} from './mcp-response'

type BatchRetryResponse = z.infer<typeof batchRetryValidationResponseSchema>
type CopyReviewList = z.infer<typeof copyReviewListSchema>
type ScreeningQuarantineList = z.infer<typeof screeningQuarantineListSchema>
type ScreeningSubmissionList = z.infer<typeof screeningSubmissionListSchema>
type StuckSubmissionsList = z.infer<typeof stuckSubmissionsListSchema>
type ValidatorAssignmentList = z.infer<typeof validatorAssignmentListSchema>
type ValidatorFleetObservability = z.infer<typeof validatorFleetObservabilitySchema>
type OwnerFootprintDetail = z.infer<typeof ownerFootprintDetailSchema>

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
    const { evidence, finding, review_notes: reviewNotes, ...rest } = quarantine
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
      review_notes_count: reviewNotes?.length ?? null,
      review_note_kinds: reviewNotes?.map((note) => note.kind) ?? null,
      finding: findingSummary,
    } as ResponseRow
  })
  return compactListField({ ...response, detail, items }, 'items', {
    pin: ['quarantine_id'],
  })
}

/**
 * Compact the ATH review queue into one triage row per held agent.
 *
 * The queue's job is to answer "who is waiting, why, and since when" for every
 * hold at once; the deep evidence belongs to `get_ath_review` and
 * `get_copy_review_source_diff`, which the operator calls for the one row they
 * picked. So each row keeps the identities a decision starts from — the held
 * agent, its miner, when it was submitted and held, the review kind, and for a
 * copy hold the identity of the agent it was matched against — and drops the
 * per-row evidence that would make a 200-row page unreadable:
 * `fingerprint_versions` (algorithm provenance, identical across a generation),
 * and the deferred hold's `review_audit` transcript, which is the largest
 * object on the row and is kept as its digest so the audit stays verifiable.
 *
 * `agent_status` is deliberately NOT dropped: a `pending` review whose agent
 * reads anything but `ath_pending_review` is a stranded hold rather than a
 * queue entry, and `resolve_ath_review` will 409 on it.
 */
export function compactAthReviewQueue(response: CopyReviewList) {
  const items = response.items.map((review) => {
    const { original, current_comparison: _comparison, ...rest } = review
    const {
      fingerprint_versions: _fingerprints,
      deferred_review: deferred,
      ...hold
    } = original
    return {
      ...rest,
      hold: {
        ...hold,
        deferred_review: deferred
          ? (() => {
              const { review_audit: _audit, ...trigger } = deferred
              return trigger
            })()
          : null,
      },
    } as ResponseRow
  })
  return compactListField({ ...response, items }, 'items', {
    pin: ['agent_id', 'review_id'],
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

/**
 * Compact one fleet-wide stuck-submission list.
 *
 * The platform list is summary-only and server-paginated. `quorum` is dropped
 * from the rows because the envelope already
 * carries it, and the platform repeats the same number on every row.
 */
export function compactStuckSubmissions(response: StuckSubmissionsList) {
  return compactListField(response, 'submissions', {
    pin: ['agent_id'],
    omit: ['quorum'],
  })
}

type VersionHistogramRow = {
  value: string | number | null
  count: number
  online_count: number
  serving_count: number
}

function versionHistogram(
  rows: ReadonlyArray<{
    value: string | number | null | undefined
    online: boolean
    serving: boolean
  }>,
): VersionHistogramRow[] {
  const groups = new Map<string, VersionHistogramRow>()
  for (const row of rows) {
    const value = row.value ?? null
    const key = value === null ? '' : String(value)
    const current = groups.get(key) ?? {
      value,
      count: 0,
      online_count: 0,
      serving_count: 0,
    }
    current.count += 1
    if (row.online) current.online_count += 1
    if (row.serving) current.serving_count += 1
    groups.set(key, current)
  }
  return [...groups.values()].sort((left, right) => {
    if (right.count !== left.count) return right.count - left.count
    return String(left.value ?? '').localeCompare(String(right.value ?? ''))
  })
}

/**
 * Compact the public validator heartbeat view for MCP.
 *
 * The slot-cap console drops software/stack/scorer identity so a browser page
 * cannot ship calibration manifests. MCP needs those identities to wait for a
 * release, so this keeps component revisions and updater versions, counts
 * live work instead of embedding progress blobs, and adds a fleet-wide
 * histogram computed before any hotkey filter or page slice.
 */
export function compactValidatorFleet(response: ValidatorFleetObservability) {
  const validators = response.validators
  const serving = (row: (typeof validators)[number]) =>
    row.bench_serviceability === 'serving'
  const rolloutRows = (value: (row: (typeof validators)[number]) => string | number | null) =>
    validators.map((row) => ({
      value: value(row),
      online: row.online,
      serving: serving(row),
    }))

  return {
    generated_at: response.generated_at,
    active_bench_version: response.active_bench_version,
    online_window_seconds: response.online_window_seconds,
    stale_window_seconds: response.stale_window_seconds,
    reported_count: response.reported_count ?? validators.length,
    online_count: response.online_count ?? validators.filter((row) => row.online).length,
    serving_count: validators.filter((row) => serving(row)).length,
    online_serving_count: validators.filter((row) => row.online && serving(row)).length,
    software_obsolete_count: validators.filter(
      (row) => row.bench_serviceability === 'software_obsolete',
    ).length,
    scorer_unverified_count: validators.filter(
      (row) => row.bench_serviceability === 'scorer_unverified',
    ).length,
    rollout: {
      software_versions: versionHistogram(rolloutRows((row) => row.software_version)),
      protocol_versions: versionHistogram(rolloutRows((row) => row.protocol_version)),
      updater_current_versions: versionHistogram(
        rolloutRows((row) => row.updater_status?.current_version ?? null),
      ),
      dittobench_api_revisions: versionHistogram(
        rolloutRows((row) => row.stack?.components?.dittobench_api?.source_revision ?? null),
      ),
      model_relay_revisions: versionHistogram(
        rolloutRows((row) => row.stack?.components?.model_relay?.source_revision ?? null),
      ),
      scorer_source_revisions: versionHistogram(
        rolloutRows((row) => row.scorer?.source_revision ?? null),
      ),
    },
    validators,
  }
}

export function compactValidatorAssignments(
  response: Pick<ValidatorAssignmentList, 'items'> & Record<string, unknown>,
) {
  return compactListField(response, 'items', {
    pin: ['agent_id', 'validator_hotkey'],
  })
}

/**
 * Compact one miner owner footprint.
 *
 * The linkage walk is the tool that answers "who else does this operator
 * control?", so its payload scales with the size of the operator's cluster and
 * each hotkey row embeds a complete public leaderboard standing. Two forms of
 * repetition dominate: the standing repeats the row's own `miner_hotkey` (it is
 * the join key Backroom used to fetch it), and every field the board states
 * identically for the whole cluster — quorum, bench version, dataset pin,
 * truncation flags — rides on every row.
 *
 * So the nested hotkey is dropped from each standing, the fields identical
 * across every ranked standing move once into a sibling `standings_shared`
 * object, and the row-level fields identical across every hotkey are hoisted
 * into `hotkeys_shared`. Reconstruction is lossless: a row is
 * `{ ...hotkeys_shared, ...row }`, its standing is
 * `{ ...standings_shared, ...row.leaderboard }`, and a standing's hotkey is
 * its row's `miner_hotkey`. Nothing is summarised away and no hotkey, agent,
 * or standing field disappears.
 */
export function compactMinerOwnerFootprint(response: OwnerFootprintDetail) {
  const rows = response.hotkeys.map((hotkey) => {
    if (hotkey.leaderboard === null) return hotkey as unknown as ResponseRow
    const { miner_hotkey: _repeatedRowIdentity, ...standing } = hotkey.leaderboard
    return { ...hotkey, leaderboard: standing } as unknown as ResponseRow
  })

  const standings = rows
    .map((row) => row.leaderboard)
    .filter((standing): standing is ResponseRow => standing !== null)
  const { shared: standingsShared } = hoistSharedFields(standings)
  const standingsSharedKeys = new Set(Object.keys(standingsShared))
  const stripped = rows.map((row) =>
    row.leaderboard === null || row.leaderboard === undefined
      ? row
      : {
          ...row,
          leaderboard: Object.fromEntries(
            Object.entries(row.leaderboard).filter(
              ([key]) => !standingsSharedKeys.has(key),
            ),
          ),
        },
  )

  const compacted = compactListField({ ...response, hotkeys: stripped }, 'hotkeys', {
    pin: ['miner_hotkey'],
  })
  return Object.keys(standingsShared).length > 0
    ? { ...compacted, standings_shared: standingsShared }
    : compacted
}
