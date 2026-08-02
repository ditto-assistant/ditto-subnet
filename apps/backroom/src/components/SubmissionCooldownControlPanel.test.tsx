// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  submissionSettingsConfirmation,
  type SubmissionSettingsControl,
} from '../lib/admin.schemas'
import { SubmissionCooldownControlPanel } from './SubmissionCooldownControlPanel'

const getSubmissionSettingsControl = vi.fn()
const setSubmissionSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getSubmissionSettingsControl: () => getSubmissionSettingsControl(),
  setSubmissionSettings: (input: unknown) => setSubmissionSettings(input),
}))

const initial: SubmissionSettingsControl = {
  current: {
    revision: 1,
    parent_revision: 0,
    cooldown_seconds: 3600,
    fee_amount_rao: 40_000_000,
    reason: 'Initialize existing one-hour submission cooldown',
    actor: 'migration',
    created_at: '2026-07-24T12:00:00Z',
  },
  history: [],
}

describe('SubmissionCooldownControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getSubmissionSettingsControl.mockReset().mockResolvedValue(initial)
    setSubmissionSettings.mockReset().mockResolvedValue({
      current: { ...initial.current, revision: 2, parent_revision: 1, cooldown_seconds: 1800 },
      history: [],
    })
  })

  it('requires reason and exact confirmation before applying', async () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /30 minutes/ }))
    const action = screen.getByRole('button', { name: 'Apply settings' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'reduce cadence for the current capacity window' },
    })
    const expected = submissionSettingsConfirmation(1800, 40_000_000)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(action)

    await waitFor(() => expect(setSubmissionSettings).toHaveBeenCalledTimes(1))
    expect(setSubmissionSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 1,
        cooldownSeconds: 1800,
        feeAmountRao: 40_000_000,
        reason: 'reduce cadence for the current capacity window',
        confirmation: expected,
      },
    })
  })

  it('keeps write controls disabled for read-only operators', () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly />)

    expect((screen.getByRole('button', { name: /30 minutes/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByLabelText(/Cooldown in minutes/) as HTMLInputElement).disabled).toBe(true)
  })

  it('allows changing the TAO fee without changing the cooldown', async () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.05' },
    })
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'set the current miner submission fee' },
    })
    const expected = submissionSettingsConfirmation(3600, 50_000_000)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }))

    await waitFor(() => expect(setSubmissionSettings).toHaveBeenCalledTimes(1))
    expect(setSubmissionSettings).toHaveBeenCalledWith({
      data: expect.objectContaining({ cooldownSeconds: 3600, feeAmountRao: 50_000_000 }),
    })
  })
})
