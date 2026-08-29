import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// Every Platform response field must have an explicit Backroom validator. Making
// optional OpenAPI properties required in this mapped shape is deliberate: it
// catches newly-added optional fields too, which z.strictObject would otherwise
// reject at runtime without any compile-time warning. Each validator's output
// must also remain assignable to the generated field type.
type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type GeneratedConfirmationBundleView =
  PlatformComponents['schemas']['ConfirmationBundleView']
type GeneratedConfirmationBundleList =
  PlatformComponents['schemas']['AdminConfirmationBundleListResponse']
type GeneratedSourceReviewCausalEvidence =
  PlatformComponents['schemas']['SourceReviewCausalEvidence']
type GeneratedSourceReviewInvariantAssessment =
  PlatformComponents['schemas']['SourceReviewInvariantAssessment']
type GeneratedSourceReviewInvariantDecision =
  PlatformComponents['schemas']['SourceReviewInvariantDecision']
type GeneratedSourceReviewFinding = PlatformComponents['schemas']['SourceReviewFinding']
type GeneratedValidatorUpdaterStatus = PlatformComponents['schemas']['ValidatorUpdaterStatus']
type GeneratedAdminActiveHotkeyBan = PlatformComponents['schemas']['AdminActiveHotkeyBan']
type GeneratedAdminHotkeyBanAuditEntry =
  PlatformComponents['schemas']['AdminHotkeyBanAuditEntry']
type GeneratedAdminHotkeyBanControl = PlatformComponents['schemas']['AdminHotkeyBanControl']
type GeneratedAdminHotkeyBanList = PlatformComponents['schemas']['AdminHotkeyBanList']
type GeneratedAdminHotkeyUnbanResponse =
  PlatformComponents['schemas']['AdminHotkeyUnbanResponse']

// Every bench epoch that carries the signed confirmation evidence stack. One
// definition, derived from the generated contract -- restating it per schema is
// exactly what stranded this lane on bench 9 while the network ran on 11.
type GeneratedConfirmationBenchVersion =
  PlatformComponents['schemas']['LongMemEvidence']['bench_version']

export const confirmationBenchVersionSchema = z.union([
  z.literal(9),
  z.literal(10),
  z.literal(11),
  z.literal(12),
])

// Exact set equality against the contract, checked in BOTH directions. A plain
// `satisfies z.ZodType<...>` cannot catch this: a narrower union stays
// assignable to a wider one, so dropping a member would type-check and then
// reject real evidence at runtime. The false branches must be `false`, NOT
// `never` -- `never extends true` is vacuously true, so a `never` branch makes
// this whole guard silently pass (verified: narrowing the union to 9|10|11 with
// a `never` branch still compiled clean).
type AssertTrue<Value extends true> = Value
export type ConfirmationBenchVersionsMatchContract = AssertTrue<
  [GeneratedConfirmationBenchVersion] extends [z.infer<typeof confirmationBenchVersionSchema>]
    ? [z.infer<typeof confirmationBenchVersionSchema>] extends [GeneratedConfirmationBenchVersion]
      ? true
      : false
    : false
>

export const auditReasonSchema = (minimum: 3 | 8) =>
  z.string().trim().min(minimum)

export const inferenceRouteHealthSchema = z.enum(['discovered', 'healthy', 'degraded', 'offline'])

export const inferenceRouteCalibrationStatusSchema = z.enum(['shadow', 'eligible', 'disabled'])

export const inferenceRouteSchema = z.object({
  model: z.string().min(1),
  provider: z.string().min(1),
  profile_revision: z.string().min(1),
  quantization: z.string().nullable(),
  status: inferenceRouteHealthSchema,
  calibration_status: inferenceRouteCalibrationStatusSchema,
  calibration_revision: z.number().int().nonnegative(),
  calibration_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  calibration_sample_count: z.number().int().nonnegative(),
  calibration_tool_accuracy: z.number().min(0).max(1).nullable(),
  calibration_composite: z.number().min(0).max(1).nullable(),
  sample_count: z.number().int().nonnegative(),
  selected_ticket_count: z.number().int().nonnegative(),
  exploration_ticket_count: z.number().int().nonnegative(),
  last_selected_at: z.string().nullable(),
  ewma_tokens_per_second: z.number().nonnegative().nullable(),
  ewma_latency_ms: z.number().nonnegative().nullable(),
  ewma_error_rate: z.number().min(0).max(1),
  ewma_timeout_rate: z.number().min(0).max(1),
  prompt_price_per_token: z.number().nonnegative().nullable(),
  completion_price_per_token: z.number().nonnegative().nullable(),
  updated_at: z.string(),
})

export const inferenceRoutingPolicySchema = z.object({
  model: z.string().min(1),
  revision: z.number().int().nonnegative(),
  enabled: z.boolean(),
  speed_weight: z.number().min(0).max(1),
  cost_weight: z.number().min(0).max(1),
  exploration_weight: z.number().min(0).max(1),
  exploration_ticket_budget: z.number().int().min(0).max(100),
  min_tool_accuracy: z.number().min(0).max(1),
  min_composite: z.number().min(0).max(1),
  min_calibration_samples: z.number().int().min(1).max(10_000),
  max_error_rate: z.number().min(0).max(1),
  max_timeout_rate: z.number().min(0).max(1),
  cooldown_seconds: z.number().int().min(1).max(3_600),
  ewma_alpha: z.number().gt(0).max(1),
  updated_at: z.string(),
})

export const inferenceRoutingAuditSchema = z.object({
  audit_id: z.string().uuid(),
  actor: z.string().min(1),
  action: z.string().min(1),
  model: z.string().min(1),
  profile_revision: z.string().nullable(),
  payload: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])),
  recorded_at: z.string(),
})

export const inferenceProviderTelemetrySchema = z
  .object({
    provider: z.string().min(1),
    request_count: z.number().int().nonnegative(),
    completed_count: z.number().int().nonnegative(),
    failed_count: z.number().int().nonnegative(),
    inflight_count: z.number().int().nonnegative(),
    timeout_count: z.number().int().nonnegative(),
    upstream_attempt_count: z.number().int().nonnegative(),
    openrouter_attempt_count: z.number().int().nonnegative().nullish().transform((value) => value ?? 0),
    recovered_after_fallback_count: z.number().int().nonnegative().nullish().transform((value) => value ?? 0),
    terminal_failure_count: z.number().int().nonnegative().nullish().transform((value) => value ?? 0),
    prompt_tokens: z.number().int().nonnegative(),
    completion_tokens: z.number().int().nonnegative(),
    cost_microusd: z.number().int().nonnegative(),
    average_latency_ms: z.number().nonnegative().nullable(),
    observed_output_tps: z.number().nonnegative().nullish().transform((value) => value ?? null),
  })
  .refine((row) => row.completed_count <= row.request_count, {
    message: 'Completed requests cannot exceed total requests',
  })
  .refine((row) => row.timeout_count <= row.request_count, {
    message: 'Timed out requests cannot exceed total requests',
  })
  .refine((row) => row.failed_count <= row.request_count, {
    message: 'Failed requests cannot exceed total requests',
  })
  .refine((row) => row.inflight_count <= row.request_count, {
    message: 'In-flight requests cannot exceed total requests',
  })

export const inferenceRouteIdentitySchema = z.object({
  model: z.string().min(1),
  provider: z.string().min(1),
  profile_revision: z.string().min(1),
  provider_sort: z.enum(['operator_order', 'throughput']),
  provider_order: z.array(z.string().min(1)),
  reliability_provider_order: z.array(z.string().min(1)).nullish().transform((value) => value ?? []),
  ignored_providers: z.array(z.string().min(1)),
  allow_fallbacks: z.literal(false),
})

export const relayRecoveryTelemetrySchema = z.object({
  benchmark_relay_abort_ticket_count: z.number().int().nonnegative().default(0),
  broker_recovery_exhausted_ticket_count: z.number().int().nonnegative().default(0),
})

export const inferenceRoutingInventorySchema = z.object({
  routing_mode: z
    .enum(['aggregate_throughput', 'adaptive'])
    .nullish()
    .transform((value) => value ?? 'aggregate_throughput'),
  aggregate_route: inferenceRouteIdentitySchema
    .nullish()
    .transform((value) => value ?? null),
  policies: z.array(inferenceRoutingPolicySchema),
  routes: z.array(inferenceRouteSchema),
  audits: z.array(inferenceRoutingAuditSchema).max(100),
  provider_telemetry: z.array(inferenceProviderTelemetrySchema).nullish().transform(
    (value) => value ?? [],
  ),
  relay_recovery_telemetry: relayRecoveryTelemetrySchema
    .nullish()
    .transform((value) => value ?? {
      benchmark_relay_abort_ticket_count: 0,
      broker_recovery_exhausted_ticket_count: 0,
    }),
})

export const inferenceRouteCalibrationInputSchema = z.object({
  profileRevision: z.string().min(1),
  model: z.string().min(1),
  provider: z.string().min(1),
  expectedRevision: z.number().int().nonnegative(),
  action: inferenceRouteCalibrationStatusSchema,
  manifestSha256: z.string().regex(/^[0-9a-f]{64}$/),
  toolAccuracy: z.number().min(0).max(1),
  composite: z.number().min(0).max(1),
  sampleCount: z.number().int().min(1),
  confirmation: z.string(),
})

export const inferenceRoutingPolicyInputSchema = z
  .object({
    model: z.string().min(1),
    expectedRevision: z.number().int().nonnegative(),
    enabled: z.boolean(),
    speedWeight: z.number().min(0).max(1),
    costWeight: z.number().min(0).max(1),
    explorationWeight: z.number().min(0).max(1),
    explorationTicketBudget: z.number().int().min(0).max(100),
    minToolAccuracy: z.number().min(0).max(1),
    minComposite: z.number().min(0).max(1),
    minCalibrationSamples: z.number().int().min(1).max(10_000),
    maxErrorRate: z.number().min(0).max(1),
    maxTimeoutRate: z.number().min(0).max(1),
    cooldownSeconds: z.number().int().min(1).max(3_600),
    ewmaAlpha: z.number().gt(0).max(1),
    confirmation: z.string(),
  })
  .refine((input) => input.speedWeight + input.costWeight + input.explorationWeight > 0, {
    message: 'Routing weights cannot all be zero',
  })

export function inferenceRouteConfirmation(
  action: z.infer<typeof inferenceRouteCalibrationStatusSchema>,
  profileRevision: string,
) {
  return `${action.toUpperCase()} INFERENCE ROUTE ${profileRevision}`
}

export function inferencePolicyConfirmation(model: string) {
  return `UPDATE INFERENCE POLICY ${model}`
}

export type InferenceRoute = z.infer<typeof inferenceRouteSchema>
export type InferenceRouteIdentity = z.infer<typeof inferenceRouteIdentitySchema>
export type InferenceRoutingPolicy = z.infer<typeof inferenceRoutingPolicySchema>
export type InferenceRoutingInventory = z.infer<typeof inferenceRoutingInventorySchema>
export type InferenceRoutingAudit = z.infer<typeof inferenceRoutingAuditSchema>
export type InferenceProviderTelemetry = z.infer<typeof inferenceProviderTelemetrySchema>
export type InferenceRouteCalibrationAction = z.infer<typeof inferenceRouteCalibrationStatusSchema>

export const quarantineResolutionSchema = z.enum(['release', 'rescreen', 'reject'])
export const screeningDisputeResolutionSchema = z.enum(['release', 'uphold'])

export const screenerReviewModeSchema = z.enum(['off', 'shadow', 'enforce', 'inherit'])
export const screenerReviewModelSchema = z.enum([
  'moonshotai/kimi-k3',
  'z-ai/glm-5.2',
  'openai/gpt-5.6-sol',
])
export const sourceReviewModelSchema = z.enum(['openai/gpt-5.6-luna'])
export const policyManifestProfileSchema = z.enum(['core', 'l1', 'l1_l2'])
export const screenerReviewSettingsSchema = z
  .object({
    mode: screenerReviewModeSchema,
    l2_model: screenerReviewModelSchema,
    l2_fallback_models: z.array(screenerReviewModelSchema).max(2),
    l3_enabled: z.boolean().default(true),
    l3_model: z.literal('openai/gpt-5.6-sol'),
    timeout_seconds: z.number().int().min(30).max(900),
    max_steps: z.number().int().min(1).max(20),
    source_review_max_steps: z.number().int().min(1).max(240).default(200),
    source_review_max_read_bytes: z.number().int().min(32_000).max(16_000_000).default(8_000_000),
    source_review_max_completion_tokens: z.number().int().min(2_000).max(32_000).default(8_000),
    source_review_reasoning_effort: z.enum(['low', 'medium', 'high']).default('high'),
    source_review_model: sourceReviewModelSchema.default('openai/gpt-5.6-luna'),
    source_review_timeout_seconds: z.number().int().min(60).max(3_600).default(1_800),
    concern_hold_count: z.number().int().min(1).max(16).default(3),
    clear_min_notes: z.number().int().min(1).max(32).default(3),
    adjudicator_mode: z.enum(['off', 'shadow', 'enforce']).default('off'),
    adjudicator_model: z.literal('z-ai/glm-5.3-flash').default('z-ai/glm-5.3-flash'),
    adjudicator_max_steps: z.number().int().min(1).max(64).default(24),
    adjudicator_timeout_seconds: z.number().int().min(60).max(3_600).default(600),
    max_input_tokens: z.number().int().min(1).max(1_000_000),
    max_output_tokens: z.number().int().min(1).max(128_000),
    max_completion_tokens: z.number().int().min(1).max(128_000),
    max_cost_usd: z.number().positive().max(10),
    critic_reasoning_effort: z.enum(['low', 'medium', 'high']),
    cache_ttl_seconds: z.number().int().min(60).max(2_592_000),
    audit_retention_days: z.number().int().min(1).max(365),
    policy_manifest_profile: policyManifestProfileSchema.default('l1'),
    policy_manifest_rotation_id: z.string().regex(/^[a-zA-Z0-9._-]{1,80}$/).default('v8-luna-source-review-behavioral-oracle'),
  })
  .superRefine((value, context) => {
    const chain = [value.l2_model, ...value.l2_fallback_models]
    if (new Set(chain).size !== chain.length) {
      context.addIssue({ code: 'custom', message: 'Model chain cannot contain duplicates' })
    }
    if (value.max_completion_tokens > value.max_output_tokens) {
      context.addIssue({
        code: 'custom',
        message: 'Completion budget cannot exceed output budget',
        path: ['max_completion_tokens'],
      })
    }
  })

export const screenerReviewRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: screenerReviewSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const appliedScreenerReviewSettingsSchema = z.object({
  instance_id: z.string(),
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  mode: screenerReviewModeSchema,
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
  source: z.enum(['platform', 'cache', 'bootstrap']),
  seen_at: z.string(),
  fresh: z.boolean(),
  matches_effective: z.boolean(),
  expected_revision: z.number().int().nonnegative(),
  expected_scope: z.string(),
  expected_checksum: z.string().regex(/^[0-9a-f]{64}$/),
  policy_manifest_profile: policyManifestProfileSchema.default('l1'),
  policy_manifest_rotation_id: z.string().default('v8-luna-source-review-behavioral-oracle'),
  policy_manifest_digest: z.string().regex(/^[0-9a-f]{64}$/).default('0'.repeat(64)),
  expected_policy_manifest_digest: z.string().regex(/^[0-9a-f]{64}$/).default('0'.repeat(64)),
})

export const screenerPolicyManifestSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  policy_version: z.number().int().positive(),
  profile: policyManifestProfileSchema,
  rotation_id: z.string(),
  digest: z.string().regex(/^[0-9a-f]{64}$/),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
})

export const shadowReviewObservationSchema = z.object({
  attempt_id: z.string(),
  agent_id: z.string(),
  settings_revision: z.number().int().nonnegative(),
  settings_scope: z.string(),
  settings_checksum: z.string().regex(/^[0-9a-f]{64}$/),
  disposition: z.enum(['safe', 'violation', 'inconclusive', 'retryable_infra']),
  risk_level: z.enum(['low', 'medium', 'high']).nullable(),
  categories: z.array(z.string()),
  finding_digest: z.string().nullable(),
  resolution_basis: z.string().nullable(),
  clearance_path: z.string().nullable(),
  critic_disposition: z.string().nullable(),
  adjudicator_disposition: z.string().nullable(),
  response_models: z.array(z.string()),
  response_providers: z.array(z.string()),
  usage: z.record(z.string(), z.number().nullable()),
  created_at: z.string(),
})

export const screenerReviewControlSchema = z.object({
  current: z.array(screenerReviewRevisionSchema),
  history: z.array(screenerReviewRevisionSchema),
  known_instances: z.array(z.string()),
  applied_instances: z.array(appliedScreenerReviewSettingsSchema),
  shadow_observations: z.array(shadowReviewObservationSchema),
  policy_manifests: z.array(screenerPolicyManifestSchema).default([]),
})

export const applyScreenerReviewSettingsInputSchema = z.object({
  scope: z.string().regex(/^(?:\*|[a-zA-Z0-9._-]{1,63})$/),
  expectedRevision: z.number().int().nonnegative(),
  settings: screenerReviewSettingsSchema,
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const rotateScreenerPolicyManifestInputSchema = z.object({
  scope: z.string().regex(/^(?:\*|[a-zA-Z0-9._-]{1,63})$/).default('*'),
  expectedRevision: z.number().int().nonnegative(),
  profile: policyManifestProfileSchema,
  rotationId: z.string().regex(/^[a-zA-Z0-9._-]{1,80}$/),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const screenerPolicyManifestControlSchema = z.object({
  current: z.array(screenerPolicyManifestSchema),
  history: z.array(screenerPolicyManifestSchema),
  applied_instances: z.array(appliedScreenerReviewSettingsSchema.pick({
    instance_id: true,
    revision: true,
    scope: true,
    seen_at: true,
    fresh: true,
    matches_effective: true,
    policy_manifest_profile: true,
    policy_manifest_rotation_id: true,
    policy_manifest_digest: true,
    expected_policy_manifest_digest: true,
  })),
})

export type ScreenerReviewControl = z.infer<typeof screenerReviewControlSchema>
export type ScreenerReviewSettings = z.infer<typeof screenerReviewSettingsSchema>

const screenerProviderSchema = z.enum(['gcp', 'targon', 'hetzner', 'home', 'test'])
const capacityProviderSchema = z.enum(['hetzner', 'targon', 'gcp'])
const providerPrioritySchema = z.array(capacityProviderSchema).min(1).max(3).superRefine((value, context) => {
  if (new Set(value).size !== value.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'Provider priorities must be unique.' })
  }
  if (!value.includes('gcp')) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'Provider priorities must retain GCE fallback.' })
  }
})

export const screenerProviderSettingsSchema = z.object({
  build_provider_priority: providerPrioritySchema,
  runtime_provider_priority: providerPrioritySchema,
  source_review_provider_priority: providerPrioritySchema,
  gce_overflow_enabled: z.boolean().default(false),
  primary_node_id: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/).nullable().default(null),
  gce_overflow_backlog_multiplier: z.number().int().min(2).max(20).default(3),
  gce_overflow_min_backlog: z.number().int().min(1).max(1000).default(12),
  gce_overflow_max_instances: z.number().int().min(0).max(32).default(6),
})

export const screenerProviderSettingsRevisionSchema = z.object({
  environment: z.string().min(1),
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  settings: screenerProviderSettingsSchema,
  reason: z.string().min(1),
  actor: z.string().min(1),
  created_at: z.string().nullable(),
})

export const screenerProviderSettingsControlSchema = z.object({
  current: screenerProviderSettingsRevisionSchema,
  history: z.array(screenerProviderSettingsRevisionSchema),
})

export const setScreenerProviderSettingsInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  settings: screenerProviderSettingsSchema,
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

const screenerBootstrapGrantBaseSchema = z.object({
  nodeId: z.string().regex(/^[a-zA-Z0-9._-]{1,63}$/),
  provider: screenerProviderSchema,
  providerResourceId: z.string().min(1).max(200),
  imageReference: z.string().regex(
    /^[a-z0-9.-]+(?::[0-9]+)?\/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$/,
  ),
  expectedControllerEpoch: z.string().regex(/^[a-zA-Z0-9._:-]{1,100}$/),
  reason: auditReasonSchema(8),
})

export function screenerBootstrapGrantConfirmation(
  input: z.infer<typeof screenerBootstrapGrantBaseSchema>,
) {
  return `CREATE SCREENER BOOTSTRAP GRANT NODE=${input.nodeId} PROVIDER=${input.provider} RESOURCE=${input.providerResourceId} IMAGE=${input.imageReference}`
}

export const createScreenerBootstrapGrantInputSchema = screenerBootstrapGrantBaseSchema
  .extend({ confirmation: z.string() })
  .superRefine((input, context) => {
    const expected = screenerBootstrapGrantConfirmation(input)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['confirmation'],
        message: `confirmation must be exactly ${expected}`,
      })
    }
  })

export const screenerBootstrapGrantResponseSchema = z.object({
  grant_id: z.string().uuid(),
  registration_token: z.string().min(43).max(128),
  expires_at: z.string(),
})

export function screenerProviderSettingsConfirmation(
  settings: z.infer<typeof screenerProviderSettingsSchema>,
) {
  const overflow = settings.gce_overflow_enabled
    ? `ENABLED:${settings.primary_node_id}:${settings.gce_overflow_backlog_multiplier}X:MIN=${settings.gce_overflow_min_backlog}:MAX=${settings.gce_overflow_max_instances}`
    : 'DISABLED'
  return `APPLY SCREENER PROVIDERS BUILDS=${settings.build_provider_priority.join('>')} RUNTIME=${settings.runtime_provider_priority.join('>')} SOURCE_REVIEW=${settings.source_review_provider_priority.join('>')} GCE_OVERFLOW=${overflow}`
}

export const screenerNodeChannelSettingsSchema = z.object({
  screening_concurrency: z.number().int().min(0).max(32),
  sandbox_slots: z.number().int().min(0).max(16),
  build_concurrency: z.number().int().min(0).max(16),
  runtime_concurrency: z.number().int().min(0).max(16),
  source_review_concurrency: z.number().int().min(0).max(32),
})

export const screenerNodeChannelSettingsRevisionSchema = z.object({
  environment: z.string().min(1),
  node_id: z.string().min(1),
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  settings: screenerNodeChannelSettingsSchema,
  reason: z.string().min(1),
  actor: z.string().min(1),
  created_at: z.string().nullable(),
})

export const screenerNodeChannelSettingsControlSchema = z.object({
  current: screenerNodeChannelSettingsRevisionSchema,
  history: z.array(screenerNodeChannelSettingsRevisionSchema),
  usage: z.object({
    screening_active: z.number().int().nonnegative(),
    sandbox_active: z.number().int().nonnegative(),
    build_active: z.number().int().nonnegative(),
    runtime_active: z.number().int().nonnegative(),
    source_review_active: z.number().int().nonnegative(),
  }).nullable(),
})

export const setScreenerNodeChannelSettingsInputSchema = z.object({
  nodeId: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/),
  expectedRevision: z.number().int().nonnegative(),
  settings: screenerNodeChannelSettingsSchema,
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export function screenerNodeChannelSettingsConfirmation(
  nodeId: string,
  settings: z.infer<typeof screenerNodeChannelSettingsSchema>,
) {
  return `APPLY SCREENER NODE ${nodeId} SCREENING=${settings.screening_concurrency} SANDBOX=${settings.sandbox_slots} BUILD=${settings.build_concurrency} RUNTIME=${settings.runtime_concurrency} SOURCE_REVIEW=${settings.source_review_concurrency}`
}
const screenerNodeStatusSchema = z.enum(['active', 'draining', 'quarantined', 'revoked'])

export const screenerCapacityEventSchema = z.object({
  event_id: z.string().uuid(),
  event_type: z.string().min(1),
  provider: screenerProviderSchema.nullable(),
  node_id: z.string().nullable(),
  detail: z.string().min(1),
  controller_epoch: z.string().min(1),
  created_at: z.string(),
})

export const screenerCapacitySnapshotSchema = z.object({
  environment: z.string().min(1),
  controller_epoch: z.string().min(1),
  provider_settings_revision: z.number().int().nonnegative(),
  runnable_backlog: z.number().int().nonnegative(),
  active_leases: z.number().int().nonnegative(),
  desired_slots: z.number().int().nonnegative(),
  global_cap: z.number().int().nonnegative(),
  targon_capability: z.enum(['go', 'nogo', 'unknown']),
  targon_available: z.number().int().nonnegative(),
  targon_healthy: z.number().int().nonnegative(),
  targon_pending: z.number().int().nonnegative(),
  targon_draining: z.number().int().nonnegative(),
  gce_target: z.number().int().nonnegative(),
  gce_healthy: z.number().int().nonnegative(),
  gce_pending: z.number().int().nonnegative(),
  gce_draining: z.number().int().nonnegative(),
  fallback_reason: z.string().nullable(),
  last_provider_success_at: z.string().nullable(),
  last_provider_error_code: z.string().nullable(),
  last_provider_error_at: z.string().nullable(),
  events: z.array(screenerCapacityEventSchema).default([]),
  controller_heartbeat_at: z.string(),
  controller_lease_expires_at: z.string(),
  updated_at: z.string(),
})

/** Hardware the worker announced about itself in its own signed heartbeat
 * (protocol v6+), not what the provider was asked to allocate. Absent while a
 * node has yet to report, and during a Backroom-first rolling deploy. */
export const screenerHostSpecsSchema = z.object({
  cpu_count: z.number().int().positive(),
  cpu_physical_cores: z.number().int().positive().nullish().transform((value) => value ?? null),
  memory_total_mib: z.number().int().positive(),
  disk_total_gib: z.number().int().positive(),
  architecture: z.string().min(1),
})

export const screenerCapacityNodeSchema = z.object({
  environment: z.string().min(1),
  node_id: z.string().min(1),
  provider: screenerProviderSchema,
  provider_resource_id: z.string().min(1),
  screener_hotkey: z.string().min(1),
  status: screenerNodeStatusSchema,
  capacity: z.number().int().positive(),
  token_expires_at: z.string(),
  registered_at: z.string(),
  rotated_at: z.string(),
  revoked_at: z.string().nullable(),
  status_reason: z.string().nullable(),
  heartbeat_seen_at: z.string().nullable(),
  software_version: z.string().nullable(),
  protocol_version: z.number().int().nullable(),
  policy_version: z.number().int().nullable(),
  current_phase: z.string().nullable(),
  host_specs: screenerHostSpecsSchema.nullish().transform((value) => value ?? null),
})

export const trustedImageBuildSchema = z.object({
  build_id: z.string().uuid(),
  environment: z.string().min(1),
  component: z.literal('screener'),
  source_repository: z.string().url(),
  source_sha: z.string().regex(/^[0-9a-f]{40}$/),
  context_path: z.literal('.'),
  dockerfile_path: z.literal('workers/screener/Dockerfile'),
  destination: z.string().min(1),
  status: z.enum([
    'queued',
    'leased',
    'running',
    'succeeded',
    'failed',
    'fallback_required',
    'canceled',
  ]),
  provider: z.enum(['hetzner', 'targon', 'gcp']).nullable(),
  provider_resource_id: z.string().nullable(),
  image_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/).nullable(),
  error_code: z.string().nullable(),
  attempt_count: z.number().int().nonnegative(),
  controller_epoch: z.string().nullable(),
  lease_expires_at: z.string().nullable(),
  created_by: z.string().min(1),
  reason: z.string().min(1),
  created_at: z.string(),
  started_at: z.string().nullable(),
  completed_at: z.string().nullable(),
  updated_at: z.string(),
})

export const retryTrustedImageBuildInputSchema = z.object({
  buildId: z.string().uuid(),
  expectedStatus: z.enum(['failed', 'fallback_required', 'canceled']),
  expectedAttemptCount: z.number().int().min(1),
  reason: auditReasonSchema(8),
})

export const screenerCapacityViewSchema = z.object({
  snapshot: screenerCapacitySnapshotSchema.nullable(),
  nodes: z.array(screenerCapacityNodeSchema),
  events: z.array(screenerCapacityEventSchema),
  builds: z.array(trustedImageBuildSchema).default([]),
  provider_jobs: z.array(z.object({
    job_id: z.string().uuid(),
    lane: z.enum(['build', 'runtime', 'source_review']),
    status: z.string().min(1),
    provider: z.enum(['hetzner', 'targon', 'gcp']).nullable(),
    node_id: z.string().nullable().default(null),
    provider_resource_id: z.string().nullable(),
    image_reference: z.string().nullable(),
    error_code: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })).default([]),
  provider_control: screenerProviderSettingsControlSchema.default({
    current: {
      environment: 'prod',
      revision: 0,
      parent_revision: 0,
      settings: {
        build_provider_priority: ['targon', 'gcp'],
        runtime_provider_priority: ['targon', 'gcp'],
        source_review_provider_priority: ['targon', 'gcp'],
        gce_overflow_enabled: false,
        primary_node_id: null,
        gce_overflow_backlog_multiplier: 3,
        gce_overflow_min_backlog: 12,
        gce_overflow_max_instances: 6,
      },
      reason: 'Built-in single-shot Targon settings',
      actor: 'platform',
      created_at: null,
    },
    history: [],
  }),
  node_controls: z.array(screenerNodeChannelSettingsControlSchema).default([]),
})

export type ScreenerCapacityView = z.infer<typeof screenerCapacityViewSchema>
export type ScreenerCapacityNode = z.infer<typeof screenerCapacityNodeSchema>
export type ScreenerHostSpecs = z.infer<typeof screenerHostSpecsSchema>
export type TrustedImageBuild = z.infer<typeof trustedImageBuildSchema>
export type ScreenerProviderSettings = z.infer<typeof screenerProviderSettingsSchema>
export type ScreenerProviderSettingsControl = z.infer<typeof screenerProviderSettingsControlSchema>
export type ScreenerNodeChannelSettings = z.infer<typeof screenerNodeChannelSettingsSchema>
export type ScreenerNodeChannelSettingsControl = z.infer<typeof screenerNodeChannelSettingsControlSchema>

export const ARTIFACT_RELEASE_MIN_HOURS = 6
// Mirrors the platform's range bound. 48 hours is still the community-agreed
// default (ARTIFACT_RELEASE_DEFAULT_HOURS); the ceiling only bounds what an
// operator may choose, so the console can offer week-, month- and year-long
// windows. One year is where the finite range stops because past it the
// honest value is `never`, which is a policy rather than a duration — so the
// range and the terminal option meet with no gap between them.
export const ARTIFACT_RELEASE_MAX_HOURS = 8760
export const ARTIFACT_RELEASE_DEFAULT_HOURS = 48

// Whether public release ever happens. A second axis on the same policy, not
// a very large `embargoHours`: "never" is not 8761 hours, and a number that
// secretly meant forever would be ambiguous in the check constraint, in this
// schema and on the stage row alike.
export const SOURCE_DISCLOSURE_VALUES = ['public', 'never'] as const
export const sourceDisclosureSchema = z.enum(SOURCE_DISCLOSURE_VALUES)
export type SourceDisclosure = z.infer<typeof sourceDisclosureSchema>

export const artifactReleaseRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  // Defaulted, not optional: a platform build predating the field answers
  // `public`, which is the status quo and the visible direction to fail in.
  disclosure: sourceDisclosureSchema.default('public'),
  embargo_hours: z
    .number()
    .int()
    .min(ARTIFACT_RELEASE_MIN_HOURS)
    .max(ARTIFACT_RELEASE_MAX_HOURS),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string().nullable(),
})

export const artifactReleaseControlSchema = z.object({
  current: artifactReleaseRevisionSchema,
  history: z.array(artifactReleaseRevisionSchema).max(100),
})

// `z.object` strips what it does not declare, and this board stores a whole
// policy, so anything missing here is reset to its default on every save. That
// has bitten six times; on a release-visibility setting it would mean the
// subnet quietly going public again after an unrelated window change. Declare
// every field the platform's request model carries.
export const updateArtifactReleaseSettingsInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  disclosure: sourceDisclosureSchema.default('public'),
  // Required and in range even under `never`, matching the platform: it is
  // retained rather than used, so returning to `public` restores the window
  // the subnet last agreed on instead of forcing one to be re-chosen.
  embargoHours: z
    .number()
    .int()
    .min(ARTIFACT_RELEASE_MIN_HOURS)
    .max(ARTIFACT_RELEASE_MAX_HOURS),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

/**
 * The exact phrase an operator must type. `never` gets its own wording rather
 * than an hour count, because no hour count means it — and because a phrase
 * differing from the embargo one only in a number would be submitted by habit.
 */
export function artifactReleaseConfirmation(
  hours: number,
  disclosure: SourceDisclosure = 'public',
) {
  if (disclosure === 'never') return 'SET SOURCE DISCLOSURE NEVER'
  return `SET SOURCE EMBARGO ${hours} HOURS`
}

/**
 * A day-scale gloss for windows too long to read in hours, or `null` when the
 * hour count already reads plainly. Hours stay the primary unit everywhere —
 * the platform stores hours and the confirmation phrase quotes hours — so this
 * is only ever shown alongside the number, never instead of it.
 */
export function artifactReleaseWindowGloss(hours: number) {
  if (hours < 72) return null
  const days = hours / 24
  const whole = Math.floor(days)
  const remainder = hours % 24
  if (remainder === 0) return `${whole} days`
  return `${whole}d ${remainder}h`
}

export type ArtifactReleaseControl = z.infer<typeof artifactReleaseControlSchema>

export const SUBMISSION_COOLDOWN_MIN_SECONDS = 60
export const SUBMISSION_COOLDOWN_MAX_SECONDS = 86_400
export const SUBMISSION_FEE_MIN_RAO = 1
export const SUBMISSION_FEE_MAX_RAO = 1_000_000_000_000
export const RAO_PER_TAO = 1_000_000_000

export const submissionSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  cooldown_seconds: z
    .number()
    .int()
    .min(SUBMISSION_COOLDOWN_MIN_SECONDS)
    .max(SUBMISSION_COOLDOWN_MAX_SECONDS),
  fee_amount_rao: z.number().int().min(SUBMISSION_FEE_MIN_RAO).max(SUBMISSION_FEE_MAX_RAO),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string().nullable(),
})

export const submissionSettingsControlSchema = z.object({
  current: submissionSettingsRevisionSchema,
  history: z.array(submissionSettingsRevisionSchema).max(100),
})

