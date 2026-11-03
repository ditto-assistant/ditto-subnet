import { describe, expect, expectTypeOf, it } from 'vitest'
import type { input as ZodInput } from 'zod'
import type { components as PlatformComponents } from '../generated/platform-api'
import { runtimeInterfaceReportSchema } from './runtime-interface.schemas'

type Generated = PlatformComponents['schemas']['RuntimeInterfaceReport']

describe('runtime interface report schema', () => {
  it('matches the generated Platform contract', () => {
    // Enforced by `pnpm check` (tsc); vitest does not evaluate type assertions.
    expectTypeOf<Generated>().toMatchTypeOf<ZodInput<typeof runtimeInterfaceReportSchema>>()
    expectTypeOf<keyof Generated>().toEqualTypeOf<
      keyof ZodInput<typeof runtimeInterfaceReportSchema>
    >()
  })

  it('parses the v479 report Platform serves', () => {
    const report = runtimeInterfaceReportSchema.parse({
      block: 9305810,
      block_hash: `0x${'11'.repeat(32)}`,
      runtime_code_hash: '0x44e952fd1cebe9bd15c0c55cee6e1d7c6c97b44d941b79127343118dbbd342bc',
      spec_version: 479,
      surfaces: [
        {
          surface: 'source_emission',
          admitted_by: 'interface',
          interface_digest: 'd'.repeat(64),
          nearest_audited_release: 'v478',
          changed_entries: [],
        },
        {
          surface: 'treasury_receipt',
          admitted_by: null,
          interface_digest: null,
          nearest_audited_release: null,
          changed_entries: [],
          error: 'runtime lacks consumed interface: call:Proxy.proxy',
        },
      ],
      treasury_signer: {
        approved_runtime_code_hash: `0x${'7e'.repeat(32)}`,
        signs_on_this_runtime: false,
        call_baseline_release: 'v478',
        changed_calls: ['SubtensorModule.set_reject_staking_hotkey_growth'],
        changed_calls_truncated: false,
      },
    })
    expect(report.surfaces[0].error).toBeNull()
    expect(report.surfaces[1].admitted_by).toBeNull()
    expect(report.treasury_signer.changed_calls).toHaveLength(1)
  })
})
