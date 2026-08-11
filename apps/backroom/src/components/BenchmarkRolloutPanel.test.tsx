// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  benchmarkRolloutConfirmation,
  type BenchmarkRolloutControl,
} from '../lib/admin.schemas'
import { BenchmarkRolloutPanel } from './BenchmarkRolloutPanel'

const getBenchmarkRolloutControl = vi.fn()
const startBenchmarkRollout = vi.fn()
const supersedeBenchmarkRollout = vi.fn()
const selectActiveBenchmark = vi.fn()

vi.mock('@tanstack/react-start', () => ({ useServerFn: (value: unknown) => value }))
vi.mock('../server/admin.functions', () => ({
  getBenchmarkRolloutControl: () => getBenchmarkRolloutControl(),
  startBenchmarkRollout: (input: unknown) => startBenchmarkRollout(input),
  supersedeBenchmarkRollout: (input: unknown) => supersedeBenchmarkRollout(input),
  selectActiveBenchmark: (input: unknown) => selectActiveBenchmark(input),
}))

const contracts = [
  {
    version: 5,
    minimum_screening_policy_version: 9,
    requires_screened_image: true,
    capable_validator_count: 4,
    start_ready: true,
    start_blockers: [],
  },
  {
    version: 6,
    minimum_screening_policy_version: 9,
    requires_screened_image: true,
    capable_validator_count: 4,
    start_ready: true,
    start_blockers: [],
  },
]

const ready: BenchmarkRolloutControl = {
  active_version: 5,
  desired_version: 5,
  status: 'activated',
  blocked_reason: null,
  capability_bench_version: 6,
  canary_capable_validator_count: 4,
  v3_capable_validator_count: 4,
  current_hybrid_top_five: [],
  ranked_quorum_agents: 5,
  min_ranked_quorum_agents: 5,
  qualification_converged: true,
  cohort_size: 5,
  cohort_ready_count: 5,
  priority_cohort_size: 5,
  priority_complete: true,
  members: [],
  qualification_blockers: [],
  contracts,
  available_target_versions: [6],
  active_contract_candidates: [],
  degraded_sections: [],
}

const collecting: BenchmarkRolloutControl = {
  ...ready,
  desired_version: 6,
  status: 'collecting',
  capability_bench_version: 6,
  canary_capable_validator_count: 4,
  qualification_converged: false,
  available_target_versions: [6],
  members: [
    {
      agent_id: '11111111-1111-4111-8111-111111111111',
      position: 1,
      score_count: 1,
      currently_top_five: true,
    },
  ],
}

const recoverable: BenchmarkRolloutControl = {
  ...ready,
  active_version: 4,
  desired_version: 6,
  status: 'superseded',
  available_target_versions: [5, 6],
  active_contract_candidates: [
    {
      version: 5,
      ready: true,
      ranked_quorum_agents: 5,
      min_ranked_quorum_agents: 5,
      blocked_reason: null,
    },
    {
      version: 6,
      ready: false,
      ranked_quorum_agents: 0,
      min_ranked_quorum_agents: 5,
      blocked_reason: 'the priority cohort does not yet have five ranked quorums',
    },
  ],
}

const v9Ready: BenchmarkRolloutControl = {
  ...ready,
  active_version: 8,
  desired_version: 8,
  capability_bench_version: 9,
  contracts: [
    {
      version: 8,
      minimum_screening_policy_version: 9,
      requires_screened_image: true,
      capable_validator_count: 4,
      start_ready: false,
      start_blockers: [],
    },
    {
      version: 9,
      minimum_screening_policy_version: 9,
      requires_screened_image: true,
      capable_validator_count: 4,
      start_ready: true,
      start_blockers: [],
    },
  ],
  available_target_versions: [9],
}

const v9Qualified: BenchmarkRolloutControl = {
  ...v9Ready,
  desired_version: 9,
  status: 'superseded',
  available_target_versions: [9],
  active_contract_candidates: [
    {
      version: 9,
      ready: true,
      ranked_quorum_agents: 5,
      min_ranked_quorum_agents: 5,
      blocked_reason: null,
    },
  ],
}

const v9CollectingWithStableMembership: BenchmarkRolloutControl = {
  ...v9Ready,
  desired_version: 9,
  status: 'collecting',
  qualification_converged: true,
  priority_cohort_size: 5,
  priority_complete: false,
  members: [2, 3, 2, 2, 3].map((score_count, index) => ({
    agent_id: `00000000-0000-4000-8000-00000000000${index + 1}`,
    position: index + 1,
    score_count,
    currently_top_five: true,
  })),
}