export const updateSubmissionSettingsInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  cooldownSeconds: z
    .number()
    .int()
    .min(SUBMISSION_COOLDOWN_MIN_SECONDS)
    .max(SUBMISSION_COOLDOWN_MAX_SECONDS),
  feeAmountRao: z.number().int().min(SUBMISSION_FEE_MIN_RAO).max(SUBMISSION_FEE_MAX_RAO),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export function submissionSettingsConfirmation(seconds: number, feeAmountRao: number) {
  return `SET SUBMISSION COOLDOWN ${seconds} SECONDS FEE ${feeAmountRao} RAO`
}

export type SubmissionSettingsControl = z.infer<typeof submissionSettingsControlSchema>

export const activeHotkeyBanSchema = z.object({
  hotkey: z.string().min(1),
  reason: z.string().nullable(),
  banned_at: z.string().datetime({ offset: true }),
} satisfies PlatformResponseShape<GeneratedAdminActiveHotkeyBan>)

export const hotkeyBanAuditEntrySchema = z.object({
  seq: z.number().int().positive(),
  hotkey: z.string().min(1),
  action: z.literal('unban'),
  actor: z.string().min(1),
  reason: z.string().min(1),
  previous_reason: z.string().nullable(),
  previous_banned_at: z.string().datetime({ offset: true }),
  recorded_at: z.string().datetime({ offset: true }),
} satisfies PlatformResponseShape<GeneratedAdminHotkeyBanAuditEntry>)

export const hotkeyBanControlSchema = z.object({
  hotkey: z.string().min(1),
  banned: z.boolean(),
  active_ban: activeHotkeyBanSchema.nullable(),
  history: z.array(hotkeyBanAuditEntrySchema).max(100),
} satisfies PlatformResponseShape<GeneratedAdminHotkeyBanControl>)

export const hotkeyBanListSchema = z.object({
  total: z.number().int().nonnegative(),
  bans: z.array(activeHotkeyBanSchema).max(200),
} satisfies PlatformResponseShape<GeneratedAdminHotkeyBanList>)

export const hotkeyBanLookupInputSchema = z.object({
  hotkey: z.string().trim().min(3).max(96),
  historyLimit: z.number().int().min(0).max(100).default(20),
})

export const unbanHotkeyInputSchema = z
  .object({
    hotkey: z.string().trim().min(3).max(96),
    expectedBannedAt: z.string().datetime({ offset: true }),
    reason: z.string().trim().min(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = `UNBAN HOTKEY ${input.hotkey}`
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: `confirmation must be exactly ${expected}`,
      })
    }
  })

export const hotkeyUnbanResponseSchema = z.object({
  hotkey: z.string().min(1),
  banned: z.literal(false),
  action: hotkeyBanAuditEntrySchema,
} satisfies PlatformResponseShape<GeneratedAdminHotkeyUnbanResponse>)

export function unbanHotkeyConfirmation(hotkey: string) {
  return `UNBAN HOTKEY ${hotkey}`
}

export type HotkeyBanControl = z.infer<typeof hotkeyBanControlSchema>

// SN118 relative token-efficiency bonus (bench_version >= 7).
//
// Subnet scoring policy owned by ditto-platform and stored as an append-only
// revision beside the score ledger, resolved at compute time so an operator can
// enable, retune, fold, or roll back the bonus with no platform redeploy.
// Deliberately NOT the product feature-flag system fronted by
// `update_feature_flag` / `set_feature_flag_override`: those are boolean,
// per-user/company/domain product entitlements served by `backend` from a
// different database.
//
// The bounds below mirror ditto-platform's `check_config` exactly, so operator
// input the platform would reject never reaches the admin API. Factor
// envelopes are `(0, 1]` / `[1, 100]`; 0.85 / 1.10 remain the seed defaults.
export const EFFICIENCY_BONUS_SCOPE = '*'
export const EFFICIENCY_BONUS_MAX_CAP = 0.1
export const EFFICIENCY_FACTOR_MINIMUM = 0
export const EFFICIENCY_FACTOR_MAXIMUM = 100
export const EFFICIENCY_FACTOR_DEFAULT_MINIMUM = 0.85
export const EFFICIENCY_FACTOR_DEFAULT_MAXIMUM = 1.1

const efficiencyBonusSettingsShape = {
  enabled: z.boolean(),
  fold_enabled: z.boolean(),
  cap: z.number().gt(0).max(EFFICIENCY_BONUS_MAX_CAP),
  deep_cap: z.number().gt(0).max(EFFICIENCY_BONUS_MAX_CAP),
  deep_frontier_ratio: z.number().gt(0).lt(1),
  // Defaults keep revisions written before the bounded v3 curve readable.
  factor_alpha: z.number().gt(0).max(1).default(0.25),
  minimum_factor: z
    .number()
    .gt(EFFICIENCY_FACTOR_MINIMUM)
    .max(1)
    .default(EFFICIENCY_FACTOR_DEFAULT_MINIMUM),
  maximum_factor: z
    .number()
    .min(1)
    .max(EFFICIENCY_FACTOR_MAXIMUM)
    .default(EFFICIENCY_FACTOR_DEFAULT_MAXIMUM),
  cohort_size: z.number().int().min(2),
  min_cohort: z.number().int().min(2),
  epoch_hours: z.number().int().min(1),
  quality_floor: z.number().min(0).max(1),
  memory_floor: z.number().min(0).max(1),
}

type EfficiencyBonusSettingsEnvelope = {
  cap: number
  deep_cap: number
  minimum_factor: number
  maximum_factor: number
  cohort_size: number
  min_cohort: number
}

function refineEfficiencyBonusSettings(
  value: EfficiencyBonusSettingsEnvelope,
  context: z.RefinementCtx,
) {
  if (value.cap > value.deep_cap) {
    context.addIssue({
      code: 'custom',
      message: 'deep_cap must satisfy cap <= deep_cap <= 0.10',
      path: ['deep_cap'],
    })
  }
  if (value.minimum_factor > value.maximum_factor) {
    context.addIssue({
      code: 'custom',
      message: 'minimum_factor must not exceed maximum_factor',
      path: ['minimum_factor'],
    })
  }
  if (value.cohort_size < value.min_cohort) {
    context.addIssue({
      code: 'custom',
      message: 'cohort_size must be at least min_cohort',
      path: ['cohort_size'],
    })
  }
}

export const efficiencyBonusSettingsSchema = z
  .object(efficiencyBonusSettingsShape)
  .superRefine(refineEfficiencyBonusSettings)

// Whole-policy writes must name the v3 authority knobs. Read schemas retain
// defaults for historical rows, but an older admin client must not silently
// reset a non-default policy while changing an unrelated setting.
export const efficiencyBonusSettingsWriteSchema = z
  .object({
    ...efficiencyBonusSettingsShape,
    factor_alpha: z.number().gt(0).max(1),
    minimum_factor: z.number().gt(EFFICIENCY_FACTOR_MINIMUM).max(1),
    maximum_factor: z.number().min(1).max(EFFICIENCY_FACTOR_MAXIMUM),
  })
  .superRefine(refineEfficiencyBonusSettings)

export const efficiencyBonusSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: efficiencyBonusSettingsSchema,
  checksum_settings: z.record(z.string(), z.unknown()),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveEfficiencyBonusSettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: efficiencyBonusSettingsSchema,
  checksum_settings: z.record(z.string(), z.unknown()),
  // Revision 0 is the deployment env seed. It was never written to a row, so
  // it carries no checksum.
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'seed']),
  // The platform clamps `fold requires enabled` at read time, so the fold can
  // be persisted true while folding nothing.
  fold_effective: z.boolean(),
  max_age_seconds: z.number().nonnegative(),
})

export const efficiencyBonusSettingsControlSchema = z.object({
  current: z.array(efficiencyBonusSettingsRevisionSchema),
  history: z.array(efficiencyBonusSettingsRevisionSchema),
  seed_default: efficiencyBonusSettingsSchema,
  effective: effectiveEfficiencyBonusSettingsSchema,
})

export function efficiencyBonusConfirmation(enabled: boolean) {
  return `APPLY EFFICIENCY BONUS ${enabled ? 'ENABLED' : 'DISABLED'}`
}

export const setEfficiencyBonusSettingsInputSchema = z
  .object({
    scope: z.literal(EFFICIENCY_BONUS_SCOPE).default(EFFICIENCY_BONUS_SCOPE),
    expectedRevision: z.number().int().nonnegative(),
    settings: efficiencyBonusSettingsWriteSchema,
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = efficiencyBonusConfirmation(input.settings.enabled)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
    // The platform stores this revision but folds nothing while the master
    // switch is off. Refuse it here so an operator never reads a persisted
    // fold as a live one.
    if (input.settings.fold_enabled && !input.settings.enabled) {
      context.addIssue({
        code: 'custom',
        message: 'fold_enabled requires enabled; the fold is clamped off while the bonus is off',
        path: ['settings', 'fold_enabled'],
      })
    }
  })

export type EfficiencyBonusSettings = z.infer<typeof efficiencyBonusSettingsSchema>
export type EfficiencyBonusSettingsControl = z.infer<
  typeof efficiencyBonusSettingsControlSchema
>

// SN118 emission burn.
//
// `burn_share` is the fraction of miner emission the validator weight fold
// routes to the subnet owner's burn hotkey; `1 - burn_share` is normalized
// across the eligible miner weights, so this scales the competitive vector
// without re-ordering it. It reaches validators on the scoring ledger, which
// they read before every weight submission — but a validator that has already
// submitted this epoch keeps its vector until the next one, so the subnet-wide
// effect lands over roughly an epoch rather than at once.
export const BURN_SETTINGS_SCOPE = '*'
export const BURN_CONFIRMATION = 'APPLY BURN SETTINGS'
// Bounds mirror the platform's. `1.0` is a real setting rather than a footgun
// to forbid: it is the same all-to-burn vector the fold already submits when no
// agent holds a positive score.
export const MIN_BURN_SHARE = 0
export const MAX_BURN_SHARE = 1

const burnSettingsBaseSchema = z.object({
  burn_share: z.number().min(MIN_BURN_SHARE).max(MAX_BURN_SHARE),
})

export const burnSettingsSchema = burnSettingsBaseSchema
export const burnSettingsWriteSchema = burnSettingsBaseSchema

export const burnSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: burnSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveBurnSettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: burnSettingsSchema,
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'default']),
  max_age_seconds: z.number().nonnegative(),
  // What the fold actually takes. Derived by the platform from the same value,
  // so it is reported rather than recomputed here and the two cannot disagree.
  miner_emission_share: z.number().min(0).max(1),
  // Bounds come from the platform so this page cannot offer a share it refuses.
  min_burn_share: z.number().min(0).max(1).default(MIN_BURN_SHARE),
  max_burn_share: z.number().min(0).max(1).default(MAX_BURN_SHARE),
  // Zero live validators means the dial is not attached to anything, which is
  // worth seeing before rather than after applying a burn.
  live_validator_count: z.number().int().nonnegative().nullable().default(null),
})

export const burnSettingsControlSchema = z.object({
  current: z.array(burnSettingsRevisionSchema),
  history: z.array(burnSettingsRevisionSchema),
  default: burnSettingsSchema,
  effective: effectiveBurnSettingsSchema,
})

export const setBurnSettingsInputSchema = z.object({
  scope: z.literal(BURN_SETTINGS_SCOPE).default(BURN_SETTINGS_SCOPE),
  expectedRevision: z.number().int().nonnegative(),
  settings: burnSettingsWriteSchema,
  reason: auditReasonSchema(8),
  confirmation: z.literal(BURN_CONFIRMATION),
})

export type BurnSettings = z.infer<typeof burnSettingsSchema>
export type BurnSettingsWrite = z.infer<typeof burnSettingsWriteSchema>
export type BurnSettingsControl = z.infer<typeof burnSettingsControlSchema>

export const CONTINUAL_RETEST_SETTINGS_SCOPE = '*'
export const CONTINUAL_RETEST_CONFIRMATION = 'APPLY CONTINUAL RETEST SETTINGS'

// The emission set is five, and it is the floor of the retest cohort rather
// than a value an operator can lower. The ceiling exists because every extra
// cohort member is real validator work on every wave seed. Both mirror the
// platform's own bounds so input it would reject never leaves this page; the
// platform stays the authority and its 422 detail is surfaced verbatim.
export const EMISSION_SET_SIZE = 5
export const MAX_RETEST_COHORT_SIZE = 25
// The tie-tolerance band is measured in standard errors. Past three the band
// stops meaning "tied" and starts meaning "nearby", so the platform hard-stops
// there and the only thing still bounding the cohort would be the size ceiling.
export const MAX_RETEST_ELIGIBILITY_Z = 3
export const DEFAULT_RETEST_ELIGIBILITY_Z = 1.64
// Not `strict`. The platform ships `participants` deliberately, and `strict` is
// kept as the audited rollback path rather than as the default.
export const DEFAULT_WAVE_MEMBERSHIP = 'participants'

// The ceiling can never cut into the cohort the rank cutoff already admitted.
// Mirrored from the platform's own model validator so the operator sees which
// field is wrong here instead of a bare 422 after the round trip.
const checkCohortCeiling = (
  settings: { retest_cohort_size: number; retest_cohort_max_size: number },
  ctx: z.RefinementCtx,
) => {
  if (settings.retest_cohort_max_size < settings.retest_cohort_size) {
    ctx.addIssue({
      code: 'custom',
      path: ['retest_cohort_max_size'],
      message: `retest_cohort_max_size (${settings.retest_cohort_max_size}) cannot be below retest_cohort_size (${settings.retest_cohort_size}): the ceiling would cut into the cohort the fixed rank already admitted`,
    })
  }
}

// Every field is defaulted on the read path so this page keeps rendering against
// a platform that predates it; the platform owns the contract and ships each
// field first. Defaults mirror the platform's, so a defaulted read reports what
// that build actually does.
const continualRetestSettingsBaseSchema = z.object({
  aggregate_mode: z.enum(['disabled', 'fleet_ready', 'enabled']),
  tie_weighting_mode: z.enum(['disabled', 'fleet_ready']).default('disabled'),
  idle_retests_enabled: z.boolean(),
  rollout_standdown: z
    .enum(['off', 'capable_validators', 'all'])
    .default('capable_validators'),
  // Changes what validators weight: it widens the estimator behind
  // `official_composite`, so it re-orders the tail and moves emission shares.
  // `strict` is the rollback path to the pre-#489 fold, one audited revision
  // away and with no redeploy — which is exactly why it has to be writable
  // from here.
  //
  // This is the one field whose read default is NOT what a platform missing it
  // does. The default mirrors the platform's, so a revision that stores the
  // field reads back correctly; but a build old enough to omit it predates
  // #489 and is therefore folding `strict`. `field_support` is what tells the
  // two apart, and CONTINUAL_RETEST_EXTENDED_FIELDS carries `strict` as the
  // legacy value for exactly this reason.
  wave_membership: z
    .enum(['strict', 'participants', 'per_agent'])
    .default(DEFAULT_WAVE_MEMBERSHIP),
  retest_cohort_size: z
    .number()
    .int()
    .min(EMISSION_SET_SIZE)
    .max(MAX_RETEST_COHORT_SIZE)
    .default(EMISSION_SET_SIZE),
  retest_eligibility_mode: z.enum(['fixed', 'statistical']).default('fixed'),
  retest_eligibility_z: z
    .number()
    .min(0)
    .max(MAX_RETEST_ELIGIBILITY_Z)
    .default(DEFAULT_RETEST_ELIGIBILITY_Z),
  retest_cohort_max_size: z
    .number()
    .int()
    .min(EMISSION_SET_SIZE)
    .max(MAX_RETEST_COHORT_SIZE)
    .default(MAX_RETEST_COHORT_SIZE),
})

export const continualRetestSettingsSchema =
  continualRetestSettingsBaseSchema.superRefine(checkCohortCeiling)

// A revision stores the whole policy, so an omitted field is not "leave it
// alone" — it is a write of the default. Reading tolerates a missing field (an
// older platform, an older revision); writing does not, or an MCP caller
// flipping the idle switch would silently collapse a top-25 cohort back to the
// emission set, return the fold to `strict`, and throw away a tie band.
//
// Refinement is applied after the extend rather than inherited: zod refuses to
// overwrite keys on a schema that already carries refinements.
export const continualRetestSettingsWriteSchema = continualRetestSettingsBaseSchema
  .extend({
    tie_weighting_mode: z.enum(['disabled', 'fleet_ready']),
    wave_membership: z.enum(['strict', 'participants', 'per_agent']),
    retest_cohort_size: z.number().int().min(EMISSION_SET_SIZE).max(MAX_RETEST_COHORT_SIZE),
    retest_eligibility_mode: z.enum(['fixed', 'statistical']),
    retest_eligibility_z: z.number().min(0).max(MAX_RETEST_ELIGIBILITY_Z),
    retest_cohort_max_size: z
      .number()
      .int()
      .min(EMISSION_SET_SIZE)
      .max(MAX_RETEST_COHORT_SIZE),
  })
  .superRefine(checkCohortCeiling)

export const continualRetestSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: continualRetestSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveContinualRetestSettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: continualRetestSettingsSchema,
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'default']),
  fleet_protocol_ready: z.boolean(),
  aggregate_active: z.boolean(),
  tie_weighting_fleet_ready: z.boolean().default(false),
  tie_weighting_active: z.boolean().default(false),
  max_age_seconds: z.number().nonnegative(),
  open_rollout_desired_version: z.number().int().positive().nullable().default(null),
  rollout_standdown_active: z.boolean().default(false),
  // Bounds and field depth come from the platform rather than being hardcoded
  // here, so this page cannot drift into offering a cohort the platform refuses.
  emission_set_size: z.number().int().positive().default(EMISSION_SET_SIZE),
  max_retest_cohort_size: z.number().int().positive().default(MAX_RETEST_COHORT_SIZE),
  max_retest_eligibility_z: z.number().positive().default(MAX_RETEST_ELIGIBILITY_Z),
  eligible_agent_count: z.number().int().nonnegative().nullable().default(null),
  // What the ranking actually admitted once ties at the cutoff were absorbed,
  // as against `retest_cohort_size`, which is what the operator asked for. The
  // two differ only in `statistical` mode and that difference is the whole
  // point of the mode, so it belongs on the page rather than in validator logs.
  resolved_cohort_size: z.number().int().nonnegative().nullable().default(null),
})

export const continualRetestSettingsControlSchema = z.object({
  current: z.array(continualRetestSettingsRevisionSchema),
  history: z.array(continualRetestSettingsRevisionSchema),
  default: continualRetestSettingsSchema,
  effective: effectiveContinualRetestSettingsSchema,
})

export type ContinualRetestSettingsWrite = z.infer<typeof continualRetestSettingsWriteSchema>

/**
 * The policy fields the platform grew after this page first shipped.
 *
 * Backroom and the platform deploy separately, so every one of these can be
 * absent from the build actually answering. Reading defaults them in so the
 * page still renders, which means the parsed policy cannot tell "the lane
 * rescores five" from "this build has never heard of the field". Only the raw
 * payload can, and the write path needs that difference: the platform request
 * model forbids unknown fields, so sending one it does not carry fails the
 * whole revision with a bare 422 — taking down the aggregate mode, the idle
 * switch, and the stand-down policy, which that build understands perfectly
 * well.
 *
 * `legacyValue` is what the field is worth on a build that lacks it — the
 * setting under which dropping the field from the request changes nothing.
 * Adding the next platform field means adding one entry here, not another pair
 * of bespoke helpers.
 */
type ContinualRetestExtendedFieldSpec = {
  field: keyof ContinualRetestSettingsWrite
  /** Noun phrase for the operator-facing refusal. */
  label: string
  legacyValue: (bounds: { emission_set_size: number; max_retest_cohort_size: number }) => unknown
  /** What a build without the field does instead. */
  legacyBehaviour: (bounds: {
    emission_set_size: number
    max_retest_cohort_size: number
  }) => string
}

export const CONTINUAL_RETEST_EXTENDED_FIELDS: ReadonlyArray<ContinualRetestExtendedFieldSpec> = [
  {
    field: 'tie_weighting_mode',
    label: 'a tie-aware weight policy',
    legacyValue: () => 'disabled',
    legacyBehaviour: () => 'fixed KOTH rank shares remain in effect',
  },
  {
    field: 'retest_cohort_size',
    label: 'a retest cohort size',
    legacyValue: (bounds) => bounds.emission_set_size,
    legacyBehaviour: (bounds) =>
      `the lane rescores the emission set (top ${bounds.emission_set_size})`,
  },
  {
    field: 'wave_membership',
    label: 'a wave membership policy',
    // A build without the field is the pre-#489 fold, which is `strict`.
    legacyValue: () => 'strict',
    legacyBehaviour: () =>
      'the fold intersects over every current emission-set member (strict)',
  },
  {
    field: 'retest_eligibility_mode',
    label: 'a retest eligibility mode',
    legacyValue: () => 'fixed',
    legacyBehaviour: () => 'the cohort cuts at exactly the configured rank (fixed)',
  },
  {
    field: 'retest_eligibility_z',
    label: 'a tie-tolerance band',
    // There is no band to widen on such a build, so only the shipped width is
    // a request it can honour by omission.
    legacyValue: () => DEFAULT_RETEST_ELIGIBILITY_Z,
    legacyBehaviour: () => 'no tie band is applied at the cutoff at all',
  },
  {
    field: 'retest_cohort_max_size',
    label: 'a cohort ceiling',
    // Without a tie band nothing can push the cohort past the rank cutoff, so
    // the ceiling never binds and asking for the ceiling itself is a no-op.
    legacyValue: (bounds) => bounds.max_retest_cohort_size,
    legacyBehaviour: (bounds) =>
      `the cohort can never exceed the rank cutoff, capped at ${bounds.max_retest_cohort_size}`,
  },
]

export type ContinualRetestFieldSupport = Record<string, boolean>

/** Which of the extended fields the platform behind this page actually carries. */
export function continualRetestFieldSupport(payload: unknown): ContinualRetestFieldSupport {
  let settings: Record<string, unknown> | null = null
  if (typeof payload === 'object' && payload !== null) {
    const effective = (payload as { effective?: unknown }).effective
    if (typeof effective === 'object' && effective !== null) {
      const candidate = (effective as { settings?: unknown }).settings
      if (typeof candidate === 'object' && candidate !== null) {
        settings = candidate as Record<string, unknown>
      }
    }
  }
  return Object.fromEntries(
    CONTINUAL_RETEST_EXTENDED_FIELDS.map((spec) => [
      spec.field,
      settings !== null && settings[spec.field] !== undefined,
    ]),
  )
}

/** Whether the platform this page is talking to accepts a retest cohort size. */
export function platformSupportsRetestCohortSize(payload: unknown): boolean {
  return continualRetestFieldSupport(payload).retest_cohort_size
}

/** Parse the platform payload and record what its contract actually carries. */
export function parseContinualRetestSettingsControl(payload: unknown) {
  const field_support = continualRetestFieldSupport(payload)
  return {
    ...continualRetestSettingsControlSchema.parse(payload),
    field_support,
    // Retained because it is part of the MCP read contract; derived rather than
    // probed separately so it cannot drift from the generic support map.
    cohort_sizing_supported: field_support.retest_cohort_size,
  }
}

/**
 * The settings block to put on the wire, given what the platform accepts.
 *
 * Dropping unsupported fields unconditionally would re-open the silent reset
 * the write schema exists to prevent, so an operator who actually asked for a
 * different policy is told the platform cannot do it yet rather than being
 * quietly answered with the default. A request that matches what the build
 * already does is not a change worth failing over: the field comes off and the
 * rest of the policy writes normally.
 */
export function continualRetestSettingsForPlatform(
  settings: ContinualRetestSettingsWrite,
  control: {
    field_support: ContinualRetestFieldSupport
    effective: { emission_set_size: number; max_retest_cohort_size?: number }
  },
) {
  const bounds = {
    emission_set_size: control.effective.emission_set_size,
    max_retest_cohort_size: control.effective.max_retest_cohort_size ?? MAX_RETEST_COHORT_SIZE,
  }
  const wire: Record<string, unknown> = { ...settings }
  const refusals: Array<string> = []

  for (const spec of CONTINUAL_RETEST_EXTENDED_FIELDS) {
    if (control.field_support[spec.field]) continue
    const requested = settings[spec.field]
    if (requested === spec.legacyValue(bounds)) {
      delete wire[spec.field]
      continue
    }
    refusals.push(
      `This platform build does not accept ${spec.label} yet, so ${spec.legacyBehaviour(bounds)}. Deploy a platform that carries \`${spec.field}\`, then ask for ${JSON.stringify(requested)} again.`,
    )
  }

  if (refusals.length > 0) throw new Error(refusals.join(' '))
  return wire
}

export const setContinualRetestSettingsInputSchema = z.object({
  scope: z.literal(CONTINUAL_RETEST_SETTINGS_SCOPE).default(CONTINUAL_RETEST_SETTINGS_SCOPE),
  expectedRevision: z.number().int().nonnegative(),
  settings: continualRetestSettingsWriteSchema,
  reason: auditReasonSchema(8),
  confirmation: z.literal(CONTINUAL_RETEST_CONFIRMATION),
})

export type ContinualRetestSettingsControl = ReturnType<
  typeof parseContinualRetestSettingsControl
>

// SN118 validator queue policy.
//
// Platform-owned, stored as an append-only revision and resolved when the
// scheduler hands out work, so cohort sizing, the validator lane cycle, and
// previous-generation carryover all change with no platform redeploy. Two
// different lifetimes live in one policy:
//
//   * `rescore_cohort_size` and `priority_cohort_size` are next-rollout
//     policy. The platform reads them once when a benchmark rollout starts and
//     freezes them onto the rollout row, so a change never resizes an
//     in-flight rollout.
//   * `lane_cycle_size` and `fresh_submission_slots` are live, but the
//     platform refuses them while a benchmark rollout is open. The lane
//     counter is "completed jobs since rollout start, mod N", so changing N
//     mid-rollout discontinuously reassigns validators between the fresh and
//     cohort lanes.
//
// The bounds below mirror the platform's own validation so operator input it
// would reject never reaches the admin API, but the platform stays the
// authority: its 409/422 detail text is surfaced verbatim.
export const QUEUE_POLICY_SETTINGS_SCOPE = '*'
export const QUEUE_POLICY_CONFIRMATION = 'APPLY QUEUE POLICY SETTINGS'

export const prevGenCarryoverDedupeScopeSchema = z.enum(['coldkey', 'hotkey', 'none'])

// Ships disabled. Turning it on admits previous-generation submissions that can
// never finalize on their own, because nobody will ever issue the third
// prior-version score once the new version activates.
export const PREV_GEN_CARRYOVER_DEFAULT = {
  enabled: false,
  max_agents: 10,
  min_score_count: 2,
  include_exhausted: false,
  dedupe_scope: 'coldkey',
  require_cohort_complete: true,
  require_desired_era_drained: true,
} as const

export const prevGenCarryoverSchema = z.object({
  enabled: z.boolean().default(PREV_GEN_CARRYOVER_DEFAULT.enabled),
  max_agents: z.number().int().min(1).max(50).default(PREV_GEN_CARRYOVER_DEFAULT.max_agents),
  // 2 admits only submissions that already hold two of three scores, so they
  // have demonstrated they can run. 0 also admits never-ticketed ones.
  min_score_count: z
    .number()
    .int()
    .min(0)
    .max(2)
    .default(PREV_GEN_CARRYOVER_DEFAULT.min_score_count),
  include_exhausted: z.boolean().default(PREV_GEN_CARRYOVER_DEFAULT.include_exhausted),
  // coldkey: a miner who has already submitted something newer under the same
  // coldkey does not get their older stranded submissions scored.
  dedupe_scope: prevGenCarryoverDedupeScopeSchema.default(
    PREV_GEN_CARRYOVER_DEFAULT.dedupe_scope,
  ),
  require_cohort_complete: z
    .boolean()
    .default(PREV_GEN_CARRYOVER_DEFAULT.require_cohort_complete),
  // Both previous-generation lanes wait for the desired era to have nothing
  // leasable. Note what that does NOT mean: "nothing leasable this instant" is
  // not "the new era is finished", because owner serialization and the
  // one-ticket-per-(agent, version, validator) rule empty the leasable set
  // while the queue behind it is still long.
  require_desired_era_drained: z
    .boolean()
    .default(PREV_GEN_CARRYOVER_DEFAULT.require_desired_era_drained),
  // `allow_retired_era_backfill` used to sit here. It is GONE, not defaulted
  // off, and it must not come back: benchmark versions below 7 are retired and
  // the platform now refuses them in the schema (CHECK constraints on the score
  // ledgers plus a `validator_tickets` lease trigger). See ditto-platform's
  // `MIN_SCOREABLE_BENCH_VERSION`.
  //
  // Removing it here is required, not cosmetic. The Platform ignores additive
  // fields for rolling compatibility, so retaining the retired key would look
  // accepted to an old client even though it can no longer affect policy.
})

// The write half of `prevGenCarryoverSchema`. The platform stores the carryover
// block whole and names every omitted key back (`prev_gen_carryover is stored
// whole too; missing [...]`), so a default that is convenient on the read path
// is a silent policy rewrite on the write path.
export const prevGenCarryoverWriteSchema = prevGenCarryoverSchema.extend({
  enabled: z.boolean(),
  max_agents: z.number().int().min(1).max(50),
  min_score_count: z.number().int().min(0).max(2),
  include_exhausted: z.boolean(),
  dedupe_scope: prevGenCarryoverDedupeScopeSchema,
  require_cohort_complete: z.boolean(),
  require_desired_era_drained: z.boolean(),
})

export const QUEUE_POLICY_DEFAULT_FRESH_SUBMISSION_SLOTS = [0, 1, 3]

// How many of one owner's submissions may hold live leases at once, on the
// allocator's last-resort pass only. Mirrors the platform's
// MIN/MAX/DEFAULT_OWNER_CONCURRENT_SUBMISSIONS exactly.
export const OWNER_CONCURRENT_SUBMISSION_MIN = 1
export const OWNER_CONCURRENT_SUBMISSION_MAX = 3
export const OWNER_CONCURRENT_SUBMISSION_DEFAULT = 2

export const SIMILARITY_CONCURRENT_SUBMISSION_MIN = 1
export const SIMILARITY_CONCURRENT_SUBMISSION_MAX = 3
export const SIMILARITY_CONCURRENT_SUBMISSION_DEFAULT = 1
export const SIMILARITY_THRESHOLD_MIN = 0.7
export const SIMILARITY_THRESHOLD_MAX = 1
export const SIMILARITY_BUDGET_DEFAULT = {
  enabled: true,
  concurrent_submission_limit: SIMILARITY_CONCURRENT_SUBMISSION_DEFAULT,
  jaccard_threshold: 0.9,
  containment_threshold: 0.95,
} as const

// Post-score source review is intentionally part of queue policy, not the
// screener worker's L2/L3 settings. The queue decides when a successfully built
// submission has enough score evidence to justify the expensive review. Older
// screeners extra-forbid unknown reviewer settings, while queue-policy reads
// already have an additive client-first default path.
export const DEFERRED_SOURCE_REVIEW_DEFAULT = {
  mode: 'off',
  min_cohort_size: 8,
  composite_mad_multiplier: 6,
  axis_mad_multiplier: 6,
  min_composite_delta: 0.1,
  min_axis_delta: 0.15,
} as const

// `bypass` is the no-source-review mode: build-only admission and no post-score
// qualification, so an admitted submission goes straight to validator scoring.
// It is the source-integrity branch only -- copy/plagiarism holds are opened by
// a separate path that never reads this policy. Note `off` is the HEAVIEST mode
// (full deep screen on every submission), not the off switch its name suggests.
export const deferredSourceReviewModeSchema = z.enum([
  'off',
  'observe',
  'enforce',
  'bypass',
])

const deferredSourceReviewSchema = z.object({
  mode: deferredSourceReviewModeSchema.default(DEFERRED_SOURCE_REVIEW_DEFAULT.mode),
  min_cohort_size: z.number().int().min(5).max(100).default(
    DEFERRED_SOURCE_REVIEW_DEFAULT.min_cohort_size,
  ),
  composite_mad_multiplier: z.number().min(1).max(20).default(
    DEFERRED_SOURCE_REVIEW_DEFAULT.composite_mad_multiplier,
  ),
  axis_mad_multiplier: z.number().min(1).max(20).default(
    DEFERRED_SOURCE_REVIEW_DEFAULT.axis_mad_multiplier,
  ),
  min_composite_delta: z.number().min(0).max(1).default(
    DEFERRED_SOURCE_REVIEW_DEFAULT.min_composite_delta,
  ),
  min_axis_delta: z.number().min(0).max(1).default(
    DEFERRED_SOURCE_REVIEW_DEFAULT.min_axis_delta,
  ),
})

const deferredSourceReviewWriteSchema = deferredSourceReviewSchema.extend({
  mode: deferredSourceReviewModeSchema,
  min_cohort_size: z.number().int().min(5).max(100),
  composite_mad_multiplier: z.number().min(1).max(20),
  axis_mad_multiplier: z.number().min(1).max(20),
  min_composite_delta: z.number().min(0).max(1),
  min_axis_delta: z.number().min(0).max(1),
})

const similarityBudgetSchema = z.object({
  enabled: z.boolean().default(true),
  concurrent_submission_limit: z
    .number()
    .int()
    .min(SIMILARITY_CONCURRENT_SUBMISSION_MIN)
    .max(SIMILARITY_CONCURRENT_SUBMISSION_MAX)
    .default(SIMILARITY_CONCURRENT_SUBMISSION_DEFAULT),
  jaccard_threshold: z
    .number()
    .min(SIMILARITY_THRESHOLD_MIN)
    .max(SIMILARITY_THRESHOLD_MAX)
    .default(0.9),
  containment_threshold: z
    .number()
    .min(SIMILARITY_THRESHOLD_MIN)
    .max(SIMILARITY_THRESHOLD_MAX)
    .default(0.95),
})

const similarityBudgetWriteSchema = similarityBudgetSchema.extend({
  enabled: z.boolean(),
  concurrent_submission_limit: z
    .number()
    .int()
    .min(SIMILARITY_CONCURRENT_SUBMISSION_MIN)
    .max(SIMILARITY_CONCURRENT_SUBMISSION_MAX),
  jaccard_threshold: z.number().min(SIMILARITY_THRESHOLD_MIN).max(SIMILARITY_THRESHOLD_MAX),
  containment_threshold: z
    .number()
    .min(SIMILARITY_THRESHOLD_MIN)
    .max(SIMILARITY_THRESHOLD_MAX),
})

