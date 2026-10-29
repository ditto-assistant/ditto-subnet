import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'

// What a miner submits to each competition (GET /api/v1/public/submission-terms):
// the deliverable (artifact kind, contract version, required files and every
// upload-time limit with where it comes from), the upload cooldown, and the
// competition's own fee. This is the miner submission contract; build
// bounties are a separate system with their own terms.

type PlatformResponseShape<Response extends object> = {
  [Field in keyof Response]-?: z.ZodType<Response[Field]>
}

type GeneratedArtifactLimit = PlatformComponents['schemas']['ArtifactLimit']
type GeneratedArtifactContract = PlatformComponents['schemas']['ArtifactContract']
type GeneratedSubmissionFeeTerms = PlatformComponents['schemas']['SubmissionFeeTerms']
type GeneratedSubmissionCooldownTerms = PlatformComponents['schemas']['SubmissionCooldownTerms']
type GeneratedCompetitionSubmissionTerms =
  PlatformComponents['schemas']['CompetitionSubmissionTerms']

const limitSource = z.enum(['code', 'environment', 'setting'])

export const artifactLimitSchema = z.object({
  name: z.string().min(1),
  value: z.union([z.number().int(), z.string()]),
  unit: z.enum(['bytes', 'files', 'lines', 'characters', 'cases', 'version']),
  source: limitSource,
  reference: z.string(),
  description: z.string(),
} satisfies PlatformResponseShape<GeneratedArtifactLimit>)

export const artifactContractSchema = z.object({
  kind: z.enum(['agent_harness_tarball', 'generator_source_archive']),
  format: z.string(),
  contract_version: z.string().nullable().default(null),
  required_files: z.array(z.string()).max(64).default([]),
  limits: z.array(artifactLimitSchema).max(64).default([]),
  upload_endpoint: z.string(),
  signature_payload: z.string(),
  notes: z.array(z.string()).max(32).default([]),
} satisfies PlatformResponseShape<GeneratedArtifactContract>)

export const submissionFeeTermsSchema = z.object({
  required: z.boolean(),
  amount_rao: z.number().int().positive(),
  amount_source: z.enum(['default', 'override']),
  required_source: z.enum(['always', 'competition_settings', 'override']),
  send_address: z.string(),
  asset: z.literal('TAO').default('TAO'),
  settings_revision: z.number().int().nonnegative(),
} satisfies PlatformResponseShape<GeneratedSubmissionFeeTerms>)

export const submissionCooldownTermsSchema = z.object({
  seconds: z.number().int().nonnegative(),
  scope: z.enum(['owner_coldkey', 'hotkey']),
  source: limitSource,
  reference: z.string(),
} satisfies PlatformResponseShape<GeneratedSubmissionCooldownTerms>)

export const competitionSubmissionTermsSchema = z.object({
  competition: z.enum(['memory', 'generator']),
  accepting: z.boolean(),
  deliverable: artifactContractSchema,
  cooldown: submissionCooldownTermsSchema,
  fee: submissionFeeTermsSchema,
  terms_endpoint: z.string().nullable().default(null),
} satisfies PlatformResponseShape<GeneratedCompetitionSubmissionTerms>)

export const submissionTermsSchema = z.object({
  competitions: z.array(competitionSubmissionTermsSchema).max(16),
})

// competitions null: the Platform predates per-competition submission terms;
// the note says where the same facts live on that Platform.
export const submissionTermsReadSchema = z.object({
  competitions: z.array(competitionSubmissionTermsSchema).max(16).nullable(),
  note: z.string().nullable(),
})

export type SubmissionTermsRead = z.infer<typeof submissionTermsReadSchema>
