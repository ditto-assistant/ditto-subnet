// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  formatRaoAsTao,
  parseTaoToRao,
  submissionSettingsConfirmation,
  submissionSettingsControlSchema,
  type SubmissionSettingsControl,
  type SubmissionSettingsPreview,
} from '../lib/admin.schemas'
import { SubmissionCooldownControlPanel } from './SubmissionCooldownControlPanel'

const getSubmissionSettingsControl = vi.fn()
const previewSubmissionSettingsChange = vi.fn()
const setSubmissionSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getSubmissionSettingsControl: () => getSubmissionSettingsControl(),
  previewSubmissionSettingsChange: (input: unknown) => previewSubmissionSettingsChange(input),
  setSubmissionSettings: (input: unknown) => setSubmissionSettings(input),
}))

const initial: SubmissionSettingsControl = submissionSettingsControlSchema.parse({
  current: {
    revision: 1,
    parent_revision: 0,
    cooldown_seconds: 3600,
    fee_amount_rao: 40_000_000,
    fee_amount_tao: '0.040000000',
    fee_denomination: 'fixed_tao',
    previous_fee_amount_rao: null,
    previous_cooldown_seconds: null,
    reason: 'Initialize existing one-hour submission cooldown',
    actor: 'migration',
    created_at: '2026-07-24T12:00:00Z',
  },
  history: [],
  bounds: {
    min_fee_amount_rao: 1_000_000,
    max_fee_amount_rao: 10_000_000_000,
    min_cooldown_seconds: 60,
    max_cooldown_seconds: 86_400,
  },
  quote_lifetime_seconds: 86_400,
})

function previewFor(input: {
  data: { expectedRevision: number; cooldownSeconds: number; feeAmountRao: number }
}): SubmissionSettingsPreview {
  const { expectedRevision, cooldownSeconds, feeAmountRao } = input.data
  return {
    current: initial.current,
    proposed: {
      cooldown_seconds: cooldownSeconds,
      fee_amount_rao: feeAmountRao,
      fee_amount_tao: '0.000000000',
      fee_denomination: 'fixed_tao',
    },
    expected_revision: expectedRevision,
    stale: false,
    fee_changed: feeAmountRao !== 40_000_000,
    cooldown_changed: cooldownSeconds !== 3600,
    fee_change_ratio: feeAmountRao !== 40_000_000 ? '1.2500' : null,
    applicable: true,
    required_confirmation: submissionSettingsConfirmation(cooldownSeconds, feeAmountRao),
    bounds: initial.bounds,
    quote_lifetime_seconds: 86_400,
    in_flight_quotes: 3,
    in_flight_quotes_at_other_fees: 2,
    in_flight_quotes_expire_by: '2026-07-25T12:00:00Z',
  }
}

