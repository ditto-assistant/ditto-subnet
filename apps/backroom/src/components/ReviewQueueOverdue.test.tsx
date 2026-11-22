// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ReviewQueueOverdue } from './ReviewQueueOverdue'

afterEach(cleanup)

describe('ReviewQueueOverdue', () => {
  it('names each clock past its SLO with the oldest held agent', () => {
    render(
      <ReviewQueueOverdue
        clocks={[
          {
            clock: 'benchmark_overfit',
            backlog_count: 3,
            overdue_count: 3,
            max_age_threshold_hours: 72,
            oldest_age_hours: 3072,
            oldest_agent_id: '1914239c-9279-4a71-9fe4-0dd676063c33',
            oldest_reason: 'escalation',
            p95_age_hours: 3000,
            p95_threshold_hours: 48,
            p95_exceeds_threshold: true,
          },
        ]}
        error={null}
      />,
    )
    expect(screen.getByText('1 review clock is past SLO')).toBeTruthy()
    expect(screen.getByText('Benchmark overfit')).toBeTruthy()
    expect(screen.getByText('3 of 3 older than 3d')).toBeTruthy()
    expect(screen.getByText('oldest 128d (escalation)')).toBeTruthy()
    expect(screen.getByText('1914239c')).toBeTruthy()
  })

  it('reports a healthy queue and a failed read distinctly', () => {
    const { rerender } = render(<ReviewQueueOverdue clocks={[]} error={null} />)
    expect(screen.getByText('Every review clock is within its SLO.')).toBeTruthy()
    rerender(<ReviewQueueOverdue clocks={[]} error="platform 503" />)
    expect(screen.queryByText('Every review clock is within its SLO.')).toBeNull()
    expect(screen.getByText('Queue SLO read failed: platform 503')).toBeTruthy()
  })
})
