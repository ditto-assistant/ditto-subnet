// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScreenerCapacityView } from '../lib/admin.schemas'
import { ScreenerCapacityPanel } from './ScreenerCapacityPanel'

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  getScreenerCapacity: vi.fn(),
  updateScreenerProviderSettings: vi.fn(),
}))

afterEach(cleanup)

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
  })

  it('does not show the blocked-worker banner for a GO capability', () => {
    const state = capacity()
    state.snapshot = state.snapshot ? { ...state.snapshot, targon_capability: 'go', fallback_reason: null } : null

    render(<ScreenerCapacityPanel initialState={state} readOnly />)

    expect(screen.queryByText('Full Targon worker lane is blocked')).toBeNull()
  })
})