describe('SubmissionCooldownControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getSubmissionSettingsControl.mockReset().mockResolvedValue(initial)
    previewSubmissionSettingsChange.mockReset().mockImplementation(async (input) => previewFor(input))
    setSubmissionSettings.mockReset().mockResolvedValue({
      ...initial,
      current: { ...initial.current, revision: 2, parent_revision: 1, cooldown_seconds: 1800 },
    })
  })

  it('requires a preview, reason, and exact confirmation before applying', async () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /30 minutes/ }))
    const action = screen.getByRole('button', { name: 'Apply settings' })
    expect((action as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByLabelText('Operator reason')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }))
    await waitFor(() => expect(previewSubmissionSettingsChange).toHaveBeenCalledTimes(1))
    expect(previewSubmissionSettingsChange).toHaveBeenCalledWith({
      data: { expectedRevision: 1, cooldownSeconds: 1800, feeAmountRao: 40_000_000 },
    })
    await screen.findByLabelText('Change preview')
    expect(screen.getByText(/2 of 3 keep an earlier fee/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'reduce cadence for the current capacity window' },
    })
    const expected = submissionSettingsConfirmation(1800, 40_000_000)
    expect((action as HTMLButtonElement).disabled).toBe(true)
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

  it('changes the TAO fee exactly, without floating-point rounding', async () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.037271710' },
    })
    expect(screen.getByText('Exactly 37,271,710 rao')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }))
    await screen.findByLabelText('Change preview')
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'set the current miner submission fee' },
    })
    const expected = submissionSettingsConfirmation(3600, 37_271_710)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }))

    await waitFor(() => expect(setSubmissionSettings).toHaveBeenCalledTimes(1))
    expect(setSubmissionSettings).toHaveBeenCalledWith({
      data: expect.objectContaining({ cooldownSeconds: 3600, feeAmountRao: 37_271_710 }),
    })
  })

  it('refuses a fee outside the safe bounds or finer than one rao', () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)
    const input = screen.getByLabelText('Submission fee in TAO')
    const preview = screen.getByRole('button', { name: 'Preview change' }) as HTMLButtonElement

    for (const value of ['0.0000001', '11', '0.0400000001', '-1', '4e-2', 'abc']) {
      fireEvent.change(input, { target: { value } })
      expect(preview.disabled, value).toBe(true)
      expect(input.getAttribute('aria-invalid'), value).toBe('true')
    }
  })

  it('does not enable apply for a stale preview', async () => {
    previewSubmissionSettingsChange.mockImplementation(async (input) => ({
      ...previewFor(input),
      stale: true,
      applicable: false,
    }))
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)

    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.05' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }))
    await screen.findByText(/Refresh before applying/)
    expect(screen.queryByLabelText('Operator reason')).toBeNull()
    expect(
      (screen.getByRole('button', { name: 'Apply settings' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('shows old and new values in the revision history', () => {
    const withHistory = submissionSettingsControlSchema.parse({
      ...initial,
      history: [
        {
          ...initial.current,
          revision: 3,
          parent_revision: 2,
          fee_amount_rao: 37_271_710,
          previous_fee_amount_rao: 100_000_000,
          actor: 'operator@example.com',
          reason: 'measured platform cost',
        },
      ],
    })
    render(<SubmissionCooldownControlPanel initialState={withHistory} readOnly />)

    expect(screen.getByText(/Revision 3 · 0.1 → 0.03727171 TAO/)).toBeTruthy()
    expect(screen.getByText('operator@example.com: measured platform cost')).toBeTruthy()
  })
})

describe('exact TAO conversion', () => {
  it('round-trips rao without floating point', () => {
    expect(parseTaoToRao('0.037271710')).toBe(37_271_710)
    expect(parseTaoToRao('0.1')).toBe(100_000_000)
    expect(parseTaoToRao('1')).toBe(1_000_000_000)
    expect(parseTaoToRao('0.000000001')).toBe(1)
    // 0.1 + 0.2 style float drift must never reach a fee.
    expect(parseTaoToRao('0.3')).toBe(300_000_000)
    expect(parseTaoToRao('0.0000000001')).toBeNull()
    expect(parseTaoToRao('1e-3')).toBeNull()
    expect(parseTaoToRao('-0.1')).toBeNull()
    expect(formatRaoAsTao(37_271_710)).toBe('0.03727171')
    expect(formatRaoAsTao(1_000_000_000)).toBe('1')
    expect(formatRaoAsTao(1)).toBe('0.000000001')
  })

  it('defaults a missing denomination to fixed_tao and rejects any other', () => {
    const { fee_denomination: _omitted, ...legacy } = initial.current
    expect(
      submissionSettingsControlSchema.parse({ current: legacy, history: [] }).current
        .fee_denomination,
    ).toBe('fixed_tao')
    expect(() =>
      submissionSettingsControlSchema.parse({
        current: { ...initial.current, fee_denomination: 'usd_indexed' },
        history: [],
      }),
    ).toThrow()
  })
})
