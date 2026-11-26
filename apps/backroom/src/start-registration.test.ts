import type { Register } from '@tanstack/react-start'
import { describe, expectTypeOf, it } from 'vitest'
import type { getRouter } from './router'
import type { startInstance } from './start'

describe('Start registration', () => {
  it('retains router, SSR, and config types after route generation', () => {
    expectTypeOf<Register['ssr']>().toEqualTypeOf<true>()
    expectTypeOf<Register['router']>().toEqualTypeOf<ReturnType<typeof getRouter>>()
    expectTypeOf<Register['config']>().toEqualTypeOf<Awaited<ReturnType<typeof startInstance.getOptions>>>()
  })
})
