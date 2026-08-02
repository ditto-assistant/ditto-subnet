// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CONTINUAL_RETEST_CONFIRMATION,
  parseContinualRetestSettingsControl,
  type ContinualRetestSettingsControl,
} from '../lib/admin.schemas'
import { ContinualRetestControlPanel } from './ContinualRetestControlPanel'

const getContinualRetestSettings = vi.fn()
const updateContinualRetestSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getContinualRetestSettings: () => getContinualRetestSettings(),
  updateContinualRetestSettings: (input: unknown) => updateContinualRetestSettings(input),
}))

const SETTINGS: ContinualRetestSettingsControl['effective']['settings'] = {
  aggregate_mode: 'fleet_ready',
  idle_retests_enabled: false,
  rollout_standdown: 'capable_validators',
  wave_membership: 'participants',
  retest_cohort_size: 5,
  retest_eligibility_mode: 'fixed',
  retest_eligibility_z: 1.64,
  retest_cohort_max_size: 25,
}

function control(
  effective: Partial<ContinualRetestSettingsControl['effective']> = {},
): ContinualRetestSettingsControl {
  return {
    current: [],
    history: [],
    default: SETTINGS,
    effective: {
      revision: 3,
      scope: '*',
      settings: SETTINGS,
      checksum: 'a'.repeat(64),
      source: 'revision',
      fleet_protocol_ready: true,
      aggregate_active: true,
      max_age_seconds: 5,
      open_rollout_desired_version: null,
      rollout_standdown_active: false,
      emission_set_size: 5,
      max_retest_cohort_size: 25,
      max_retest_eligibility_z: 3,
      eligible_agent_count: 40,
      resolved_cohort_size: 5,
      ...effective,
    },
    field_support: {
      retest_cohort_size: true,
      wave_membership: true,
      retest_eligibility_mode: true,
      retest_eligibility_z: true,
      retest_cohort_max_size: true,
    },
    cohort_sizing_supported: true,
  }
}

