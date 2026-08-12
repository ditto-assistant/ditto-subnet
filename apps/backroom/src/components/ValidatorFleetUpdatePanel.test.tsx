// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ValidatorFleetUpdatePreview } from '../lib/admin.schemas'
import { ValidatorFleetUpdatePanel } from './ValidatorFleetUpdatePanel'

const getValidatorFleetUpdate = vi.fn()
const forceValidatorFleetUpdate = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getValidatorFleetUpdate: () => getValidatorFleetUpdate(),
  forceValidatorFleetUpdate: (input: unknown) => forceValidatorFleetUpdate(input),
}))

const operationId = '11111111-1111-4111-8111-111111111111'

function preview(): ValidatorFleetUpdatePreview {
  return {
    generated_at: '2026-08-12T16:00:00Z',
    snapshot: 'a'.repeat(64),
    target_count: 1,
    active_lease_count: 2,
    targets: [
      {
        validator_hotkey: '5ManagedValidatorabcdefghijklmnop',
        software_version: '0.53.14',
        stack_revision: 'b'.repeat(40),
        active_lease_count: 2,
        acknowledged: false,
      },
    ],
    latest_operation: null,
    confirmation: 'FORCE UPDATE VALIDATOR FLEET',
  }
}

describe('ValidatorFleetUpdatePanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getValidatorFleetUpdate.mockReset().mockResolvedValue(preview())
    forceValidatorFleetUpdate.mockReset().mockResolvedValue({
      operation: {
        operation_id: operationId,
        expected_snapshot: 'a'.repeat(64),
        targets: preview().targets,
        revoked_lease_count: 2,
        acknowledged_count: 0,
        actor: 'operator@example.com',
        reason: 'emergency scorer repair across the managed fleet',
        created_at: '2026-08-12T16:00:01Z',
      },
      idempotent: false,
    })
  })

  it('shows the exact blast radius without pre-filling confirmation', () => {
    render(<ValidatorFleetUpdatePanel initialPreview={preview()} readOnly={false} />)

    expect(screen.getByText('1', { selector: 'p' })).toBeTruthy()
    expect(screen.getByText('2', { selector: 'p' })).toBeTruthy()
    expect((screen.getByLabelText(/Type to confirm/) as HTMLInputElement).value).toBe('')
    expect(screen.getByText(/no-fault compensation/i)).toBeTruthy()
    expect(screen.getByText(/next timer poll/i)).toBeTruthy()
  })

  it('refuses a mistyped phrase before issuing the destructive request', async () => {
    render(<ValidatorFleetUpdatePanel initialPreview={preview()} readOnly={false} />)
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'emergency scorer repair across the managed fleet' },
    })
    fireEvent.change(screen.getByLabelText(/Type to confirm/), {
      target: { value: 'FORCE UPDATE VALIDATORS' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Stop work and force update/ }))

    expect((await screen.findByRole('alert')).textContent).toContain('Nothing was sent')
    expect(forceValidatorFleetUpdate).not.toHaveBeenCalled()
  })

  it('sends a fresh request id and the preview snapshot, then refreshes receipt state', async () => {
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(operationId)
    render(<ValidatorFleetUpdatePanel initialPreview={preview()} readOnly={false} />)
    fireEvent.change(screen.getByLabelText(/Operator reason/), {
      target: { value: 'emergency scorer repair across the managed fleet' },
    })
    fireEvent.change(screen.getByLabelText(/Type to confirm/), {
      target: { value: 'FORCE UPDATE VALIDATOR FLEET' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Stop work and force update/ }))

    await waitFor(() =>
      expect(forceValidatorFleetUpdate).toHaveBeenCalledWith({
        data: {
          requestId: operationId,
          expectedSnapshot: 'a'.repeat(64),
          reason: 'emergency scorer repair across the managed fleet',
          confirmation: 'FORCE UPDATE VALIDATOR FLEET',
        },
      }),
    )
    expect(getValidatorFleetUpdate).toHaveBeenCalledTimes(1)
    expect((await screen.findByRole('status')).textContent).toContain(
      'revoked 2 live leases',
    )
  })
})