const queuePolicySettingsBaseSchema = z.object({
  rescore_cohort_size: z.number().int().min(5).max(25).default(10),
  priority_cohort_size: z.number().int().min(5).max(25).default(5),
  lane_cycle_size: z.number().int().min(2).max(12).default(4),
  // Which positions in the lane cycle serve a fresh submission instead of a
  // rollout-cohort job. The default of three fresh slots in a four-job cycle
  // is three fresh-submission jobs per one cohort job per validator.
  fresh_submission_slots: z
    .array(z.number().int().nonnegative())
    .default([...QUEUE_POLICY_DEFAULT_FRESH_SUBMISSION_SLOTS]),
  // Declared because the platform HAS it. Omitting it is not neutral: the
  // platform's `_require_complete_policy` names every missing key, so while
  // this field was absent here `z.object` stripped it from every body and the
  // platform 422'd every write. That is the same failure `require_desired_era_
  // drained` had, one field over -- the board must describe exactly what the
  // platform accepts, no less and no more.
  owner_concurrent_submission_limit: z
    .number()
    .int()
    .min(OWNER_CONCURRENT_SUBMISSION_MIN)
    .max(OWNER_CONCURRENT_SUBMISSION_MAX)
    .default(OWNER_CONCURRENT_SUBMISSION_DEFAULT),
  // Platform #532 stores this block whole. Keep read defaults for additive
  // client-first deployment, but require every nested field on writes so an
  // older Backroom cannot silently reset a live threshold.
  similarity_budget: similarityBudgetSchema.default(SIMILARITY_BUDGET_DEFAULT),
  // Platform-owned post-score source-review policy. It is stored whole with
  // the rest of queue policy; read defaults preserve client-first deployment,
  // while the write schema below requires the complete nested block.
  deferred_source_review: deferredSourceReviewSchema.default(
    DEFERRED_SOURCE_REVIEW_DEFAULT,
  ),
  // `provisional_contender_lane_size` deliberately absent: the platform's
  // policy model does not have that field and forbids extras, so declaring it
  // here (with a default, no less) put a value in every body we sent that the
  // platform would refuse. The contender lane is not operator policy yet --
  // its size feeds three consumers, one of which disagrees with the allocator
  // about who a miner is -- and this board must describe what the platform
  // actually accepts, nothing more.
  prev_gen_carryover: prevGenCarryoverSchema.default(PREV_GEN_CARRYOVER_DEFAULT),
})

const refineQueuePolicyCoherence = (
  value: z.infer<typeof queuePolicySettingsBaseSchema>,
  context: z.RefinementCtx,
) => {
    if (value.priority_cohort_size > value.rescore_cohort_size) {
      context.addIssue({
        code: 'custom',
        message: 'priority_cohort_size must be at most rescore_cohort_size',
        path: ['priority_cohort_size'],
      })
    }
    const slots = value.fresh_submission_slots
    if (new Set(slots).size !== slots.length) {
      context.addIssue({
        code: 'custom',
        message: 'fresh_submission_slots must be unique lane positions',
        path: ['fresh_submission_slots'],
      })
    }
    if (slots.some((slot) => slot >= value.lane_cycle_size)) {
      context.addIssue({
        code: 'custom',
        message: `every fresh_submission_slots entry must be a lane position in [0, ${value.lane_cycle_size})`,
        path: ['fresh_submission_slots'],
      })
    }
    // The fresh lane can never be empty: that floor is what stops new miners
    // from being starved. It can never be the whole cycle either, or no
    // rollout cohort job is ever served.
    if (slots.length < 1) {
      context.addIssue({
        code: 'custom',
        message: 'fresh_submission_slots needs at least one slot so new miners are never starved',
        path: ['fresh_submission_slots'],
      })
    }
    if (slots.length > value.lane_cycle_size - 1) {
      context.addIssue({
        code: 'custom',
        message: `fresh_submission_slots must leave at least one cohort slot, so at most ${
          value.lane_cycle_size - 1
        } of ${value.lane_cycle_size}`,
        path: ['fresh_submission_slots'],
      })
    }
}

// The read half. Defaults keep this page rendering against a platform that
// predates a field, and let `parse({})` stand in for "no revision yet".
export const queuePolicySettingsSchema =
  queuePolicySettingsBaseSchema.superRefine(refineQueuePolicyCoherence)

// The write half. Every field is required, because a revision stores the WHOLE
// policy: an omitted key is not "leave it alone", it is a write of the shipped
// default. With defaults on the write path an MCP caller sending only
// `{lane_cycle_size: 6}` silently reset every other knob -- and
// `expectedRevision` cannot catch it, because they do hold the current
// revision, they just under-specified the body. Same reasoning as
// `continualRetestSettingsWriteSchema`.
export const queuePolicySettingsWriteSchema = queuePolicySettingsBaseSchema
  .extend({
    rescore_cohort_size: z.number().int().min(5).max(25),
    priority_cohort_size: z.number().int().min(5).max(25),
    lane_cycle_size: z.number().int().min(2).max(12),
    fresh_submission_slots: z.array(z.number().int().nonnegative()),
    owner_concurrent_submission_limit: z
      .number()
      .int()
      .min(OWNER_CONCURRENT_SUBMISSION_MIN)
      .max(OWNER_CONCURRENT_SUBMISSION_MAX),
    similarity_budget: similarityBudgetWriteSchema,
    deferred_source_review: deferredSourceReviewWriteSchema,
    prev_gen_carryover: prevGenCarryoverWriteSchema,
  })
  .superRefine(refineQueuePolicyCoherence)

export const queuePolicySettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: queuePolicySettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveQueuePolicySettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: queuePolicySettingsSchema,
  // Revision 0 is the shipped default. It was never written to a row, so it
  // carries no checksum.
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'default']),
  // What an open benchmark rollout froze at its start, which is what is
  // actually governing right now regardless of the settings above.
  open_rollout_desired_version: z.number().int().positive().nullable().default(null),
  open_rollout_rescore_cohort_target: z.number().int().nonnegative().nullable().default(null),
  open_rollout_priority_cohort_target: z.number().int().nonnegative().nullable().default(null),
  open_rollout_overrides_setting: z.boolean().default(false),
  // Fields the platform will refuse while that rollout stays open.
  rollout_locked_fields: z.array(z.string()).default([]),
  // Whether that lock is actually ACTIVE. `rollout_locked_fields` is a
  // constant -- always the same two names regardless of rollout state -- so it
  // can never answer "is a rollout open right now?". This boolean is the only
  // field that can, and it was being stripped, which is why the MCP tool text
  // and the 409 recovery copy both pointed operators at a list that never
  // changes.
  rollout_is_open: z.boolean().default(false),
  // The platform-advertised cohort bounds. Read them rather than trusting the
  // 5/25 hardcoded above, so a platform-side widening is visible here without
  // a backroom deploy.
  min_cohort_size: z.number().int().positive().default(5),
  max_cohort_size: z.number().int().positive().default(25),
})

export const queuePolicySettingsControlSchema = z.object({
  current: z.array(queuePolicySettingsRevisionSchema),
  history: z.array(queuePolicySettingsRevisionSchema),
  default: queuePolicySettingsSchema,
  effective: effectiveQueuePolicySettingsSchema,
})

export const setQueuePolicySettingsInputSchema = z.object({
  scope: z.literal(QUEUE_POLICY_SETTINGS_SCOPE).default(QUEUE_POLICY_SETTINGS_SCOPE),
  expectedRevision: z.number().int().nonnegative(),
  settings: queuePolicySettingsWriteSchema,
  reason: auditReasonSchema(8),
  confirmation: z.literal(QUEUE_POLICY_CONFIRMATION),
})

export type QueuePolicySettings = z.infer<typeof queuePolicySettingsSchema>
export type QueuePolicySettingsControl = z.infer<typeof queuePolicySettingsControlSchema>

// SN118 hosted inference admission policy.
//
// The two per-grant chat allowances plus both three-level hosted concurrency
// hierarchies, stored by ditto-platform as one append-only revision and
// refreshed into the admission path every five seconds. This is the board an operator
// reaches for *while watching* a benchmark run -- and it is the board that was
// unreachable from here until now: the platform shipped it in #477 and backroom
// had no schema, no service call, no MCP tool and no page, so the only way to
// move `chat_token_budget` was a curl with an admin bearer.
//
// That matters because `chat_token_budget` is the value that ended the runs
// #473 tried to save. Raising the request budget alone left the heaviest agents
// failing in the same place; the token budget was the binding allowance.
//
// Both budgets are stamped onto a grant when the grant is MINTED and read from
// the grant's own row thereafter, so a revision governs the next lease and can
// never retroactively exhaust one already in flight. Chat and embedding
// concurrency AND request-per-minute limits are enforced at admission instead,
// which makes either per-ticket value a live emergency brake: the platform
// answers a concurrency or RPM decline with the same 503 + Retry-After, so a
// validator holding a ticket backs off rather than discarding the run. RPM
// used to be boot-time only; 8-wide tickets then died as inference_lane_saturated
// on the 240/min cap while concurrency peaks looked idle.
//
// Every bound below mirrors ditto-platform's MAX_* constants, which are the same
// constants its boot-time `check_config` enforces -- deliberately identical, so
// this board can never accept a number the next platform restart would refuse.
export const INFERENCE_CONCURRENCY_SCOPE = '*'
export const INFERENCE_CONCURRENCY_CONFIRMATION = 'APPLY INFERENCE CONCURRENCY SETTINGS'
export const RUNTIME_PROFILE_CONFIRMATION = 'CAPTURE RUNTIME PROFILE'

const inferenceRequestKindSchema = z.enum(['chat', 'embedding'])
const runtimeProfileTargetSchema = z.enum(['platform-relay-1', 'platform-relay-2'])
const runtimeProfileTypeSchema = z.enum(['cpu', 'heap', 'allocs', 'goroutine'])

export const inferenceRuntimeMetricsSchema = z.object({
  observed_at: z.string(),
  settings_revision: z.number().int().nonnegative(),
  settings_checksum: z.string(),
  lanes: z.array(
    z.object({
      request_kind: inferenceRequestKindSchema,
      active_requests: z.number().int().nonnegative(),
      live_grants: z.number().int().nonnegative(),
      stale_started_requests: z.number().int().nonnegative(),
      per_ticket_limit: z.number().int().positive(),
      per_validator_limit: z.number().int().positive(),
      global_limit: z.number().int().positive(),
      per_ticket_rpm_limit: z.number().int().positive(),
      per_validator_rpm_limit: z.number().int().positive(),
      global_rpm_limit: z.number().int().positive(),
      peak_per_ticket_concurrency_60m: z.number().int().nonnegative(),
      peak_per_validator_concurrency_60m: z.number().int().nonnegative(),
      peak_global_concurrency_60m: z.number().int().nonnegative(),
    }),
  ),
  windows: z.array(
    z.object({
      window_seconds: z.number().int().positive(),
      request_kind: inferenceRequestKindSchema,
      calls: z.number().int().nonnegative(),
      calls_per_second: z.number().nonnegative(),
      tokens: z.number().int().nonnegative(),
      tokens_per_second: z.number().nonnegative(),
      completed: z.number().int().nonnegative(),
      failed: z.number().int().nonnegative(),
      canceled: z.number().int().nonnegative(),
      timed_out: z.number().int().nonnegative(),
      latency_p50_ms: z.number().int().nonnegative().nullable(),
      latency_p95_ms: z.number().int().nonnegative().nullable(),
      latency_max_ms: z.number().int().nonnegative().nullable(),
      peak_global_concurrency: z.number().int().nonnegative(),
    }),
  ),
  relays: z.array(
    z.object({
      target: runtimeProfileTargetSchema,
      status: z.enum(['ok', 'unavailable']),
      source_revision: z.string().nullable().optional(),
      checked_out_revision: z.string().nullable().optional(),
      revision_drift: z.boolean().nullable().optional(),
      process_started_at: z.string().nullable().optional(),
      capacity_declines: z.record(z.string(), z.number().int().nonnegative()).default({}),
      error: z.string().nullable().optional(),
    }),
  ),
})

export const runtimeProfileCaptureInputSchema = z
  .object({
    target: runtimeProfileTargetSchema,
    profileType: runtimeProfileTypeSchema,
    seconds: z.number().int().min(5).max(30).optional(),
    reason: z.string().trim().min(8),
    confirmation: z.literal(RUNTIME_PROFILE_CONFIRMATION),
  })
  .superRefine((value, context) => {
    if (value.profileType === 'cpu' && value.seconds === undefined) {
      context.addIssue({
        code: 'custom',
        path: ['seconds'],
        message: 'seconds is required for a CPU profile',
      })
    }
    if (value.profileType !== 'cpu' && value.seconds !== undefined) {
      context.addIssue({
        code: 'custom',
        path: ['seconds'],
        message: 'seconds is only valid for a CPU profile',
      })
    }
  })

export const runtimeProfileLookupInputSchema = z.object({
  profileId: z.string().uuid(),
})

export const runtimeProfileArtifactSchema = z.object({
  profile_id: z.string().uuid(),
  target: runtimeProfileTargetSchema,
  profile_type: runtimeProfileTypeSchema,
  seconds: z.number().int().min(5).max(30).nullable(),
  source_revision: z.string().length(40),
  checked_out_revision: z.string().length(40),
  revision_drift: z.boolean(),
  actor: z.string(),
  reason: z.string(),
  created_at: z.string(),
  expires_at: z.string(),
  byte_size: z.number().int().nonnegative(),
  sha256: z.string().regex(/^[0-9a-f]{64}$/),
  media_type: z.string(),
  filename: z.string(),
  download_path: z.string(),
})

export const runtimeProfileDownloadSchema = z.object({
  profile: runtimeProfileArtifactSchema,
  encoding: z.literal('base64'),
  data_base64: z.string(),
})

export const MAX_CHAT_REQUEST_BUDGET = 32768
export const MAX_CHAT_TOKEN_BUDGET = 200_000_000
export const MAX_CHAT_CONCURRENCY = 512
export const MAX_EMBEDDING_CONCURRENCY = 512
export const MAX_REQUESTS_PER_MINUTE = 100_000
export const MAX_BENCHMARK_CASE_CONCURRENCY = 64
export const MAX_RELAY_DELAY_FINGERPRINT_MS = 5_000

export const benchmarkRuntimeSettingsSchema = z
  .object({
    case_concurrency: z.number().int().min(1).max(MAX_BENCHMARK_CASE_CONCURRENCY),
    relay_delay_fingerprint_mode: z.enum(['off', 'shadow']),
    relay_delay_fingerprint_min_ms: z.number().int().min(0).max(MAX_RELAY_DELAY_FINGERPRINT_MS),
    relay_delay_fingerprint_max_ms: z.number().int().min(0).max(MAX_RELAY_DELAY_FINGERPRINT_MS),
  })
  .refine(
    (value) => value.relay_delay_fingerprint_min_ms <= value.relay_delay_fingerprint_max_ms,
    {
      message: 'Relay delay minimum may not exceed the maximum',
      path: ['relay_delay_fingerprint_min_ms'],
    },
  )

export const DEFAULT_BENCHMARK_RUNTIME_SETTINGS = {
  case_concurrency: 4,
  relay_delay_fingerprint_mode: 'off' as const,
  relay_delay_fingerprint_min_ms: 25,
  relay_delay_fingerprint_max_ms: 250,
}

const inferenceConcurrencySettingsBaseSchema = z.object({
  // Chat completions one scoring ticket's grant may spend in total. Ships at
  // 16384, ~15x the heaviest observed run.
  chat_request_budget: z.number().int().min(1).max(MAX_CHAT_REQUEST_BUDGET),
  // Chat tokens (prompt + completion) one grant may spend. Ships at 25,000,000,
  // ~7x the heaviest observed run. This is the number to move when a legitimate
  // strategy stuffs large contexts. It is a CAP, not a spend: raising it changes
  // only which runs are permitted to finish, never what an agent is charged.
  chat_token_budget: z.number().int().min(1).max(MAX_CHAT_TOKEN_BUDGET),
  chat_per_ticket_concurrency: z.number().int().min(1).max(MAX_CHAT_CONCURRENCY),
  chat_per_validator_concurrency: z.number().int().min(1).max(MAX_CHAT_CONCURRENCY),
  chat_global_concurrency: z.number().int().min(1).max(MAX_CHAT_CONCURRENCY),
  // Concurrent hosted embedding requests one ticket's grant may hold. The
  // emergency brake: lowering it takes effect fleet-wide on the next admission,
  // with no release and no restart.
  embedding_per_ticket_concurrency: z.number().int().min(1).max(MAX_EMBEDDING_CONCURRENCY),
  embedding_per_validator_concurrency: z.number().int().min(1).max(MAX_EMBEDDING_CONCURRENCY),
  // Enforced by a cross-grant aggregate, so it is best-effort under a
  // simultaneous burst: concurrent admissions can overshoot it by at most the
  // number of racers. Size it as a load-shedding backstop, not an exact valve.
  embedding_global_concurrency: z.number().int().min(1).max(MAX_EMBEDDING_CONCURRENCY),
  chat_per_ticket_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  chat_per_validator_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  chat_global_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  embedding_per_ticket_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  embedding_per_validator_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  embedding_global_requests_per_minute: z.number().int().min(1).max(MAX_REQUESTS_PER_MINUTE),
  // Additive rolling-upgrade field. A Platform predating the v10 controls
  // omits it, which fills the shipped default (4 overlapping /run, delay off).
  benchmark_runtime: benchmarkRuntimeSettingsSchema
    .nullish()
    .transform((value) => value ?? DEFAULT_BENCHMARK_RUNTIME_SETTINGS),
})

// The platform enforces this hierarchy with its own model validator and 422s a
// violation. Mirrored here so the operator sees which field is wrong before a
// round trip.
const refineInferenceConcurrencyHierarchy = (
  value: z.infer<typeof inferenceConcurrencySettingsBaseSchema>,
  context: z.RefinementCtx,
) => {
  if (value.chat_per_ticket_concurrency > value.chat_per_validator_concurrency) {
    context.addIssue({
      code: 'custom',
      message:
        'chat_per_ticket_concurrency may not exceed chat_per_validator_concurrency',
      path: ['chat_per_ticket_concurrency'],
    })
  }
  if (value.chat_per_validator_concurrency > value.chat_global_concurrency) {
    context.addIssue({
      code: 'custom',
      message: 'chat_per_validator_concurrency may not exceed chat_global_concurrency',
      path: ['chat_per_validator_concurrency'],
    })
  }
  if (value.embedding_per_ticket_concurrency > value.embedding_per_validator_concurrency) {
    context.addIssue({
      code: 'custom',
      message:
        'embedding_per_ticket_concurrency may not exceed embedding_per_validator_concurrency: a ticket cannot be allowed more concurrency than the validator hosting it',
      path: ['embedding_per_ticket_concurrency'],
    })
  }
  if (value.embedding_per_validator_concurrency > value.embedding_global_concurrency) {
    context.addIssue({
      code: 'custom',
      message:
        'embedding_per_validator_concurrency may not exceed embedding_global_concurrency: a single validator cannot be allowed more concurrency than the fleet',
      path: ['embedding_per_validator_concurrency'],
    })
  }
  if (value.chat_per_ticket_requests_per_minute > value.chat_per_validator_requests_per_minute) {
    context.addIssue({
      code: 'custom',
      message:
        'chat_per_ticket_requests_per_minute may not exceed chat_per_validator_requests_per_minute',
      path: ['chat_per_ticket_requests_per_minute'],
    })
  }
  if (value.chat_per_validator_requests_per_minute > value.chat_global_requests_per_minute) {
    context.addIssue({
      code: 'custom',
      message:
        'chat_per_validator_requests_per_minute may not exceed chat_global_requests_per_minute',
      path: ['chat_per_validator_requests_per_minute'],
    })
  }
  if (
    value.embedding_per_ticket_requests_per_minute >
    value.embedding_per_validator_requests_per_minute
  ) {
    context.addIssue({
      code: 'custom',
      message:
        'embedding_per_ticket_requests_per_minute may not exceed embedding_per_validator_requests_per_minute',
      path: ['embedding_per_ticket_requests_per_minute'],
    })
  }
  if (
    value.embedding_per_validator_requests_per_minute >
    value.embedding_global_requests_per_minute
  ) {
    context.addIssue({
      code: 'custom',
      message:
        'embedding_per_validator_requests_per_minute may not exceed embedding_global_requests_per_minute',
      path: ['embedding_per_validator_requests_per_minute'],
    })
  }
}

export const inferenceConcurrencySettingsSchema =
  inferenceConcurrencySettingsBaseSchema.superRefine(refineInferenceConcurrencyHierarchy)

const inferenceConcurrencySettingsWriteSchema = inferenceConcurrencySettingsBaseSchema
  .extend({ benchmark_runtime: benchmarkRuntimeSettingsSchema })
  .superRefine(refineInferenceConcurrencyHierarchy)

export const inferenceConcurrencySettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: inferenceConcurrencySettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveInferenceConcurrencySettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: inferenceConcurrencySettingsSchema,
  // Revision 0 is the shipped default. It was never written to a row, so it
  // carries no checksum.
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'default']),
})

export const inferenceConcurrencySettingsControlSchema = z.object({
  current: z.array(inferenceConcurrencySettingsRevisionSchema),
  history: z.array(inferenceConcurrencySettingsRevisionSchema),
  default: inferenceConcurrencySettingsSchema,
  effective: effectiveInferenceConcurrencySettingsSchema,
})

// No `.default()` anywhere on the write path, for the same reason validator-slot
// has none: the platform's `_require_complete_policy` stores the whole object
// and names every omitted key, so a default here would send a value the operator
// never chose.
export const setInferenceConcurrencySettingsInputSchema = z.object({
  scope: z.literal(INFERENCE_CONCURRENCY_SCOPE).default(INFERENCE_CONCURRENCY_SCOPE),
  expectedRevision: z.number().int().nonnegative(),
  settings: inferenceConcurrencySettingsWriteSchema,
  reason: auditReasonSchema(8),
  confirmation: z.literal(INFERENCE_CONCURRENCY_CONFIRMATION),
})

export type InferenceConcurrencySettings = z.infer<typeof inferenceConcurrencySettingsSchema>
export type InferenceConcurrencySettingsControl = z.infer<
  typeof inferenceConcurrencySettingsControlSchema
>

// Bench v9 confirmation bundles are a bounded, append-only control plane. The
// Platform owns issuance and ranking; Backroom may only append a complete
// settings revision, inspect signed evidence, or authorize one explicit retest.
// Keep these response schemas strict: accepting an unmodelled evidence field in
// an operator console makes that field effectively invisible during rollout.
export const CONFIRMATION_BUNDLE_SCOPE = '*'
export const CONFIRMATION_BUNDLE_RETEST_CONFIRMATION =
  'AUTHORIZE CONFIRMATION BUNDLE RETEST'
export const confirmationBundleModeSchema = z.enum(['off', 'shadow', 'enforce'])
export const confirmationBundleStateSchema = z.enum([
  'blocked_budget',
  'pending',
  'leased',
  'failed',
  'completed',
  'superseded',
])

const confirmationSha256Schema = z.string().regex(/^[0-9a-f]{64}$/)
// Empty string is the canonical unused/zero-lane receipt digest from Go, shared
// Python, and Platform. A 64-hex digest is required on any positive lane.
const confirmationReceiptSetDigestSchema = z.string().regex(/^(?:|[0-9a-f]{64})$/)
const confirmationUsageCountSchema = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER)
const confirmationPositiveUsageCountSchema = confirmationUsageCountSchema.min(1)
const confirmationScoreMicrosSchema = z.number().int().min(0).max(1_000_000)
const confirmationFactorSchema = z.union([z.literal(0), z.literal(10_000)])
const confirmationTimestampSchema = z.string().datetime({ offset: true })

export const confirmationBundleSettingsSchema = z
  .strictObject({
    mode: confirmationBundleModeSchema,
    eligibility_mode: z.enum(['rank', 'score_threshold']),
    top_n: z.number().int().min(1).max(10),
    min_base_score_micros: confirmationScoreMicrosSchema,
    daily_bundle_cap: z.number().int().min(0).max(1_000),
    daily_dollar_cap_microusd: z.number().int().min(0).max(2_000_000_000),
    per_bundle_request_cap: z.number().int().min(0).max(100_000),
    per_bundle_token_cap: z.number().int().min(0).max(100_000_000),
    profile_revision: z.string().min(1).max(128).nullable(),
    profile_checksum: confirmationSha256Schema.nullable(),
    challenger_z: z.number().min(0).max(3),
  })
  .superRefine((settings, context) => {
    if ((settings.profile_revision === null) !== (settings.profile_checksum === null)) {
      context.addIssue({
        code: 'custom',
        message: 'profile_revision and profile_checksum must be configured together',
        path: ['profile_revision'],
      })
    }
    if (settings.mode === 'off') return
    for (const key of [
      'daily_bundle_cap',
      'daily_dollar_cap_microusd',
      'per_bundle_request_cap',
      'per_bundle_token_cap',
    ] as const) {
      if (settings[key] === 0) {
        context.addIssue({
          code: 'custom',
          message: `${key} must be positive in ${settings.mode} mode`,
          path: [key],
        })
      }
    }
    if (settings.profile_revision === null) {
      context.addIssue({
        code: 'custom',
        message: 'an immutable confirmation profile is required in active modes',
        path: ['profile_revision'],
      })
    }
  })

export function confirmationBundleSettingsConfirmation(
  mode: z.infer<typeof confirmationBundleModeSchema>,
) {
  return `APPLY V9 CONFIRMATION MODE ${mode.toUpperCase()}`
}

