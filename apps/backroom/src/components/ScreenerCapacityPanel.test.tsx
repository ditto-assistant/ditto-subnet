// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScreenerCapacityView } from '../lib/admin.schemas'
import { ScreenerCapacityPanel } from './ScreenerCapacityPanel'

const getScreenerCapacity = vi.fn()
const updateScreenerProviderSettings = vi.fn()

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  getScreenerCapacity: (...args: unknown[]) => getScreenerCapacity(...args),
  updateScreenerProviderSettings: (...args: unknown[]) => updateScreenerProviderSettings(...args),
}))

afterEach(() => {
  cleanup()
  getScreenerCapacity.mockReset()
  updateScreenerProviderSettings.mockReset()
})

function capacity(overrides: Partial<ScreenerCapacityView> = {}): ScreenerCapacityView {
  return {
    snapshot: {
      environment: 'prod',
      controller_epoch: 'prod:epoch',
      provider_settings_revision: 0,
      runnable_backlog: 0,
      active_leases: 0,
      desired_slots: 0,
      global_cap: 6,
      targon_capability: 'nogo',
      targon_available: 5,
      targon_healthy: 0,
      targon_pending: 0,
      targon_draining: 0,
      gce_target: 0,
      gce_healthy: 0,
      gce_pending: 0,
      gce_draining: 0,
      fallback_reason: 'TARGON_CAPABILITY_ATTESTATION_EXPIRED',
      last_provider_success_at: '2026-08-13T21:40:00Z',
      last_provider_error_code: null,
      last_provider_error_at: null,
      events: [],
      controller_heartbeat_at: '2099-08-13T21:40:00Z',
      controller_lease_expires_at: '2099-08-13T21:43:00Z',
      updated_at: '2026-08-13T21:40:00Z',
    },
    nodes: [],
    events: [],
    builds: [],
    provider_jobs: [],
    provider_control: {
      current: {
        environment: 'prod',
        revision: 0,
        parent_revision: 0,
        settings: {
          runtime_provider_priority: ['targon', 'gcp'],
          source_review_provider_priority: ['targon', 'gcp'],
          build_provider_priority: ['targon', 'gcp'],
        },
        reason: 'Built-in default',
        actor: 'platform',
        created_at: null,
      },
      history: [],
    },
    ...overrides,
  }
}

