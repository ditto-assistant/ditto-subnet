// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BURN_CONFIRMATION, type BurnSettingsControl } from '../lib/admin.schemas'
import { BurnControlPanel } from './BurnControlPanel'

const getBurnSettings = vi.fn()
const updateBurnSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getBurnSettings: () => getBurnSettings(),
  updateBurnSettings: (input: unknown) => updateBurnSettings(input),
}))

const control = (burn_share: number, overrides: Record<string, unknown> = {}) =>
  ({
    current: [],
    history: [],
    default: { burn_share: 0 },
    effective: {
      revision: 3,
      scope: '*',
      settings: { burn_share },
      checksum: 'ab'.repeat(32),
      source: 'revision',
      max_age_seconds: 5,
      miner_emission_share: 1 - burn_share,
      min_burn_share: 0,
      max_burn_share: 1,
      live_validator_count: 4,
      ...overrides,
    },
  }) as BurnSettingsControl

describe('BurnControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getBurnSettings.mockReset().mockResolvedValue(control(0))
    updateBurnSettings.mockReset().mockResolvedValue(control(0.25))
  })

  it('requires a reason and the exact confirmation before applying', async () => {
    render(<BurnControlPanel initialState={control(0)} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /Burn 25%/ }))
    const action = screen.getByRole('button', { name: 'Apply burn' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'owner-approved burn for the treasury window' },
    })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText(new RegExp(BURN_CONFIRMATION)), {
      target: { value: BURN_CONFIRMATION },
    })
    fireEvent.click(action)

    await waitFor(() => expect(updateBurnSettings).toHaveBeenCalledTimes(1))
    expect(updateBurnSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 3,
        settings: { burn_share: 0.25 },
        reason: 'owner-approved burn for the treasury window',
        confirmation: BURN_CONFIRMATION,
      },
    })
  })

  it('will not submit the share that is already in force', () => {
    render(<BurnControlPanel initialState={control(0.25)} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'reapply the share already in force' },
    })
    // No confirmation field is even offered for a no-op, so the guard holds
    // without depending on the operator noticing.
    expect(screen.queryByLabelText(new RegExp(BURN_CONFIRMATION))).toBeNull()
    expect(
      (screen.getByRole('button', { name: 'Apply burn' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('refuses a share the platform would reject', () => {
    render(<BurnControlPanel initialState={control(0)} readOnly={false} />)

    fireEvent.change(screen.getByLabelText(/Burn percentage/), {
      target: { value: '140' },
    })
    expect(screen.getByText(/Enter a percentage from 0 through 100/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Apply burn' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('warns when no validator is folding the policy', () => {
    render(
      <BurnControlPanel
        initialState={control(0, { live_validator_count: 0 })}
        readOnly={false}
      />,
    )
    expect(screen.getByText(/No validator has heartbeated recently/)).toBeTruthy()
  })

  it('keeps write controls disabled for read-only operators', () => {
    render(<BurnControlPanel initialState={control(0)} readOnly />)

    expect(
      (screen.getByRole('button', { name: /Burn 25%/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect((screen.getByLabelText(/Burn percentage/) as HTMLInputElement).disabled).toBe(
      true,
    )
  })
})
