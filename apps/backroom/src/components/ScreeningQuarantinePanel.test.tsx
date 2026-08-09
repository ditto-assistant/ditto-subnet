// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  ScreeningDispute,
  ScreeningQuarantine,
  ScreeningQuarantineContext,
  ScreeningSubmission,
  ShadowReviewObservation,
} from '../lib/admin.schemas'
import { ScreeningQuarantinePanel } from './ScreeningQuarantinePanel'

vi.mock('@tanstack/react-start', () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props}>{children}</a>
  ),
}))

vi.mock('../server/admin.functions', () => ({
  decideScreeningDispute: vi.fn(),
  decideScreeningQuarantine: vi.fn(),
  executeScreeningQuarantineDecisions: vi.fn(),
  getOwnerAttestations: vi.fn(() => new Promise(() => {})),
  getScreeningArtifact: vi.fn(),
  getScreeningBaselineDiff: vi.fn(),
  readScreeningBaselineDiffFile: vi.fn(),
  getScreeningQuarantineContext: vi.fn(() => new Promise(() => {})),
  listScreeningQuarantines: vi.fn(),
  listScreeningDisputes: vi.fn(),
  listScreeningSourceFiles: vi.fn(),
  previewScreeningQuarantineDecisions: vi.fn(),
  readScreeningSourceFile: vi.fn(),
}))

import {
  executeScreeningQuarantineDecisions,
  getOwnerAttestations,
  getScreeningQuarantineContext,
  listScreeningQuarantines,
  previewScreeningQuarantineDecisions,
  readScreeningSourceFile,
} from '../server/admin.functions'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const quarantine: ScreeningQuarantine = {
  quarantine_id: '11111111-1111-4111-8111-111111111111',
  agent_id: '22222222-2222-4222-8222-222222222222',
  attempt_id: '33333333-3333-4333-8333-333333333333',
  miner_hotkey: 'miner-hotkey',
  miner_coldkey: 'miner-coldkey',
  agent_name: 'Review agent',
  agent_version: 3,
  artifact_sha256: 'a'.repeat(64),
  policy_version: 6,
  manifest_digest: 'b'.repeat(64),
  finding_digest: 'c'.repeat(64),
  reason_code: 'suspicious_source',
  evidence: [
    {
      module_id: 'luna-source-review',
      code: 'agentic-source-review-tripwire',
      summary: 'private source analysis selected a behavioral audit',
      digest: 'c'.repeat(64),
    },
  ],
  finding: {
    artifact_sha256: 'a'.repeat(64),
    prompt_revision: 'source-review-v2',
    risk_level: 'high',
    confidence: 0.97,
    categories: ['benchmark_emulation'],
    evidence: [{ path: 'src/main.rs', line: 42, category: 'benchmark_emulation' }],
    summary: 'Deterministic shortcut bypasses the general provider path.',
  },
  finding_verified: true,
  status: 'active',
  created_at: '2026-07-14T15:00:00Z',
  resolved_at: null,
  resolved_by: null,
  resolution: null,
  resolution_reason: null,
}

