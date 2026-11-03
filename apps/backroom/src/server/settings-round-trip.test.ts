import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import {
  applyScreenerReviewSettings,
  fetchScreenerReviewControl,
  rotateScreenerPolicyManifest,
  updateScreenerProviderSettings,
} from './admin.service'
import {
  continualRetestSettingsSchema,
  screenerProviderSettingsConfirmation,
  screenerProviderSettingsSchema,
  screenerReviewSettingsSchema,
} from '../lib/admin.schemas'

// #2979: a legacy revision read back through Backroom has its unreported
// fields filled with read defaults, and a whole-policy write sends every field.
// That is only safe while each read default is the value Platform itself
// resolves for the field, and it means a value the operator chose always
// reaches Platform, even one equal to the default.

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const actor = 'operator@example.com'
const REVIEW_SETTINGS_URL = 'https://platform-api.heyditto.ai/api/v1/admin/screener-review-settings'
const PROVIDER_SETTINGS_URL = 'https://platform-api.heyditto.ai/api/v1/admin/screener-provider-settings'

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
})

// The `@default` Platform's OpenAPI schema documents for each field of one
// settings model, from the client generated off the same monorepo commit.
function platformDefaults(model: string) {
  const source = readFileSync(new URL('../generated/platform-api.ts', import.meta.url), 'utf8')
  const start = source.indexOf(`\n        ${model}: {\n`)
  expect(start, `components.schemas.${model}`).toBeGreaterThan(-1)
  const block = source.slice(start, source.indexOf('\n        };', start))
  const defaults = new Map<string, string | undefined>()
  for (const [, doc, field] of block.matchAll(/\/\*\*([\s\S]*?)\*\/\s*\n\s*(\w+)\??:/g)) {
    const text = doc!.split('\n').map((line) => line.replace(/^\s*\*\s?/, '')).join('\n')
    defaults.set(field!, /@default ([\s\S]*?)(?=\n@|\s*$)/.exec(text)?.[1]?.trim())
  }
  return defaults
}