describe('ScreenerCapacityPanel', () => {
  it('separates the hostile worker gate from the Targon build lane', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly />)

    expect(screen.getByText('Full Targon worker lane is blocked')).toBeTruthy()
    expect(
      screen.getByText(/not the independently controlled credential-minimal Kaniko build lane/),
    ).toBeTruthy()
    expect(screen.getByText(/safety record expired; this is not a Targon API outage/)).toBeTruthy()
    expect(screen.getByText(/Release and miner-image builds follow the independent builder priority/)).toBeTruthy()
    expect(screen.queryByText(/No miner artifact is sent to Targon/)).toBeNull()
  })

  it('explains why idle inventory does not create healthy workers', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly />)

    expect(screen.getByText(/Advertised inventory is available supply, not an active worker count/)).toBeTruthy()
    expect(screen.getByText(/5 CPU rentals advertised; full workers require a GO capability/)).toBeTruthy()
  })

  it('renders operator timestamps in an explicit server-safe timezone', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly />)

    expect(screen.getByText(/heartbeat Aug 13, 2099, 9:40 PM UTC/)).toBeTruthy()
  })

  it('surfaces cleanup-required events above the event stream', () => {
    const cleanupEvent = {
      event_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      event_type: 'provider_cleanup_required',
      provider: 'targon' as const,
      node_id: null,
      detail: 'A suspended zero-replica submission build rental requires provider deletion retry.',
      controller_epoch: 'builder:prod',
      created_at: '2026-08-13T21:28:05Z',
    }

    render(
      <ScreenerCapacityPanel
        initialState={capacity({ events: [cleanupEvent, { ...cleanupEvent, event_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }] })}
        readOnly
      />,
    )

    expect(screen.getByText('Targon provider cleanup is incomplete')).toBeTruthy()
    expect(screen.getByText(/2 cleanup-required events/)).toBeTruthy()
    expect(screen.getByText(/suspended at zero replicas/)).toBeTruthy()
    expect(screen.getByText(/build, runtime, or source-review jobs/)).toBeTruthy()
  })

  it('uses outcome-specific tones for one-shot provider jobs', () => {
    const providerJob = {
      job_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      lane: 'source_review' as const,
      status: 'succeeded',
      provider: 'targon' as const,
      provider_resource_id: 'wrk-source-review',
      image_reference: 'sha256:source-review',
      error_code: null,
      created_at: '2026-08-14T13:49:00Z',
      updated_at: '2026-08-14T13:50:00Z',
    }

    render(
      <ScreenerCapacityPanel
        initialState={capacity({
          provider_jobs: [
            providerJob,
            {
              ...providerJob,
              job_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
              lane: 'runtime',
              status: 'fallback_required',
            },
          ],
        })}
        readOnly
      />,
    )

    expect(screen.getByText('succeeded').className).toContain('text-[var(--acid)]')
    expect(screen.getByText('fallback required').className).toContain('text-[var(--amber)]')
  })

  it('does not show the blocked-worker banner for a GO capability', () => {
    const state = capacity()
    state.snapshot = state.snapshot ? { ...state.snapshot, targon_capability: 'go', fallback_reason: null } : null

    render(<ScreenerCapacityPanel initialState={state} readOnly />)

    expect(screen.queryByText('Full Targon worker lane is blocked')).toBeNull()
  })

  it('offers Targon-first and GCE-only controls without a GCP-first hybrid', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly={false} />)

    expect(screen.getAllByRole('button', { name: 'Targon first' })).toHaveLength(3)
    expect(screen.getAllByRole('button', { name: 'Targon off (GCE only)' })).toHaveLength(3)
    expect(screen.getByRole('button', { name: 'Cut over to GCE only' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Restore Targon-first' })).toBeTruthy()
    expect(screen.getByText('All lanes Targon-first')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'GCP first' })).toBeNull()
    expect(screen.queryByText('GCP first')).toBeNull()
  })

  it('treats a stored gcp-then-targon revision as GCE only', () => {
    render(
      <ScreenerCapacityPanel
        initialState={capacity({
          provider_control: {
            current: {
              environment: 'prod',
              revision: 3,
              parent_revision: 2,
              settings: {
                runtime_provider_priority: ['gcp', 'targon'],
                source_review_provider_priority: ['gcp', 'targon'],
                build_provider_priority: ['gcp', 'targon'],
              },
              reason: 'legacy gcp-first revision',
              actor: 'operator@example.com',
              created_at: '2026-08-13T21:40:00Z',
            },
            history: [],
          },
        })}
        readOnly={false}
      />,
    )

    expect(screen.getByText('All lanes GCE only')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Targon off (GCE only)' }).every((button) => button.getAttribute('aria-pressed') === 'true')).toBe(true)
  })

  it('drafts all three lanes to GCE only from the emergency cutover', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cut over to GCE only' }))

    expect(screen.getByText('All lanes GCE only')).toBeTruthy()
    expect(screen.getByText('APPLY SCREENER PROVIDERS BUILDS=gcp RUNTIME=gcp SOURCE_REVIEW=gcp')).toBeTruthy()
  })

  it('drafts targon>gcp on every lane from restore Targon-first', () => {
    render(
      <ScreenerCapacityPanel
        initialState={capacity({
          provider_control: {
            current: {
              environment: 'prod',
              revision: 2,
              parent_revision: 1,
              settings: {
                runtime_provider_priority: ['gcp'],
                source_review_provider_priority: ['gcp'],
                build_provider_priority: ['gcp'],
              },
              reason: 'emergency GCE cutover',
              actor: 'operator@example.com',
              created_at: '2026-08-16T12:00:00Z',
            },
            history: [],
          },
        })}
        readOnly={false}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Restore Targon-first' }))

    expect(screen.getByText('All lanes Targon-first')).toBeTruthy()
    expect(
      screen.getByText('APPLY SCREENER PROVIDERS BUILDS=targon>gcp RUNTIME=targon>gcp SOURCE_REVIEW=targon>gcp'),
    ).toBeTruthy()
  })

  it('applies a per-lane GCE-only draft through the existing confirmation flow', async () => {
    updateScreenerProviderSettings.mockResolvedValue({
      current: {
        environment: 'prod',
        revision: 1,
        parent_revision: 0,
        settings: {
          runtime_provider_priority: ['gcp'],
          source_review_provider_priority: ['targon', 'gcp'],
          build_provider_priority: ['targon', 'gcp'],
        },
        reason: 'disable Targon runtime smoke only',
        actor: 'operator@example.com',
        created_at: '2026-08-16T12:00:00Z',
      },
      history: [],
    })

    render(<ScreenerCapacityPanel initialState={capacity()} readOnly={false} />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Targon off (GCE only)' })[1])
    fireEvent.change(screen.getByLabelText(/Audit reason/), {
      target: { value: 'disable Targon runtime smoke only' },
    })
    fireEvent.change(screen.getByLabelText(/Type to confirm/), {
      target: { value: 'APPLY SCREENER PROVIDERS BUILDS=targon>gcp RUNTIME=gcp SOURCE_REVIEW=targon>gcp' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append provider revision' }))

    await waitFor(() => {
      expect(updateScreenerProviderSettings).toHaveBeenCalledWith({
        data: {
          expectedRevision: 0,
          settings: {
            runtime_provider_priority: ['gcp'],
            source_review_provider_priority: ['targon', 'gcp'],
            build_provider_priority: ['targon', 'gcp'],
          },
          reason: 'disable Targon runtime smoke only',
          confirmation: 'APPLY SCREENER PROVIDERS BUILDS=targon>gcp RUNTIME=gcp SOURCE_REVIEW=targon>gcp',
        },
      })
    })
  })
})
