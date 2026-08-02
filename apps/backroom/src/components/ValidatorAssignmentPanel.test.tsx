// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ValidatorAssignment } from '../lib/admin.schemas'
import { releaseActiveValidatorAssignment } from '../server/admin.functions'
import { ValidatorAssignmentPanel } from './ValidatorAssignmentPanel'

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  listValidatorAssignments: vi.fn(),
  releaseActiveValidatorAssignment: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const assignment: ValidatorAssignment = {
  agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
  agent_name: 'memory-agent',
  miner_hotkey: '5Miner',
  validator_hotkey: '5Validator',
  issued_at: '2026-07-15T07:00:00Z',
  deadline: '2026-07-15T08:30:00Z',
  bench_version: 2,
  attempt_count: 1,
  score_count: 2,
  provisional_composite: 0.81,
}

describe('ValidatorAssignmentPanel', () => {
  it('shows safe operational lease details including benchmark version', () => {
    render(
      <ValidatorAssignmentPanel initialItems={[assignment]} readOnly={false} />,
    )

    expect(screen.getByText('Active lease')).toBeTruthy()
    expect(screen.getByText('Benchmark v2')).toBeTruthy()
    expect(screen.getByText('Attempt')).toBeTruthy()
    expect(screen.getByText('Issued')).toBeTruthy()
    expect(screen.getByText('Deadline')).toBeTruthy()
    expect(screen.getByText('2 of 3 accepted · provisional 0.810')).toBeTruthy()
    expect(screen.getByText('90cb5697… · 5Validat…')).toBeTruthy()
  })

  it('requires an explicit audit reason and releases the exact lease', async () => {
    vi.mocked(releaseActiveValidatorAssignment).mockResolvedValue({
      agent_id: assignment.agent_id,
      validator_hotkey: assignment.validator_hotkey,
      status: 'expired',
      retry_after: '2026-07-15T14:00:00Z',
    })
    render(
      <ValidatorAssignmentPanel initialItems={[assignment]} readOnly={false} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Release assignment' }))
    expect(screen.getByText(/six-hour cooldown/)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: 'Confirm release' })
    expect(confirm.getAttribute('disabled')).not.toBeNull()

    fireEvent.change(screen.getByRole('textbox', { name: 'Audit reason' }), {
      target: { value: 'Validator process was intentionally stopped' },
    })
    fireEvent.click(confirm)

    await waitFor(() =>
      expect(releaseActiveValidatorAssignment).toHaveBeenCalledOnce(),
    )
    expect(releaseActiveValidatorAssignment).toHaveBeenCalledWith({
      data: {
        agentId: assignment.agent_id,
        validatorHotkey: assignment.validator_hotkey,
        expectedDeadline: assignment.deadline,
        reason: 'Validator process was intentionally stopped',
      },
    })
    expect(screen.queryByText('memory-agent')).toBeNull()
  })

  it('uses a unique audit-reason control for each concurrent lease', () => {
    render(
      <ValidatorAssignmentPanel
        initialItems={[
          assignment,
          { ...assignment, validator_hotkey: '5SecondValidator' },
        ]}
        readOnly={false}
      />,
    )

    const releaseButtons = screen.getAllByRole('button', {
      name: 'Release assignment',
    })
    fireEvent.click(releaseButtons[0])
    const firstControl = screen.getByRole('textbox', { name: 'Audit reason' })
    fireEvent.click(releaseButtons[1])
    const secondControl = screen.getByRole('textbox', { name: 'Audit reason' })

    expect(firstControl.id).not.toBe(secondControl.id)
    expect(firstControl.id).toContain(assignment.validator_hotkey)
    expect(secondControl.id).toContain('5SecondValidator')
  })
})
