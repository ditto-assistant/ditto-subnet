import { describe, expect, expectTypeOf, it } from 'vitest'
import type { input as ZodInput } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'
import { revealPlanShadowSchema } from './admin.schemas'

type Generated = PlatformComponents['schemas']['RevealPlanShadowStatus']

describe('reveal plan shadow schema', () => {
  it('matches the generated Platform contract', () => {
    // Enforced by `pnpm check` (tsc); vitest does not evaluate type assertions.
    expectTypeOf<Generated>().toMatchTypeOf<ZodInput<typeof revealPlanShadowSchema>>()
    expectTypeOf<keyof Generated>().toEqualTypeOf<
      keyof ZodInput<typeof revealPlanShadowSchema>
    >()
  })

  it('defaults a summary written before every counter existed', () => {
    const shadow = revealPlanShadowSchema.parse({ mode: 'shadow', ranges: 1, agreed: 1 })
    expect(shadow.missed).toBe(0)
    expect(shadow.recent_misses).toEqual([])
  })
})
