import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { submissionTermsSchema } from './submission-terms.schemas'

// The exact GET /api/v1/public/submission-terms body Platform serves with
// default settings, pinned by Platform's
// test_submission_terms_fixture_is_the_live_response.
const platformTerms = JSON.parse(
  readFileSync(
    new URL(
      '../../../platform/ditto/tests/fixtures/submission_terms_model_context.json',
      import.meta.url,
    ),
    'utf8',
  ),
) as unknown

describe('submission terms schema', () => {
  it('parses the live Platform payload, model_context included', () => {
    const terms = submissionTermsSchema.parse(platformTerms)
    expect(terms.competitions.map((row) => row.competition)).toEqual([
      'memory',
      'generator',
      'model_context',
    ])
    const modelContext = terms.competitions.find((row) => row.competition === 'model_context')
    expect(modelContext?.accepting).toBe(false)
    expect(modelContext?.deliverable.kind).toBe('model_weights_repo')
    expect(modelContext?.deliverable.limits.map((limit) => limit.unit)).toContain('rank')
    expect(modelContext?.fee.required).toBe(false)
    expect(modelContext?.terms_endpoint).toBe('/api/v1/model-context/upload-terms')
  })
})