export const confirmationBundleSettingsRevisionSchema = z.strictObject({
  revision: z.number().int().positive(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.literal(CONFIRMATION_BUNDLE_SCOPE),
  settings: confirmationBundleSettingsSchema,
  checksum: confirmationSha256Schema,
  reason: z.string().min(1),
  actor: z.string().min(1),
  created_at: confirmationTimestampSchema,
})

export const effectiveConfirmationBundleSettingsSchema = z.strictObject({
  revision: z.number().int().nonnegative(),
  scope: z.literal(CONFIRMATION_BUNDLE_SCOPE),
  settings: confirmationBundleSettingsSchema,
  checksum: confirmationSha256Schema.nullable(),
  source: z.enum(['default', 'revision']),
  configured: z.boolean(),
  issuance_active: z.boolean(),
  max_top_n: z.literal(10),
  max_daily_bundle_cap: z.literal(1_000),
  max_daily_dollar_microusd: z.literal(2_000_000_000),
  max_bundle_request_cap: z.literal(100_000),
  max_bundle_token_cap: z.literal(100_000_000),
})

export const confirmationBundleSettingsControlSchema = z
  .strictObject({
    current: z.array(confirmationBundleSettingsRevisionSchema),
    history: z.array(confirmationBundleSettingsRevisionSchema),
    default: confirmationBundleSettingsSchema,
    effective: effectiveConfirmationBundleSettingsSchema,
  })
  .superRefine((control, context) => {
    if (control.default.mode !== 'off') {
      context.addIssue({
        code: 'custom',
        message: 'the shipped confirmation default must remain off',
        path: ['default', 'mode'],
      })
    }
    const expectedConfigured =
      control.effective.settings.profile_revision !== null &&
      control.effective.settings.profile_checksum !== null
    if (control.effective.configured !== expectedConfigured) {
      context.addIssue({
        code: 'custom',
        message: 'configured contradicts the immutable profile identity',
        path: ['effective', 'configured'],
      })
    }
    const expectedActive =
      control.effective.settings.mode !== 'off' && expectedConfigured
    if (control.effective.issuance_active !== expectedActive) {
      context.addIssue({
        code: 'custom',
        message: 'issuance_active contradicts the effective mode and profile',
        path: ['effective', 'issuance_active'],
      })
    }
  })

export const setConfirmationBundleSettingsInputSchema = z
  .strictObject({
    scope: z.literal(CONFIRMATION_BUNDLE_SCOPE),
    expectedRevision: z.number().int().nonnegative(),
    settings: confirmationBundleSettingsSchema,
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = confirmationBundleSettingsConfirmation(input.settings.mode)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })

const longMemCapabilitySchema = z.enum([
  'extraction',
  'multi_session_reasoning',
  'temporal_reasoning',
  'knowledge_update',
  'preference',
  'abstention',
])
const longMemCapabilityOrder = longMemCapabilitySchema.options

const longMemCapabilityScoreSchema = z
  .strictObject({
    capability: longMemCapabilitySchema,
    correct: confirmationUsageCountSchema,
    count: confirmationPositiveUsageCountSchema,
    mean_micros: confirmationScoreMicrosSchema,
  })
  .superRefine((score, context) => {
    if (score.correct > score.count) {
      context.addIssue({ code: 'custom', message: 'correct cannot exceed count' })
    }
    if (score.mean_micros !== Math.round((score.correct / score.count) * 1_000_000)) {
      context.addIssue({
        code: 'custom',
        message: 'mean_micros must be derived from correct and count',
        path: ['mean_micros'],
      })
    }
  })

const longMemProviderLaneSchema = z
  .strictObject({
    lane: z.enum(['reader', 'judge']),
    cost_source: z.literal('provider_receipt_v1'),
    currency: z.literal('USD'),
    provider: z.string().min(1).max(128),
    profile_revision: z.string().min(1).max(128),
    model: z.string().min(1).max(256),
    fallback_used: z.literal(false),
    requests: confirmationUsageCountSchema,
    successes: confirmationUsageCountSchema,
    receipted_requests: confirmationUsageCountSchema,
    prompt_tokens: confirmationUsageCountSchema,
    completion_tokens: confirmationUsageCountSchema,
    total_tokens: confirmationUsageCountSchema,
    cost_usd_micros: confirmationUsageCountSchema,
    receipt_set_sha256: confirmationReceiptSetDigestSchema,
  })
  .superRefine((lane, context) => {
    if (lane.requests === 0) {
      if (
        lane.successes !== 0 ||
        lane.receipted_requests !== 0 ||
        lane.prompt_tokens !== 0 ||
        lane.completion_tokens !== 0 ||
        lane.total_tokens !== 0 ||
        lane.cost_usd_micros !== 0 ||
        lane.receipt_set_sha256 !== ''
      ) {
        context.addIssue({
          code: 'custom',
          message: 'zero provider lane must have exact zero accounting',
        })
      }
      return
    }
    if (lane.successes === 0 || lane.receipted_requests === 0) {
      context.addIssue({
        code: 'custom',
        message: 'positive provider lane must have successful receipted requests',
      })
    }
    if (lane.receipt_set_sha256.length !== 64) {
      context.addIssue({
        code: 'custom',
        message: 'positive provider lane must have a receipt digest',
        path: ['receipt_set_sha256'],
      })
    }
    if (lane.successes > lane.requests) {
      context.addIssue({ code: 'custom', message: 'successes cannot exceed requests' })
    }
    if (lane.receipted_requests !== lane.requests) {
      context.addIssue({ code: 'custom', message: 'every provider request must be receipted' })
    }
    if (lane.total_tokens !== lane.prompt_tokens + lane.completion_tokens) {
      context.addIssue({
        code: 'custom',
        message: 'total_tokens must equal prompt_tokens plus completion_tokens',
      })
    }
  })

const longMemEvidenceSchema = z
  .strictObject({
    schema_version: z.literal(2),
    artifact_sha256: confirmationSha256Schema,
    bench_version: confirmationBenchVersionSchema,
    profile_checksum: confirmationSha256Schema,
    case_set_digest: confirmationSha256Schema,
    dataset_revision: z.string().min(1).max(128),
    dataset_sha256: confirmationSha256Schema,
    score: z.strictObject({
      longmem_mean_micros: confirmationScoreMicrosSchema,
      longmem_stderr_micros: confirmationScoreMicrosSchema,
      case_count: confirmationPositiveUsageCountSchema,
      per_capability: z.array(longMemCapabilityScoreSchema).length(6),
    }),
    provider_evidence: z.array(longMemProviderLaneSchema).length(2),
  })
  .superRefine((evidence, context) => {
    const capabilities = evidence.score.per_capability.map((row) => row.capability)
    if (capabilities.some((capability, index) => capability !== longMemCapabilityOrder[index])) {
      context.addIssue({
        code: 'custom',
        message: 'per_capability must contain the six capabilities in canonical order',
        path: ['score', 'per_capability'],
      })
    }
    if (
      evidence.score.per_capability.reduce((total, row) => total + row.count, 0) !==
      evidence.score.case_count
    ) {
      context.addIssue({
        code: 'custom',
        message: 'case_count must equal the capability counts',
        path: ['score', 'case_count'],
      })
    }
    const lanes = evidence.provider_evidence
    if (lanes[0]?.lane !== 'judge' || lanes[1]?.lane !== 'reader') {
      context.addIssue({
        code: 'custom',
        message: 'provider_evidence must contain judge then reader in canonical order',
        path: ['provider_evidence'],
      })
    }
    const zeroLanes = lanes.filter((lane) => lane.requests === 0)
    if (zeroLanes.length === 0) return
    const exactZeroScore =
      evidence.score.longmem_mean_micros === 0 &&
      evidence.score.longmem_stderr_micros === 0 &&
      evidence.score.per_capability.every((row) => row.correct === 0 && row.mean_micros === 0)
    if (!exactZeroScore) {
      context.addIssue({
        code: 'custom',
        message: 'zero-provider LongMem evidence requires an exact zero score',
        path: ['score'],
      })
      return
    }
    if (zeroLanes.length === lanes.length) return
    const judge = lanes.find((lane) => lane.lane === 'judge')
    const reader = lanes.find((lane) => lane.lane === 'reader')
    if (
      reader?.requests !== 0 ||
      judge === undefined ||
      judge.requests !== evidence.score.case_count ||
      judge.successes !== evidence.score.case_count ||
      judge.receipted_requests !== evidence.score.case_count
    ) {
      context.addIssue({
        code: 'custom',
        message:
          'mixed LongMem provider evidence requires an unused reader, one receipted judge request per case, and an exact zero score',
        path: ['provider_evidence'],
      })
    }
  })

const ablationBudgetSchema = z.strictObject({
  max_chat_requests: confirmationUsageCountSchema,
  max_chat_input_bytes: confirmationUsageCountSchema,
  max_embedding_requests: confirmationUsageCountSchema,
  max_embedding_inputs: confirmationUsageCountSchema,
  max_embedding_input_bytes: confirmationUsageCountSchema,
})

const ablationSyntheticUsageSchema = z
  .strictObject({
    synthetic: z.literal(true),
    intervention: z.enum(['inference', 'embedding']),
    budget: ablationBudgetSchema,
    chat_attempts: confirmationUsageCountSchema,
    chat_applied: confirmationUsageCountSchema,
    chat_input_bytes: confirmationUsageCountSchema,
    embedding_attempts: confirmationUsageCountSchema,
    embedding_applied: confirmationUsageCountSchema,
    embedding_inputs: confirmationUsageCountSchema,
    embedding_input_bytes: confirmationUsageCountSchema,
    rejected_requests: confirmationUsageCountSchema,
    budget_exhausted: z.boolean(),
    upstream_requests: z.literal(0),
    upstream_input_tokens: z.literal(0),
    upstream_output_tokens: z.literal(0),
    upstream_provider_cost_microusd: z.literal(0),
  })
  .superRefine((usage, context) => {
    if (usage.chat_applied > usage.chat_attempts) {
      context.addIssue({ code: 'custom', message: 'chat_applied cannot exceed chat_attempts' })
    }
    if (usage.embedding_applied > usage.embedding_attempts) {
      context.addIssue({ code: 'custom', message: 'embedding_applied cannot exceed embedding_attempts' })
    }
    if ((usage.rejected_requests > 0) !== usage.budget_exhausted) {
      context.addIssue({
        code: 'custom',
        message: 'budget_exhausted must agree with rejected_requests',
      })
    }
    if (usage.intervention === 'inference') {
      if (
        usage.embedding_attempts !== 0 ||
        usage.embedding_applied !== 0 ||
        usage.embedding_inputs !== 0 ||
        usage.embedding_input_bytes !== 0 ||
        usage.chat_attempts !== usage.chat_applied + usage.rejected_requests ||
        usage.chat_applied > usage.budget.max_chat_requests ||
        usage.chat_input_bytes > usage.budget.max_chat_input_bytes
      ) {
        context.addIssue({ code: 'custom', message: 'invalid inference synthetic accounting' })
      }
    } else if (
      usage.chat_attempts !== 0 ||
      usage.chat_applied !== 0 ||
      usage.chat_input_bytes !== 0 ||
      usage.embedding_attempts !== usage.embedding_applied + usage.rejected_requests ||
      usage.embedding_applied > usage.budget.max_embedding_requests ||
      usage.embedding_inputs > usage.budget.max_embedding_inputs ||
      usage.embedding_input_bytes > usage.budget.max_embedding_input_bytes
    ) {
      context.addIssue({ code: 'custom', message: 'invalid embedding synthetic accounting' })
    }
  })

const ablationEvidenceSchema = z
  .strictObject({
    contract_version: z.string().min(1).max(128),
    bench_version: confirmationBenchVersionSchema,
    artifact_sha256: confirmationSha256Schema,
    intervention: z.enum(['inference', 'embedding']),
    mode: z.enum(['off', 'shadow', 'enforce']),
    status: z.enum(['not_run', 'passed', 'failed', 'unavailable']),
    reason: z.string().min(1),
    profile_revision: z.string().min(1).max(128),
    profile_checksum: confirmationSha256Schema,
    threshold_manifest_sha256: confirmationSha256Schema,
    coordinator_sha256: confirmationSha256Schema,
    dataset_sha256: confirmationSha256Schema,
    case_set_sha256: confirmationSha256Schema,
    baseline_scores_sha256: confirmationSha256Schema.nullable(),
    ablated_scores_sha256: confirmationSha256Schema.nullable(),
    baseline_mean_micros: confirmationScoreMicrosSchema.nullable(),
    ablated_mean_micros: confirmationScoreMicrosSchema.nullable(),
    delta_micros: z.number().int().min(-1_000_000).max(1_000_000).nullable(),
    threshold_micros: confirmationScoreMicrosSchema,
    sample_count: confirmationUsageCountSchema,
    affected_call_count: confirmationUsageCountSchema,
    semantic_factor_bps: confirmationFactorSchema.nullable(),
    applied_factor_bps: confirmationFactorSchema.nullable(),
    synthetic_usage: ablationSyntheticUsageSchema,
  })
  .superRefine((evidence, context) => {
    if (evidence.intervention !== evidence.synthetic_usage.intervention) {
      context.addIssue({ code: 'custom', message: 'synthetic intervention mismatch' })
    }
    const numeric = [
      evidence.baseline_scores_sha256,
      evidence.ablated_scores_sha256,
      evidence.baseline_mean_micros,
      evidence.ablated_mean_micros,
      evidence.delta_micros,
      evidence.semantic_factor_bps,
      evidence.applied_factor_bps,
    ]
    if (evidence.status === 'passed' || evidence.status === 'failed') {
      if (numeric.some((value) => value === null) || evidence.sample_count === 0) {
        context.addIssue({ code: 'custom', message: 'completed ablation evidence is incomplete' })
        return
      }
      const delta = evidence.baseline_mean_micros! - evidence.ablated_mean_micros!
      const dropMeetsThreshold = delta >= evidence.threshold_micros
      // Observational drop is a completed failure whose delta meets the gate
      // but is not causal. Semantic follows status (0), not the naive pass.
      if (evidence.reason === 'observational_drop_not_causal') {
        if (
          evidence.status !== 'failed' ||
          (evidence.mode !== 'shadow' && evidence.mode !== 'enforce') ||
          !dropMeetsThreshold
        ) {
          context.addIssue({ code: 'custom', message: 'ablation result fields contradict one another' })
          return
        }
      } else if ((evidence.status === 'passed') !== dropMeetsThreshold) {
        context.addIssue({ code: 'custom', message: 'ablation result fields contradict one another' })
        return
      }
      const semantic = evidence.status === 'passed' ? 10_000 : 0
      const applied = evidence.mode === 'shadow' ? 10_000 : semantic
      if (
        evidence.delta_micros !== delta ||
        evidence.semantic_factor_bps !== semantic ||
        evidence.applied_factor_bps !== applied
      ) {
        context.addIssue({ code: 'custom', message: 'ablation result fields contradict one another' })
      }
    } else if (numeric.some((value) => value !== null)) {
      context.addIssue({
        code: 'custom',
        message: 'not-run or unavailable ablation cannot carry a numeric gate',
      })
    }
    const appliedCalls =
      evidence.intervention === 'inference'
        ? evidence.synthetic_usage.chat_applied
        : evidence.synthetic_usage.embedding_applied
    if (evidence.affected_call_count !== appliedCalls) {
      context.addIssue({ code: 'custom', message: 'ablation affected-call count is not derived' })
    }
    if (
      evidence.status !== 'unavailable' &&
      evidence.status !== 'not_run' &&
      evidence.synthetic_usage.budget_exhausted
    ) {
      context.addIssue({ code: 'custom', message: 'budget-exhausted ablation cannot complete' })
    }
    if (
      evidence.reason === 'counterfactual_proof_unavailable' &&
      (evidence.status !== 'unavailable' || evidence.mode === 'off')
    ) {
      context.addIssue({
        code: 'custom',
        message: 'counterfactual proof reason requires an active unavailable result',
      })
    }
  })

const longMemDimensionEnvelopeSchema = z.strictObject({
  status: z.literal('completed'),
  evidence_sha256: confirmationSha256Schema,
  latency_ms: confirmationUsageCountSchema,
  request_count: confirmationUsageCountSchema,
  input_tokens: confirmationUsageCountSchema,
  output_tokens: confirmationUsageCountSchema,
  provider_cost_microusd: confirmationUsageCountSchema,
  synthetic: z.literal(false),
  evidence: longMemEvidenceSchema,
}).superRefine((envelope, context) => {
  const totals = envelope.evidence.provider_evidence.reduce(
    (sum, lane) => ({
      requests: sum.requests + lane.requests,
      input: sum.input + lane.prompt_tokens,
      output: sum.output + lane.completion_tokens,
      cost: sum.cost + lane.cost_usd_micros,
    }),
    { requests: 0, input: 0, output: 0, cost: 0 },
  )
  if (
    envelope.request_count !== totals.requests ||
    envelope.input_tokens !== totals.input ||
    envelope.output_tokens !== totals.output ||
    envelope.provider_cost_microusd !== totals.cost
  ) {
    context.addIssue({
      code: 'custom',
      message: 'LongMem envelope usage must equal its provider receipt lanes',
    })
  }
})

const ablationDimensionEnvelopeSchema = z
  .strictObject({
    status: z.enum(['completed', 'not_run', 'unavailable']),
    evidence_sha256: confirmationSha256Schema,
    latency_ms: confirmationUsageCountSchema,
    request_count: z.literal(0),
    input_tokens: z.literal(0),
    output_tokens: z.literal(0),
    provider_cost_microusd: z.literal(0),
    synthetic: z.literal(true),
    evidence: ablationEvidenceSchema,
  })
  .superRefine((envelope, context) => {
    const expected =
      envelope.evidence.status === 'passed' || envelope.evidence.status === 'failed'
        ? 'completed'
        : envelope.evidence.status
    if (envelope.status !== expected) {
      context.addIssue({ code: 'custom', message: 'envelope status contradicts evidence status' })
    }
  })

const confirmationUsageTotalsSchema = z.strictObject({
  request_count: confirmationUsageCountSchema,
  input_tokens: confirmationUsageCountSchema,
  output_tokens: confirmationUsageCountSchema,
  provider_cost_microusd: confirmationUsageCountSchema,
  latency_ms: confirmationUsageCountSchema,
})

const confirmationCompositePolicySchema = z
  .strictObject({
    schema_version: z.literal(1),
    revision: z.string().min(1).max(128),
    formula_revision: z.literal('weighted-quality-gates-v1'),
    base_weight_bps: z.number().int().positive().max(9_999),
    longmem_weight_bps: z.number().int().positive().max(9_999),
    checksum: confirmationSha256Schema,
  })
  .superRefine((policy, context) => {
    if (policy.base_weight_bps + policy.longmem_weight_bps !== 10_000) {
      context.addIssue({ code: 'custom', message: 'composite weights must sum to 10000' })
    }
  })

const confirmationEvidenceRootSchema = z
  .strictObject({
    schema_version: z.literal(1),
    artifact_sha256: confirmationSha256Schema,
    bench_version: confirmationBenchVersionSchema,
    confirmation_profile_revision: z.string().min(1).max(128),
    confirmation_profile_checksum: confirmationSha256Schema,
    settings_revision: z.number().int().positive(),
    settings_checksum: confirmationSha256Schema,
    retest_generation: z.number().int().nonnegative(),
    ablation_coordinator_latency_ms: confirmationUsageCountSchema,
    composite_policy: confirmationCompositePolicySchema,
    longmemeval: longMemDimensionEnvelopeSchema,
    inference_ablation: ablationDimensionEnvelopeSchema,
    embedding_ablation: ablationDimensionEnvelopeSchema,
    totals: confirmationUsageTotalsSchema,
  })
  .superRefine((root, context) => {
    if (
      root.totals.request_count !== root.longmemeval.request_count ||
      root.totals.input_tokens !== root.longmemeval.input_tokens ||
      root.totals.output_tokens !== root.longmemeval.output_tokens ||
      root.totals.provider_cost_microusd !== root.longmemeval.provider_cost_microusd ||
      root.totals.latency_ms !==
        root.longmemeval.latency_ms + root.ablation_coordinator_latency_ms
    ) {
      context.addIssue({ code: 'custom', message: 'root totals do not match trusted dimensions' })
    }
    if (
      root.inference_ablation.evidence.coordinator_sha256 !==
      root.embedding_ablation.evidence.coordinator_sha256
    ) {
      context.addIssue({ code: 'custom', message: 'ablations must share one coordinator digest' })
    }
  })

const confirmationDimensionEvidenceSchema = z
  .strictObject({
    dimension: z.enum(['longmemeval', 'inference_ablation', 'embedding_ablation']),
    status: z.enum(['completed', 'not_run', 'unavailable']),
    evidence_sha256: confirmationSha256Schema,
    request_count: confirmationUsageCountSchema,
    input_tokens: confirmationUsageCountSchema,
    output_tokens: confirmationUsageCountSchema,
    provider_cost_microusd: confirmationUsageCountSchema,
    latency_ms: confirmationUsageCountSchema,
    synthetic: z.boolean(),
    evidence: z.union([longMemEvidenceSchema, ablationEvidenceSchema]),
    created_at: confirmationTimestampSchema,
  })
  .superRefine((dimension, context) => {
    if (dimension.dimension === 'longmemeval') {
      // Shape, not version: `'score' in evidence` is what distinguishes LongMem
      // evidence from ablation evidence. The old `bench_version !== 9` arm
      // restated a version pin the member schemas already own, and rejected
      // every valid v10+ LongMem dimension.
      if (dimension.synthetic || !('score' in dimension.evidence)) {
        context.addIssue({ code: 'custom', message: 'longmemeval requires non-synthetic LongMem evidence' })
      }
      return
    }
    const intervention = dimension.dimension === 'inference_ablation' ? 'inference' : 'embedding'
    if (!dimension.synthetic || !('intervention' in dimension.evidence) || dimension.evidence.intervention !== intervention) {
      context.addIssue({ code: 'custom', message: `${dimension.dimension} evidence mismatch` })
    }
  })

const confirmationBundleSubjectSchema = z.strictObject({
  agent_id: z.string().uuid(),
  // Generated contract says `number` -- this view reads the bundle's stored
  // epoch, not the evidence union. Pinning it to 9 was stricter than the
  // contract and rejected every carried-forward bundle.
  bench_version: z.number().int().positive(),
  artifact_sha256: confirmationSha256Schema,
  result_status: z.enum(['base_only', 'provisional', 'full_confirmed']),
  base_evidence_sha256: confirmationSha256Schema,
  base_quality_micros: confirmationScoreMicrosSchema,
  base_stderr_micros: confirmationScoreMicrosSchema,
  base_model_factor_bps: confirmationFactorSchema,
  base_tool_factor_bps: confirmationFactorSchema,
  full_quality_micros: confirmationScoreMicrosSchema.nullable(),
  full_stderr_micros: confirmationScoreMicrosSchema.nullable(),
  semantic_factor_bps: confirmationFactorSchema.nullable(),
  applied_factor_bps: confirmationFactorSchema.nullable(),
  full_effective_micros: confirmationScoreMicrosSchema.nullable(),
  bundle_id: z.string().uuid().nullable(),
  created_at: confirmationTimestampSchema,
  updated_at: confirmationTimestampSchema,
})

const confirmationBundleTicketSchema = z.strictObject({
  ticket_id: z.string().uuid(),
  validator_hotkey: z.string().min(1),
  slot_id: z.string().min(1),
  status: z.enum(['issued', 'scored', 'expired']),
  attempt: z.number().int().positive(),
  issued_at: confirmationTimestampSchema,
  deadline: confirmationTimestampSchema,
  failure_reason: z.string().nullable(),
  // The signed, allowlisted diagnostics behind ``failure_reason``. Optional on
  // the wire: a reporter predating the contract sends neither, so an older
  // ticket in the same list must still parse.
  failure_class: z.string().min(1).nullable().default(null),
  failure_stage: z.string().min(1).nullable().default(null),
  failed_at: confirmationTimestampSchema.nullable(),
  // Allowlisted Go→Python prepare-report 409. Null when prepare never ran or
  // succeeded; a consumer must not require it of historical tickets.
  prepare_rejection: z
    .enum([
      'go_evidence_digest_mismatch',
      'go_evidence_fields_drifted',
      'unsupported_ablation_status',
      'unsupported_ablation_contract',
      'ablation_profile_drift',
      'ablation_accounting',
      'ablation_digest_mismatch',
      'longmem_profile_drift',
      'longmem_accounting',
      'longmem_digest_mismatch',
      'longmem_latency_drift',
      'unsupported_bench_version',
      'confirmation_wire',
      'confirmation_evidence',
      'unclassified',
    ])
    .nullable()
    .default(null),
  prepare_rejected_at: confirmationTimestampSchema.nullable().default(null),
})

export const confirmationBundleViewSchema = z
  .strictObject({
    bundle_id: z.string().uuid(),
    artifact_sha256: confirmationSha256Schema,
    // Generated contract says `number`; see confirmationBundleSubjectSchema.
    bench_version: z.number().int().positive(),
    profile_revision: z.string().min(1).max(128),
    profile_checksum: confirmationSha256Schema,
    retest_generation: z.number().int().nonnegative(),
    generation_reason: z.enum(['initial', 'operator_retest', 'settings_supersession']),
    source_bundle_id: z.string().uuid().nullable(),
    state: confirmationBundleStateSchema,
    settings_revision: z.number().int().positive(),
    settings_checksum: confirmationSha256Schema,
    qualification_status: z.enum(['qualified', 'unqualified']).nullable(),
    completion_mode: z.enum(['shadow', 'enforce']).nullable(),
    completion_ticket_id: z.string().uuid().nullable(),
    evidence_sha256: confirmationSha256Schema.nullable(),
    reporter_hotkey: z.string().min(1).nullable(),
    bundle_signature: z.string().regex(/^[0-9a-f]{2,512}$/).nullable(),
    evidence_root: confirmationEvidenceRootSchema.nullable(),
    verified_at: confirmationTimestampSchema.nullable(),
    completed_at: confirmationTimestampSchema.nullable(),
    created_at: confirmationTimestampSchema,
    updated_at: confirmationTimestampSchema,
    subjects: z.array(confirmationBundleSubjectSchema),
    dimensions: z.array(confirmationDimensionEvidenceSchema),
    tickets: z.array(confirmationBundleTicketSchema),
  } satisfies PlatformResponseShape<GeneratedConfirmationBundleView>)
  .superRefine((bundle, context) => {
    const initialGeneration = bundle.generation_reason === 'initial'
    const validGenerationLineage = initialGeneration
      ? bundle.retest_generation === 0 && bundle.source_bundle_id === null
      : bundle.retest_generation > 0 && bundle.source_bundle_id !== null
    if (!validGenerationLineage) {
      context.addIssue({ code: 'custom', message: 'bundle generation lineage is inconsistent' })
    }
    const completion = [
      bundle.qualification_status,
      bundle.completion_mode,
      bundle.completion_ticket_id,
      bundle.evidence_sha256,
      bundle.reporter_hotkey,
      bundle.bundle_signature,
      bundle.evidence_root,
      bundle.verified_at,
      bundle.completed_at,
    ]
    const hasCompletedEvidence = completion.every((value) => value !== null)
    const hasNoCompletedEvidence = completion.every((value) => value === null)
    if (!hasCompletedEvidence && !hasNoCompletedEvidence) {
      context.addIssue({ code: 'custom', message: 'bundle completion fields are inconsistent' })
    }
    if (bundle.state === 'completed' && !hasCompletedEvidence) {
      context.addIssue({ code: 'custom', message: 'completed bundle requires completion evidence' })
    }
    if (
      bundle.state !== 'completed' &&
      bundle.state !== 'superseded' &&
      !hasNoCompletedEvidence
    ) {
      context.addIssue({ code: 'custom', message: 'unfinished bundle cannot publish completion evidence' })
    }
    if (!hasCompletedEvidence && bundle.dimensions.length > 0) {
      context.addIssue({ code: 'custom', message: 'bundle without completion evidence cannot publish dimensions' })
    }
    if (bundle.evidence_root) {
      const root = bundle.evidence_root
      if (
        root.artifact_sha256 !== bundle.artifact_sha256 ||
        root.confirmation_profile_revision !== bundle.profile_revision ||
        root.confirmation_profile_checksum !== bundle.profile_checksum ||
        root.retest_generation !== bundle.retest_generation ||
        root.settings_revision !== bundle.settings_revision ||
        root.settings_checksum !== bundle.settings_checksum
      ) {
        context.addIssue({ code: 'custom', message: 'evidence root does not bind this bundle' })
      }
      if (
        bundle.completion_mode === null ||
        root.inference_ablation.evidence.mode !== bundle.completion_mode ||
        root.embedding_ablation.evidence.mode !== bundle.completion_mode
      ) {
        context.addIssue({
          code: 'custom',
          message: 'completion_mode does not match the signed ablation evidence',
        })
      }
    }
    if (
      bundle.completion_ticket_id !== null &&
      !bundle.tickets.some(
        (ticket) =>
          ticket.ticket_id === bundle.completion_ticket_id && ticket.status === 'scored',
      )
    ) {
      context.addIssue({
        code: 'custom',
        message: 'completion_ticket_id must identify a scored ticket in this bundle',
      })
    }
    if (
      bundle.completion_mode === 'shadow' &&
      bundle.subjects.some((subject) => subject.result_status === 'full_confirmed')
    ) {
      context.addIssue({
        code: 'custom',
        message: 'shadow completion cannot make a subject fully confirmed',
      })
    }
    if (new Set(bundle.dimensions.map((row) => row.dimension)).size !== bundle.dimensions.length) {
      context.addIssue({ code: 'custom', message: 'dimension rows must be unique' })
    }
    if (
      hasCompletedEvidence &&
      bundle.dimensions.map((row) => row.dimension).sort().join(',') !==
        'embedding_ablation,inference_ablation,longmemeval'
    ) {
      context.addIssue({
        code: 'custom',
        message: 'completed bundles must publish all three dimension rows',
      })
    }
  })

export const confirmationShadowCalibrationSchema = z
  .strictObject({
    observed_from_utc_day: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
    observed_through_utc_day: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
    observation_days: z.number().int().nonnegative(),
    confirmation_profile_revision: z.string().min(1).max(128).nullable(),
    confirmation_profile_checksum: confirmationSha256Schema.nullable(),
    base_run_count: z.number().int().nonnegative(),
    measured_base_cost_microusd: z.number().int().nonnegative().nullable(),
    confirmation_bundle_count: z.number().int().nonnegative(),
    measured_bundle_cost_microusd: z.number().int().nonnegative().nullable(),
    bench_version: z.number().int().positive().default(9),
    // completed = produced verified evidence. Superseded/failed generations are
    // separate axes so an execution outage is not read as an unpromoted cohort.
    completed_bundle_count: z.number().int().nonnegative(),
    superseded_bundle_count: z.number().int().nonnegative().default(0),
    failed_bundle_count: z.number().int().nonnegative().default(0),
    qualified_bundle_count: z.number().int().nonnegative(),
    promotion_rate_bps: z.number().int().min(0).max(10_000).nullable(),
    projected_daily_spend_microusd: z.number().int().nonnegative().nullable(),
    epoch_duration_seconds: z.number().int().positive().nullable(),
    projected_epoch_spend_microusd: z.number().int().nonnegative().nullable(),
    epoch_projection_unavailable_reason: z.string().min(1).nullable(),
  })
  .superRefine((calibration, context) => {
    if (
      (calibration.confirmation_profile_revision === null) !==
      (calibration.confirmation_profile_checksum === null)
    ) {
      context.addIssue({ code: 'custom', message: 'confirmation profile identity must be complete' })
    }
    if (calibration.qualified_bundle_count > calibration.completed_bundle_count) {
      context.addIssue({ code: 'custom', message: 'qualified count cannot exceed completed count' })
    }
    if ((calibration.base_run_count === 0) !== (calibration.measured_base_cost_microusd === null)) {
      context.addIssue({ code: 'custom', message: 'base cost availability must match its sample count' })
    }
    if (
      (calibration.confirmation_bundle_count === 0) !==
      (calibration.measured_bundle_cost_microusd === null)
    ) {
      context.addIssue({ code: 'custom', message: 'bundle cost availability must match its sample count' })
    }
    if ((calibration.completed_bundle_count === 0) !== (calibration.promotion_rate_bps === null)) {
      context.addIssue({ code: 'custom', message: 'promotion rate availability must match its sample count' })
    }
    const dailyAvailable = calibration.observation_days > 0
    if (
      dailyAvailable !== (calibration.observed_from_utc_day !== null) ||
      dailyAvailable !== (calibration.observed_through_utc_day !== null) ||
      dailyAvailable !== (calibration.projected_daily_spend_microusd !== null)
    ) {
      context.addIssue({ code: 'custom', message: 'daily projection requires a complete observation window' })
    } else if (dailyAvailable) {
      const from = Date.parse(`${calibration.observed_from_utc_day}T00:00:00Z`)
      const through = Date.parse(`${calibration.observed_through_utc_day}T00:00:00Z`)
      const inclusiveDays = Math.round((through - from) / 86_400_000) + 1
      if (calibration.observation_days !== inclusiveDays) {
        context.addIssue({ code: 'custom', message: 'observation days must match the UTC date window' })
      }
    }
    const epochAvailable = calibration.epoch_duration_seconds !== null
    if (
      epochAvailable !== (calibration.projected_epoch_spend_microusd !== null) ||
      epochAvailable === (calibration.epoch_projection_unavailable_reason !== null)
    ) {
      context.addIssue({ code: 'custom', message: 'epoch projection must expose exactly one availability state' })
    }
  })

export const confirmationBundleListSchema = z
  .strictObject({
    items: z.array(confirmationBundleViewSchema),
    count: z.number().int().nonnegative(),
    budget: z.strictObject({
      utc_day: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
      revision: z.number().int().nonnegative(),
      issued_attempts: z.number().int().nonnegative(),
      outstanding_reserved_microusd: z.number().int().nonnegative(),
      settled_microusd: z.number().int().nonnegative(),
    }),
    shadow_calibration: confirmationShadowCalibrationSchema,
  } satisfies PlatformResponseShape<GeneratedConfirmationBundleList>)
  .superRefine((response, context) => {
    if (response.count < response.items.length) {
      context.addIssue({ code: 'custom', message: 'count cannot be smaller than returned bundle rows' })
    }
  })

export const confirmationBundleListInputSchema = z.strictObject({
  state: confirmationBundleStateSchema.optional(),
  limit: z.number().int().min(1).max(200).default(100),
  offset: z.number().int().min(0).default(0),
})

export const confirmationBundleDetailInputSchema = z.strictObject({
  bundleId: z.string().uuid(),
})

export const authorizeConfirmationBundleRetestInputSchema = z.strictObject({
  bundleId: z.string().uuid(),
  requestId: z.string().uuid(),
  expectedGeneration: z.number().int().nonnegative(),
  reason: auditReasonSchema(8),
  confirmation: z.literal(CONFIRMATION_BUNDLE_RETEST_CONFIRMATION),
})

export const confirmationBundleRetestResponseSchema = z.strictObject({
  authorization_id: z.string().uuid(),
  superseded_bundle_id: z.string().uuid(),
  bundle: confirmationBundleViewSchema,
  replayed: z.boolean(),
})

export type ConfirmationBundleSettings = z.infer<typeof confirmationBundleSettingsSchema>
export type ConfirmationBundleSettingsControl = z.infer<
  typeof confirmationBundleSettingsControlSchema
>
export type ConfirmationBundleView = z.infer<typeof confirmationBundleViewSchema>
export type ConfirmationBundleList = z.infer<typeof confirmationBundleListSchema>

// SN118 validator slot policy.
//
// How many concurrent benchmark slots the platform will issue live tickets for
// on any ONE validator, plus the disk circuit breaker that narrows a nearly-full
// host to a single slot. Platform-owned, stored as an append-only revision and
// resolved on the dispatch path behind a short TTL, so the cap is both the kill
// switch (drop to 1 and multi-slot dispatch stops at the next ticket issue) and
// the ramp control (2 -> 3 -> 4 as confidence grows) with no platform redeploy.
//
// Sibling of the queue policy above: same append-only revision store, same
// optimistic-concurrency guard, same audited actor. It answers a different
// question, though — the queue policy decides WHICH job a validator is handed,
// this decides HOW MANY it may hold at once.
//
// Deliberately NOT the product feature-flag system fronted by
// `update_feature_flag` / `set_feature_flag_override`: those are boolean,
// per-user/company/domain product entitlements served by `backend` from a
// different database.
//
// The bounds below mirror ditto-platform's `ValidatorSlotSettings` exactly, so
// operator input the platform would reject never reaches the admin API, but the
// platform stays the authority: its 409/422 detail text is surfaced verbatim.
export const VALIDATOR_SLOT_SETTINGS_SCOPE = '*'

// The protocol's own maximum advertised slots (`^slot-[0-7]$`). A schema bound,
// not a policy knob: the operator cap can narrow the fleet but can never widen
// it past what a validator is able to advertise.
export const VALIDATOR_HARD_SLOT_CEILING = 8

// `SystemMetrics.disk_percent` is reported on a 5% grid, so a ceiling off that
// grid would fire at the next grid point up and silently misdescribe itself (87
// behaves exactly like 90). The platform rejects it; reject it here too.
export const DISK_PERCENT_QUANTUM = 5

// `cpu_percent` and `memory_percent` ride the identical grid, so every ceiling in
// this policy is held to it.
export const CEILING_DISABLED = 0
export const MIN_ENABLED_CEILING = 50
const SS58_HOTKEY_PATTERN = /^[1-9A-HJ-NP-Za-km-z]{47,48}$/

// A ceiling is either CEILING_DISABLED ("do not gate on this resource at all") or
// a multiple of 5 in [MIN_ENABLED_CEILING, 100]. Below 50 a ceiling throttles a
// healthy host rather than protecting a failing one.
function refineCeiling(value: number, key: string, context: z.RefinementCtx) {
  if (value === CEILING_DISABLED) return
  if (value % DISK_PERCENT_QUANTUM) {
    context.addIssue({
      code: 'custom',
      message: `${key} must be a multiple of ${DISK_PERCENT_QUANTUM} because heartbeat host metrics are reported on that grid`,
      path: [key],
    })
  }
  if (value < MIN_ENABLED_CEILING) {
    context.addIssue({
      code: 'custom',
      message: `${key} must be ${CEILING_DISABLED} (disabled) or at least ${MIN_ENABLED_CEILING}`,
      path: [key],
    })
  }
}

// Every knob is REQUIRED. A revision stores the complete policy and never a
// diff, so a field omitted from a write is not inherited from the current
// revision — it would resolve to the shipped default. The platform correctly
// 422s a partial body; defaulting here would pre-fill it into a full body before
// it ever got there, and the operator would silently ship a default they never
// chose. That is the empty-default failure class, so there are no `.default()`
// calls in this object.
export const validatorSlotSettingsSchema = z
  .object({
    max_concurrent_slots: z.number().int().min(1).max(VALIDATOR_HARD_SLOT_CEILING),
    // Tier one, the throttle: cross a per-resource ceiling and the validator is
    // held to `disk_restricted_slots` concurrent leases.
    disk_percent_ceiling: z.number().int().min(CEILING_DISABLED).max(100),
    memory_percent_ceiling: z.number().int().min(CEILING_DISABLED).max(100),
    cpu_percent_ceiling: z.number().int().min(CEILING_DISABLED).max(100),
    // Tier two, the refusal: cross this on any ENABLED resource and the validator
    // is issued no tickets at all until a later heartbeat says it recovered. A
    // resource whose own ceiling is CEILING_DISABLED is exempt here too, which is
    // what keeps a pinned CPU — the ordinary state of a working benchmark host —
    // from blocking anything by default.
    resource_block_percent_ceiling: z.number().int().min(CEILING_DISABLED).max(100),
    // Exact-validator issuance brakes. Required on every whole-policy write so
    // an older or partial client cannot resume a paused validator while
    // changing an unrelated capacity knob.
    paused_validator_hotkeys: z.array(z.string().regex(SS58_HOTKEY_PATTERN)).max(256),
  })
  .superRefine((value, context) => {
    refineCeiling(value.disk_percent_ceiling, 'disk_percent_ceiling', context)
    refineCeiling(value.memory_percent_ceiling, 'memory_percent_ceiling', context)
    refineCeiling(value.cpu_percent_ceiling, 'cpu_percent_ceiling', context)
    refineCeiling(
      value.resource_block_percent_ceiling,
      'resource_block_percent_ceiling',
      context,
    )
    const highestThrottle = Math.max(
      value.disk_percent_ceiling,
      value.memory_percent_ceiling,
      value.cpu_percent_ceiling,
    )
    if (
      value.resource_block_percent_ceiling !== CEILING_DISABLED &&
      value.resource_block_percent_ceiling < highestThrottle
    ) {
      context.addIssue({
        code: 'custom',
        message: `resource_block_percent_ceiling must be at or above every enabled per-resource ceiling (${highestThrottle}); a hard stop below the throttle makes the throttle unreachable`,
        path: ['resource_block_percent_ceiling'],
      })
    }
    if (
      value.paused_validator_hotkeys.some(
        (hotkey, index) =>
          index > 0 && hotkey <= value.paused_validator_hotkeys[index - 1],
      )
    ) {
      context.addIssue({
        code: 'custom',
        message: 'paused_validator_hotkeys must be sorted and duplicate-free',
        path: ['paused_validator_hotkeys'],
      })
    }
  })

export const validatorSlotSettingsRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: validatorSlotSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
})

export const effectiveValidatorSlotSettingsSchema = z.object({
  revision: z.number().int().nonnegative(),
  scope: z.string(),
  settings: validatorSlotSettingsSchema,
  // Revision 0 is the module-level default. It was never written to a row, so
  // it carries no checksum.
  checksum: z.string().regex(/^(?:[0-9a-f]{64})?$/),
  source: z.enum(['revision', 'default']),
  hard_slot_ceiling: z.number().int().positive(),
  // How many slots a validator is held to once ANY per-resource ceiling is
  // tripped. Named for disk because disk was the only breaker when it landed.
  disk_restricted_slots: z.number().int().nonnegative(),
  // Upper bound in seconds on how long a Backroom change takes to reach the
  // dispatch path. 0 means every read re-reads.
  max_age_seconds: z.number().nonnegative(),
})

export const validatorSlotSettingsControlSchema = z.object({
  current: z.array(validatorSlotSettingsRevisionSchema),
  history: z.array(validatorSlotSettingsRevisionSchema),
  default: validatorSlotSettingsSchema,
  effective: effectiveValidatorSlotSettingsSchema,
})

// The confirmation names the RESULTING cap, so the number is stated twice in one
// request and a fat-fingered ramp cannot land silently. Backroom must never
// build this string from the caller's own number — that would collapse the two
// statements back into one and confirm nothing. The caller supplies both halves
// independently and this only checks that they agree.
export function validatorSlotConfirmation(maxConcurrentSlots: number) {
  return `APPLY VALIDATOR SLOT CAP ${maxConcurrentSlots}`
}

export function validatorIssuanceConfirmation(validatorHotkey: string, paused: boolean) {
  return `${paused ? 'PAUSE' : 'RESUME'} VALIDATOR ${validatorHotkey}`
}

