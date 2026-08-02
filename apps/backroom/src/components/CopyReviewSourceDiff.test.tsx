// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SourceDiffManifest } from '../lib/admin.schemas'
import { CopyReviewSourceDiff } from './CopyReviewSourceDiff'

vi.mock('@tanstack/react-start', () => ({ useServerFn: (serverFn: unknown) => serverFn }))
vi.mock('../server/admin.functions', () => ({
  getCopyReviewSourceDiff: vi.fn(),
  getCopyReviewSourceDiffFile: vi.fn(),
}))

import {
  getCopyReviewSourceDiff,
  getCopyReviewSourceDiffFile,
} from '../server/admin.functions'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const AGENT_ID = '11111111-1111-4111-8111-111111111111'

function manifest(): SourceDiffManifest {
  return {
    agent_id: AGENT_ID,
    reference_agent_id: '22222222-2222-4222-8222-222222222222',
    candidate_sha256: 'a'.repeat(64),
    reference_sha256: 'b'.repeat(64),
    file_count: 2,
    identical_count: 1,
    modified_count: 1,
    added_count: 0,
    removed_count: 0,
    truncated: false,
    files: [
      {
        path: 'src/lib.rs',
        status: 'identical',
        candidate_lines: 10,
        reference_lines: 10,
        added_lines: 0,
        removed_lines: 0,
        similarity: 1,
        normalized_identical: true,
      },
      {
        path: 'src/agent.rs',
        status: 'modified',
        candidate_lines: 20,
        reference_lines: 18,
        added_lines: 4,
        removed_lines: 2,
        similarity: 0.8,
        normalized_identical: false,
      },
    ],
  }
}

describe('CopyReviewSourceDiff', () => {
  it('hides source from operators without write access', () => {
    render(<CopyReviewSourceDiff agentId={AGENT_ID} canView={false} />)
    expect(screen.getByText(/requires write access/i)).toBeTruthy()
  })

  it('loads the manifest and lazily fetches a file diff on expand', async () => {
    vi.mocked(getCopyReviewSourceDiff).mockResolvedValue(manifest())
    vi.mocked(getCopyReviewSourceDiffFile).mockResolvedValue({
      agent_id: AGENT_ID,
      reference_agent_id: '22222222-2222-4222-8222-222222222222',
      path: 'src/agent.rs',
      candidate_present: true,
      reference_present: true,
      identical: false,
      diff_lines: ['@@ -1 +1 @@', '-old line', '+new line'],
      truncated: false,
    })

    render(<CopyReviewSourceDiff agentId={AGENT_ID} canView={true} />)
    fireEvent.click(screen.getByRole('button', { name: /load file-by-file diff/i }))

    // Modified files sort ahead of identical ones.
    await waitFor(() => expect(screen.getByText('src/agent.rs')).toBeTruthy())
    expect(getCopyReviewSourceDiff).toHaveBeenCalledWith({ data: { agentId: AGENT_ID } })

    fireEvent.click(screen.getByText('src/agent.rs'))
    await waitFor(() => expect(screen.getByText('+new line')).toBeTruthy())
    expect(getCopyReviewSourceDiffFile).toHaveBeenCalledWith({
      data: { agentId: AGENT_ID, path: 'src/agent.rs' },
    })
  })

  it('renders bidi control characters in a diff as visible escapes', async () => {
    // Diff bodies are untrusted miner source just like source excerpts: a
    // direction override could otherwise reorder which side of the diff a line
    // appears to be on, hiding an injected change from the reviewer.
    vi.mocked(getCopyReviewSourceDiff).mockResolvedValue(manifest())
    vi.mocked(getCopyReviewSourceDiffFile).mockResolvedValue({
      agent_id: AGENT_ID,
      reference_agent_id: '22222222-2222-4222-8222-222222222222',
      path: 'src/agent.rs',
      candidate_present: true,
      reference_present: true,
      identical: false,
      diff_lines: ['+if (admin) {‮ // pwned'],
      truncated: false,
    })

    render(<CopyReviewSourceDiff agentId={AGENT_ID} canView={true} />)
    fireEvent.click(screen.getByRole('button', { name: /load file-by-file diff/i }))
    fireEvent.click(await screen.findByText('src/agent.rs'))

    await waitFor(() => expect(screen.getByText(/\\u202E/)).toBeTruthy())
  })

  it('does not fetch a file diff for an identical file', async () => {
    vi.mocked(getCopyReviewSourceDiff).mockResolvedValue(manifest())
    render(<CopyReviewSourceDiff agentId={AGENT_ID} canView={true} />)
    fireEvent.click(screen.getByRole('button', { name: /load file-by-file diff/i }))
    await waitFor(() => expect(screen.getByText('src/lib.rs')).toBeTruthy())

    fireEvent.click(screen.getByText('src/lib.rs'))
    await waitFor(() =>
      expect(screen.getByText(/byte-for-byte identical/i)).toBeTruthy(),
    )
    expect(getCopyReviewSourceDiffFile).not.toHaveBeenCalled()
  })
})
