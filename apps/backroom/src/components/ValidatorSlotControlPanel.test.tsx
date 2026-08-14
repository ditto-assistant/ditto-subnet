// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ValidatorFleet,
  ValidatorSlotSettingsControl,
} from '../lib/admin.schemas'
import { ValidatorSlotControlPanel } from './ValidatorSlotControlPanel'

const getValidatorSlotSettings = vi.fn()
const getValidatorFleet = vi.fn()
const updateValidatorSlotSettings = vi.fn()
const updateValidatorIssuancePause = vi.fn()
const primaryHotkey = `5${'A'.repeat(47)}`
const secondaryHotkey = `5${'B'.repeat(47)}`

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getValidatorSlotSettings: () => getValidatorSlotSettings(),
  getValidatorFleet: () => getValidatorFleet(),
  updateValidatorSlotSettings: (input: unknown) => updateValidatorSlotSettings(input),
  updateValidatorIssuancePause: (input: unknown) => updateValidatorIssuancePause(input),
}))

// The live shape on 2026-07-25: revision 1 raised the cap from the shipped
// default of 2 to 3.
function control(
  overrides: Partial<ValidatorSlotSettingsControl['effective']> = {},
): ValidatorSlotSettingsControl {
  const revision = {
    revision: 1,
    parent_revision: 0,
    scope: '*',
    settings: {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
      paused_validator_hotkeys: [],
    },
    reason: 'ramp the fleet to three slots now that dispatch is stable',
    actor: 'peyton@omniaura.ai',
    created_at: '2026-07-25T21:11:12Z',
    checksum: 'f'.repeat(64),
  }
  return {
    current: [revision],
    history: [revision],
    default: {
      max_concurrent_slots: 2,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
      paused_validator_hotkeys: [],
    },
    effective: {
      revision: 1,
      scope: '*',
      settings: revision.settings,
      checksum: 'f'.repeat(64),
      source: 'revision',
      hard_slot_ceiling: 8,
      disk_restricted_slots: 1,
      max_age_seconds: 5,
      ...overrides,
    },
  }
}

function fleet(): ValidatorFleet {
  return {
    generated_at: '2026-07-25T21:17:06Z',
    active_bench_version: 7,
    validators: [
      {
        validator_hotkey: primaryHotkey,
        configured_slots: 4,
        healthy_slot_count: 4,
        admission: 'accepting',
        active_benchmark_count: 3,
        online: true,
        disk_percent: 85,
        bench_serviceability: 'serving',
        orphaned_slots: [],
        updater_status: {
          enabled: true,
          channel: 'compat-2',
          state: 'backoff',
          transaction_phase: null,
          current_descriptor: `ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:${'a'.repeat(64)}`,
          current_version: '0.63.1',
          candidate_descriptor: `ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:${'b'.repeat(64)}`,
          candidate_version: '0.64.0',
          failed_candidate_count: 2,
          retry_after: 1_784_021_000,
          suppressed: false,
          last_success_at: 1_784_000_000,
          last_failure_at: 1_784_020_700,
          last_failure_reason: 'candidate_readiness_failed',
          observed_at: 1_784_020_800,
        },
      },
      {
        validator_hotkey: secondaryHotkey,
        configured_slots: 1,
        healthy_slot_count: 1,
        admission: 'accepting',
        active_benchmark_count: 0,
        online: true,
        disk_percent: 5,
        bench_serviceability: 'serving',
        orphaned_slots: [],
        updater_status: null,
      },
    ],
  }
}

function setCap(value: string) {
  fireEvent.change(screen.getByLabelText(/Concurrent slot cap/), { target: { value } })
}

function setReason(value = 'ramp to four now that three has been stable for a day') {
  fireEvent.change(screen.getByLabelText(/Operator reason/), { target: { value } })
}

function setConfirmation(value: string) {
  fireEvent.change(screen.getByLabelText(/Type to confirm/), { target: { value } })
}

