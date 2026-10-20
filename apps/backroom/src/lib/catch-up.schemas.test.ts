import { describe, expect, expectTypeOf, it } from 'vitest'
import type { input as ZodInput } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'
import { catchUpSchema } from './admin.schemas'

type Generated = PlatformComponents['schemas']['CatchUpStatus']

describe('catch-up schema', () => {
  it('matches the generated Platform contract', () => {
    // Enforced by `pnpm check` (tsc); vitest does not evaluate type assertions.
    expectTypeOf<Generated>().toMatchTypeOf<ZodInput<typeof catchUpSchema>>()
    expectTypeOf<keyof Generated>().toEqualTypeOf<keyof ZodInput<typeof catchUpSchema>>()
  })

  it('defaults a summary written before every counter existed', () => {
    const catchUp = catchUpSchema.parse({ planner: 'reveal', ranges: 1, sparse: 1 })
    expect(catchUp.full_scan).toBe(0)
    expect(catchUp.full_scan_reasons).toEqual({})
  })
})
