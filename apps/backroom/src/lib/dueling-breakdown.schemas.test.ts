import { describe, expect, it } from 'vitest'
import { publicDuelingBreakdownSchema } from './admin.schemas'

const breakdown = {
  formula: 'gate x ((1 - w|U|) x datagen + w x sum col)',
  season_id: 's18-0001',
  season_index: 26169,
  datagen_raw: 0.9,
  datagen_gate: 0.75,
  column_weight: 0.1,
  counted_columns: 4,
  datagen_share: 0.6,
  generator_sum: 0.23,
  pre_gate_composite: 0.77,
  composite: 0.5775,
  priced_cost_applied_factor_bps: 9392,
  other_gate_factor: 0.84,
  columns: [],
}

describe('public dueling breakdown schema (SN-500)', () => {
  it('keeps an applied memory factor instead of stripping it', () => {
    const parsed = publicDuelingBreakdownSchema.parse({
      ...breakdown,
      memory_factor_applied_factor_bps: 9500,
      memory_factor: 0.95,
      memory_factor_p95_bytes: 1280 * 1048576,
    })
    expect(parsed.memory_factor_applied_factor_bps).toBe(9500)
    expect(parsed.memory_factor).toBe(0.95)
    expect(parsed.memory_factor_p95_bytes).toBe(1280 * 1048576)
  })

  it('reads a Platform without the fields, or a shadow factor, as null', () => {
    const parsed = publicDuelingBreakdownSchema.parse(breakdown)
    expect(parsed.memory_factor_applied_factor_bps).toBeNull()
    expect(parsed.memory_factor).toBeNull()
    expect(parsed.memory_factor_p95_bytes).toBeNull()
  })
})