describe('Backroom read defaults', () => {
  it.each([
    ['ScreenerReviewSettings', screenerReviewSettingsSchema.shape],
    ['ContinualRetestSettings', continualRetestSettingsSchema.shape],
    ['ScreenerProviderSettings', screenerProviderSettingsSchema.shape],
  ] as const)('match the default Platform resolves for each %s field', (model, shape) => {
    const platform = platformDefaults(model)
    const compared: string[] = []
    for (const [field, validator] of Object.entries(shape)) {
      if (!(validator instanceof z.ZodDefault)) continue
      expect(platform.has(field), `${model}.${field} is a Platform field`).toBe(true)
      const value: unknown = validator.def.defaultValue
      const documented = platform.get(field)
      if (documented === undefined) expect(value, `${model}.${field}`).toBeNull()
      else if (typeof value === 'string') expect(value, `${model}.${field}`).toBe(documented)
      else expect(value, `${model}.${field}`).toEqual(JSON.parse(documented))
      compared.push(field)
    }
    expect(compared.length).toBeGreaterThan(0)
  })

  it('read a legacy L1 timeout as the 3,600 seconds Platform resolves', () => {
    expect(platformDefaults('ScreenerReviewSettings').get('source_review_timeout_seconds')).toBe('3600')
    expect(screenerReviewSettingsSchema.shape.source_review_timeout_seconds.def.defaultValue).toBe(3_600)
  })
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

function stubReviewSettings() {
  process.env.DITTO_ADMIN_API_TOKEN = 'secret'
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === 'POST'
      ? Response.json({ ...legacyRevision, revision: 8, parent_revision: 7 })
      : Response.json(legacyReviewControl),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function postedSettings(fetchMock: ReturnType<typeof vi.fn>, url: string) {
  const posts = fetchMock.mock.calls.filter(
    ([called, init]) => called === url && (init as RequestInit | undefined)?.method === 'POST',
  )
  expect(posts, `POST ${url}`).toHaveLength(1)
  return JSON.parse(String((posts[0]![1] as RequestInit).body)).settings as Record<string, unknown>
}

describe('legacy settings read followed by a whole-policy write', () => {
  it('writes the policy Platform already resolves for fields the revision did not report', async () => {
    const fetchMock = stubReviewSettings()

    // The panel and MCP callers both start from the parsed read.
    const settings = (await fetchScreenerReviewControl()).current[0]!.settings
    await applyScreenerReviewSettings(actor, {
      scope: '*',
      expectedRevision: 7,
      settings: { ...settings, max_cost_usd: 3 },
      reason: 'Raise the review budget.',
      confirmation: 'APPLY SCREENER REVIEW * SHADOW',
    })

    // Platform stores `model_dump()` of the request, so these are exactly the
    // values it would materialize for the unreported fields anyway.
    const posted = postedSettings(fetchMock, REVIEW_SETTINGS_URL)
    expect(posted).toEqual({ ...settings, max_cost_usd: 3 })
    expect(posted).toMatchObject({
      ...legacySettings,
      max_cost_usd: 3,
      source_review_timeout_seconds: 3_600,
      source_review_max_steps: 200,
      probe_battery_mode: 'off',
    })
  })

  it('sends a value the operator chose for an unreported field even when it equals the default', async () => {
    const fetchMock = stubReviewSettings()

    const settings = (await fetchScreenerReviewControl()).current[0]!.settings
    await applyScreenerReviewSettings(actor, {
      scope: '*',
      expectedRevision: 7,
      settings: { ...settings, source_review_max_steps: 200, source_review_timeout_seconds: 1_800 },
      reason: 'Pin the L1 budget explicitly.',
      confirmation: 'APPLY SCREENER REVIEW * SHADOW',
    })

    expect(postedSettings(fetchMock, REVIEW_SETTINGS_URL)).toMatchObject({
      source_review_max_steps: 200,
      source_review_timeout_seconds: 1_800,
    })
  })

  it('writes a first revision for a scope that has none', async () => {
    const fetchMock = stubReviewSettings()

    const settings = (await fetchScreenerReviewControl()).current[0]!.settings
    await applyScreenerReviewSettings(actor, {
      scope: 'l2-report-canary',
      expectedRevision: 0,
      settings,
      reason: 'Seed the canary posture.',
      confirmation: 'APPLY SCREENER REVIEW l2-report-canary SHADOW',
    })

    expect(postedSettings(fetchMock, REVIEW_SETTINGS_URL)).toEqual(settings)
  })

  it('rotates the manifest over the current revision exactly as read', async () => {
    const fetchMock = stubReviewSettings()
    const settings = (await fetchScreenerReviewControl()).current[0]!.settings

    await rotateScreenerPolicyManifest(actor, {
      scope: '*',
      expectedRevision: 7,
      profile: 'l1_l2',
      rotationId: 'policy-v13-rotation',
      reason: 'Rotate the manifest after the policy update.',
      confirmation: 'ROTATE SCREENER POLICY * policy-v13-rotation',
    })

    // Platform's rotate-only check compares this with the previous revision as
    // it resolves it, so every other field must be the read, unchanged.
    expect(postedSettings(fetchMock, REVIEW_SETTINGS_URL)).toEqual({
      ...settings,
      policy_manifest_profile: 'l1_l2',
      policy_manifest_rotation_id: 'policy-v13-rotation',
    })
  })

  it('writes provider settings as given without reading first', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const settings: Parameters<typeof screenerProviderSettingsConfirmation>[0] = {
      build_provider_priority: ['gcp'],
      runtime_provider_priority: ['gcp'],
      source_review_provider_priority: ['gcp'],
      gce_overflow_enabled: false,
      primary_node_id: null,
      gce_overflow_backlog_multiplier: 3,
      gce_overflow_min_backlog: 12,
      gce_overflow_max_instances: 6,
    }
    const revision = {
      environment: 'prod',
      revision: 1,
      parent_revision: 0,
      settings,
      reason: 'Seed provider routing.',
      actor,
      created_at: '2026-11-03T00:00:00Z',
    }
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? new Response(null, { status: 204 })
        : Response.json({ current: revision, history: [revision] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await updateScreenerProviderSettings(actor, {
      expectedRevision: 0,
      settings,
      reason: 'Seed provider routing.',
      confirmation: screenerProviderSettingsConfirmation(settings),
    })

    expect(fetchMock.mock.calls[0]![0]).toBe(PROVIDER_SETTINGS_URL)
    expect((fetchMock.mock.calls[0]![1] as RequestInit).method).toBe('POST')
    expect(postedSettings(fetchMock, PROVIDER_SETTINGS_URL)).toEqual(settings)
  })
})