export const setValidatorIssuancePauseInputSchema = z
  .object({
    validatorHotkey: z.string().regex(SS58_HOTKEY_PATTERN),
    paused: z.boolean(),
    expectedRevision: z.number().int().nonnegative(),
    reason: auditReasonSchema(8),
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = validatorIssuanceConfirmation(input.validatorHotkey, input.paused)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}`,
        path: ['confirmation'],
      })
    }
  })

export const setValidatorSlotSettingsInputSchema = z
  .object({
    scope: z
      .literal(VALIDATOR_SLOT_SETTINGS_SCOPE)
      .default(VALIDATOR_SLOT_SETTINGS_SCOPE),
    expectedRevision: z.number().int().nonnegative(),
    settings: validatorSlotSettingsSchema,
    reason: auditReasonSchema(8),
    // Not a literal: the expected text depends on the cap being applied, and the
    // caller has to type the number themselves for the double statement to mean
    // anything.
    confirmation: z.string(),
  })
  .superRefine((input, context) => {
    const expected = validatorSlotConfirmation(input.settings.max_concurrent_slots)
    if (input.confirmation !== expected) {
      context.addIssue({
        code: 'custom',
        message: `confirmation must be exactly ${expected}, naming the cap this revision applies`,
        path: ['confirmation'],
      })
    }
  })

export type ValidatorSlotSettings = z.infer<typeof validatorSlotSettingsSchema>
export type ValidatorSlotSettingsControl = z.infer<
  typeof validatorSlotSettingsControlSchema
>
export type ValidatorSlotSettingsRevision = z.infer<
  typeof validatorSlotSettingsRevisionSchema
>
export type SetValidatorIssuancePauseInput = z.infer<
  typeof setValidatorIssuancePauseInputSchema
>

// What the operator screen needs from the fleet to choose a cap, read from the
// platform's existing public validator heartbeat view. It is decoration, not
// policy: the cap is a subnet-global number and the platform resolves it without
// consulting any of this. But a cap is only meaningful against the capacity the
// fleet advertises and the disk headroom it reports, so the numbers belong next
// to the control rather than in a separate console.
//
// Deliberately lenient. Heartbeats arrive from validators running several
// protocol versions and the block is advisory, so an unreadable field degrades
// to an honest blank instead of failing the page that carries the kill switch.

// A slot the platform released out from under a still-executing benchmark. An
// operator eviction ends the platform's half of a lease at once; the
// validator's container runs to completion and has its late score refused with
// a 409. For that window the host is doing a full benchmark's worth of work
// that cannot produce a score, and before this signal existed every such slot
// rendered as Idle — which is how a fleet with no headroom reads as a fleet
// with plenty. A slot listed here is NOT free: treat it as occupied when
// reasoning about fleet headroom, even in the `indeterminate` state.
export const publicOrphanedSlotSchema = z.object({
  agent_id: z.string(),
  agent_name: z.string().nullish().default(null),
  bench_version: z.number().int().positive(),
  evicted_at: z.string(),
  original_deadline: z.string().nullish().default(null),
  orphaned_for_seconds: z.number().nonnegative(),
  protocol_version: z.number().nullish().default(null),
  reason: z.string(),
  slot_id: z.string(),
  state: z.enum(['still_running', 'indeterminate']),
})

export const validatorUpdaterStatusSchema = z
  .object({
    candidate_descriptor: z.string().nullable().optional(),
    candidate_version: z.string().nullable().optional(),
    channel: z.literal('compat-2').nullable().optional(),
    current_descriptor: z.string().nullable().optional(),
    current_version: z.string().nullable().optional(),
    enabled: z.boolean(),
    failed_candidate_count: z.number().int().min(0).max(100),
    last_failure_at: z.number().int().nonnegative().nullable().optional(),
    last_failure_reason: z
      .enum([
        'candidate_deploy_failed',
        'candidate_readiness_failed',
        'transaction_interrupted',
        'unknown',
      ])
      .nullable()
      .optional(),
    last_success_at: z.number().int().nonnegative().nullable().optional(),
    self_refresh_installed: z.boolean().nullable().optional(),
    self_refresh_revision: z.string().nullable().optional(),
    self_refresh_last_success_at: z.number().int().nonnegative().nullable().optional(),
    observed_at: z.number().int().nonnegative(),
    retry_after: z.number().int().nonnegative().nullable().optional(),
    state: z.enum([
      'not_managed',
      'disabled',
      'unavailable',
      'idle',
      'prefetched',
      'draining',
      'replacing',
      'verifying',
      'rollback',
      'backoff',
      'retry_ready',
      'suppressed',
    ]),
    suppressed: z.boolean(),
    transaction_phase: z
      .enum([
        'prepared',
        'drained',
        'old_stopped',
        'candidate_started',
        'committed',
        'rollback_pending',
        'rollback_ready',
      ])
      .nullable()
      .optional(),
  } satisfies PlatformResponseShape<GeneratedValidatorUpdaterStatus>)
  .nullable()
  .optional()
  .catch(null)

export const validatorFleetMemberSchema = z
  .object({
    validator_hotkey: z.string(),
    // Slots the validator itself advertises. The cap narrows this; it can never
    // widen it, so a validator advertising 1 stays at 1 however high the cap goes.
    configured_slots: z.number().int().nonnegative().catch(1),
    healthy_slots: z.array(z.string()).catch([]),
    admission: z.string().catch('accepting'),
    // Leases the validator is running right now, which is what the cap gates at
    // the next ticket issue. Only the count crosses into the console.
    active_benchmarks: z.array(z.unknown()).catch([]),
    online: z.boolean().catch(false),
    system_metrics: z
      .object({ disk_percent: z.number().int().min(0).max(100) })
      .nullish()
      .catch(null),
    // Whether this validator can serve the active benchmark version, and if
    // not, why. This is the gate the platform itself applies before leasing
    // work, so anything other than `serving` means the validator is issued
    // nothing and cannot earn a score however healthy its host metrics read.
    // `catch('serving')` because an older platform that predates the field does
    // not do this gate — every validator is effectively serving — and a
    // malformed value on an advisory page must never blank the cap control.
    bench_serviceability: z
      .enum(['serving', 'scorer_unverified', 'software_obsolete'])
      .catch('serving'),
    // Slots whose lease an operator evicted while the validator's benchmark
    // container may still be executing. Empty in the ordinary case; a slot here
    // is NOT free capacity.
    orphaned_slots: z.array(publicOrphanedSlotSchema).catch([]),
    updater_status: validatorUpdaterStatusSchema,
  })
  .transform((member) => ({
    validator_hotkey: member.validator_hotkey,
    configured_slots: member.configured_slots,
    healthy_slot_count: member.healthy_slots.length,
    admission: member.admission,
    active_benchmark_count: member.active_benchmarks.length,
    online: member.online,
    // Null means this validator did not report host metrics, which is not the
    // same as reporting a healthy disk. The console renders the difference.
    disk_percent: member.system_metrics?.disk_percent ?? null,
    bench_serviceability: member.bench_serviceability,
    orphaned_slots: member.orphaned_slots,
    updater_status: member.updater_status ?? null,
  }))

export const validatorFleetSchema = z.object({
  generated_at: z.string(),
  // The benchmark version the platform is scoring now — the era every lease is
  // issued against. Nullish for a platform that predates the field; the console
  // omits the era label rather than guessing one.
  active_bench_version: z.number().int().positive().nullish().default(null),
  validators: z.array(validatorFleetMemberSchema).catch([]),
})

export type ValidatorFleet = z.infer<typeof validatorFleetSchema>
export type ValidatorFleetMember = z.infer<typeof validatorFleetMemberSchema>

// MCP fleet observability keeps identity the slot-cap console deliberately
// drops: software_version, protocol, stack component revisions, scorer probe
// identity, and updater current/candidate. Calibration manifests and per-check
// progress stay out of the parse so one heartbeat cannot flood the catalog.
const validatorComponentIdentitySchema = z
  .object({
    image_digest: z.string().nullish().catch(null),
    source_revision: z.string().nullish().catch(null),
    version: z.string().nullish().catch(null),
    provenance: z.string().catch('unknown'),
  })
  .nullish()
  .catch(null)

export const validatorFleetObservabilityMemberSchema = z
  .object({
    validator_hotkey: z.string(),
    software_version: z.string().catch('unreported'),
    protocol_version: z.number().int().positive().catch(0),
    configured_slots: z.number().int().nonnegative().catch(1),
    allowed_slots: z.number().int().nonnegative().nullish().catch(null),
    issuance_paused: z.boolean().catch(false),
    healthy_slots: z.array(z.string()).catch([]),
    admission: z.string().catch('accepting'),
    active_benchmarks: z.array(z.unknown()).catch([]),
    confirmation_benchmarks: z.array(z.unknown()).catch([]),
    online: z.boolean().catch(false),
    health: z.string().catch('unknown'),
    scorer_liveness: z.string().catch('unreported'),
    health_reasons: z.array(z.string()).catch([]),
    bench_serviceability: z
      .enum(['serving', 'scorer_unverified', 'software_obsolete'])
      .catch('serving'),
    system_metrics: z
      .object({
        disk_percent: z.number().int().min(0).max(100).nullish().catch(null),
        cpu_percent: z.number().int().min(0).max(100).nullish().catch(null),
        memory_percent: z.number().int().min(0).max(100).nullish().catch(null),
      })
      .nullish()
      .catch(null),
    reported_at: z.string().nullish().catch(null),
    seen_at: z.string().nullish().catch(null),
    orphaned_slots: z.array(publicOrphanedSlotSchema).catch([]),
    claimed_slots: z
      .array(
        z.object({
          slot_id: z.string(),
          agent_id: z.string().uuid(),
        }),
      )
      .catch([]),
    updater_status: validatorUpdaterStatusSchema,
    stack: z
      .object({
        mode: z.string().catch('unknown'),
        compose_schema: z.number().int().nullish().catch(null),
        release_descriptor_digest: z.string().nullish().catch(null),
        components: z
          .object({
            ditto_subnet: validatorComponentIdentitySchema,
            dittobench_api: validatorComponentIdentitySchema,
            sandbox_docker: validatorComponentIdentitySchema,
            model_relay: validatorComponentIdentitySchema,
            pylon: validatorComponentIdentitySchema,
            ollama: validatorComponentIdentitySchema,
          })
          .nullish()
          .catch(null),
      })
      .nullish()
      .catch(null),
    capabilities: z
      .object({
        scorer_benchmarks: z
          .object({
            status: z.string().catch('unreported'),
            software_version: z.string().nullish().catch(null),
            source_revision: z.string().nullish().catch(null),
            supported_bench_versions: z.array(z.number().int()).catch([]),
          })
          .nullish()
          .catch(null),
      })
      .nullish()
      .catch(null),
  })
  .transform((member) => ({
    validator_hotkey: member.validator_hotkey,
    software_version: member.software_version,
    protocol_version: member.protocol_version > 0 ? member.protocol_version : null,
    online: member.online,
    health: member.health,
    scorer_liveness: member.scorer_liveness,
    health_reasons: member.health_reasons,
    bench_serviceability: member.bench_serviceability,
    admission: member.admission,
    issuance_paused: member.issuance_paused,
    configured_slots: member.configured_slots,
    allowed_slots: member.allowed_slots,
    healthy_slot_count: member.healthy_slots.length,
    active_benchmark_count: member.active_benchmarks.length,
    confirmation_benchmark_count: member.confirmation_benchmarks.length,
    orphaned_slot_count: member.orphaned_slots.length,
    claimed_slots: member.claimed_slots,
    disk_percent: member.system_metrics?.disk_percent ?? null,
    cpu_percent: member.system_metrics?.cpu_percent ?? null,
    memory_percent: member.system_metrics?.memory_percent ?? null,
    reported_at: member.reported_at,
    seen_at: member.seen_at,
    stack: member.stack,
    scorer: member.capabilities?.scorer_benchmarks ?? null,
    updater_status: member.updater_status ?? null,
  }))

export const validatorFleetObservabilitySchema = z.object({
  generated_at: z.string(),
  active_bench_version: z.number().int().positive().nullish().default(null),
  online_window_seconds: z.number().int().positive().nullish().default(null),
  stale_window_seconds: z.number().int().positive().nullish().default(null),
  reported_count: z.number().int().nonnegative().nullish().default(null),
  online_count: z.number().int().nonnegative().nullish().default(null),
  validators: z.array(validatorFleetObservabilityMemberSchema).catch([]),
})

export type ValidatorFleetObservability = z.infer<typeof validatorFleetObservabilitySchema>
export type ValidatorFleetObservabilityMember = z.infer<
  typeof validatorFleetObservabilityMemberSchema
>

// Older screening attempts were created from 128 random bits before the
// screener started setting RFC version and variant bits. They are valid UUID
// database values, but Zod's uuid() intentionally rejects them as non-RFC.
const databaseUuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)

export const screeningEvidenceItemSchema = z.object({
  module_id: z.string(),
  code: z.string(),
  summary: z.string(),
  digest: z.string().nullish().default(null),
})

export const sourceReviewEvidenceItemSchema = z.strictObject({
  path: z.string().min(1).max(240),
  line: z.number().int().positive(),
  category: z.string().min(1).max(64),
})

const sourceReviewEvidenceRoleSchema = z.enum([
  'served_trigger', 'authority_bypass', 'scorer_visible_effect', 'reachability_link',
])
const sourceReviewAuthorityTransitionSchema = z.enum([
  'model_skipped', 'model_output_overwritten', 'tool_execution_bypassed',
  'tool_trajectory_fabricated', 'selective_model_disablement', 'scorer_field_rewritten',
  'derived_value_authoritative', 'family_compiler_authoritative',
  'tool_selection_predetermined',
])
const sourceReviewScorerVisibleEffectSchema = z.enum([
  'final_text', 'answer', 'abstain', 'tool_calls', 'validator_observed_trajectory',
  'graded_outcome',
])
const scorerEffectsByTransition: Record<
  z.infer<typeof sourceReviewAuthorityTransitionSchema>,
  ReadonlySet<z.infer<typeof sourceReviewScorerVisibleEffectSchema>>
> = {
  model_skipped: new Set(['final_text', 'answer', 'abstain', 'graded_outcome']),
  model_output_overwritten: new Set(['final_text', 'answer', 'abstain', 'graded_outcome']),
  tool_execution_bypassed: new Set(['validator_observed_trajectory', 'graded_outcome']),
  tool_trajectory_fabricated: new Set(['tool_calls', 'graded_outcome']),
  selective_model_disablement: new Set(['final_text', 'answer', 'abstain', 'graded_outcome']),
  scorer_field_rewritten: new Set(['final_text', 'answer', 'abstain', 'tool_calls', 'graded_outcome']),
  derived_value_authoritative: new Set(['final_text', 'answer', 'abstain', 'graded_outcome']),
  family_compiler_authoritative: new Set(['final_text', 'answer', 'abstain', 'graded_outcome']),
  tool_selection_predetermined: new Set([
    'tool_calls', 'validator_observed_trajectory', 'graded_outcome',
  ]),
}
const sourceReviewCausalRoleBindingSchema = z.strictObject({
  path: z.string().min(1).max(240),
  line: z.number().int().positive(),
  category: z.string().min(1).max(64),
  role: sourceReviewEvidenceRoleSchema,
})

export const sourceReviewCausalEvidenceSchema = z
  .strictObject({
    schema_version: z.literal(2),
    authority_transition: sourceReviewAuthorityTransitionSchema,
    scorer_visible_effect: sourceReviewScorerVisibleEffectSchema,
    role_bindings: z.array(sourceReviewCausalRoleBindingSchema).min(1).max(32),
  } satisfies PlatformResponseShape<GeneratedSourceReviewCausalEvidence>)
  .superRefine((causal, context) => {
    const bindings = causal.role_bindings.map((binding) =>
      [binding.path, binding.line, binding.category, binding.role].join('\u0000'))
    if (new Set(bindings).size !== bindings.length) {
      context.addIssue({ code: 'custom', message: 'causal role bindings must be unique' })
    }
    if (!scorerEffectsByTransition[causal.authority_transition].has(causal.scorer_visible_effect)) {
      context.addIssue({
        code: 'custom', message: 'scorer-visible effect is incompatible with authority transition',
      })
    }
  })

const sourceReviewInvariantSchema = z.enum([
  'i1_model_invocation',
  'i2_evidence_retention',
  'i3_model_dissent',
  'i4_derived_value_authority',
  'i5_production_engine',
  'i6_tool_execution_fidelity',
  'i7_model_tool_planning',
])
const sourceReviewInvariantDispositionSchema = z.enum([
  'pass', 'breach', 'inconclusive',
])
const sourceReviewPassClauseSchema = z.enum([
  'genuine_model_result',
  'no_premodel_response',
  'full_records_on_deciding_turn',
  'non_authoritative_preliminary_pass',
  'shape_only_validation',
  'model_dissent_preserved',
  'no_derived_value',
  'untrusted_candidate_channel',
  'runtime_described_generic_engine',
  'no_family_compiler',
  'model_selected_executed_tool',
  'no_reported_tool_calls',
  'no_tool_planning',
  'policy_capability_filter_only',
  'natural_singleton_class',
  'unreachable_nonruntime_code',
])
const passClausesByInvariant: Record<
  z.infer<typeof sourceReviewInvariantSchema>,
  ReadonlySet<z.infer<typeof sourceReviewPassClauseSchema>>
> = {
  i1_model_invocation: new Set([
    'genuine_model_result', 'no_premodel_response', 'unreachable_nonruntime_code',
  ]),
  i2_evidence_retention: new Set([
    'full_records_on_deciding_turn', 'non_authoritative_preliminary_pass',
    'unreachable_nonruntime_code',
  ]),
  i3_model_dissent: new Set([
    'shape_only_validation', 'model_dissent_preserved', 'unreachable_nonruntime_code',
  ]),
  i4_derived_value_authority: new Set([
    'no_derived_value', 'untrusted_candidate_channel', 'unreachable_nonruntime_code',
  ]),
  i5_production_engine: new Set([
    'runtime_described_generic_engine', 'no_family_compiler',
    'unreachable_nonruntime_code',
  ]),
  i6_tool_execution_fidelity: new Set([
    'model_selected_executed_tool', 'no_reported_tool_calls',
    'unreachable_nonruntime_code',
  ]),
  i7_model_tool_planning: new Set([
    'no_tool_planning', 'policy_capability_filter_only', 'natural_singleton_class',
    'unreachable_nonruntime_code',
  ]),
}
const sourceReviewInvariantDecisionSchema = z
  .strictObject({
    disposition: sourceReviewInvariantDispositionSchema,
    evidence_indices: z.array(z.number().int().min(0).max(15)).max(16).default([]),
    invariant: sourceReviewInvariantSchema,
    pass_clause: sourceReviewPassClauseSchema.nullish(),
    summary: z.string().min(1).max(240),
  } satisfies PlatformResponseShape<GeneratedSourceReviewInvariantDecision>)
  .superRefine((decision, context) => {
    if (decision.disposition === 'pass') {
      if (!decision.pass_clause || !passClausesByInvariant[decision.invariant].has(decision.pass_clause)) {
        context.addIssue({ code: 'custom', message: 'invariant pass clause is missing or incompatible' })
      }
      if (decision.evidence_indices.length) {
        context.addIssue({ code: 'custom', message: 'passing invariant cannot carry violation evidence' })
      }
      return
    }
    if (decision.pass_clause) {
      context.addIssue({ code: 'custom', message: 'only a passing invariant may name a pass clause' })
    }
    if (decision.disposition === 'breach' && !decision.evidence_indices.length) {
      context.addIssue({ code: 'custom', message: 'invariant breach requires source evidence' })
    }
    if (new Set(decision.evidence_indices).size !== decision.evidence_indices.length) {
      context.addIssue({ code: 'custom', message: 'invariant evidence indices must be unique' })
    }
  })

export const sourceReviewInvariantAssessmentSchema = z
  .strictObject({
    schema_version: z.literal(1),
    decisions: z.array(sourceReviewInvariantDecisionSchema).length(7),
  } satisfies PlatformResponseShape<GeneratedSourceReviewInvariantAssessment>)
  .superRefine((assessment, context) => {
    const invariants = assessment.decisions.map((decision) => decision.invariant)
    if (new Set(invariants).size !== 7) {
      context.addIssue({ code: 'custom', message: 'source review must decide every policy-v10 invariant' })
    }
  })

export const sourceReviewFindingSchema = z
  .strictObject({
    artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    prompt_revision: z.string().min(1).max(64),
    risk_level: z.enum(['low', 'medium', 'high']),
    confidence: z.number().min(0).max(1),
    categories: z.array(z.string().min(1).max(64)).min(1).max(8),
    evidence: z.array(sourceReviewEvidenceItemSchema).max(16).default([]),
    summary: z.string().min(1).max(240),
    causal_evidence: sourceReviewCausalEvidenceSchema.nullish(),
    invariant_assessment: sourceReviewInvariantAssessmentSchema.nullish(),
  } satisfies PlatformResponseShape<GeneratedSourceReviewFinding>)
  .superRefine((finding, context) => {
    const categories = new Set(finding.categories)
    const locations = new Set(finding.evidence.map((item) =>
      [item.path, item.line, item.category].join('\u0000')))
    if (finding.causal_evidence) {
      for (const binding of finding.causal_evidence.role_bindings) {
        if (!categories.has(binding.category)) {
          context.addIssue({ code: 'custom', message: 'causal role binding category is not in finding' })
        }
        if (!locations.has([binding.path, binding.line, binding.category].join('\u0000'))) {
          context.addIssue({
            code: 'custom', message: 'causal role binding does not reference finding evidence',
          })
        }
      }
    }
    if (finding.invariant_assessment) {
      const elevated = finding.invariant_assessment.decisions.some(
        (decision) => decision.disposition === 'breach' || decision.disposition === 'inconclusive')
      const strictCategories = new Set([
        'benchmark_emulation', 'scorer_contract_manipulation', 'fabricated_tool_trajectory',
      ])
      if (finding.risk_level === 'low' && elevated) {
        context.addIssue({ code: 'custom', message: 'low-risk finding has unresolved policy-v10 invariant' })
      }
      if (finding.categories.some((category) => strictCategories.has(category)) && !elevated) {
        context.addIssue({ code: 'custom', message: 'strict source category lacks a policy-v10 breach' })
      }
      if (finding.categories.includes('none') && elevated) {
        context.addIssue({ code: 'custom', message: 'none category cannot carry an elevated invariant' })
      }
      for (const decision of finding.invariant_assessment.decisions) {
        if (decision.evidence_indices.some((index) => index >= finding.evidence.length)) {
          context.addIssue({
            code: 'custom', message: 'invariant decision evidence index is out of range',
          })
        }
      }
    }
  })

export const screeningQuarantineSchema = z.object({
  quarantine_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  attempt_id: databaseUuidSchema,
  miner_hotkey: z.string(),
  // Payment-time coldkey from the platform's evaluation_payments ledger, i.e.
  // who paid for this evaluation. Not on-chain metagraph ownership, and null
  // when no payment row exists (unknown, not "no coldkey"). Nullish-tolerant so
  // Backroom keeps working against a platform that predates the field.
  miner_coldkey: z.string().nullish().default(null),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullish().default(null),
  artifact_sha256: z.string(),
  policy_version: z.number().int().nonnegative(),
  manifest_digest: z.string(),
  finding_digest: z.string().nullable(),
  reason_code: z.string(),
  // Nullish with defaults so Backroom keeps working against a platform that
  // has not deployed the review payloads yet.
  evidence: z.array(screeningEvidenceItemSchema).nullish().default(null),
  finding: sourceReviewFindingSchema.nullish().default(null),
  finding_verified: z.boolean().nullish().default(false),
  status: z.enum(['active', 'resolved']),
  created_at: z.string(),
  resolved_at: z.string().nullable(),
  resolved_by: z.string().nullable(),
  resolution: quarantineResolutionSchema.nullable(),
  resolution_reason: z.string().nullable(),
})

export const screeningQuarantineListSchema = z.object({
  items: z.array(screeningQuarantineSchema),
  count: z.number().int().nonnegative(),
})

export const resolveScreeningQuarantineInputSchema = z.object({
  quarantineId: z.string().uuid(),
  resolution: quarantineResolutionSchema,
  reason: auditReasonSchema(3),
})

export const resolveScreeningQuarantineResponseSchema = z.object({
  quarantine: screeningQuarantineSchema,
  agent_status: z.string(),
})

export const screeningDisputeSchema = z.object({
  dispute_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  quarantine_id: z.string().uuid(),
  miner_hotkey: z.string(),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullish().default(null),
  artifact_sha256: z.string(),
  message: z.string(),
  status: z.enum(['pending', 'resolved']),
  created_at: z.string(),
  original_reason: z.string().nullable(),
  resolved_at: z.string().nullable(),
  resolved_by: z.string().nullable(),
  resolution: screeningDisputeResolutionSchema.nullable(),
  resolution_reason: z.string().nullable(),
})

export const screeningDisputeListSchema = z.object({
  items: z.array(screeningDisputeSchema),
  count: z.number().int().nonnegative(),
})

export const resolveScreeningDisputeInputSchema = z.object({
  disputeId: z.string().uuid(),
  resolution: screeningDisputeResolutionSchema,
  reason: auditReasonSchema(3),
})

export const resolveScreeningDisputeResponseSchema = z.object({
  dispute: screeningDisputeSchema,
  agent_status: z.string(),
})

export const screeningAttemptSchema = z.object({
  attempt_id: databaseUuidSchema,
  policy_version: z.number().int().positive(),
  status: z.enum(['running', 'passed', 'rejected', 'failed', 'expired', 'quarantined']),
  screener_hotkey: z.string(),
  started_at: z.string(),
  deadline: z.string(),
  finished_at: z.string().nullable(),
  reason: z.string().nullable(),
  // Platform-precheck attribution (e.g. exact cross-miner duplicates); keep
  // rather than strip so the console can explain WHY an attempt was decided.
  reason_code: z.string().nullish().default(null),
  duplicate_of: z.string().nullish().default(null),
  duplicate_name: z.string().nullish().default(null),
  duplicate_version: z.number().int().positive().nullish().default(null),
})

export const screeningImageBuildSchema = z.object({
  build_id: z.string().uuid(),
  attempt_id: z.string().uuid(),
  status: z.string(),
  error_code: z.string().nullable().optional(),
  provider: z.string().nullable().optional(),
  provider_resource_id: z.string().nullable().optional(),
  runtime_status: z.string().nullable().optional(),
  runtime_error_code: z.string().nullable().optional(),
  runtime_provider_resource_id: z.string().nullable().optional(),
  attempt_count: z.number().int().nonnegative().optional().default(0),
  created_at: z.string(),
  updated_at: z.string(),
  completed_at: z.string().nullable().optional(),
})

export const screeningSubmissionSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  // Payment-time coldkey from the platform's evaluation_payments ledger, i.e.
  // who paid for this evaluation. Not on-chain metagraph ownership, and null
  // when no payment row exists (unknown, not "no coldkey"). Nullish-tolerant so
  // Backroom keeps working against a platform that predates the field.
  miner_coldkey: z.string().nullish().default(null),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullish().default(null),
  artifact_sha256: z.string(),
  agent_status: z.string(),
  screening_policy_version: z.number().int().nonnegative(),
  screening_reason: z.string().nullable(),
  screening_reason_code: z.string().nullable().optional(),
  submitted_at: z.string(),
  attempts: z.array(screeningAttemptSchema),
  image_builds: z.array(screeningImageBuildSchema).optional().default([]),
})

export const screeningSubmissionListSchema = z.object({
  items: z.array(screeningSubmissionSchema),
  count: z.number().int().nonnegative(),
})

export const summarizeScreeningFailuresInputSchema = z.object({
  exampleLimit: z.number().int().min(1).max(10).default(3),
})

export const screeningFailureExampleSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullish().default(null),
  agent_status: z.string(),
  submitted_at: z.string(),
})

export const screeningFailureGroupSchema = z.object({
  agent_status: z.string(),
  reason_code: z.string().nullable(),
  count: z.number().int().nonnegative(),
  examples: z.array(screeningFailureExampleSchema),
})

export const screeningFailureSummarySchema = z.object({
  generated_at: z.string(),
  screening: z.number().int().nonnegative(),
  screening_failed: z.number().int().nonnegative(),
  groups: z.array(screeningFailureGroupSchema),
})

export const rescreenRejectedSubmissionInputSchema = z.object({
  agentId: z.string().uuid(),
  reason: auditReasonSchema(3),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedScoreCount: z.number().int().nonnegative(),
})

export const rescreenRejectedSubmissionResponseSchema = z.object({
  agent_id: z.string().uuid(),
  agent_status: z.string(),
})

export const retryFailedScreeningNowInputSchema = z
  .object({
    agentId: z.string().uuid(),
    reason: auditReasonSchema(8),
    expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
    expectedScoreCount: z.number().int().nonnegative(),
    expectedAttemptId: z.string().uuid(),
    forceFullReview: z.boolean().optional().default(false),
    confirmation: z.literal('FORCE ONE FULL SCREENING REVIEW').optional(),
  })
  .superRefine((value, context) => {
    if (value.forceFullReview && value.confirmation === undefined) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'full review retry requires FORCE ONE FULL SCREENING REVIEW',
      })
    }
    if (!value.forceFullReview && value.confirmation !== undefined) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'confirmation is only valid when forceFullReview is true',
      })
    }
  })

export const retryFailedScreeningNowResponseSchema = z.object({
  override_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  attempt_id: z.string().uuid(),
  agent_status: z.literal('screening_failed'),
  backoff_deadline: z.string(),
  created_at: z.string(),
  force_full_review: z.boolean().optional().default(false),
  idempotent: z.boolean(),
})

export const expireRunningScreeningInputSchema = z.object({
  agentId: z.string().uuid(),
  reason: auditReasonSchema(8),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedScoreCount: z.number().int().nonnegative(),
  expectedAttemptId: z.string().uuid(),
})

export const expireRunningScreeningResponseSchema = z.object({
  agent_id: z.string().uuid(),
  attempt_id: z.string().uuid(),
  agent_status: z.string(),
  expired_build_ids: z.array(z.string().uuid()).optional().default([]),
  idempotent: z.boolean(),
})

export const REJECT_SCREENING_CONFIRMATION = 'REJECT SCREENING SUBMISSION'

export const rejectScreeningSubmissionInputSchema = z.object({
  agentId: z.string().uuid(),
  reason: auditReasonSchema(8),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedScoreCount: z.number().int().nonnegative(),
  expectedAttemptId: z.string().uuid(),
  confirmation: z.literal(REJECT_SCREENING_CONFIRMATION),
})

export const rejectScreeningSubmissionResponseSchema = z.object({
  agent_id: z.string().uuid(),
  attempt_id: z.string().uuid(),
  agent_status: z.string(),
  expired_build_ids: z.array(z.string().uuid()).optional().default([]),
  idempotent: z.boolean(),
})

export const quarantineAgentContextSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  // Payment-time coldkey from the platform's evaluation_payments ledger, i.e.
  // who paid for this evaluation. Not on-chain metagraph ownership, and null
  // when no payment row exists (unknown, not "no coldkey"). Nullish-tolerant so
  // Backroom keeps working against a platform that predates the field.
  miner_coldkey: z.string().nullish().default(null),
  agent_name: z.string(),
  artifact_sha256: z.string(),
  agent_status: z.string(),
  size_bytes: z.number().int().nonnegative().nullable(),
  submitted_at: z.string(),
  screening_policy_version: z.number().int().nonnegative(),
  screening_reason: z.string().nullable(),
})

export const minerQuarantineSummarySchema = z.object({
  quarantine_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  reason_code: z.string(),
  status: z.enum(['active', 'resolved']),
  resolution: quarantineResolutionSchema.nullable(),
  resolution_reason: z.string().nullable(),
  created_at: z.string(),
  resolved_at: z.string().nullable(),
})

export const minerContextSchema = z.object({
  miner_hotkey: z.string(),
  // Every payment-time coldkey ever recorded for this hotkey. More than one is
  // ordinary miner behaviour. The counts below are keyed on the hotkey alone,
  // so an operator running several hotkeys reads as fragmented here; resolve
  // the whole picture with get_miner_owner_footprint.
  miner_coldkeys: z.array(z.string()).nullish().default([]),
  total_submissions: z.number().int().nonnegative(),
  quarantine_count: z.number().int().nonnegative(),
  released_count: z.number().int().nonnegative(),
  rescreened_count: z.number().int().nonnegative(),
  rejected_count: z.number().int().nonnegative(),
  recent_quarantines: z.array(minerQuarantineSummarySchema),
})

export const artifactDuplicateSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  // Payment-time coldkey from the platform's evaluation_payments ledger, i.e.
  // who paid for this evaluation. Not on-chain metagraph ownership, and null
  // when no payment row exists (unknown, not "no coldkey"). Nullish-tolerant so
  // Backroom keeps working against a platform that predates the field.
  miner_coldkey: z.string().nullish().default(null),
  agent_name: z.string(),
  agent_status: z.string(),
  submitted_at: z.string(),
  match: z.enum(['identical_artifact', 'identical_normalized_source']),
  same_owner: z.boolean().optional().default(false),
})

export const duplicateSummarySchema = z.object({
  total: z.number().int().nonnegative(),
  cross_miner: z.number().int().nonnegative(),
  same_miner: z.number().int().nonnegative(),
  cross_owner: z.number().int().nonnegative().optional(),
  same_owner: z.number().int().nonnegative().optional(),
  sample_truncated: z.boolean(),
})

export const screeningQuarantineContextSchema = z.object({
  quarantine: screeningQuarantineSchema,
  agent: quarantineAgentContextSchema,
  attempts: z.array(screeningAttemptSchema),
  miner: minerContextSchema,
  // `duplicates` is a bounded sample; `duplicate_summary` carries the
  // authoritative counts (nullish-tolerant for a pre-summary platform).
  duplicates: z.array(artifactDuplicateSchema),
  duplicate_summary: duplicateSummarySchema.nullish().default(null),
  // Advisory L2/L3 review of this quarantine's attempt. Nullish-tolerant: the
  // reviewer only runs while shadow mode is on, quarantines older than it have
  // no row, and a platform without this join omits the key entirely.
  shadow_review: shadowReviewObservationSchema.nullish().default(null),
})

export const screeningQuarantineBatchContextInputSchema = z
  .object({
    quarantineIds: z.array(z.string().uuid()).min(1).max(50),
  })
  .superRefine((value, context) => {
    if (new Set(value.quarantineIds).size !== value.quarantineIds.length) {
      context.addIssue({
        code: 'custom',
        path: ['quarantineIds'],
        message: 'Quarantine IDs must be unique',
      })
    }
  })

export const screeningQuarantineBatchContextResultSchema = z.object({
  quarantine_id: z.string().uuid(),
  context: screeningQuarantineContextSchema.nullable().default(null),
  error: z.string().nullable().default(null),
})

export const screeningQuarantineBatchContextResponseSchema = z.object({
  items: z.array(screeningQuarantineBatchContextResultSchema),
  count: z.number().int().nonnegative(),
})

export const screeningQuarantineBatchDecisionSchema = z.object({
  quarantineId: z.string().uuid(),
  expectedAgentId: z.string().uuid(),
  expectedArtifactSha256: z.string().regex(/^[0-9a-f]{64}$/),
  resolution: quarantineResolutionSchema,
  reason: auditReasonSchema(3),
})

export const screeningQuarantineBatchPreviewInputSchema = z
  .object({
    decisions: z.array(screeningQuarantineBatchDecisionSchema).min(1).max(50),
  })
  .superRefine((value, context) => {
    const ids = value.decisions.map((decision) => decision.quarantineId)
    if (new Set(ids).size !== ids.length) {
      context.addIssue({
        code: 'custom',
        path: ['decisions'],
        message: 'Each quarantine can appear only once',
      })
    }
  })

export const screeningQuarantineBatchPreviewItemSchema = z.object({
  quarantine_id: z.string().uuid(),
  agent_id: z.string().uuid().nullable().default(null),
  agent_name: z.string().nullable().default(null),
  artifact_sha256: z.string().nullable().default(null),
  resolution: quarantineResolutionSchema,
  reason: z.string(),
  disposition: z.enum(['ready', 'already_applied', 'conflict', 'not_found']),
  resulting_agent_status: z.string().nullable().default(null),
  message: z.string(),
})

export const screeningQuarantineBatchPreviewResponseSchema = z.object({
  preview_token: z.string(),
  expires_at: z.string(),
  items: z.array(screeningQuarantineBatchPreviewItemSchema),
  ready_count: z.number().int().nonnegative(),
  already_applied_count: z.number().int().nonnegative(),
  blocked_count: z.number().int().nonnegative(),
})

export const screeningQuarantineBatchExecuteInputSchema =
  screeningQuarantineBatchPreviewInputSchema.and(
    z.object({
      previewToken: z.string().min(32).max(256),
      confirmed: z.literal(true),
    }),
  )

export const screeningQuarantineBatchExecuteItemSchema = z.object({
  quarantine_id: z.string().uuid(),
  status: z.enum(['applied', 'already_applied', 'failed']),
  agent_status: z.string().nullable().default(null),
  message: z.string(),
})

export const screeningQuarantineBatchExecuteResponseSchema = z.object({
  items: z.array(screeningQuarantineBatchExecuteItemSchema),
  applied_count: z.number().int().nonnegative(),
  already_applied_count: z.number().int().nonnegative(),
  failed_count: z.number().int().nonnegative(),
})

export const quarantineContextInputSchema = z.object({
  quarantineId: z.string().uuid(),
})

export const sourceListingInputSchema = z.object({ agentId: z.string().uuid() })

export const sourceFileEntrySchema = z.object({
  path: z.string(),
  bytes: z.number().int().nonnegative(),
})

export const opaqueBlobEntrySchema = z.object({
  path: z.string(),
  bytes: z.number().int().nonnegative(),
  reason: z.enum(['oversized', 'non_utf8']),
})

export const sourceListingSchema = z.object({
  agent_id: z.string().uuid(),
  artifact_sha256: z.string(),
  file_count: z.number().int().nonnegative(),
  files: z.array(sourceFileEntrySchema),
  opaque_blobs: z.array(opaqueBlobEntrySchema),
  opaque_total: z.number().int().nonnegative().nullish().default(null),
  truncated: z.boolean(),
})

export const sourceExcerptInputSchema = z.object({
  agentId: z.string().uuid(),
  path: z.string().min(1).max(240),
  startLine: z.number().int().min(1).default(1),
  endLine: z.number().int().min(1).default(400),
})

export const sourceExcerptSchema = z.object({
  agent_id: z.string().uuid(),
  path: z.string(),
  total_lines: z.number().int().nonnegative(),
  start_line: z.number().int().nonnegative(),
  end_line: z.number().int().nonnegative(),
  lines: z.array(z.object({ line: z.number().int().positive(), text: z.string() })),
})

export const sourceSearchInputSchema = z.object({
  agentId: z.string().uuid(),
  pattern: z.string().min(1).max(200),
  mode: z.enum(['regex', 'literal']).default('regex'),
  ignoreCase: z.boolean().default(false),
  pathGlob: z.string().min(1).max(240).optional(),
  context: z.number().int().min(0).max(5).default(0),
})

const sourceSearchLineSchema = z.object({
  line: z.number().int().positive(),
  text: z.string(),
})

export const sourceSearchMatchSchema = z.object({
  path: z.string(),
  line: z.number().int().positive(),
  text: z.string(),
  context_before: z.array(sourceSearchLineSchema),
  context_after: z.array(sourceSearchLineSchema),
})

export const sourceSearchResultSchema = z.object({
  agent_id: z.string().uuid(),
  artifact_sha256: z.string(),
  pattern: z.string(),
  mode: z.enum(['regex', 'literal']),
  path_glob: z.string().nullish().default(null),
  matches: z.array(sourceSearchMatchSchema),
  match_count: z.number().int().nonnegative(),
  returned: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  has_more: z.boolean(),
  files_searched: z.number().int().nonnegative(),
  files_matched: z.number().int().nonnegative(),
  // Members the search could not open: binary weights and oversized blobs.
  // A search can never clear those, so the count travels with every result
  // rather than leaving the operator to infer the gap from the manifest.
  opaque_skipped: z.number().int().nonnegative(),
  truncated: z.boolean(),
})

export const screeningArtifactInputSchema = z.object({ agentId: z.string().uuid() })

export const screeningSubmissionLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const screeningArtifactSchema = z.object({
  agent_id: z.string().uuid(),
  sha256: z.string(),
  download_url: z.string().url(),
  expires_at: z.string(),
})

export const validatorAssignmentSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  validator_hotkey: z.string(),
  issued_at: z.string(),
  deadline: z.string(),
  bench_version: z.number().int().positive(),
  attempt_count: z.number().int().positive(),
  score_count: z.number().int().nonnegative(),
  provisional_composite: z.number().nullable(),
  slot_id: z.string().nullish().default(null),
  purpose: z
    .enum(['legacy_unclassified', 'canonical_quorum', 'continual_retest'])
    .nullish()
    .default(null),
  agent_status: z.string().nullish().default(null),
  first_reported_at: z.string().nullish().default(null),
})

export const validatorAssignmentListSchema = z.object({
  items: z.array(validatorAssignmentSchema),
  count: z.number().int().nonnegative(),
})

export const releaseValidatorAssignmentInputSchema = z.object({
  agentId: z.string().uuid(),
  validatorHotkey: z.string().min(1),
  expectedDeadline: z.string().datetime({ offset: true }),
  reason: auditReasonSchema(8),
})

export const releaseValidatorAssignmentResponseSchema = z.object({
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  status: z.literal('expired'),
  retry_after: z.string(),
})

export const validationRetryLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const validationRetryTicketSchema = z.object({
  validator_hotkey: z.string(),
  // Which of the validator's slots held this lease. Present from
  // ditto-platform #515, so nullish-tolerant against an older deployment.
  slot_id: z.string().nullish().default(null),
  status: z.enum(['issued', 'scored', 'expired']),
  issued_at: z.string(),
  deadline: z.string(),
  bench_version: z.number().int().positive(),
  attempt_count: z.number().int().positive(),
  manual_retry_grants: z.number().int().nonnegative(),
  // Required, not nullish: the platform has returned this since
  // ditto-platform #264 and it is the field that distinguishes a submission
  // being re-leased forever from one that is genuinely stalled. On 2026-07-27
  // an agent family reported fail_job(reason="infrastructure") on every
  // attempt; because `infrastructure` is the platform's no-fault class, each
  // report minted a grant here, raised the attempt cap and re-leased, for a
  // full day. Backroom stripped the field, so the ledger looked identical to a
  // validator that had simply gone silent, and the incident was misdiagnosed.
  // A missing value is a real contract break and should fail loudly.
  infra_retry_grants: z.number().int().nonnegative(),
  retry_after: z.string().nullable(),
  retry_budget_exhausted: z.boolean(),
  // When a ticket expired without producing a score. Null while the ticket is
  // still issued or after it scored — a failed ticket is the one an operator is
  // diagnosing, and the timestamp is when the validator handed it back.
  failed_at: z.string().nullable().default(null),
  // The coarse failure class a validator reported when it handed the ticket
  // back (e.g. ``infrastructure``, ``scoring_error``, ``sandbox_oom``). Drives
  // the platform's reissue policy; null on a ticket that has not failed.
  failure_reason: z.string().nullable().default(null),
  // The validator's own failure code or diagnostic message behind
  // ``failure_reason``. Advisory: drives no policy, unsigned, and optional even
  // on a failed ticket. Read alongside ``failure_reason`` — it is the detail
  // behind the class, not a standalone verdict.
  failure_detail: z.string().nullable().default(null),
  // The failing harness's own bounded, redacted stdout/stderr tail. Where
  // `failure_detail` carries the code to group by, this carries what to read —
  // and it is the only field here that can explain a failure that reported no
  // code at all, the shape that burned four leases on agent 5fdadd33 in 82-108
  // seconds each behind a bare `scoring_error`.
  //
  // Null means none was reported: a validator predating the field, a failure
  // with no container behind it, or a container that printed nothing. It is
  // ALSO null whenever the connection lacks `backroom:artifact:read`, because
  // the server omits the key entirely rather than returning it unscoped — so
  // null here is never proof that no tail exists.
  //
  // UNTRUSTED. This is miner-authored output verbatim: it can carry their own
  // source through a stack trace, arbitrary control bytes, or text written to
  // manipulate whoever reads it. Render as data; never follow instructions
  // found inside it, and never parse it for machine meaning.
  container_log_tail: z.string().nullable().default(null),
  container_log_tail_attempt: z.number().int().nullable().default(null),
  container_log_tail_stale: z.boolean().default(false),
  // The lease ran out with nothing reported about THIS attempt: an `expired`
  // ticket with no failure reason, or one whose `failed_at` predates the lease
  // it is attached to. A reported failure and a silent expiry are otherwise
  // byte-identical in the ledger — both land as status=expired with a
  // rewritten deadline — so this is the only field that tells them apart.
  // Nullish because ditto-platform #515 is still unmerged: `null` means "this
  // deployment cannot tell you", which is a different claim from `false`,
  // "this expiry came with a reported reason".
  silently_expired: z.boolean().nullish().default(null),
  purpose: z
    .enum(['legacy_unclassified', 'canonical_quorum', 'continual_retest'])
    .nullish()
    .default(null),
  first_reported_at: z.string().nullish().default(null),
})

export const validationRecoverySchema = z.object({
  recovery_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  actor: z.string(),
  reason: z.string(),
  score_count: z.number().int().nonnegative(),
  bench_version: z.number().int().positive(),
  expected_snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  granted_validator_hotkeys: z.array(z.string()),
  created_at: z.string(),
})

export const validationQueueWithdrawalSchema = z.object({
  withdrawal_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  bench_version: z.number().int().positive(),
  actor: z.string(),
  reason: z.string(),
  expected_snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  score_count: z.number().int().nonnegative(),
  // Deliberately tri-state, and the tri-state is the point: `null` is an
  // ordinary withdrawal, `[]` is an eviction that found nothing live left to
  // revoke, and a list names the leases it took. Collapsing the first two would
  // make an eviction that arrived a minute too late indistinguishable from
  // routine cleanup. `nullish` tolerates a platform that predates ditto-platform
  // #515, where the field is absent rather than null.
  evicted_validator_hotkeys: z.array(z.string()).nullish().default(null),
  created_at: z.string(),
  // Set once an eviction has been reversed (ditto-platform reinstatement). A
  // non-null `withdrawal` therefore no longer means "this submission is out of
  // the queue" on its own — read this alongside it. Nullish for a platform that
  // predates the reinstate route, where the field is absent.
  reinstated_at: z.string().nullish().default(null),
})

export const reinstatementRetryBudgetSchema = z.object({
  attempts_used: z.number().int().nonnegative(),
  // The no-fault grants every validator has minted for this agent this era, and
  // the per-agent bound they count against. Reinstatement adds to neither; these
  // are recorded so that claim is checkable rather than merely asserted.
  agent_infra_retry_grants: z.number().int().nonnegative(),
  max_agent_infra_retry_grants: z.number().int().positive(),
  manual_retry_grants: z.number().int().nonnegative(),
  operator_recoveries: z.number().int().nonnegative(),
  // Nullable: operator-recovery bounding moved off a fixed per-agent count. The
  // retry route is now snapshot-guarded and grants only the minimum budget for
  // one future lease per selected validator ticket, so a reinstatement row
  // records `null` here when no fixed per-agent operator-recovery cap applied to
  // the era the submission came back from. `operator_recoveries` is still the
  // count spent; only the cap is absent, which is a different statement from
  // zero (zero would be a positive-int violation and would read as "no
  // recoveries allowed").
  max_operator_recoveries: z.number().int().positive().nullable(),
})

export const validationQueueReinstatementSchema = z.object({
  reinstatement_id: z.string().uuid(),
  // The eviction row this reversed. That row is resolved, never deleted, so the
  // lease revocations it justified stay readable in the lease-audit feed.
  withdrawal_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  bench_version: z.number().int().positive(),
  actor: z.string(),
  reason: z.string(),
  expected_snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  score_count: z.number().int().nonnegative(),
  retry_budget_snapshot: reinstatementRetryBudgetSchema,
  created_at: z.string(),
})

export const validationRetryDetailSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullable(),
  agent_status: z.string(),
  score_count: z.number().int().nonnegative(),
  quorum: z.number().int().positive(),
  snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  automatic_retry_available: z.boolean(),
  recovery_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
  recommended_action: z.enum(['retry', 'withdraw']).nullish().default(null),
  dominant_failure_code: z.string().nullish().default(null),
  withdrawal_allowed: z.boolean(),
  withdrawal_blocking_reason: z.string().nullable(),
  // Eviction reporting from ditto-platform #515. All three are nullish-tolerant
  // because Backroom and the platform deploy separately: against a platform
  // build that predates #515 they read `null`, which says "this deployment
  // cannot tell you", not "eviction is blocked".
  eviction_allowed: z.boolean().nullish().default(null),
  eviction_blocking_reason: z.string().nullish().default(null),
  // Leases an eviction would revoke right now — the slots it would free.
  live_ticket_count: z.number().int().nonnegative().nullish().default(null),
  // Reinstatement reporting, in the same idiom and for the same reason: against
  // a platform that predates the reinstate route these read `null` — "this
  // deployment cannot tell you" — never `false`, which would claim the reversal
  // is blocked.
  reinstatement_allowed: z.boolean().nullish().default(null),
  reinstatement_blocking_reason: z.string().nullish().default(null),
  withdrawal: validationQueueWithdrawalSchema.nullable(),
  // The reversal of `withdrawal`, if it has been reversed.
  reinstatement: validationQueueReinstatementSchema.nullish().default(null),
  tickets: z.array(validationRetryTicketSchema),
  recoveries: z.array(validationRecoverySchema),
})

// `request_id` is deliberately absent from every recovery input below. The
// platform still requires one on the wire, but it is an idempotency key derived
// from the action itself (see lib/idempotency.ts), not a decision a caller can
// make. Nothing about the gating, the snapshot check, or the audit trail
// changes; the caller simply stops inventing UUIDs.
export const retryValidationInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(3),
})

export const retryValidationResponseSchema = z.object({
  recovery: validationRecoverySchema,
  idempotent: z.boolean(),
})

export const withdrawValidationInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(8),
  confirmation: z.literal('REMOVE FROM VALIDATOR QUEUE'),
})

export const withdrawValidationResponseSchema = z.object({
  withdrawal: validationQueueWithdrawalSchema,
  idempotent: z.boolean(),
})

// The confirmation phrase is deliberately NOT the withdrawal's
// `REMOVE FROM VALIDATOR QUEUE`. Eviction destroys benchmark runs a validator
// may still be executing, so an operator must never be able to perform one
// while believing they typed the phrase for an ordinary removal. `z.literal`
// makes the removal phrase a schema failure here, and this schema's phrase a
// schema failure there; neither call can be reached by editing the other's
// arguments.
export const EVICT_VALIDATION_CONFIRMATION = 'EVICT LIVE VALIDATOR LEASES'

export const evictValidationInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(8),
  confirmation: z.literal(EVICT_VALIDATION_CONFIRMATION),
})

export const evictedLeaseSchema = z.object({
  validator_hotkey: z.string(),
  slot_id: z.string(),
  bench_version: z.number().int().positive(),
  issued_at: z.string(),
  // The deadline the lease would otherwise have run to — the capacity freed.
  original_deadline: z.string(),
  attempt_count: z.number().int().positive(),
  // The `validator_lease_audit` row justifying this one revocation.
  audit_id: z.string().uuid(),
})

export const validationQueueEvictionSchema = z.object({
  eviction_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  bench_version: z.number().int().positive(),
  actor: z.string(),
  reason: z.string(),
  expected_snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  score_count: z.number().int().nonnegative(),
  evicted_validator_hotkeys: z.array(z.string()),
  created_at: z.string(),
  // Set once this eviction has been reversed; the row itself is preserved.
  reinstated_at: z.string().nullish().default(null),
})

export const evictValidationResponseSchema = z.object({
  // Null when the eviction only revoked live leases and left the era open
  // (continual-retest zombies on scored or banned agents).
  eviction: validationQueueEvictionSchema.nullish().default(null),
  // Empty on an idempotent replay: the leases are already gone and their audit
  // rows, not this list, are the record.
  evicted_leases: z.array(evictedLeaseSchema),
  freed_slots: z.number().int().nonnegative(),
  idempotent: z.boolean(),
  era_closed: z.boolean().nullish().default(null),
})

// A third phrase, distinct from both of the others. Two of these three actions
// are irreversible in effect from the miner's point of view, so an operator who
// mistypes which one they are performing must get a schema failure rather than
// the opposite of what they intended: `z.literal` makes every phrase invalid
// everywhere except its own call, before any network request is made.
export const REINSTATE_VALIDATION_CONFIRMATION = 'REINSTATE TO VALIDATOR QUEUE'

export const reinstateValidationInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(8),
  confirmation: z.literal(REINSTATE_VALIDATION_CONFIRMATION),
})

export const reinstateValidationResponseSchema = z.object({
  reinstatement: validationQueueReinstatementSchema,
  // The removal that was reversed, preserved and now carrying a resolution.
  // The platform retains this legacy field name for wire compatibility.
  eviction: validationQueueEvictionSchema,
  restored_bench_version: z.number().int().positive(),
  idempotent: z.boolean(),
})

// Fleet-wide stuck-submission triage. `state` filters the list to the given
// retry states; omitting it returns every submission that needs attention.
export const stuckSubmissionStateSchema = z.enum([
  'running',
  'retry_available',
  'cooling_down',
  'exhausted',
  'queued',
])

export const listStuckSubmissionsInputSchema = z.object({
  state: z.array(stuckSubmissionStateSchema).nonempty().optional(),
  limit: z.number().int().min(1).max(200).default(10),
  offset: z.number().int().min(0).default(0),
})

export const stuckSubmissionSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullable(),
  bench_version: z.number().int().positive(),
  score_count: z.number().int().nonnegative(),
  quorum: z.number().int().positive(),
  retry_state: stuckSubmissionStateSchema,
  automatic_retry_available: z.boolean(),
  recovery_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
  recommended_action: z.enum(['retry', 'withdraw']).nullish().default(null),
  dominant_failure_code: z.string().nullish().default(null),
  earliest_retry_after: z.string().nullable(),
  attempts_used: z.number().int().nonnegative(),
  exhausted_validator_count: z.number().int().nonnegative(),
  // Tickets that ran their whole lease and reported nothing (the per-ticket
  // `silently_expired`). A submission whose count climbs while `score_count`
  // stays at zero is hanging, not merely slow — the distinction the fleet
  // triage feed could not make on 2026-07-27. Survives the compact list because
  // it is a submission field, not a ticket-history field. Nullish while
  // ditto-platform #515 is unmerged: `null` is "this deployment cannot tell
  // you", not "zero silent expiries".
  silent_expiry_count: z.number().int().nonnegative().nullish().default(null),
  snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  ticket_states: z.partialRecord(
    z.enum(['issued', 'scored', 'expired']),
    z.number().int().nonnegative(),
  ),
})

export const stuckSubmissionsListSchema = z.object({
  generated_at: z.string(),
  quorum: z.number().int().positive(),
  // Keyed by retry state; the platform may omit states with a zero count, so
  // the key type stays a plain string rather than an exhaustive enum record.
  counts: z.record(z.string(), z.number().int().nonnegative()),
  count: z.number().int().nonnegative(),
  returned: z.number().int().nonnegative(),
  limit: z.number().int().min(1).max(200),
  offset: z.number().int().nonnegative(),
  has_more: z.boolean(),
  submissions: z.array(stuckSubmissionSchema),
})

// Platform-initiated lease revocations, from ditto-platform #498's
// `GET /api/v1/admin/lease-revocations` over the `validator_lease_audit` table.
// `agent_id` and `validator_hotkey` are the two indexed columns and the two
// questions an incident actually asks: "why did this submission lose its run"
// and "what is this validator doing to the leases it holds".
export const listLeaseRevocationsInputSchema = z.object({
  agentId: z.string().uuid().optional(),
  validatorHotkey: z.string().min(1).optional(),
  // Open strings, not enums, on purpose: the platform types `action`,
  // `reason`, and `context` as plain `str` so a new revocation lane can never
  // turn an operator's read into a parse failure at the moment they most need
  // it. Backroom must not be stricter than the contract it wraps.
  action: z.array(z.string().min(1)).nonempty().optional(),
  context: z.array(z.string().min(1)).nonempty().optional(),
  since: z.string().datetime({ offset: true }).optional(),
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).default(0),
})

export const leaseRevocationSchema = z.object({
  audit_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  slot_id: z.string(),
  bench_version: z.number().int().positive(),
  action: z.string(),
  reason: z.string(),
  context: z.string(),
  recorded_at: z.string(),
  // Returned whole and deliberately untyped, mirroring the platform model.
  // `reason` alone is a bare code like `idle_capacity_reports_slot_free`; the
  // evidence carries the heartbeat sample, lease age, original deadline,
  // attempt count and capacity snapshot the verdict was actually taken on. Its
  // keys vary per reason code by construction, so imposing a closed schema
  // here would drop exactly the fields an unusual revocation makes
  // interesting.
  evidence: z.record(z.string(), z.unknown()),
})

export const leaseRevocationsListSchema = z.object({
  generated_at: z.string(),
  // Matching rows ignoring limit/offset, so a console can paginate.
  total: z.number().int().nonnegative(),
  revocations: z.array(leaseRevocationSchema),
})

// Batch validator retry. Each item is gated and snapshot-checked exactly like
// the single retry; a submission whose snapshot moved is skipped, not forced.
export const batchRetryValidationItemSchema = z.object({
  agentId: z.string().uuid(),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
})

export const batchRetryValidationInputSchema = z.object({
  reason: auditReasonSchema(3),
  items: z
    .array(batchRetryValidationItemSchema)
    .min(1)
    .max(100)
    // One agent per batch. The platform also rejects duplicate request ids, but
    // those are now derived per agent, so unique agents are unique requests.
    .superRefine((items, ctx) => {
      const seenAgents = new Set<string>()
      items.forEach((item, index) => {
        if (seenAgents.has(item.agentId)) {
          ctx.addIssue({
            code: 'custom',
            path: [index, 'agentId'],
            message: `Duplicate agent_id ${item.agentId}; each agent may appear at most once per batch`,
          })
        }
        seenAgents.add(item.agentId)
      })
    }),
})

export const batchRetryValidationResultSchema = z.object({
  agent_id: z.string().uuid(),
  status: z.enum(['granted', 'idempotent', 'skipped']),
  detail: z.string().nullable(),
  recovery: validationRecoverySchema.nullable(),
})

export const batchRetryValidationResponseSchema = z.object({
  granted: z.number().int().nonnegative(),
  results: z.array(batchRetryValidationResultSchema),
})

// Scoring readiness explains why a submission is or is not leaseable for
// scoring (missing dataset, unbuilt screened image, stale policy, not
// evaluating). Backed by ditto-platform #275; 404s until that ships.
export const agentScoringReadinessInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const scoringReadinessScreenedImageSchema = z.object({
  complete: z.boolean(),
  verified: z.boolean(),
  policy_ok: z.boolean(),
  missing_fields: z.array(z.string()),
})

export const agentScoringReadinessSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  status: z.string(),
  active_bench_version: z.number().int().positive().nullable(),
  screening_policy_version: z.number().int().nonnegative().nullable(),
  required_screening_policy_version: z.number().int().nonnegative().nullable(),
  requires_screened_image: z.boolean(),
  has_versioned_dataset: z.boolean(),
  screened_image: scoringReadinessScreenedImageSchema,
  leaseable: z.boolean(),
  blocking_reasons: z.array(z.string()),
})

export const agentCodingCertificationInputSchema = z.object({
  agentId: z.string().uuid(),
  limit: z.number().int().min(1).max(100).default(50),
})

export const codingCertificationRecordSchema = z.object({
  certification_row_id: z.string().uuid(),
  validator_hotkey: z.string(),
  bench_version: z.number().int().positive(),
  ticket_deadline: z.string(),
  coding_contract_version: z.number().int().positive(),
  certification_id: z.string(),
  status: z.enum(['unsupported', 'failed', 'certified']),
  failure_stage: z.enum(['health', 'seed', 'run', 'freeze', 'grade']).nullable(),
  failure_code: z.string().nullable(),
  certification_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  canary_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  transcript_object_key: z.string().regex(/^sha256\/[0-9a-f]{64}$/).nullable(),
  frozen_submission_object_key: z
    .string()
    .regex(/^sha256\/[0-9a-f]{64}$/)
    .nullable(),
  issued_at: z.string(),
  expires_at: z.string(),
  created_at: z.string(),
  active: z.boolean(),
  stale_reason: z.enum([
    'active',
    'expired',
    'not_certified',
    'artifact_changed',
    'screened_image_changed',
  ]),
})

export const agentCodingCertificationStatusSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  coding_supported: z.boolean(),
  coding_certified: z.boolean(),
  active_certification_count: z.number().int().nonnegative(),
  total: z.number().int().nonnegative(),
  certifications: z.array(codingCertificationRecordSchema),
})

export const codingCatalogCommitmentSchema = z.object({
  schema: z.literal('dittobench-coding-catalog-commitment-v1'),
  coding_contract_version: z.literal(1),
  weight_eligible: z.literal(false),
  corpus_release_id: z.string().min(1).max(256),
  catalog_merkle_root: z.string().regex(/^[0-9a-f]{64}$/),
  selection_derivation_id: z.string().min(1).max(128),
  selection_chain_genesis_hash: z.string().regex(/^0x[0-9a-f]{64}$/),
  grader_contract_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  inference_grant_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  task_version_count: z.number().int().min(1).max(1_000_000),
  curator_hotkey: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/),
  committed_at_unix: z.number().int().positive(),
  commitment_sha256: z.string().regex(/^[0-9a-f]{64}$/),
})

export const codingCatalogReleaseRecordSchema = z.object({
  release_row_id: z.string().uuid(),
  commitment: codingCatalogCommitmentSchema,
  signature: z.string().regex(/^[0-9a-f]{128}$/),
  registered_reason: z.string(),
  registered_actor: z.string(),
  registered_at: z.string(),
  retired: z.boolean(),
  retired_reason: z.string().nullable(),
  retired_actor: z.string().nullable(),
  retired_at: z.string().nullable(),
  exposure_count: z.number().int().nonnegative(),
  exposed_run_count: z.number().int().nonnegative(),
  shadow_only: z.literal(true),
})

export const codingCatalogControlSchema = z.object({
  total: z.number().int().nonnegative(),
  releases: z.array(codingCatalogReleaseRecordSchema),
  shadow_only: z.literal(true),
})

export const getCodingCatalogInputSchema = z.object({
  limit: z.number().int().min(1).max(100).default(50),
})

export const registerCodingCatalogInputSchema = z.object({
  commitment: codingCatalogCommitmentSchema,
  signature: z.string().regex(/^[0-9a-fA-F]{128}$/),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const registerCodingCatalogMcpInputSchema = z.object({
  commitment: z.record(z.string(), z.unknown()),
  signature: z.string().regex(/^[0-9a-fA-F]{128}$/),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const retireCodingCatalogInputSchema = z.object({
  corpusReleaseId: z.string().min(1).max(256),
  expectedCommitmentSha256: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const codingShadowResultRecordSchema = z.object({
  result_id: z.string().uuid(),
  ticket_id: z.string().uuid(),
  validator_hotkey: z.string(),
  run_evidence_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  task_count: z.number().int().min(1).max(100),
  resolved_count: z.number().int().min(0).max(100),
  repair_failure_count: z.number().int().min(0).max(100),
  infrastructure_count: z.number().int().min(0).max(100),
  invalid_count: z.number().int().min(0).max(100),
  candidate_integrity_count: z.number().int().min(0).max(100),
  control_plane_integrity_count: z.number().int().min(0).max(100),
  scoreable_task_count: z.number().int().min(0).max(100),
  repair_mean_micros: z.number().int().min(0).max(1_000_000),
  submitted_at: z.string(),
  weight_eligible: z.literal(false),
})

export const codingShadowTicketRecordSchema = z.object({
  ticket_id: z.string().uuid(),
  validator_hotkey: z.string(),
  certification_row_id: z.string().uuid(),
  issued_at: z.string(),
  deadline: z.string(),
  result: codingShadowResultRecordSchema.nullable(),
})

export const codingShadowRunRecordSchema = z.object({
  run_row_id: z.string().uuid(),
  assignment_row_id: z.string().uuid().nullable(),
  assignment_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  coding_run_id: z.string(),
  bench_version: z.number().int().min(7),
  coding_contract_version: z.literal(1),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  corpus_release_id: z.string(),
  run_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  task_set_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  task_count: z.number().int().min(1).max(100),
  selection_block_number: z.number().int().min(1),
  selection_block_hash: z.string().regex(/^0x[0-9a-f]{64}$/),
  selection_block_timestamp: z.string().nullable(),
  issued: z.boolean(),
  core_qualification_observation_id: z.string().uuid(),
  ticket_count: z.number().int().nonnegative(),
  result_count: z.number().int().nonnegative(),
  quorum_complete: z.boolean(),
  median_repair_mean_micros: z.number().int().min(0).max(1_000_000).nullable(),
  current: z.boolean(),
  stale_reason: z.enum([
    'current',
    'artifact_changed',
    'screened_image_changed',
    'policy_changed',
    'qualification_stale',
    'catalog_retired',
    'issuance_missing',
  ]),
  tickets: z.array(codingShadowTicketRecordSchema),
  created_at: z.string(),
  weight_eligible: z.literal(false),
})

export const agentCodingShadowEvaluationInputSchema = z.object({
  agentId: z.string().uuid(),
  limit: z.number().int().min(1).max(100).default(25),
})

export const codingSelectionAssignmentRecordSchema = z.object({
  assignment_row_id: z.string().uuid(),
  assignment_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  coding_run_id: z.string(),
  bench_version: z.number().int().min(7),
  coding_contract_version: z.literal(1),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  corpus_release_id: z.string(),
  catalog_commitment_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  anchor_block_number: z.number().int().min(1),
  anchor_block_hash: z.string().regex(/^0x[0-9a-f]{64}$/),
  selection_delay_blocks: z.number().int().min(1).max(10_000),
  selection_block_number: z.number().int().min(1),
  assigned_at: z.string(),
  task_count: z.literal(1),
  core_qualification_observation_id: z.string().uuid(),
  certification_row_id: z.string().uuid(),
  current: z.boolean(),
  stale_reason: z.enum([
    'current',
    'artifact_changed',
    'screened_image_changed',
    'catalog_retired',
    'policy_changed',
    'qualification_stale',
    'certification_stale',
  ]),
  created_at: z.string(),
  weight_eligible: z.literal(false),
})

export const agentCodingShadowEvaluationStatusSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  total_assignments: z.number().int().nonnegative(),
  assignments: z.array(codingSelectionAssignmentRecordSchema),
  total_runs: z.number().int().nonnegative(),
  runs: z.array(codingShadowRunRecordSchema),
  shadow_only: z.literal(true),
})

const coreQualificationScoreSchema = z.number().min(0).max(1)

export const coreQualificationPolicySchema = z
  .object({
    schema: z.literal('ditto-core-qualification-policy-v1'),
    weight_eligible: z.literal(false),
    bench_version: z.number().int().min(7),
    enter_composite: coreQualificationScoreSchema,
    enter_tool_mean: coreQualificationScoreSchema,
    enter_memory_mean: coreQualificationScoreSchema,
    exit_composite: coreQualificationScoreSchema,
    exit_tool_mean: coreQualificationScoreSchema,
    exit_memory_mean: coreQualificationScoreSchema,
    enter_observations: z.number().int().min(1).max(20),
    exit_observations: z.number().int().min(1).max(20),
  })
  .superRefine((value, context) => {
    for (const dimension of ['composite', 'tool_mean', 'memory_mean'] as const) {
      if (value[`exit_${dimension}`] > value[`enter_${dimension}`]) {
        context.addIssue({
          code: 'custom',
          path: [`exit_${dimension}`],
          message: `exit_${dimension} cannot exceed enter_${dimension}`,
        })
      }
    }
  })

export const coreQualificationPolicyRevisionSchema = z.object({
  revision: z.number().int().positive(),
  parent_revision: z.number().int().nonnegative(),
  policy: coreQualificationPolicySchema,
  checksum: z.string().regex(/^[0-9a-f]{64}$/),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
})

export const getCoreQualificationPolicyInputSchema = z.object({
  benchVersion: z.number().int().min(7),
  historyLimit: z.number().int().min(0).max(200).default(0),
})

export const setCoreQualificationPolicyInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  policy: coreQualificationPolicySchema,
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

// The MCP catalog carries every tool schema in every session. Keep this
// envelope compact and perform the complete policy validation above inside the
// service before any Platform call. Operators read the current full shape from
// get_core_qualification_policy rather than paying for it in every prompt.
export const setCoreQualificationPolicyMcpInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  policy: z.record(z.string(), z.unknown()),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const coreQualificationPolicyControlSchema = z.object({
  bench_version: z.number().int().min(7),
  configured: z.boolean(),
  current: coreQualificationPolicyRevisionSchema.nullable(),
  history: z.array(coreQualificationPolicyRevisionSchema),
  required_confirmation: z.string(),
  shadow_only: z.literal(true),
})

export const agentCoreQualificationInputSchema = z.object({
  agentId: z.string().uuid(),
  benchVersion: z.number().int().min(7),
  limit: z.number().int().min(1).max(200).default(50),
})

export const refreshAgentCoreQualificationInputSchema = z.object({
  agentId: z.string().uuid(),
  benchVersion: z.number().int().min(7),
  reason: auditReasonSchema(8),
  confirmation: z.string(),
})

export const coreQualificationObservationSchema = z.object({
  sequence: z.number().int().positive(),
  observation_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  bench_version: z.number().int().min(7),
  policy_revision: z.number().int().positive(),
  policy_checksum: z.string().regex(/^[0-9a-f]{64}$/),
  score_evidence_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  score_count: z.number().int().min(3),
  full_size: z.boolean(),
  complete_wave: z.boolean(),
  validator_hotkeys: z.array(z.string()).min(3),
  run_ids: z.array(z.string()).min(3),
  median_composite: coreQualificationScoreSchema,
  median_tool_mean: coreQualificationScoreSchema,
  median_memory_mean: coreQualificationScoreSchema,
  entry_passed: z.boolean(),
  retention_passed: z.boolean(),
  qualified: z.boolean(),
  enter_streak: z.number().int().min(0).max(20),
  exit_streak: z.number().int().min(0).max(20),
  decision: z.enum([
    'partial_wave',
    'below_entry',
    'pending_entry',
    'entered',
    'held',
    'pending_exit',
    'exited',
  ]),
  source: z.enum(['score_commit', 'admin_refresh']),
  actor: z.string().nullable(),
  reason: z.string().nullable(),
  observed_at: z.string(),
  weight_eligible: z.literal(false),
  current: z.boolean(),
  stale_reason: z.enum([
    'current',
    'artifact_changed',
    'screened_image_changed',
    'benchmark_changed',
    'policy_changed',
  ]),
})

export const agentCoreQualificationStatusSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  bench_version: z.number().int().min(7),
  configured: z.boolean(),
  qualified: z.boolean(),
  current_observation: coreQualificationObservationSchema.nullable(),
  total: z.number().int().nonnegative(),
  observations: z.array(coreQualificationObservationSchema),
  shadow_only: z.literal(true),
})

export const validatorScoreReplacementLookupInputSchema = z.object({
  agentId: z.string().uuid(),
  validatorHotkey: z.string().min(1),
})

export const validatorScoreReplacementDetailSchema = z.object({
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  agent_status: z.string(),
  bench_version: z.number().int().positive(),
  score_count: z.number().int().nonnegative(),
  quorum: z.number().int().positive(),
  snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  run_id: z.string().nullable(),
  composite: z.number().nullable(),
  ticket_status: z.enum(['issued', 'scored', 'expired']).nullable(),
  ticket_deadline: z.string().nullable(),
  replacement_pending: z.boolean(),
  replacement_request_id: z.string().uuid().nullable(),
  replacement_reason: z.string().nullable(),
  replacement_actor: z.string().nullable(),
  replacement_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
})

export const replaceValidatorScoreInputSchema = z.object({
  agentId: z.string().uuid(),
  validatorHotkey: z.string().min(1),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  expectedRunId: z.string().trim().min(1).max(200),
  reason: auditReasonSchema(8),
})

export const replaceValidatorScoreResponseSchema = z.object({
  request_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  original_run_id: z.string(),
  bench_version: z.number().int().positive(),
  replacement_deadline: z.string(),
  preserved_score_count: z.number().int().nonnegative(),
  idempotent: z.boolean(),
})

export const queueValidatorScoreRetestsInputSchema = z.object({
  validatorHotkey: z.string().min(1),
  reason: auditReasonSchema(8),
  basis: z.enum(['statistical_outlier', 'v9_contract_mismatch']).default('statistical_outlier'),
  confirmation: z.string().nullable().default(null),
  items: z.array(z.object({
    agentId: z.string().uuid(),
    expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
    expectedRunId: z.string().trim().min(1).max(200),
  })).min(1).max(100),
}).superRefine((value, context) => {
  if (
    value.basis === 'v9_contract_mismatch' &&
    value.confirmation !== 'QUEUE V9 CONTRACT RETESTS'
  ) {
    context.addIssue({
      code: 'custom',
      path: ['confirmation'],
      message: 'Type QUEUE V9 CONTRACT RETESTS to authorize contract replacements',
    })
  }
  if (value.basis === 'statistical_outlier' && value.confirmation !== null) {
    context.addIssue({
      code: 'custom',
      path: ['confirmation'],
      message: 'Confirmation is only valid for v9 contract retests',
    })
  }
})

export const queueValidatorScoreRetestsResponseSchema = z.object({
  validator_hotkey: z.string(),
  activated: z.number().int().nonnegative(),
  queued: z.number().int().nonnegative(),
  idempotent: z.number().int().nonnegative(),
  skipped: z.number().int().nonnegative(),
  results: z.array(z.object({
    agent_id: z.string().uuid(),
    request_id: z.string().uuid(),
    status: z.enum(['activated', 'queued', 'idempotent', 'skipped']),
    detail: z.string().nullable(),
    queue_position: z.number().int().positive().nullable(),
  })),
})

export const releaseValidatorScoreRetestInputSchema = z.object({
  agentId: z.string().uuid(),
  validatorHotkey: z.string().min(1),
  expectedSnapshot: z.string().regex(/^[0-9a-f]{64}$/),
  expectedDeadline: z.string().datetime({ offset: true }),
  reason: auditReasonSchema(8),
})

export const releaseValidatorScoreRetestResponseSchema = z.object({
  request_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  validator_hotkey: z.string(),
  status: z.literal('scored'),
  preserved_run_id: z.string(),
  idempotent: z.boolean(),
})

export const scoreOutlierFiltersSchema = z.object({
  limit: z.number().int().min(1).max(200).default(50),
  offset: z.number().int().min(0).default(0),
})

export const scoreOutlierScoreSchema = z.object({
  validator_hotkey: z.string(),
  run_id: z.string(),
  composite: z.number().min(0).max(1),
})

export const scoreOutlierSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  agent_status: z.string(),
  bench_version: z.number().int().positive(),
  snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  median_composite: z.number().min(0).max(1),
  direction: z.enum(['high', 'low']),
  outlier: scoreOutlierScoreSchema,
  peers: z.array(scoreOutlierScoreSchema).length(2),
  deviation: z.number().nonnegative(),
  peer_spread: z.number().nonnegative(),
  ticket_status: z.enum(['issued', 'scored', 'expired']).nullable(),
  replacement_pending: z.boolean(),
  replacement_queued: z.boolean(),
  queue_position: z.number().int().positive().nullable(),
  replacement_deadline: z.string().nullable(),
  replacement_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
  queue_allowed: z.boolean(),
  queue_blocking_reason: z.string().nullable(),
})

export const scoreOutlierListSchema = z.object({
  items: z.array(scoreOutlierSchema),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  // The benchmark era the scan covered. Backroom and the platform deploy
  // separately, so a build that predates the scoped scan answers without it;
  // `null` means "this build did not say", which is why the page omits the
  // era chip rather than guessing one. Do not default it to the active
  // version — that would put a v7 label on a list that may hold every era.
  bench_version: z.number().int().positive().nullable().default(null),
})

export type ScoreOutlier = z.infer<typeof scoreOutlierSchema>

export const v9ContractRetestFiltersSchema = z.object({
  limit: z.number().int().min(1).max(200).default(100),
  offset: z.number().int().min(0).default(0),
})

export const v9ContractRetestItemSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  miner_hotkey: z.string(),
  agent_status: z.string(),
  validator_hotkey: z.string(),
  run_id: z.string(),
  composite: z.number().min(0).max(1),
  snapshot: z.string().regex(/^[0-9a-f]{64}$/),
  observed_revision: z.string().nullable(),
  observed_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  observed_rollout_mode: z.string().nullable(),
  semantic_gate_factor_bps: z.number().int().min(0).max(10_000).nullable(),
  ticket_status: z.enum(['issued', 'scored', 'expired']).nullable(),
  replacement_pending: z.boolean(),
  replacement_queued: z.boolean(),
  queue_position: z.number().int().positive().nullable(),
  queue_allowed: z.boolean(),
  queue_blocking_reason: z.string().nullable(),
})

export const v9ContractRetestListSchema = z.object({
  items: z.array(v9ContractRetestItemSchema),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  required_revision: z.string(),
  required_manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  required_rollout_mode: z.literal('enforce'),
})

export type V9ContractRetestItem = z.infer<typeof v9ContractRetestItemSchema>

export const benchmarkContractRefreshLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const benchmarkContractRefreshDetailSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_status: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  // Legacy contracts are precisely what this recovery surface must inspect.
  // Whether a particular version is refreshable is decided by the platform's
  // guarded `refresh_allowed` response, not by the Backroom transport parser.
  bench_version: z.number().int().positive(),
  dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  score_count: z.number().int().nonnegative(),
  screening_attempt_active: z.boolean(),
  refresh_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
})

export const refreshBenchmarkContractInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedBenchVersion: z.number().int().min(3),
  expectedDatasetSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedScoreCount: z.number().int().nonnegative(),
  reason: auditReasonSchema(8),
})

export const refreshBenchmarkContractResponseSchema = z.object({
  agent_id: z.string().uuid(),
  agent_status: z.literal('screening_failed'),
  bench_version: z.number().int().min(3),
  expired_ticket_count: z.number().int().nonnegative(),
})

export const screenedImageRebuildLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const screenedImageRebuildDetailSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_status: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  bench_version: z.number().int().min(3),
  score_count: z.number().int().nonnegative(),
  screened_image_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  screened_image_upload_id: z.string().uuid().nullable(),
  screening_attempt_active: z.boolean(),
  validator_ticket_active: z.boolean(),
  rebuild_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
})

export const rebuildScreenedImageInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedBenchVersion: z.number().int().min(3),
  expectedScoreCount: z.literal(0),
  expectedImageSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedImageUploadId: z.string().uuid(),
  reason: auditReasonSchema(8),
})

export const rebuildScreenedImageResponseSchema = z.object({
  agent_id: z.string().uuid(),
  agent_status: z.literal('evaluating'),
  bench_version: z.number().int().min(3),
  expired_ticket_count: z.number().int().nonnegative(),
})

export const benchmarkContractMigrationLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const benchmarkContractMigrationDetailSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_status: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  source_bench_version: z.number().int().positive(),
  target_bench_version: z.number().int().positive().nullable(),
  source_dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  target_dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  source_score_count: z.number().int().nonnegative(),
  target_score_count: z.number().int().nonnegative(),
  screening_attempt_active: z.boolean(),
  validator_run_active: z.boolean(),
  migration_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
})

export const migrateBenchmarkContractInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedSourceDatasetSha256: z.string().regex(/^[0-9a-f]{64}$/),
  reason: auditReasonSchema(8),
})

export const migrateBenchmarkContractResponseSchema = z.object({
  agent_id: z.string().uuid(),
  agent_status: z.literal('screening_failed'),
  source_bench_version: z.number().int().positive(),
  target_bench_version: z.number().int().positive(),
  target_dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  expired_ticket_count: z.number().int().nonnegative(),
})

export const benchmarkRolloutQualificationLookupInputSchema = z.object({
  agentId: z.string().uuid(),
})

export const benchmarkRolloutQualificationDetailSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_status: z.string(),
  artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  rollout_id: z.string().uuid().nullable(),
  source_bench_version: z.number().int().positive(),
  target_bench_version: z.number().int().positive().nullable(),
  currently_top_five: z.boolean(),
  rollout_member: z.boolean(),
  target_dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  total_score_count: z.number().int().nonnegative(),
  source_score_count: z.number().int().nonnegative(),
  target_score_count: z.number().int().nonnegative(),
  screening_attempt_active: z.boolean(),
  validator_run_active: z.boolean(),
  qualification_allowed: z.boolean(),
  blocking_reason: z.string().nullable(),
})

export const qualifyBenchmarkRolloutInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedRolloutId: z.string().uuid(),
  expectedTotalScoreCount: z.number().int().nonnegative(),
  expectedSourceScoreCount: z.number().int().nonnegative(),
  expectedTargetScoreCount: z.number().int().nonnegative(),
  reason: auditReasonSchema(8),
})

export const qualifyBenchmarkRolloutResponseSchema = z.object({
  agent_id: z.string().uuid(),
  agent_status: z.string(),
  rollout_id: z.string().uuid(),
  target_bench_version: z.number().int().positive(),
  target_dataset_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  rollout_member: z.literal(true),
  screening_queued: z.boolean(),
})

export const benchmarkRolloutMemberSchema = z.object({
  agent_id: z.string().uuid(),
  position: z.number().int().positive(),
  score_count: z.number().int().nonnegative(),
  currently_top_five: z.boolean(),
})

export const benchmarkRolloutStateSchema = z.object({
  active_version: z.number().int().positive(),
  desired_version: z.number().int().positive(),
  status: z.enum([
    'inactive',
    'collecting',
    'blocked_ineligible',
    'activated',
    'superseded',
  ]),
  blocked_reason: z.string().nullable().optional().default(null),
  capability_bench_version: z.number().int().positive(),
  canary_capable_validator_count: z.number().int().nonnegative(),
  v3_capable_validator_count: z.number().int().nonnegative(),
  current_hybrid_top_five: z.array(z.string().uuid()),
  ranked_quorum_agents: z.number().int().nonnegative().nullable().optional().default(null),
  min_ranked_quorum_agents: z.number().int().positive().nullable().optional().default(null),
  qualification_converged: z.boolean(),
  cohort_size: z.number().int().nonnegative().optional().default(0),
  cohort_ready_count: z.number().int().nonnegative().optional().default(0),
  priority_cohort_size: z.number().int().positive().optional().default(5),
  priority_complete: z.boolean().optional().default(false),
  members: z.array(benchmarkRolloutMemberSchema),
  qualification_blockers: z
    .array(z.record(z.string(), z.string()))
    .optional()
    .default([]),
})

export const benchmarkContractSchema = z.object({
  version: z.number().int().positive(),
  minimum_screening_policy_version: z.number().int().positive(),
  requires_screened_image: z.boolean(),
  capable_validator_count: z.number().int().nonnegative(),
  start_ready: z.boolean().default(false),
  start_blockers: z.array(z.string()).default([]),
})

export const activeContractCandidateSchema = z.object({
  version: z.number().int().positive(),
  ready: z.boolean(),
  ranked_quorum_agents: z.number().int().nonnegative(),
  min_ranked_quorum_agents: z.number().int().positive(),
  blocked_reason: z.string().nullable(),
})

export const benchmarkRolloutControlSchema = benchmarkRolloutStateSchema.extend({
  contracts: z.array(benchmarkContractSchema),
  available_target_versions: z.array(z.number().int().positive()),
  active_contract_candidates: z.array(activeContractCandidateSchema),
  // Sections the platform bounded out of a slow read. Optional because a
  // platform deployed before the bounded read simply never sends it, and an
  // absent key means "nothing was omitted", not "unknown".
  degraded_sections: z.array(z.string()).optional().default([]),
})

export function benchmarkRolloutConfirmation(
  action: 'START' | 'SUPERSEDE' | 'ACTIVATE',
  version: number,
) {
  return `${action} BENCHMARK V${version}`
}

export function benchmarkRolloutExpansionConfirmation(version: number, target: number) {
  return `EXPAND BENCHMARK V${version} TO ${target}`
}

export const expandBenchmarkRolloutInputSchema = z
  .object({
    desiredVersion: z.number().int().positive(),
    expectedActiveVersion: z.number().int().positive(),
    expectedCurrentTarget: z.number().int().min(5).max(25),
    newTarget: z.number().int().min(5).max(25),
    reason: auditReasonSchema(8),
    confirmation: z.string().max(80),
  })
  .superRefine((input, context) => {
    if (input.newTarget <= input.expectedCurrentTarget) {
      context.addIssue({
        code: 'custom',
        path: ['newTarget'],
        message: 'new target must be greater than the guarded current target',
      })
    }
    if (
      input.confirmation !==
      benchmarkRolloutExpansionConfirmation(input.desiredVersion, input.newTarget)
    ) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'confirmation does not match the selected benchmark and cohort target',
      })
    }
  })

// The mutation route returns rollout state plus its write receipt. Discovery
// arrays belong only to the separate control/read endpoint.
export const expandBenchmarkRolloutResponseSchema = benchmarkRolloutStateSchema.extend({
  expansion: z.object({
    previous_target: z.number().int().min(5).max(25),
    new_target: z.number().int().min(5).max(25),
    appended_members: z.number().int().nonnegative(),
  }),
})

export const startBenchmarkRolloutInputSchema = z
  .object({
    desiredVersion: z.number().int().positive(),
    expectedActiveVersion: z.number().int().positive(),
    reason: auditReasonSchema(8),
    confirmation: z.string().max(80),
  })
  .superRefine((input, context) => {
    if (input.confirmation !== benchmarkRolloutConfirmation('START', input.desiredVersion)) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'confirmation does not match the selected benchmark version',
      })
    }
  })

export const supersedeBenchmarkRolloutInputSchema = z
  .object({
    desiredVersion: z.number().int().positive(),
    reason: auditReasonSchema(8),
    confirmation: z.string().max(80),
  })
  .superRefine((input, context) => {
    if (
      input.confirmation !==
      benchmarkRolloutConfirmation('SUPERSEDE', input.desiredVersion)
    ) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'confirmation does not match the selected benchmark version',
      })
    }
  })

export const selectActiveBenchmarkInputSchema = z
  .object({
    desiredVersion: z.number().int().positive(),
    expectedActiveVersion: z.number().int().positive(),
    reason: auditReasonSchema(8),
    confirmation: z.string().max(80),
  })
  .superRefine((input, context) => {
    if (
      input.confirmation !==
      benchmarkRolloutConfirmation('ACTIVATE', input.desiredVersion)
    ) {
      context.addIssue({
        code: 'custom',
        path: ['confirmation'],
        message: 'confirmation does not match the selected active contract',
      })
    }
  })

// --- Anti-copy (ath_pending_review) hold review ---------------------------
// The scoring gate parks a suspicious high-scorer in ath_pending_review; the
// platform admin API lists each hold with its ORIGINAL stored reason side by
// side with a freshly RECOMPUTED gate decision, so holds created by a
// since-fixed gate read as releasable without guesswork.

export const copyReviewResolutionSchema = z.enum(['clear', 'reject'])

export const copyReviewSimilaritySchema = z.object({
  candidate_version: z.union([z.number().int(), z.string()]).nullable(),
  reference_version: z.union([z.number().int(), z.string()]).nullable(),
  compatible: z.boolean(),
  applicable: z.boolean(),
  candidate_cardinality: z.number().int().nonnegative().nullable(),
  reference_cardinality: z.number().int().nonnegative().nullable(),
  jaccard: z.number().min(0).max(1).nullable(),
  containment: z.number().min(0).max(1).nullable(),
  above_threshold: z.boolean(),
  decision_role: z.string(),
})

export const availableCopyReviewComparisonSchema = z.object({
  availability: z.literal('available'),
  bulk_eligible: z.boolean(),
  algorithm_version: z.string(),
  lexical_fingerprint_version: z.number().int(),
  normalized_source_fingerprint_version: z.string(),
  prompt_fingerprint_version: z.string(),
  canonical_reference_revision: z.string(),
  reference_corpus_id: z.string(),
  reference_exclusion_mode: z.string(),
  miner_exclusion_mode: z.string(),
  same_miner_excluded: z.boolean(),
  chronology_direction: z.string(),
  chronology_eligible: z.boolean(),
  exact_byte_match: z.boolean(),
  normalized_source_match: z.boolean(),
  lexical: copyReviewSimilaritySchema,
  structural: copyReviewSimilaritySchema,
  prompt: copyReviewSimilaritySchema,
  triggered: z.boolean(),
  triggered_signal: z.string().nullable(),
  current_decision: z.string(),
})

export const unavailableCopyReviewComparisonSchema = z.object({
  availability: z.literal('unavailable'),
  bulk_eligible: z.literal(false),
  reason: z.string(),
})

export const copyReviewCurrentComparisonSchema = z.discriminatedUnion('availability', [
  availableCopyReviewComparisonSchema,
  unavailableCopyReviewComparisonSchema,
])

export const screenReviewAuditSchema = z.object({
  stage: z.enum(['l1', 'l2']),
  reason_code: z.string(),
  prompt_revision: z.string(),
  harness_revision: z.string().nullish().default(null),
  max_steps: z.number().int().positive(),
  steps_used: z.number().int().nonnegative(),
  max_read_bytes: z.number().int().positive().nullish().default(null),
  read_bytes_used: z.number().int().nonnegative().nullish().default(null),
  max_input_tokens: z.number().int().positive().nullish().default(null),
  input_tokens_used: z.number().int().nonnegative().nullish().default(null),
  max_output_tokens: z.number().int().positive().nullish().default(null),
  output_tokens_used: z.number().int().nonnegative().nullish().default(null),
  max_cost_usd: z.number().positive().nullish().default(null),
  cost_usd_used: z.number().nonnegative().nullish().default(null),
})

export const deferredReviewEvidenceSchema = z.object({
  mode: z.enum(['observe', 'enforce']),
  triggers: z.array(z.enum([
    'top_five',
    'composite_anomaly',
    'tool_anomaly',
    'memory_anomaly',
  ])),
  rank: z.number().int().nullish().default(null),
  cohort_size: z.number().int().nonnegative(),
  peer_count: z.number().int().nonnegative(),
  candidate: z.record(z.string(), z.number()),
  thresholds: z.record(z.string(), z.record(z.string(), z.number())).nullish().default(null),
  screening_attempt_id: z.string().uuid().nullish().default(null),
  screening_reason_code: z.string().nullish().default(null),
  review_audit: screenReviewAuditSchema.nullish().default(null),
  review_audit_digest: z.string().regex(/^[0-9a-f]{64}$/).nullish().default(null),
})

export const unavailableCopyReviewComparison = (reason: string) =>
  copyReviewCurrentComparisonSchema.parse({
    availability: 'unavailable',
    bulk_eligible: false,
    reason,
  })

// One definition of the ATH review kinds. `copyReviewOriginalSchema` used to
// restate all four inline, which is how a newly-added kind reaches the console
// as an unhandled value in some paths and a valid one in others.
export const athReviewKindSchema = z.enum([
  'copy',
  'benchmark_overfit',
  'deferred_source_review',
  'anomalous_score',
])

export type AthReviewKind = z.infer<typeof athReviewKindSchema>

export const copyReviewOriginalSchema = z.object({
  review_kind: athReviewKindSchema.default('copy'),
  duplicate_of: z.string().uuid().nullable(),
  reason: z.string().nullable(),
  policy_version: z.number().int(),
  fingerprint_versions: z.record(
    z.string(),
    z.union([z.number().int(), z.string(), z.null()]),
  ),
  reference_provenance: z.string(),
  backfilled: z.boolean(),
  // Identity of the matched submission (platform #162). Nullish defaults keep
  // the console working against a platform that predates the identity fields.
  duplicate_of_name: z.string().nullish().default(null),
  duplicate_of_version: z.number().int().nullish().default(null),
  duplicate_of_hotkey: z.string().nullish().default(null),
  // Payment-time coldkey of the matched submission. Compare against the held
  // agent's miner_coldkey to see whether both were paid for from the same
  // coldkey. A match is one signal of common control; a mismatch is not
  // evidence of different operators.
  duplicate_of_coldkey: z.string().nullish().default(null),
  duplicate_of_submitted_at: z.string().nullish().default(null),
  // Public-safe score trigger and terminal screener evidence only. Source,
  // prompts, responses and private rules never enter this admin projection.
  deferred_review: deferredReviewEvidenceSchema.nullish().default(null),
})

export const copyReviewItemSchema = z.object({
  review_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  // Payment-time coldkey from the platform's evaluation_payments ledger, i.e.
  // who paid for this evaluation. Not on-chain metagraph ownership, and null
  // when no payment row exists (unknown, not "no coldkey"). Nullish-tolerant so
  // Backroom keeps working against a platform that predates the field.
  miner_coldkey: z.string().nullish().default(null),
  agent_name: z.string(),
  agent_version: z.number().int().nullish().default(null),
  submitted_at: z.string(),
  status: z.enum(['pending', 'resolved']),
  // Live agents.status for the held agent. A pending review whose agent reads
  // anything other than ath_pending_review is a stranded hold rather than a
  // queue entry, and resolve 409s on it. Nullish-tolerant for a platform that
  // predates the field.
  agent_status: z.string().nullish().default(null),
  opened_at: z.string(),
  resolved_at: z.string().nullable(),
  resolved_by: z.string().nullable(),
  resolution: copyReviewResolutionSchema.nullable(),
  resolution_reason: z.string().nullable(),
  original: copyReviewOriginalSchema,
  // Embedded by platforms with #163 when the list is requested with
  // include=current_comparison; null from older platforms (fan-out fallback).
  current_comparison: copyReviewCurrentComparisonSchema.nullish().default(null),
})

export const copyReviewListSchema = z.object({
  items: z.array(copyReviewItemSchema),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  review_kind: athReviewKindSchema.nullish().default(null),
  generation: z.enum(['active', 'rollout', 'history', 'all']),
  active_bench_version: z.number().int().positive(),
  rollout_bench_version: z.number().int().positive().nullable().default(null),
})

/**
 * The operator ATH review queue.
 *
 * Only `reviewKind` is exposed. The platform list also takes `status` and
 * `generation`, and neither belongs on a queue:
 *
 * - `status` — a queue is unresolved work by definition. A "queue" that can
 *   return resolved rows just invites reading a closed hold as an open one;
 *   `get_ath_review` serves a resolved review with its full audit trail.
 * - `generation` — it selects reviews by whether the held agent has a score at
 *   a given benchmark version, which is a scoring-cohort question, not a queue
 *   question. Its `active` default is exactly how the console can show an empty
 *   list while holds are waiting: a copy hold opened at upload has no scores at
 *   all, and a hold that survived a benchmark rollout has none at the new
 *   active version. Both are still waiting for an operator. Pinned to `all`.
 */
export const athReviewQueueInputSchema = z.object({
  reviewKind: athReviewKindSchema.optional(),
})

export const copyReviewGenerationSchema = z.enum(['active', 'rollout', 'history'])
export const listCopyReviewsInputSchema = z.object({
  generation: copyReviewGenerationSchema.default('active'),
})

export const copyReviewConsoleItemSchema = copyReviewItemSchema.extend({
  current_comparison: copyReviewCurrentComparisonSchema,
})

export const copyReviewConsoleListSchema = z.object({
  items: z.array(copyReviewConsoleItemSchema),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  bulk_eligible_count: z.number().int().nonnegative(),
  generation: z.enum(['active', 'rollout', 'history', 'all']),
  active_bench_version: z.number().int().positive(),
  rollout_bench_version: z.number().int().positive().nullable(),
})

export const resolveCopyReviewInputSchema = z.object({
  agentId: z.string().uuid(),
  resolution: copyReviewResolutionSchema,
  reason: auditReasonSchema(3),
})

export const resolveCopyReviewResponseSchema = z.object({
  review: copyReviewItemSchema,
  agent_status: z.string(),
  idempotent: z.boolean(),
})

export const getAthReviewInputSchema = z.object({
  agentId: z.string().uuid(),
})

/**
 * Search decided ATH reviews as case law.
 *
 * The queue (`get_screening_review_queue`) is unresolved work. This surface
 * is the reporter: resolved `clear` / `reject` reasons an operator can cite.
 * `status` and `generation` stay pinned — a "precedent" that can return an
 * open hold is a queue row in disguise, and scoring-cohort filters hide
 * holdings that survived a bench rollout.
 */
export const searchAthPrecedentsInputSchema = z.object({
  query: z.string().trim().min(2).max(200).optional(),
  resolution: copyReviewResolutionSchema.optional(),
  reviewKind: athReviewKindSchema.optional(),
})

export const athPrecedentItemSchema = z.object({
  review_id: z.string().uuid(),
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().nullable().default(null),
  miner_hotkey: z.string(),
  status: z.enum(['pending', 'resolved']),
  resolution: copyReviewResolutionSchema.nullable(),
  resolution_reason: z.string().nullable(),
  original_reason: z.string().nullable(),
  review_kind: athReviewKindSchema,
  opened_at: z.string(),
  resolved_at: z.string().nullable(),
  resolved_by: z.string().nullable(),
})

export const athPrecedentListSchema = z.object({
  items: z.array(athPrecedentItemSchema),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  q: z.string().nullable().default(null),
  resolution: z.enum(['clear', 'reject', 'all']),
  review_kind: athReviewKindSchema.nullable().default(null),
  status: z.enum(['resolved', 'all']),
})

export const athReviewAuditSchema = z.object({
  review: copyReviewItemSchema,
  agent_status: z.string(),
  held_artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  held_score_count: z.number().int().nonnegative().nullable(),
  previous_status: z.string().nullable(),
  opened_by: z.string().nullable(),
  action_history: z.array(z.object({
    action: z.enum(['reopen', 'clear', 'reject']),
    reason: z.string(),
    actor: z.string(),
    created_at: z.string(),
    previous_status: z.string().nullable(),
    artifact_sha256: z.string().nullable(),
    score_count: z.number().int().nonnegative().nullable(),
  })).default([]),
})

export const openAthReviewInputSchema = z.object({
  agentId: z.string().uuid(),
  expectedSha256: z.string().regex(/^[0-9a-f]{64}$/),
  expectedScoreCount: z.number().int().nonnegative(),
  reason: auditReasonSchema(3),
})

export const openAthReviewResponseSchema = z.object({
  review: copyReviewItemSchema,
  agent_status: z.string(),
  idempotent: z.boolean(),
  // Defaults false while the platform API rollout catches up.
  reopened: z.boolean().default(false),
})

// --- Copy-review source diff (platform #170) ------------------------------
// Per-file diff between a held agent and the agent it was matched against, so
// an operator can see which files were copied verbatim vs. altered inline.

export const sourceDiffInputSchema = z.object({ agentId: z.string().uuid() })

export const sourceDiffFileSchema = z.object({
  path: z.string(),
  status: z.enum(['added', 'removed', 'modified', 'identical', 'renamed']),
  candidate_lines: z.number().int().nonnegative(),
  reference_lines: z.number().int().nonnegative(),
  added_lines: z.number().int().nonnegative(),
  removed_lines: z.number().int().nonnegative(),
  similarity: z.number().min(0).max(1),
  normalized_identical: z.boolean(),
  from_path: z.string().nullish(),
  to_path: z.string().nullish(),
})

export const sourceDiffManifestSchema = z.object({
  agent_id: z.string().uuid(),
  reference_agent_id: z.string().uuid(),
  candidate_sha256: z.string(),
  reference_sha256: z.string(),
  files: z.array(sourceDiffFileSchema),
  file_count: z.number().int().nonnegative(),
  identical_count: z.number().int().nonnegative(),
  modified_count: z.number().int().nonnegative(),
  added_count: z.number().int().nonnegative(),
  removed_count: z.number().int().nonnegative(),
  renamed_count: z.number().int().nonnegative().default(0),
  truncated: z.boolean(),
})

export const sourceDiffFileInputSchema = z.object({
  agentId: z.string().uuid(),
  path: z.string().min(1).max(240),
})

export const sourceDiffFileDetailSchema = z.object({
  agent_id: z.string().uuid(),
  reference_agent_id: z.string().uuid(),
  path: z.string(),
  candidate_present: z.boolean(),
  reference_present: z.boolean(),
  identical: z.boolean(),
  diff_lines: z.array(z.string()),
  truncated: z.boolean(),
  from_path: z.string().nullish(),
  to_path: z.string().nullish(),
})

// --- Starter-kit baseline diff -------------------------------------------
// Every submission descends from the official starter kit, so most of a
// quarantined crate is code the miner never wrote. This diffs a submission
// against the pinned kit so review starts from the miner's own work.

export const baselineDiffInputSchema = z.object({ agentId: z.string().uuid() })

export const starterKitProvenanceSchema = z.object({
  source: z.string(),
  revision: z.string(),
  commit_set_sha256: z.string(),
  commit_count: z.number().int().nonnegative(),
})

export const baselineDiffFileSchema = z.object({
  path: z.string(),
  status: z.enum(['added', 'removed', 'modified', 'identical']),
  candidate_lines: z.number().int().nonnegative(),
  reference_lines: z.number().int().nonnegative(),
  added_lines: z.number().int().nonnegative(),
  removed_lines: z.number().int().nonnegative(),
  similarity: z.number().min(0).max(1),
  normalized_identical: z.boolean(),
  // Broader than status === 'identical': true when the content is kit code at
  // any revision, so a miner who forked an older commit is not credited with
  // authoring it.
  stock_kit: z.boolean(),
})

export const baselineDiffManifestSchema = z.object({
  agent_id: z.string().uuid(),
  artifact_sha256: z.string(),
  baseline: starterKitProvenanceSchema,
  files: z.array(baselineDiffFileSchema),
  file_count: z.number().int().nonnegative(),
  identical_count: z.number().int().nonnegative(),
  modified_count: z.number().int().nonnegative(),
  added_count: z.number().int().nonnegative(),
  removed_count: z.number().int().nonnegative(),
  stock_kit_count: z.number().int().nonnegative(),
  custom_file_count: z.number().int().nonnegative(),
  custom_added_lines: z.number().int().nonnegative(),
  path_aligned: z.boolean(),
  truncated: z.boolean(),
})

export const baselineDiffFileInputSchema = z.object({
  agentId: z.string().uuid(),
  path: z.string().min(1).max(240),
})

export const baselineDiffFileDetailSchema = z.object({
  agent_id: z.string().uuid(),
  path: z.string(),
  candidate_present: z.boolean(),
  reference_present: z.boolean(),
  identical: z.boolean(),
  stock_kit: z.boolean(),
  diff_lines: z.array(z.string()),
  truncated: z.boolean(),
})

// --- Owner-link attestation -------------------------------------------------
//
// A SIGNED, SYMMETRIC link between two hotkeys saying one operator holds both.
// There is no direction: the pair is stored sorted (`hotkey_lo`/`hotkey_hi`)
// and each endpoint proves its own half, either with that hotkey's own key or
// with the coldkey bound to it by payment records. `counterparty` is simply
// the other hotkey relative to the one asked about.
//
// A signature is a stronger ownership signal than the payment-coldkey
// inference sharing the same review panel: a shared coldkey says the same
// wallet paid, a signature says the key holder signed. `evidence_grade` grades
// how much of the proof came from hotkeys rather than coldkeys, but it is
// REVIEWER CONTEXT ONLY — it does not gate the exemption, and screening treats
// all three grades identically. Do not build a threshold on it.
//
// The link is also narrow: it exempts near-duplicate plagiarism screening
// between the two hotkeys' submissions and nothing else. It does not affect
// emission-slot allocation, which stays partitioned by payment-time coldkey.
// And it is NOT transitive — only direct links are reported, so a hotkey
// linked to a hotkey linked to this one is legitimately absent.
//
// Nullish-tolerant on everything the platform may not report yet, so a
// Backroom deployed ahead of the platform reads a missing field as *unknown*
// rather than failing the parse and taking the review surface down with it.
// Identity fields — the hotkeys and the attestation id — stay strict: a link
// we cannot name both ends of is not a link a reviewer can act on.

export const ownerAttestationLookupInputSchema = z.object({
  hotkey: z.string().trim().min(3).max(96),
})

/**
 * How much of the two-sided proof came from hotkeys rather than payment-bound
 * coldkeys. Context for the reviewer, never a gate: all three grades establish
 * the link identically as far as screening is concerned.
 */
export const attestationEvidenceGradeSchema = z.enum([
  'coldkey-coldkey',
  'mixed',
  'hotkey-hotkey',
])

/** Which key each endpoint signed its half with. */
export const attestationKeyKindSchema = z.enum(['hotkey', 'coldkey'])

export const ownerAttestationSchema = z.object({
  attestation_id: z.string(),
  netuid: z.number().int().nonnegative().nullish().default(null),
  // The pair, stored sorted rather than as old/new: the link is symmetric and
  // carries no claim about which hotkey came first.
  hotkey_lo: z.string(),
  hotkey_hi: z.string(),
  // The other end, relative to the hotkey that was queried.
  counterparty: z.string(),
  evidence_grade: attestationEvidenceGradeSchema.nullish().default(null),
  lo_key_kind: attestationKeyKindSchema.nullish().default(null),
  lo_signer: z.string().nullish().default(null),
  hi_key_kind: attestationKeyKindSchema.nullish().default(null),
  hi_signer: z.string().nullish().default(null),
  nonce: z.string().nullish().default(null),
  issued_at: z.string().nullish().default(null),
  created_at: z.string().nullish().default(null),
  // Revoked links stay in the response on purpose. Disputes turn on whether a
  // link was live at the time of the submission under review, which a caller
  // cannot reconstruct from a filtered list.
  revoked_at: z.string().nullish().default(null),
  revoked_by: z.string().nullish().default(null),
  revoked_reason: z.string().nullish().default(null),
  // The platform's own summary flag for "does this count right now".
  // `revoked_at` stays authoritative for "was this link live then": a platform
  // predating `active` reports null here, which is unknown rather than
  // inactive.
  active: z.boolean().nullish().default(null),
})

/**
 * A hotkey proven to be the same operator as the one queried, with the link
 * that proves it. Currently-active links only, and direct links only — the
 * relation is not transitive, so this is never a closure.
 */
export const linkedHotkeySchema = z.object({
  hotkey: z.string(),
  attestation_id: z.string(),
  evidence_grade: attestationEvidenceGradeSchema.nullish().default(null),
})

export const ownerAttestationsSchema = z.object({
  hotkey: z.string(),
  netuid: z.number().int().nonnegative().nullish().default(null),
  // An unknown hotkey is an empty list, not an error: "this miner has never
  // attested a link" is a real and common answer, and the one that matters
  // most when a miner claims otherwise.
  attestations: z.array(ownerAttestationSchema).nullish().default([]),
  linked_hotkeys: z.array(linkedHotkeySchema).nullish().default([]),
  // Distinguishes a signature from the payment-record inference exposed
  // elsewhere in the same review surface.
  linkage_basis: z
    .literal('signed_owner_attestation')
    .nullish()
    .default('signed_owner_attestation'),
  // The platform's own sentence on what the link does and does not buy.
  scope_caveat: z.string().nullish().default(null),
})

export type AttestationEvidenceGrade = z.infer<typeof attestationEvidenceGradeSchema>
export type OwnerAttestation = z.infer<typeof ownerAttestationSchema>
export type LinkedHotkey = z.infer<typeof linkedHotkeySchema>
export type OwnerAttestations = z.infer<typeof ownerAttestationsSchema>

export type ScreeningQuarantine = z.infer<typeof screeningQuarantineSchema>
export type QuarantineResolution = z.infer<typeof quarantineResolutionSchema>
export type ScreeningQuarantineBatchDecision = z.infer<
  typeof screeningQuarantineBatchDecisionSchema
>
export type ScreeningQuarantineBatchPreview = z.infer<
  typeof screeningQuarantineBatchPreviewResponseSchema
>
export type ScreeningDispute = z.infer<typeof screeningDisputeSchema>
export type ScreeningDisputeResolution = z.infer<typeof screeningDisputeResolutionSchema>
export type ScreeningSubmission = z.infer<typeof screeningSubmissionSchema>
export type ScreeningEvidenceItem = z.infer<typeof screeningEvidenceItemSchema>
export type SourceReviewFinding = z.infer<typeof sourceReviewFindingSchema>
export type ScreeningQuarantineContext = z.infer<typeof screeningQuarantineContextSchema>
export type ShadowReviewObservation = z.infer<typeof shadowReviewObservationSchema>
export type SourceListing = z.infer<typeof sourceListingSchema>
export type SourceExcerpt = z.infer<typeof sourceExcerptSchema>
export type SourceSearchResult = z.infer<typeof sourceSearchResultSchema>
export type CopyReviewItem = z.infer<typeof copyReviewItemSchema>
export type CopyReviewGeneration = z.infer<typeof copyReviewGenerationSchema>
export type CopyReviewConsoleItem = z.infer<typeof copyReviewConsoleItemSchema>
export type CopyReviewCurrentComparison = z.infer<typeof copyReviewCurrentComparisonSchema>
export type CopyReviewResolution = z.infer<typeof copyReviewResolutionSchema>
export type AthReviewAudit = z.infer<typeof athReviewAuditSchema>
export type OpenAthReviewInput = z.infer<typeof openAthReviewInputSchema>
export type AthPrecedentList = z.infer<typeof athPrecedentListSchema>
export type CopyReviewList = z.infer<typeof copyReviewConsoleListSchema>
export type SourceDiffFile = z.infer<typeof sourceDiffFileSchema>
export type SourceDiffManifest = z.infer<typeof sourceDiffManifestSchema>
export type SourceDiffFileDetail = z.infer<typeof sourceDiffFileDetailSchema>
export type StarterKitProvenance = z.infer<typeof starterKitProvenanceSchema>
export type BaselineDiffFile = z.infer<typeof baselineDiffFileSchema>
export type BaselineDiffManifest = z.infer<typeof baselineDiffManifestSchema>
export type BaselineDiffFileDetail = z.infer<typeof baselineDiffFileDetailSchema>
export type ValidatorAssignment = z.infer<typeof validatorAssignmentSchema>
export type ValidationRetryDetail = z.infer<typeof validationRetryDetailSchema>
export type StuckSubmissionState = z.infer<typeof stuckSubmissionStateSchema>
export type StuckSubmission = z.infer<typeof stuckSubmissionSchema>
export type StuckSubmissionsList = z.infer<typeof stuckSubmissionsListSchema>
export type LeaseRevocation = z.infer<typeof leaseRevocationSchema>
export type LeaseRevocationsList = z.infer<typeof leaseRevocationsListSchema>
export type BatchRetryValidationResponse = z.infer<
  typeof batchRetryValidationResponseSchema
>
export type AgentScoringReadiness = z.infer<typeof agentScoringReadinessSchema>
export type AgentCodingCertificationStatus = z.infer<
  typeof agentCodingCertificationStatusSchema
>
export type BenchmarkContractRefreshDetail = z.infer<
  typeof benchmarkContractRefreshDetailSchema
>
export type ScreenedImageRebuildDetail = z.infer<
  typeof screenedImageRebuildDetailSchema
>
export type BenchmarkContractMigrationDetail = z.infer<
  typeof benchmarkContractMigrationDetailSchema
>
export type BenchmarkRolloutQualificationDetail = z.infer<
  typeof benchmarkRolloutQualificationDetailSchema
>
export type BenchmarkRolloutState = z.infer<typeof benchmarkRolloutStateSchema>
export type BenchmarkRolloutControl = z.infer<typeof benchmarkRolloutControlSchema>

// --- Production score reads (public score ledger) -------------------------
//
// These schemas parse the platform score ledger. Rank and composite still
// match /api/v1/public/leaderboard and /api/v1/public/agent/{id}/scores;
// operator name reads go through /api/v1/admin/leaderboard so reserved-handle
// collisions stay visible. They deliberately pick the aggregate fields an
// operator review needs and drop the heavyweight per-case payloads
// (case_results, per_category, token_usage) so MCP responses stay compact;
// zod strips the unlisted keys.

export const agentScoresLookupInputSchema = z
  .object({
    agentId: z.string().uuid().optional(),
    minerHotkey: z.string().trim().min(1).max(64).optional(),
  })
  .superRefine((input, context) => {
    if (Boolean(input.agentId) === Boolean(input.minerHotkey)) {
      context.addIssue({
        code: 'custom',
        path: ['agentId'],
        message: 'Provide exactly one of agentId or minerHotkey',
      })
    }
  })

export const scoreLeaderboardInputSchema = z.object({
  benchVersion: z.number().int().positive().optional(),
  status: z.enum(['all', 'finalized', 'provisional']).default('all'),
  limit: z.number().int().min(1).max(100).default(25),
  offset: z.number().int().min(0).default(0),
})

// The platform intentionally publishes ONE combined benchmark-quality
// multiplier: individual integrity/behaviour gates stay scorer-owned, so the
// arithmetic is transparent without leaking answer-key material.
export const publicCompositeBreakdownSchema = z.object({
  formula: z.string(),
  tool_weight: z.number().min(0).max(1),
  memory_weight: z.number().min(0).max(1),
  base_accuracy: z.number().min(0).max(1),
  benchmark_quality_multiplier: z.number().min(0).max(1),
  pre_token_composite: z.number().min(0).max(1),
  token_efficiency_multiplier: z.number().min(0).max(1).nullable().optional(),
  token_penalty: z.number().min(0).max(1).nullable().optional(),
  maximum_token_penalty: z.number().min(0).max(1).nullable().optional(),
  final_composite: z.number().min(0).max(1),
})

export const publicLeaderboardEntrySchema = z.object({
  // Rank is only meaningful for eligible entries; provisional rows trail the
  // finalized board by construction.
  //
  // Null is a real, routine value rather than a degenerate one: Bench v9
  // base-only and provisional rows carry `rank: null` in confirmation enforce
  // mode, because only full-confirmed rows rank. A required `z.number()` here
  // fails the WHOLE `entries` array, so a single unranked provisional row made
  // the entire leaderboard unreadable through Backroom rather than costing that
  // one row its rank. Nullish (not merely nullable) because the Platform field
  // carries a default and is therefore optional in the OpenAPI schema.
  rank: z.number().int().positive().nullish().default(null),
  finalized: z.boolean(),
  score_count: z.number().int().nonnegative(),
  score_quorum: z.number().int().positive(),
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().positive().nullable().optional(),
  miner_hotkey: z.string(),
  miner_uid: z.number().int().nonnegative().nullable().optional(),
  registered: z.boolean().nullable().optional(),
  emission_eligible: z.boolean().nullable().optional(),
  composite: z.number().min(0).max(1),
  raw_composite: z.number().min(0).max(1).nullable().optional(),
  composite_stderr: z.number().nonnegative().nullable().optional(),
  settled_composite: z.number().min(0).max(1).nullable().optional(),
  rollout_composite: z.number().min(0).max(1).nullable().optional(),
  rollout_score_count: z.number().int().nonnegative().nullable().optional(),
  tool_mean: z.number().min(0).max(1),
  memory_mean: z.number().min(0).max(1),
  first_seen: z.string(),
  median_ms: z.number().int().nonnegative().nullable().optional(),
  n: z.number().int().nonnegative().nullable().optional(),
  eligible: z.boolean(),
  bench_version: z.number().int().nullable().optional(),
  dataset_sha256: z.string().nullable().optional(),
  composite_breakdown: publicCompositeBreakdownSchema.nullable().optional(),
  history: z.array(z.number()).nullable().optional(),
})

export const publicDethroneDecisionSchema = z.object({
  challenger_lead: z.number(),
  required_lead: z.number().nonnegative(),
  margin_lead: z.number().nonnegative(),
  statistical_lead: z.number().nonnegative().nullable().optional(),
  method: z.enum(['flat', 'unpaired', 'paired']),
  dethrones: z.boolean(),
  paired_standard_error: z.number().nonnegative().nullable().optional(),
  shared_seed_count: z.number().int().nonnegative().nullable().optional(),
  seed_differences: z.array(z.number()).nullable().optional(),
})

export const publicEmissionRecipientSchema = z.object({
  role: z.enum(['champion', 'tail']),
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  raw_rank: z.number().int().positive(),
  share_of_miner_pool: z.number().positive().max(1),
  // Confirmation-seed depth: distinct champion-anchored CRN seeds this top-5
  // agent has been re-scored on by the continual rescore lane.
  shared_seed_confirmations: z.number().int().nonnegative().default(0),
})

export const publicKothEmissionsSchema = z.object({
  margin: z.number().min(0).max(1),
  dethrone_z: z.number().nonnegative(),
  band_decay_min_bench_version: z.number().int().positive(),
  band_decay_start_composite: z.number().min(0).max(1),
  band_decay_rate: z.number().positive(),
  // Protocol 24. Without these a replay of the fold from this payload
  // reconstructs the uncapped band and reports a dethrone requirement the
  // validators are not actually enforcing.
  ceiling_headroom_share: z.number().positive().max(1).optional(),
  ceiling_band_clamp_active: z.boolean().optional(),
  ceiling_band_clamp_required_protocol: z.number().int().positive().optional(),
  champion_share: z.number().positive().max(1),
  rank_shares: z.array(z.number().positive().max(1)),
  tail_size: z.number().int().nonnegative(),
  champion_agent_id: z.string().uuid(),
  champion_miner_hotkey: z.string(),
  raw_leader_agent_id: z.string().uuid(),
  raw_leader_miner_hotkey: z.string(),
  raw_leader_decision: publicDethroneDecisionSchema.nullable().optional(),
  recipients: z.array(publicEmissionRecipientSchema),
})

export const publicLeaderboardSchema = z.object({
  generated_at: z.string(),
  count: z.number().int().nonnegative(),
  current_bench_version: z.number().int().positive(),
  active_bench_version: z.number().int().positive(),
  desired_bench_version: z.number().int().positive(),
  available_bench_versions: z.array(z.number().int().positive()),
  selection_mode: z.enum(['authoritative', 'historical']),
  entries: z.array(publicLeaderboardEntrySchema),
  emissions: publicKothEmissionsSchema.nullable().optional(),
})

export const scoreLeaderboardPageSchema = z.object({
  generated_at: z.string(),
  current_bench_version: z.number().int().positive(),
  active_bench_version: z.number().int().positive(),
  desired_bench_version: z.number().int().positive(),
  available_bench_versions: z.array(z.number().int().positive()),
  selection_mode: z.enum(['authoritative', 'historical']),
  status: z.enum(['all', 'finalized', 'provisional']),
  count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  entries: z.array(publicLeaderboardEntrySchema),
  emissions: publicKothEmissionsSchema.nullable(),
})

/**
 * A DittoBench dataset seed, as an exact decimal string.
 *
 * Seeds are 64-bit identifiers, not arithmetic operands: nothing in Backroom
 * adds, averages, or compares them for magnitude, it only groups, dedupes, and
 * displays them. Real production seeds (for example `989366151180340909`)
 * exceed `Number.MAX_SAFE_INTEGER`, so a JavaScript number cannot hold one
 * exactly and `z.number().int()` rejected every real response outright.
 *
 * A string is the only representation that round-trips byte-exactly through
 * `JSON.stringify` into an MCP tool result and on into a published
 * reproduction command. `bigint` would satisfy the precision requirement but
 * throws on `JSON.stringify` without a custom replacer.
 *
 * This also matches the platform's own precedent: the pipeline endpoint already
 * declares `PublicProvisionalScore.seed` / `PublicConfirmationScore.seed` as
 * `str` with pattern `^\d+$` and the comment "Encoded as a string to avoid
 * JavaScript integer rounding". The score endpoints Backroom reads simply never
 * got the same treatment.
 *
 * Platform seeds are non-negative (`derive_seed` masks to 63 bits), but the
 * column is a signed `BigInteger`, so a leading `-` is accepted rather than
 * hard-failing an operator read on a hypothetical legacy row.
 *
 * The number branch accepts small seeds (legacy rows, fixtures) and keeps
 * `.int()`, which in Zod 4 rejects anything outside the safe range. That is
 * intentional: `parseJsonPreservingLargeIntegers` delivers every 64-bit seed as
 * a string, so an out-of-range *number* reaching here means the value was
 * already rounded by a plain `JSON.parse` upstream. Failing loudly beats
 * emitting a corrupted seed. `Number()` is never applied to a seed anywhere.
 */
export const seedSchema = z
  .union([
    z.string().regex(/^-?(0|[1-9][0-9]*)$/, 'A seed must be an exact decimal integer'),
    z.number().int(),
  ])
  .transform((seed) => (typeof seed === 'string' ? seed : String(seed)))

export const publicValidatorScoreSchema = z.object({
  validator_hotkey: z.string(),
  composite: z.number().min(0).max(1),
  tool_mean: z.number().min(0).max(1),
  memory_mean: z.number().min(0).max(1),
  raw_composite: z.number().min(0).max(1).nullable().optional(),
  composite_breakdown: publicCompositeBreakdownSchema.nullable().optional(),
  median_ms: z.number().int().nonnegative(),
  n: z.number().int().nonnegative(),
  // Null identifies a legacy score recorded before benchmark versioning.
  bench_version: z.number().int().positive().nullable(),
  seed: seedSchema,
  run_id: z.string(),
  ticket_deadline: z.string().nullable().optional(),
  generated_at: z.string(),
  transform_robustness: z.number().min(0).max(1).nullable().optional(),
  audit_case_count: z.number().int().nonnegative().nullable().optional(),
  transcript_sha256: z.string().nullable().optional(),
})

export const publicAgentScoresSchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  status: z.string(),
  quorum: z.number().int().positive(),
  score_count: z.number().int().nonnegative(),
  median_composite: z.number().min(0).max(1).nullable(),
  dataset_seed: seedSchema.nullable(),
  dataset_sha256: z.string().nullable(),
  dataset_run_size: z.string().nullable(),
  // A Bittensor block height, not a seed: it stays a number because it is
  // ordered and compared, and chain heights are nowhere near 2^53.
  dataset_seed_block: z.number().int().nullable().optional(),
  dataset_seed_block_hash: z.string().nullable().optional(),
  scores: z.array(publicValidatorScoreSchema),
  generated_at: z.string(),
})

/**
 * One accepted score toward a submission's quorum, before OR after it settles.
 *
 * The platform publishes an accepted score twice, on two surfaces with two
 * disclosure levels. `/agent/{id}/scores` serves the settled k=3 record in
 * full. `/agent/{id}/pipeline` serves the same scores while the submission is
 * still below quorum, but deliberately withholds validator identity, run ids,
 * signatures and the per-axis means: "Validator identity, signatures, ticket
 * leases, answer keys, and scorer internals remain outside the public
 * in-progress surface" (`PublicProvisionalScore`).
 *
 * So the fields below widen to null relative to
 * {@link publicValidatorScoreSchema}, which still parses the settled endpoint
 * strictly. A null here means the platform has not published that field YET —
 * never that no validator produced the score. Backroom must not close the gap
 * by pairing a provisional composite with a scored ticket's hotkey: that would
 * be an inferred attribution the ledger never made.
 */
export const agentScoreRowSchema = publicValidatorScoreSchema.extend({
  validator_hotkey: z.string().nullable(),
  run_id: z.string().nullable(),
  tool_mean: z.number().min(0).max(1).nullable(),
  memory_mean: z.number().min(0).max(1).nullable(),
  median_ms: z.number().int().nonnegative().nullable(),
  n: z.number().int().nonnegative().nullable(),
})

export const agentScoresDetailSchema = publicAgentScoresSchema.extend({
  // False while the submission is below quorum (or held for copy review): the
  // scores below are accepted and real, but `median_composite` is not yet the
  // canonical number and the submission holds no finalized rank. Same word,
  // same meaning as `finalized` on a leaderboard entry.
  finalized: z.boolean(),
  scores: z.array(agentScoreRowSchema),
  // Null on the pre-quorum surface, which is keyed by agent id and publishes
  // no miner hotkey; the leaderboard row below carries it when the submission
  // holds one.
  miner_hotkey: z.string().nullable(),
  active_bench_version: z.number().int().positive(),
  desired_bench_version: z.number().int().positive(),
  // Null when this exact submission is not the miner's current leaderboard
  // row (for example a superseded earlier upload): the k=3 record above is
  // still authoritative for the submission itself.
  leaderboard: publicLeaderboardEntrySchema.nullable(),
})

/**
 * The pre-quorum view of one submission (`/api/v1/public/agent/{id}/pipeline`).
 *
 * Only the scoring fields are declared; the screening history, dispute and
 * validator-lease detail on the same payload belong to other tools
 * (`list_stuck_submissions`, `agent_scoring_readiness`) and zod strips them.
 */
export const publicProvisionalScoreSchema = z.object({
  composite: z.number().min(0).max(1),
  raw_composite: z.number().min(0).max(1).nullable().optional(),
  composite_breakdown: publicCompositeBreakdownSchema.nullable().optional(),
  // Already an exact decimal string on this endpoint; seedSchema keeps the one
  // representation every score tool emits.
  seed: seedSchema,
  bench_version: z.number().int().positive().nullable().optional(),
  accepted_at: z.string(),
  transcript_sha256: z.string().nullable().optional(),
})

export const publicSubmissionPipelineSchema = z.object({
  agent_id: z.string().uuid(),
  status: z.string(),
  quorum: z.number().int().positive(),
  // Scoped to `score_bench_version`, not the active version: the platform
  // never mixes eras in this count and neither does the record built from it.
  score_count: z.number().int().nonnegative(),
  score_bench_version: z.number().int().positive(),
  // Null until quorum: "the canonical aggregate remains null until the
  // independent-score quorum is reached".
  final_composite: z.number().min(0).max(1).nullable().optional(),
  provisional_scores: z.array(publicProvisionalScoreSchema).default([]),
  generated_at: z.string(),
})

export const agentScoreHistoryVersionSchema = z.object({
  // Null groups legacy scores recorded before benchmark versioning.
  bench_version: z.number().int().positive().nullable(),
  score_count: z.number().int().positive(),
  median_composite: z.number().min(0).max(1),
  min_composite: z.number().min(0).max(1),
  max_composite: z.number().min(0).max(1),
  median_tool_mean: z.number().min(0).max(1),
  median_memory_mean: z.number().min(0).max(1),
  first_scored_at: z.string(),
  last_scored_at: z.string(),
  validators: z.array(z.string()),
  seeds: z.array(seedSchema),
  // Median-composite change against the previous listed version; null for
  // the first version group.
  composite_delta_vs_previous: z.number().nullable(),
})

export const agentScoreHistorySchema = z.object({
  agent_id: z.string().uuid(),
  miner_hotkey: z.string(),
  status: z.string(),
  quorum: z.number().int().positive(),
  total_score_count: z.number().int().nonnegative(),
  versions: z.array(agentScoreHistoryVersionSchema),
  generated_at: z.string(),
})

// --- Owner footprint -------------------------------------------------------
//
// "Who else does this operator control?" answered from the platform's
// evaluation_payments ledger. Coldkey linkage is a payment record, never an
// ownership determination: see the tool description and linkage_caveat.

export const ownerFootprintLookupInputSchema = z.object({
  key: z.string().trim().min(3).max(96),
  // One round is hotkey -> its payment coldkeys -> every other hotkey those
  // coldkeys paid for. Deeper rounds chain through shared coldkeys and
  // over-link quickly, so the default stays at the strongest evidence.
  depth: z.number().int().min(1).max(3).default(1),
  agentsPerHotkey: z.number().int().min(0).max(50).default(10),
})

export const ownerFootprintAgentSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().nullish().default(null),
  agent_status: z.string(),
  artifact_sha256: z.string(),
  submitted_at: z.string(),
  miner_coldkey: z.string().nullish().default(null),
})

export const ownerFootprintHotkeySchema = z.object({
  miner_hotkey: z.string(),
  miner_coldkeys: z.array(z.string()).default([]),
  // Payment-record edges from the key that was asked about: 0 is that key,
  // 1 shares a coldkey with it, higher hops are progressively weaker.
  link_hop: z.number().int().nonnegative(),
  submission_count: z.number().int().nonnegative(),
  // The gap against submission_count is the part of this hotkey's history no
  // coldkey can speak to (no payment row).
  paid_submission_count: z.number().int().nonnegative(),
  latest_submitted_at: z.string().nullish().default(null),
  agents: z.array(ownerFootprintAgentSchema).default([]),
  agents_truncated: z.boolean().default(false),
})

export const ownerFootprintSchema = z.object({
  identifier: z.string(),
  identifier_kind: z.enum(['miner_hotkey', 'miner_coldkey', 'both', 'unknown']),
  depth: z.number().int().positive(),
  miner_coldkeys: z.array(z.string()).default([]),
  hotkeys: z.array(ownerFootprintHotkeySchema).default([]),
  hotkey_count: z.number().int().nonnegative(),
  submission_count: z.number().int().nonnegative(),
  // False when the walk stopped at a ceiling with more linkage still
  // reachable, so a truncated set is never read as the whole footprint.
  expansion_complete: z.boolean(),
  ownership_basis: z.literal('evaluation_payment_records'),
  linkage_caveat: z.string(),
})

/** One linked hotkey, joined with its current public leaderboard standing. */
export const ownerFootprintHotkeyStandingSchema =
  ownerFootprintHotkeySchema.extend({
    // Null when the hotkey holds no row on the current authoritative board:
    // never scored, superseded, or held. Absence is not ineligibility.
    leaderboard: publicLeaderboardEntrySchema.nullable(),
  })

export const ownerFootprintDetailSchema = ownerFootprintSchema.extend({
  hotkeys: z.array(ownerFootprintHotkeyStandingSchema),
  // Board context for the standings joined above, so a reviewer knows which
  // benchmark era the ranks belong to.
  active_bench_version: z.number().int().positive(),
  desired_bench_version: z.number().int().positive(),
  leaderboard_generated_at: z.string(),
  // Count of linked hotkeys carrying a leaderboard row at all.
  ranked_hotkey_count: z.number().int().nonnegative(),
})

export type PublicLeaderboardEntry = z.infer<typeof publicLeaderboardEntrySchema>
export type ScoreLeaderboardPage = z.infer<typeof scoreLeaderboardPageSchema>
export type AgentScoresDetail = z.infer<typeof agentScoresDetailSchema>
export type PublicSubmissionPipeline = z.infer<typeof publicSubmissionPipelineSchema>
export type AgentScoreHistory = z.infer<typeof agentScoreHistorySchema>
export type OwnerFootprintDetail = z.infer<typeof ownerFootprintDetailSchema>

// --- Inference trace archive (private Hippius bucket ditto-subnet-traces) ---

export const traceObjectSchema = z.object({
  key: z.string(),
  size: z.number().int().nonnegative(),
  last_modified: z.string(),
  etag: z.string(),
})

export const traceObjectListSchema = z.object({
  bucket: z.string(),
  prefix: z.string(),
  objects: z.array(traceObjectSchema),
  continuation_token: z.string().nullable(),
})

export const listInferenceTracesInputSchema = z.object({
  scope: z.enum(['traces', 'ledger']).default('traces'),
  lane: z.enum(['inference', 'confirmation', 'screening']).optional(),
  kind: z.enum(['chat', 'embedding', 'kaniko', 'smoke', 'review']).optional(),
  dt: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/, 'dt is a UTC day like 2026-08-24')
    .optional(),
  hour: z
    .string()
    .regex(/^\d{2}$/, 'hour is a two-digit UTC hour like 17')
    .optional(),
  prefix: z.string().max(512).optional(),
  maxKeys: z.number().int().min(1).max(1000).default(200),
  continuationToken: z.string().max(2048).optional(),
})

export const traceDownloadUrlInputSchema = z.object({
  key: z.string().min(1).max(1024),
  expiresInSeconds: z.number().int().min(60).max(3600).default(300),
})

export const traceDownloadUrlSchema = z.object({
  bucket: z.string(),
  key: z.string(),
  url: z.string(),
  expires_in: z.number().int().positive(),
})

export const peekInferenceTraceInputSchema = z.object({
  key: z.string().min(1).max(1024),
  maxRecords: z.number().int().min(1).max(50).default(5),
  offsetRecords: z.number().int().min(0).max(100_000).default(0),
  includeBodies: z.boolean().default(false),
})

export const traceRecordSummarySchema = z.object({
  index: z.number().int().nonnegative(),
  recorded_at: z.string().nullish(),
  event: z.string().nullish(),
  lane: z.string().nullish(),
  kind: z.string().nullish(),
  run_id: z.string().nullish(),
  case_id: z.string().nullish(),
  grant_id: z.string().nullish(),
  nonce: z.string().nullish(),
  agent_id: z.string().nullish(),
  validator_hotkey: z.string().nullish(),
  bench_version: z.number().int().nullish(),
  status: z.string().nullish(),
  prompt_tokens: z.number().int().nullish(),
  completion_tokens: z.number().int().nullish(),
  provider: z.string().nullish(),
  latency_ms: z.number().int().nullish(),
  body_bytes: z.number().int().nullish(),
  record: z.record(z.string(), z.unknown()).nullish(),
  record_omitted: z.literal('too_large').nullish(),
})

export const tracePeekResponseSchema = z.object({
  bucket: z.string(),
  key: z.string(),
  records: z.array(traceRecordSummarySchema),
  records_scanned: z.number().int().nonnegative(),
  scan_complete: z.boolean(),
})

// Future screening-policy activation.
//
// The platform keeps the live policy in force and holds ONE pending activation
// that flips the policy version at a scheduled instant. Revisions are
// append-only: scheduling a new activation supersedes the pending one but the
// old rows stay readable as audit. `state` is computed by the platform at read
// time ("due" once now >= activate_at), so it is never stored and never
// defaulted here.
export const SCREENER_POLICY_ACTIVATION_CONFIRMATION = 'SCHEDULE SCREENER POLICY ACTIVATION'
export const RESTORE_SCORED_SCREENING_SNAPSHOT_CONFIRMATION =
  'RESTORE SCORED SCREENING SNAPSHOT'
export const MAX_SCREENER_POLICY_ACTIVATION_REVISIONS = 200

export const screenerPolicyActivationRevisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  parent_revision: z.number().int().nonnegative(),
  target_policy_version: z.number().int().positive(),
  activate_at: z.string(),
  rescreen_scored: z.boolean(),
  reason: z.string(),
  actor: z.string(),
  created_at: z.string(),
  state: z.enum(['pending', 'due']),
})

export const screenerPolicyActivationViewSchema = z.object({
  effective_policy_version: z.number().int().positive(),
  floor_policy_version: z.number().int().positive(),
  builtin_policy_version: z.number().int().positive(),
  // Null until the first activation has ever been scheduled.
  latest: screenerPolicyActivationRevisionSchema.nullable(),
  // Newest-first, bounded by the platform at MAX_SCREENER_POLICY_ACTIVATION_REVISIONS.
  revisions: z
    .array(screenerPolicyActivationRevisionSchema)
    .max(MAX_SCREENER_POLICY_ACTIVATION_REVISIONS),
})

// The offset requirement is what makes the schedule unambiguous: a naive
// "2026-08-29T09:00:00" would be the platform's UTC wall clock, not the
// operator's, and the 422 that catches it should never have to.
export const scheduleScreenerPolicyActivationInputSchema = z.object({
  expectedRevision: z.number().int().nonnegative(),
  // The platform bounds this to [floor..builtin] and answers 422 out of range;
  // it stays the authority, so this only rejects nonsense a positive int catches.
  targetPolicyVersion: z.number().int().positive(),
  activateAt: z.string().datetime({ offset: true }),
  rescreenScored: z.boolean().default(true),
  reason: auditReasonSchema(8),
  confirmation: z.literal(SCREENER_POLICY_ACTIVATION_CONFIRMATION),
})

export const restoreScoredScreeningSnapshotInputSchema = z.object({
  expectedCurrentActivationRevision: z.number().int().positive(),
  sourceActivationRevision: z.number().int().positive(),
  sourcePolicyVersion: z.number().int().positive(),
  targetPolicyVersion: z.number().int().positive(),
  benchVersion: z.number().int().positive(),
  expectedCount: z.number().int().min(1).max(500),
  reason: auditReasonSchema(8),
  confirmation: z.literal(RESTORE_SCORED_SCREENING_SNAPSHOT_CONFIRMATION),
})

export const restoreScoredScreeningSnapshotResponseSchema = z.object({
  batch_id: z.string().uuid(),
  restored_count: z.number().int().nonnegative(),
  source_activation_revision: z.number().int().positive(),
  current_activation_revision: z.number().int().positive(),
  source_policy_version: z.number().int().positive(),
  target_policy_version: z.number().int().positive(),
  bench_version: z.number().int().positive(),
  submissions: z.array(
    z.object({
      agent_id: z.string().uuid(),
      displaced_attempt_id: z.string().uuid(),
      restored_attempt_id: z.string().uuid(),
      restored_policy_version: z.number().int().positive(),
      score_count: z.number().int().min(3),
    }),
  ),
})

export type ScreenerPolicyActivationView = z.infer<typeof screenerPolicyActivationViewSchema>