const quarantineContext: ScreeningQuarantineContext = {
  quarantine,
  agent: {
    agent_id: quarantine.agent_id,
    miner_hotkey: quarantine.miner_hotkey,
    miner_coldkey: quarantine.miner_coldkey,
    agent_name: quarantine.agent_name,
    artifact_sha256: quarantine.artifact_sha256,
    agent_status: 'quarantined',
    size_bytes: 20480,
    submitted_at: '2026-07-14T14:00:00Z',
    screening_policy_version: 7,
    screening_reason: 'Submission held for anti-cheat review',
  },
  attempts: [
    {
      attempt_id: quarantine.attempt_id,
      policy_version: 7,
      status: 'quarantined',
      screener_hotkey: 'screener-hotkey',
      started_at: '2026-07-14T14:00:00Z',
      deadline: '2026-07-14T14:30:00Z',
      finished_at: '2026-07-14T14:05:00Z',
      reason: 'Submission held for anti-cheat review',
      reason_code: 'agentic-source-review-tripwire',
      duplicate_of: null,
      duplicate_name: null,
      duplicate_version: null,
    },
  ],
  miner: {
    miner_hotkey: quarantine.miner_hotkey,
    miner_coldkeys: ['miner-coldkey'],
    total_submissions: 6,
    quarantine_count: 3,
    released_count: 1,
    rescreened_count: 0,
    rejected_count: 1,
    recent_quarantines: [
      {
        quarantine_id: '44444444-4444-4444-8444-444444444444',
        agent_id: '55555555-5555-4555-8555-555555555555',
        agent_name: 'Review agent v1',
        reason_code: 'behavioral-oracle-wrong-answer',
        status: 'resolved',
        resolution: 'reject',
        resolution_reason: 'Static answer table confirmed',
        created_at: '2026-07-10T10:00:00Z',
        resolved_at: '2026-07-11T10:00:00Z',
      },
    ],
  },
  duplicates: [
    {
      agent_id: '66666666-6666-4666-8666-666666666666',
      miner_hotkey: 'other-miner-hotkey',
      miner_coldkey: 'other-miner-coldkey',
      agent_name: 'Copycat agent',
      agent_status: 'uploaded',
      submitted_at: '2026-07-13T10:00:00Z',
      match: 'identical_artifact',
      same_owner: false,
    },
  ],
  duplicate_summary: {
    total: 3,
    cross_miner: 2,
    same_miner: 1,
    cross_owner: 2,
    same_owner: 1,
    sample_truncated: true,
  },
  // The common case: shadow mode off, or a quarantine older than the reviewer.
  shadow_review: null,
}

const longCausalPath = `src/${'nested/'.repeat(20)}serve.ts`
const causalQuarantine: ScreeningQuarantine = {
  ...quarantine,
  finding: {
    ...quarantine.finding!,
    evidence: [
      { path: longCausalPath, line: 42, category: 'benchmark_emulation' },
      { path: 'src/score.ts', line: 87, category: 'benchmark_emulation' },
    ],
    causal_evidence: {
      schema_version: 2,
      authority_transition: 'model_output_overwritten',
      scorer_visible_effect: 'answer',
      role_bindings: [
        { path: longCausalPath, line: 42, category: 'benchmark_emulation', role: 'served_trigger' },
        { path: longCausalPath, line: 42, category: 'benchmark_emulation', role: 'authority_bypass' },
        { path: 'src/score.ts', line: 87, category: 'benchmark_emulation', role: 'scorer_visible_effect' },
        { path: 'src/score.ts', line: 87, category: 'benchmark_emulation', role: 'reachability_link' },
      ],
    },
  },
}

const shadowReview: ShadowReviewObservation = {
  attempt_id: quarantine.attempt_id,
  agent_id: quarantine.agent_id,
  settings_revision: 12,
  settings_scope: 'ditto-screener-prod',
  settings_checksum: 'd'.repeat(64),
  disposition: 'safe',
  risk_level: 'low',
  categories: ['none'],
  finding_digest: null,
  resolution_basis: 'authoritative_model_tool_path',
  clearance_path: 'l3_adjudicated_safe',
  critic_disposition: 'confirm_safe',
  adjudicator_disposition: 'confirm_safe',
  response_models: ['moonshotai/kimi-k3', 'openai/gpt-5.6-sol'],
  response_providers: ['openrouter', 'openrouter'],
  usage: {
    input_tokens: 41000,
    output_tokens: 3100,
    cached_input_tokens: 26000,
    reasoning_tokens: 900,
    estimated_cost_usd: 0.82,
    reported_cost_usd: 0.79,
  },
  created_at: '2026-07-14T14:04:00Z',
}