describe('BenchmarkRolloutPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    getBenchmarkRolloutControl.mockReset().mockResolvedValue(ready)
    startBenchmarkRollout.mockReset().mockResolvedValue(collecting)
    supersedeBenchmarkRollout.mockReset().mockResolvedValue({
      ...ready,
      desired_version: 6,
      status: 'superseded',
    })
    selectActiveBenchmark.mockReset().mockResolvedValue(ready)
  })

  it('shows shipped v6 as available without claiming it is active', () => {
    render(<BenchmarkRolloutPanel initialState={ready} readOnly={false} />)

    expect(screen.getByText('Benchmark v5 active')).toBeTruthy()
    expect(screen.getAllByText('Benchmark v6').length).toBeGreaterThan(0)
    expect(screen.getByText('Start benchmark v5 → v6 rollout')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Review v6 rollout' })).toBeTruthy()
  })

  it('offers the shipped v9 contract from an active v8 authority', () => {
    render(<BenchmarkRolloutPanel initialState={v9Ready} readOnly={false} />)

    expect(screen.getByText('Benchmark v8 active')).toBeTruthy()
    expect(screen.getAllByText('Benchmark v9').length).toBeGreaterThan(0)
    expect(screen.getByText('Start benchmark v8 → v9 rollout')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Review v9 rollout' })).toBeTruthy()
  })

  it('separates stable membership from the incomplete priority score gate', () => {
    render(
      <BenchmarkRolloutPanel
        initialState={v9CollectingWithStableMembership}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Top-five membership')).toBeTruthy()
    expect(screen.getByText('Stable')).toBeTruthy()
    expect(screen.getByText('Priority scoring gate')).toBeTruthy()
    expect(screen.getByText('2/5 at 3/3')).toBeTruthy()
    expect(screen.getByText('· pending')).toBeTruthy()
  })

  it('starts v9 only after the existing guarded confirmation', async () => {
    startBenchmarkRollout.mockResolvedValue({
      ...v9Ready,
      desired_version: 9,
      status: 'collecting',
    })
    render(<BenchmarkRolloutPanel initialState={v9Ready} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review v9 rollout' }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'v9 scorer, route, and validator capacity verified' },
    })
    const expected = benchmarkRolloutConfirmation('START', 9)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start rollout' }))

    await waitFor(() => expect(startBenchmarkRollout).toHaveBeenCalledTimes(1))
    expect(startBenchmarkRollout).toHaveBeenCalledWith({
      data: {
        desiredVersion: 9,
        expectedActiveVersion: 8,
        reason: 'v9 scorer, route, and validator capacity verified',
        confirmation: expected,
      },
    })
  })

  it('blocks v9 review when the exact start preflight is not ready', () => {
    const blocker = 'benchmark v9 rollout requires at least one healthy reviewed inference calibration'
    render(
      <BenchmarkRolloutPanel
        initialState={{
          ...v9Ready,
          contracts: v9Ready.contracts.map((contract) =>
            contract.version === 9
              ? { ...contract, start_ready: false, start_blockers: [blocker] }
              : contract,
          ),
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText(blocker)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Review v9 rollout' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('activates a qualified v9 contract through the separate authority guard', async () => {
    selectActiveBenchmark.mockResolvedValue({ ...v9Qualified, active_version: 9 })
    render(<BenchmarkRolloutPanel initialState={v9Qualified} readOnly={false} />)

    expect(screen.getByText('Active contract · v9 ready')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Review activation of v9' }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'v9 qualification and confirmation evidence verified' },
    })
    const expected = benchmarkRolloutConfirmation('ACTIVATE', 9)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Activate contract' }))

    await waitFor(() => expect(selectActiveBenchmark).toHaveBeenCalledTimes(1))
    expect(selectActiveBenchmark).toHaveBeenCalledWith({
      data: {
        desiredVersion: 9,
        expectedActiveVersion: 8,
        reason: 'v9 qualification and confirmation evidence verified',
        confirmation: expected,
      },
    })
  })

  it('requires reason and the version-specific confirmation before starting', async () => {
    render(<BenchmarkRolloutPanel initialState={ready} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review v6 rollout' }))
    const action = screen.getByRole('button', { name: 'Start rollout' })
    expect((action as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'v6 scorer and screened-image capacity verified' },
    })
    const expected = benchmarkRolloutConfirmation('START', 6)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    expect((action as HTMLButtonElement).disabled).toBe(false)
    expect(action.className).toContain('bg-[var(--amber)]')
    expect(action.className).not.toContain('bg-current')
    fireEvent.click(action)

    await waitFor(() => expect(startBenchmarkRollout).toHaveBeenCalledTimes(1))
    expect(startBenchmarkRollout).toHaveBeenCalledWith({
      data: {
        desiredVersion: 6,
        expectedActiveVersion: 5,
        reason: 'v6 scorer and screened-image capacity verified',
        confirmation: expected,
      },
    })
    expect(await screen.findByText('Benchmark v6 collecting')).toBeTruthy()
    expect(
      screen.getByText(
        'Eligible top-five agents are gathering v6 scores. Benchmark v5 remains authoritative until activation.',
      ),
    ).toBeTruthy()
  })

  it('renders target-version progress and keeps start hidden while collecting', () => {
    render(<BenchmarkRolloutPanel initialState={collecting} readOnly={false} />)

    expect(screen.getByText('Qualified agents · v6')).toBeTruthy()
    expect(screen.getByText('1/3 scores')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Review v6 rollout/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Review supersede' })).toBeTruthy()
  })

  it('requires a separate exact confirmation to supersede an open rollout', async () => {
    render(<BenchmarkRolloutPanel initialState={collecting} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review supersede' }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'v6 contract needs replacement before activation' },
    })
    const expected = benchmarkRolloutConfirmation('SUPERSEDE', 6)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    const action = screen.getByRole('button', { name: 'Supersede rollout' })
    expect(action.className).toContain('bg-[var(--red)]')
    expect(action.className).not.toContain('bg-current')
    fireEvent.click(action)

    await waitFor(() => expect(supersedeBenchmarkRollout).toHaveBeenCalledTimes(1))
    expect(supersedeBenchmarkRollout).toHaveBeenCalledWith({
      data: {
        desiredVersion: 6,
        reason: 'v6 contract needs replacement before activation',
        confirmation: expected,
      },
    })
  })

  it('does not offer supersede after the target already owns authority', () => {
    render(
      <BenchmarkRolloutPanel
        initialState={{ ...collecting, active_version: collecting.desired_version }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Benchmark v6 authority active')).toBeTruthy()
    expect(screen.getByText('Benchmark v6 owns active authority')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Review supersede' })).toBeNull()
  })

  it('activates a qualified historical contract separately from rollout target', async () => {
    selectActiveBenchmark.mockResolvedValue({ ...recoverable, active_version: 5 })
    render(<BenchmarkRolloutPanel initialState={recoverable} readOnly={false} />)

    expect(screen.getByText('Active contract · v5 ready')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Review activation of v5' }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'restore the completed v5 authority contract' },
    })
    const expected = benchmarkRolloutConfirmation('ACTIVATE', 5)
    fireEvent.change(screen.getByLabelText(new RegExp(expected)), {
      target: { value: expected },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Activate contract' }))

    await waitFor(() => expect(selectActiveBenchmark).toHaveBeenCalledTimes(1))
    expect(selectActiveBenchmark).toHaveBeenCalledWith({
      data: {
        desiredVersion: 5,
        expectedActiveVersion: 4,
        reason: 'restore the completed v5 authority contract',
        confirmation: expected,
      },
    })
  })

  it('says readiness is unknown rather than letting a bounded read read as "none"', () => {
    // The platform drops this section when a slow read blows its budget. An
    // empty candidate list then means "not loaded", not "nothing qualifies" --
    // and the operator must be able to tell those apart before concluding a
    // contract is not ready to own weight authority.
    render(
      <BenchmarkRolloutPanel
        initialState={{
          ...recoverable,
          active_contract_candidates: [],
          degraded_sections: ['active_contract_candidates'],
        }}
        readOnly={false}
      />,
    )

    expect(screen.getByText('Activation readiness not loaded')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Review activation/ })).toBeNull()
  })

  it('keeps all rollout mutations unavailable for read-only operators', () => {
    render(<BenchmarkRolloutPanel initialState={ready} readOnly />)

    expect(screen.getByText(/Your Backroom account is read only/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Review v6 rollout' })).toBeNull()
  })

  it('surfaces a platform readiness failure without changing local state', async () => {
    startBenchmarkRollout.mockRejectedValue(
      new Error('benchmark v6 rollout requires five eligible distinct miners'),
    )
    render(<BenchmarkRolloutPanel initialState={ready} readOnly={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Review v6 rollout' }))
    fireEvent.change(screen.getByLabelText('Operator reason'), {
      target: { value: 'attempt v6 after checking validator capacity' },
    })
    fireEvent.change(screen.getByLabelText(/START BENCHMARK V6/), {
      target: { value: benchmarkRolloutConfirmation('START', 6) },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start rollout' }))

    expect((await screen.findByRole('alert')).textContent).toContain(
      'benchmark v6 rollout requires five eligible distinct miners',
    )
    expect(screen.getByText('Benchmark v5 active')).toBeTruthy()
  })
})