describe('ContinualRetestControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getContinualRetestSettings.mockReset().mockResolvedValue(control())
    updateContinualRetestSettings.mockReset().mockResolvedValue(control())
  })

  it('explains an active stand-down instead of looking broken', () => {
    render(
      <ContinualRetestControlPanel
        initialState={control({
          open_rollout_desired_version: 7,
          rollout_standdown_active: true,
        })}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Standing down')).toBeTruthy()
    expect(screen.getByText(/Retests are paused, not broken/)).toBeTruthy()
    expect(screen.getByText(/lifts on\s+its own/)).toBeTruthy()
  })

  it('reports a running lane when no rollout is open', () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    expect(screen.getByText('Running')).toBeTruthy()
    expect(screen.queryByText(/Retests are paused/)).toBeNull()
  })

  it('warns before overriding the stand-down during an open rollout', () => {
    render(
      <ContinualRetestControlPanel
        initialState={control({
          open_rollout_desired_version: 7,
          rollout_standdown_active: true,
        })}
        readOnly={false}
      />,
    )

    expect(screen.queryByText(/will slow it down/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Never yield/ }))
    expect(screen.getByText(/will slow it down/)).toBeTruthy()
  })

  it('applies the stand-down mode with a reason and exact confirmation', async () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    const action = screen.getByRole('button', { name: 'Apply policy' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: /Yield every slot/ }))
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'pause all rescoring for the v7 rollout window' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })
    fireEvent.click(action)

    await waitFor(() => expect(updateContinualRetestSettings).toHaveBeenCalledTimes(1))
    // The whole policy goes on the wire, not just the knob that moved: a
    // revision stores all eight fields and an omission is a write of the
    // default.
    expect(updateContinualRetestSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 3,
        settings: { ...SETTINGS, rollout_standdown: 'all' },
        reason: 'pause all rescoring for the v7 rollout window',
        confirmation: CONTINUAL_RETEST_CONFIRMATION,
      },
    })
  })

  it('widens the retest cohort past the emission set', async () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    expect(screen.getByText(/of 40 ranked/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/Cohort size/), { target: { value: '25' } })
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'retest the top 25 while the field is deep' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply policy' }))

    await waitFor(() => expect(updateContinualRetestSettings).toHaveBeenCalledTimes(1))
    expect(updateContinualRetestSettings.mock.calls[0][0].data.settings).toEqual({
      ...SETTINGS,
      retest_cohort_size: 25,
    })
  })

  it('refuses a cohort size the platform would reject', () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    fireEvent.change(screen.getByLabelText(/Cohort size/), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'retest far more than the platform allows' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })

    expect(screen.getByText(/must be a whole number between 5 and 25/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Apply policy' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('says so when the ranking cannot fill the requested cohort', () => {
    render(
      <ContinualRetestControlPanel
        initialState={control({ eligible_agent_count: 9 })}
        readOnly={false}
      />,
    )

    fireEvent.change(screen.getByLabelText(/Cohort size/), { target: { value: '25' } })

    expect(screen.getByText(/Only 9 ranked agents exist/)).toBeTruthy()
  })

  it('keeps stand-down controls disabled for read-only operators', () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly />)

    expect(
      (screen.getByRole('button', { name: /Yield every slot/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  const legacy = () =>
    parseContinualRetestSettingsControl({
      current: [],
      history: [],
      default: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
      effective: {
        revision: 3,
        scope: '*',
        settings: { aggregate_mode: 'fleet_ready', idle_retests_enabled: false },
        checksum: 'a'.repeat(64),
        source: 'revision',
        fleet_protocol_ready: true,
        aggregate_active: true,
        max_age_seconds: 5,
      },
    })

  it('renders against a platform that predates the stand-down contract', () => {
    // The platform owns the contract and ships the fields first; a backroom
    // deployed ahead of it must degrade to the safe default, not blank the page.
    const state = legacy()

    expect(state.effective.settings.rollout_standdown).toBe('capable_validators')
    expect(state.effective.rollout_standdown_active).toBe(false)
    // A platform without the cohort dial is a platform retesting the top five.
    expect(state.effective.settings.retest_cohort_size).toBe(5)
    expect(state.effective.eligible_agent_count).toBeNull()
    expect(state.cohort_sizing_supported).toBe(false)
    render(<ContinualRetestControlPanel initialState={state} readOnly={false} />)
    expect(screen.getByText('Running')).toBeTruthy()
  })

  it('parks the cohort dial when the platform has no cohort-size field', () => {
    // Leaving it live would let an operator type 25, satisfy every local check,
    // and get back a bare "request validation failed" from the platform.
    render(<ContinualRetestControlPanel initialState={legacy()} readOnly={false} />)

    expect(
      (screen.getByLabelText(/Cohort size/) as HTMLInputElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Top 25' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(screen.getByText(/no cohort-size field yet/)).toBeTruthy()
  })

  it('still applies the rest of the policy on a platform without cohort sizing', async () => {
    render(<ContinualRetestControlPanel initialState={legacy()} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /Yield every slot/ }))
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'pause all rescoring for the v7 rollout window' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply policy' }))

    await waitFor(() => expect(updateContinualRetestSettings).toHaveBeenCalledTimes(1))
    // Every extended field is sent at the value that build already behaves as,
    // so the server helper can drop them without changing anything.
    expect(updateContinualRetestSettings.mock.calls[0][0].data.settings).toEqual({
      aggregate_mode: 'fleet_ready',
      idle_retests_enabled: false,
      rollout_standdown: 'all',
      wave_membership: 'strict',
      retest_cohort_size: 5,
      retest_eligibility_mode: 'fixed',
      retest_eligibility_z: 1.64,
      retest_cohort_max_size: 25,
    })
  })

  it('parks the fold and tie-band controls on a platform that has neither', () => {
    // The fold rule is the one an operator most wants to reach — strict is the
    // rollback path — so an inert control has to say why rather than look
    // merely unresponsive.
    render(<ContinualRetestControlPanel initialState={legacy()} readOnly={false} />)

    expect(
      (screen.getByRole('button', { name: /Per agent/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(screen.getByText(/no wave-membership field yet/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: /Tie tolerant/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect((screen.getByLabelText(/Cohort ceiling/) as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText(/no tie-tolerance fields yet/)).toBeTruthy()
  })

  it('applies a rollback to the strict fold', async () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /Strict \(rollback\)/ }))
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'roll the fold back to strict while we investigate' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply policy' }))

    await waitFor(() => expect(updateContinualRetestSettings).toHaveBeenCalledTimes(1))
    expect(updateContinualRetestSettings.mock.calls[0][0].data.settings).toEqual({
      ...SETTINGS,
      wave_membership: 'strict',
    })
  })

  it('refuses a ceiling that cuts into the rank cutoff', () => {
    render(<ContinualRetestControlPanel initialState={control()} readOnly={false} />)

    fireEvent.change(screen.getByLabelText(/Cohort size/), { target: { value: '20' } })
    fireEvent.change(screen.getByLabelText(/Cohort ceiling/), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'ask for a ceiling below the cohort on purpose' },
    })
    fireEvent.change(screen.getByLabelText(new RegExp(CONTINUAL_RETEST_CONFIRMATION)), {
      target: { value: CONTINUAL_RETEST_CONFIRMATION },
    })

    expect(screen.getByText(/cannot sit below the cohort size/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Apply policy' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('shows what the tie band actually admitted, not just what was asked for', () => {
    render(
      <ContinualRetestControlPanel
        initialState={control({
          settings: { ...SETTINGS, retest_eligibility_mode: 'statistical' },
          resolved_cohort_size: 8,
        })}
        readOnly={false}
      />,
    )

    expect(screen.getByText(/→ 8 admitted/)).toBeTruthy()
  })
})