const submission: ScreeningSubmission = {
  agent_id: quarantine.agent_id,
  miner_hotkey: quarantine.miner_hotkey,
  miner_coldkey: quarantine.miner_coldkey,
  agent_name: 'History agent',
  agent_version: null,
  artifact_sha256: quarantine.artifact_sha256,
  agent_status: 'screening_rejected',
  screening_policy_version: 6,
  screening_reason: 'Build did not pass screening',
  submitted_at: '2026-07-14T14:00:00Z',
  attempts: [
    {
      attempt_id: quarantine.attempt_id,
      policy_version: 6,
      status: 'rejected',
      screener_hotkey: 'screener-hotkey',
      started_at: '2026-07-14T14:00:00Z',
      deadline: '2026-07-14T14:10:00Z',
      finished_at: '2026-07-14T14:02:00Z',
      reason: 'Static analysis rejected the source',
      reason_code: null,
      duplicate_of: '77777777-7777-4777-8777-777777777777',
      duplicate_name: 'Jackie',
      duplicate_version: 2,
    },
  ],
}

const dispute: ScreeningDispute = {
  dispute_id: '88888888-8888-4888-8888-888888888888',
  agent_id: quarantine.agent_id,
  quarantine_id: quarantine.quarantine_id,
  miner_hotkey: quarantine.miner_hotkey,
  agent_name: 'Disputed agent',
  agent_version: 2,
  artifact_sha256: quarantine.artifact_sha256,
  message: 'The flagged code is generic routing and does not contain benchmark answers.',
  status: 'pending',
  created_at: '2026-07-15T15:00:00Z',
  original_reason: 'Source appeared benchmark-specific.',
  resolved_at: null,
  resolved_by: null,
  resolution: null,
  resolution_reason: null,
}

