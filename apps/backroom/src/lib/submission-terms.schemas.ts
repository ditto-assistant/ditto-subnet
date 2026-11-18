import { z } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'
import { readmissionActivationSchema, readmissionCostBasisSchema } from './readmission.schemas'

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
type GeneratedReadmissionTerms = PlatformComponents['schemas']['ReadmissionTerms']

// An enum of exactly the generated literal union: a value Platform adds to
// the wire model and Backroom does not list is a type error here, not a
// ZodError on the live read (a new competition once broke the whole read).
type MissingFrom<Generated extends string, Listed extends string> =
  [Exclude<Generated, Listed>] extends [never]
    ? unknown
    : { missing: Exclude<Generated, Listed> }

function platformEnum<Generated extends string>() {
  return <const Values extends readonly [Generated, ...Generated[]]>(
    values: Values & MissingFrom<Generated, Values[number]>,
  ) => z.enum(values)
}

export const SUBMISSION_TERMS_COMPETITIONS = [
  'memory',
  'generator',
  'model_context',
] as const satisfies readonly GeneratedCompetitionSubmissionTerms['competition'][]

const limitSource = platformEnum<GeneratedArtifactLimit['source']>()([
  'code',
  'environment',
  'setting',
])

export const artifactLimitSchema = z.object({
  name: z.string().min(1),
  value: z.union([z.number().int(), z.string()]),
  unit: platformEnum<GeneratedArtifactLimit['unit']>()([
    'bytes',
    'files',
    'lines',
    'characters',
    'cases',
    'version',
    'rank',
  ]),
  source: limitSource,
  reference: z.string(),
  description: z.string(),
} satisfies PlatformResponseShape<GeneratedArtifactLimit>)

export const artifactContractSchema = z.object({
  kind: platformEnum<GeneratedArtifactContract['kind']>()([
    'agent_harness_tarball',
    'generator_source_archive',
    'model_weights_repo',
  ]),
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

// SN-485 readmission rules (memory only): the activation state and the
// readmission, free-retry and refund rules in plain sentences.
export const readmissionTermsSchema = z.object({
  activation: readmissionActivationSchema,
  rules: z.array(z.string()).max(32).default([]),
  cost_basis: readmissionCostBasisSchema.nullable().default(null),
} satisfies PlatformResponseShape<GeneratedReadmissionTerms>)

export const competitionSubmissionTermsSchema = z.object({
  competition: platformEnum<GeneratedCompetitionSubmissionTerms['competition']>()(
    SUBMISSION_TERMS_COMPETITIONS,
  ),
  accepting: z.boolean(),
  deliverable: artifactContractSchema,
  cooldown: submissionCooldownTermsSchema,
  fee: submissionFeeTermsSchema,
  terms_endpoint: z.string().nullable().default(null),
  // null: a competition without readmission, or a Platform that predates it.
  readmission: readmissionTermsSchema.nullable().default(null),
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
