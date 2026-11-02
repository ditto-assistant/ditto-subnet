import { z } from 'zod'

const sha256 = z.string().regex(/^[0-9a-f]{64}$/)

export const artifactFetchAuditInputSchema = z.object({
  agentId: z.string().uuid().optional(),
  sha: sha256.optional(),
  before: z.string().datetime({ offset: true }).optional(),
  limit: z.number().int().min(1).max(1000).optional(),
}).refine((input) => (input.agentId === undefined) !== (input.sha === undefined), {
  message: 'Supply exactly one of agentId or sha',
})

const requesterKind = z.enum(['validator', 'screener', 'admin', 'public', 'miner'])

export const artifactFetchAuditSchema = z.object({
  agent_ids: z.array(z.string().uuid()),
  artifact_sha256: sha256.nullable(),
  before: z.string(),
  total: z.number().int().nonnegative(),
  truncated: z.boolean(),
  requesters: z.array(z.object({
    requester_kind: requesterKind,
    requester_id: z.string().nullable(),
    fetch_count: z.number().int().nonnegative(),
    source_fetch_count: z.number().int().nonnegative(),
    first_fetched_at: z.string(),
    last_fetched_at: z.string(),
  })),
  rows: z.array(z.object({
    fetched_at: z.string(),
    agent_id: z.string().uuid(),
    artifact_sha256: sha256.nullable(),
    endpoint: z.string(),
    requester_kind: requesterKind,
    requester_id: z.string().nullable(),
    requester_instance_id: z.string().nullable(),
    lease_id: z.string().uuid().nullable(),
    bench_version: z.number().int().nullable(),
    served_source: z.boolean(),
    served_screened_image: z.boolean().nullable(),
    source_ip_hash: z.string().regex(/^[0-9a-f]{16}$/).nullable(),
    forwarded_claim_present: z.boolean(),
  })),
  absence_proves_no_fetch: z.literal(false),
  limitations: z.string(),
})
