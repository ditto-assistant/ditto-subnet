import { z } from 'zod'

const sha256 = z.string().regex(/^[0-9a-f]{64}$/)
const signature = z.string().regex(/^[0-9a-fA-F]{128}$/)
const keyKind = z.enum(['hotkey', 'coldkey'])
export const claModeSchema = z.enum(['off', 'shadow', 'enforce'])
export const contributorHotkeyInputSchema = z.object({ hotkey: z.string().min(1).max(128) })
export const githubAccountInputSchema = z.object({ githubUserId: z.number().int().positive() })

const githubLinkSchema = z.object({
  link_id: z.string().uuid(), netuid: z.number().int(), hotkey: z.string(), coldkey: z.string(),
  github_user_id: z.number().int().positive(), github_login: z.string(),
  link_attempt_id: z.string().uuid(), key_kind: keyKind, signer: z.string(), signature,
  nonce: z.string().uuid(), issued_at: z.string(), linked_at: z.string(), revoked_at: z.string().nullable(),
  owner_block: z.number().int(), owner_block_hash: z.string(),
})
export const githubLinkStatusSchema = z.object({ netuid: z.number().int(), enabled: z.boolean(), link: githubLinkSchema.nullable() })
export const githubRevocationReadInputSchema = z.object({ linkId: z.string().uuid() })
export const githubRevocationWriteInputSchema = githubRevocationReadInputSchema.extend({
  reason: z.string().min(8), confirmation: z.string().min(1).max(200),
})
export const githubRevocationControlSchema = z.object({
  link: githubLinkSchema,
  operator_revocation: z.object({ link_id: z.string().uuid(), actor: z.string(), reason: z.string(), revoked_at: z.string() }).nullable(),
  can_revoke: z.boolean(), blocking_reason: z.string().nullable(), confirmation: z.string(),
})
export const claVersionSchema = z.object({ version: z.number().int().positive(), sha256, published_at: z.string(), effective_at: z.string() })
export const claSignatureSchema = z.object({
  signature_id: z.string().uuid(), netuid: z.number().int(), hotkey: z.string(), coldkey: z.string(),
  cla_version: z.number().int().positive(), cla_sha256: sha256, nonce: z.string().uuid(),
  issued_at: z.string(), signature, signed_at: z.string(), owner_block: z.number().int(), owner_block_hash: z.string(),
})
export const claStatusSchema = z.object({ netuid: z.number().int(), mode: claModeSchema,
  current: claVersionSchema.nullable(), signature: claSignatureSchema.nullable(), is_current: z.boolean() })
export const claSettingsRevisionSchema = z.object({ revision: z.number().int().nonnegative(), parent_revision: z.number().int().nonnegative(),
  mode: claModeSchema, actor: z.string(), reason: z.string(), created_at: z.string() })
export const claControlSchema = z.object({
  current: claSettingsRevisionSchema, history: z.array(claSettingsRevisionSchema).max(100),
  effective_version: claVersionSchema.nullable(), active_hotkeys: z.number().int().nonnegative(),
  signed_active_hotkeys: z.number().int().nonnegative(), unsigned_active_hotkeys: z.number().int().nonnegative(),
  active_hotkeys_basis: z.literal('non_retired_submissions'), unsigned_shadow_uploads: z.number().int().nonnegative(),
  history_total: z.number().int().nonnegative(), history_limit: z.number().int().min(1).max(100),
  history_offset: z.number().int().nonnegative(), history_has_more: z.boolean(),
  minimum_enforced_notice_seconds: z.number().int().positive(),
  earliest_enforced_effective_at: z.string().nullable(),
})
export const claPolicyReadInputSchema = z.object({ historyLimit: z.number().int().min(1).max(100).default(25), historyOffset: z.number().int().nonnegative().default(0) })
export const claPolicyWriteInputSchema = z.object({ expectedRevision: z.number().int().nonnegative(), mode: claModeSchema,
  reason: z.string().min(8), confirmation: z.string().min(1).max(200) })
export const claPublishInputSchema = z.object({ version: z.number().int().min(1).max(2147483647), sha256,
  text: z.string().min(1).max(262144).refine((value) => new TextEncoder().encode(value).length <= 262144,
    'CLA text exceeds the 262144 UTF-8 byte limit'),
  effectiveAt: z.string().datetime({ offset: true }), reason: z.string().min(8),
  confirmation: z.string().min(1).max(200) })