describe('ValidatorSlotControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getValidatorSlotSettings.mockReset().mockResolvedValue(control())
    getValidatorFleet.mockReset().mockResolvedValue(fleet())
    updateValidatorSlotSettings.mockReset().mockResolvedValue(control())
    updateValidatorIssuancePause.mockReset().mockResolvedValue(control())
  })

  it('shows signed updater state and an old-validator compatibility fallback', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={fleet()} readOnly={false} />,
    )

    expect(screen.getByText('backoff · v0.64.0')).toBeTruthy()
    expect(screen.getByText('unreported')).toBeTruthy()
  })

  it('separates an operator revision from a setting nobody ever chose', () => {
    const { unmount } = render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )
    expect(screen.getByText(/Operator revision 1/)).toBeTruthy()
    unmount()

    render(
      <ValidatorSlotControlPanel
        initialState={{
          ...control(),
          current: [],
          history: [],
          effective: {
            ...control().effective,
            revision: 0,
            source: 'default',
            checksum: '',
            settings: {
              ...control().default,
              max_concurrent_slots: 2,
            },
          },
        }}
        initialFleet={null}
        readOnly={false}
      />,
    )
    expect(screen.getByText(/No operator revision has ever been written/)).toBeTruthy()
    expect(screen.getByText(/No revision has ever been written/)).toBeTruthy()
  })

  it('shows the append-only history with actor, reason and timestamp', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )
    expect(screen.getByText('Revision 1')).toBeTruthy()
    expect(screen.getByText('In force')).toBeTruthy()
    expect(
      screen.getByText(/ramp the fleet to three slots now that dispatch is stable/),
    ).toBeTruthy()
    expect(screen.getByText(/peyton@omniaura\.ai/)).toBeTruthy()
  })

  it('refuses a confirmation that names a different cap before any request is issued', async () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )

    setCap('4')
    setReason()
    // The cap being applied is 4; naming the outgoing 3 is exactly the mistake
    // the double statement exists to catch.
    setConfirmation('APPLY VALIDATOR SLOT CAP 3')
    fireEvent.click(screen.getByRole('button', { name: 'Apply slot policy' }))

    expect(updateValidatorSlotSettings).not.toHaveBeenCalled()
    expect(screen.getByText(/Nothing was sent/)).toBeTruthy()
  })

  it('never pre-fills the phrase with the chosen cap', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )
    setCap('4')

    expect(
      (screen.getByLabelText(/Type to confirm/) as HTMLInputElement).value,
    ).toBe('')
    // The template is shown; the resulting number never is.
    expect(screen.getByText('APPLY VALIDATOR SLOT CAP <cap>')).toBeTruthy()
    expect(screen.queryByText('APPLY VALIDATOR SLOT CAP 4')).toBeNull()
  })

  it('rejects an off-grid disk ceiling client-side and blocks the apply', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )

    fireEvent.change(screen.getByLabelText(/Disk percent ceiling/), { target: { value: '87' } })

    expect(screen.getByText(/multiple of 5/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Apply slot policy' }) as HTMLButtonElement).disabled,
    ).toBe(true)

    fireEvent.change(screen.getByLabelText(/Disk percent ceiling/), { target: { value: '85' } })
    expect(screen.queryByText(/multiple of 5/)).toBeNull()
  })

  it('refuses a cap above the protocol hard ceiling', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )

    setCap('9')
    expect(screen.getByText(/hard_slot_ceiling/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Apply slot policy' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('carries expectedRevision and the typed confirmation through to the platform', async () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )

    setCap('4')
    setReason()
    setConfirmation('APPLY VALIDATOR SLOT CAP 4')
    fireEvent.click(screen.getByRole('button', { name: 'Apply slot policy' }))

    await waitFor(() => expect(updateValidatorSlotSettings).toHaveBeenCalledTimes(1))
    expect(updateValidatorSlotSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 1,
        settings: {
          max_concurrent_slots: 4,
          disk_percent_ceiling: 90,
          memory_percent_ceiling: 90,
          cpu_percent_ceiling: 0,
          resource_block_percent_ceiling: 95,
          paused_validator_hotkeys: [],
        },
        reason: 'ramp to four now that three has been stable for a day',
        confirmation: 'APPLY VALIDATOR SLOT CAP 4',
      },
    })
  })

  it('surfaces a concurrent-write refusal instead of clobbering it', async () => {
    updateValidatorSlotSettings.mockRejectedValueOnce(
      new Error(
        'validator slot settings changed; refresh before applying (expected 1, current 2). Nothing was applied: re-read the current policy.',
      ),
    )
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )

    setCap('4')
    setReason()
    setConfirmation('APPLY VALIDATOR SLOT CAP 4')
    fireEvent.click(screen.getByRole('button', { name: 'Apply slot policy' }))

    await waitFor(() => expect(screen.getByText(/expected 1, current 2/)).toBeTruthy())
  })

  it('reads the fleet against the ceiling being considered', () => {
    render(
      <ValidatorSlotControlPanel
        initialState={control()}
        initialFleet={fleet()}
        readOnly={false}
      />,
    )

    // Nothing is disk-restricted at the 90% ceiling in force.
    expect(screen.queryByText(/at or above the \d+% ceiling/)).toBeNull()

    fireEvent.change(screen.getByLabelText(/Disk percent ceiling/), { target: { value: '80' } })
    expect(screen.getByText(/at or above the 80% ceiling, held to 1/)).toBeTruthy()
    // And it says so: the 80% reading is a preview of an unapplied ceiling.
    expect(screen.getByText(/The 90% ceiling is still\s+the one in force/)).toBeTruthy()
  })

  it('renders the cap without fleet telemetry rather than failing', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={null} readOnly={false} />,
    )
    expect(screen.getByText(/Fleet telemetry is unavailable/)).toBeTruthy()
    expect(screen.getByLabelText(/Concurrent slot cap/)).toBeTruthy()
  })

  it('keeps every control disabled for a read-only operator', () => {
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={fleet()} readOnly />,
    )
    expect((screen.getByLabelText(/Concurrent slot cap/) as HTMLInputElement).disabled).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Apply slot policy' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    for (const button of screen.getAllByRole('button', { name: 'Pause' })) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    }
  })

  it('pauses one exact validator without changing the fleet-wide slot policy', async () => {
    const paused = control({
      revision: 2,
      settings: { ...control().effective.settings, paused_validator_hotkeys: [primaryHotkey] },
    })
    updateValidatorIssuancePause.mockResolvedValueOnce(paused)
    render(
      <ValidatorSlotControlPanel initialState={control()} initialFleet={fleet()} readOnly={false} />,
    )

    fireEvent.click(screen.getAllByRole('button', { name: 'Pause' })[0])
    fireEvent.change(screen.getAllByLabelText(/Operator reason/)[0], {
      target: { value: 'drain this validator after repeated benchmark stalls' },
    })
    fireEvent.change(screen.getAllByLabelText(/Type to confirm/)[0], {
      target: { value: `PAUSE VALIDATOR ${primaryHotkey}` },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Pause new issuance' }))

    await waitFor(() => expect(updateValidatorIssuancePause).toHaveBeenCalledTimes(1))
    expect(updateValidatorIssuancePause).toHaveBeenCalledWith({
      data: {
        validatorHotkey: primaryHotkey,
        paused: true,
        expectedRevision: 1,
        reason: 'drain this validator after repeated benchmark stalls',
        confirmation: `PAUSE VALIDATOR ${primaryHotkey}`,
      },
    })
    expect(updateValidatorSlotSettings).not.toHaveBeenCalled()
  })

  it('renders a paused validator as drained and offers an exact resume', () => {
    render(
      <ValidatorSlotControlPanel
        initialState={control({
          settings: {
            ...control().effective.settings,
            paused_validator_hotkeys: [primaryHotkey],
          },
        })}
        initialFleet={fleet()}
        readOnly={false}
      />,
    )

    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy()
    expect(screen.getByText('new leases paused')).toBeTruthy()
  })

  it('labels the fleet with the benchmark version being scored', () => {
    render(
      <ValidatorSlotControlPanel
        initialState={control()}
        initialFleet={fleet()}
        readOnly={false}
      />,
    )
    expect(screen.getByText('Scoring v7')).toBeTruthy()
  })

  it('omits the era label when the platform does not report an active bench version', () => {
    render(
      <ValidatorSlotControlPanel
        initialState={control()}
        initialFleet={{ ...fleet(), active_bench_version: null }}
        readOnly={false}
      />,
    )
    expect(screen.queryByText(/^Scoring v/)).toBeNull()
  })

  it('flags a validator that cannot serve the active benchmark', () => {
    const obsolete = {
      ...fleet().validators[0],
      bench_serviceability: 'software_obsolete' as const,
    }
    render(
      <ValidatorSlotControlPanel
        initialState={control()}
        initialFleet={{ ...fleet(), validators: [obsolete] }}
        readOnly={false}
      />,
    )
    expect(screen.getByText('software obsolete')).toBeTruthy()
  })

  it('counts orphaned slots as occupied, not free', () => {
    const orphaned = {
      ...fleet().validators[0],
      orphaned_slots: [
        {
          agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
          agent_name: 'mnemox-v55',
          bench_version: 7,
          evicted_at: '2026-07-28T11:00:00Z',
          original_deadline: '2026-07-28T12:30:00Z',
          orphaned_for_seconds: 3600,
          protocol_version: 16,
          reason: 'validator_still_claims_slot',
          slot_id: 'slot-2',
          state: 'still_running' as const,
        },
      ],
    }
    render(
      <ValidatorSlotControlPanel
        initialState={control()}
        initialFleet={{ ...fleet(), validators: [orphaned] }}
        readOnly={false}
      />,
    )
    expect(screen.getByText(/1 orphaned/)).toBeTruthy()
  })
})
