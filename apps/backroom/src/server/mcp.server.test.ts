import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import { deriveRequestId } from '../lib/idempotency'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
  type McpGrantProps,
} from './mcp.server'

const session: BackroomSession = {
  version: 2,
  uid: 'staff-1',
  email: 'peyton@omniaura.ai',
  name: 'Staff User',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 7 * 24 * 60 * 60_000,
}

const originalAdminToken = process.env.DITTO_ADMIN_API_TOKEN

async function connect(scopes: Array<string>) {
  const props: McpGrantProps = { session, scopes, clientName: 'Test client' }
  const server = createBackroomMcpServer(props)
  const client = new Client({ name: 'backroom-test', version: '1.0.0' })
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
  return { client, server }
}

function readTextResult(response: unknown) {
  if (!response || typeof response !== 'object' || !('content' in response)) {
    throw new Error('Expected an MCP content result')
  }
  const { content } = response
  if (!Array.isArray(content)) throw new Error('Expected MCP result content')
  const text = content.find(
    (item): item is { type: 'text'; text: string } =>
      !!item &&
      typeof item === 'object' &&
      item.type === 'text' &&
      typeof item.text === 'string',
  )?.text
  if (text === undefined) throw new Error('Expected an MCP text result')
  return text
}

function readJsonResult(response: unknown) {
  return JSON.parse(readTextResult(response)) as unknown
}

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalAdminToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalAdminToken
})

