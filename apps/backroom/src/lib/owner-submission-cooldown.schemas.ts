import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// Operator per-owner submission cooldown overrides
// (/api/v1/admin/submission-settings/owner-cooldowns). An override requires a
// longer minimum interval between one owner identity's submissions (a payment
// coldkey or a hotkey) than the global cooldown, until it expires or is
// revoked. Revisions are append-only and compare-and-set per identity.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type Schemas = PlatformComponents['schemas']

const timestamp = () => z.string().datetime({ offset: true })

export const OWNER_COOLDOWN_MIN_SECONDS = 60
export const OWNER_COOLDOWN_MAX_SECONDS = 30 * 86_400
export const OWNER_COOLDOWN_MAX_DAYS = 90

const ownerScopeSchema = z.enum(['coldkey', 'hotkey'])
const ownerKeySchema = z
  .string()
  .trim()
  .regex(/^[1-9A-HJ-NP-Za-km-z]{32,64}$/, 'an SS58 coldkey or hotkey')

const ownerCooldownRevisionSchema = z.object({
  scope: ownerScopeSchema,
  owner_key: z.string(),
  revision: z.number().int(),
  action: z.enum(['set', 'revoke']),
  cooldown_seconds: z.number().int().nullable(),
  expires_at: timestamp().nullable(),
  reason: z.string(),
  actor: z.string(),
  created_at: timestamp(),
  state: z.enum(['active', 'expired', 'revoked']),
} satisfies PlatformResponseShape<Schemas['OwnerCooldownRevision']>)

export const ownerCooldownLookupSchema = z.object({
  scope: ownerScopeSchema,
  owner_key: z.string(),
  current: ownerCooldownRevisionSchema.nullable(),
  global_cooldown_seconds: z.number().int(),
  latest_submission_at: timestamp().nullable(),
  next_allowed_at: timestamp().nullable(),
  history: z.array(ownerCooldownRevisionSchema),
} satisfies PlatformResponseShape<Schemas['OwnerCooldownLookup']>)

export const ownerCooldownsSchema = z.object({
  active_count: z.number().int(),
  items: z.array(ownerCooldownRevisionSchema),
  truncated: z.boolean().default(false),
  lookup: ownerCooldownLookupSchema.nullable().default(null),
} satisfies PlatformResponseShape<Schemas['AdminOwnerCooldownsResponse']>)

export const ownerCooldownsInputSchema = z.object({
  scope: ownerScopeSchema.optional(),
  ownerKey: ownerKeySchema.optional(),
  includeInactive: z.boolean().default(false),
  limit: z.number().int().min(1).max(500).default(200),
})

export const setOwnerCooldownInputSchema = z.object({
  scope: ownerScopeSchema,
  ownerKey: ownerKeySchema,
  cooldownSeconds: z
    .number()
    .int()
    .min(OWNER_COOLDOWN_MIN_SECONDS)
    .max(OWNER_COOLDOWN_MAX_SECONDS),
  expiresInDays: z.number().int().min(1).max(OWNER_COOLDOWN_MAX_DAYS),
  expectedRevision: z.number().int().nonnegative(),
  reason: z.string().trim().min(8),
  confirmation: z.string(),
})

export const revokeOwnerCooldownInputSchema = z.object({
  scope: ownerScopeSchema,
  ownerKey: ownerKeySchema,
  expectedRevision: z.number().int().positive(),
  reason: z.string().trim().min(8),
  confirmation: z.string(),
})

/** The exact phrase Platform requires to set an owner cooldown. */
export function setOwnerCooldownConfirmation(input: {
  scope: string
  ownerKey: string
  cooldownSeconds: number
  expiresInDays: number
}) {
  return (
    `SET OWNER SUBMISSION COOLDOWN ${input.scope.toUpperCase()} ${input.ownerKey} `
    + `${input.cooldownSeconds} SECONDS FOR ${input.expiresInDays} DAYS`
  )
}

/** The exact phrase Platform requires to revoke an owner cooldown. */
export function revokeOwnerCooldownConfirmation(input: { scope: string; ownerKey: string }) {
  return `REVOKE OWNER SUBMISSION COOLDOWN ${input.scope.toUpperCase()} ${input.ownerKey}`
}

export type OwnerCooldowns = z.infer<typeof ownerCooldownsSchema>
export type OwnerCooldownLookup = z.infer<typeof ownerCooldownLookupSchema>