describe('ScreeningQuarantinePanel', () => {
  it('shows a signed linked identity as its own labelled, scoped signal', async () => {
    vi.mocked(getOwnerAttestations).mockResolvedValue({
      hotkey: quarantine.miner_hotkey,
      netuid: 118,
      attestations: [
        {
          attestation_id: '77777777-7777-4777-8777-777777777777',
          netuid: 118,
          hotkey_lo: '5AlphaLinkedHotkey',
          hotkey_hi: quarantine.miner_hotkey,
          counterparty: '5AlphaLinkedHotkey',
          evidence_grade: 'hotkey-hotkey',
          lo_key_kind: 'hotkey',
          lo_signer: '5AlphaLinkedHotkey',
          hi_key_kind: 'hotkey',
          hi_signer: quarantine.miner_hotkey,
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
          hotkey_hi: quarantine.miner_hotkey,
          counterparty: '5BravoLinkedHotkey',
          evidence_grade: 'coldkey-coldkey',
          lo_key_kind: 'coldkey',
          lo_signer: '5BravoPayingColdkey',
          hi_key_kind: 'coldkey',
          hi_signer: '5MinerPayingColdkey',
          nonce: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          issued_at: '2026-05-01T00:00:00Z',
          created_at: '2026-05-01T00:05:00Z',
          revoked_at: '2026-06-01T00:00:00Z',
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
      ],
      linkage_basis: 'signed_owner_attestation',
      scope_caveat:
        'Exempts near-duplicate plagiarism screening between the linked hotkeys only; not an input to emission-slot allocation.',
    })

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(vi.mocked(getOwnerAttestations)).toHaveBeenCalledWith({
      data: { hotkey: quarantine.miner_hotkey },
    })

    const label = await screen.findByText('Linked identity (signed)')
    // A signed link and payment-coldkey inference are different classes of
    // evidence and must never read as the same row.
    expect(label.getAttribute('title')).toContain('both hotkeys signed')
    expect(label.getAttribute('title')).toContain('not transitive')
    // The counterparty is named for each link; the pair is symmetric, so
    // nothing here claims one hotkey came before the other.
    expect(screen.getByText('5AlphaLinkedHotkey')).toBeTruthy()
    expect(screen.getByText('5BravoLinkedHotkey')).toBeTruthy()
    // The grade is shown as reviewer context, never as a gate.
    expect(
      screen.getByText(/Both halves signed by hotkey/).textContent,
    ).toContain('every grade establishes the link')
    expect(
      screen.getByText(/Both halves signed by payment coldkey/).textContent,
    ).toContain('every grade establishes the link')
    // The scope claim travels with the link: screening exemption only.
    expect(
      screen.getByText(/emission-slot\s+allocation/i).textContent,
    ).toContain('not payment-coldkey inference')
    // A revoked link stays visible and marked, because whether it was live
    // when this submission was held is what a dispute turns on.
    expect(screen.getByText(/^Revoked /)).toBeTruthy()
  })

  it('says the link check is unavailable rather than implying no linked identity', async () => {
    vi.mocked(getOwnerAttestations).mockRejectedValue(
      new Error('platform did not answer'),
    )

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(
      (await screen.findByText(/Link check unavailable/)).textContent,
    ).toContain('unknown rather than absent')
  })

  it('reviews the oldest quarantine first and requests alternate API sorting', async () => {
    const older = {
      ...quarantine,
      quarantine_id: '00000000-0000-4000-8000-000000000001',
      agent_id: '00000000-0000-4000-8000-000000000002',
      attempt_id: '00000000-0000-4000-8000-000000000003',
      agent_name: 'Older agent',
      created_at: '2026-07-13T15:00:00Z',
    }
    const newer = {
      ...quarantine,
      quarantine_id: '99999999-9999-4999-8999-999999999991',
      agent_id: '99999999-9999-4999-8999-999999999992',
      attempt_id: '99999999-9999-4999-8999-999999999993',
      agent_name: 'Newer agent',
      created_at: '2026-07-15T15:00:00Z',
    }

    render(
      <ScreeningQuarantinePanel
        initialItems={[older, newer]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    const workspace = screen.getByRole('region', {
      name: 'Screening quarantine review workspace',
    })
    const sort = screen.getByRole('combobox', { name: 'Sort quarantines' })

    expect(sort).toHaveProperty('value', 'oldest')
    expect(screen.getByRole('heading', { name: 'Older agent' })).toBeTruthy()
    expect(
      Array.from(workspace.querySelectorAll('button p.text-sm')).map((item) => item.textContent),
    ).toEqual(['Older agent', 'Newer agent'])

    vi.mocked(listScreeningQuarantines).mockResolvedValue({
      items: [newer, older],
      count: 2,
    })
    fireEvent.change(sort, { target: { value: 'newest' } })

    await waitFor(() => {
      expect(
        Array.from(workspace.querySelectorAll('button p.text-sm')).map((item) => item.textContent),
      ).toEqual(['Newer agent', 'Older agent'])
    })
    expect(vi.mocked(listScreeningQuarantines)).toHaveBeenCalledWith({
      data: { status: 'active', sort: 'newest' },
    })
  })

  it('puts the source download in the active review workspace', () => {
    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(screen.getByRole('tab', { name: /Review queue/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('heading', { name: 'Review agent' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download source' }).getAttribute('disabled')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Miner-visible reason' })).toBeTruthy()
    expect(screen.getByText(/Do not include private evidence or secrets/)).toBeTruthy()
    expect(screen.queryByText('All screening outcomes')).toBeNull()
  })

  it('previews a per-item batch before executing it', async () => {
    const second = {
      ...quarantine,
      quarantine_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      agent_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      attempt_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      agent_name: 'Second review agent',
    }
    vi.mocked(previewScreeningQuarantineDecisions).mockResolvedValue({
      preview_token: '1234567890.abcdefghijklmnopqrstuvwxyz1234567890abcdef',
      expires_at: '2026-07-17T16:10:00Z',
      ready_count: 2,
      already_applied_count: 0,
      blocked_count: 0,
      items: [quarantine, second].map((item) => ({
        quarantine_id: item.quarantine_id,
        agent_id: item.agent_id,
        agent_name: item.agent_name,
        artifact_sha256: item.artifact_sha256,
        resolution: 'rescreen' as const,
        reason: 'Re-run both artifacts against the current screening policy',
        disposition: 'ready' as const,
        resulting_agent_status: 'screening_failed',
        message: 'will set submission status to screening_failed',
      })),
    })
    vi.mocked(executeScreeningQuarantineDecisions).mockResolvedValue({
      items: [quarantine, second].map((item) => ({
        quarantine_id: item.quarantine_id,
        status: 'applied' as const,
        agent_status: 'screening_failed',
        message: 'decision applied and audit event recorded',
      })),
      applied_count: 2,
      already_applied_count: 0,
      failed_count: 0,
    })
    vi.mocked(listScreeningQuarantines).mockResolvedValue({ items: [], count: 0 })

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine, second]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all shown quarantines' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Shared batch reason' }), {
      target: { value: 'Re-run both artifacts against the current screening policy' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply reason to selected' }))
    fireEvent.click(screen.getByRole('button', { name: 'Preview 2 decisions' }))

    await waitFor(() =>
      expect(previewScreeningQuarantineDecisions).toHaveBeenCalledTimes(1),
    )
    expect(executeScreeningQuarantineDecisions).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText(/Preview only/)).toBeTruthy()

    fireEvent.click(
      screen.getByRole('button', { name: 'Confirm and execute reviewed decisions' }),
    )
    await waitFor(() =>
      expect(executeScreeningQuarantineDecisions).toHaveBeenCalledWith({
        data: expect.objectContaining({ confirmed: true }),
      }),
    )
  })

  it('allows a detailed source-evidence resolution reason over 500 characters', () => {
    const detailedReason = [
      'Source review evidence shows src/router.ts:118 selects providers from the declared runtime configuration instead of matching benchmark prompts.',
      'The branch at src/router.ts:146 handles a documented timeout fallback and does not inspect prompt text, expected answers, evaluator metadata, or test fixture identifiers.',
      'A repository-wide search found no embedded benchmark answers, prompt hashes, fixture names, response lookup tables, or network calls to undeclared services.',
      'The submitted Docker image was rebuilt from the reviewed archive, then smoke-tested with unrelated prompts that exercised both the primary provider and fallback path.',
      'Observed outputs varied with the request and provider response, which is inconsistent with replay or benchmark emulation.',
      'Release is appropriate because the suspicious fast path is general routing logic; retain this source-level evidence in the audited miner-visible decision.',
    ].join(' ')
    expect(detailedReason.length).toBeGreaterThan(500)

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    const reason = screen.getByRole('textbox', { name: 'Miner-visible reason' })
    expect(reason.getAttribute('maxlength')).toBeNull()
    fireEvent.change(reason, { target: { value: detailedReason } })
    expect(reason).toHaveProperty('value', detailedReason)
    expect(
      screen.getByText(`${detailedReason.length} characters · minimum 3`),
    ).toBeTruthy()
  })

  it('separates screening history into its own view', () => {
    render(
      <ScreeningQuarantinePanel
        view="history"
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(screen.getByRole('tab', { name: /Screening history/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('All screening outcomes')).toBeTruthy()
    expect(screen.getByText('History agent')).toBeTruthy()
    expect(screen.getByText('Legacy submission')).toBeTruthy()
    expect(screen.getByText('Compared with', { exact: false })).toBeTruthy()
    expect(screen.getByText('Jackie')).toBeTruthy()
    expect(screen.getByText(/Submission v2/)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Review agent' })).toBeNull()
  })

  it('keeps artifact downloads disabled for read-only admins', () => {
    const { unmount } = render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly
      />,
    )

    expect(screen.getByRole('button', { name: 'Download source' }).getAttribute('disabled')).not.toBeNull()

    unmount()
    render(
      <ScreeningQuarantinePanel
        view="history"
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly
      />,
    )
    expect(screen.getByRole('button', { name: 'Download source' }).getAttribute('disabled')).not.toBeNull()
  })

  it('shows the evidence trail and digest-verified source review finding', () => {
    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Why it was quarantined')).toBeTruthy()
    expect(
      screen.getByText('private source analysis selected a behavioral audit'),
    ).toBeTruthy()
    expect(screen.getByText('Automated source review')).toBeTruthy()
    expect(screen.getByText('high risk')).toBeTruthy()
    expect(screen.getByText('97% confidence')).toBeTruthy()
    expect(screen.getByText('digest verified')).toBeTruthy()
    expect(
      screen.getByText('Deterministic shortcut bypasses the general provider path.'),
    ).toBeTruthy()
    expect(screen.getByText('src/main.rs:42')).toBeTruthy()
  })

  it('expands and collapses long quarantine summaries', () => {
    const longSummary =
      'The submitted server path recognizes question forms, sorts user memories, selects date-applicable facts, and injects counts and current entries before the model runs. This deterministic behavior continues with enough detail to require expansion.'
    const detailedQuarantine: ScreeningQuarantine = {
      ...quarantine,
      finding: quarantine.finding
        ? { ...quarantine.finding, summary: longSummary }
        : null,
    }

    render(
      <ScreeningQuarantinePanel
        initialItems={[detailedQuarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    const summary = screen.getByText(longSummary)
    const showAll = screen.getByRole('button', { name: 'Show all' })
    expect(summary.className).toContain('line-clamp-2')
    expect(showAll.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(showAll)
    expect(summary.className).not.toContain('line-clamp-2')
    expect(
      screen.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded'),
    ).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: 'Show less' }))
    expect(summary.className).toContain('line-clamp-2')
  })

  it('suppresses risk labels for findings that fail digest verification', () => {
    const unverified: ScreeningQuarantine = {
      ...quarantine,
      finding_verified: false,
    }
    render(
      <ScreeningQuarantinePanel
        initialItems={[unverified]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    // Neither the queue chip nor the finding card may present a risk level
    // the signed digest does not back.
    expect(screen.queryByText('high')).toBeNull()
    expect(screen.queryByText('high risk')).toBeNull()
    expect(screen.getByText('unverified')).toBeTruthy()
    expect(screen.getByText('not verified')).toBeTruthy()
  })

  it('loads miner history and duplicate warnings from the review context', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue(quarantineContext)

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Miner track record')).toBeTruthy()
    })
    expect(vi.mocked(getScreeningQuarantineContext)).toHaveBeenCalledWith({
      data: { quarantineId: quarantine.quarantine_id },
    })
    expect(
      screen.getByText('Identical code exists under 2 other payment owners'),
    ).toBeTruthy()
    expect(screen.getByText(/different owner/)).toBeTruthy()
    expect(
      screen.getByText(/Showing a sample; 3 duplicates exist in total/),
    ).toBeTruthy()
    expect(screen.getByText(/Static answer table confirmed/)).toBeTruthy()
  })

  it('flags a shadow review that diverges from the L1 quarantine', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue({
      ...quarantineContext,
      shadow_review: shadowReview,
    })

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Shadow source review (L2/L3)')).toBeTruthy()
    })
    // L1 quarantined on a high-risk finding; L2/L3 adjudicated it safe.
    expect(screen.getByText('Diverges from the quarantine')).toBeTruthy()
    expect(screen.queryByText('Concurs with the quarantine')).toBeNull()
    // The advisory boundary must be visible, not just documented.
    expect(screen.getByText('non-authoritative')).toBeTruthy()
    expect(screen.getByText('l3 adjudicated safe')).toBeTruthy()
    expect(screen.getByText('moonshotai/kimi-k3')).toBeTruthy()
    expect(screen.getByText(/2 stages/)).toBeTruthy()
    expect(screen.getByText(/\$0\.79/)).toBeTruthy()
    // The L1 finding still stands beside it.
    expect(screen.getByText('high risk')).toBeTruthy()
  })

  it('marks a shadow review that concurs with the L1 quarantine', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue({
      ...quarantineContext,
      shadow_review: {
        ...shadowReview,
        disposition: 'violation',
        risk_level: 'high',
        categories: ['benchmark_emulation'],
        critic_disposition: 'uphold_violation',
        adjudicator_disposition: 'uphold_violation',
      },
    })

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Concurs with the quarantine')).toBeTruthy()
    })
    expect(screen.queryByText('Diverges from the quarantine')).toBeNull()
  })

  it('renders a quarantine with no shadow review as an explicit absence', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue(quarantineContext)

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Miner track record')).toBeTruthy()
    })
    expect(screen.getByText(/No shadow review accompanies this quarantine/)).toBeTruthy()
    expect(screen.queryByText('Shadow source review (L2/L3)')).toBeNull()
    expect(screen.queryByText('Diverges from the quarantine')).toBeNull()
    expect(screen.queryByRole('region', { name: 'Causal authority evidence' })).toBeNull()
  })

  it('renders complete causal authority proof with long source paths accessibly', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue({
      ...quarantineContext,
      quarantine: causalQuarantine,
    })

    render(
      <ScreeningQuarantinePanel
        initialItems={[causalQuarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    const causal = await screen.findByRole('region', { name: 'Causal authority evidence' })
    expect(causal.textContent).toContain('model output overwritten')
    expect(causal.textContent).toContain('answer')
    for (const role of ['served trigger', 'authority bypass', 'scorer visible effect', 'reachability link']) {
      expect(causal.textContent).toContain(role)
    }
    expect(screen.getAllByTitle(`${longCausalPath}:42`)).toHaveLength(2)
    expect(screen.getByRole('button', { name: new RegExp(`${longCausalPath}:42`) })).toBeTruthy()
  })

  it('shows flagged source lines on demand and gates them on write access', async () => {
    vi.mocked(getScreeningQuarantineContext).mockResolvedValue(quarantineContext)
    vi.mocked(readScreeningSourceFile).mockResolvedValue({
      agent_id: quarantine.agent_id,
      path: 'src/main.rs',
      total_lines: 60,
      start_line: 28,
      end_line: 56,
      lines: [{ line: 42, text: '    fast_path();' }],
    })

    const { unmount } = render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /src\/main\.rs:42/ }))
    await waitFor(() => {
      expect(screen.getByText('fast_path();')).toBeTruthy()
    })
    expect(vi.mocked(readScreeningSourceFile)).toHaveBeenCalledWith({
      data: {
        agentId: quarantine.agent_id,
        path: 'src/main.rs',
        startLine: 28,
        endLine: 56,
      },
    })
    unmount()

    render(
      <ScreeningQuarantinePanel
        initialItems={[quarantine]}
        initialSubmissions={[submission]}
        readOnly
      />,
    )
    expect(
      screen.getByRole('button', { name: /src\/main\.rs:42/ }).getAttribute('disabled'),
    ).not.toBeNull()
    expect(
      screen.getByRole('button', { name: /Browse source files/ }).getAttribute('disabled'),
    ).not.toBeNull()
  })

  it('shows the private miner dispute beside the original reason and source download', () => {
    render(
      <ScreeningQuarantinePanel
        view="disputes"
        initialItems={[quarantine]}
        initialDisputes={[dispute]}
        initialSubmissions={[submission]}
        readOnly={false}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Disputed agent' })).toBeTruthy()
    expect(screen.getAllByText(dispute.message)).toHaveLength(2)
    expect(screen.getByText(dispute.original_reason ?? '')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download source' })).toBeTruthy()
    expect(screen.getByText('Accept and release')).toBeTruthy()
    expect(screen.getByText('Uphold rejection')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Miner-visible response' })).toBeTruthy()
  })

  it('shows server-backed history pagination from the total result count', () => {
    render(
      <ScreeningQuarantinePanel
        view="history"
        initialItems={[]}
        initialSubmissions={[submission]}
        submissionCount={294}
        page={2}
        pageSize={50}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Showing 51–100 of 294')).toBeTruthy()
    expect(screen.getByText('Page 2 of 6')).toBeTruthy()
    expect(screen.getByText('Previous')).toBeTruthy()
    expect(screen.getByText('Next')).toBeTruthy()
  })
})
