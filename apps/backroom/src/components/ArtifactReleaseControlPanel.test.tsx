// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  artifactReleaseConfirmation,
  type ArtifactReleaseControl,
} from '../lib/admin.schemas'
import { ArtifactReleaseControlPanel } from './ArtifactReleaseControlPanel'

const getArtifactReleaseControl = vi.fn()
const setArtifactReleaseSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getArtifactReleaseControl: () => getArtifactReleaseControl(),
  setArtifactReleaseSettings: (input: unknown) => setArtifactReleaseSettings(input),
}))

const initial: ArtifactReleaseControl = {
  current: {
    revision: 0,
    parent_revision: 0,
    disclosure: 'public',
    embargo_hours: 24,
    reason: 'Built-in privacy-first default',
    actor: 'platform',
    created_at: null,
  },
  history: [],
}

describe('ArtifactReleaseControlPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getArtifactReleaseControl.mockReset().mockResolvedValue(initial)
    setArtifactReleaseSettings.mockReset().mockResolvedValue({
      current: {
        ...initial.current,
        revision: 1,
        embargo_hours: 12,
        reason: 'screening capacity is ready for staged release',
        actor: 'operator@example.com',
        created_at: '2026-07-24T12:00:00Z',
      },
      history: [],
    })
  })

  it('shows the 24-hour default and irreversible retroactive warning', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    expect(screen.getAllByText('24 hours')).toHaveLength(2)
    expect(screen.getByText(/Shortening is retroactive/)).toBeTruthy()
    expect(screen.getByText(/cannot be made private again/)).toBeTruthy()
  })

  it('scopes the release to the leaderboard king only', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    expect(screen.getByText(/leaderboard king only/)).toBeTruthy()
    expect(screen.getByText(/No other miner’s source is\s+ever released/)).toBeTruthy()
    expect(screen.queryByText(/all cleared submissions/)).toBeNull()
  })

  it('requires reason and exact confirmation before shortening', async () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /12 hours/ }))
    const action = screen.getByRole('button', { name: 'Shorten embargo' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'screening capacity is ready for staged release' },
    })
    const expected = artifactReleaseConfirmation(12)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        disclosure: 'public',
        embargoHours: 12,
        reason: 'screening capacity is ready for staged release',
        confirmation: expected,
      },
    })
    expect(await screen.findByText('Source embargo shortened to 12 hours.')).toBeTruthy()
  })

  it('extends the window to a longer preset than the current default', async () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /48 hours/ }))
    const action = screen.getByRole('button', { name: 'Extend embargo' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'hold cleared source private for the launch window' },
    })
    const expected = artifactReleaseConfirmation(48)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(action)

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        disclosure: 'public',
        embargoHours: 48,
        reason: 'hold cleared source private for the launch window',
        confirmation: expected,
      },
    })
    expect(await screen.findByText('Source embargo extended to 48 hours.')).toBeTruthy()
  })

  it('accepts a custom in-range window and rejects an out-of-range one', async () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    const custom = screen.getByLabelText(/Custom window/)
    // Out of range: no confirmation panel appears. One hour past a year —
    // past that the policy value is `never`, not a bigger number.
    fireEvent.change(custom, { target: { value: '8761' } })
    expect(screen.queryByRole('button', { name: /embargo$/ })).toBeNull()
    expect(screen.getByText(/whole number between 6 and 8760/)).toBeTruthy()

    // In range and longer than the current default: extendable.
    fireEvent.change(custom, { target: { value: '36' } })
    const action = screen.getByRole('button', { name: 'Extend embargo' })

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'staged privacy window for this cohort' },
    })
    const expected = artifactReleaseConfirmation(36)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(action)

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        disclosure: 'public',
        embargoHours: 36,
        reason: 'staged privacy window for this cohort',
        confirmation: expected,
      },
    })
  })

  it('extends past the 48-hour default to a custom month-scale window', async () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    // 100 hours used to be rejected by the console's own bound; it is now an
    // ordinary in-range window, and the day gloss makes it readable.
    fireEvent.change(screen.getByLabelText(/Custom window/), { target: { value: '100' } })
    expect(screen.queryByText(/whole number between/)).toBeNull()
    expect(screen.getByText(/hours · 4d 4h/)).toBeTruthy()

    const action = screen.getByRole('button', { name: 'Extend embargo' })
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'hold source private until the disclosure review closes' },
    })
    const expected = artifactReleaseConfirmation(100)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(action)

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        disclosure: 'public',
        embargoHours: 100,
        reason: 'hold source private until the disclosure review closes',
        confirmation: expected,
      },
    })
  })

  it('offers month-scale presets and flags anything past the agreed default', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    const ceiling = screen.getByRole('button', { name: /720 hours/ })
    expect(ceiling.textContent).toContain('30 days')
    expect(screen.getByRole('button', { name: /168 hours/ }).textContent).toContain('7 days')

    // 48 is the agreed default, so extending to it carries no extra note...
    fireEvent.click(screen.getByRole('button', { name: /48 hours/ }))
    expect(screen.queryByText(/window SN118 agreed on/)).toBeNull()

    // ...but anything beyond it does.
    fireEvent.click(ceiling)
    expect(screen.getByText(/window SN118 agreed on/)).toBeTruthy()
  })

  it('keeps mutations unavailable to read-only operators', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly />)

    expect(screen.getByText(/account is read only/)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: /12 hours/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect((screen.getByLabelText(/Custom window/) as HTMLInputElement).disabled).toBe(true)
  })

  it('sends the never policy with its own phrase and the window retained', async () => {
    setArtifactReleaseSettings.mockResolvedValue({
      current: {
        ...initial.current,
        revision: 1,
        disclosure: 'never',
        reason: 'subnet policy: source is not published',
        actor: 'peyton@omniaura.ai',
        created_at: '2026-07-27T12:00:00Z',
      },
      history: [],
    })
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /Never release source/ }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'subnet policy: source is not published' },
    })
    const expected = artifactReleaseConfirmation(24, 'never')
    expect(expected).toBe('SET SOURCE DISCLOSURE NEVER')
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Stop releasing source' }))

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 0,
        disclosure: 'never',
        // Sent unchanged, not omitted: the platform requires it in range
        // under every policy and retains it, so resuming release restores
        // the window the subnet last agreed on.
        embargoHours: 24,
        reason: 'subnet policy: source is not published',
        confirmation: expected,
      },
    })
    expect(
      await screen.findByText(/no longer published. Anything already released stays public/),
    ).toBeTruthy()
  })

  it('will not accept the hours phrase for a never policy', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /Never release source/ }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'subnet policy: source is not published' },
    })
    // These two are one keystroke apart in intent and a world apart in effect.
    fireEvent.change(screen.getByLabelText(/SET SOURCE DISCLOSURE NEVER/), {
      target: { value: artifactReleaseConfirmation(24) },
    })

    expect(
      (screen.getByRole('button', { name: 'Stop releasing source' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('names the anti-plagiarism cost before never is confirmed', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)
    fireEvent.click(screen.getByRole('button', { name: /Never release source/ }))

    // The honest cost, on screen at the moment of choosing rather than in a
    // doc nobody opens: external verification of the crown stops.
    expect(screen.getByText(/repackaged copy/)).toBeTruthy()
    expect(screen.getAllByText(/already released stays public/).length).toBeGreaterThan(0)
  })

  it('shows a withheld policy as withheld, with no stage marked current', () => {
    const withheld: ArtifactReleaseControl = {
      current: { ...initial.current, disclosure: 'never', revision: 3 },
      history: [],
    }
    render(<ArtifactReleaseControlPanel initialState={withheld} readOnly={false} />)

    expect(screen.getByText('Never released')).toBeTruthy()
    // Marking the retained window as the "current stage" would imply source
    // is being released on it. Every tile offers to resume instead.
    expect(screen.queryByText('Current stage')).toBeNull()
    expect(screen.getAllByText('Resume release on this window').length).toBe(9)
  })

  it('resumes release from a withheld policy on a chosen window', async () => {
    const withheld: ArtifactReleaseControl = {
      current: { ...initial.current, disclosure: 'never', revision: 3 },
      history: [],
    }
    setArtifactReleaseSettings.mockResolvedValue({
      current: { ...initial.current, revision: 4, embargo_hours: 72 },
      history: [],
    })
    render(<ArtifactReleaseControlPanel initialState={withheld} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: /^72 hours/ }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'subnet agreed a three-day window instead' },
    })
    const expected = artifactReleaseConfirmation(72)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Resume releasing source' }))

    await waitFor(() => expect(setArtifactReleaseSettings).toHaveBeenCalledTimes(1))
    expect(setArtifactReleaseSettings).toHaveBeenCalledWith({
      data: {
        expectedRevision: 3,
        disclosure: 'public',
        embargoHours: 72,
        reason: 'subnet agreed a three-day window instead',
        confirmation: expected,
      },
    })
  })

  it('offers a year, the longest finite window under discussion', () => {
    render(<ArtifactReleaseControlPanel initialState={initial} readOnly={false} />)

    // All four options Peyton is choosing between are reachable from the
    // panel: 72h, 720h, 8760h, and never.
    expect(screen.getByRole('button', { name: /^72 hours/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^720 hours/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^8760 hours/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Never release source/ })).toBeTruthy()
  })
})
