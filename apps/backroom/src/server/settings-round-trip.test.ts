import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  applyScreenerReviewSettings,
  fetchScreenerReviewControl,
  rotateScreenerPolicyManifest,
  updateScreenerProviderSettings,
} from './admin.service'
import { screenerProviderSettingsConfirmation } from '../lib/admin.schemas'

// #2979: a legacy revision read back through Backroom has its unreported
// fields filled with read-compat defaults. Writing that read back must not
// turn those made-up values into a stored policy.

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const actor = 'operator@example.com'
const REVIEW_SETTINGS_URL = 'https://platform-api.heyditto.ai/api/v1/admin/screener-review-settings'
const PROVIDER_SETTINGS_URL = 'https://platform-api.heyditto.ai/api/v1/admin/screener-provider-settings'

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
})

// Stored before the adjudicator, fan-out, probe, manifest and source-review
// budget fields existed, and answered by a Platform that does not report them.
const legacySettings = {
  mode: 'shadow',
  l2_model: 'openai/gpt-5.6-terra',
  l2_fallback_models: ['z-ai/glm-5.2'],
  l3_model: 'openai/gpt-5.6-sol',
  timeout_seconds: 900,
  max_steps: 18,
  max_input_tokens: 425_000,
  max_output_tokens: 20_000,
  max_completion_tokens: 2_400,
  max_cost_usd: 2,
  critic_reasoning_effort: 'medium',
  cache_ttl_seconds: 604_800,
  audit_retention_days: 30,
}

const legacyRevision = {
  revision: 7,
  parent_revision: 6,
  scope: '*',
  settings: legacySettings,
  reason: 'Legacy reviewer posture.',
  actor,
  created_at: '2026-08-01T00:00:00Z',
  checksum: 'ab'.repeat(32),
}

const legacyReviewControl = {
  current: [legacyRevision],
  history: [legacyRevision],
  known_instances: ['ditto-screener-prod'],
  applied_instances: [],
  shadow_observations: [],
}

function postedSettings(fetchMock: ReturnType<typeof vi.fn>, url: string) {
  const post = fetchMock.mock.calls.find(
    ([called, init]) => called === url && (init as RequestInit).method === 'POST',
  )
  expect(post, `POST ${url}`).toBeDefined()
  return JSON.parse(String((post![1] as RequestInit).body)).settings as Record<string, unknown>
}

describe('legacy settings read followed by a whole-policy write', () => {
  it('writes only what the live screener review revision reported, plus what the operator changed', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? Response.json({ ...legacyRevision, revision: 8, parent_revision: 7 })
        : Response.json(legacyReviewControl),
    )
    vi.stubGlobal('fetch', fetchMock)

    // The panel and MCP callers both start from the parsed read.
    const read = await fetchScreenerReviewControl()
    const settings = read.current[0]!.settings
    expect(settings.adjudicator_mode).toBe('off')
    expect(settings.source_review_timeout_seconds).toBe(1_800)

    await applyScreenerReviewSettings(actor, {
      scope: '*',
      expectedRevision: 7,
      settings: { ...settings, max_cost_usd: 3, probe_battery_mode: 'shadow' },
      reason: 'Raise the review budget.',
      confirmation: 'APPLY SCREENER REVIEW * SHADOW',
    })

    const posted = postedSettings(fetchMock, REVIEW_SETTINGS_URL)
    expect(posted).toEqual({ ...legacySettings, max_cost_usd: 3, probe_battery_mode: 'shadow' })
  })

  it('keeps the live screener review revision for fields a rotation does not name', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? Response.json({ ...legacyRevision, revision: 8, parent_revision: 7 })
        : Response.json(legacyReviewControl),
    )
    vi.stubGlobal('fetch', fetchMock)

    await rotateScreenerPolicyManifest(actor, {
      scope: '*',
      expectedRevision: 7,
      profile: 'l1',
      rotationId: 'policy-v13-rotation',
      reason: 'Rotate the manifest after the policy update.',
      confirmation: 'ROTATE SCREENER POLICY * policy-v13-rotation',
    })

    // The rotation names both manifest fields, so both are sent even where
    // the request equals the read default; nothing else is made up.
    expect(postedSettings(fetchMock, REVIEW_SETTINGS_URL)).toEqual({
      ...legacySettings,
      policy_manifest_profile: 'l1',
      policy_manifest_rotation_id: 'policy-v13-rotation',
    })
  })

  it('writes only what the live provider revision reported, plus what the operator changed', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const legacyProviders = {
      build_provider_priority: ['hetzner', 'gcp'] as Array<'hetzner' | 'gcp'>,
      runtime_provider_priority: ['hetzner', 'gcp'] as Array<'hetzner' | 'gcp'>,
      source_review_provider_priority: ['hetzner', 'gcp'] as Array<'hetzner' | 'gcp'>,
    }
    const control = {
      current: {
        environment: 'prod',
        revision: 4,
        parent_revision: 3,
        settings: legacyProviders,
        reason: 'Legacy provider routing.',
        actor,
        created_at: '2026-08-01T00:00:00Z',
      },
      history: [],
    }
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? Response.json({}) : Response.json(control),
    )
    vi.stubGlobal('fetch', fetchMock)

    const settings: Parameters<typeof screenerProviderSettingsConfirmation>[0] = {
      ...legacyProviders,
      gce_overflow_enabled: false,
      primary_node_id: 'subnet-screener-2',
      gce_overflow_backlog_multiplier: 3,
      gce_overflow_min_backlog: 12,
      gce_overflow_max_instances: 6,
    }
    await updateScreenerProviderSettings(actor, {
      expectedRevision: 4,
      settings,
      reason: 'Pin the primary screener node.',
      confirmation: screenerProviderSettingsConfirmation(settings),
    })

    expect(postedSettings(fetchMock, PROVIDER_SETTINGS_URL)).toEqual({
      ...legacyProviders,
      primary_node_id: 'subnet-screener-2',
    })
  })

  it('sends every field when the live revision reports every field', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? Response.json({ ...legacyRevision, revision: 8, parent_revision: 7 })
        : Response.json(legacyReviewControl),
    )
    vi.stubGlobal('fetch', fetchMock)
    const settings = (await fetchScreenerReviewControl()).current[0]!.settings
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? Response.json({ ...legacyRevision, revision: 8, parent_revision: 7 })
        : Response.json({
            ...legacyReviewControl,
            current: [{ ...legacyRevision, settings }],
          }),
    )

    await applyScreenerReviewSettings(actor, {
      scope: '*',
      expectedRevision: 7,
      settings,
      reason: 'Restate the current posture.',
      confirmation: 'APPLY SCREENER REVIEW * SHADOW',
    })

    expect(postedSettings(fetchMock, REVIEW_SETTINGS_URL)).toEqual(settings)
  })
})
