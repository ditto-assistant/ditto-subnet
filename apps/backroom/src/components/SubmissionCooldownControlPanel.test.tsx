// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  formatRaoAsTao,
  parseTaoToRao,
  parseTaoToRaoExact,
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
  history_incomplete: false,
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
    recoverable_expired_quotes: 1,
    recoverable_expired_quotes_at_other_fees: 1,
    recoverable_expired_quotes_until: '2026-07-25T13:00:00Z',
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
    expect(screen.getByText(/Plus up to 1 recently expired/)).toBeTruthy()

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

  it('derives every hint and input limit from the server bounds', () => {
    const narrow = submissionSettingsControlSchema.parse({
      ...initial,
      bounds: {
        min_fee_amount_rao: 20_000_000,
        max_fee_amount_rao: 500_000_000,
        min_cooldown_seconds: 90,
        max_cooldown_seconds: 7_230,
      },
    })
    render(<SubmissionCooldownControlPanel initialState={narrow} readOnly={false} />)

    const minutes = screen.getByLabelText(/Cooldown in minutes \(2–120\)/) as HTMLInputElement
    expect(minutes.min).toBe('2')
    expect(minutes.max).toBe('120')
    fireEvent.change(minutes, { target: { value: '121' } })
    expect(screen.getByText('Enter a whole number from 2 through 120 minutes.')).toBeTruthy()
    expect(screen.queryByText(/1 through 1440/)).toBeNull()
    fireEvent.change(minutes, { target: { value: '1' } })
    expect(minutes.getAttribute('aria-invalid')).toBe('true')
    fireEvent.change(minutes, { target: { value: '120' } })
    expect(minutes.getAttribute('aria-invalid')).toBe('false')

    const fee = screen.getByLabelText('Submission fee in TAO')
    fireEvent.change(fee, { target: { value: '0.01' } })
    expect(
      screen.getByText('Enter 0.02 to 0.5 TAO.'),
    ).toBeTruthy()
    fireEvent.change(fee, { target: { value: '0.6' } })
    expect(fee.getAttribute('aria-invalid')).toBe('true')
    fireEvent.change(fee, { target: { value: '0.5' } })
    expect(fee.getAttribute('aria-invalid')).toBe('false')
  })

  it('lets an invalid draft be cancelled back to the applied values', () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)
    const cancel = screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
    const fee = screen.getByLabelText('Submission fee in TAO') as HTMLInputElement
    const minutes = screen.getByLabelText(/Cooldown in minutes/) as HTMLInputElement
    expect(cancel.disabled).toBe(true)

    fireEvent.change(fee, { target: { value: 'abc' } })
    fireEvent.change(minutes, { target: { value: '0' } })
    expect(fee.getAttribute('aria-invalid')).toBe('true')
    expect(
      (screen.getByRole('button', { name: 'Preview change' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(cancel.disabled).toBe(false)

    fireEvent.click(cancel)
    expect(fee.value).toBe('0.04')
    expect(minutes.value).toBe('60')
    expect(fee.getAttribute('aria-invalid')).toBe('false')
    expect(cancel.disabled).toBe(true)
  })

  it('shows a neutral time for history rows without a timestamp', () => {
    const control = submissionSettingsControlSchema.parse({
      ...initial,
      current: { ...initial.current, revision: 0, created_at: null },
      history: [
        { ...initial.current, revision: 2, created_at: null },
        { ...initial.current, revision: 1, created_at: 'not-a-date' },
      ],
    })
    render(<SubmissionCooldownControlPanel initialState={control} readOnly />)

    // Only the effective policy can be the built-in default.
    expect(screen.getAllByText('Built-in default')).toHaveLength(1)
    const rows = screen.getByRole('list', { name: 'Submission settings revisions' })
    expect(rows.textContent).not.toContain('Built-in default')
    expect(rows.textContent?.match(/—/g)).toHaveLength(2)
  })

  it('keeps a non-minute applied cooldown valid for a fee-only change', async () => {
    const control = submissionSettingsControlSchema.parse({
      ...initial,
      current: { ...initial.current, cooldown_seconds: 90 },
    })
    render(<SubmissionCooldownControlPanel initialState={control} readOnly={false} />)
    const minutes = screen.getByLabelText(/Cooldown in minutes/) as HTMLInputElement
    expect(minutes.value).toBe('1.5')
    expect(minutes.getAttribute('aria-invalid')).toBe('false')

    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.05' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }))
    await waitFor(() => expect(previewSubmissionSettingsChange).toHaveBeenCalledTimes(1))
    expect(previewSubmissionSettingsChange).toHaveBeenCalledWith({
      data: { expectedRevision: 1, cooldownSeconds: 90, feeAmountRao: 50_000_000 },
    })
    // A newly typed fractional minute is still refused.
    fireEvent.change(minutes, { target: { value: '2.5' } })
    expect(minutes.getAttribute('aria-invalid')).toBe('true')
  })

  it('names the pending action and keeps the draft across a stale refresh', async () => {
    let releasePreview: (value: SubmissionSettingsPreview) => void = () => undefined
    previewSubmissionSettingsChange.mockImplementation(
      (input) =>
        new Promise<SubmissionSettingsPreview>((resolve) => {
          releasePreview = (value) => resolve(value)
          void input
        }),
    )
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)
    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.05' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview change' }))
    // Previewing is not applying.
    expect(screen.queryByText('Applying…')).toBeNull()
    releasePreview({
      ...previewFor({
        data: { expectedRevision: 1, cooldownSeconds: 3600, feeAmountRao: 50_000_000 },
      }),
      stale: true,
      applicable: false,
    })
    await screen.findByText(/your draft is kept/)

    getSubmissionSettingsControl.mockResolvedValueOnce({
      ...initial,
      current: { ...initial.current, revision: 2 },
    })
    fireEvent.click(screen.getByRole('button', { name: /Refresh policy/ }))
    await waitFor(() => expect(getSubmissionSettingsControl).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect((screen.getByLabelText('Submission fee in TAO') as HTMLInputElement).value).toBe(
        '0.05',
      ),
    )
    expect(screen.queryByLabelText('Change preview')).toBeNull()
  })

  it('notes when Platform could not return the whole revision history', () => {
    const control = submissionSettingsControlSchema.parse({
      ...initial,
      history: [{ ...initial.current }],
      history_incomplete: true,
    })
    render(<SubmissionCooldownControlPanel initialState={control} readOnly />)
    expect(screen.getByText('Some revisions are not shown.')).toBeTruthy()
    cleanup()
    render(
      <SubmissionCooldownControlPanel
        initialState={{ ...control, history_incomplete: false }}
        readOnly
      />,
    )
    expect(screen.queryByText('Some revisions are not shown.')).toBeNull()
  })

  it('re-seeds an untouched non-minute cooldown when refresh finds a new one', async () => {
    const control = submissionSettingsControlSchema.parse({
      ...initial,
      current: { ...initial.current, cooldown_seconds: 90 },
    })
    // Another operator meanwhile set the cooldown to 150 s.
    getSubmissionSettingsControl.mockResolvedValueOnce({
      ...control,
      current: { ...control.current, revision: 2, cooldown_seconds: 150 },
    })
    render(<SubmissionCooldownControlPanel initialState={control} readOnly={false} />)
    // Only the fee is edited; the cooldown field stays untouched at "1.5".
    fireEvent.change(screen.getByLabelText('Submission fee in TAO'), {
      target: { value: '0.05' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Refresh policy/ }))
    await waitFor(() => expect(getSubmissionSettingsControl).toHaveBeenCalledTimes(1))

    const minutes = screen.getByLabelText(/Cooldown in minutes/) as HTMLInputElement
    await waitFor(() => expect(minutes.value).toBe('2.5'))
    expect(minutes.getAttribute('aria-invalid')).toBe('false')
    expect((screen.getByLabelText('Submission fee in TAO') as HTMLInputElement).value).toBe(
      '0.05',
    )
  })

  it('reports out-of-range amounts with the bounds and malformed text as format', () => {
    render(<SubmissionCooldownControlPanel initialState={initial} readOnly={false} />)
    const fee = screen.getByLabelText('Submission fee in TAO')
    for (const value of ['12345', '99999999999999', '0.0000001', '11']) {
      fireEvent.change(fee, { target: { value } })
      expect(screen.getByText('Enter 0.001 to 10 TAO.'), value).toBeTruthy()
    }
    for (const value of ['abc', '-1', '4e-2', '0.0400000001', '1,5']) {
      fireEvent.change(fee, { target: { value } })
      expect(
        screen.getByText('Enter a TAO amount as digits with at most nine decimals.'),
        value,
      ).toBeTruthy()
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
    await screen.findByText(/your draft is kept/)
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
    // Well-formed but large amounts parse; range is the bounds check's job.
    expect(parseTaoToRao('12345')).toBe(12_345_000_000_000)
    expect(parseTaoToRaoExact('99999999999999')).toBe(99_999_999_999_999_000_000_000n)
    expect(parseTaoToRao('99999999999999')).toBeNull()
    expect(parseTaoToRao('-0.1')).toBeNull()
    expect(formatRaoAsTao(37_271_710)).toBe('0.03727171')
    expect(formatRaoAsTao(1_000_000_000)).toBe('1')
    expect(formatRaoAsTao(1)).toBe('0.000000001')
  })

  it('rejects a revision that omits or changes the denomination', () => {
    const { fee_denomination: _omitted, ...legacy } = initial.current
    expect(() =>
      submissionSettingsControlSchema.parse({ ...initial, current: legacy }),
    ).toThrow()
    expect(() =>
      submissionSettingsControlSchema.parse({
        ...initial,
        current: { ...initial.current, fee_denomination: 'usd_indexed' },
      }),
    ).toThrow()
  })

  it('accepts exact decimals written with a bare leading or trailing point', () => {
    expect(parseTaoToRao('.5')).toBe(500_000_000)
    expect(parseTaoToRao('0.')).toBe(0)
    expect(parseTaoToRao('1.')).toBe(1_000_000_000)
    expect(parseTaoToRao('.')).toBeNull()
    expect(parseTaoToRao('')).toBeNull()
  })

})
