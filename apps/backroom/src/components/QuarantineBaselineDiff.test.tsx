// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BaselineDiffManifest } from '../lib/admin.schemas'
import { QuarantineBaselineDiff } from './QuarantineBaselineDiff'

vi.mock('@tanstack/react-start', () => ({ useServerFn: (serverFn: unknown) => serverFn }))
vi.mock('../server/admin.functions', () => ({
  getScreeningBaselineDiff: vi.fn(),
  readScreeningBaselineDiffFile: vi.fn(),
}))

import {
  getScreeningBaselineDiff,
  readScreeningBaselineDiffFile,
} from '../server/admin.functions'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const AGENT_ID = '11111111-1111-4111-8111-111111111111'

function manifest(overrides: Partial<BaselineDiffManifest> = {}): BaselineDiffManifest {
  return {
    agent_id: AGENT_ID,
    artifact_sha256: 'a'.repeat(64),
    baseline: {
      source: 'https://github.com/ditto-assistant/dittobench-starter-kit',
      revision: 'b'.repeat(40),
      commit_set_sha256: 'c'.repeat(64),
      commit_count: 24,
    },
    file_count: 3,
    identical_count: 1,
    modified_count: 1,
    added_count: 1,
    removed_count: 0,
    stock_kit_count: 2,
    custom_file_count: 1,
    custom_added_lines: 12,
    path_aligned: false,
    truncated: false,
    files: [
      {
        path: 'Cargo.toml',
        status: 'identical',
        candidate_lines: 10,
        reference_lines: 10,
        added_lines: 0,
        removed_lines: 0,
        similarity: 1,
        normalized_identical: true,
        stock_kit: true,
      },
      {
        // Kit code from an older revision: differs from the tip but is not the
        // miner's work, so it must be hidden with the rest of the stock kit.
        path: 'src/baseline.rs',
        status: 'modified',
        candidate_lines: 700,
        reference_lines: 739,
        added_lines: 3,
        removed_lines: 42,
        similarity: 0.9,
        normalized_identical: false,
        stock_kit: true,
      },
      {
        path: 'src/solver.rs',
        status: 'added',
        candidate_lines: 12,
        reference_lines: 0,
        added_lines: 12,
        removed_lines: 0,
        similarity: 0,
        normalized_identical: false,
        stock_kit: false,
      },
    ],
    ...overrides,
  }
}

describe('QuarantineBaselineDiff', () => {
  it('leads with the custom surface and hides stock kit files', async () => {
    vi.mocked(getScreeningBaselineDiff).mockResolvedValue(manifest())
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView />)

    fireEvent.click(screen.getByRole('button', { name: /load diff vs\. starter kit/i }))

    // The headline a reviewer triages on.
    expect(await screen.findByText('12')).toBeTruthy()
    expect(screen.getByText(/custom lines across 1 file/i)).toBeTruthy()

    // Only the miner's own file is listed; both stock files are collapsed away.
    expect(screen.getByText('src/solver.rs')).toBeTruthy()
    expect(screen.queryByText('Cargo.toml')).toBeNull()
    expect(screen.queryByText('src/baseline.rs')).toBeNull()
    expect(screen.getByRole('button', { name: /show 2 stock kit files/i })).toBeTruthy()
  })

  it('reveals stock kit files on request and labels the non-identical ones', async () => {
    vi.mocked(getScreeningBaselineDiff).mockResolvedValue(manifest())
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView />)
    fireEvent.click(screen.getByRole('button', { name: /load diff vs\. starter kit/i }))
    fireEvent.click(await screen.findByRole('button', { name: /show 2 stock kit files/i }))

    expect(screen.getByText('Cargo.toml')).toBeTruthy()
    expect(screen.getByText('src/baseline.rs')).toBeTruthy()
    // A modified-but-stock file needs the badge; an identical one is obvious.
    expect(screen.getByTitle(/matches starter-kit code at another revision/i)).toBeTruthy()
  })

  it('loads a bounded unified diff for a custom file', async () => {
    vi.mocked(getScreeningBaselineDiff).mockResolvedValue(manifest())
    vi.mocked(readScreeningBaselineDiffFile).mockResolvedValue({
      agent_id: AGENT_ID,
      path: 'src/solver.rs',
      candidate_present: true,
      reference_present: false,
      identical: false,
      stock_kit: false,
      diff_lines: ['@@ -0,0 +1 @@', '+fn solve_as_of() -> u64 { 42 }'],
      truncated: false,
    })
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView />)
    fireEvent.click(screen.getByRole('button', { name: /load diff vs\. starter kit/i }))
    fireEvent.click(await screen.findByText('src/solver.rs'))

    await waitFor(() =>
      expect(screen.getByText(/\+fn solve_as_of/)).toBeTruthy(),
    )
    expect(vi.mocked(readScreeningBaselineDiffFile)).toHaveBeenCalledWith({
      data: { agentId: AGENT_ID, path: 'src/solver.rs' },
    })
  })

  it('renders bidi control characters as visible escapes', async () => {
    vi.mocked(getScreeningBaselineDiff).mockResolvedValue(manifest())
    vi.mocked(readScreeningBaselineDiffFile).mockResolvedValue({
      agent_id: AGENT_ID,
      path: 'src/solver.rs',
      candidate_present: true,
      reference_present: false,
      identical: false,
      stock_kit: false,
      // A trojan-source payload: the override would visually reorder the line.
      diff_lines: ['+if (admin) {‮ // pwned'],
      truncated: false,
    })
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView />)
    fireEvent.click(screen.getByRole('button', { name: /load diff vs\. starter kit/i }))
    fireEvent.click(await screen.findByText('src/solver.rs'))

    await waitFor(() => expect(screen.getByText(/\\u202E/)).toBeTruthy())
  })

  it('never fetches source without write access', () => {
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView={false} />)
    expect(screen.getByText(/requires write access/i)).toBeTruthy()
    expect(vi.mocked(getScreeningBaselineDiff)).not.toHaveBeenCalled()
  })

  it('surfaces path realignment so the comparison is not silently reshaped', async () => {
    vi.mocked(getScreeningBaselineDiff).mockResolvedValue(
      manifest({ path_aligned: true }),
    )
    render(<QuarantineBaselineDiff agentId={AGENT_ID} canView />)
    fireEvent.click(screen.getByRole('button', { name: /load diff vs\. starter kit/i }))

    expect(await screen.findByText(/paths realigned/i)).toBeTruthy()
  })
})
