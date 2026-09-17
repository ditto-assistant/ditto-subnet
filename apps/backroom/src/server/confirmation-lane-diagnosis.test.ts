import { describe, expect, it } from 'vitest'
import {
  CONFIRMATION_LANE_STATES,
  diagnoseConfirmationLane,
  type ConfirmationLaneDiagnosisInput,
  type ConfirmationLanePage,
} from './confirmation-lane-diagnosis'

function emptyPage(count = 0): ConfirmationLanePage {
  return { count, items: [] }
}

function pages(
  overrides: Partial<ConfirmationLaneDiagnosisInput['pages']> = {},
): ConfirmationLaneDiagnosisInput['pages'] {
  return {
    blocked_budget: emptyPage(),
    pending: emptyPage(),
    leased: emptyPage(),
    failed: emptyPage(),
    completed: emptyPage(),
    superseded: emptyPage(),
    ...overrides,
  }
}

function ticket(overrides: Partial<ConfirmationLaneDiagnosisInput['pages']['failed']['items'][number]['tickets'][number]> = {}) {
  return {
    ticket_id: '20000000-0000-4000-8000-000000000001',
    validator_hotkey: '5ValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    slot_id: 'longmem-0',
    status: 'expired',
    issued_at: '2026-08-18T19:54:00.000Z',
    failed_at: '2026-08-18T19:54:02.000Z',
    failure_reason: 'confirmation_execution_failed',
    failure_class: 'platform',
    failure_stage: 'unknown',
    prepare_rejection: null,
    prepare_rejected_at: null,
    ...overrides,
  }
}

function failedBundle(index: number, benchVersion = 11) {
  const suffix = String(index).padStart(12, '0')
  return {
    bundle_id: `10000000-0000-4000-8000-${suffix}`,
    bench_version: benchVersion,
    state: 'failed',
    created_at: '2026-08-18T19:54:00.000Z',
    tickets: [
      ticket({
        ticket_id: `20000000-0000-4000-8000-${suffix}`,
      }),
    ],
  }
}

function baseInput(
  overrides: Partial<ConfirmationLaneDiagnosisInput> = {},
): ConfirmationLaneDiagnosisInput {
  return {
    observedAt: '2026-08-18T20:00:00.000Z',
    mode: 'shadow',
    issuanceActive: true,
    settingsRevision: 15,
    dailyBundleCap: 24,
    dailyDollarCapMicrousd: 5_000_000,
    profileRevision: 'v9-confirmation-shadow-1',
    fleet: {
      generated_at: '2026-08-18T20:00:00.000Z',
      active_bench_version: 11,
      validators: [
        {
          validator_hotkey: '5ValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          online: true,
          bench_serviceability: 'serving',
          active_benchmark_count: 0,
          updater_status: { current_version: 'v0.88.1' },
        },
      ],
    },
    pages: pages(),
    ...overrides,
  }
}

describe('diagnoseConfirmationLane', itStates)