describe('Backroom MCP tools', () => {
  it('publishes every Backroom operation with MCP safety annotations', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.listTools()

    expect(response.tools.map((tool) => tool.name).sort()).toEqual(
      [
        'execute_screening_quarantine_batch',
        'expand_benchmark_rollout_cohort',
        'get_backroom_access',
        'get_backroom_tool_help',
        'get_ath_review',
        'search_ath_precedents',
        'get_benchmark_contract_refresh',
        'get_benchmark_contract_migration',
        'get_benchmark_rollout_control',
        'get_benchmark_rollout_qualification',
        'get_burn_settings',
        'get_copy_review_source_diff',
        'get_continual_retest_settings',
        'get_confirmation_bundle_settings',
        'get_confirmation_bundle',
        'get_confirmation_lane_diagnosis',
        'get_efficiency_bonus_settings',
        'get_inference_concurrency_settings',
        'get_inference_runtime_metrics',
        'list_inference_traces',
        'download_inference_trace',
        'peek_inference_trace',
        'download_runtime_profile',
        'get_queue_policy_settings',
        'get_screener_capacity',
        'get_screener_review_settings',
        'apply_screener_review_settings',
        'get_screener_policy_manifest',
        'rotate_screener_policy_manifest',
        'get_screener_policy_activation',
        'schedule_screener_policy_activation',
        'get_validator_fleet',
        'get_validator_slot_settings',
        'list_validator_assignments',
        'list_confirmation_bundles',
        'set_burn_settings',
        'set_continual_retest_settings',
        'set_efficiency_bonus_settings',
        'set_queue_policy_settings',
        'set_validator_slot_settings',
        'set_confirmation_bundle_settings',
        'authorize_confirmation_bundle_retest',
        'read_copy_review_source_diff_file',
        'get_screening_baseline_diff',
        'read_screening_baseline_diff_file',
        'get_screening_quarantine_context',
        'get_screening_quarantine_contexts',
        'get_screening_review_queue',
        'get_screening_submission',
        'get_source_release_policy',
        'get_owner_attestations',
        'get_submission_cooldown',
        'get_validation_retry',
        'list_stuck_submissions',
        'list_lease_revocations',
        'list_hotkey_bans',
        'batch_retry_validator_evaluation',
        'agent_scoring_readiness',
        'get_agent_coding_certifications',
        'get_agent_scores',
        'get_leaderboard',
        'get_miner_owner_footprint',
        'get_score_history',
        'get_screened_image_rebuild',
        'get_validator_score_replacement',
        'list_v9_contract_retests',
        'open_ath_review',
        'preview_screening_quarantine_batch',
        'list_screening_quarantines',
        'list_screening_disputes',
        'list_screening_source_files',
        'list_screening_submissions',
        'summarize_screening_failures',
        'read_screening_source_file',
        'search_screening_source',
        'rebuild_screened_image',
        'get_screening_artifact',
        'refresh_benchmark_contract',
        'migrate_zero_score_benchmark_contract',
        'remove_failed_submission_from_queue',
        'evict_live_validator_leases',
        'reinstate_evicted_submission_to_queue',
        'qualify_scored_benchmark_rollout',
        'start_benchmark_rollout',
        'resolve_screening_quarantine',
        'resolve_screening_dispute',
        'resolve_ath_review',
        'rescreen_rejected_submission',
        'retry_failed_screening_now',
        'retry_trusted_image_build',
        'expire_running_screening',
        'reject_screening_submission',
        'retry_validator_evaluation',
        'replace_validator_score',
        'queue_validator_score_retests',
        'set_inference_concurrency_settings',
        'start_runtime_profile',
        'set_source_release_policy',
        'set_submission_cooldown',
        'unban_hotkey',
      ].sort(),
    )
    expect(response.tools.map((tool) => tool.name)).not.toContain(
      'set_submission_deposit_address',
    )
    for (const tool of response.tools) {
      const reason = (
        tool.inputSchema as {
          properties?: Record<string, { maxLength?: number }>
        }
      ).properties?.reason
      expect(reason?.maxLength, `${tool.name} must preserve detailed reasons`).toBeUndefined()
    }
    // The catalog is loaded before any call. Keep both the complete JSON and
    // its descriptions bounded so a new operational tutorial cannot silently
    // tax every MCP session. Before catalog summaries this was ~109k chars,
    // including ~57k chars of descriptions alone.
    //
    // Prose is guarded exactly by the two description assertions below; this
    // number is the coarse whole-payload backstop, and input schemas dominate
    // it. Curve v3 and the runtime metrics/capture contracts added legitimate,
    // bounded input schemas without relaxing either prose budget below. The
    // 98_400 whole-payload includes the L1 model/timeout fields on the
    // screener-review settings write schema, the validator fleet/assignment
    // read schemas, the three inference-trace archive tools, the operator
    // screening-reject tool, the gradient-hold and adjudicator controls, and
    // the screener policy-activation write schema (revision guard, versioned
    // target, timezone-aware instant, rescreen flag). Keep modest headroom for
    // schema evolution; tighten the description budgets, not this
    // whole-payload backstop, to push back on tutorials.
    expect(JSON.stringify(response.tools).length).toBeLessThanOrEqual(98_400)
    const descriptions = response.tools.map((tool) => tool.description ?? '')
    // Includes concise rollout and protected-policy controls; tutorials live
    // in get_backroom_tool_help, not here. 22_000 admits the screener
    // policy-activation pair: one required catalog line each, with their
    // operational tutorials kept in get_backroom_tool_help.
    expect(descriptions.reduce((total, value) => total + value.length, 0)).toBeLessThanOrEqual(
      22_000,
    )
    expect(Math.max(...descriptions.map((value) => value.length))).toBeLessThanOrEqual(600)
    expect(
      response.tools.find((tool) => tool.name === 'get_screening_review_queue')?.annotations
        ?.readOnlyHint,
    ).toBe(true)
    expect(
      response.tools.find((tool) => tool.name === 'resolve_screening_quarantine')?.annotations
        ?.destructiveHint,
    ).toBe(true)
    expect(
      response.tools.find((tool) => tool.name === 'get_screening_artifact')?.annotations
        ?.readOnlyHint,
    ).toBe(true)
    const startRollout = response.tools.find((tool) => tool.name === 'start_benchmark_rollout')
    expect(startRollout?.annotations?.readOnlyHint).toBe(false)
    expect(startRollout?.annotations?.destructiveHint).toBe(true)
    expect(response.tools.some((tool) => tool.name.includes('supersede_benchmark'))).toBe(false)
    // Subnet scoring policy is a production mutation, never a read hint, and it
    // must stay separable from the product entitlement flags in its description.
    expect(
      response.tools.find((tool) => tool.name === 'get_efficiency_bonus_settings')?.annotations
        ?.readOnlyHint,
    ).toBe(true)
    const efficiencyWrite = response.tools.find(
      (tool) => tool.name === 'set_efficiency_bonus_settings',
    )
    expect(efficiencyWrite?.annotations?.readOnlyHint).toBe(false)
    expect(efficiencyWrite?.annotations?.destructiveHint).toBe(true)
    expect(efficiencyWrite?.description).toContain('not served by this server')
    expect(
      response.tools.find((tool) => tool.name === 'get_continual_retest_settings')
        ?.annotations?.readOnlyHint,
    ).toBe(true)
    const retestWrite = response.tools.find(
      (tool) => tool.name === 'set_continual_retest_settings',
    )
    expect(retestWrite?.annotations?.destructiveHint).toBe(true)
    // A revision stores the whole policy, so the published schema has to demand
    // the whole policy. If any of these drifts back to optional, an agent that
    // flips one switch silently writes defaults over the rest — reverting the
    // wave_membership rollback among them.
    const retestSettings = (
      retestWrite?.inputSchema?.properties as
        | { settings?: { required?: Array<string> } }
        | undefined
    )?.settings
    expect(retestSettings?.required).toEqual(
      expect.arrayContaining([
        'aggregate_mode',
        'tie_weighting_mode',
        'idle_retests_enabled',
        'wave_membership',
        'retest_cohort_size',
        'retest_eligibility_mode',
        'retest_eligibility_z',
        'retest_cohort_max_size',
      ]),
    )
    expect(retestWrite?.description).toContain('CHANGES WHAT VALIDATORS WEIGHT')
    expect(retestWrite?.description).toContain('every one of these fields is required')
    expect(
      response.tools.find((tool) => tool.name === 'get_queue_policy_settings')?.annotations
        ?.readOnlyHint,
    ).toBe(true)
    const queuePolicyWrite = response.tools.find(
      (tool) => tool.name === 'set_queue_policy_settings',
    )
    expect(queuePolicyWrite?.annotations?.readOnlyHint).toBe(false)
    expect(queuePolicyWrite?.annotations?.destructiveHint).toBe(true)
    // Operators read these descriptions to decide what is live now versus what
    // only lands at the next rollout, so the two lifetimes must stay documented.
    expect(queuePolicyWrite?.description).toContain('NEVER resizes an in-flight rollout')
    expect(queuePolicyWrite?.description).toContain('REFUSED while a benchmark rollout is open')
    expect(queuePolicyWrite?.description).toContain('queue-fairness and capacity rail')
    expect(queuePolicyWrite?.description).toContain('The whole nested block is required')
    expect(queuePolicyWrite?.description).toContain('ships DISABLED')
    expect(queuePolicyWrite?.description).toContain('not served by this server')
    expect(
      response.tools.find((tool) => tool.name === 'get_validator_slot_settings')?.annotations
        ?.readOnlyHint,
    ).toBe(true)
    const validatorSlotWrite = response.tools.find(
      (tool) => tool.name === 'set_validator_slot_settings',
    )
    expect(validatorSlotWrite?.annotations?.readOnlyHint).toBe(false)
    expect(validatorSlotWrite?.annotations?.destructiveHint).toBe(true)
    // Operators read these descriptions before ramping a live fleet, so the
    // properties that make the confirmation meaningful must stay documented.
    expect(validatorSlotWrite?.description).toContain('APPLY VALIDATOR SLOT CAP <n>')
    expect(validatorSlotWrite?.description).toContain('It is deliberately not derived')
    expect(validatorSlotWrite?.description).toContain('never revokes tickets a validator already holds')
    expect(validatorSlotWrite?.description).toContain('a partial write is rejected')
    expect(validatorSlotWrite?.description).toContain('not served by this server')
    // The read tool has to pre-empt the "fleet looks idle" misread.
    expect(
      response.tools.find((tool) => tool.name === 'get_validator_slot_settings')?.description,
    ).toContain('not an underutilized host')
    // Eviction destroys benchmark runs a validator may still be executing, and
    // the tool description is the operator's only documentation at the call
    // site. These are the four things they must not have to guess: what it
    // frees, what survives it, which phrase it demands, and why the two
    // queue-removal tools are not interchangeable.
    const evictLeases = response.tools.find(
      (tool) => tool.name === 'evict_live_validator_leases',
    )
    expect(evictLeases?.annotations?.readOnlyHint).toBe(false)
    expect(evictLeases?.annotations?.destructiveHint).toBe(true)
    expect(evictLeases?.description).toContain('EVICT LIVE VALIDATOR LEASES')
    expect(evictLeases?.description).toContain('REMOVE FROM VALIDATOR QUEUE')
    expect(evictLeases?.description).toContain('remove_failed_submission_from_queue')
    expect(evictLeases?.description).toContain('can still reach quorum automatically')
    expect(evictLeases?.description).toContain('90-minute lease')
    expect(evictLeases?.description).toContain('does NOT mint a no-fault retry grant')
    expect(evictLeases?.description).toContain('NOT deletion, NOT rejection, and NOT rescreening')
    // And the operator who hits the removal tool's refusal has to be told where
    // the escape hatch is, at the moment they are refused.
    expect(
      response.tools.find((tool) => tool.name === 'remove_failed_submission_from_queue')
        ?.description,
    ).toContain('evict_live_validator_leases')
    // The read tool is where an operator decides whether to reach for it.
    const validationRetryRead = response.tools.find(
      (tool) => tool.name === 'get_validation_retry',
    )
    expect(validationRetryRead?.description).toContain('eviction_allowed')
    expect(validationRetryRead?.description).toContain('eviction_blocking_reason')
    expect(validationRetryRead?.description).toContain('live_ticket_count')
    expect(validationRetryRead?.description).toContain('evicted_validator_hotkeys')
    // Reinstatement is what makes the eviction lever usable at all, so the
    // operator must be told at the eviction call site that it is reversible,
    // and told at the reversal call site exactly what it does not give back.
    expect(evictLeases?.description).toContain('REVERSIBLE')
    expect(evictLeases?.description).toContain('reinstate_evicted_submission_to_queue')
    const reinstate = response.tools.find(
      (tool) => tool.name === 'reinstate_evicted_submission_to_queue',
    )
    expect(reinstate?.annotations?.readOnlyHint).toBe(false)
    expect(reinstate?.annotations?.destructiveHint).toBe(true)
    expect(reinstate?.description).toContain('REINSTATE TO VALIDATOR QUEUE')
    expect(reinstate?.description).toContain('EVICT LIVE VALIDATOR LEASES')
    expect(reinstate?.description).toContain('REMOVE FROM VALIDATOR QUEUE')
    expect(reinstate?.description).toContain('does not mint a no-fault retry grant')
    expect(reinstate?.description).toContain('no longer the active one')
    expect(reinstate?.description).toContain('retry_budget_snapshot')
    expect(validationRetryRead?.description).toContain('reinstatement_allowed')
    expect(validationRetryRead?.description).toContain('reinstated_at')
    // Why a ticket ended, at the call site. infra_retry_grants is named
    // explicitly because it is the field whose absence caused 2026-07-27 to be
    // read as "the validator has gone silent" when the failures were in fact
    // being reported and the platform was re-leasing on them.
    expect(validationRetryRead?.description).toContain('infra_retry_grants')
    expect(validationRetryRead?.description).toContain('silently_expired')
    expect(validationRetryRead?.description).toContain('failure_reason')
    const stuckList = response.tools.find(
      (tool) => tool.name === 'list_stuck_submissions',
    )
    expect(stuckList?.description).toContain('silent_expiry_count')
    expect(stuckList?.description).toContain('infra_retry_grants')
    // The revocation ledger is read-only, and its two hazards have to be stated
    // where an operator reads them: evidence is not a fixed shape, and an empty
    // answer is a finding rather than an unwired feature.
    const revocations = response.tools.find(
      (tool) => tool.name === 'list_lease_revocations',
    )
    expect(revocations?.annotations?.readOnlyHint).toBe(true)
    expect(revocations?.annotations?.destructiveHint).toBe(false)
    expect(revocations?.description).toContain('WHOLE AND UNTYPED')
    expect(revocations?.description).toContain(
      'AN EMPTY RESULT IS A FINDING, NOT AN UNWIRED FEATURE',
    )
    expect(revocations?.description).toContain('validator_lease_audit')
    expect(revocations?.description).toContain('operator_evicted')

    await client.close()
    await server.close()
  })

  it('pages the trace archive openly but gates record content on the artifact scope', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const key =
      'traces/v1/lane=inference/kind=chat/dt=2026-08-24/hour=17/relay_1-a.jsonl.zst'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({
          bucket: 'ditto-subnet-traces',
          prefix: 'traces/v1/lane=inference/kind=chat/dt=2026-08-24/',
          objects: [
            {
              key,
              size: 1234,
              last_modified: '2026-08-24T17:10:00.000Z',
              etag: 'abc',
            },
          ],
          continuation_token: null,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          bucket: 'ditto-subnet-traces',
          key,
          records: [
            {
              index: 0,
              recorded_at: '2026-08-24T17:09:59Z',
              event: 'inference.settled',
              lane: 'inference',
              kind: 'chat',
              run_id: 'run-1',
              case_id: 'web_search-0001',
              status: 'completed',
              prompt_tokens: 12,
              completion_tokens: 5,
              provider: 'deepinfra',
              latency_ms: 800,
            },
          ],
          records_scanned: 1,
          scan_complete: true,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const readOnly = await connect([BACKROOM_READ_SCOPE])
    const listing = await readOnly.client.callTool({
      name: 'list_inference_traces',
      arguments: { lane: 'inference', kind: 'chat', dt: '2026-08-24' },
    })
    expect(listing.isError).not.toBe(true)
    expect(readJsonResult(listing)).toMatchObject({
      objects: [{ key, size: 1234 }],
      continuation_token: null,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/traces?scope=traces&lane=inference&kind=chat&dt=2026-08-24&max_keys=200',
      expect.objectContaining({ method: 'GET' }),
    )

    // Record content is miner-sensitive: without the artifact scope the peek
    // is refused before any platform call is made.
    const refused = await readOnly.client.callTool({
      name: 'peek_inference_trace',
      arguments: { key },
    })
    expect(refused.isError).toBe(true)
    expect(readTextResult(refused)).toContain('backroom:artifact:read')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await readOnly.client.close()
    await readOnly.server.close()

    const artifactConn = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    const peek = await artifactConn.client.callTool({
      name: 'peek_inference_trace',
      arguments: { key, maxRecords: 1 },
    })
    expect(peek.isError).not.toBe(true)
    expect(readJsonResult(peek)).toMatchObject({
      records: [{ case_id: 'web_search-0001', provider: 'deepinfra' }],
      scan_complete: true,
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/traces/peek',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )
    await artifactConn.client.close()
    await artifactConn.server.close()
  })

  it('reads confirmation policy as an isolated no-activation control', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      mode: 'shadow',
      eligibility_mode: 'rank',
      top_n: 5,
      min_base_score_micros: 950_000,
      daily_bundle_cap: 10,
      daily_dollar_cap_microusd: 1_000_000,
      per_bundle_request_cap: 100,
      per_bundle_token_cap: 10_000,
      profile_revision: 'confirmation-v9-1',
      profile_checksum: 'a'.repeat(64),
      challenger_z: 1.64,
    }
    const revision = {
      revision: 1,
      parent_revision: 0,
      scope: '*',
      settings,
      checksum: 'b'.repeat(64),
      reason: 'collect bounded shadow evidence before enforcement',
      actor: 'operator@example.com',
      created_at: '2026-08-08T12:00:00Z',
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        current: [revision],
        history: [revision],
        default: {
          ...settings,
          mode: 'off',
          daily_bundle_cap: 0,
          daily_dollar_cap_microusd: 0,
          per_bundle_request_cap: 0,
          per_bundle_token_cap: 0,
          profile_revision: null,
          profile_checksum: null,
        },
        effective: {
          revision: 1,
          scope: '*',
          settings,
          checksum: 'b'.repeat(64),
          source: 'revision',
          configured: true,
          issuance_active: true,
          max_top_n: 10,
          max_daily_bundle_cap: 1_000,
          max_daily_dollar_microusd: 2_000_000_000,
          max_bundle_request_cap: 100_000,
          max_bundle_token_cap: 100_000_000,
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_confirmation_bundle_settings',
      arguments: { historyLimit: 0 },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: {
        revision: 1,
        settings: { mode: 'shadow' },
        issuance_active: true,
      },
      history: [],
      history_count: 1,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundle-settings',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('diagnoses the confirmation lane from settings, bundle states, and fleet', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const digest = 'a'.repeat(64)
    const settings = {
      mode: 'shadow',
      eligibility_mode: 'rank',
      top_n: 5,
      min_base_score_micros: 950_000,
      daily_bundle_cap: 10,
      daily_dollar_cap_microusd: 1_000_000,
      per_bundle_request_cap: 100,
      per_bundle_token_cap: 10_000,
      profile_revision: 'confirmation-v9-1',
      profile_checksum: digest,
      challenger_z: 1.64,
    }
    const revision = {
      revision: 15,
      parent_revision: 14,
      scope: '*',
      settings,
      checksum: digest,
      reason: 'keep shadow issuance while diagnosing execution',
      actor: 'operator@example.com',
      created_at: '2026-08-18T12:00:00Z',
    }
    const settingsControl = {
      current: [revision],
      history: [revision],
      default: {
        ...settings,
        mode: 'off',
        daily_bundle_cap: 0,
        daily_dollar_cap_microusd: 0,
        per_bundle_request_cap: 0,
        per_bundle_token_cap: 0,
        profile_revision: null,
        profile_checksum: null,
      },
      effective: {
        revision: 15,
        scope: '*',
        settings,
        checksum: digest,
        source: 'revision',
        configured: true,
        issuance_active: true,
        max_top_n: 10,
        max_daily_bundle_cap: 1_000,
        max_daily_dollar_microusd: 2_000_000_000,
        max_bundle_request_cap: 100_000,
        max_bundle_token_cap: 100_000_000,
      },
    }
    const failedBundle = {
      bundle_id: '10000000-0000-4000-8000-000000000001',
      artifact_sha256: digest,
      bench_version: 11,
      profile_revision: 'confirmation-v9-1',
      profile_checksum: digest,
      retest_generation: 0,
      generation_reason: 'initial',
      source_bundle_id: null,
      state: 'failed',
      settings_revision: 15,
      settings_checksum: digest,
      qualification_status: null,
      completion_mode: null,
      completion_ticket_id: null,
      evidence_sha256: null,
      reporter_hotkey: null,
      bundle_signature: null,
      evidence_root: null,
      verified_at: null,
      completed_at: null,
      created_at: '2026-08-18T19:54:00.000Z',
      updated_at: '2026-08-18T19:54:02.000Z',
      subjects: [],
      dimensions: [],
      tickets: [
        {
          ticket_id: '20000000-0000-4000-8000-000000000001',
          validator_hotkey: '5ValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          slot_id: 'longmem-0',
          status: 'expired',
          attempt: 1,
          issued_at: '2026-08-18T19:54:00.000Z',
          deadline: '2026-08-18T21:54:00.000Z',
          failure_reason: 'confirmation_execution_failed',
          failure_class: 'platform',
          failure_stage: 'unknown',
          failed_at: '2026-08-18T19:54:02.000Z',
          prepare_rejection: null,
          prepare_rejected_at: null,
        },
      ],
    }
    const emptyList = {
      items: [] as Array<typeof failedBundle>,
      count: 0,
      budget: {
        utc_day: '2026-08-18',
        revision: 15,
        issued_attempts: 9,
        outstanding_reserved_microusd: 0,
        settled_microusd: 0,
      },
      shadow_calibration: {
        observed_from_utc_day: null,
        observed_through_utc_day: null,
        observation_days: 0,
        confirmation_profile_revision: 'confirmation-v9-1',
        confirmation_profile_checksum: digest,
        base_run_count: 0,
        measured_base_cost_microusd: null,
        confirmation_bundle_count: 0,
        measured_bundle_cost_microusd: null,
        bench_version: 11,
        completed_bundle_count: 0,
        superseded_bundle_count: 0,
        failed_bundle_count: 0,
        qualified_bundle_count: 0,
        promotion_rate_bps: null,
        projected_daily_spend_microusd: null,
        epoch_duration_seconds: null,
        projected_epoch_spend_microusd: null,
        epoch_projection_unavailable_reason:
          'Bench v11 has no configured epoch duration; no projection was guessed.',
      },
    }
    const failedList = {
      ...emptyList,
      items: Array.from({ length: 5 }, (_, index) => ({
        ...failedBundle,
        bundle_id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
        tickets: [
          {
            ...failedBundle.tickets[0],
            ticket_id: `20000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
          },
        ],
      })),
      count: 9,
      shadow_calibration: {
        ...emptyList.shadow_calibration,
        failed_bundle_count: 9,
      },
    }
    const fetchMock = vi.fn(async (url: string) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/confirmation-bundle-settings')) {
        return Response.json(settingsControl)
      }
      if (parsed.pathname.endsWith('/public/validators')) {
        return Response.json({
          generated_at: '2026-08-18T20:00:00.000Z',
          active_bench_version: 11,
          validators: [],
        })
      }
      if (parsed.searchParams.get('state') === 'failed') {
        return Response.json(failedList)
      }
      return Response.json(emptyList)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_confirmation_lane_diagnosis',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      policy: { mode: 'shadow', issuance_active: true, settings_revision: 15 },
      counts: { failed: 9, completed: 0 },
      likely_cause: { code: 'leftover_validator_v9_identity_pin' },
    })
    expect(fetchMock).toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('writes a complete confirmation policy with the signed-in actor', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      mode: 'enforce',
      eligibility_mode: 'rank',
      top_n: 5,
      min_base_score_micros: 950_000,
      daily_bundle_cap: 10,
      daily_dollar_cap_microusd: 1_000_000,
      per_bundle_request_cap: 100,
      per_bundle_token_cap: 10_000,
      profile_revision: 'confirmation-v9-1',
      profile_checksum: 'a'.repeat(64),
      challenger_z: 1.64,
    }
    const control = {
      current: [],
      history: [],
      default: {
        ...settings,
        mode: 'off',
        daily_bundle_cap: 0,
        daily_dollar_cap_microusd: 0,
        per_bundle_request_cap: 0,
        per_bundle_token_cap: 0,
        profile_revision: null,
        profile_checksum: null,
      },
      effective: {
        revision: 2,
        scope: '*',
        settings,
        checksum: 'b'.repeat(64),
        source: 'revision',
        configured: true,
        issuance_active: true,
        max_top_n: 10,
        max_daily_bundle_cap: 1_000,
        max_daily_dollar_microusd: 2_000_000_000,
        max_bundle_request_cap: 100_000,
        max_bundle_token_cap: 100_000_000,
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ revision: 2 }))
      .mockResolvedValueOnce(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'set_confirmation_bundle_settings',
      arguments: {
        scope: '*',
        expectedRevision: 1,
        settings,
        reason: 'enforce only after the bounded shadow audit passed',
        confirmation: 'APPLY V9 CONFIRMATION MODE ENFORCE',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 2, settings: { mode: 'enforce' } },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/confirmation-bundle-settings',
    )
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer platform-admin-token',
      'X-Admin-Actor': 'peyton@omniaura.ai',
    })
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 1,
      settings,
      reason: 'enforce only after the bounded shadow audit passed',
      actor: 'peyton@omniaura.ai',
      confirmation: 'APPLY V9 CONFIRMATION MODE ENFORCE',
    })

    await client.close()
    await server.close()
  })

  it('keeps detailed operational help available without loading it in the catalog', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_backroom_tool_help',
      arguments: { tool: 'set_queue_policy_settings' },
    })

    expect(response.isError).not.toBe(true)
    const payload = readJsonResult(response) as { tool: string; guidance: string }
    expect(payload.tool).toBe('set_queue_policy_settings')
    expect(payload.guidance.length).toBeGreaterThan(3_000)
    expect(payload.guidance).toContain('APPLY QUEUE POLICY SETTINGS')
    expect(payload.guidance).toContain('deferred_source_review')

    await client.close()
    await server.close()
  })

  it('omits settings history by default and pages it newest-first on demand', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const revision = (number: number, createdDay = number) => ({
      revision: number,
      parent_revision: Math.max(0, number - 1),
      disclosure: 'public',
      embargo_hours: 48,
      reason: `policy revision ${number}`,
      actor: 'peyton@omniaura.ai',
      created_at: `2026-07-0${createdDay}T12:00:00Z`,
    })
    const control = {
      current: revision(3),
      // Deliberately unordered: the MCP contract, not upstream incidental
      // ordering, guarantees newest-first audit pages.
      // Revision 1 was backfilled after revisions 2 and 3. Timestamp order,
      // rather than revision order, must win.
      history: [revision(1, 5), revision(3, 3), revision(2, 4)],
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(Response.json(control))
      .mockResolvedValueOnce(
        Response.json({
          current: { ...revision(4), created_at: null },
          history: [2, 4, 3].map((number) => ({
            ...revision(number),
            created_at: null,
          })),
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const currentOnly = await client.callTool({
      name: 'get_source_release_policy',
      arguments: {},
    })
    expect(readJsonResult(currentOnly)).toMatchObject({
      current: { revision: 3 },
      history: [],
      history_count: 3,
      history_limit: 0,
      history_offset: 0,
      history_has_more: true,
    })

    const historyPage = await client.callTool({
      name: 'get_source_release_policy',
      arguments: { historyLimit: 2, historyOffset: 0 },
    })
    expect(readJsonResult(historyPage)).toMatchObject({
      history: [{ revision: 1 }, { revision: 2 }],
      history_count: 3,
      history_limit: 2,
      history_offset: 0,
      history_has_more: true,
    })

    const nullTimestampPage = await client.callTool({
      name: 'get_source_release_policy',
      arguments: { historyLimit: 3 },
    })
    expect(readJsonResult(nullTimestampPage)).toMatchObject({
      history: [{ revision: 4 }, { revision: 3 }, { revision: 2 }],
      history_count: 3,
      history_has_more: false,
    })

    await client.close()
    await server.close()
  })

  it('publishes bounded pagination inputs for every MCP collection page', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.listTools()
    // Page sizes are bounded so one call cannot flood model context. The
    // source manifest is the deliberate exception: its rows are a path and a
    // byte count, and a row silently left off page one is a file the operator
    // never learns exists, which is a worse failure than the context a whole
    // 512-row manifest costs. Its bound is the platform's own listing cap.
    const paginatedTools: Record<
      string,
      { maxLimit: number; maxDefault: number }
    > = {
      get_screening_review_queue: { maxLimit: 200, maxDefault: 50 },
      list_screening_quarantines: { maxLimit: 200, maxDefault: 50 },
      list_screening_disputes: { maxLimit: 200, maxDefault: 50 },
      list_screening_source_files: { maxLimit: 512, maxDefault: 512 },
      list_screening_submissions: { maxLimit: 200, maxDefault: 50 },
      search_screening_source: { maxLimit: 200, maxDefault: 50 },
      list_stuck_submissions: { maxLimit: 200, maxDefault: 10 },
      list_lease_revocations: { maxLimit: 200, maxDefault: 50 },
      list_hotkey_bans: { maxLimit: 200, maxDefault: 50 },
      get_leaderboard: { maxLimit: 200, maxDefault: 50 },
      get_validator_fleet: { maxLimit: 200, maxDefault: 50 },
      list_validator_assignments: { maxLimit: 200, maxDefault: 50 },
    }

    for (const [name, bounds] of Object.entries(paginatedTools)) {
      const tool = response.tools.find((candidate) => candidate.name === name)
      const properties = tool?.inputSchema?.properties as
        | Record<
            string,
            {
              default?: unknown
              maximum?: number
              minimum?: number
            }
          >
        | undefined

      expect(tool, `${name} must stay registered`).toBeDefined()
      expect(properties?.limit, `${name} must publish a limit`).toMatchObject({
        minimum: 1,
      })
      expect(properties?.limit?.maximum, `${name} must cap its page size`).toBeLessThanOrEqual(
        bounds.maxLimit,
      )
      expect(
        properties?.limit?.default,
        `${name} must default to a context-safe page`,
      ).toBeLessThanOrEqual(bounds.maxDefault)
      expect(properties?.offset, `${name} must publish an offset`).toMatchObject({
        minimum: 0,
        default: 0,
      })
    }

    await client.close()
    await server.close()
  })

  it('makes every settings audit history opt-in and bounded', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.listTools()
    const settingsReads = [
      'get_efficiency_bonus_settings',
      'get_source_release_policy',
      'get_submission_cooldown',
      'get_continual_retest_settings',
      'get_queue_policy_settings',
      'get_validator_slot_settings',
      'get_inference_concurrency_settings',
    ]

    for (const name of settingsReads) {
      const tool = response.tools.find((candidate) => candidate.name === name)
      const properties = tool?.inputSchema?.properties as
        | Record<string, { default?: unknown; maximum?: number; minimum?: number }>
        | undefined
      expect(properties?.historyLimit, name).toMatchObject({
        minimum: 0,
        maximum: 50,
        default: 0,
      })
      expect(properties?.historyOffset, name).toMatchObject({ minimum: 0, default: 0 })
    }

    for (const name of ['list_screening_submissions', 'list_screening_quarantines']) {
      const tool = response.tools.find((candidate) => candidate.name === name)
      const properties = tool?.inputSchema?.properties as
        | Record<string, { default?: unknown; enum?: Array<string> }>
        | undefined
      expect(properties?.detail, name).toMatchObject({
        default: 'summary',
        enum: ['summary', 'full'],
      })
    }

    await client.close()
    await server.close()
  })

  it('reports the staff identity and read-only grant without network access', async () => {
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_backroom_access',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      accessLevel: 'read-only',
      scopes: [BACKROOM_READ_SCOPE],
      user: { email: 'peyton@omniaura.ai' },
    })
    expect(response.structuredContent).toBeUndefined()
    expect(readTextResult(response)).not.toContain('\n')

    await client.close()
    await server.close()
  })

  it('defense-in-depth rejects a write tool when the grant is read-only', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'resolve_screening_quarantine',
      arguments: {
        quarantineId: '11111111-1111-4111-8111-111111111111',
        resolution: 'reject',
        reason: 'a read-only grant must never reach this call',
      },
    })

    expect(response.isError).toBe(true)
    expect(response.content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: expect.stringContaining('read-only') }),
      ]),
    )
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('keeps batch preview read-only and batch execution write-scoped', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const decision = {
      quarantineId: '11111111-1111-4111-8111-111111111111',
      expectedAgentId: '22222222-2222-4222-8222-222222222222',
      expectedArtifactSha256: 'ab'.repeat(32),
      resolution: 'rescreen',
      reason: 'Run the preserved artifact against the current policy',
    }
    const previewPayload = {
      preview_token: `1234567890.${'a'.repeat(64)}`,
      expires_at: '2026-07-17T16:10:00Z',
      items: [
        {
          quarantine_id: decision.quarantineId,
          agent_id: decision.expectedAgentId,
          agent_name: 'review-agent',
          artifact_sha256: decision.expectedArtifactSha256,
          resolution: decision.resolution,
          reason: decision.reason,
          disposition: 'ready',
          resulting_agent_status: 'screening_failed',
          message: 'will set submission status to screening_failed',
        },
      ],
      ready_count: 1,
      already_applied_count: 0,
      blocked_count: 0,
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(previewPayload))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const preview = await client.callTool({
      name: 'preview_screening_quarantine_batch',
      arguments: { decisions: [decision] },
    })
    expect(preview.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/batch-preview')

    const execute = await client.callTool({
      name: 'execute_screening_quarantine_batch',
      arguments: {
        decisions: [decision],
        previewToken: previewPayload.preview_token,
        confirmed: true,
      },
    })
    expect(execute.isError).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await client.close()
    await server.close()
  })

  it('inspects and refreshes a benchmark contract with exact MCP guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const artifactSha256 = 'ab'.repeat(32)
    const datasetSha256 = 'cd'.repeat(32)
    const detail = {
      agent_id: agentId,
      agent_name: 'stale-v3-agent',
      agent_status: 'evaluating',
      artifact_sha256: artifactSha256,
      bench_version: 3,
      dataset_sha256: datasetSha256,
      score_count: 0,
      screening_attempt_active: false,
      refresh_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(
        Response.json({
          agent_id: agentId,
          agent_status: 'screening_failed',
          bench_version: 3,
          expired_ticket_count: 1,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const inspection = await client.callTool({
      name: 'get_benchmark_contract_refresh',
      arguments: { agentId },
    })
    expect(inspection.isError).not.toBe(true)
    expect(readJsonResult(inspection)).toMatchObject({
      agent_id: agentId,
      refresh_allowed: true,
    })

    const refresh = await client.callTool({
      name: 'refresh_benchmark_contract',
      arguments: {
        agentId,
        expectedSha256: artifactSha256,
        expectedBenchVersion: 3,
        expectedDatasetSha256: datasetSha256,
        expectedScoreCount: 0,
        reason: 'Confirmed generator and validator dataset drift',
      },
    })
    expect(refresh.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/refresh-benchmark-contract`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          reason: 'Confirmed generator and validator dataset drift',
          expected_sha256: artifactSha256,
          expected_bench_version: 3,
          expected_dataset_sha256: datasetSha256,
          expected_score_count: 0,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('inspects and rebuilds only a stale screened image with exact MCP guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const artifactSha256 = 'ab'.repeat(32)
    const imageSha256 = 'cd'.repeat(32)
    const imageUploadId = '22345678-1234-4234-8234-123456789012'
    const detail = {
      agent_id: agentId,
      agent_name: 'stale-image-agent',
      agent_status: 'evaluating',
      artifact_sha256: artifactSha256,
      bench_version: 8,
      score_count: 0,
      screened_image_sha256: imageSha256,
      screened_image_upload_id: imageUploadId,
      screening_attempt_active: false,
      validator_ticket_active: true,
      rebuild_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(Response.json({
        agent_id: agentId,
        agent_status: 'evaluating',
        bench_version: 8,
        expired_ticket_count: 3,
      }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const inspection = await client.callTool({
      name: 'get_screened_image_rebuild',
      arguments: { agentId },
    })
    expect(inspection.isError).not.toBe(true)
    expect(readJsonResult(inspection)).toMatchObject({
      agent_id: agentId,
      rebuild_allowed: true,
    })

    const rebuild = await client.callTool({
      name: 'rebuild_screened_image',
      arguments: {
        agentId,
        expectedSha256: artifactSha256,
        expectedBenchVersion: 8,
        expectedScoreCount: 0,
        expectedImageSha256: imageSha256,
        expectedImageUploadId: imageUploadId,
        reason: 'Healthy validators reject this legacy image archive',
      },
    })
    expect(rebuild.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/rebuild-screened-image`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          reason: 'Healthy validators reject this legacy image archive',
          expected_sha256: artifactSha256,
          expected_bench_version: 8,
          expected_score_count: 0,
          expected_image_sha256: imageSha256,
          expected_image_upload_id: imageUploadId,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('inspects and replaces one validator score with exact MCP guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const validatorHotkey = '5Validator'
    const requestId = '11111111-1111-4111-8111-111111111111'
    const snapshot = 'ab'.repeat(32)
    const detail = {
      agent_id: agentId,
      validator_hotkey: validatorHotkey,
      agent_status: 'scored',
      bench_version: 4,
      score_count: 3,
      quorum: 3,
      snapshot,
      run_id: 'run-123',
      composite: 0.488,
      ticket_status: 'scored',
      ticket_deadline: '2026-07-20T04:00:00Z',
      replacement_pending: false,
      replacement_request_id: null,
      replacement_reason: null,
      replacement_actor: null,
      replacement_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(
        Response.json({
          request_id: requestId,
          agent_id: agentId,
          validator_hotkey: validatorHotkey,
          original_run_id: 'run-123',
          bench_version: 4,
          replacement_deadline: '2026-07-20T05:30:00Z',
          preserved_score_count: 3,
          idempotent: false,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const inspection = await client.callTool({
      name: 'get_validator_score_replacement',
      arguments: { agentId, validatorHotkey },
    })
    expect(inspection.isError).not.toBe(true)
    expect(readJsonResult(inspection)).toMatchObject({
      agent_id: agentId,
      validator_hotkey: validatorHotkey,
      replacement_allowed: true,
      run_id: 'run-123',
    })

    const replacement = await client.callTool({
      name: 'replace_validator_score',
      arguments: {
        agentId,
        validatorHotkey,
        expectedSnapshot: snapshot,
        expectedRunId: 'run-123',
        reason: 'Verified validator relay failure corrupted this run',
      },
    })
    expect(replacement.isError).not.toBe(true)
    expect(readJsonResult(replacement)).toMatchObject({
      original_run_id: 'run-123',
      preserved_score_count: 3,
      idempotent: false,
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/validators/${validatorHotkey}/replace-score`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('previews and queues exact v9 contract repairs through MCP', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const validatorHotkey = '5Validator'
    const snapshot = 'ab'.repeat(32)
    const runId = 'run-shadow'
    const preview = {
      items: [{
        agent_id: agentId,
        agent_name: 'white-bolt',
        miner_hotkey: '5Miner',
        agent_status: 'scored',
        validator_hotkey: validatorHotkey,
        run_id: runId,
        composite: 0.99,
        snapshot,
        observed_revision: 'v9-base-shadow-calibration-v1',
        observed_manifest_sha256: 'cd'.repeat(32),
        observed_rollout_mode: 'shadow',
        semantic_gate_factor_bps: 0,
        ticket_status: 'expired',
        replacement_pending: false,
        replacement_queued: false,
        queue_position: null,
        queue_allowed: true,
        queue_blocking_reason: null,
      }],
      count: 1,
      limit: 100,
      offset: 0,
      required_revision: 'v9-base-enforce-efficiency-v1',
      required_manifest_sha256: 'ef'.repeat(32),
      required_rollout_mode: 'enforce',
    }
    const queued = {
      validator_hotkey: validatorHotkey,
      activated: 0,
      queued: 1,
      idempotent: 0,
      skipped: 0,
      results: [{
        agent_id: agentId,
        request_id: '11111111-1111-4111-8111-111111111111',
        status: 'queued',
        detail: null,
        queue_position: 1,
      }],
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(preview))
      .mockResolvedValueOnce(Response.json(queued))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const inspection = await client.callTool({
      name: 'list_v9_contract_retests',
      arguments: { limit: 100, offset: 0 },
    })
    expect(inspection.isError).not.toBe(true)

    const queue = await client.callTool({
      name: 'queue_validator_score_retests',
      arguments: {
        validatorHotkey,
        basis: 'v9_contract_mismatch',
        confirmation: 'QUEUE V9 CONTRACT RETESTS',
        reason: 'Restore authoritative v9 evidence after continual ticket reuse',
        items: [{
          agentId,
          expectedSnapshot: snapshot,
          expectedRunId: runId,
        }],
      },
    })
    expect(queue.isError).not.toBe(true)
    expect(readJsonResult(queue)).toMatchObject({ queued: 1, skipped: 0 })
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/v9-contract-retests?limit=100&offset=0',
      expect.any(Object),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/validators/${validatorHotkey}/queue-score-retests`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('expands an open rollout cohort with exact MCP guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        active_version: 7,
        desired_version: 8,
        status: 'collecting',
        blocked_reason: null,
        capability_bench_version: 8,
        canary_capable_validator_count: 4,
        v3_capable_validator_count: 4,
        current_hybrid_top_five: [],
        qualification_converged: false,
        members: [],
        expansion: { previous_target: 10, new_target: 15, appended_members: 5 },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'expand_benchmark_rollout_cohort',
      arguments: {
        desiredVersion: 8,
        expectedActiveVersion: 7,
        expectedCurrentTarget: 10,
        newTarget: 15,
        reason: 'restore the intended top fifteen rollout cohort',
        confirmation: 'EXPAND BENCHMARK V8 TO 15',
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      expansion: { previous_target: 10, new_target: 15, appended_members: 5 },
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/8/expand',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('reads rollout control without starting a transition', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        active_version: 11,
        desired_version: 11,
        status: 'activated',
        blocked_reason: null,
        capability_bench_version: 12,
        canary_capable_validator_count: 5,
        v3_capable_validator_count: 5,
        current_hybrid_top_five: ['11111111-1111-4111-8111-111111111111'],
        qualification_converged: true,
        members: [
          {
            agent_id: '11111111-1111-4111-8111-111111111111',
            position: 1,
            score_count: 3,
            currently_top_five: true,
          },
        ],
        contracts: [
          {
            version: 11,
            minimum_screening_policy_version: 9,
            requires_screened_image: true,
            capable_validator_count: 5,
            start_ready: false,
            start_blockers: [],
          },
          {
            version: 12,
            minimum_screening_policy_version: 9,
            requires_screened_image: true,
            capable_validator_count: 5,
            start_ready: true,
            start_blockers: [],
          },
        ],
        available_target_versions: [12],
        active_contract_candidates: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_benchmark_rollout_control',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      active_version: 11,
      desired_version: 11,
      available_target_versions: [12],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout',
      expect.objectContaining({
        method: 'GET',
      }),
    )

    await client.close()
    await server.close()
  })

  it('starts a selected rollout with CAS, reason, and confirmation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ status: 'collecting' }))
      .mockResolvedValueOnce(
        Response.json({
          active_version: 11,
          desired_version: 12,
          status: 'collecting',
          blocked_reason: null,
          capability_bench_version: 12,
          canary_capable_validator_count: 5,
          v3_capable_validator_count: 5,
          current_hybrid_top_five: [],
          qualification_converged: false,
          members: [],
          contracts: [
            {
              version: 12,
              minimum_screening_policy_version: 9,
              requires_screened_image: true,
              capable_validator_count: 5,
              start_ready: true,
              start_blockers: [],
            },
          ],
          available_target_versions: [12],
          active_contract_candidates: [],
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'start_benchmark_rollout',
      arguments: {
        desiredVersion: 12,
        expectedActiveVersion: 11,
        reason: 'v12 fleet advertises 12; unseat the v11 king',
        confirmation: 'START BENCHMARK V12',
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      active_version: 11,
      desired_version: 12,
      status: 'collecting',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/benchmark-rollout/12',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('inspects and qualifies a scored rollout member with exact MCP guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const rolloutId = '8f734f51-fde1-4cbb-8f43-15e6882804f4'
    const artifactSha256 = 'ab'.repeat(32)
    const datasetSha256 = 'cd'.repeat(32)
    const detail = {
      agent_id: agentId,
      agent_name: 'legacy-champion',
      agent_status: 'scored',
      artifact_sha256: artifactSha256,
      rollout_id: rolloutId,
      source_bench_version: 2,
      target_bench_version: 3,
      currently_top_five: true,
      rollout_member: false,
      target_dataset_sha256: null,
      total_score_count: 3,
      source_score_count: 3,
      target_score_count: 0,
      screening_attempt_active: false,
      validator_run_active: false,
      qualification_allowed: true,
      blocking_reason: null,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(detail))
      .mockResolvedValueOnce(
        Response.json({
          agent_id: agentId,
          agent_status: 'scored',
          rollout_id: rolloutId,
          target_bench_version: 3,
          target_dataset_sha256: datasetSha256,
          rollout_member: true,
          screening_queued: true,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const inspection = await client.callTool({
      name: 'get_benchmark_rollout_qualification',
      arguments: { agentId },
    })
    expect(inspection.isError).not.toBe(true)
    expect(readJsonResult(inspection)).toMatchObject({
      agent_id: agentId,
      qualification_allowed: true,
    })

    const qualification = await client.callTool({
      name: 'qualify_scored_benchmark_rollout',
      arguments: {
        agentId,
        expectedSha256: artifactSha256,
        expectedRolloutId: rolloutId,
        expectedTotalScoreCount: 3,
        expectedSourceScoreCount: 3,
        expectedTargetScoreCount: 0,
        reason: 'Current hybrid champion requires policy v9 and benchmark v3',
      },
    })
    expect(qualification.isError).not.toBe(true)
    expect(readJsonResult(qualification)).toMatchObject({
      agent_id: agentId,
      rollout_member: true,
      screening_queued: true,
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/qualify-benchmark-rollout`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          reason: 'Current hybrid champion requires policy v9 and benchmark v3',
          expected_sha256: artifactSha256,
          expected_rollout_id: rolloutId,
          expected_total_score_count: 3,
          expected_source_score_count: 3,
          expected_target_score_count: 0,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('does not qualify a scored rollout member without write access', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
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
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('reads the efficiency bonus policy still governed by the deployment seed', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const seed = {
      enabled: false,
      fold_enabled: false,
      cap: 0.05,
      deep_cap: 0.1,
      deep_frontier_ratio: 0.5,
      factor_alpha: 0.25,
      minimum_factor: 0.85,
      maximum_factor: 1.1,
      cohort_size: 25,
      min_cohort: 8,
      epoch_hours: 24,
      quality_floor: 0,
      memory_floor: 0,
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        current: [],
        history: [],
        seed_default: seed,
        effective: {
          revision: 0,
          scope: '*',
          settings: seed,
          checksum_settings: seed,
          checksum: '',
          source: 'seed',
          fold_effective: false,
          max_age_seconds: 5,
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_efficiency_bonus_settings',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      current: [],
      effective: { revision: 0, source: 'seed', fold_effective: false },
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/efficiency-bonus-settings',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('reads screener review settings through a read-only grant', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        current: [],
        history: [],
        known_instances: ['ditto-screener-prod'],
        applied_instances: [],
        shadow_observations: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screener_review_settings',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      known_instances: ['ditto-screener-prod'],
      applied_instances: [],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screener-review-settings',
      expect.any(Object),
    )
    await client.close()
    await server.close()
  })

  it('reads authoritative screener capacity and provider state', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ snapshot: null, nodes: [], events: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screener_capacity',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      snapshot: null,
      nodes: [],
      provider_control: {
        current: {
          revision: 0,
          settings: {
            build_provider_priority: ['targon'],
            runtime_provider_priority: ['targon'],
            source_review_provider_priority: ['targon'],
          },
        },
      },
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screener-capacity',
      expect.any(Object),
    )
    await client.close()
    await server.close()
  })

  it('reads the emission burn and the miner share it leaves', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const control = {
      current: [],
      history: [],
      default: { burn_share: 0 },
      effective: {
        revision: 0,
        scope: '*',
        settings: { burn_share: 0 },
        checksum: '',
        source: 'default',
        max_age_seconds: 5,
        miner_emission_share: 1,
        min_burn_share: 0,
        max_burn_share: 1,
        live_validator_count: 3,
      },
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({ name: 'get_burn_settings', arguments: {} })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 0, source: 'default', miner_emission_share: 1 },
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/burn-settings',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('applies one burn revision and attributes it to the connected operator', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const control = (burn_share: number, revision: number) => ({
      current: [],
      history: [],
      default: { burn_share: 0 },
      effective: {
        revision,
        scope: '*',
        settings: { burn_share },
        checksum: revision === 0 ? '' : 'ab'.repeat(32),
        source: revision === 0 ? 'default' : 'revision',
        max_age_seconds: 5,
        miner_emission_share: 1 - burn_share,
        min_burn_share: 0,
        max_burn_share: 1,
        live_validator_count: 3,
      },
    })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({
          revision: 1,
          parent_revision: 0,
          scope: '*',
          settings: { burn_share: 0.25 },
          reason: 'owner-approved emission burn change',
          actor: 'peyton@omniaura.ai',
          created_at: '2026-08-08T12:00:00Z',
          checksum: 'ab'.repeat(32),
        }),
      )
      .mockResolvedValueOnce(Response.json(control(0.25, 1)))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_burn_settings',
      arguments: {
        expectedRevision: 0,
        settings: { burn_share: 0.25 },
        reason: 'owner-approved emission burn change',
        confirmation: 'APPLY BURN SETTINGS',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 1, settings: { burn_share: 0.25 }, miner_emission_share: 0.75 },
    })
    // The signed-in operator, not a shared service identity, is what the
    // platform records as the actor on a revision that moves TAO.
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://platform-api.heyditto.ai/api/v1/admin/burn-settings',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          scope: '*',
          expected_revision: 0,
          settings: { burn_share: 0.25 },
          reason: 'owner-approved emission burn change',
          actor: 'peyton@omniaura.ai',
          confirmation: 'APPLY BURN SETTINGS',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('applies one efficiency bonus revision with the exact platform contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      enabled: true,
      fold_enabled: false,
      cap: 0.05,
      deep_cap: 0.1,
      deep_frontier_ratio: 0.5,
      factor_alpha: 0.25,
      minimum_factor: 0.85,
      maximum_factor: 1.1,
      cohort_size: 25,
      min_cohort: 8,
      epoch_hours: 24,
      quality_floor: 0,
      memory_floor: 0,
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        revision: 1,
        parent_revision: 0,
        scope: '*',
        settings,
        checksum_settings: settings,
        reason: 'Enable the v7 efficiency bonus and watch the shadow board',
        actor: 'peyton@omniaura.ai',
        created_at: '2026-07-23T12:00:00Z',
        checksum: 'ab'.repeat(32),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'set_efficiency_bonus_settings',
      arguments: {
        scope: '*',
        expectedRevision: 0,
        settings,
        reason: 'Enable the v7 efficiency bonus and watch the shadow board',
        confirmation: 'APPLY EFFICIENCY BONUS ENABLED',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      revision: 1,
      parent_revision: 0,
      scope: '*',
      settings: { enabled: true, fold_enabled: false },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/efficiency-bonus-settings',
    )
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer platform-admin-token',
      'X-Admin-Actor': 'peyton@omniaura.ai',
    })
    // The audited actor is always the signed-in operator, never a tool argument.
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 0,
      settings,
      reason: 'Enable the v7 efficiency bonus and watch the shadow board',
      actor: 'peyton@omniaura.ai',
      confirmation: 'APPLY EFFICIENCY BONUS ENABLED',
    })

    await client.close()
    await server.close()
  })

  it('surfaces a stale efficiency bonus revision as a recoverable conflict', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json(
        {
          detail:
            'efficiency bonus settings changed; refresh before applying (expected 0, current 2)',
        },
        { status: 409 },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'set_efficiency_bonus_settings',
      arguments: {
        expectedRevision: 0,
        settings: {
          enabled: true,
          fold_enabled: true,
          cap: 0.05,
          deep_cap: 0.1,
          deep_frontier_ratio: 0.5,
          factor_alpha: 0.25,
          minimum_factor: 0.85,
          maximum_factor: 1.1,
          cohort_size: 25,
          min_cohort: 8,
          epoch_hours: 24,
          quality_floor: 0,
          memory_floor: 0,
        },
        reason: 'Fold the efficiency bonus into validator weights',
        confirmation: 'APPLY EFFICIENCY BONUS ENABLED',
      },
    })

    expect(response.isError).toBe(true)
    const message = readTextResult(response)
    expect(message).toContain('expected 0, current 2')
    expect(message).toContain('Nothing was applied')
    expect(message).toContain('get_efficiency_bonus_settings')

    await client.close()
    await server.close()
  })

  it('rejects an efficiency bonus revision with the wrong confirmation before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
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
        confirmation: 'APPLY EFFICIENCY BONUS DISABLED',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('does not change subnet scoring policy without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
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
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('reads the queue policy governed by an open rollout it cannot resize', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      rescore_cohort_size: 20,
      priority_cohort_size: 8,
      lane_cycle_size: 4,
      fresh_submission_slots: [0, 1, 3],
      prev_gen_carryover: {
        enabled: false,
        max_agents: 10,
        min_score_count: 2,
        include_exhausted: false,
        dedupe_scope: 'coldkey',
        require_cohort_complete: true,
        require_desired_era_drained: true,
      },
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        current: [],
        history: [],
        default: settings,
        effective: {
          revision: 3,
          scope: '*',
          settings,
          checksum: 'cd'.repeat(32),
          source: 'revision',
          open_rollout_desired_version: 8,
          open_rollout_rescore_cohort_target: 10,
          open_rollout_priority_cohort_target: 5,
          open_rollout_overrides_setting: true,
          rollout_locked_fields: ['lane_cycle_size', 'fresh_submission_slots'],
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_queue_policy_settings',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: {
        revision: 3,
        source: 'revision',
        open_rollout_desired_version: 8,
        open_rollout_rescore_cohort_target: 10,
        open_rollout_overrides_setting: true,
      },
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/queue-policy-settings',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('applies one queue policy revision with the exact platform contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      rescore_cohort_size: 15,
      priority_cohort_size: 6,
      lane_cycle_size: 5,
      fresh_submission_slots: [0, 1, 3],
      owner_concurrent_submission_limit: 2,
      deferred_source_review: {
        mode: 'off',
        min_cohort_size: 8,
        composite_mad_multiplier: 6,
        axis_mad_multiplier: 6,
        min_composite_delta: 0.1,
        min_axis_delta: 0.15,
      },
      similarity_budget: {
        enabled: true,
        concurrent_submission_limit: 1,
        jaccard_threshold: 0.9,
        containment_threshold: 0.95,
      },
      prev_gen_carryover: {
        enabled: true,
        max_agents: 8,
        min_score_count: 2,
        include_exhausted: false,
        dedupe_scope: 'coldkey',
        require_cohort_complete: true,
        require_desired_era_drained: true,
      },
    }
    const control = {
      current: [],
      history: [],
      default: settings,
      effective: {
        revision: 4,
        scope: '*',
        settings,
        checksum: 'ef'.repeat(32),
        source: 'revision',
        open_rollout_desired_version: null,
        open_rollout_rescore_cohort_target: null,
        open_rollout_priority_cohort_target: null,
        open_rollout_overrides_setting: false,
        rollout_locked_fields: [],
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ revision: 4 }))
      .mockResolvedValueOnce(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_queue_policy_settings',
      arguments: {
        scope: '*',
        expectedRevision: 3,
        settings,
        reason: 'admit stranded prior-generation submissions for one wave',
        confirmation: 'APPLY QUEUE POLICY SETTINGS',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 4, source: 'revision' },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('https://platform-api.heyditto.ai/api/v1/admin/queue-policy-settings')
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer platform-admin-token',
      'X-Admin-Actor': 'peyton@omniaura.ai',
    })
    // The audited actor is always the signed-in operator, never a tool argument.
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 3,
      settings,
      reason: 'admit stranded prior-generation submissions for one wave',
      actor: 'peyton@omniaura.ai',
      confirmation: 'APPLY QUEUE POLICY SETTINGS',
    })

    await client.close()
    await server.close()
  })

  it('surfaces the open-rollout lane refusal verbatim with its recovery', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const detail =
      'lane_cycle_size cannot change while benchmark rollout 8 is open: the lane counter is completed jobs since rollout start mod N'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ detail }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_queue_policy_settings',
      arguments: {
        expectedRevision: 3,
        settings: {
          rescore_cohort_size: 10,
          priority_cohort_size: 5,
          lane_cycle_size: 6,
          fresh_submission_slots: [0, 1, 2, 4],
          owner_concurrent_submission_limit: 2,
          deferred_source_review: {
            mode: 'off',
            min_cohort_size: 8,
            composite_mad_multiplier: 6,
            axis_mad_multiplier: 6,
            min_composite_delta: 0.1,
            min_axis_delta: 0.15,
          },
          similarity_budget: {
            enabled: true,
            concurrent_submission_limit: 1,
            jaccard_threshold: 0.9,
            containment_threshold: 0.95,
          },
          prev_gen_carryover: {
            enabled: false,
            max_agents: 10,
            min_score_count: 2,
            include_exhausted: false,
            dedupe_scope: 'coldkey',
            require_cohort_complete: true,
            require_desired_era_drained: true,
          },
        },
        reason: 'lengthen the lane cycle for the onboarding wave',
        confirmation: 'APPLY QUEUE POLICY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    const message = readTextResult(response)
    expect(message).toContain(detail)
    expect(message).toContain('rollout_locked_fields')
    expect(message).toContain('Nothing was applied')

    await client.close()
    await server.close()
  })

  // The hosted v7 inference admission board. It existed on the platform from
  // ditto-platform#477 and was unreachable from backroom until this suite.
  const inferenceConcurrencySettings = {
    chat_request_budget: 8192,
    chat_token_budget: 25_000_000,
    chat_per_ticket_concurrency: 16,
    chat_per_validator_concurrency: 48,
    chat_global_concurrency: 96,
    embedding_per_ticket_concurrency: 12,
    embedding_per_validator_concurrency: 48,
    embedding_global_concurrency: 96,
    chat_per_ticket_requests_per_minute: 1920,
    chat_per_validator_requests_per_minute: 7680,
    chat_global_requests_per_minute: 23040,
    embedding_per_ticket_requests_per_minute: 10_000,
    embedding_per_validator_requests_per_minute: 40_000,
    embedding_global_requests_per_minute: 100_000,
    benchmark_runtime: {
      case_concurrency: 4,
      relay_delay_fingerprint_mode: 'off' as const,
      relay_delay_fingerprint_min_ms: 25,
      relay_delay_fingerprint_max_ms: 250,
    },
  }

  it('reads the hosted inference admission policy in force', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        current: [],
        history: [],
        default: inferenceConcurrencySettings,
        effective: {
          revision: 0,
          scope: '*',
          settings: inferenceConcurrencySettings,
          checksum: '',
          source: 'default',
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_inference_concurrency_settings',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: {
        revision: 0,
        source: 'default',
        settings: { chat_token_budget: 25_000_000 },
      },
    })
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/inference-concurrency-settings',
    )

    await client.close()
    await server.close()
  })

  it('reads current and recent inference runtime pressure', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        observed_at: '2026-08-15T19:26:00Z',
        settings_revision: 14,
        settings_checksum: 'ab'.repeat(32),
        lanes: [
          {
            request_kind: 'chat',
            active_requests: 5,
            live_grants: 12,
            stale_started_requests: 11,
            per_ticket_limit: 32,
            per_validator_limit: 256,
            global_limit: 512,
            per_ticket_rpm_limit: 1920,
            per_validator_rpm_limit: 7680,
            global_rpm_limit: 23040,
            peak_per_ticket_concurrency_60m: 1,
            peak_per_validator_concurrency_60m: 5,
            peak_global_concurrency_60m: 11,
          },
        ],
        windows: [
          {
            window_seconds: 60,
            request_kind: 'chat',
            calls: 93,
            calls_per_second: 1.55,
            tokens: 1_859_056,
            tokens_per_second: 30_984.3,
            completed: 90,
            failed: 0,
            canceled: 0,
            timed_out: 0,
            latency_p50_ms: 1953,
            latency_p95_ms: 9995,
            latency_max_ms: 10_738,
            peak_global_concurrency: 6,
          },
        ],
        relays: [
          {
            target: 'platform-relay-1',
            status: 'ok',
            source_revision: 'ab'.repeat(20),
            checked_out_revision: 'ab'.repeat(20),
            revision_drift: false,
            process_started_at: '2026-08-15T15:41:56Z',
            capacity_declines: {},
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_inference_runtime_metrics',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      settings_revision: 14,
      lanes: [{ request_kind: 'chat', peak_global_concurrency_60m: 11 }],
      windows: [{ calls_per_second: 1.55, latency_p95_ms: 9995 }],
    })

    await client.close()
    await server.close()
  })

  const runtimeProfile = {
    profile_id: '11111111-1111-4111-8111-111111111111',
    target: 'platform-relay-1',
    profile_type: 'cpu',
    seconds: 15,
    source_revision: 'ab'.repeat(20),
    checked_out_revision: 'ab'.repeat(20),
    revision_drift: false,
    actor: 'peyton@omniaura.ai',
    reason: 'investigate slow benchmark runs',
    created_at: '2026-08-15T19:26:00Z',
    expires_at: '2026-08-15T19:41:00Z',
    byte_size: 5,
    sha256: '137a5d59256c9738cc9c854fcce790757623d7ce2df7bbafe972d45dbd46ee80',
    media_type: 'application/octet-stream',
    filename: 'platform-relay-1-cpu.pb.gz',
    download_path:
      '/api/v1/admin/runtime-profiles/11111111-1111-4111-8111-111111111111/download',
  }

  it('starts a bounded audited runtime profile capture', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(runtimeProfile))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'start_runtime_profile',
      arguments: {
        target: 'platform-relay-1',
        profileType: 'cpu',
        seconds: 15,
        reason: 'investigate slow benchmark runs',
        confirmation: 'CAPTURE RUNTIME PROFILE',
      },
    })

    expect(response.isError).not.toBe(true)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('https://platform-api.heyditto.ai/api/v1/admin/runtime-profiles')
    expect(init.headers).toMatchObject({ 'X-Admin-Actor': 'peyton@omniaura.ai' })
    expect(JSON.parse(String(init.body))).toEqual({
      target: 'platform-relay-1',
      profile_type: 'cpu',
      seconds: 15,
      reason: 'investigate slow benchmark runs',
      confirmation: 'CAPTURE RUNTIME PROFILE',
    })

    await client.close()
    await server.close()
  })

  it('downloads a checksum-pinned profile only through artifact scope', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(runtimeProfile))
      .mockResolvedValueOnce(
        new Response(new TextEncoder().encode('pprof'), {
          status: 200,
          headers: {
            'Content-Type': 'application/octet-stream',
            'X-Profile-SHA256': runtimeProfile.sha256,
          },
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_ARTIFACT_SCOPE,
    ])

    const response = await client.callTool({
      name: 'download_runtime_profile',
      arguments: { profileId: runtimeProfile.profile_id },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      encoding: 'base64',
      data_base64: 'cHByb2Y=',
      profile: { profile_id: runtimeProfile.profile_id },
    })

    await client.close()
    await server.close()
  })

  it('applies one inference concurrency revision with the exact platform contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    // The move the incident wanted: raise the token budget, leave the rest.
    const nextSettings = { ...inferenceConcurrencySettings, chat_token_budget: 40_000_000 }
    const control = {
      current: [],
      history: [],
      default: inferenceConcurrencySettings,
      effective: {
        revision: 5,
        scope: '*',
        settings: nextSettings,
        checksum: 'ab'.repeat(32),
        source: 'revision',
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ revision: 5 }))
      .mockResolvedValueOnce(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_inference_concurrency_settings',
      arguments: {
        scope: '*',
        expectedRevision: 4,
        settings: nextSettings,
        reason: 'let the heaviest v7 strategies finish a full run',
        confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 5, source: 'revision' },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/inference-concurrency-settings',
    )
    expect(init.method).toBe('POST')
    // The audited actor is always the signed-in operator, never a tool argument.
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 4,
      settings: nextSettings,
      reason: 'let the heaviest v7 strategies finish a full run',
      actor: 'peyton@omniaura.ai',
      confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
    })

    await client.close()
    await server.close()
  })

  it('refuses a partial inference policy before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    // Omitting chat_token_budget must not silently reset it to the shipped
    // default: a revision stores the whole object.
    const { chat_token_budget: _dropped, ...partial } = inferenceConcurrencySettings
    const response = await client.callTool({
      name: 'set_inference_concurrency_settings',
      arguments: {
        expectedRevision: 4,
        settings: partial,
        reason: 'raise the per-ticket embedding concurrency',
        confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    expect(readTextResult(response)).toContain('chat_token_budget')
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('refuses an inference concurrency hierarchy the platform would reject', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_inference_concurrency_settings',
      arguments: {
        expectedRevision: 4,
        settings: {
          ...inferenceConcurrencySettings,
          embedding_per_ticket_concurrency: 64,
          embedding_per_validator_concurrency: 48,
        },
        reason: 'widen the per-ticket embedding lane for a calibration run',
        confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    expect(readTextResult(response)).toContain('embedding_per_validator_concurrency')
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('refuses an inverted chat concurrency hierarchy before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_inference_concurrency_settings',
      arguments: {
        expectedRevision: 4,
        settings: {
          ...inferenceConcurrencySettings,
          chat_per_ticket_concurrency: 64,
          chat_per_validator_concurrency: 48,
        },
        reason: 'widen the per-ticket chat lane for a benchmark run',
        confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    expect(readTextResult(response)).toContain('chat_per_validator_concurrency')
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('names the recovery for a stale inference concurrency revision', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const detail =
      'inference concurrency settings changed; refresh before applying (expected 4, current 6)'
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ detail }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_inference_concurrency_settings',
      arguments: {
        expectedRevision: 4,
        settings: inferenceConcurrencySettings,
        reason: 'restore the shipped hosted inference allowances',
        confirmation: 'APPLY INFERENCE CONCURRENCY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    const message = readTextResult(response)
    expect(message).toContain(detail)
    expect(message).toContain('get_inference_concurrency_settings')
    expect(message).toContain('Nothing was applied')

    await client.close()
    await server.close()
  })

  it('rejects a queue policy revision with the wrong confirmation before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_queue_policy_settings',
      arguments: {
        expectedRevision: 0,
        settings: { lane_cycle_size: 4, fresh_submission_slots: [0, 1, 3] },
        reason: 'restore the shipped lane cycle after the wave',
        confirmation: 'APPLY QUEUE POLICY',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('does not change queue policy without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'set_queue_policy_settings',
      arguments: {
        expectedRevision: 0,
        settings: { prev_gen_carryover: { enabled: true } },
        reason: 'admit stranded prior-generation submissions for one wave',
        confirmation: 'APPLY QUEUE POLICY SETTINGS',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  const screenerPolicyActivationView = {
    effective_policy_version: 10,
    floor_policy_version: 10,
    builtin_policy_version: 11,
    latest: {
      revision: 3,
      parent_revision: 2,
      target_policy_version: 11,
      activate_at: '2026-08-29T13:00:00Z',
      rescreen_scored: true,
      reason: 'scheduled v11 activation for the planner-forced I7 amendment',
      actor: 'peyton@omniaura.ai',
      created_at: '2026-08-28T12:00:00Z',
      state: 'pending',
    },
    revisions: [
      {
        revision: 3,
        parent_revision: 2,
        target_policy_version: 11,
        activate_at: '2026-08-29T13:00:00Z',
        rescreen_scored: true,
        reason: 'scheduled v11 activation for the planner-forced I7 amendment',
        actor: 'peyton@omniaura.ai',
        created_at: '2026-08-28T12:00:00Z',
        state: 'pending',
      },
    ],
  }

  it('reads the future screening-policy activation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(screenerPolicyActivationView))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_screener_policy_activation',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toEqual(screenerPolicyActivationView)
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screener-policy-activation',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('schedules one screener policy activation with the exact platform contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ revision: 4 }))
      .mockResolvedValueOnce(Response.json(screenerPolicyActivationView))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'schedule_screener_policy_activation',
      arguments: {
        expectedRevision: 3,
        targetPolicyVersion: 11,
        activateAt: '2026-08-29T09:00:00-04:00',
        rescreenScored: true,
        reason: 'scheduled v11 activation for the planner-forced I7 amendment',
        confirmation: 'SCHEDULE SCREENER POLICY ACTIVATION',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      latest: { revision: 3, state: 'pending' },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('https://platform-api.heyditto.ai/api/v1/admin/screener-policy-activation')
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer platform-admin-token',
      'X-Admin-Actor': 'peyton@omniaura.ai',
    })
    // The audited actor is always the signed-in operator, never a tool argument.
    expect(JSON.parse(String(init.body))).toEqual({
      expected_revision: 3,
      target_policy_version: 11,
      activate_at: '2026-08-29T09:00:00-04:00',
      rescreen_scored: true,
      reason: 'scheduled v11 activation for the planner-forced I7 amendment',
      actor: 'peyton@omniaura.ai',
      confirmation: 'SCHEDULE SCREENER POLICY ACTIVATION',
    })

    await client.close()
    await server.close()
  })

  it('rejects a screener policy activation with the wrong confirmation before any admin call', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'schedule_screener_policy_activation',
      arguments: {
        expectedRevision: 3,
        targetPolicyVersion: 11,
        activateAt: '2026-08-29T09:00:00-04:00',
        reason: 'scheduled v11 activation for the planner-forced I7 amendment',
        confirmation: 'SCHEDULE SCREENER POLICY',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('does not schedule a screener policy activation without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'schedule_screener_policy_activation',
      arguments: {
        expectedRevision: 3,
        targetPolicyVersion: 11,
        activateAt: '2026-08-29T09:00:00-04:00',
        reason: 'scheduled v11 activation for the planner-forced I7 amendment',
        confirmation: 'SCHEDULE SCREENER POLICY ACTIVATION',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('reads the validator slot policy the dispatch path resolves', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      max_concurrent_slots: 2,
      disk_percent_ceiling: 90,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
      paused_validator_hotkeys: [],
    }
    const fetchMock = vi.fn().mockResolvedValueOnce(
      // The exact payload production answers today, before any revision.
      Response.json({
        current: [],
        history: [],
        default: settings,
        effective: {
          revision: 0,
          scope: '*',
          settings,
          checksum: '',
          source: 'default',
          hard_slot_ceiling: 8,
          disk_restricted_slots: 1,
          max_age_seconds: 5.0,
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_validator_slot_settings',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: {
        revision: 0,
        source: 'default',
        settings: {
          ...settings,
        },
        hard_slot_ceiling: 8,
        disk_restricted_slots: 1,
        max_age_seconds: 5,
      },
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validator-slot-settings',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('reads validator fleet identity and a whole-fleet version histogram', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const current = '5' + 'A'.repeat(47)
    const lagging = '5' + 'B'.repeat(47)
    const digest = (ch: string) => `sha256:${ch.repeat(64)}`
    const revision = (ch: string) => ch.repeat(40)
    const component = (ch: string) => ({
      image_digest: digest(ch),
      source_revision: revision(ch),
      version: '0.64.0',
      provenance: 'signed_descriptor',
    })
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        generated_at: '2026-08-20T13:40:00Z',
        active_bench_version: 11,
        online_window_seconds: 90,
        stale_window_seconds: 300,
        reported_count: 2,
        online_count: 2,
        validators: [
          {
            validator_hotkey: current,
            software_version: '0.64.0',
            protocol_version: 23,
            state: 'idle',
            configured_slots: 2,
            healthy_slots: ['slot-0', 'slot-1'],
            admission: 'accepting',
            active_benchmarks: [],
            confirmation_benchmarks: [],
            assignment_state: 'idle',
            reported_at: '2026-08-20T13:39:50Z',
            seen_at: '2026-08-20T13:39:51Z',
            online: true,
            availability: 'available',
            health: 'healthy',
            scorer_liveness: 'serving',
            bench_serviceability: 'serving',
            stack: {
              mode: 'managed',
              compose_schema: 2,
              release_descriptor_digest: digest('c'),
              components: {
                ditto_subnet: component('a'),
                dittobench_api: component('d'),
                sandbox_docker: component('e'),
                model_relay: component('f'),
                pylon: component('g'),
                ollama: null,
              },
            },
            capabilities: {
              scorer_benchmarks: {
                status: 'fresh_verified',
                software_version: '0.64.0',
                source_revision: revision('d'),
                supported_bench_versions: [11],
              },
            },
            updater_status: {
              enabled: true,
              channel: 'compat-2',
              state: 'idle',
              current_version: '0.64.0',
              current_descriptor: `ghcr.io/ditto-assistant/ditto-subnet-stack@${digest('c')}`,
              candidate_version: null,
              candidate_descriptor: null,
              failed_candidate_count: 0,
              suppressed: false,
              observed_at: 1_787_000_000,
            },
          },
          {
            validator_hotkey: lagging,
            software_version: '0.63.1',
            protocol_version: 22,
            state: 'idle',
            configured_slots: 2,
            healthy_slots: ['slot-0'],
            admission: 'accepting',
            active_benchmarks: [{ slot_id: 'slot-0' }],
            online: true,
            health: 'healthy',
            scorer_liveness: 'unreported',
            bench_serviceability: 'software_obsolete',
            updater_status: {
              enabled: true,
              state: 'prefetched',
              current_version: '0.63.1',
              candidate_version: '0.64.0',
              failed_candidate_count: 0,
              suppressed: false,
              observed_at: 1_787_000_100,
            },
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_validator_fleet',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    const body = readJsonResult(response) as {
      online_serving_count: number
      software_obsolete_count: number
      rollout: { software_versions: Array<{ value: string; count: number }> }
      validators: Array<{ validator_hotkey: string; software_version: string }>
    }
    expect(body.online_serving_count).toBe(1)
    expect(body.software_obsolete_count).toBe(1)
    expect(body.rollout.software_versions).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ value: '0.64.0', count: 1, serving_count: 1 }),
        expect.objectContaining({ value: '0.63.1', count: 1, serving_count: 0 }),
      ]),
    )
    expect(body.validators.map((row) => row.software_version).sort()).toEqual([
      '0.63.1',
      '0.64.0',
    ])
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/public/validators',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('fails closed when the validator fleet heartbeat view is unreachable', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(Response.json({ detail: 'nope' }, { status: 503 })),
    )
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_validator_fleet',
      arguments: {},
    })
    expect(response.isError).toBe(true)
    expect(readTextResult(response)).not.toMatch(/"validators":\s*\[\]/)

    await client.close()
    await server.close()
  })

  it('reads live validator assignments without changing them', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '12345678-1234-4234-8234-123456789012'
    const fetchMock = vi.fn().mockResolvedValueOnce(
      Response.json({
        count: 1,
        items: [
          {
            agent_id: agentId,
            agent_name: 'lets_5.6',
            miner_hotkey: '5' + 'C'.repeat(47),
            validator_hotkey: '5' + 'A'.repeat(47),
            issued_at: '2026-08-20T13:00:00Z',
            deadline: '2026-08-20T15:00:00Z',
            bench_version: 11,
            attempt_count: 2,
            score_count: 0,
            provisional_composite: null,
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'list_validator_assignments',
      arguments: {},
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      count: 1,
      returned: 1,
      items: [{ agent_id: agentId, agent_name: 'lets_5.6', bench_version: 11 }],
    })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validator-assignments',
      expect.objectContaining({ method: 'GET' }),
    )

    await client.close()
    await server.close()
  })

  it('ramps the slot cap with the exact platform contract', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const settings = {
      max_concurrent_slots: 3,
      disk_percent_ceiling: 85,
      memory_percent_ceiling: 90,
      cpu_percent_ceiling: 0,
      resource_block_percent_ceiling: 95,
      paused_validator_hotkeys: [],
    }
    const control = {
      current: [],
      history: [],
      default: {
        max_concurrent_slots: 2,
        disk_percent_ceiling: 90,
        memory_percent_ceiling: 90,
        cpu_percent_ceiling: 0,
        resource_block_percent_ceiling: 95,
        paused_validator_hotkeys: [],
      },
      effective: {
        revision: 1,
        scope: '*',
        settings,
        checksum: 'ef'.repeat(32),
        source: 'revision',
        hard_slot_ceiling: 8,
        disk_restricted_slots: 1,
        max_age_seconds: 5.0,
      },
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ revision: 1 }))
      .mockResolvedValueOnce(Response.json(control))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_validator_slot_settings',
      arguments: {
        scope: '*',
        expectedRevision: 0,
        settings,
        reason: 'ramp the fleet to three slots now that dispatch is stable',
        confirmation: 'APPLY VALIDATOR SLOT CAP 3',
      },
    })
    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      effective: { revision: 1, source: 'revision', settings },
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      'https://platform-api.heyditto.ai/api/v1/admin/validator-slot-settings',
    )
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer platform-admin-token',
      'X-Admin-Actor': 'peyton@omniaura.ai',
    })
    // The audited actor is always the signed-in operator, never a tool argument.
    expect(JSON.parse(String(init.body))).toEqual({
      scope: '*',
      expected_revision: 0,
      settings,
      reason: 'ramp the fleet to three slots now that dispatch is stable',
      actor: 'peyton@omniaura.ai',
      confirmation: 'APPLY VALIDATOR SLOT CAP 3',
    })

    await client.close()
    await server.close()
  })

  // The confirmation names the resulting cap, so the number is stated twice and
  // the two statements are only ever checked against each other. A caller who
  // types the current cap while ramping to a new one is caught here.
  it('refuses a slot revision whose confirmation names a different cap', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_validator_slot_settings',
      arguments: {
        expectedRevision: 0,
        settings: {
          max_concurrent_slots: 3,
          disk_percent_ceiling: 90,
          memory_percent_ceiling: 90,
          cpu_percent_ceiling: 0,
          resource_block_percent_ceiling: 95,
          paused_validator_hotkeys: [],
        },
        reason: 'ramp the fleet to three slots now that dispatch is stable',
        confirmation: 'APPLY VALIDATOR SLOT CAP 2',
      },
    })

    expect(response.isError).toBe(true)
    expect(readTextResult(response)).toContain('APPLY VALIDATOR SLOT CAP 3')
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  // The empty-default failure class: the platform 422s a partial body, but a
  // client that defaults its knobs pre-fills the omission into a full body and
  // the operator silently ships a default they never chose.
  it('rejects a partial slot policy instead of defaulting the omitted knob', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_validator_slot_settings',
      arguments: {
        expectedRevision: 0,
        // disk_percent_ceiling omitted: the current revision's value is NOT
        // inherited, so accepting this would ship the shipped default silently.
        settings: { max_concurrent_slots: 3 },
        reason: 'ramp the fleet to three slots now that dispatch is stable',
        confirmation: 'APPLY VALIDATOR SLOT CAP 3',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('surfaces the stale-revision refusal verbatim with its recovery', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const detail =
      'validator slot settings changed; refresh before applying (expected 0, current 2)'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ detail }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'set_validator_slot_settings',
      arguments: {
        expectedRevision: 0,
        settings: {
          max_concurrent_slots: 3,
          disk_percent_ceiling: 90,
          memory_percent_ceiling: 90,
          cpu_percent_ceiling: 0,
          resource_block_percent_ceiling: 95,
          paused_validator_hotkeys: [],
        },
        reason: 'ramp the fleet to three slots now that dispatch is stable',
        confirmation: 'APPLY VALIDATOR SLOT CAP 3',
      },
    })

    expect(response.isError).toBe(true)
    const message = readTextResult(response)
    expect(message).toContain(detail)
    expect(message).toContain('get_validator_slot_settings')
    expect(message).toContain('Nothing was applied')
    // No re-read after a refusal, so a fresh GET is never mistaken for an apply.
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await client.close()
    await server.close()
  })

  it('does not change the slot cap without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'set_validator_slot_settings',
      arguments: {
        expectedRevision: 0,
        settings: {
          max_concurrent_slots: 1,
          disk_percent_ceiling: 90,
          memory_percent_ceiling: 90,
          cpu_percent_ceiling: 0,
          resource_block_percent_ceiling: 95,
          paused_validator_hotkeys: [],
        },
        reason: 'drop to the serial-dispatch kill switch',
        confirmation: 'APPLY VALIDATOR SLOT CAP 1',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('keeps benchmark contract refresh read-only without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'refresh_benchmark_contract',
      arguments: {
        agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
        expectedSha256: 'ab'.repeat(32),
        expectedBenchVersion: 3,
        expectedDatasetSha256: 'cd'.repeat(32),
        expectedScoreCount: 0,
        reason: 'Confirmed generator and validator dataset drift',
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('does not issue screening artifact URLs for a read-only grant', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_artifact',
      arguments: { agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054' },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('defense-in-depth rejects Alan even with a stale write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const props: McpGrantProps = {
      session: { ...session, email: 'alan@omniaura.ai', accessLevel: 'read' },
      scopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
      clientName: 'Test client',
    }
    const server = createBackroomMcpServer(props)
    const client = new Client({ name: 'backroom-test', version: '1.0.0' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])

    const response = await client.callTool({
      name: 'approve_app',
      arguments: { appId: 'app-123' },
    })
    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
    await client.close()
    await server.close()
  })

  it('opens an attributed ATH hold with exact artifact guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const expectedSha256 = 'ab'.repeat(32)
    const detailedReason = `Deterministic benchmark-family routing: ${'e'.repeat(1_000)}`
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        review: {
          review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          agent_id: agentId,
          miner_hotkey: '5Miner',
          agent_name: 'benchmax',
          agent_version: 1,
          submitted_at: '2026-07-16T12:00:00Z',
          status: 'pending',
          opened_at: '2026-07-16T13:00:00Z',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
          original: {
            review_kind: 'benchmark_overfit',
            duplicate_of: null,
            reason: 'Deterministic benchmark-family routing',
            policy_version: 8,
            fingerprint_versions: {},
            reference_provenance: 'unknown',
            backfilled: false,
          },
          current_comparison: null,
        },
        agent_status: 'ath_pending_review',
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'open_ath_review',
      arguments: {
        agentId,
        expectedSha256,
        expectedScoreCount: 3,
        reason: detailedReason,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/${agentId}/open`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          expected_sha256: expectedSha256,
          expected_score_count: 3,
          reason: detailedReason,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('explains a durable ATH hold through a read-only grant', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const expectedSha256 = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        review: {
          review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          agent_id: agentId,
          miner_hotkey: '5Miner',
          agent_name: 'benchmax',
          agent_version: 1,
          submitted_at: '2026-07-16T12:00:00Z',
          status: 'pending',
          opened_at: '2026-07-16T13:00:00Z',
          resolved_at: null,
          resolved_by: null,
          resolution: null,
          resolution_reason: null,
          original: {
            review_kind: 'benchmark_overfit',
            duplicate_of: null,
            reason: 'Deterministic benchmark-family routing',
            policy_version: 8,
            fingerprint_versions: {},
            reference_provenance: 'unknown',
            backfilled: false,
          },
          current_comparison: null,
        },
        agent_status: 'ath_pending_review',
        held_artifact_sha256: expectedSha256,
        held_score_count: 3,
        previous_status: 'live',
        opened_by: 'operator@omniaura.ai',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_ath_review',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      review: {
        agent_id: agentId,
        original: {
          review_kind: 'benchmark_overfit',
          reason: 'Deterministic benchmark-family routing',
        },
      },
      held_artifact_sha256: expectedSha256,
      held_score_count: 3,
      previous_status: 'live',
      opened_by: 'operator@omniaura.ai',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/${agentId}/audit`,
      expect.any(Object),
    )

    await client.close()
    await server.close()
  })

  it('searches resolved ATH holdings as precedents through a read-only grant', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        items: [
          {
            review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            agent_id: '11111111-1111-4111-8111-111111111111',
            agent_name: 'Omar-miner',
            agent_version: 20,
            miner_hotkey: '5Omar',
            status: 'resolved',
            resolution: 'reject',
            resolution_reason: 'v19 phrase table remains',
            original_reason: 'phrase table plus character-match',
            review_kind: 'benchmark_overfit',
            opened_at: '2026-08-17T15:00:00Z',
            resolved_at: '2026-08-17T16:00:00Z',
            resolved_by: 'peyton@omniaura.ai',
          },
        ],
        count: 1,
        limit: 20,
        offset: 0,
        q: 'phrase table',
        resolution: 'reject',
        review_kind: 'benchmark_overfit',
        status: 'resolved',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'search_ath_precedents',
      arguments: {
        query: 'phrase table',
        resolution: 'reject',
        reviewKind: 'benchmark_overfit',
        limit: 20,
        offset: 0,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      count: 1,
      q: 'phrase table',
      items: [
        {
          agent_id: '11111111-1111-4111-8111-111111111111',
          agent_name: 'Omar-miner',
          resolution: 'reject',
        },
      ],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/precedents?status=resolved&limit=20&offset=0&q=phrase+table&resolution=reject&review_kind=benchmark_overfit',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('resolves an ATH hold with an attributed public reason', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const detailedReason = `General behavior confirmed: ${'e'.repeat(1_000)}`
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        review: {
          review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          agent_id: agentId,
          miner_hotkey: '5Miner',
          agent_name: 'benchmax',
          agent_version: 1,
          submitted_at: '2026-07-16T12:00:00Z',
          status: 'resolved',
          opened_at: '2026-07-16T13:00:00Z',
          resolved_at: '2026-07-16T14:00:00Z',
          resolved_by: 'peyton@omniaura.ai',
          resolution: 'clear',
          resolution_reason: 'General behavior confirmed',
          original: {
            review_kind: 'benchmark_overfit',
            duplicate_of: null,
            reason: 'Deterministic benchmark-family routing',
            policy_version: 8,
            fingerprint_versions: {},
            reference_provenance: 'unknown',
            backfilled: false,
          },
          current_comparison: null,
        },
        agent_status: 'live',
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])

    const response = await client.callTool({
      name: 'resolve_ath_review',
      arguments: {
        agentId,
        resolution: 'clear',
        reason: detailedReason,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/copy-reviews/${agentId}/resolve`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          resolution: 'clear',
          reason: detailedReason,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('lists screening quarantines through the platform admin API', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ items: [], count: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'list_screening_quarantines',
      arguments: { status: 'resolved', limit: 17, offset: 34 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      items: [],
      count: 0,
      limit: 17,
      offset: 34,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screening-quarantines?status=resolved&sort=newest&limit=17&offset=34',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('enumerates the ATH hold queue, not the auto-resolved quarantine queue', async () => {
    // The defect: this tool read /admin/screening-quarantines?status=active,
    // which the platform actor `platform:deferred-source-review` auto-resolves
    // to `rescreen` within milliseconds. It therefore answered
    // `{items: [], count: 0}` while agents sat in ath_pending_review, and the
    // only working enumeration was an eight-call ~1.5MB sweep of
    // list_screening_submissions.
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        items: [
          {
            review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            agent_id: '11111111-1111-4111-8111-111111111111',
            miner_hotkey: '5HeldMiner',
            miner_coldkey: '5HeldColdkey',
            agent_name: 'gate',
            agent_version: 2,
            submitted_at: '2026-08-01T00:00:00Z',
            status: 'pending',
            agent_status: 'ath_pending_review',
            opened_at: '2026-08-02T00:00:00Z',
            resolved_at: null,
            resolved_by: null,
            resolution: null,
            resolution_reason: null,
            original: {
              review_kind: 'deferred_source_review',
              duplicate_of: null,
              reason: 'score-qualified source review',
              policy_version: 9,
              fingerprint_versions: { lexical: 1, structural: 1, prompt: 'p1' },
              reference_provenance: 'corpus',
              backfilled: false,
            },
          },
        ],
        count: 38,
        limit: 12,
        offset: 24,
        review_kind: 'deferred_source_review',
        generation: 'all',
        active_bench_version: 9,
        rollout_bench_version: null,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_review_queue',
      arguments: { reviewKind: 'deferred_source_review', limit: 12, offset: 24 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      count: 38,
      limit: 12,
      offset: 24,
      items: [
        {
          agent_id: '11111111-1111-4111-8111-111111111111',
          agent_name: 'gate',
          agent_version: 2,
          miner_hotkey: '5HeldMiner',
          miner_coldkey: '5HeldColdkey',
          submitted_at: '2026-08-01T00:00:00Z',
          opened_at: '2026-08-02T00:00:00Z',
          agent_status: 'ath_pending_review',
          hold: { review_kind: 'deferred_source_review', duplicate_of: null },
        },
      ],
    })
    // status and generation are pinned, never taken from the caller: a queue
    // narrowed by review status or scoring cohort is how it went empty.
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/copy-reviews?status=pending&generation=all&limit=12&offset=24&review_kind=deferred_source_review',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('carries the matched agent identity on a copy hold and flags stranded holds', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const row = (overrides: Record<string, unknown>) => ({
      review_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      agent_id: '11111111-1111-4111-8111-111111111111',
      miner_hotkey: '5HeldMiner',
      miner_coldkey: null,
      agent_name: 'held',
      agent_version: 1,
      submitted_at: '2026-08-01T00:00:00Z',
      status: 'pending',
      agent_status: 'ath_pending_review',
      opened_at: '2026-08-02T00:00:00Z',
      resolved_at: null,
      resolved_by: null,
      resolution: null,
      resolution_reason: null,
      original: {
        review_kind: 'copy',
        duplicate_of: '22222222-2222-4222-8222-222222222222',
        reason: 'near-copy signal',
        policy_version: 9,
        fingerprint_versions: { lexical: 1, structural: 1, prompt: 'p1' },
        reference_provenance: 'corpus',
        backfilled: false,
        duplicate_of_name: 'origin',
        duplicate_of_version: 4,
        duplicate_of_hotkey: '5OriginalMiner',
        duplicate_of_coldkey: '5OriginalColdkey',
        duplicate_of_submitted_at: '2026-07-01T00:00:00Z',
      },
      ...overrides,
    })
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        items: [
          row({}),
          row({
            review_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
            agent_id: '33333333-3333-4333-8333-333333333333',
            // A pending review whose agent already left the hold. It is not
            // queue work, and resolve_ath_review 409s on it.
            agent_status: 'scored',
          }),
        ],
        count: 2,
        limit: 50,
        offset: 0,
        review_kind: null,
        generation: 'all',
        active_bench_version: 9,
        rollout_bench_version: null,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_review_queue',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    const queue = readJsonResult(response) as {
      items: Array<Record<string, unknown>>
      items_shared?: Record<string, unknown>
    }
    const holds = queue.items.map((item) => ({
      agent_id: item.agent_id,
      agent_status:
        (item.agent_status as string | undefined) ??
        (queue.items_shared?.agent_status as string | undefined),
      hold: {
        ...(queue.items_shared?.hold as Record<string, unknown> | undefined),
        ...(item.hold as Record<string, unknown> | undefined),
      },
    }))
    expect(holds[0]).toMatchObject({
      agent_status: 'ath_pending_review',
      hold: {
        review_kind: 'copy',
        duplicate_of: '22222222-2222-4222-8222-222222222222',
        duplicate_of_name: 'origin',
        duplicate_of_hotkey: '5OriginalMiner',
        duplicate_of_coldkey: '5OriginalColdkey',
        duplicate_of_submitted_at: '2026-07-01T00:00:00Z',
      },
    })
    expect(holds[1].agent_status).toBe('scored')
    // Per-row algorithm provenance is not queue triage information; the deep
    // evidence belongs to get_ath_review.
    expect(JSON.stringify(queue)).not.toContain('fingerprint_versions')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/copy-reviews?status=pending&generation=all&limit=50&offset=0',
      expect.anything(),
    )

    await client.close()
    await server.close()
  })

  it('lists screening attempt history without write access', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ items: [], count: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'list_screening_submissions',
      arguments: { limit: 17, offset: 34 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      items: [],
      count: 0,
      limit: 17,
      offset: 34,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screening-submissions?limit=17&offset=34',
      expect.any(Object),
    )

    await client.close()
    await server.close()
  })

  it('gets one exact screening submission without artifact scope', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const submission = {
      agent_id: agentId,
      miner_hotkey: '5Miner',
      agent_name: 'exact-agent',
      agent_version: 2,
      artifact_sha256: 'ab'.repeat(32),
      agent_status: 'scored',
      screening_policy_version: 9,
      screening_reason: null,
      screening_reason_code: null,
      submitted_at: '2026-07-19T12:00:00Z',
      attempts: [
        {
          attempt_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          policy_version: 9,
          status: 'passed',
          screener_hotkey: '5Screener',
          started_at: '2026-07-19T12:01:00Z',
          deadline: '2026-07-19T13:11:00Z',
          finished_at: '2026-07-19T12:05:00Z',
          reason: null,
          reason_code: 'behavioral-oracle-passed',
          duplicate_of: null,
          duplicate_name: null,
          duplicate_version: null,
        },
      ],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(submission))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_submission',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toEqual({
      ...submission,
      miner_coldkey: null,
      image_builds: [],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}`,
      expect.any(Object),
    )

    await client.close()
    await server.close()
  })

  it('lists, reads, and guardedly removes hotkey-level upload bans', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const hotkey = '5FKbkmKbJHTgsELVPigLJqbmovaviDN7dHZzX7UJ6xoqG4fx'
    const bannedAt = '2026-08-18T03:57:07.967881Z'
    const activeBan = {
      hotkey,
      reason: 'benchmark emulation on agent 11ad9203-0860-40a9-9432-059b4ef68865',
      banned_at: bannedAt,
    }
    const action = {
      seq: 1,
      hotkey,
      action: 'unban',
      actor: 'peyton@omniaura.ai',
      reason: 'allow rebuilt architecture to submit under current screening',
      previous_reason: activeBan.reason,
      previous_banned_at: bannedAt,
      recorded_at: '2026-08-28T17:00:00Z',
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ total: 1, bans: [activeBan] }))
      .mockResolvedValueOnce(Response.json({ hotkey, banned: false, action }))
      .mockResolvedValueOnce(
        Response.json({ hotkey, banned: false, active_ban: null, history: [action] }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const readConnection = await connect([BACKROOM_READ_SCOPE])
    const listing = await readConnection.client.callTool({
      name: 'list_hotkey_bans',
      arguments: { limit: 50, offset: 0 },
    })
    expect(readJsonResult(listing)).toMatchObject({
      total: 1,
      count: 1,
      returned: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    })
    await readConnection.client.close()
    await readConnection.server.close()

    const writeConnection = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    const cleared = await writeConnection.client.callTool({
      name: 'unban_hotkey',
      arguments: {
        hotkey,
        expectedBannedAt: bannedAt,
        reason: action.reason,
        confirmation: `UNBAN HOTKEY ${hotkey}`,
      },
    })
    expect(cleared.isError).not.toBe(true)
    expect(readJsonResult(cleared)).toMatchObject({
      hotkey,
      banned: false,
      active_ban: null,
      history: [action],
    })
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `https://platform-api.heyditto.ai/api/v1/admin/hotkey-bans/${hotkey}/unban`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
      }),
    )
    await writeConnection.client.close()
    await writeConnection.server.close()
  })

  it('answers owner attestations on read scope alone, keeping every grade and revoked links', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const queried = '5QueriedHotkey'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        hotkey: queried,
        netuid: 118,
        attestations: [
          {
            attestation_id: '77777777-7777-4777-8777-777777777777',
            netuid: 118,
            hotkey_lo: '5AlphaLinkedHotkey',
            hotkey_hi: queried,
            counterparty: '5AlphaLinkedHotkey',
            evidence_grade: 'hotkey-hotkey',
            lo_key_kind: 'hotkey',
            lo_signer: '5AlphaLinkedHotkey',
            hi_key_kind: 'hotkey',
            hi_signer: queried,
            nonce: '88888888-8888-4888-8888-888888888888',
            issued_at: '2026-07-01T00:00:00Z',
            created_at: '2026-07-01T00:05:00Z',
            revoked_at: null,
            revoked_by: null,
            revoked_reason: null,
            active: true,
          },
          {
            attestation_id: '99999999-9999-4999-8999-999999999999',
            netuid: 118,
            hotkey_lo: '5BravoLinkedHotkey',
            hotkey_hi: queried,
            counterparty: '5BravoLinkedHotkey',
            evidence_grade: 'mixed',
            lo_key_kind: 'coldkey',
            lo_signer: '5BravoPayingColdkey',
            hi_key_kind: 'hotkey',
            hi_signer: queried,
            nonce: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            issued_at: '2026-06-01T00:00:00Z',
            created_at: '2026-06-01T00:05:00Z',
            revoked_at: null,
            revoked_by: null,
            revoked_reason: null,
            active: true,
          },
          {
            attestation_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
            netuid: 118,
            hotkey_lo: '5CharlieLinkedHotkey',
            hotkey_hi: queried,
            counterparty: '5CharlieLinkedHotkey',
            evidence_grade: 'coldkey-coldkey',
            lo_key_kind: 'coldkey',
            lo_signer: '5CharliePayingColdkey',
            hi_key_kind: 'coldkey',
            hi_signer: '5QueriedPayingColdkey',
            nonce: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
            issued_at: '2026-05-01T00:00:00Z',
            created_at: '2026-05-01T00:05:00Z',
            revoked_at: '2026-06-15T00:00:00Z',
            revoked_by: 'peyton@omniaura.ai',
            revoked_reason: 'Miner reported the linked hotkey compromised',
            active: false,
          },
        ],
        linked_hotkeys: [
          {
            hotkey: '5AlphaLinkedHotkey',
            attestation_id: '77777777-7777-4777-8777-777777777777',
            evidence_grade: 'hotkey-hotkey',
          },
          {
            hotkey: '5BravoLinkedHotkey',
            attestation_id: '99999999-9999-4999-8999-999999999999',
            evidence_grade: 'mixed',
          },
        ],
        linkage_basis: 'signed_owner_attestation',
        scope_caveat:
          'Exempts near-duplicate plagiarism screening between the linked hotkeys only; not an input to emission-slot allocation.',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    // Identity metadata, so no artifact scope and no write scope: a signed
    // owner link exposes no miner source.
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_owner_attestations',
      arguments: { hotkey: queried },
    })

    expect(response.isError).not.toBe(true)
    const payload = readJsonResult(response) as {
      linkage_basis: string
      scope_caveat: string
      attestations: Array<Record<string, unknown>>
      attestations_shared: Record<string, unknown>
      linked_hotkeys: Array<Record<string, unknown>>
    }
    expect(payload.linkage_basis).toBe('signed_owner_attestation')
    expect(payload.scope_caveat).toContain('emission-slot allocation')
    // Every grade survives intact and none is ranked away: all three establish
    // the link identically, so the tool must not editorialise.
    expect(payload.attestations.map((row) => row.evidence_grade)).toEqual([
      'hotkey-hotkey',
      'mixed',
      'coldkey-coldkey',
    ])
    // The counterparty stays on every row rather than being hoisted, so a
    // reader never has to reconstruct who a link names.
    expect(payload.attestations.map((row) => row.counterparty)).toEqual([
      '5AlphaLinkedHotkey',
      '5BravoLinkedHotkey',
      '5CharlieLinkedHotkey',
    ])
    // A revoked link is reported, not filtered: whether it was live at the
    // time of the held submission is the question a dispute turns on.
    expect(payload.attestations[2]).toMatchObject({
      revoked_at: '2026-06-15T00:00:00Z',
      revoked_by: 'peyton@omniaura.ai',
      revoked_reason: 'Miner reported the linked hotkey compromised',
      active: false,
    })
    // Only the active links appear as linked hotkeys, and the relation is not
    // transitive, so this is a direct-link list rather than a closure.
    expect(payload.linked_hotkeys).toEqual([
      {
        hotkey: '5AlphaLinkedHotkey',
        attestation_id: '77777777-7777-4777-8777-777777777777',
        evidence_grade: 'hotkey-hotkey',
      },
      {
        hotkey: '5BravoLinkedHotkey',
        attestation_id: '99999999-9999-4999-8999-999999999999',
        evidence_grade: 'mixed',
      },
    ])
    // Compaction lifts what every row repeats; nothing is summarised away.
    expect(payload.attestations_shared).toEqual({
      netuid: 118,
      hotkey_hi: queried,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/owner-attestations/${queried}`,
      expect.any(Object),
    )

    await client.close()
    await server.close()
  })

  it('answers an unlinked hotkey with empty lists rather than an error', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          hotkey: '5NeverLinked',
          netuid: 118,
          attestations: [],
          linked_hotkeys: [],
          linkage_basis: 'signed_owner_attestation',
          scope_caveat: 'No signed owner link is recorded for this hotkey.',
        }),
      ),
    )
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_owner_attestations',
      arguments: { hotkey: '5NeverLinked' },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      attestations: [],
      linked_hotkeys: [],
    })

    await client.close()
    await server.close()
  })

  // A platform that has not shipped the grade yet still answers; the link is
  // established either way, since the grade never gated it.
  it('answers from a platform that predates the evidence grade', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json({
          hotkey: '5QueriedHotkey',
          attestations: [
            {
              attestation_id: '77777777-7777-4777-8777-777777777777',
              hotkey_lo: '5AlphaLinkedHotkey',
              hotkey_hi: '5QueriedHotkey',
              counterparty: '5AlphaLinkedHotkey',
            },
          ],
        }),
      ),
    )
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_owner_attestations',
      arguments: { hotkey: '5QueriedHotkey' },
    })

    expect(response.isError).not.toBe(true)
    const payload = readJsonResult(response) as {
      attestations: Array<Record<string, unknown>>
    }
    expect(payload.attestations[0]).toMatchObject({
      counterparty: '5AlphaLinkedHotkey',
      evidence_grade: null,
      active: null,
    })

    await client.close()
    await server.close()
  })

  it('lists miner disputes without write access', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ items: [], count: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'list_screening_disputes',
      arguments: { status: 'pending', limit: 17, offset: 34 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      items: [],
      count: 0,
      limit: 17,
      offset: 34,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/screening-disputes?status=pending&limit=17&offset=34',
      expect.any(Object),
    )

    await client.close()
    await server.close()
  })

  it('attributes an explicitly write-scoped quarantine resolution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const detailedReason = [
      'Source review evidence shows src/router.ts:118 selects providers from the declared runtime configuration instead of matching benchmark prompts.',
      'The branch at src/router.ts:146 handles a documented timeout fallback and does not inspect prompt text, expected answers, evaluator metadata, or test fixture identifiers.',
      'A repository-wide search found no embedded benchmark answers, prompt hashes, fixture names, response lookup tables, or network calls to undeclared services.',
      'The submitted Docker image was rebuilt from the reviewed archive, then smoke-tested with unrelated prompts that exercised both the primary provider and fallback path.',
      'Observed outputs varied with the request and provider response, which is inconsistent with replay or benchmark emulation.',
      'Release is appropriate because the suspicious fast path is general routing logic; retain this source-level evidence in the audited miner-visible decision.',
    ].join(' ')
    expect(detailedReason.length).toBeGreaterThan(500)
    const quarantine = {
      quarantine_id: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
      agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
      attempt_id: '20236f60-c143-43b0-b03e-2cbe51f281d8',
      miner_hotkey: '5Miner',
      agent_name: 'memory-agent',
      artifact_sha256: 'artifact',
      policy_version: 7,
      manifest_digest: 'manifest',
      finding_digest: 'finding',
      reason_code: 'source_review_suspicious',
      status: 'resolved',
      created_at: '2026-07-14T12:00:00Z',
      resolved_at: '2026-07-14T12:30:00Z',
      resolved_by: 'peyton@omniaura.ai',
      resolution: 'rescreen',
      resolution_reason: detailedReason,
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ quarantine, agent_status: 'screening_failed' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    const response = await client.callTool({
      name: 'resolve_screening_quarantine',
      arguments: {
        quarantineId: quarantine.quarantine_id,
        resolution: 'rescreen',
        reason: detailedReason,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-quarantines/${quarantine.quarantine_id}/resolve`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({ resolution: 'rescreen', reason: detailedReason }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('rescreens a rejected submission with explicit concurrency guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const expectedSha256 = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ agent_id: agentId, agent_status: 'screening_failed' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])
    const response = await client.callTool({
      name: 'rescreen_rejected_submission',
      arguments: {
        agentId,
        reason: 'Build was interrupted by the screening worker DNS incident',
        expectedSha256,
        expectedScoreCount: 0,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      agent_id: agentId,
      agent_status: 'screening_failed',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/rescreen`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          reason: 'Build was interrupted by the screening worker DNS incident',
          expected_sha256: expectedSha256,
          expected_score_count: 0,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('does not rescreen a rejected submission without write access', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'rescreen_rejected_submission',
      arguments: {
        agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
        reason: 'Build was interrupted by the screening worker DNS incident',
        expectedSha256: 'ab'.repeat(32),
        expectedScoreCount: 0,
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('waives one exact failed screening backoff with explicit guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '270acbcc-268d-4380-9db7-c5fb90726941'
    const attemptId = 'af86d39d-51c6-46d7-83d4-36b61cab6aef'
    const overrideId = 'a32db723-c43e-4f1b-a4fc-6d8ec1ed985b'
    const expectedSha256 = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        override_id: overrideId,
        agent_id: agentId,
        attempt_id: attemptId,
        agent_status: 'screening_failed',
        backoff_deadline: '2026-08-02T16:54:16.324536Z',
        created_at: '2026-08-02T16:25:00Z',
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])
    const response = await client.callTool({
      name: 'retry_failed_screening_now',
      arguments: {
        agentId,
        reason: 'Retry immediately after source-review budget exhaustion',
        expectedSha256,
        expectedScoreCount: 0,
        expectedAttemptId: attemptId,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      override_id: overrideId,
      agent_id: agentId,
      attempt_id: attemptId,
      idempotent: false,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/retry-now`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          reason: 'Retry immediately after source-review budget exhaustion',
          expected_sha256: expectedSha256,
          expected_score_count: 0,
          expected_attempt_id: attemptId,
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('rejects a screening submission with explicit guards and confirmation', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = 'd47bd70b-f0d4-46ad-a5ec-6f2fc392c406'
    const attemptId = 'd83ae76a-ba23-4a5e-9874-2dce1e41da3d'
    const buildId = '016cbf75-2f76-4ddc-ad52-fb2c305d1c9e'
    const expectedSha256 = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        attempt_id: attemptId,
        agent_status: 'rejected',
        expired_build_ids: [buildId],
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])
    const response = await client.callTool({
      name: 'reject_screening_submission',
      arguments: {
        agentId,
        reason: 'Miner requested removal of a compile-fail screening crate',
        expectedSha256,
        expectedScoreCount: 0,
        expectedAttemptId: attemptId,
        confirmation: 'REJECT SCREENING SUBMISSION',
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      agent_id: agentId,
      attempt_id: attemptId,
      agent_status: 'rejected',
      idempotent: false,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/reject`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          reason: 'Miner requested removal of a compile-fail screening crate',
          expected_sha256: expectedSha256,
          expected_score_count: 0,
          expected_attempt_id: attemptId,
          confirmation: 'REJECT SCREENING SUBMISSION',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('does not let write scope implicitly issue screening artifact URLs', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_artifact',
      arguments: { agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054' },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('attributes an explicitly write-scoped dispute resolution', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const disputeId = 'c5973a8c-36e3-431e-96b0-9a05f4ab35ac'
    const dispute = {
      dispute_id: disputeId,
      agent_id: '90cb5697-cbc1-40f4-a27e-439a7986a054',
      quarantine_id: 'e3bb1518-530f-42d7-a50b-b21ac9853798',
      miner_hotkey: '5Miner',
      agent_name: 'memory-agent',
      artifact_sha256: 'artifact',
      message: 'The finding was caused by generic routing logic.',
      status: 'resolved',
      created_at: '2026-07-15T12:00:00Z',
      original_reason: 'Benchmark-specific behavior',
      resolved_at: '2026-07-15T12:30:00Z',
      resolved_by: 'peyton@omniaura.ai',
      resolution: 'release',
      resolution_reason: 'Manual source review confirmed generic behavior',
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ dispute, agent_status: 'evaluating' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    const response = await client.callTool({
      name: 'resolve_screening_dispute',
      arguments: {
        disputeId,
        resolution: 'release',
        reason: 'Manual source review confirmed generic behavior',
      },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-disputes/${disputeId}/resolve`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('serves quarantine review context to a read-only grant', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const quarantineId = 'e3bb1518-530f-42d7-a50b-b21ac9853798'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const attemptId = '20236f60-c143-43b0-b03e-2cbe51f281d8'
    const finding = {
      artifact_sha256: 'ab'.repeat(32),
      prompt_revision: 'source-review-v2',
      risk_level: 'high',
      confidence: 0.97,
      categories: ['benchmark_emulation'],
      evidence: [{ path: 'src/main.rs', line: 42, category: 'benchmark_emulation' }],
      summary: 'Deterministic shortcut bypasses the general provider path.',
    }
    const quarantine = {
      quarantine_id: quarantineId,
      agent_id: agentId,
      attempt_id: attemptId,
      miner_hotkey: '5Miner',
      agent_name: 'memory-agent',
      artifact_sha256: 'ab'.repeat(32),
      policy_version: 7,
      manifest_digest: 'cd'.repeat(32),
      finding_digest: 'ef'.repeat(32),
      reason_code: 'agentic-source-review-tripwire',
      evidence: [
        {
          module_id: 'luna-source-review',
          code: 'agentic-source-review-tripwire',
          summary: 'private source analysis selected a behavioral audit',
          digest: 'ef'.repeat(32),
        },
      ],
      finding,
      finding_verified: true,
      status: 'active',
      created_at: '2026-07-14T12:00:00Z',
      resolved_at: null,
      resolved_by: null,
      resolution: null,
      resolution_reason: null,
    }
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        quarantine,
        agent: {
          agent_id: agentId,
          miner_hotkey: '5Miner',
          agent_name: 'memory-agent',
          artifact_sha256: 'ab'.repeat(32),
          agent_status: 'quarantined',
          size_bytes: 20480,
          submitted_at: '2026-07-14T11:00:00Z',
          screening_policy_version: 7,
          screening_reason: 'Submission held for anti-cheat review',
        },
        attempts: [
          {
            attempt_id: attemptId,
            policy_version: 7,
            status: 'quarantined',
            screener_hotkey: '5Screener',
            started_at: '2026-07-14T11:30:00Z',
            deadline: '2026-07-14T12:00:00Z',
            finished_at: '2026-07-14T11:45:00Z',
            reason: 'Submission held for anti-cheat review',
          },
        ],
        miner: {
          miner_hotkey: '5Miner',
          total_submissions: 4,
          quarantine_count: 2,
          released_count: 1,
          rescreened_count: 0,
          rejected_count: 0,
          recent_quarantines: [],
        },
        duplicates: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_quarantine_context',
      arguments: { quarantineId },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-quarantines/${quarantineId}/context`,
      expect.any(Object),
    )
    expect(readJsonResult(response)).toMatchObject({
      quarantine: expect.objectContaining({ finding_verified: true }),
      miner: expect.objectContaining({ total_submissions: 4 }),
    })

    await client.close()
    await server.close()
  })

  it('gates source reads on the artifact scope and audits them', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        path: 'src/main.rs',
        total_lines: 3,
        start_line: 1,
        end_line: 3,
        lines: [{ line: 2, text: '    fast_path();' }],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    for (const scopes of [
      [BACKROOM_READ_SCOPE],
      [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
    ]) {
      const refusedGrant = await connect(scopes)
      const refused = await refusedGrant.client.callTool({
        name: 'read_screening_source_file',
        arguments: { agentId, path: 'src/main.rs' },
      })
      expect(refused.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
      await refusedGrant.client.close()
      await refusedGrant.server.close()
    }

    const granted = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    const allowed = await granted.client.callTool({
      name: 'read_screening_source_file',
      arguments: { agentId, path: 'src/main.rs', startLine: 1, endLine: 3 },
    })
    expect(allowed.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/source-file?path=src%2Fmain.rs&start_line=1&end_line=3`,
      expect.objectContaining({
        headers: expect.objectContaining({
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )
    await granted.client.close()
    await granted.server.close()
  })

  it('locally pages source-file manifests while preserving the total file count', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        artifact_sha256: 'ab'.repeat(32),
        file_count: 3,
        files: [
          { path: 'src/a.rs', bytes: 10 },
          { path: 'src/b.rs', bytes: 20 },
          { path: 'src/c.rs', bytes: 30 },
        ],
        opaque_blobs: [{ path: 'assets/model.bin', bytes: 40, reason: 'non_utf8' }],
        opaque_total: 1,
        truncated: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_ARTIFACT_SCOPE,
    ])
    const response = await client.callTool({
      name: 'list_screening_source_files',
      arguments: { agentId, limit: 1, offset: 1 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      file_count: 3,
      files: [{ path: 'src/b.rs', bytes: 20 }],
      opaque_blobs: [{ path: 'assets/model.bin' }],
      count: 3,
      returned: 1,
      limit: 1,
      offset: 1,
      has_more: true,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/source-files`,
      expect.objectContaining({
        headers: expect.objectContaining({
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('finds the response construction in one call and gates on the artifact scope', async () => {
    // The regression this tool exists for: a deferred_source_review decision
    // needs the `protocol::RunResponse` construction, which sits at line 8919
    // of a 10,795-line baseline.rs. Six to eight blind 400-line reads used to
    // be the only way there.
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        artifact_sha256: 'ab'.repeat(32),
        pattern: 'RunResponse',
        mode: 'regex',
        path_glob: 'src/*.rs',
        matches: [
          {
            path: 'src/baseline.rs',
            line: 8919,
            text: '    Ok(protocol::RunResponse { answer, abstain })',
            context_before: [{ line: 8918, text: '    let answer = pick(&ctx);' }],
            context_after: [{ line: 8920, text: '}' }],
          },
        ],
        match_count: 1,
        returned: 1,
        limit: 50,
        offset: 0,
        has_more: false,
        files_searched: 12,
        files_matched: 1,
        opaque_skipped: 2,
        truncated: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const refused = await connect([BACKROOM_READ_SCOPE])
    const denied = await refused.client.callTool({
      name: 'search_screening_source',
      arguments: { agentId, pattern: 'RunResponse' },
    })
    expect(denied.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
    await refused.client.close()
    await refused.server.close()

    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_ARTIFACT_SCOPE,
    ])
    const response = await client.callTool({
      name: 'search_screening_source',
      arguments: {
        agentId,
        pattern: 'RunResponse',
        pathGlob: 'src/*.rs',
        context: 1,
        limit: 50,
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      match_count: 1,
      has_more: false,
      truncated: false,
      // A search can never clear the binary members it never opened, so their
      // count travels with the result.
      opaque_skipped: 2,
      matches: [{ path: 'src/baseline.rs', line: 8919 }],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/source-search?pattern=RunResponse&mode=regex&ignore_case=false&context=1&limit=50&offset=0&path_glob=src%2F*.rs`,
      expect.objectContaining({
        headers: expect.objectContaining({
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('pages source search on the platform so match windows stay stable', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        artifact_sha256: 'ab'.repeat(32),
        pattern: 'answer',
        mode: 'literal',
        path_glob: null,
        matches: [{ path: 'src/b.rs', line: 4, text: 'answer', context_before: [], context_after: [] }],
        match_count: 900,
        returned: 1,
        limit: 1,
        offset: 3,
        has_more: true,
        files_searched: 12,
        files_matched: 4,
        opaque_skipped: 0,
        truncated: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_ARTIFACT_SCOPE,
    ])
    const response = await client.callTool({
      name: 'search_screening_source',
      arguments: { agentId, pattern: 'answer', mode: 'literal', limit: 1, offset: 3 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      match_count: 900,
      offset: 3,
      has_more: true,
    })
    // The window is resolved by the platform, which holds the whole ordered
    // match list; the worker must not re-slice a page it never had.
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('mode=literal&ignore_case=false&context=0&limit=1&offset=3'),
      expect.anything(),
    )

    await client.close()
    await server.close()
  })

  // Regression: the manifest is the reviewer's map of what exists inside a
  // submission, and it used to default to 50 rows. A 51-file agent answered
  // with count=51, truncated=false and 50 rows, so the one hidden module was
  // invisible to an operator who never guessed to pass an offset — an
  // adjudication could be made without knowing the file existed at all.
  it('returns whole source-file manifests past the old 50-row page by default', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '98d56bdf-3ef9-4fa1-8d1a-6c07234ecaf1'
    const files = Array.from({ length: 51 }, (_, index) => ({
      path: `src/module_${String(index).padStart(2, '0')}.rs`,
      bytes: 100 + index,
    }))
    files[files.length - 1] = { path: 'src/world_bind.rs', bytes: 55_529 }
    // A fresh Response per call: the body of one is consumed by the first read.
    const fetchMock = vi.fn().mockImplementation(async () =>
      Response.json({
        agent_id: agentId,
        artifact_sha256: 'cd'.repeat(32),
        file_count: 51,
        files,
        opaque_blobs: [],
        opaque_total: 0,
        truncated: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_ARTIFACT_SCOPE,
    ])

    const whole = await client.callTool({
      name: 'list_screening_source_files',
      arguments: { agentId },
    })
    expect(whole.isError).not.toBe(true)
    const manifest = readJsonResult(whole) as {
      count: number
      returned: number
      has_more: boolean
      files: { path: string }[]
    }
    expect(manifest).toMatchObject({
      file_count: 51,
      count: 51,
      returned: 51,
      offset: 0,
      has_more: false,
    })
    expect(manifest.files).toHaveLength(51)
    expect(manifest.files.map((file) => file.path)).toContain('src/world_bind.rs')

    // Paging still works, and a window that drops rows says so on its face:
    // has_more, not count, is what tells a reviewer to keep reading.
    const page = await client.callTool({
      name: 'list_screening_source_files',
      arguments: { agentId, limit: 50 },
    })
    expect(page.isError).not.toBe(true)
    const firstPage = readJsonResult(page) as { files: { path: string }[] }
    expect(firstPage).toMatchObject({
      file_count: 51,
      count: 51,
      returned: 50,
      limit: 50,
      offset: 0,
      has_more: true,
      truncated: false,
    })
    expect(firstPage.files).toHaveLength(50)

    const tail = await client.callTool({
      name: 'list_screening_source_files',
      arguments: { agentId, limit: 50, offset: 50 },
    })
    expect(tail.isError).not.toBe(true)
    expect(readJsonResult(tail)).toMatchObject({
      count: 51,
      returned: 1,
      offset: 50,
      has_more: false,
      files: [{ path: 'src/world_bind.rs', bytes: 55_529 }],
    })

    await client.close()
    await server.close()
  })

  it('gates the starter-kit baseline diff on the artifact scope and audits it', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        artifact_sha256: 'a'.repeat(64),
        baseline: {
          source: 'https://github.com/ditto-assistant/dittobench-starter-kit',
          revision: 'b'.repeat(40),
          commit_set_sha256: 'c'.repeat(64),
          commit_count: 24,
        },
        files: [
          {
            path: 'src/solver.rs',
            status: 'added',
            candidate_lines: 3,
            reference_lines: 0,
            added_lines: 3,
            removed_lines: 0,
            similarity: 0,
            normalized_identical: false,
            stock_kit: false,
          },
        ],
        file_count: 1,
        identical_count: 0,
        modified_count: 0,
        added_count: 1,
        removed_count: 0,
        stock_kit_count: 0,
        custom_file_count: 1,
        custom_added_lines: 3,
        path_aligned: false,
        truncated: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const refusedGrant = await connect([BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE])
    const refused = await refusedGrant.client.callTool({
      name: 'get_screening_baseline_diff',
      arguments: { agentId },
    })
    expect(refused.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
    await refusedGrant.client.close()
    await refusedGrant.server.close()

    const granted = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    const allowed = await granted.client.callTool({
      name: 'get_screening_baseline_diff',
      arguments: { agentId },
    })
    expect(allowed.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/baseline-diff`,
      expect.objectContaining({
        headers: expect.objectContaining({
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )
    await granted.client.close()
    await granted.server.close()
  })

  it('issues an audited artifact URL only with explicit artifact scope', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        sha256: 'ab'.repeat(32),
        download_url: 'https://signed.example/agent.tar.gz?signature=short-lived',
        expires_at: '2026-07-14T12:05:00Z',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE])
    const response = await client.callTool({
      name: 'get_screening_artifact',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/screening-submissions/${agentId}/artifact`,
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('reports what each queue remedy would do to one stuck submission', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        miner_hotkey: '5Miner',
        agent_name: 'mnemox-v55',
        agent_version: 55,
        agent_status: 'evaluating',
        score_count: 0,
        quorum: 3,
        snapshot,
        automatic_retry_available: false,
        recovery_allowed: false,
        blocking_reason: null,
        withdrawal_allowed: false,
        withdrawal_blocking_reason: 'submission can still reach quorum automatically',
        eviction_allowed: true,
        eviction_blocking_reason: null,
        live_ticket_count: 3,
        withdrawal: null,
        tickets: [],
        recoveries: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_validation_retry',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    // The 2026-07-27 shape exactly: removal refuses it, eviction is the move,
    // and the operator can see the three slots it is holding.
    expect(readJsonResult(response)).toMatchObject({
      withdrawal_allowed: false,
      eviction_allowed: true,
      eviction_blocking_reason: null,
      live_ticket_count: 3,
    })

    await client.close()
    await server.close()
  })

  it('evicts live validator leases under a write grant and its own phrase', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'ab'.repeat(32)
    const reason = 'Hung through three full leases with zero scores reported'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        eviction: {
          eviction_id: '33333333-3333-4333-8333-333333333333',
          agent_id: agentId,
          bench_version: 7,
          actor: 'peyton@omniaura.ai',
          reason,
          expected_snapshot: snapshot,
          score_count: 0,
          evicted_validator_hotkeys: ['5ValidatorA', '5ValidatorB'],
          created_at: '2026-07-27T18:00:00Z',
        },
        evicted_leases: [
          {
            validator_hotkey: '5ValidatorA',
            slot_id: 'slot-1',
            bench_version: 7,
            issued_at: '2026-07-27T17:00:00Z',
            original_deadline: '2026-07-27T18:30:00Z',
            attempt_count: 9,
            audit_id: '44444444-4444-4444-8444-444444444444',
          },
          {
            validator_hotkey: '5ValidatorB',
            slot_id: 'slot-2',
            bench_version: 7,
            issued_at: '2026-07-27T17:00:00Z',
            original_deadline: '2026-07-27T18:30:00Z',
            attempt_count: 9,
            audit_id: '55555555-5555-4555-8555-555555555555',
          },
        ],
        freed_slots: 2,
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'evict_live_validator_leases',
      arguments: {
        agentId,
        expectedSnapshot: snapshot,
        reason,
        confirmation: 'EVICT LIVE VALIDATOR LEASES',
      },
    })

    expect(response.isError).not.toBe(true)
    const evicted = readJsonResult(response) as {
      freed_slots: number
      evicted_leases: Array<Record<string, unknown>>
      evicted_leases_shared: Record<string, unknown>
    }
    expect(evicted.freed_slots).toBe(2)
    // Both revoked leases survive with their own audit id; what is identical
    // across them is stated once.
    expect(evicted.evicted_leases).toEqual([
      {
        validator_hotkey: '5ValidatorA',
        slot_id: 'slot-1',
        audit_id: '44444444-4444-4444-8444-444444444444',
      },
      {
        validator_hotkey: '5ValidatorB',
        slot_id: 'slot-2',
        audit_id: '55555555-5555-4555-8555-555555555555',
      },
    ])
    expect(evicted.evicted_leases_shared).toMatchObject({
      bench_version: 7,
      original_deadline: '2026-07-27T18:30:00Z',
    })

    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/evict`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          request_id: await deriveRequestId('validation-evict', [
            agentId,
            'peyton@omniaura.ai',
            reason,
            snapshot,
          ]),
          expected_snapshot: snapshot,
          reason,
          confirmation: 'EVICT LIVE VALIDATOR LEASES',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('refuses an eviction typed with the ordinary removal phrase', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'evict_live_validator_leases',
      arguments: {
        agentId: '974832d2-bfd0-4f38-a0d6-518be0d2571d',
        expectedSnapshot: 'ab'.repeat(32),
        reason: 'Hung through three full leases with zero scores reported',
        confirmation: 'REMOVE FROM VALIDATOR QUEUE',
      },
    })

    // An operator reaching for an ordinary removal must never land here by
    // editing the tool name and keeping the phrase.
    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('reinstates an evicted submission under a write grant and its own phrase', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'ab'.repeat(32)
    const reason = 'source review found no hang primitives, only latency work'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        reinstatement: {
          reinstatement_id: '66666666-6666-4666-8666-666666666666',
          withdrawal_id: '33333333-3333-4333-8333-333333333333',
          agent_id: agentId,
          bench_version: 7,
          actor: 'peyton@omniaura.ai',
          reason,
          expected_snapshot: snapshot,
          score_count: 0,
          retry_budget_snapshot: {
            attempts_used: 2,
            agent_infra_retry_grants: 4,
            max_agent_infra_retry_grants: 12,
            manual_retry_grants: 1,
            operator_recoveries: 1,
            max_operator_recoveries: 3,
          },
          created_at: '2026-07-27T19:00:00Z',
        },
        eviction: {
          eviction_id: '33333333-3333-4333-8333-333333333333',
          agent_id: agentId,
          bench_version: 7,
          actor: 'peyton@omniaura.ai',
          reason: 'Hung through three full leases with zero scores reported',
          expected_snapshot: snapshot,
          score_count: 0,
          evicted_validator_hotkeys: ['5ValidatorA'],
          created_at: '2026-07-27T18:00:00Z',
          reinstated_at: '2026-07-27T19:00:00Z',
        },
        restored_bench_version: 7,
        idempotent: false,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'reinstate_evicted_submission_to_queue',
      arguments: {
        agentId,
        expectedSnapshot: snapshot,
        reason,
        confirmation: 'REINSTATE TO VALIDATOR QUEUE',
      },
    })

    expect(response.isError).not.toBe(true)
    // The eviction comes back resolved rather than erased, and the budget the
    // reversal left alone comes back with it.
    expect(readJsonResult(response)).toMatchObject({
      restored_bench_version: 7,
      eviction: {
        evicted_validator_hotkeys: ['5ValidatorA'],
        reinstated_at: '2026-07-27T19:00:00Z',
      },
      reinstatement: {
        retry_budget_snapshot: {
          agent_infra_retry_grants: 4,
          max_agent_infra_retry_grants: 12,
          operator_recoveries: 1,
        },
      },
    })

    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/validation-retries/${agentId}/reinstate`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
          'X-Admin-Actor': 'peyton@omniaura.ai',
        }),
        body: JSON.stringify({
          // Its own namespace: deriving the eviction's key would make a
          // reversal collide with the action it reverses.
          request_id: await deriveRequestId('validation-reinstate', [
            agentId,
            'peyton@omniaura.ai',
            reason,
            snapshot,
          ]),
          expected_snapshot: snapshot,
          reason,
          confirmation: 'REINSTATE TO VALIDATOR QUEUE',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('refuses a reinstatement typed with either removal phrase', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    for (const confirmation of [
      'EVICT LIVE VALIDATOR LEASES',
      'REMOVE FROM VALIDATOR QUEUE',
    ]) {
      const response = await client.callTool({
        name: 'reinstate_evicted_submission_to_queue',
        arguments: {
          agentId: '974832d2-bfd0-4f38-a0d6-518be0d2571d',
          expectedSnapshot: 'ab'.repeat(32),
          reason: 'source review found no hang primitives, only latency work',
          confirmation,
        },
      })

      // The mirror of the eviction guard: an operator must not reverse an
      // eviction by editing the tool name and keeping the phrase, in either
      // direction.
      expect(response.isError).toBe(true)
    }
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('refuses to reinstate an evicted submission on a read-only grant', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'reinstate_evicted_submission_to_queue',
      arguments: {
        agentId: '974832d2-bfd0-4f38-a0d6-518be0d2571d',
        expectedSnapshot: 'ab'.repeat(32),
        reason: 'source review found no hang primitives, only latency work',
        confirmation: 'REINSTATE TO VALIDATOR QUEUE',
      },
    })

    expect(response.isError).toBe(true)
    expect(response.content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: expect.stringContaining('read-only') }),
      ]),
    )
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('refuses to evict live leases on a read-only grant', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'evict_live_validator_leases',
      arguments: {
        agentId: '974832d2-bfd0-4f38-a0d6-518be0d2571d',
        expectedSnapshot: 'ab'.repeat(32),
        reason: 'Hung through three full leases with zero scores reported',
        confirmation: 'EVICT LIVE VALIDATOR LEASES',
      },
    })

    expect(response.isError).toBe(true)
    expect(response.content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: expect.stringContaining('read-only') }),
      ]),
    )
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('lists stuck submissions as a read-scoped fleet triage view', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const snapshot = 'ab'.repeat(32)
    const payload = {
      generated_at: '2026-07-21T00:00:00Z',
      quorum: 3,
      counts: { exhausted: 1, cooling_down: 0 },
      count: 1,
      returned: 1,
      limit: 10,
      offset: 0,
      has_more: false,
      submissions: [
        {
          agent_id: agentId,
          miner_hotkey: '5Miner',
          agent_name: 'stuck-agent',
          agent_version: 4,
          bench_version: 4,
          score_count: 2,
          quorum: 3,
          retry_state: 'exhausted',
          automatic_retry_available: false,
          recovery_allowed: true,
          blocking_reason: null,
          earliest_retry_after: null,
          attempts_used: 5,
          exhausted_validator_count: 1,
          snapshot,
          ticket_states: { expired: 1 },
        },
      ],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'list_stuck_submissions',
      arguments: { state: ['exhausted', 'cooling_down'] },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      quorum: 3,
      counts: { exhausted: 1 },
      submissions: [{ agent_id: agentId, retry_state: 'exhausted' }],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validation-retries?state=exhausted&state=cooling_down&limit=10&offset=0',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('lists every stuck submission when no state filter is given', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        generated_at: '2026-07-21T00:00:00Z',
        quorum: 3,
        counts: {},
        count: 0,
        returned: 0,
        limit: 10,
        offset: 0,
        has_more: false,
        submissions: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'list_stuck_submissions',
      arguments: {},
    })

    expect(response.isError).not.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validation-retries?limit=10&offset=0',
      expect.anything(),
    )

    await client.close()
    await server.close()
  })

  it('requests one compact page from the platform', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const otherAgentId = '11111111-1111-4111-8111-111111111111'
    const submission = {
      agent_id: otherAgentId,
      miner_hotkey: '5Miner',
      agent_name: 'agent-1111',
      agent_version: 3,
      bench_version: 7,
      score_count: 1,
      quorum: 3,
      retry_state: 'exhausted',
      automatic_retry_available: false,
      recovery_allowed: true,
      blocking_reason: null,
      earliest_retry_after: null,
      attempts_used: 3,
      exhausted_validator_count: 3,
      snapshot: 'cd'.repeat(32),
      ticket_states: { expired: 2 },
    }
    const payload = {
      generated_at: '2026-07-25T18:00:00Z',
      quorum: 3,
      counts: { exhausted: 2 },
      count: 2,
      returned: 1,
      limit: 1,
      offset: 1,
      has_more: false,
      submissions: [submission],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const summary = await client.callTool({
      name: 'list_stuck_submissions',
      arguments: { limit: 1, offset: 1 },
    })
    expect(summary.isError).not.toBe(true)
    const summarised = readJsonResult(summary) as {
      submissions: Array<Record<string, unknown>>
      submissions_shared: Record<string, unknown>
      count: number
      limit: number
      offset: number
    }
    // The selected row survives with the snapshot a retry needs, while count
    // still reports the total matching fleet before server-side paging.
    expect(summarised.submissions).toEqual([
      {
        agent_id: otherAgentId,
        agent_name: 'agent-1111',
        snapshot: 'cd'.repeat(32),
        miner_hotkey: '5Miner',
        agent_version: 3,
        bench_version: 7,
        score_count: 1,
        retry_state: 'exhausted',
        automatic_retry_available: false,
        recovery_allowed: true,
        blocking_reason: null,
        recommended_action: null,
        dominant_failure_code: null,
        earliest_retry_after: null,
        attempts_used: 3,
        exhausted_validator_count: 3,
        silent_expiry_count: null,
        ticket_states: { expired: 2 },
      },
    ])
    expect(summarised).toMatchObject({ count: 2, limit: 1, offset: 1 })
    expect(summarised).not.toHaveProperty('submissions_shared')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validation-retries?limit=1&offset=1',
      expect.anything(),
    )

    await client.close()
    await server.close()
  })

  it('shows a re-lease loop as a re-lease loop, not as a silent validator', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const snapshot = 'ab'.repeat(32)
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        agent_id: agentId,
        miner_hotkey: '5Miner',
        agent_name: 'mnemox-v55',
        agent_version: 55,
        agent_status: 'evaluating',
        score_count: 0,
        quorum: 3,
        snapshot,
        automatic_retry_available: true,
        recovery_allowed: false,
        blocking_reason: null,
        withdrawal_allowed: false,
        withdrawal_blocking_reason:
          'submission can still reach quorum automatically',
        withdrawal: null,
        tickets: [
          {
            validator_hotkey: '5ValidatorA',
            slot_id: 'slot-1',
            status: 'expired',
            issued_at: '2026-07-27T15:00:00Z',
            deadline: '2026-07-27T16:30:00Z',
            bench_version: 7,
            attempt_count: 9,
            manual_retry_grants: 0,
            infra_retry_grants: 8,
            retry_after: null,
            retry_budget_exhausted: false,
            failure_reason: 'infrastructure',
            failed_at: '2026-07-27T16:29:00Z',
            silently_expired: false,
          },
        ],
        recoveries: [],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_validation_retry',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    const detail = readJsonResult(response) as {
      tickets: Array<Record<string, unknown>>
    }
    // Backroom used to strip all five of these. What is left without them is a
    // ticket that expired with a rewritten deadline and a rising attempt count,
    // which is byte-identical to a validator that stopped answering. With them,
    // eight no-fault grants against zero manual ones says the failures WERE
    // reported and the platform kept re-leasing on them.
    expect(detail.tickets[0]).toMatchObject({
      infra_retry_grants: 8,
      manual_retry_grants: 0,
      failure_reason: 'infrastructure',
      failed_at: '2026-07-27T16:29:00Z',
      silently_expired: false,
      slot_id: 'slot-1',
    })

    await client.close()
    await server.close()
  })

  it('reads the lease revocation ledger, empty answer included', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '974832d2-bfd0-4f38-a0d6-518be0d2571d'
    const rows = {
      generated_at: '2026-07-27T18:00:00Z',
      total: 2,
      revocations: [
        {
          audit_id: '44444444-4444-4444-8444-444444444444',
          agent_id: agentId,
          validator_hotkey: '5ValidatorA',
          slot_id: 'slot-1',
          bench_version: 7,
          action: 'operator_evicted',
          reason: 'operator_evicted_occupied_not_progressing',
          context: 'issue_ticket',
          recorded_at: '2026-07-27T17:59:00Z',
          evidence: {
            lease_age_seconds: 5400,
            original_deadline: '2026-07-27T18:30:00Z',
            heartbeat: { slot_id: 'slot-1', running: true },
          },
        },
        {
          audit_id: '55555555-5555-4555-8555-555555555555',
          agent_id: agentId,
          validator_hotkey: '5ValidatorB',
          slot_id: 'slot-2',
          bench_version: 7,
          action: 'operator_evicted',
          reason: 'operator_evicted_occupancy_unobservable',
          context: 'issue_ticket',
          recorded_at: '2026-07-27T17:59:00Z',
          evidence: { last_seen_at: null },
        },
      ],
    }
    const fetchMock = vi.fn().mockImplementation(() => Response.json(rows))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'list_lease_revocations',
      arguments: { agentId, action: ['operator_evicted'] },
    })

    expect(response.isError).not.toBe(true)
    const ledger = readJsonResult(response) as {
      total: number
      revocations: Array<Record<string, unknown>>
      revocations_shared: Record<string, unknown>
    }
    expect(ledger.total).toBe(2)
    // Each row keeps its own audit id, hotkey, slot, reason code, and — the
    // point of the tool — its own evidence object, whose keys differ per reason
    // code and are therefore never hoisted or trimmed.
    expect(ledger.revocations[0]).toMatchObject({
      audit_id: '44444444-4444-4444-8444-444444444444',
      reason: 'operator_evicted_occupied_not_progressing',
      evidence: {
        lease_age_seconds: 5400,
        heartbeat: { slot_id: 'slot-1', running: true },
      },
    })
    expect(ledger.revocations[1]).toMatchObject({
      evidence: { last_seen_at: null },
    })
    expect(ledger.revocations_shared).toMatchObject({
      agent_id: agentId,
      action: 'operator_evicted',
      context: 'issue_ticket',
    })

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(new URL(url).pathname).toBe('/api/v1/admin/lease-revocations')
    expect(new URL(url).searchParams.getAll('action')).toEqual([
      'operator_evicted',
    ])

    // An empty ledger is the production state today: force_expire_lease has
    // never fired. It must come back as a clean, readable zero rather than an
    // error an operator could mistake for a missing endpoint.
    fetchMock.mockImplementation(() =>
      Response.json({
        generated_at: '2026-07-27T18:00:00Z',
        total: 0,
        revocations: [],
      }),
    )
    const empty = await client.callTool({
      name: 'list_lease_revocations',
      arguments: {},
    })
    expect(empty.isError).not.toBe(true)
    expect(readJsonResult(empty)).toMatchObject({ total: 0, revocations: [] })

    await client.close()
    await server.close()
  })

  it('batch retries validator evaluation with per-item snapshot guards', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentA = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const agentB = '11111111-1111-4111-8111-111111111111'
    const snapshotA = 'ab'.repeat(32)
    const snapshotB = 'cd'.repeat(32)
    const reason = 'Verified validator datacenter outage across both runs'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        granted: 1,
        results: [
          { agent_id: agentA, status: 'granted', detail: null, recovery: null },
          {
            agent_id: agentB,
            status: 'skipped',
            detail: 'snapshot moved since inspection',
            recovery: null,
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'batch_retry_validator_evaluation',
      arguments: {
        reason,
        items: [
          { agentId: agentA, expectedSnapshot: snapshotA },
          { agentId: agentB, expectedSnapshot: snapshotB },
        ],
      },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      counts: { total: 2, granted: 1, idempotent: 0, skipped: 1 },
      results: {
        granted: { items: [{ agent_id: agentA }] },
        skipped: {
          items: [
            { agent_id: agentB, detail: 'snapshot moved since inspection' },
          ],
        },
      },
    })
    // The caller supplied no request ids; each is derived per agent from the
    // action itself, exactly as the single-agent retry derives it.
    expect(fetchMock).toHaveBeenCalledWith(
      'https://platform-api.heyditto.ai/api/v1/admin/validation-retries/batch-retry',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Admin-Actor': 'peyton@omniaura.ai' }),
        body: JSON.stringify({
          reason,
          items: [
            {
              agent_id: agentA,
              request_id: await deriveRequestId('validation-retry', [
                agentA,
                'peyton@omniaura.ai',
                reason,
                snapshotA,
              ]),
              expected_snapshot: snapshotA,
            },
            {
              agent_id: agentB,
              request_id: await deriveRequestId('validation-retry', [
                agentB,
                'peyton@omniaura.ai',
                reason,
                snapshotB,
              ]),
              expected_snapshot: snapshotB,
            },
          ],
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('rejects a batch retry with a duplicate agent id before any network call', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const agentA = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])

    const response = await client.callTool({
      name: 'batch_retry_validator_evaluation',
      arguments: {
        reason: 'Verified validator datacenter outage',
        items: [
          { agentId: agentA, expectedSnapshot: 'ab'.repeat(32) },
          { agentId: agentA, expectedSnapshot: 'cd'.repeat(32) },
        ],
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('does not batch retry without the write scope', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'batch_retry_validator_evaluation',
      arguments: {
        reason: 'Verified validator datacenter outage',
        items: [
          {
            agentId: '90cb5697-cbc1-40f4-a27e-439a7986a054',
            expectedSnapshot: 'ab'.repeat(32),
          },
        ],
      },
    })

    expect(response.isError).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()

    await client.close()
    await server.close()
  })

  it('reports agent scoring readiness as a read-scoped lease diagnosis', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const payload = {
      agent_id: agentId,
      agent_name: 'readiness-agent',
      miner_hotkey: '5Miner',
      status: 'evaluating',
      active_bench_version: 4,
      screening_policy_version: 8,
      required_screening_policy_version: 9,
      requires_screened_image: true,
      has_versioned_dataset: true,
      screened_image: {
        complete: false,
        verified: false,
        policy_ok: false,
        missing_fields: ['digest'],
      },
      leaseable: false,
      blocking_reasons: ['stale_screening_policy', 'screened_image_incomplete'],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'agent_scoring_readiness',
      arguments: { agentId },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      agent_id: agentId,
      leaseable: false,
      blocking_reasons: ['stale_screening_policy', 'screened_image_incomplete'],
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/agents/${agentId}/scoring-readiness`,
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('reports artifact-bound shadow coding certification history', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '90cb5697-cbc1-40f4-a27e-439a7986a054'
    const payload = {
      agent_id: agentId,
      agent_name: 'coding-agent',
      miner_hotkey: '5Miner',
      artifact_sha256: 'a'.repeat(64),
      screened_image_sha256: 'b'.repeat(64),
      coding_supported: true,
      coding_certified: true,
      active_certification_count: 1,
      total: 1,
      certifications: [
        {
          certification_row_id: '11111111-1111-4111-8111-111111111111',
          validator_hotkey: '5Validator',
          bench_version: 12,
          ticket_deadline: '2026-08-20T20:00:00Z',
          coding_contract_version: 1,
          certification_id: 'cert-001',
          status: 'certified',
          failure_stage: null,
          failure_code: null,
          certification_sha256: 'c'.repeat(64),
          canary_manifest_sha256: 'd'.repeat(64),
          screened_image_sha256: 'b'.repeat(64),
          transcript_object_key: `sha256/${'e'.repeat(64)}`,
          frozen_submission_object_key: `sha256/${'f'.repeat(64)}`,
          issued_at: '2026-08-20T18:00:00Z',
          expires_at: '2026-08-20T19:00:00Z',
          created_at: '2026-08-20T18:00:01Z',
          active: true,
          stale_reason: 'active',
        },
      ],
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const response = await client.callTool({
      name: 'get_agent_coding_certifications',
      arguments: { agentId, limit: 25 },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      agent_id: agentId,
      coding_certified: true,
      active_certification_count: 1,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `https://platform-api.heyditto.ai/api/v1/admin/agents/${agentId}/coding-certifications?limit=25`,
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer platform-admin-token',
        }),
      }),
    )

    await client.close()
    await server.close()
  })

  it('serves authoritative scores, leaderboard, and history to a read-only grant without the admin token', async () => {
    delete process.env.DITTO_ADMIN_API_TOKEN
    const agentId = '11111111-1111-4111-8111-111111111111'
    const entry = {
      rank: 1,
      finalized: true,
      score_count: 3,
      score_quorum: 3,
      agent_id: agentId,
      agent_name: 'apex-agent',
      agent_version: 4,
      miner_hotkey: '5TopMiner',
      miner_uid: 12,
      registered: true,
      emission_eligible: true,
      composite: 0.957,
      raw_composite: 0.965,
      composite_stderr: 0.003,
      tool_mean: 0.981,
      memory_mean: 0.933,
      first_seen: '2026-07-01T00:00:00Z',
      median_ms: 2100,
      n: 40,
      eligible: true,
      bench_version: 7,
      dataset_sha256: 'ab'.repeat(32),
      composite_breakdown: null,
      history: [0.91, 0.957],
    }
    const leaderboard = {
      generated_at: '2026-07-23T00:00:00Z',
      count: 1,
      current_bench_version: 7,
      active_bench_version: 7,
      desired_bench_version: 7,
      available_bench_versions: [7, 6],
      selection_mode: 'authoritative',
      entries: [entry],
      emissions: null,
    }
    const scoreRow = (overrides: Record<string, unknown>) => ({
      validator_hotkey: '5ValA',
      composite: 0.957,
      tool_mean: 0.981,
      memory_mean: 0.933,
      median_ms: 2100,
      n: 40,
      bench_version: 7,
      seed: 424242,
      run_id: 'run-a7',
      generated_at: '2026-07-21T00:00:00Z',
      ...overrides,
    })
    const agentScores = {
      agent_id: agentId,
      miner_hotkey: '5TopMiner',
      status: 'scored',
      quorum: 3,
      score_count: 6,
      median_composite: 0.957,
      dataset_seed: 987654321,
      dataset_sha256: 'cd'.repeat(32),
      dataset_run_size: 'full',
      scores: [
        scoreRow({ composite: 0.91, bench_version: 6, seed: 111, run_id: 'run-a6' }),
        scoreRow({ validator_hotkey: '5ValB', composite: 0.92, bench_version: 6, seed: 111, run_id: 'run-b6' }),
        scoreRow({ validator_hotkey: '5ValC', composite: 0.9, bench_version: 6, seed: 111, run_id: 'run-c6' }),
        scoreRow({}),
        scoreRow({ validator_hotkey: '5ValB', composite: 0.955, run_id: 'run-b7' }),
        scoreRow({ validator_hotkey: '5ValC', composite: 0.96, run_id: 'run-c7' }),
      ],
      generated_at: '2026-07-23T00:00:00Z',
    }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      void init
      return Promise.resolve(
        String(url).includes('/public/leaderboard')
          ? Response.json(leaderboard)
          : Response.json(agentScores),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])

    const scores = await client.callTool({
      name: 'get_agent_scores',
      arguments: { minerHotkey: '5TopMiner' },
    })
    expect(scores.isError).not.toBe(true)
    expect(readJsonResult(scores)).toMatchObject({
      agent_id: agentId,
      median_composite: 0.957,
      active_bench_version: 7,
      leaderboard: { rank: 1, emission_eligible: true },
    })

    const board = await client.callTool({
      name: 'get_leaderboard',
      arguments: { status: 'finalized', limit: 10 },
    })
    expect(board.isError).not.toBe(true)
    expect(readJsonResult(board)).toMatchObject({
      count: 1,
      selection_mode: 'authoritative',
      entries: [{ rank: 1, agent_id: agentId, composite: 0.957 }],
    })

    const history = await client.callTool({
      name: 'get_score_history',
      arguments: { agentId },
    })
    expect(history.isError).not.toBe(true)
    expect(readJsonResult(history)).toMatchObject({
      agent_id: agentId,
      total_score_count: 6,
      versions: [
        { bench_version: 6, median_composite: 0.91 },
        { bench_version: 7, median_composite: 0.957 },
      ],
    })

    // Every score read stayed on the credential-free public ledger.
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).toContain('/api/v1/public/')
      const headers = (call[1] as { headers?: Record<string, string> } | undefined)?.headers
      expect(headers ?? {}).not.toHaveProperty('Authorization')
    }

    await client.close()
    await server.close()
  })
  it('resolves an owner footprint on read scope alone, with standings joined', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'platform-admin-token'
    const agentId = '55555555-5555-4555-8555-555555555555'
    const footprint = {
      identifier: '5TopMiner',
      identifier_kind: 'miner_hotkey',
      depth: 1,
      miner_coldkeys: ['5Cold'],
      hotkey_count: 2,
      submission_count: 3,
      expansion_complete: true,
      ownership_basis: 'evaluation_payment_records',
      linkage_caveat:
        'Coldkeys here are payment-time records of who paid for each evaluation, not on-chain metagraph ownership.',
      hotkeys: [
        {
          miner_hotkey: '5TopMiner',
          miner_coldkeys: ['5Cold'],
          link_hop: 0,
          submission_count: 2,
          paid_submission_count: 2,
          latest_submitted_at: '2026-07-22T00:00:00Z',
          agents_truncated: false,
          agents: [
            {
              agent_id: agentId,
              agent_name: 'apex-agent',
              agent_version: 4,
              agent_status: 'scored',
              artifact_sha256: 'ab'.repeat(32),
              submitted_at: '2026-07-22T00:00:00Z',
              miner_coldkey: '5Cold',
            },
          ],
        },
        {
          miner_hotkey: '5Sibling',
          miner_coldkeys: ['5Cold'],
          link_hop: 1,
          submission_count: 1,
          paid_submission_count: 0,
          latest_submitted_at: '2026-07-19T00:00:00Z',
          agents_truncated: false,
          agents: [],
        },
      ],
    }
    const leaderboard = {
      generated_at: '2026-07-23T00:00:00Z',
      count: 1,
      current_bench_version: 7,
      active_bench_version: 7,
      desired_bench_version: 7,
      available_bench_versions: [7],
      selection_mode: 'authoritative',
      entries: [
        {
          rank: 1,
          finalized: true,
          score_count: 3,
          score_quorum: 3,
          agent_id: agentId,
          agent_name: 'apex-agent',
          agent_version: 4,
          miner_hotkey: '5TopMiner',
          miner_uid: 12,
          registered: true,
          emission_eligible: true,
          composite: 0.957,
          tool_mean: 0.981,
          memory_mean: 0.933,
          first_seen: '2026-07-01T00:00:00Z',
          eligible: true,
          bench_version: 7,
        },
      ],
      emissions: null,
    }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      void init
      return Promise.resolve(
        String(url).includes('/admin/leaderboard')
          ? Response.json(leaderboard)
          : Response.json(footprint),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    // No artifact scope and no write scope: coldkey is identity metadata.
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    const response = await client.callTool({
      name: 'get_miner_owner_footprint',
      arguments: { key: '5TopMiner' },
    })

    expect(response.isError).not.toBe(true)
    expect(readJsonResult(response)).toMatchObject({
      identifier_kind: 'miner_hotkey',
      ownership_basis: 'evaluation_payment_records',
      hotkey_count: 2,
      ranked_hotkey_count: 1,
      expansion_complete: true,
      hotkeys: [
        { miner_hotkey: '5TopMiner', leaderboard: { rank: 1, emission_eligible: true } },
        { miner_hotkey: '5Sibling', leaderboard: null },
      ],
    })

    // Linkage and standings both ride the admin token so reserved-handle
    // collisions keep their stored names.
    const [linkageUrl, linkageInit] = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/admin/miner-owners/'),
    )!
    expect(String(linkageUrl)).toContain('depth=1')
    expect(
      (linkageInit as { headers: Record<string, string> }).headers,
    ).toHaveProperty('Authorization')
    const boardCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/admin/leaderboard'),
    )!
    expect(
      (boardCall[1] as { headers: Record<string, string> }).headers,
    ).toHaveProperty('Authorization')

    await client.close()
    await server.close()
  })
})
