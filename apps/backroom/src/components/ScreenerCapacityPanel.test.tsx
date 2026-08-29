// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScreenerCapacityView, TrustedImageBuild } from '../lib/admin.schemas'
import { ScreenerCapacityPanel } from './ScreenerCapacityPanel'

const getScreenerCapacity = vi.fn()
const retryTrustedImageBuild = vi.fn()
const updateScreenerProviderSettings = vi.fn()
const updateScreenerNodeChannelSettings = vi.fn()
const overflowDefaults = {
  gce_overflow_enabled: false,
  primary_node_id: null,
  gce_overflow_backlog_multiplier: 3,
  gce_overflow_min_backlog: 12,
  gce_overflow_max_instances: 6,
} as const

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('../server/admin.functions', () => ({
  getScreenerCapacity: (...args: unknown[]) => getScreenerCapacity(...args),
  retryTrustedImageBuild: (...args: unknown[]) => retryTrustedImageBuild(...args),
  updateScreenerProviderSettings: (...args: unknown[]) => updateScreenerProviderSettings(...args),
  updateScreenerNodeChannelSettings: (...args: unknown[]) => updateScreenerNodeChannelSettings(...args),
}))

afterEach(() => {
  cleanup()
  getScreenerCapacity.mockReset()
  retryTrustedImageBuild.mockReset()
  updateScreenerProviderSettings.mockReset()
  updateScreenerNodeChannelSettings.mockReset()
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
          ...overflowDefaults,
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
    node_controls: [],
    ...overrides,
  }
}

describe('ScreenerCapacityPanel', () => {
  it('keeps one-shot Targon lanes without the nested-Docker worker gate', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly />)

    expect(screen.queryByText('Full Targon worker lane is blocked')).toBeNull()
    expect(screen.queryByText('Cut over to GCE only')).toBeNull()
    expect(screen.queryByText('Restore Targon-first')).toBeNull()
    expect(screen.queryByText('Normal writer lease is healthy')).toBeNull()
    expect(screen.queryByText(/5 CPU rentals advertised/)).toBeNull()
    expect(screen.getByText(/Trusted screener release-image builds remain separate/)).toBeTruthy()
    expect(screen.getByText(/Hetzner handles normal work/)).toBeTruthy()
  })

  it('renders one-shot cleanup timestamps in an explicit server-safe timezone', () => {
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
        initialState={capacity({ events: [cleanupEvent] })}
        readOnly
      />,
    )

    expect(screen.getByText(/Latest event Aug 13, 2026, 9:28 PM UTC/)).toBeTruthy()
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
      node_id: null,
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

  it('omits skipped runtime jobs so GCE-only cutover does not look live', () => {
    const providerJob = {
      job_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      lane: 'source_review' as const,
      status: 'succeeded',
      provider: 'targon' as const,
      node_id: null,
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
              job_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
              lane: 'runtime',
              status: 'skipped',
              provider: null,
              provider_resource_id: null,
              image_reference: null,
              error_code: 'TARGON_RUNTIME_DISABLED_BY_POLICY',
            },
          ],
        })}
        readOnly
      />,
    )

    expect(screen.getByText('source review')).toBeTruthy()
    expect(screen.queryByText('skipped')).toBeNull()
    expect(screen.queryByText('runtime')).toBeNull()
  })

  it('offers Hetzner base load, GCE only, and legacy Targon controls', () => {
    render(<ScreenerCapacityPanel initialState={capacity()} readOnly={false} />)

    expect(screen.getAllByRole('button', { name: 'Targon-first + GCE' })).toHaveLength(3)
    expect(screen.getAllByRole('button', { name: 'GCE only' })).toHaveLength(3)
    expect(screen.getAllByRole('button', { name: 'Hetzner + GCE overflow' })).toHaveLength(3)
    expect(screen.queryByRole('button', { name: 'Cut over to GCE only' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Restore Targon-first' })).toBeNull()
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
                ...overflowDefaults,
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
    expect(screen.getAllByRole('button', { name: 'GCE only' }).every((button) => button.getAttribute('aria-pressed') === 'true')).toBe(true)
  })

  it('applies a per-lane GCE-only draft through the existing confirmation flow', async () => {
    updateScreenerProviderSettings.mockResolvedValue({
      current: {
        environment: 'prod',
        revision: 1,
        parent_revision: 0,
        settings: {
          ...overflowDefaults,
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

    fireEvent.click(screen.getAllByRole('button', { name: 'GCE only' })[1])
    fireEvent.change(screen.getByLabelText(/Audit reason/), {
      target: { value: 'disable Targon runtime smoke only' },
    })
    fireEvent.change(screen.getByLabelText(/Type to confirm/), {
      target: { value: 'APPLY SCREENER PROVIDERS BUILDS=targon>gcp RUNTIME=gcp SOURCE_REVIEW=targon>gcp GCE_OVERFLOW=DISABLED' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Append provider revision' }))

    await waitFor(() => {
      expect(updateScreenerProviderSettings).toHaveBeenCalledWith({
        data: {
          expectedRevision: 0,
          settings: {
            ...overflowDefaults,
            runtime_provider_priority: ['gcp'],
            source_review_provider_priority: ['targon', 'gcp'],
            build_provider_priority: ['targon', 'gcp'],
          },
          reason: 'disable Targon runtime smoke only',
          confirmation: 'APPLY SCREENER PROVIDERS BUILDS=targon>gcp RUNTIME=gcp SOURCE_REVIEW=targon>gcp GCE_OVERFLOW=DISABLED',
        },
      })
    })
  })

  it('manually retries an exact terminal trusted build with current-state guards', async () => {
    const failedBuild: TrustedImageBuild = {
      build_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      environment: 'prod',
      component: 'screener',
      source_repository: 'https://github.com/ditto-assistant/ditto-subnet.git',
      source_sha: 'a'.repeat(40),
      context_path: '.',
      dockerfile_path: 'workers/screener/Dockerfile',
      destination: 'example.invalid/screener:sha-test',
      status: 'failed',
      provider: 'targon',
      provider_resource_id: 'build-failed-1',
      image_digest: null,
      error_code: 'TARGON_BUILD_FAILED',
      attempt_count: 47,
      controller_epoch: 'controller-before-repair',
      lease_expires_at: null,
      created_by: 'release@example.com',
      reason: 'Build exact release candidate',
      created_at: '2026-08-27T12:00:00Z',
      started_at: '2026-08-27T12:01:00Z',
      completed_at: '2026-08-27T12:02:00Z',
      updated_at: '2026-08-27T12:02:00Z',
    }
    retryTrustedImageBuild.mockResolvedValue({
      ...failedBuild,
      status: 'queued',
      provider: null,
      provider_resource_id: null,
      error_code: null,
      controller_epoch: null,
      started_at: null,
      completed_at: null,
      updated_at: '2026-08-27T12:03:00Z',
    })

    render(
      <ScreenerCapacityPanel
        initialState={capacity({ builds: [failedBuild] })}
        readOnly={false}
      />,
    )

    fireEvent.change(screen.getByLabelText('Retry reason for aaaaaaaaaaaa'), {
      target: { value: 'Targon builder infrastructure is repaired' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(retryTrustedImageBuild).toHaveBeenCalledWith({
        data: {
          buildId: failedBuild.build_id,
          expectedStatus: 'failed',
          expectedAttemptCount: 47,
          reason: 'Targon builder infrastructure is repaired',
        },
      })
    })
    expect(screen.getByText('queued')).toBeTruthy()
  })
})
