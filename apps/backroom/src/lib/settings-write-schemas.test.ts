import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import {
  applyScreenerReviewSettingsInputSchema,
  applyUnreleasedCopySettingsInputSchema,
  continualRetestSettingsSchema,
  inferenceConcurrencySettingsSchema,
  screenerProviderSettingsSchema,
  screenerReviewSettingsSchema,
  setContinualRetestSettingsInputSchema,
  setInferenceConcurrencySettingsInputSchema,
  setScreenerProviderSettingsInputSchema,
} from './admin.schemas'

// #2979: a settings revision stores the whole policy and Platform appends it
// verbatim, so a default on a write schema is not "leave it alone" but a write
// of that default over the live value.

const schemaModules = import.meta.glob<Record<string, unknown>>('./*.schemas.ts', {
  eager: true,
})

const WRITE_INPUT = /^(set|apply|update)\w*(Settings|Policy)\w*InputSchema$/

// Writes whose omitted fields resolve to a shipped default by documented
// contract, rather than silently: each is spelled out to the caller.
const DOCUMENTED_WRITE_DEFAULTS = new Map([
  // set_generator_settings documents "defaults when omitted" field by field,
  // and generator-competition.service.test.ts asserts it.
  ['generator-competition.schemas:setGeneratorSettingsInputSchema', 'documented per field'],
  // The confirmation phrase names the disclosure the default applies:
  // "SET SOURCE EMBARGO N HOURS" is a public policy, never has its own phrase.
  ['admin.schemas:updateArtifactReleaseSettingsInputSchema', 'bound by confirmation'],
])

// `scope` selects which revision chain a write appends to; it is not policy.
const SELECTOR_KEYS = new Set(['scope'])

function writeDefaults(schema: z.ZodType, path: string, out: string[]) {
  if (schema instanceof z.ZodDefault || schema instanceof z.ZodPrefault || schema instanceof z.ZodCatch) {
    out.push(path)
    return
  }
  if (schema instanceof z.ZodOptional || schema instanceof z.ZodNullable || schema instanceof z.ZodReadonly) {
    writeDefaults(schema.unwrap() as z.ZodType, path, out)
  } else if (schema instanceof z.ZodPipe) {
    writeDefaults(schema.in as z.ZodType, path, out)
  } else if (schema instanceof z.ZodArray) {
    writeDefaults(schema.element as z.ZodType, `${path}[]`, out)
  } else if (schema instanceof z.ZodObject) {
    for (const [key, field] of Object.entries(schema.shape)) {
      writeDefaults(field as z.ZodType, path ? `${path}.${key}` : key, out)
    }
  }
}

describe('settings write schemas (#2979)', () => {
  it('fill no omitted policy field with a read default', () => {
    const offenders: string[] = []
    let checked = 0
    for (const [file, exports] of Object.entries(schemaModules)) {
      const module = file.replace(/^\.\//, '').replace(/\.ts$/, '')
      for (const [name, schema] of Object.entries(exports)) {
        if (!WRITE_INPUT.test(name) || !(schema instanceof z.ZodObject)) continue
        if (DOCUMENTED_WRITE_DEFAULTS.has(`${module}:${name}`)) continue
        checked += 1
        const found: string[] = []
        for (const [key, field] of Object.entries(schema.shape)) {
          if (!SELECTOR_KEYS.has(key)) writeDefaults(field as z.ZodType, key, found)
        }
        offenders.push(...found.map((path) => `${module}:${name} ${path}`))
      }
    }
    // The pattern must keep matching the real write tools, or this passes vacuously.
    expect(checked).toBeGreaterThan(10)
    expect(offenders).toEqual([])
  })

  // Each case: the read schema fills its defaults into a minimal stored policy
  // (the effective settings an operator reads back), and that policy must be a
  // valid write unchanged, while dropping any filled field must be refused.
  const cases = [
    {
      name: 'continual retest',
      read: continualRetestSettingsSchema,
      write: setContinualRetestSettingsInputSchema.shape.settings,
      stored: { aggregate_mode: 'fleet_ready', idle_retests_enabled: true },
      live: { rollout_standdown: 'all' },
    },
    {
      name: 'screener review',
      read: screenerReviewSettingsSchema,
      write: applyScreenerReviewSettingsInputSchema.shape.settings,
      stored: {
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
      },
      live: { adjudicator_mode: 'enforce', policy_manifest_profile: 'l1_l2' },
    },
    {
      name: 'screener providers',
      read: screenerProviderSettingsSchema,
      write: setScreenerProviderSettingsInputSchema.shape.settings,
      stored: {
        build_provider_priority: ['targon', 'gcp'],
        runtime_provider_priority: ['targon', 'gcp'],
        source_review_provider_priority: ['targon', 'gcp'],
      },
      live: { gce_overflow_enabled: true, primary_node_id: 'ditto-screener-prod' },
    },
  ] as const

  for (const { name, read, write, stored, live } of cases) {
    it(`round-trips the effective ${name} policy and refuses a partial one`, () => {
      const effective = read.parse({ ...stored, ...live }) as Record<string, unknown>
      expect(write.parse(effective)).toEqual(effective)
      const filled = Object.keys(effective).filter((key) => !(key in stored))
      expect(filled.length).toBeGreaterThan(0)
      for (const key of filled) {
        const { [key]: _omitted, ...partial } = effective
        expect(write.safeParse(partial).success, `${name} write omitting ${key}`).toBe(false)
      }
    })
  }

  it('requires every benchmark runtime field on a concurrency write', () => {
    const effective = inferenceConcurrencySettingsSchema.parse({
      chat_request_budget: 8192,
      chat_token_budget: 25_000_000,
      chat_per_ticket_concurrency: 16,
      chat_per_validator_concurrency: 48,
      chat_global_concurrency: 96,
      embedding_per_ticket_concurrency: 8,
      embedding_per_validator_concurrency: 24,
      embedding_global_concurrency: 32,
      chat_per_ticket_requests_per_minute: 1920,
      chat_per_validator_requests_per_minute: 7680,
      chat_global_requests_per_minute: 23040,
      embedding_per_ticket_requests_per_minute: 10_000,
      embedding_per_validator_requests_per_minute: 40_000,
      embedding_global_requests_per_minute: 100_000,
      benchmark_runtime: {
        case_concurrency: 4,
        relay_delay_fingerprint_mode: 'off',
        relay_delay_fingerprint_min_ms: 25,
        relay_delay_fingerprint_max_ms: 250,
        memory_factor_posture: 'enforce',
      },
    })
    const write = setInferenceConcurrencySettingsInputSchema.shape.settings
    expect(write.parse(effective)).toEqual(effective)
    for (const key of [
      'sandbox_memory_limit_mib',
      'memory_factor_posture',
      'memory_factor_free_mib',
      'memory_factor_floor_bps',
    ] as const) {
      const { [key]: _omitted, ...runtime } = effective.benchmark_runtime
      expect(
        write.safeParse({ ...effective, benchmark_runtime: runtime }).success,
        `concurrency write omitting benchmark_runtime.${key}`,
      ).toBe(false)
    }
  })

  it('requires the unreleased-copy threshold on a mode change', () => {
    const input = {
      expectedRevision: 3,
      mode: 'enforce',
      reason: 'start holding unreleased copies',
      confirmation: 'APPLY UNRELEASED COPY ENFORCE',
    }
    expect(applyUnreleasedCopySettingsInputSchema.safeParse(input).success).toBe(false)
    expect(applyUnreleasedCopySettingsInputSchema.parse({ ...input, threshold: 55 }).threshold).toBe(55)
  })
})
