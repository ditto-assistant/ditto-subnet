import { describe, expect, it } from 'vitest'
import { BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE } from './mcp.server'
import {
  callsWriteTool,
  insufficientScopeResponse,
  requiredScopesForRequest,
} from './mcp-scope.server'

describe('MCP scope challenges', () => {
  it('recognizes write tool calls without consuming the request body', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: 'resolve_screening_quarantine', arguments: { quarantineId: 'q-1' } },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await request.json()).toMatchObject({ method: 'tools/call' })
  })

  it('does not challenge read tools', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: 'get_queue_policy_settings', arguments: {} },
      }),
    })
    expect(await callsWriteTool(request)).toBe(false)
  })

  it('leaves the owner footprint on the ordinary read scope', async () => {
    // Coldkeys are identity metadata from the payment ledger, not miner source.
    // The artifact scope exists for source (tarballs, file listings, diffs), so
    // gating identity behind it would both misstate the sensitivity model and
    // block routine duplicate adjudication. This also keeps the tool clear of
    // the accessLevel snapshot that hasWriteAccess/hasArtifactAccess consult.
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'get_miner_owner_footprint',
          arguments: { key: '5TopMiner' },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([])
  })

  it('keeps validator retry diagnosis on the ordinary read scope', async () => {
    // The ticket ledger is operational telemetry every reader needs; only the
    // container_log_tail FIELD inside it discloses miner source, and that is
    // redacted per-field in the handler. Gating the whole tool on the artifact
    // scope would lock plain readers out of routine stuck-lease triage to
    // protect one optional column -- and this assertion is what would catch
    // that mistake, since the field-level gate is invisible from out here.
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'get_validation_retry',
          arguments: { agentId: '5fdadd33-bd0f-492d-ba71-49bef159f069' },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([])
  })

  it('recognizes rejected submission rescreens as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'rescreen_rejected_submission',
          arguments: {
            agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
            reason: 'Worker DNS incident',
            expectedSha256: 'ab'.repeat(32),
            expectedScoreCount: 0,
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([
      BACKROOM_WRITE_SCOPE,
    ])
  })

  it('recognizes immediate failed screening retries as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'retry_failed_screening_now',
          arguments: {
            agentId: '270acbcc-268d-4380-9db7-c5fb90726941',
            reason: 'Retry after source-review budget exhaustion',
            expectedSha256: 'ab'.repeat(32),
            expectedScoreCount: 0,
            expectedAttemptId: 'af86d39d-51c6-46d7-83d4-36b61cab6aef',
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([
      BACKROOM_WRITE_SCOPE,
    ])
  })

  it('recognizes scored rollout qualification as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'qualify_scored_benchmark_rollout',
          arguments: {
            agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
            expectedSha256: 'ab'.repeat(32),
            expectedRolloutId: '8f734f51-fde1-4cbb-8f43-15e6882804f4',
            expectedTotalScoreCount: 3,
            expectedSourceScoreCount: 3,
            expectedTargetScoreCount: 0,
            reason: 'Current hybrid champion requires policy v9 and benchmark v3',
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([
      BACKROOM_WRITE_SCOPE,
    ])
  })

  it('recognizes validator infrastructure retry as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'retry_validator_evaluation',
          arguments: {
            agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
            expectedSnapshot: 'ab'.repeat(32),
            reason: 'Verified validator OOM',
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_WRITE_SCOPE])
  })

  it('recognizes batch validator retry as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'batch_retry_validator_evaluation',
          arguments: {
            reason: 'Verified validator OOM across the batch',
            items: [
              {
                agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
                expectedSnapshot: 'ab'.repeat(32),
              },
            ],
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_WRITE_SCOPE])
  })

  it('recognizes an efficiency bonus policy revision as write-scoped', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'set_efficiency_bonus_settings',
          arguments: {
            expectedRevision: 0,
            settings: {
              enabled: true,
              fold_enabled: false,
              cap: 0.05,
              deep_cap: 0.1,
              deep_frontier_ratio: 0.5,
              cohort_size: 25,
              min_cohort: 8,
              epoch_hours: 24,
              quality_floor: 0,
              memory_floor: 0,
            },
            reason: 'Enable the v7 efficiency bonus for the shadow window',
            confirmation: 'APPLY EFFICIENCY BONUS ENABLED',
          },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(true)
    expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_WRITE_SCOPE])
  })

  it('recognizes a queue policy revision as write-scoped and its read as not', async () => {
    const write = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'set_queue_policy_settings',
          arguments: {
            expectedRevision: 0,
            settings: { rescore_cohort_size: 15, priority_cohort_size: 6 },
            reason: 'widen the rescore cohort for the next rollout',
            confirmation: 'APPLY QUEUE POLICY SETTINGS',
          },
        },
      }),
    })
    expect(await callsWriteTool(write)).toBe(true)
    expect(await requiredScopesForRequest(write)).toEqual([BACKROOM_WRITE_SCOPE])

    const read = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: 'get_queue_policy_settings', arguments: {} },
      }),
    })
    expect(await callsWriteTool(read)).toBe(false)
    expect(await requiredScopesForRequest(read)).toEqual([])
  })

  it('recognizes a validator slot revision as write-scoped and its read as not', async () => {
    const write = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'set_validator_slot_settings',
          arguments: {
            expectedRevision: 0,
            settings: {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
    },
            reason: 'ramp the fleet to three slots now that dispatch is stable',
            confirmation: 'APPLY VALIDATOR SLOT CAP 3',
          },
        },
      }),
    })
    expect(await callsWriteTool(write)).toBe(true)
    expect(await requiredScopesForRequest(write)).toEqual([BACKROOM_WRITE_SCOPE])

    const read = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: 'get_validator_slot_settings', arguments: {} },
      }),
    })
    expect(await callsWriteTool(read)).toBe(false)
    expect(await requiredScopesForRequest(read)).toEqual([])
  })

  it('scopes confirmation policy and retest mutations without scoping audit reads', async () => {
    for (const name of [
      'set_confirmation_bundle_settings',
      'authorize_confirmation_bundle_retest',
    ]) {
      const request = new Request('https://backroom.dittobench.ai/mcp', {
        method: 'POST',
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: { name, arguments: {} },
        }),
      })
      expect(await callsWriteTool(request)).toBe(true)
      expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_WRITE_SCOPE])
    }

    for (const name of [
      'get_confirmation_bundle_settings',
      'list_confirmation_bundles',
      'get_confirmation_lane_diagnosis',
      'get_confirmation_bundle',
    ]) {
      const request = new Request('https://backroom.dittobench.ai/mcp', {
        method: 'POST',
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: { name, arguments: {} },
        }),
      })
      expect(await callsWriteTool(request)).toBe(false)
      expect(await requiredScopesForRequest(request)).toEqual([])
    }
  })

  it('keeps the efficiency bonus policy read unscoped beyond backroom:read', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: { name: 'get_efficiency_bonus_settings', arguments: {} },
      }),
    })

    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([])
  })

  it('keeps the stuck-submission list and scoring readiness read-only', async () => {
    for (const name of [
      'list_stuck_submissions',
      'agent_scoring_readiness',
      'get_agent_coding_certifications',
    ]) {
      const request = new Request('https://backroom.dittobench.ai/mcp', {
        method: 'POST',
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: { name, arguments: {} },
        }),
      })
      expect(await callsWriteTool(request)).toBe(false)
      expect(await requiredScopesForRequest(request)).toEqual([])
    }
  })

  it('recognizes sensitive screening artifact calls as artifact-scoped reads', async () => {
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'get_screening_artifact',
          arguments: { agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054' },
        },
      }),
    })
    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_ARTIFACT_SCOPE])
  })

  it('gates source search on the artifact scope like an excerpt read', async () => {
    // A search returns the matching source lines themselves. Treating it as an
    // ordinary read because it "only" answers a location question would let a
    // read-scoped connection extract miner code a line at a time.
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'search_screening_source',
          arguments: {
            agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
            pattern: 'RunResponse',
          },
        },
      }),
    })
    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([BACKROOM_ARTIFACT_SCOPE])
  })

  it('leaves owner-link attestations on the ordinary read scope', async () => {
    // A signed owner link is identity metadata, not miner source. The artifact
    // scope exists for source (tarballs, file listings, diffs), so gating
    // identity behind it would misstate the sensitivity model and block routine
    // duplicate adjudication — the same reasoning that keeps owner coldkeys on
    // backroom:read. It also keeps this tool clear of the frozen accessLevel
    // snapshot that only hasWriteAccess/hasArtifactAccess consult.
    const request = new Request('https://backroom.dittobench.ai/mcp', {
      method: 'POST',
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'tools/call',
        params: {
          name: 'get_owner_attestations',
          arguments: { hotkey: '5QueriedHotkey' },
        },
      }),
    })

    expect(await callsWriteTool(request)).toBe(false)
    expect(await requiredScopesForRequest(request)).toEqual([])
    expect(await requiredScopesForRequest(request)).not.toContain(
      BACKROOM_ARTIFACT_SCOPE,
    )
  })

  it('returns an RFC 6750 step-up challenge for write access', async () => {
    const response = insufficientScopeResponse(
      new Request('https://backroom.dittobench.ai/mcp'),
      BACKROOM_WRITE_SCOPE,
    )
    expect(response.status).toBe(403)
    expect(response.headers.get('WWW-Authenticate')).toContain(
      'error="insufficient_scope"',
    )
    expect(response.headers.get('WWW-Authenticate')).toContain(
      'scope="backroom:read backroom:write"',
    )
    expect(response.headers.get('WWW-Authenticate')).toContain(
      '/.well-known/oauth-protected-resource/mcp',
    )
  })

  it('returns a dedicated step-up challenge for artifact access', () => {
    const response = insufficientScopeResponse(
      new Request('https://backroom.dittobench.ai/mcp'),
      BACKROOM_ARTIFACT_SCOPE,
    )
    expect(response.status).toBe(403)
    expect(response.headers.get('WWW-Authenticate')).toContain(
      'scope="backroom:read backroom:artifact:read"',
    )
  })
})