function itStates() {
  it('covers every confirmation lifecycle state in the window', () => {
    expect(CONFIRMATION_LANE_STATES).toEqual([
      'blocked_budget',
      'pending',
      'leased',
      'failed',
      'completed',
      'superseded',
    ])
  })

  it('names the leftover validator v9 identity pin from the live fail signature', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        pages: pages({
          failed: {
            count: 9,
            items: [1, 2, 3, 4, 5].map((index) => failedBundle(index, 11)),
            budget: {
              utc_day: '2026-08-18',
              revision: 15,
              issued_attempts: 12,
              outstanding_reserved_microusd: 0,
              settled_microusd: 0,
            },
            shadow_calibration: {
              completed_bundle_count: 0,
              failed_bundle_count: 9,
              superseded_bundle_count: 154,
              bench_version: 11,
            },
          },
        }),
      }),
    )

    expect(diagnosis.likely_cause.code).toBe('leftover_validator_v9_identity_pin')
    expect(diagnosis.counts.failed).toBe(9)
    expect(diagnosis.bench_versions).toEqual([{ key: '11', count: 5 }])
    expect(diagnosis.failure_histogram[0]).toMatchObject({
      failure_class: 'platform',
      failure_stage: 'unknown',
      failure_reason: 'confirmation_execution_failed',
      count: 5,
      median_duration_ms: 2000,
    })
    expect(diagnosis.recent_failures).toHaveLength(5)
  })

  it('surfaces dittobench bind-style execution failures in the histogram', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        pages: pages({
          failed: {
            count: 3,
            items: [1, 2, 3].map((index) => ({
              ...failedBundle(index, 11),
              tickets: [
                ticket({
                  ticket_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
                  failure_class: 'dittobench',
                  failure_stage: 'running_confirmation',
                  failure_reason: 'confirmation_execution_failed',
                  issued_at: '2026-08-18T18:00:00.000Z',
                  failed_at: '2026-08-18T18:01:10.000Z',
                }),
              ],
            })),
          },
        }),
      }),
    )

    expect(diagnosis.failure_histogram[0]).toMatchObject({
      failure_class: 'dittobench',
      failure_stage: 'running_confirmation',
      failure_reason: 'confirmation_execution_failed',
      count: 3,
    })
    expect(diagnosis.recent_failures).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          failure_class: 'dittobench',
          failure_stage: 'running_confirmation',
          failure_reason: 'confirmation_execution_failed',
        }),
      ]),
    )
    expect(diagnosis.likely_cause.code).toBe('execution_after_preparing')
  })

  it('does not blame the v9 pin when failures happen after execution starts', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        pages: pages({
          failed: {
            count: 4,
            items: [1, 2, 3, 4].map((index) => ({
              ...failedBundle(index, 11),
              tickets: [
                ticket({
                  ticket_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
                  failure_class: 'longmem_run_http_status',
                  failure_stage: 'running_confirmation',
                  issued_at: '2026-08-18T18:00:00.000Z',
                  failed_at: '2026-08-18T18:20:00.000Z',
                }),
              ],
            })),
          },
        }),
      }),
    )

    expect(diagnosis.likely_cause.code).toBe('execution_after_preparing')
  })

  it('names prepare-report rejection when execute finished and convert/rebuild 409d', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        pages: pages({
          failed: {
            count: 4,
            items: [1, 2, 3, 4].map((index) => ({
              ...failedBundle(index, 11),
              tickets: [
                ticket({
                  ticket_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
                  failure_class: 'platform',
                  failure_stage: 'finalizing',
                  prepare_rejection: 'ablation_profile_drift',
                  prepare_rejected_at: '2026-08-18T18:20:00.000Z',
                  issued_at: '2026-08-18T18:00:00.000Z',
                  failed_at: '2026-08-18T18:20:00.000Z',
                }),
              ],
            })),
          },
        }),
      }),
    )

    expect(diagnosis.likely_cause.code).toBe('prepare_report_rejected')
    expect(diagnosis.failure_histogram[0]).toMatchObject({
      failure_class: 'platform',
      failure_stage: 'finalizing',
      prepare_rejection: 'ablation_profile_drift',
      count: 4,
    })
    expect(diagnosis.recent_failures[0]).toMatchObject({
      prepare_rejection: 'ablation_profile_drift',
    })
  })

  it('reports issuance_disabled when mode is off', () => {
    const diagnosis = diagnoseConfirmationLane(baseInput({ mode: 'off', issuanceActive: false }))
    expect(diagnosis.likely_cause.code).toBe('issuance_disabled')
  })

  it('reports profile_not_installed when the pinned profile is absent from the release', () => {
    // Production revision 39 pinned v7 after the release moved to v8: mode read
    // shadow while every claim returned no work.
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        issuanceActive: false,
        profileRevision: 'v9-confirmation-shadow-bounded-2026-08-25-zdr-v7',
        profileInstalled: false,
        installedProfiles: [
          {
            revision: 'v9-confirmation-shadow-bounded-2026-08-27-no-retry-v8',
            checksum: '8e5be01e3efd17dbf6cc21e79b9d53d82d842b1563d403b0318f2a272d6297af',
          },
        ],
      }),
    )
    expect(diagnosis.likely_cause.code).toBe('profile_not_installed')
    expect(diagnosis.likely_cause.evidence).toContain('profile_installed=false')
    expect(diagnosis.likely_cause.evidence).toContain(
      'installed_profiles=v9-confirmation-shadow-bounded-2026-08-27-no-retry-v8@8e5be01e',
    )
    expect(diagnosis.policy).toMatchObject({
      profile_installed: false,
      installed_profiles: [
        expect.objectContaining({
          revision: 'v9-confirmation-shadow-bounded-2026-08-27-no-retry-v8',
        }),
      ],
    })
  })

  it('keeps issuance_inactive for an older Platform that does not report profile installation', () => {
    const diagnosis = diagnoseConfirmationLane(baseInput({ issuanceActive: false }))
    expect(diagnosis.likely_cause.code).toBe('issuance_inactive')
    expect(diagnosis.policy).toMatchObject({ profile_installed: null, installed_profiles: null })
  })

  it('does not report profile_not_installed while mode is off', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({ mode: 'off', issuanceActive: false, profileInstalled: false }),
    )
    expect(diagnosis.likely_cause.code).toBe('issuance_disabled')
  })

  it('reports healthy when completed evidence exists and no failures are sampled', () => {
    const diagnosis = diagnoseConfirmationLane(
      baseInput({
        pages: pages({
          completed: {
            count: 2,
            items: [
              {
                bundle_id: '10000000-0000-4000-8000-000000000099',
                bench_version: 11,
                state: 'completed',
                created_at: '2026-08-18T18:00:00.000Z',
                tickets: [],
              },
            ],
            shadow_calibration: {
              completed_bundle_count: 2,
              failed_bundle_count: 0,
              superseded_bundle_count: 0,
              bench_version: 11,
            },
          },
        }),
      }),
    )
    expect(diagnosis.likely_cause.code).toBe('healthy')
  })
}
